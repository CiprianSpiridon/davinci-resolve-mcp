"""Fusion keyframe-animation tools for the DaVinci Resolve MCP server.

This module owns *keyframing* a Fusion tool input on a timeline item's
composition. Unlike a static ``SetInput``, animating an input requires a
modifier: a plain ``SetInput`` on a virgin (unanimated) input only changes its
constant value and does NOT create a keyframe. So ``fusion_add_keyframe``
attaches a ``BezierSpline`` modifier to the input the first time it is keyed
(``tool.AddModifier(input, "BezierSpline")``), then writes the keyframe on the
resulting spline (``spline.SetKeyFrames({frame: value}, replace)``, or the
``tool[input][frame] = value`` subscript as a fallback). Subsequent calls on the
same input detect the existing modifier and only add the new key — the modifier
is never re-attached.

It reuses the module-level accessors defined by the Fusion node engine —
:func:`davinci_resolve_mcp.tools.fusion._get_fusion_comp` and
:func:`~davinci_resolve_mcp.tools.fusion._resolve_tool` — plus the shared
timeline-item locator, so the ownership contract stays in ``tools/fusion.py``.

All tools reach Resolve lazily inside their bodies via ``_get_timeline_item()``
— nothing here imports ``DaVinciResolveScript`` or touches a live Resolve
instance at import time — and every tool ALWAYS returns a ``str`` (an ``Error:``
string on failure), never raising out of the tool.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from ..app import mcp
from ..helpers import _coerce_value, _get_timeline_item
from .fusion import _get_fusion_comp, _resolve_tool


def _input_for(tool, input_name: str):
    """Return the FusionInput object for ``input_name`` on ``tool``, or ``None``.

    Prefers a match on the input list's ``INPS_ID`` attribute and falls back to
    ``tool[input_name]`` subscript access. Never raises.
    """
    try:
        inputs = tool.GetInputList() or {}
    except Exception:
        inputs = {}
    for inp in inputs.values():
        try:
            if (inp.GetAttrs() or {}).get("INPS_ID") == input_name:
                return inp
        except Exception:
            continue
    try:
        return tool[input_name]
    except Exception:
        return None


def _existing_spline(inp):
    """Return the modifier tool already animating ``inp``, or ``None``.

    An animated input is connected to the output of its modifier (e.g. a
    ``BezierSpline``); ``GetConnectedOutput().GetTool()`` recovers that modifier
    so a second keyframe can reuse it instead of re-adding a modifier. Never
    raises — returns ``None`` when the input is a plain constant.
    """
    if inp is None:
        return None
    try:
        output = inp.GetConnectedOutput()
    except Exception:
        output = None
    if output is None:
        return None
    try:
        return output.GetTool()
    except Exception:
        return None


def _keyframe_count(spline) -> Optional[int]:
    """Return the number of keys on ``spline`` via ``GetKeyFrames()``, or None."""
    if spline is None:
        return None
    try:
        keys = spline.GetKeyFrames()
    except Exception:
        return None
    if keys is None:
        return None
    try:
        return len(keys)
    except Exception:
        return None


@mcp.tool()
def fusion_add_keyframe(
    tool_name: str,
    input_name: str,
    frame: int,
    value: str,
    comp_index: int = 1,
    replace: bool = False,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Keyframe a Fusion tool input (animate it with a BezierSpline).

    A plain ``SetInput`` on a virgin input only changes its constant value — it
    does NOT create animation. So the first time an input is keyed this attaches
    a ``BezierSpline`` modifier (``tool.AddModifier(input, "BezierSpline")``),
    then writes the key on the spline. A second call on the SAME input detects
    the existing modifier and only adds the new key — the modifier is never
    re-attached.

    The string ``value`` is auto-coerced to int/float/bool where it looks
    numeric/boolean (e.g. "1.0" -> 1, "true" -> True); otherwise it is passed
    through as a string. After keying, the value is read back at ``frame`` and
    the spline's keyframe count is reported.

    Parameters:
    - tool_name: the tool whose input is being animated.
    - input_name: input ID (e.g. "Blend", "Size", "Angle", "Center").
    - frame: the frame (time) at which to set the keyframe.
    - value: the keyframe value, as a string (auto-coerced).
    - comp_index: 1-based Fusion composition index on the item (default: 1).
    - replace: replace ALL existing keys on the spline instead of merging
      (default: False — add/merge this one key).
    - track_type: "video" (default), "audio", or "subtitle".
    - track_index: 1-based track index (default: 1).
    - item_index: 0-based index of the item within that track (default: 0).
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        comp = _get_fusion_comp(item, comp_index)
        tool = _resolve_tool(comp, tool_name)

        inp = _input_for(tool, input_name)
        if inp is None:
            return (
                f"Error: no input '{input_name}' on tool '{tool_name}'. "
                f"Use fusion_list_inputs to discover valid input IDs."
            )

        coerced = _coerce_value(value)
        key_frame = float(frame)

        # Reuse an existing modifier when the input is already animated;
        # otherwise attach a BezierSpline (a plain SetInput would not animate).
        spline = _existing_spline(inp)
        added_modifier = False
        if spline is None:
            try:
                ok = tool.AddModifier(input_name, "BezierSpline")
            except Exception as add_err:
                return (
                    f"Error: could not add BezierSpline modifier to "
                    f"'{input_name}' on '{tool_name}': {add_err}"
                )
            if not ok:
                return (
                    f"Error: AddModifier returned falsy for '{input_name}' on "
                    f"'{tool_name}' — the input may not accept a BezierSpline."
                )
            added_modifier = True
            spline = _existing_spline(_input_for(tool, input_name))

        # Write the key. Prefer the spline's SetKeyFrames; fall back to the
        # input subscript (tool[input][frame] = value) when unavailable.
        keyed = False
        if spline is not None:
            try:
                spline.SetKeyFrames({key_frame: coerced}, bool(replace))
                keyed = True
            except Exception:
                keyed = False
        if not keyed:
            try:
                tool[input_name][key_frame] = coerced
                keyed = True
            except Exception as key_err:
                return (
                    f"Error: could not set keyframe at frame {frame} on "
                    f"'{input_name}' of '{tool_name}': {key_err}"
                )

        try:
            readback = tool.GetInput(input_name, key_frame)
        except Exception:
            readback = None

        count = _keyframe_count(spline)

        return json.dumps({
            "success": True,
            "tool_name": tool_name,
            "input_name": input_name,
            "frame": frame,
            "value": coerced,
            "readback": readback,
            "keyframe_count": count,
            "added_modifier": added_modifier,
            "modifier": "BezierSpline",
        }, indent=2, default=str)
    except Exception as e:
        return f"Error: {e}"
