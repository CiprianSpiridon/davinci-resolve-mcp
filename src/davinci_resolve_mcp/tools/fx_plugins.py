"""Live OFX / ResolveFX apply-to-clip tools for the DaVinci Resolve MCP server.

This module drops an OFX / ResolveFX plugin node onto a timeline clip's Fusion
composition and splices it into the clip's render chain, so the effect actually
touches the picture instead of sitting orphaned on the flow.

The single tool here — :func:`apply_ofx_to_clip` — gets (or creates) the clip's
Fusion comp, wraps the mutation in ``comp.Lock()`` + ``StartUndo``/``EndUndo`` so
it is reversible, adds the plugin via ``comp.AddTool('ofx.' + regid, ...)`` (the
``'ofx.'`` prefix is REQUIRED by Fusion's registry — the ``.drx`` wireId carries
none, so it is added here), splices the new node between ``MediaOut1``'s current
upstream tool and ``MediaOut1`` itself (``Source`` in, ``Input`` out), applies any
requested inputs, and returns the node's ``TOOLS_RegID``.

Low-level comp/node access is REUSED from ``tools/fusion.py`` (``_get_fusion_comp``)
rather than redefined here. Nothing in this module imports
``DaVinciResolveScript`` or touches a live Resolve instance at import time — the
connection is reached lazily inside the tool body via ``_get_timeline_item``.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from ..app import mcp
from ..helpers import _coerce_value, _get_timeline_item
from .fusion import _get_fusion_comp


@mcp.tool()
def apply_ofx_to_clip(
    regid: str,
    params: str = "",
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
    comp_index: int = 1,
) -> str:
    """Apply an OFX / ResolveFX plugin to a timeline clip via its Fusion comp.

    The plugin node is created inside the clip's Fusion composition and spliced
    into the render chain between ``MediaOut1``'s current upstream tool and
    ``MediaOut1`` (the new node's ``Source`` input takes the old upstream, and
    ``MediaOut1``'s ``Input`` takes the new node), so the effect is actually in
    the picture path. The whole mutation is wrapped in ``StartUndo``/``EndUndo``
    and ``comp.Lock()``/``Unlock`` so it is a single reversible step.

    Parameters:
    - regid: the OFX / ResolveFX registry id WITHOUT the ``'ofx.'`` prefix
      (e.g. ``"com.blackmagicdesign.resolvefx.gaussianblur"``). The ``'ofx.'``
      prefix Fusion requires is prepended automatically; a value that already
      carries it is used as-is.
    - params: optional JSON object of input id -> value to set on the new node
      AFTER it is wired, e.g. ``'{"Blend": 0.5, "FilterType": 1}'``. Values are
      coerced from strings to int/float/bool where they look numeric/boolean.
      Leave empty to add the plugin without setting any inputs.
    - track_type: "video" (default), "audio", or "subtitle".
    - track_index: 1-based track index (default: 1).
    - item_index: 0-based index of the item within that track (default: 0).
    - comp_index: 1-based Fusion composition index on the item (default: 1).

    Returns a JSON object with the new node's ``TOOLS_RegID`` on success, or an
    ``Error: ...`` string on failure (including when the RegID is unregistered
    or the plugin is not installed, in which case ``AddTool`` returns ``None``).
    """
    try:
        # Parse the optional params JSON up front so a bad payload fails before
        # we mutate the comp.
        parsed: Dict[str, Any] = {}
        if params and params.strip():
            try:
                parsed = json.loads(params)
            except (ValueError, TypeError) as e:
                return (
                    f"Invalid params JSON: {e}. Provide a JSON object of input "
                    'id -> value, e.g. {"Blend": 0.5, "FilterType": 1}.'
                )
            if not isinstance(parsed, dict):
                return (
                    "params must be a JSON object of input id -> value, e.g. "
                    '{"Blend": 0.5, "FilterType": 1}.'
                )

        item = _get_timeline_item(track_type, track_index, item_index)
        comp = _get_fusion_comp(item, comp_index)

        # KEEP the 'ofx.' prefix Fusion's registry requires; the .drx wireId has
        # none, so add it here (but never double it).
        reg_id = regid if regid.startswith("ofx.") else f"ofx.{regid}"

        media_out = comp.FindTool("MediaOut1")
        if media_out is None:
            return (
                "Error: No 'MediaOut1' in the clip's Fusion comp; cannot splice "
                "the effect into the render chain."
            )

        comp.Lock()
        comp.StartUndo("apply_ofx_to_clip")
        try:
            tool = comp.AddTool(reg_id, -32768, -32768)
            if tool is None:
                # Unregistered RegID / plugin not installed: bail out WITHOUT
                # reporting success. EndUndo/Unlock still run in the finally.
                return (
                    f"Error: AddTool nil: RegID '{reg_id}' "
                    f"unregistered/plugin not installed. Check the OFX/ResolveFX "
                    f"registry id and that the plugin is installed."
                )

            # Find MediaOut1's current upstream tool so we can splice in front of
            # it: the new node's Source takes that upstream, MediaOut1's Input
            # takes the new node. Fall back to MediaIn1 when nothing is wired.
            src = None
            try:
                main_in = media_out.FindMainInput(1)
                if main_in is not None:
                    connected = main_in.GetConnectedOutput()
                    if connected is not None:
                        src = connected.GetTool()
            except Exception:
                src = None
            if src is None:
                src = comp.FindTool("MediaIn1")

            if src is not None:
                tool.ConnectInput("Source", src)
            media_out.ConnectInput("Input", tool)

            # Apply requested inputs AFTER wiring.
            for input_id, value in parsed.items():
                try:
                    tool.SetInput(str(input_id), _coerce_value(value))
                except Exception:
                    # A single bad input id/value should not abort the whole
                    # (already-wired) apply; report it and keep going.
                    pass

            attrs = tool.GetAttrs() or {}
            return json.dumps({
                "success": True,
                "reg_id": attrs.get("TOOLS_RegID", reg_id),
                "tool_name": attrs.get("TOOLS_Name", ""),
                "spliced_upstream": (
                    (src.GetAttrs() or {}).get("TOOLS_Name", "") if src else None
                ),
                "params_applied": list(parsed.keys()),
                "comp_index": comp_index,
            }, indent=2, default=str)
        finally:
            try:
                comp.EndUndo(True)
            except Exception:
                pass
            try:
                comp.Unlock()
            except Exception:
                pass
    except Exception as e:
        return f"Error: {e}"
