"""Keyframe-animation tools for the DaVinci Resolve MCP server.

This module owns two distinct keyframe surfaces:

1. *Edit-page transform keyframes* on a ``TimelineItem`` — ``get_transform_keyframes``
   reads back the Inspector's Transform/Crop/Composite properties (Pan, Tilt,
   ZoomX/Y, RotationAngle, AnchorPointX/Y, CropLeft/Right/Top/Bottom, Opacity) via
   ``GetKeyframeCount`` + ``GetKeyframeAtIndex`` / ``GetPropertyAtKeyframeIndex``.
   NOTE: this Resolve scripting API does NOT expose a usable
   ``TimelineItem.AddKeyframe`` / ``DeleteKeyframe`` — Edit-page transform keys
   cannot be *created* from scripting on the tested build (Studio 21.0.2), so
   ``add_transform_keyframe`` fails gracefully and points at the Fusion path
   (``animate_clip_transform``) for animated moves or ``set_transform`` for a
   static value. ``set_keyframe_mode`` is the *Color* page keyframe mode and is
   unrelated to these Edit-page transform properties.

2. *Fusion input keyframes* (below): keyframing a Fusion tool input on a
   timeline item's composition.

The Fusion path owns *keyframing* a Fusion tool input on a timeline item's
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
from typing import Optional

from ..app import mcp
from ..helpers import _check_choice, _coerce_value, _get_timeline_item
from .fusion import _get_fusion_comp, _resolve_tool

# Edit-page transform properties — the Inspector's Transform/Crop/Composite
# vocabulary, distinct from the Fusion spline path above (which keys a Fusion
# tool input on the item's comp). These can be READ back
# (``get_transform_keyframes``) but NOT created from scripting on this build:
# ``TimelineItem.AddKeyframe``/``DeleteKeyframe`` are not exposed.
TRANSFORM_KEYFRAME_PROPERTIES = (
    "Pan", "Tilt", "ZoomX", "ZoomY", "RotationAngle",
    "AnchorPointX", "AnchorPointY",
    "CropLeft", "CropRight", "CropTop", "CropBottom",
    "Opacity",
)

# Interpolation vocabulary accepted by ``TimelineItem.SetKeyframeInterpolation``.
KEYFRAME_INTERPOLATIONS = ("Linear", "Bezier", "EaseIn", "EaseOut", "EaseInOut")


def _read_transform_keyframes(item, prop):
    """Read back the keyframes for ``prop`` on ``item`` as ``(count, keys)``.

    Uses ``GetKeyframeCount`` + ``GetKeyframeAtIndex`` /
    ``GetPropertyAtKeyframeIndex``. Returns ``(0, [])`` when the property has no
    keyframes (or the count is unavailable). Never raises — an unreadable index
    contributes a ``None`` frame/value rather than aborting the read.
    """
    try:
        count = item.GetKeyframeCount(prop)
    except Exception:
        count = None
    if not count:
        return 0, []
    keys = []
    for i in range(count):
        try:
            kf = item.GetKeyframeAtIndex(prop, i)
        except Exception:
            kf = None
        frame = kf.get("frame") if isinstance(kf, dict) else kf
        try:
            val = item.GetPropertyAtKeyframeIndex(prop, i)
        except Exception:
            val = None
        keys.append({"frame": frame, "value": val})
    return count, keys


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
                # BezierSpline.SetKeyFrames expects a table of subtables whose
                # first element is the keyframe value (frame -> {value}). In the
                # Fusion Python bridge a list becomes a Lua array-table, so
                # ``[coerced]`` serializes to ``[key_frame] = { coerced }`` —
                # the documented "simple keyframe" form. A bare scalar
                # (``{key_frame: coerced}``) is the wrong shape and is silently
                # dropped by the bridge.
                spline.SetKeyFrames({key_frame: [coerced]}, bool(replace))
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


# ── Edit-page transform keyframes (TimelineItem) ────────────────────────────


@mcp.tool()
def add_transform_keyframe(
    property_name: str,
    frame: int,
    value: str,
    interpolation: str = "",
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Attempt an Edit-page transform keyframe on a timeline item.

    IMPORTANT: this Resolve scripting API does NOT expose a usable
    ``TimelineItem.AddKeyframe`` — Edit-page Inspector transform keyframes
    (Pan/Tilt/Zoom/Crop/Opacity) are not scriptable on the tested build
    (Studio 21.0.2), so this tool returns an ``Error:`` explaining that rather
    than creating a key. ``set_keyframe_mode`` does NOT enable it — that is the
    *Color* page keyframe mode and is unrelated to these properties.

    For an ANIMATED move (Ken-Burns / push-in), use ``animate_clip_transform``,
    which drives a keyframed Fusion Transform on a video-only carrier clip — the
    only scriptable path to animated Edit-page-style motion. For a STATIC value,
    use ``set_transform`` (or ``item.SetProperty("ZoomX"/"ZoomY", ...)``), which
    works reliably.

    The string ``value`` is auto-coerced to int/float/bool where it looks
    numeric/boolean (e.g. "1.0" -> 1, "0.5" -> 0.5). ``interpolation`` is
    validated but only applied if a key is created.

    Parameters:
    - property_name: one of "Pan", "Tilt", "ZoomX", "ZoomY", "RotationAngle",
      "AnchorPointX", "AnchorPointY", "CropLeft", "CropRight", "CropTop",
      "CropBottom", "Opacity".
    - frame: the (timeline) frame at which to place the keyframe.
    - value: the keyframe value, as a string (auto-coerced).
    - interpolation: optional — one of "Linear", "Bezier", "EaseIn", "EaseOut",
      "EaseInOut". Empty string (default) leaves interpolation at its default.
    - track_type: "video" (default), "audio", or "subtitle".
    - track_index: 1-based track index (default: 1).
    - item_index: 0-based index of the item within that track (default: 0).
    """
    try:
        prop, err = _check_choice(
            property_name, TRANSFORM_KEYFRAME_PROPERTIES, "transform property"
        )
        if err:
            return err

        interp = None
        if interpolation:
            interp, ierr = _check_choice(
                interpolation, KEYFRAME_INTERPOLATIONS, "keyframe interpolation"
            )
            if ierr:
                return ierr

        item = _get_timeline_item(track_type, track_index, item_index)

        coerced = _coerce_value(value)

        # NOTE: Resolve PyRemoteObjects return True for hasattr(obj, ANYTHING),
        # so a hasattr guard is useless — probe for a real callable instead.
        add_kf = getattr(item, "AddKeyframe", None)
        if not callable(add_kf):
            return (
                "Error: this Resolve build exposes no usable "
                "TimelineItem.AddKeyframe — Edit-page transform keyframes are not "
                "scriptable here. Use an animated Fusion Transform instead "
                "(animate_clip_transform), or set a STATIC value with "
                "set_transform."
            )
        try:
            added = add_kf(prop, frame, coerced)
        except TypeError:
            added = None
        if not added:
            return (
                "Error: could not add an Edit-page transform keyframe — not "
                "supported by this API. Use a Fusion Transform "
                "(animate_clip_transform) or a static set_transform."
            )

        interp_applied = None
        if interp is not None:
            try:
                interp_applied = bool(
                    item.SetKeyframeInterpolation(prop, frame, interp)
                )
            except Exception:
                interp_applied = False

        count, keys = _read_transform_keyframes(item, prop)
        return json.dumps({
            "success": True,
            "property": prop,
            "frame": frame,
            "value": coerced,
            "interpolation": interp,
            "interpolation_applied": interp_applied,
            "keyframe_count": count,
            "keyframes": keys,
        }, indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def get_transform_keyframes(
    property_name: str,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Read back the Edit-page transform keyframes for a property on an item.

    Returns the keyframe count and, for each key, its frame and value (via
    ``GetKeyframeCount`` + ``GetKeyframeAtIndex`` / ``GetPropertyAtKeyframeIndex``).
    A property with no keyframes yields ``"count": 0`` and an empty ``keyframes``
    list — this never raises.

    Parameters:
    - property_name: one of "Pan", "Tilt", "ZoomX", "ZoomY", "RotationAngle",
      "AnchorPointX", "AnchorPointY", "CropLeft", "CropRight", "CropTop",
      "CropBottom", "Opacity".
    - track_type: "video" (default), "audio", or "subtitle".
    - track_index: 1-based track index (default: 1).
    - item_index: 0-based index of the item within that track (default: 0).
    """
    try:
        prop, err = _check_choice(
            property_name, TRANSFORM_KEYFRAME_PROPERTIES, "transform property"
        )
        if err:
            return err

        item = _get_timeline_item(track_type, track_index, item_index)
        count, keys = _read_transform_keyframes(item, prop)
        return json.dumps({
            "success": True,
            "property": prop,
            "count": count,
            "keyframes": keys,
        }, indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def delete_transform_keyframe(
    property_name: str,
    frame: int,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Delete an Edit-page transform keyframe at a frame on a timeline item.

    Calls ``item.DeleteKeyframe(property, frame)`` and reports the remaining
    keyframe count for the property. Deleting when no keyframe exists at that
    frame (or the property has no keyframes at all) returns ``"deleted": false``
    with the current count rather than raising.

    Parameters:
    - property_name: one of "Pan", "Tilt", "ZoomX", "ZoomY", "RotationAngle",
      "AnchorPointX", "AnchorPointY", "CropLeft", "CropRight", "CropTop",
      "CropBottom", "Opacity".
    - frame: the (timeline) frame of the keyframe to delete.
    - track_type: "video" (default), "audio", or "subtitle".
    - track_index: 1-based track index (default: 1).
    - item_index: 0-based index of the item within that track (default: 0).
    """
    try:
        prop, err = _check_choice(
            property_name, TRANSFORM_KEYFRAME_PROPERTIES, "transform property"
        )
        if err:
            return err

        item = _get_timeline_item(track_type, track_index, item_index)
        deleted = False
        if callable(getattr(item, "DeleteKeyframe", None)):
            try:
                deleted = bool(item.DeleteKeyframe(prop, frame))
            except Exception:
                deleted = False

        count, keys = _read_transform_keyframes(item, prop)
        return json.dumps({
            "success": True,
            "property": prop,
            "frame": frame,
            "deleted": deleted,
            "keyframe_count": count,
            "keyframes": keys,
        }, indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"
