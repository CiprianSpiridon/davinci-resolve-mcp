"""Color-page grading and gallery tools for the DaVinci Resolve MCP server.

Covers the color-grading node graph for a located ``TimelineItem`` (node
count, per-node label/LUT/enabled state, LUT get/set, CDL, grade-from-DRX,
reset-all-grades), a clip's color versions (add/get-current/delete/load),
and gallery still-grabbing (``Timeline.GrabStill`` / ``GrabAllStills``).

Per the ownership contract, this module owns ``grab_still`` and
``grab_all_stills`` — no other module in this server defines them.

The Resolve scripting API only exposes a clip's ``Graph`` object while the
Color page is active with that clip loaded; ``TimelineItem.GetNodeGraph()``
returns ``None`` otherwise. Every graph-based tool here checks for that and
returns a clear "switch to the Color page" message instead of raising or
letting a ``NoneType`` attribute error escape.

All tools reach Resolve lazily via ``_conn()``/``_get_timeline_item()``
inside their bodies — nothing here imports ``DaVinciResolveScript`` or
touches a live Resolve instance at import time.
"""

from __future__ import annotations

import json

from ..app import mcp
from ..helpers import _conn, _get_timeline_item, _ok
from ..resolve_utils import node_graph_to_dict

_NO_GRAPH_MSG = (
    "No node graph available. Make sure you are on the Color page with "
    "this clip loaded, then try again."
)

_VERSION_TYPES = {0: "local", 1: "remote"}


def _get_graph(track_type: str, track_index: int, item_index: int):
    """Locate a timeline item and return ``(item, graph)``.

    ``graph`` is ``None`` when the item has no node graph available (i.e.
    the Color page isn't active with this clip loaded) — callers should
    check for that and return ``_NO_GRAPH_MSG`` rather than raising.
    """
    item = _get_timeline_item(track_type, track_index, item_index)
    graph = item.GetNodeGraph()
    return item, graph


# ── Node graph ───────────────────────────────────────────────────────────


@mcp.tool()
def get_node_graph(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Get the color-grading node graph (node count + per-node label/LUT) for a clip.

    Must be on the Color page with the clip loaded, otherwise returns a
    message explaining that.

    Parameters:
    - track_type: "video", "audio", or "subtitle" (default "video").
    - track_index: 1-based track index (default 1).
    - item_index: 0-based item index within that track (default 0).
    """
    try:
        _item, graph = _get_graph(track_type, track_index, item_index)
        if graph is None:
            return _NO_GRAPH_MSG
        return json.dumps(node_graph_to_dict(graph), indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def get_num_nodes(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Get the number of nodes in a clip's color node graph.

    Parameters:
    - track_type: "video", "audio", or "subtitle" (default "video").
    - track_index: 1-based track index (default 1).
    - item_index: 0-based item index within that track (default 0).
    """
    try:
        _item, graph = _get_graph(track_type, track_index, item_index)
        if graph is None:
            return _NO_GRAPH_MSG
        return str(graph.GetNumNodes())
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def get_node_label(
    node_index: int,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Get the label of a node in a clip's color node graph.

    Parameters:
    - node_index: 1-based node index (1 <= node_index <= get_num_nodes()).
    - track_type: "video", "audio", or "subtitle" (default "video").
    - track_index: 1-based track index (default 1).
    - item_index: 0-based item index within that track (default 0).
    """
    try:
        _item, graph = _get_graph(track_type, track_index, item_index)
        if graph is None:
            return _NO_GRAPH_MSG
        label = graph.GetNodeLabel(node_index)
        return label if label else f"(node {node_index} has no label)"
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def set_node_enabled(
    node_index: int,
    enabled: bool,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Enable or disable a node in a clip's color node graph.

    Parameters:
    - node_index: 1-based node index (1 <= node_index <= get_num_nodes()).
    - enabled: True to enable the node, False to disable it.
    - track_type: "video", "audio", or "subtitle" (default "video").
    - track_index: 1-based track index (default 1).
    - item_index: 0-based item index within that track (default 0).
    """
    try:
        _item, graph = _get_graph(track_type, track_index, item_index)
        if graph is None:
            return _NO_GRAPH_MSG
        result = graph.SetNodeEnabled(node_index, enabled)
        return _ok(
            result,
            f"Node {node_index} {'enabled' if enabled else 'disabled'}.",
            f"Failed to set enabled={enabled} on node {node_index}. "
            "Check that node_index is valid.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def get_lut(
    node_index: int,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Get the LUT path applied to a node in a clip's color node graph.

    Parameters:
    - node_index: 1-based node index (1 <= node_index <= get_num_nodes()).
    - track_type: "video", "audio", or "subtitle" (default "video").
    - track_index: 1-based track index (default 1).
    - item_index: 0-based item index within that track (default 0).
    """
    try:
        _item, graph = _get_graph(track_type, track_index, item_index)
        if graph is None:
            return _NO_GRAPH_MSG
        lut = graph.GetLUT(node_index)
        return lut if lut else f"(no LUT applied on node {node_index})"
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def set_lut(
    node_index: int,
    lut_path: str,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Apply a LUT to a node in a clip's color node graph.

    Parameters:
    - node_index: 1-based node index (1 <= node_index <= get_num_nodes()).
    - lut_path: absolute path to the LUT file (.cube, .3dl, etc.), or a
      path relative to the custom/master LUT paths Resolve already knows
      about (see refresh_lut_list-style project tools).
    - track_type: "video", "audio", or "subtitle" (default "video").
    - track_index: 1-based track index (default 1).
    - item_index: 0-based item index within that track (default 0).
    """
    try:
        _item, graph = _get_graph(track_type, track_index, item_index)
        if graph is None:
            return _NO_GRAPH_MSG
        result = graph.SetLUT(node_index, lut_path)
        return _ok(
            result,
            f"LUT applied to node {node_index}: {lut_path}",
            f"Failed to apply LUT to node {node_index}. Check that "
            "node_index is valid and the LUT file/path is known to Resolve.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def set_cdl(
    node_index: int,
    slope: str = "1.0 1.0 1.0",
    offset: str = "0.0 0.0 0.0",
    power: str = "1.0 1.0 1.0",
    saturation: float = 1.0,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Apply CDL (Color Decision List) values to a node on a clip.

    Parameters:
    - node_index: 1-based node index (1 <= node_index <= total node count).
    - slope: RGB slope as a space-separated string, e.g. "1.0 1.0 1.0".
    - offset: RGB offset as a space-separated string, e.g. "0.0 0.0 0.0".
    - power: RGB power as a space-separated string, e.g. "1.0 1.0 1.0".
    - saturation: saturation value (default 1.0).
    - track_type: "video", "audio", or "subtitle" (default "video").
    - track_index: 1-based track index (default 1).
    - item_index: 0-based item index within that track (default 0).
    """
    try:
        item, graph = _get_graph(track_type, track_index, item_index)
        if graph is None:
            return _NO_GRAPH_MSG
        cdl_map = {
            "NodeIndex": str(node_index),
            "Slope": slope,
            "Offset": offset,
            "Power": power,
            "Saturation": str(saturation),
        }
        result = item.SetCDL(cdl_map)
        return _ok(
            result,
            f"CDL applied to node {node_index}.",
            f"Failed to apply CDL to node {node_index}. Make sure you are "
            "on the Color page and node_index is valid.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def apply_grade_from_drx(
    drx_path: str,
    grade_mode: int = 0,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Apply a grade from a .drx PowerGrade file to a clip's node graph.

    This replaces the target node graph — there is no append mode. Consider
    grabbing a still first (see grab_still) if you want to back up the
    current grade before applying.

    Parameters:
    - drx_path: absolute path to the .drx file.
    - grade_mode: 0 = No keyframes, 1 = Source Timecode aligned,
      2 = Start Frames aligned (default 0).
    - track_type: "video", "audio", or "subtitle" (default "video").
    - track_index: 1-based track index (default 1).
    - item_index: 0-based item index within that track (default 0).
    """
    try:
        _item, graph = _get_graph(track_type, track_index, item_index)
        if graph is None:
            return _NO_GRAPH_MSG
        result = graph.ApplyGradeFromDRX(drx_path, grade_mode)
        return _ok(
            result,
            f"Grade applied from '{drx_path}' (grade_mode={grade_mode}).",
            f"Failed to apply grade from '{drx_path}'. Check that the file "
            "exists and is a valid .drx PowerGrade.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def reset_all_grades(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Reset all grades on a clip's color node graph.

    Parameters:
    - track_type: "video", "audio", or "subtitle" (default "video").
    - track_index: 1-based track index (default 1).
    - item_index: 0-based item index within that track (default 0).
    """
    try:
        _item, graph = _get_graph(track_type, track_index, item_index)
        if graph is None:
            return _NO_GRAPH_MSG
        if not hasattr(graph, "ResetAllGrades"):
            return (
                "ResetAllGrades is not available in this Resolve version."
            )
        result = graph.ResetAllGrades()
        return _ok(
            result,
            "All grades reset.",
            "Failed to reset grades.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Color versions ───────────────────────────────────────────────────────


@mcp.tool()
def get_current_color_version(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Get the current color version (name + type) of a clip, as JSON.

    Parameters:
    - track_type: "video", "audio", or "subtitle" (default "video").
    - track_index: 1-based track index (default 1).
    - item_index: 0-based item index within that track (default 0).
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        version = item.GetCurrentVersion()
        return json.dumps(version if version else {}, indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def add_color_version(
    version_name: str,
    version_type: int = 0,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Add a new color version to a clip.

    Parameters:
    - version_name: name for the new version.
    - version_type: 0 = local, 1 = remote (default 0).
    - track_type: "video", "audio", or "subtitle" (default "video").
    - track_index: 1-based track index (default 1).
    - item_index: 0-based item index within that track (default 0).
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        result = item.AddVersion(version_name, version_type)
        kind = _VERSION_TYPES.get(version_type, str(version_type))
        return _ok(
            result,
            f"Added {kind} color version '{version_name}'.",
            f"Failed to add color version '{version_name}'. A version with "
            "that name may already exist for this version_type.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def delete_color_version(
    version_name: str,
    version_type: int = 0,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Delete a color version from a clip by name.

    Parameters:
    - version_name: name of the version to delete.
    - version_type: 0 = local, 1 = remote (default 0).
    - track_type: "video", "audio", or "subtitle" (default "video").
    - track_index: 1-based track index (default 1).
    - item_index: 0-based item index within that track (default 0).
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        result = item.DeleteVersionByName(version_name, version_type)
        kind = _VERSION_TYPES.get(version_type, str(version_type))
        return _ok(
            result,
            f"Deleted {kind} color version '{version_name}'.",
            f"Failed to delete color version '{version_name}'. Check that "
            "it exists for the given version_type.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def load_color_version(
    version_name: str,
    version_type: int = 0,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Load a named color version as the active version on a clip.

    Parameters:
    - version_name: name of the version to load.
    - version_type: 0 = local, 1 = remote (default 0).
    - track_type: "video", "audio", or "subtitle" (default "video").
    - track_index: 1-based track index (default 1).
    - item_index: 0-based item index within that track (default 0).
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        result = item.LoadVersionByName(version_name, version_type)
        kind = _VERSION_TYPES.get(version_type, str(version_type))
        return _ok(
            result,
            f"Loaded {kind} color version '{version_name}'.",
            f"Failed to load color version '{version_name}'. Check that "
            "it exists for the given version_type.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Gallery stills ───────────────────────────────────────────────────────


@mcp.tool()
def grab_still() -> str:
    """Grab a still of the current frame under the playhead into the gallery.

    Requires the Color page to be active with a clip under the playhead;
    returns a clear message when there's no active timeline or the grab
    fails for that reason.
    """
    try:
        conn = _conn()
        timeline = conn.get_current_timeline()
        if timeline is None:
            return "No active timeline. Create or open a timeline first."
        still = timeline.GrabStill()
        return _ok(
            still is not None,
            "Grabbed a still from the current frame into the gallery.",
            "Failed to grab a still. Make sure the Color page is active "
            "with a clip under the playhead.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def grab_all_stills(still_frame_source: int = 1) -> str:
    """Grab stills from every clip in the current timeline into the gallery.

    Parameters:
    - still_frame_source: 1 = first frame of each clip, 2 = middle frame of
      each clip (default 1).

    Requires the Color page to be active; returns a clear message when
    there's no active timeline or the grab fails for that reason.
    """
    try:
        conn = _conn()
        timeline = conn.get_current_timeline()
        if timeline is None:
            return "No active timeline. Create or open a timeline first."
        stills = timeline.GrabAllStills(still_frame_source)
        count = len(stills) if stills else 0
        return _ok(
            bool(stills),
            f"Grabbed {count} still(s) into the gallery.",
            "Failed to grab stills. Make sure the Color page is active "
            "and the timeline has clips.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"
