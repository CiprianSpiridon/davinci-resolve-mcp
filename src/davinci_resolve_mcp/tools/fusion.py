"""Fusion composition-management tools for the DaVinci Resolve MCP server.

This module owns Fusion *comp management* on timeline items — listing,
adding, importing, exporting, loading, deleting, and renaming Fusion
compositions attached to a ``TimelineItem`` (``TimelineItem.GetFusionCompCount``
/ ``GetFusionCompNameList`` / ``AddFusionComp`` / ``ImportFusionComp`` /
``ExportFusionComp`` / ``LoadFusionCompByName`` / ``DeleteFusionCompByName`` /
``RenameFusionCompByName``) — plus ``create_fusion_clip``
(``Timeline.CreateFusionClip``).

This module also hosts the live Fusion *node engine* — adding, wiring, and
reading tools (nodes) inside a comp attached to a timeline item — plus two
module-level accessors (:func:`_get_fusion_comp` and :func:`_resolve_tool`)
that sibling modules (``tools/keyframes.py``, ``tools/fx_plugins.py``) reuse.

On top of the node engine it hosts three comp-authoring tools:
``set_title_text`` (locate the Text+ node by ``TOOLS_RegID`` and update its
``StyledText``/``Size``/``Center``), ``apply_macro_to_clip`` (apply a
``.setting`` macro to a clip's comp via a FILE round-trip — never passing an
in-memory settings table across the bridge), and ``execute_fusion_lua`` (a Lua
escape hatch via ``Resolve.Fusion().Execute``).

Per the ownership contract, ``insert_fusion_generator`` and
``insert_fusion_title`` (playhead-insert operations) live in
``tools/timeline_edit.py``, not here.

All tools reach Resolve lazily via ``_conn()``/``_require_timeline()``/
``_get_timeline_item()`` inside their bodies — nothing here imports
``DaVinciResolveScript`` or touches a live Resolve instance at import time.
"""

from __future__ import annotations

import json
import os
from typing import Any, List, Optional

from ..app import mcp
from ..helpers import _coerce_value, _conn, _get_timeline_item, _ok, _require_timeline


@mcp.tool()
def get_fusion_comp_list(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Get all Fusion compositions associated with a timeline item.

    Parameters:
    - track_type: "video" (default), "audio", or "subtitle".
    - track_index: 1-based track index (default: 1).
    - item_index: 0-based index of the item within that track (default: 0).
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        count = item.GetFusionCompCount() or 0
        names = item.GetFusionCompNameList() or []
        return json.dumps({
            "item_name": item.GetName(),
            "fusion_comp_count": count,
            "fusion_comp_names": list(names),
        }, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def add_fusion_comp(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Add a new Fusion composition to a timeline item.

    Parameters:
    - track_type: "video" (default), "audio", or "subtitle".
    - track_index: 1-based track index (default: 1).
    - item_index: 0-based index of the item within that track (default: 0).
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        comp = item.AddFusionComp()
        return _ok(comp, f"Fusion composition added to '{item.GetName()}'", "Failed to add Fusion composition")
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def import_fusion_comp(
    comp_path: str,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Import a Fusion composition from a file into a timeline item.

    Parameters:
    - comp_path: absolute path to the .comp or .setting file to import.
    - track_type: "video" (default), "audio", or "subtitle".
    - track_index: 1-based track index (default: 1).
    - item_index: 0-based index of the item within that track (default: 0).
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        comp = item.ImportFusionComp(comp_path)
        return _ok(
            comp,
            f"Fusion comp imported from '{comp_path}'",
            f"Failed to import Fusion composition from '{comp_path}'. Check the file path.",
        )
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def export_fusion_comp(
    export_path: str,
    comp_index: int = 1,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Export a Fusion composition from a timeline item to a file.

    Parameters:
    - export_path: destination file path for the exported .comp/.setting file.
    - comp_index: 1-based Fusion composition index on the item (default: 1).
    - track_type: "video" (default), "audio", or "subtitle".
    - track_index: 1-based track index (default: 1).
    - item_index: 0-based index of the item within that track (default: 0).
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        success = item.ExportFusionComp(export_path, comp_index)
        return _ok(
            success,
            f"Fusion comp {comp_index} exported to '{export_path}'",
            f"Failed to export Fusion composition to '{export_path}'. Check the destination path.",
        )
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def load_fusion_comp(
    comp_name: str,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Load a named Fusion composition as the active composition on a timeline item.

    Parameters:
    - comp_name: name of the Fusion composition to load.
    - track_type: "video" (default), "audio", or "subtitle".
    - track_index: 1-based track index (default: 1).
    - item_index: 0-based index of the item within that track (default: 0).
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        comp = item.LoadFusionCompByName(comp_name)
        return _ok(comp, f"Loaded Fusion composition '{comp_name}'", f"Failed to load Fusion composition '{comp_name}'")
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def delete_fusion_comp(
    comp_name: str,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Delete a named Fusion composition from a timeline item.

    Parameters:
    - comp_name: name of the Fusion composition to delete.
    - track_type: "video" (default), "audio", or "subtitle".
    - track_index: 1-based track index (default: 1).
    - item_index: 0-based index of the item within that track (default: 0).
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        success = item.DeleteFusionCompByName(comp_name)
        return _ok(success, f"Deleted Fusion composition '{comp_name}'", f"Failed to delete Fusion composition '{comp_name}'")
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def rename_fusion_comp(
    old_name: str,
    new_name: str,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Rename a Fusion composition on a timeline item.

    Parameters:
    - old_name: current name of the Fusion composition.
    - new_name: new name for the composition.
    - track_type: "video" (default), "audio", or "subtitle".
    - track_index: 1-based track index (default: 1).
    - item_index: 0-based index of the item within that track (default: 0).
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        success = item.RenameFusionCompByName(old_name, new_name)
        return _ok(success, f"Renamed '{old_name}' to '{new_name}'", f"Failed to rename Fusion composition '{old_name}'")
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def create_fusion_clip(
    track_type: str = "video",
    track_index: int = 1,
    item_indices: Optional[List[int]] = None,
) -> str:
    """Create a Fusion clip from one or more timeline items.

    Parameters:
    - track_type: "video" (default), "audio", or "subtitle".
    - track_index: 1-based track index (default: 1).
    - item_indices: list of 0-based item indices (within that track) to
      merge into the Fusion clip. If omitted, all items on the track are used.
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)

        all_items = timeline.GetItemListInTrack(track_type, track_index)
        if not all_items:
            return f"No items found on {track_type} track {track_index}"

        if item_indices is not None:
            items = [all_items[i] for i in item_indices if 0 <= i < len(all_items)]
        else:
            items = list(all_items)

        if not items:
            return "No valid items selected"

        result = timeline.CreateFusionClip(items)
        return _ok(result, f"Fusion clip created from {len(items)} item(s)", "Failed to create Fusion clip")
    except Exception as e:
        return f"Error: {e}"


# ── Node-engine accessors (reused by tools/keyframes.py, tools/fx_plugins.py) ──
#
# These are plain module-level functions (NOT @mcp.tool()) so sibling modules
# can import them. They locate the working comp and a named tool inside it,
# raising a RuntimeError with an actionable message on failure so callers can
# turn it into a clean "Error: ..." string instead of leaking a traceback.


def _get_fusion_comp(item, comp_index: int = 1, create: bool = True):
    """Return the Fusion composition at ``comp_index`` on a ``TimelineItem``.

    Wraps ``TimelineItem.GetFusionCompByIndex``; when no comp exists at that
    index and ``create`` is True, falls back to ``TimelineItem.AddFusionComp``.

    Parameters:
    - item: a live ``TimelineItem`` object.
    - comp_index: 1-based Fusion composition index on the item (default: 1).
    - create: create a new comp via ``AddFusionComp`` when none is found.

    Raises ``RuntimeError`` when no comp can be obtained (including when
    ``AddFusionComp`` returns ``None``), so the caller surfaces a failure
    string rather than raising out of the tool.
    """
    comp = None
    try:
        comp = item.GetFusionCompByIndex(comp_index)
    except Exception:
        comp = None
    if comp is None and create:
        comp = item.AddFusionComp()
    if comp is None:
        try:
            name = item.GetName()
        except Exception:
            name = "?"
        raise RuntimeError(
            f"No Fusion composition at index {comp_index} on '{name}' "
            f"(and one could not be created)."
        )
    return comp


def _resolve_tool(comp, tool_name: str):
    """Return the named tool (node) in ``comp`` via ``comp.FindTool``.

    Raises ``RuntimeError`` with a clear message when no tool of that name
    exists, so callers turn it into a failure string (no traceback).
    """
    tool = comp.FindTool(tool_name)
    if tool is None:
        raise RuntimeError(
            f"No Fusion tool named '{tool_name}' in the composition. "
            f"Use fusion_list_inputs / add the tool first."
        )
    return tool


def _input_object(tool, input_id: str):
    """Return the FusionInput object whose ``INPS_ID`` matches ``input_id``.

    Falls back to subscript access (``tool[input_id]``) when the input list
    does not surface a matching id. Returns ``None`` when unavailable.
    """
    try:
        inputs = tool.GetInputList() or {}
    except Exception:
        inputs = {}
    for inp in inputs.values():
        try:
            if (inp.GetAttrs() or {}).get("INPS_ID") == input_id:
                return inp
        except Exception:
            continue
    try:
        return tool[input_id]
    except Exception:
        return None


def _ofx_enum_index(inp, label: str) -> Optional[int]:
    """Best-effort map an OFX enum/combo choice LABEL to its integer index.

    ResolveFX/OFX enum ("choice") inputs are addressed by integer index, not
    by their display label — so an unmatched label string never sticks. This
    scans the input's attributes for a list/table of choice labels and returns
    the 0-based index of a case-insensitive match, or ``None`` if not found.
    """
    if inp is None:
        return None
    try:
        attrs = inp.GetAttrs() or {}
    except Exception:
        return None
    target = str(label).strip().lower()
    for value in attrs.values():
        items: Optional[list] = None
        if isinstance(value, (list, tuple)):
            items = list(value)
        elif isinstance(value, dict):
            # Fusion often returns 1-based ordered tables as dicts.
            try:
                items = [value[k] for k in sorted(value)]
            except Exception:
                items = list(value.values())
        if not items:
            continue
        for idx, choice in enumerate(items):
            if isinstance(choice, str) and choice.strip().lower() == target:
                return idx
    return None


def _values_match(a: Any, b: Any) -> bool:
    """Loose equality for input readback (numeric tolerance; bool-safe)."""
    try:
        if isinstance(a, bool) or isinstance(b, bool):
            return bool(a) == bool(b)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return abs(float(a) - float(b)) < 1e-6
    except Exception:
        pass
    return str(a) == str(b)


# ── Node-engine tools ─────────────────────────────────────────────────────


@mcp.tool()
def fusion_add_tool(
    reg_id: str,
    tool_name: Optional[str] = None,
    comp_index: int = 1,
    pos_x: int = -32768,
    pos_y: int = -32768,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Add a Fusion tool (node) to a timeline item's composition.

    Parameters:
    - reg_id: the tool registry ID. Native tools use their bare name
      (e.g. "Merge", "Background", "TextPlus", "Transform", "Blur").
      ResolveFX/OFX tools KEEP their full "ofx." prefix, e.g.
      "ofx.com.blackmagicdesign.resolvefx.gaussianblur" — it is passed to
      ``AddTool`` verbatim, never stripped.
    - tool_name: optional name to assign the new tool (TOOLS_Name).
    - comp_index: 1-based Fusion composition index on the item (default: 1).
    - pos_x / pos_y: flow position; -32768 (default) auto-places the node.
    - track_type: "video" (default), "audio", or "subtitle".
    - track_index: 1-based track index (default: 1).
    - item_index: 0-based index of the item within that track (default: 0).
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        comp = _get_fusion_comp(item, comp_index)
        tool = comp.AddTool(reg_id, pos_x, pos_y)
        if tool is None:
            return (
                f"Error: AddTool returned None for RegID '{reg_id}'. Check the "
                f"registry ID (ResolveFX RegIDs keep their 'ofx.' prefix)."
            )
        if tool_name:
            try:
                tool.SetAttrs({"TOOLS_Name": tool_name})
            except Exception:
                pass
        attrs = tool.GetAttrs() or {}
        return json.dumps({
            "success": True,
            "tool_name": attrs.get("TOOLS_Name", ""),
            "tool_type": attrs.get("TOOLS_RegID", reg_id),
            "reg_id": reg_id,
            "comp_index": comp_index,
        }, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def fusion_connect_input(
    tool_name: str,
    input_name: str,
    source_tool: str = "",
    source_output: str = "Output",
    comp_index: int = 1,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Wire (or unwire) a tool input to another tool's output.

    Parameters:
    - tool_name: the receiving tool (its input is being connected).
    - input_name: input ID on the receiving tool (e.g. "Background",
      "Foreground", "Input", "EffectMask").
    - source_tool: the source tool whose output feeds the input. Leave EMPTY
      (default) to DISCONNECT the input instead.
    - source_output: output ID on the source tool (default: "Output").
    - comp_index: 1-based Fusion composition index on the item (default: 1).
    - track_type: "video" (default), "audio", or "subtitle".
    - track_index: 1-based track index (default: 1).
    - item_index: 0-based index of the item within that track (default: 0).
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        comp = _get_fusion_comp(item, comp_index)
        tool = _resolve_tool(comp, tool_name)

        if not source_tool:
            result = tool.ConnectInput(input_name, None)
            return _ok(
                result,
                f"Disconnected input '{input_name}' on '{tool_name}'.",
                f"Error: Failed to disconnect input '{input_name}' on '{tool_name}'.",
            )

        src = _resolve_tool(comp, source_tool)
        source_obj: Any = src
        if source_output and source_output != "Output":
            try:
                outputs = src.GetOutputList() or {}
                for out in outputs.values():
                    if (out.GetAttrs() or {}).get("OUTS_ID") == source_output:
                        source_obj = out
                        break
            except Exception:
                source_obj = src

        result = tool.ConnectInput(input_name, source_obj)
        return _ok(
            result,
            f"Connected '{tool_name}.{input_name}' <- '{source_tool}.{source_output}'.",
            f"Error: Failed to connect '{tool_name}.{input_name}' from '{source_tool}'. "
            f"Check the input and output IDs.",
        )
    except Exception as e:
        return f"Error: {e}"


def _set_tool_input(
    tool, input_name: str, value: Any, time: Optional[int] = None
) -> Dict[str, Any]:
    """Coerce, set, and readback-verify ONE Fusion tool input.

    Shared core of :func:`fusion_set_input` and
    ``fx_plugins.set_template_fields`` so both get identical coercion/verify
    behaviour. Text inputs receive the raw string (never coerced, so literals
    like "1.0"/"007" survive); numeric/boolean inputs are coerced. On a readback
    mismatch with a label string, the OFX-enum integer-index fallback is applied.

    Returns ``{"ok": bool, "value": coerced, "readback": ...}`` optionally with
    ``"resolved_index"`` + ``"note"`` (enum fallback) or ``"warning"`` (mismatch).
    """
    inp = _input_object(tool, input_name)
    data_type = ""
    try:
        data_type = (inp.GetAttrs() or {}).get("INPS_DataType", "") if inp else ""
    except Exception:
        data_type = ""
    if isinstance(data_type, str) and data_type.startswith("Text"):
        coerced = value
    else:
        coerced = _coerce_value(value)

    def _set(v: Any) -> None:
        if time is not None:
            tool.SetInput(input_name, v, time)
        else:
            tool.SetInput(input_name, v)

    def _get() -> Any:
        if time is not None:
            return tool.GetInput(input_name, time)
        return tool.GetInput(input_name)

    _set(coerced)
    readback = _get()
    if _values_match(readback, coerced):
        return {"ok": True, "value": coerced, "readback": readback}
    if isinstance(coerced, str):
        idx = _ofx_enum_index(_input_object(tool, input_name), coerced)
        if idx is not None:
            _set(idx)
            readback = _get()
            return {"ok": True, "value": coerced, "resolved_index": idx,
                    "readback": readback,
                    "note": "OFX enum label mapped to integer index"}
    return {"ok": False, "value": coerced, "readback": readback,
            "warning": "readback did not match the requested value"}


@mcp.tool()
def fusion_set_input(
    tool_name: str,
    input_name: str,
    value: str,
    comp_index: int = 1,
    time: Optional[int] = None,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Set a value on a Fusion tool input, verifying it by readback.

    For numeric/boolean inputs the string ``value`` is auto-coerced to
    int/float/bool where it looks numeric/boolean (e.g. "1.0" -> 1,
    "true" -> True); otherwise it is passed through as a string. Text inputs
    (e.g. "StyledText") always receive the raw string verbatim — never coerced —
    so literal values like "1.0" or "007" are preserved. After setting, the
    value is read back and compared.

    If the readback does not match and ``value`` is a label string, an
    integer-index fallback is attempted: OFX/ResolveFX enum ("choice") inputs
    are addressed by index, not label, so the label is mapped to its index and
    re-applied.

    Parameters:
    - tool_name: the tool whose input is being set.
    - input_name: input ID (e.g. "Blend", "StyledText", "Size", "FilterType").
    - value: value to set, as a string.
    - comp_index: 1-based Fusion composition index on the item (default: 1).
    - time: optional frame number for a keyframed value; omit for a static set.
    - track_type / track_index / item_index: locate the timeline item.
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        comp = _get_fusion_comp(item, comp_index)
        tool = _resolve_tool(comp, tool_name)
        res = _set_tool_input(tool, input_name, value, time)
        payload: Dict[str, Any] = {
            "success": True,
            "tool_name": tool_name,
            "input_name": input_name,
            "value": res["value"],
            "readback": res.get("readback"),
        }
        if "resolved_index" in res:
            payload["resolved_index"] = res["resolved_index"]
            payload["note"] = res["note"]
        if res.get("warning"):
            payload["warning"] = res["warning"]
        return json.dumps(payload, indent=2, default=str)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def fusion_get_input(
    tool_name: str,
    input_name: str,
    comp_index: int = 1,
    time: Optional[int] = None,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Read the current value of a Fusion tool input.

    Parameters:
    - tool_name: the tool whose input is being read.
    - input_name: input ID (e.g. "Blend", "StyledText", "Size").
    - comp_index: 1-based Fusion composition index on the item (default: 1).
    - time: optional frame number; omit to read at the current time.
    - track_type / track_index / item_index: locate the timeline item.
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        comp = _get_fusion_comp(item, comp_index)
        tool = _resolve_tool(comp, tool_name)
        if time is not None:
            value = tool.GetInput(input_name, time)
        else:
            value = tool.GetInput(input_name)
        return json.dumps({
            "tool_name": tool_name,
            "input_name": input_name,
            "value": value,
        }, indent=2, default=str)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def fusion_list_inputs(
    tool_name: str,
    comp_index: int = 1,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """List all inputs of a Fusion tool (their INPS_ID / INPS_Name / type).

    Use this to discover the input IDs accepted by ``fusion_set_input`` /
    ``fusion_get_input`` / ``fusion_connect_input``.

    Parameters:
    - tool_name: the tool to inspect.
    - comp_index: 1-based Fusion composition index on the item (default: 1).
    - track_type: "video" (default), "audio", or "subtitle".
    - track_index: 1-based track index (default: 1).
    - item_index: 0-based index of the item within that track (default: 0).
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        comp = _get_fusion_comp(item, comp_index)
        tool = _resolve_tool(comp, tool_name)
        inputs = tool.GetInputList() or {}
        result = []
        for inp in inputs.values():
            try:
                attrs = inp.GetAttrs() or {}
            except Exception:
                attrs = {}
            result.append({
                "id": attrs.get("INPS_ID", ""),
                "name": attrs.get("INPS_Name", ""),
                "data_type": attrs.get("INPS_DataType", ""),
            })
        return json.dumps({
            "tool_name": tool_name,
            "input_count": len(result),
            "inputs": result,
        }, indent=2)
    except Exception as e:
        return f"Error: {e}"


# ── Comp-authoring tools (title text, macro apply, Lua escape hatch) ───────


@mcp.tool()
def set_title_text(
    text: str,
    size: Optional[float] = None,
    center_x: Optional[float] = None,
    center_y: Optional[float] = None,
    comp_index: int = 1,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Set the text (and optional Size/Center) of a Text+ title on a clip.

    Locates the ``TextPlus`` node inside the timeline item's Fusion comp by
    iterating ``comp.GetToolList()`` and matching ``GetAttrs('TOOLS_RegID')``
    == ``'TextPlus'``, then ``SetInput('StyledText', text)`` — plus
    ``SetInput('Size', size)`` and ``SetInput('Center', {1: x, 2: y})`` when
    those are supplied.

    A clip with no Fusion composition, or a comp with no Text+ node, returns a
    clear ``'No Text+ tool'`` message rather than raising.

    Parameters:
    - text: the styled text to display.
    - size: optional Text+ ``Size`` input (relative font size, e.g. 0.08).
    - center_x / center_y: optional normalized ``Center`` position (0..1);
      both must be supplied to move the text.
    - comp_index: 1-based Fusion composition index on the item (default: 1).
    - track_type: "video" (default), "audio", or "subtitle".
    - track_index: 1-based track index (default: 1).
    - item_index: 0-based index of the item within that track (default: 0).
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        try:
            name = item.GetName()
        except Exception:
            name = "?"

        comp = None
        try:
            comp = item.GetFusionCompByIndex(comp_index)
        except Exception:
            comp = None
        if comp is None:
            return (
                f"No Text+ tool: '{name}' has no Fusion composition at index "
                f"{comp_index}."
            )

        tools = comp.GetToolList() or {}
        text_tool = None
        for tool in tools.values():
            try:
                if tool.GetAttrs("TOOLS_RegID") == "TextPlus":
                    text_tool = tool
                    break
            except Exception:
                continue
        if text_tool is None:
            return (
                f"No Text+ tool found in the Fusion composition on '{name}'."
            )

        text_tool.SetInput("StyledText", text)
        if size is not None:
            text_tool.SetInput("Size", size)
        if center_x is not None and center_y is not None:
            text_tool.SetInput("Center", {1: center_x, 2: center_y})

        return json.dumps({
            "success": True,
            "item_name": name,
            "styled_text": text,
            "size": size,
            "center": (
                {"x": center_x, "y": center_y}
                if center_x is not None and center_y is not None
                else None
            ),
        }, indent=2, default=str)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def apply_macro_to_clip(
    setting_path: str,
    comp_index: int = 1,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Apply a Fusion ``.setting`` macro to a clip's composition (file round-trip).

    The macro is applied via a FILE round-trip — the ``.setting`` file is read
    by Resolve/Fusion directly, never by passing an in-memory settings table
    across the bridge. It first tries ``TimelineItem.ImportFusionComp`` and, if
    that fails, falls back to loading the settings onto a target tool via
    ``tool.LoadSettings(setting_path)`` (LoadSettings is an Operator/tool
    method, not a Composition method).

    A missing or invalid ``.setting`` path returns an ``'Error'`` string.

    Parameters:
    - setting_path: absolute path to the ``.setting`` (or ``.comp``) macro file.
    - comp_index: 1-based Fusion composition index on the item (default: 1).
    - track_type: "video" (default), "audio", or "subtitle".
    - track_index: 1-based track index (default: 1).
    - item_index: 0-based index of the item within that track (default: 0).
    """
    try:
        if not setting_path or not os.path.isfile(setting_path):
            return (
                f"Error: .setting file not found at '{setting_path}'. "
                f"Provide an absolute path to an existing macro file."
            )

        item = _get_timeline_item(track_type, track_index, item_index)
        try:
            name = item.GetName()
        except Exception:
            name = "?"

        # Preferred path: import the macro file straight onto the timeline item.
        result = None
        try:
            result = item.ImportFusionComp(setting_path)
        except Exception:
            result = None
        if result:
            return _ok(
                result,
                f"Applied macro '{setting_path}' to '{name}' via "
                f"ImportFusionComp",
                f"Error: Failed to apply macro '{setting_path}' to '{name}'.",
            )

        # Fallback: load the settings from file onto a target tool. NOTE:
        # LoadSettings is an Operator (tool) method, not a Composition method —
        # ``comp.LoadSettings`` does not exist and always errors, so call it on
        # a specific tool via a file round-trip instead.
        comp = _get_fusion_comp(item, comp_index)
        target = None
        try:
            target = comp.ActiveTool
        except Exception:
            target = None
        if not target:
            # No active tool — fall back to any tool present in the comp.
            try:
                tools = comp.GetToolList(False) or {}
                for _t in tools.values():
                    target = _t
                    break
            except Exception:
                target = None
        if not target:
            return (
                f"Error: Failed to apply macro '{setting_path}' to '{name}'. "
                f"No target tool available in the composition to load the "
                f"setting onto."
            )
        try:
            loaded = target.LoadSettings(setting_path)
        except Exception as load_err:
            return (
                f"Error: Failed to apply macro '{setting_path}' to '{name}': "
                f"{load_err}"
            )
        return _ok(
            loaded,
            f"Applied macro '{setting_path}' to '{name}' via "
            f"tool.LoadSettings",
            f"Error: Failed to apply macro '{setting_path}' to '{name}'. "
            f"Check that the file is a valid Fusion .setting.",
        )
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def execute_fusion_lua(script: str) -> str:
    """Execute a Lua script in DaVinci Resolve's Fusion (escape hatch).

    Obtains the Fusion object via ``Resolve.Fusion()`` and returns
    ``str(fusion.Execute(script))``. When Fusion is unavailable (Resolve not
    reachable or ``Resolve.Fusion()`` returns ``None``), it returns
    ``'Fusion is not available'`` rather than raising.

    Parameters:
    - script: the Lua script to execute in the Fusion context.
    """
    try:
        resolve = _conn().get_resolve()
        if resolve is None:
            return "Fusion is not available"
        try:
            fusion = resolve.Fusion()
        except Exception:
            fusion = None
        if fusion is None:
            return "Fusion is not available"
        result = fusion.Execute(script)
        return str(result) if result is not None else "Script executed successfully."
    except Exception as e:
        return f"Error: {e}"
