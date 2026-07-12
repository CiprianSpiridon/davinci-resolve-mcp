"""Fusion composition-management tools for the DaVinci Resolve MCP server.

This module owns Fusion *comp management* on timeline items — listing,
adding, importing, exporting, loading, deleting, and renaming Fusion
compositions attached to a ``TimelineItem`` (``TimelineItem.GetFusionCompCount``
/ ``GetFusionCompNameList`` / ``AddFusionComp`` / ``ImportFusionComp`` /
``ExportFusionComp`` / ``LoadFusionCompByName`` / ``DeleteFusionCompByName`` /
``RenameFusionCompByName``) — plus ``create_fusion_clip``
(``Timeline.CreateFusionClip``).

Per the ownership contract, ``insert_fusion_generator`` and
``insert_fusion_title`` (playhead-insert operations) live in
``tools/timeline_edit.py``, not here.

All tools reach Resolve lazily via ``_conn()``/``_require_timeline()``/
``_get_timeline_item()`` inside their bodies — nothing here imports
``DaVinciResolveScript`` or touches a live Resolve instance at import time.
"""

from __future__ import annotations

import json
from typing import List, Optional

from ..app import mcp
from ..helpers import _conn, _get_timeline_item, _ok, _require_timeline


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
