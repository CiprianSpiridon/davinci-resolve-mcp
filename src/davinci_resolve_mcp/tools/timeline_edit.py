"""Timeline-editing tools for the DaVinci Resolve MCP server.

This is the "mutate the timeline" half of timeline coverage (the read/
navigate/structure half — timeline list, tracks, timecode, item listing —
lives in ``tools/timeline.py``). This module owns:

- Timeline markers: add, list, delete-at-frame, delete-by-color, and
  update-custom-data (``Timeline.AddMarker`` / ``GetMarkers`` /
  ``DeleteMarkerAtFrame`` / ``DeleteMarkersByColor`` /
  ``UpdateMarkerCustomData``).
- Inserting generators/titles at the playhead: ``InsertGeneratorIntoTimeline``,
  ``InsertFusionGeneratorIntoTimeline``, ``InsertOFXGeneratorIntoTimeline``,
  ``InsertTitleIntoTimeline``, ``InsertFusionTitleIntoTimeline``.
- ``create_compound_clip`` (``Timeline.CreateCompoundClip``).
- ``detect_scene_cuts`` (``Timeline.DetectSceneCuts``, Resolve Studio only).

Per the ownership contract, ``insert_*`` and ``detect_scene_cuts`` are
defined here and nowhere else in this server.

All tools reach Resolve lazily via ``_conn()``/``_require_timeline()`` inside
their bodies — nothing here imports ``DaVinciResolveScript`` or touches a
live Resolve instance at import time.
"""

from __future__ import annotations

import json

from ..app import mcp
from ..helpers import _conn, _ok, _require_timeline

# ── Markers ──────────────────────────────────────────────────────────────


@mcp.tool()
def add_marker(
    frame_id: int,
    color: str,
    name: str,
    note: str = "",
    duration: int = 1,
    custom_data: str = "",
) -> str:
    """Add a marker to the current timeline.

    Parameters:
    - frame_id: frame position (relative to the timeline) for the marker.
    - color: marker color, e.g. "Blue", "Cyan", "Green", "Yellow", "Red",
      "Pink", "Purple", "Fuchsia", "Rose", "Lavender", "Sky", "Mint",
      "Lemon", "Sand", "Cocoa", "Cream".
    - name: marker name.
    - note: optional marker note.
    - duration: marker duration in frames (default 1).
    - custom_data: optional custom data string, retrievable/searchable later
      via update_marker_custom_data / get_markers.
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)
        result = timeline.AddMarker(frame_id, color, name, note, duration, custom_data)
        return _ok(
            result,
            f"Added {color} marker '{name}' at frame {frame_id}.",
            f"Error: Failed to add marker at frame {frame_id}. A marker may "
            "already exist at that frame, or the color/frame is invalid.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def get_markers() -> str:
    """Get all markers on the current timeline, as JSON keyed by frame number."""
    try:
        conn = _conn()
        timeline = _require_timeline(conn)
        markers = timeline.GetMarkers()
        if not markers:
            return json.dumps({})
        return json.dumps({str(k): v for k, v in markers.items()}, indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def delete_marker_at_frame(frame_id: int) -> str:
    """Delete the marker at a specific frame on the current timeline.

    Parameters:
    - frame_id: frame number of the marker to delete.
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)
        if not hasattr(timeline, "DeleteMarkerAtFrame"):
            return "Error: DeleteMarkerAtFrame is not available on this timeline object."
        result = timeline.DeleteMarkerAtFrame(frame_id)
        return _ok(
            result,
            f"Deleted marker at frame {frame_id}.",
            f"Error: Failed to delete marker at frame {frame_id}. No marker "
            "may exist at that frame.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def delete_markers_by_color(color: str) -> str:
    """Delete all markers of a given color from the current timeline.

    Parameters:
    - color: marker color to delete (e.g. "Blue"), or "All" to delete every
      marker on the timeline.
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)
        if not hasattr(timeline, "DeleteMarkersByColor"):
            return "Error: DeleteMarkersByColor is not available on this timeline object."
        result = timeline.DeleteMarkersByColor(color)
        return _ok(
            result,
            f"Deleted {color} markers from the timeline.",
            f"Error: Failed to delete {color} markers. There may be no "
            "markers of that color.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def update_marker_custom_data(frame_id: int, custom_data: str) -> str:
    """Update the custom data string of an existing marker on the current timeline.

    Custom data is not shown in the Resolve UI; it's a scripting-only field
    useful for tagging markers with data an LLM or other tool can look up
    later.

    Parameters:
    - frame_id: frame number of the existing marker to update.
    - custom_data: new custom data string for that marker.
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)
        if not hasattr(timeline, "UpdateMarkerCustomData"):
            return "Error: UpdateMarkerCustomData is not available on this timeline object."
        result = timeline.UpdateMarkerCustomData(frame_id, custom_data)
        return _ok(
            result,
            f"Updated custom data for marker at frame {frame_id}.",
            f"Error: Failed to update custom data for marker at frame "
            f"{frame_id}. No marker may exist at that frame.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Insert generators / titles ──────────────────────────────────────────
# All of these drop the new item on whatever track the Edit page's target
# track selector currently points at (there is no API to choose the track
# explicitly) — see Resolve scripting API docs.


@mcp.tool()
def insert_generator(generator_name: str) -> str:
    """Insert a generator (e.g. "Solid Color", "Bars and Tone") into the current timeline at the playhead.

    Parameters:
    - generator_name: name of the generator to insert, as it appears in the
      Effects Library's Generators bin.
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)
        if not hasattr(timeline, "InsertGeneratorIntoTimeline"):
            return "Error: InsertGeneratorIntoTimeline is not available on this timeline object."
        item = timeline.InsertGeneratorIntoTimeline(generator_name)
        return _ok(
            item,
            f"Inserted generator '{generator_name}' into the timeline.",
            f"Error: Failed to insert generator '{generator_name}'. Check "
            "the generator name and that a target track is available/unlocked.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def insert_fusion_generator(generator_name: str) -> str:
    """Insert a Fusion generator into the current timeline at the playhead.

    Parameters:
    - generator_name: name of the Fusion generator template to insert.
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)
        if not hasattr(timeline, "InsertFusionGeneratorIntoTimeline"):
            return "Error: InsertFusionGeneratorIntoTimeline is not available on this timeline object."
        item = timeline.InsertFusionGeneratorIntoTimeline(generator_name)
        return _ok(
            item,
            f"Inserted Fusion generator '{generator_name}' into the timeline.",
            f"Error: Failed to insert Fusion generator '{generator_name}'. "
            "Check the generator name and that a target track is available/unlocked.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def insert_ofx_generator(generator_name: str) -> str:
    """Insert an OFX (OpenFX) generator into the current timeline at the playhead.

    Parameters:
    - generator_name: name of the OFX generator plugin to insert.
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)
        if not hasattr(timeline, "InsertOFXGeneratorIntoTimeline"):
            return "Error: InsertOFXGeneratorIntoTimeline is not available on this timeline object."
        item = timeline.InsertOFXGeneratorIntoTimeline(generator_name)
        return _ok(
            item,
            f"Inserted OFX generator '{generator_name}' into the timeline.",
            f"Error: Failed to insert OFX generator '{generator_name}'. "
            "Check that the name matches an installed OFX generator plugin.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def insert_title(title_name: str) -> str:
    """Insert a title into the current timeline at the playhead.

    Parameters:
    - title_name: name of the title template to insert, as it appears in
      the Effects Library's Titles bin (e.g. "Text", "Text+").
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)
        if not hasattr(timeline, "InsertTitleIntoTimeline"):
            return "Error: InsertTitleIntoTimeline is not available on this timeline object."
        item = timeline.InsertTitleIntoTimeline(title_name)
        return _ok(
            item,
            f"Inserted title '{title_name}' into the timeline.",
            f"Error: Failed to insert title '{title_name}'. Check the title "
            "name and that a target track is available/unlocked.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def insert_fusion_title(title_name: str) -> str:
    """Insert a Fusion title into the current timeline at the playhead.

    Parameters:
    - title_name: name of the Fusion title template to insert.
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)
        if not hasattr(timeline, "InsertFusionTitleIntoTimeline"):
            return "Error: InsertFusionTitleIntoTimeline is not available on this timeline object."
        item = timeline.InsertFusionTitleIntoTimeline(title_name)
        return _ok(
            item,
            f"Inserted Fusion title '{title_name}' into the timeline.",
            f"Error: Failed to insert Fusion title '{title_name}'. Check the "
            "title name and that a target track is available/unlocked.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Compound clips ───────────────────────────────────────────────────────


@mcp.tool()
def create_compound_clip(
    track_type: str = "video",
    track_index: int = 1,
    item_indices: list[int] | None = None,
    name: str = "",
    start_timecode: str = "",
) -> str:
    """Create a compound clip from one or more timeline items on a single track.

    Parameters:
    - track_type: one of "video", "audio", "subtitle". Defaults to "video".
    - track_index: 1-based track index that the items live on.
    - item_indices: 0-based indices (within that track's item list, as
      returned by get_timeline_items) of the items to combine. Leave empty
      / omit to select every item on the track.
    - name: optional name for the resulting compound clip.
    - start_timecode: optional start timecode for the compound clip, e.g.
      "01:00:00:00".
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)

        valid_track_types = ("video", "audio", "subtitle")
        if track_type not in valid_track_types:
            return (
                f"Error: Invalid track_type '{track_type}'. Must be one of: "
                f"{', '.join(valid_track_types)}."
            )

        all_items = timeline.GetItemListInTrack(track_type, track_index) or []
        if not all_items:
            return f"Error: No items found on {track_type} track {track_index}."

        if item_indices:
            bad = [i for i in item_indices if i < 0 or i >= len(all_items)]
            if bad:
                return (
                    f"Error: item_indices {bad} out of range — track has "
                    f"{len(all_items)} item(s) (0-{len(all_items) - 1})."
                )
            targets = [all_items[i] for i in item_indices]
        else:
            targets = list(all_items)

        clip_info: dict[str, str] = {}
        if name:
            clip_info["name"] = name
        if start_timecode:
            clip_info["startTimecode"] = start_timecode

        if not hasattr(timeline, "CreateCompoundClip"):
            return "Error: CreateCompoundClip is not available on this timeline object."
        result = timeline.CreateCompoundClip(targets, clip_info) if clip_info else timeline.CreateCompoundClip(targets)
        if not result:
            return (
                f"Error: Failed to create compound clip from {len(targets)} "
                f"item(s) on {track_type} track {track_index}."
            )
        result_name = result.GetName() if hasattr(result, "GetName") else name or "(unnamed)"
        return f"Created compound clip '{result_name}' from {len(targets)} item(s)."
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Scene detection ──────────────────────────────────────────────────────


@mcp.tool()
def detect_scene_cuts() -> str:
    """Detect scene cuts in the current timeline and add cut markers/edits.

    Requires DaVinci Resolve Studio (not available in the free edition).
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)
        if not hasattr(timeline, "DetectSceneCuts"):
            return "Error: DetectSceneCuts is not available. Requires DaVinci Resolve Studio."
        result = timeline.DetectSceneCuts()
        return _ok(
            result,
            "Scene cuts detected and applied to the timeline.",
            "Error: Failed to detect scene cuts.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"
