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
from ..helpers import _check_choice, _conn, _get_timeline_item, _ok
from ..resolve_utils import node_graph_to_dict

_NO_GRAPH_MSG = (
    "No node graph available. Make sure you are on the Color page with "
    "this clip loaded, then try again."
)

_VERSION_TYPES = {0: "local", 1: "remote"}

# Human-readable cache_mode -> the constant name Resolve exposes on the
# top-level Resolve object (Graph.SetNodeCacheMode expects that constant).
_CACHE_MODES = {
    "auto": "CACHE_AUTO_ENABLED",
    "disabled": "CACHE_DISABLED",
    "enabled": "CACHE_ENABLED",
}

# Graph.GetNodeCacheMode returns an int; decode it back to a name.
_CACHE_MODE_NAMES = {-1: "auto", 0: "disabled", 1: "enabled"}

# Human-readable export_type -> the constant name Resolve exposes on the
# top-level Resolve object (TimelineItem.ExportLUT expects that constant).
_EXPORT_LUT_TYPES = {
    "17ptcube": "EXPORT_LUT_17PTCUBE",
    "33ptcube": "EXPORT_LUT_33PTCUBE",
    "65ptcube": "EXPORT_LUT_65PTCUBE",
    "panasonicvlut": "EXPORT_LUT_PANASONICVLUT",
}


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


@mcp.tool()
def set_node_cache_mode(
    node_index: int,
    cache_mode: str,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Set the render-cache mode of a node in a clip's color node graph.

    Parameters:
    - node_index: 1-based node index (1 <= node_index <= get_num_nodes()).
    - cache_mode: one of "auto", "disabled", or "enabled" (case-insensitive).
    - track_type: "video", "audio", or "subtitle" (default "video").
    - track_index: 1-based track index (default 1).
    - item_index: 0-based item index within that track (default 0).
    """
    try:
        canonical, err = _check_choice(
            cache_mode, tuple(_CACHE_MODES.keys()), "cache_mode"
        )
        if err:
            return err

        _item, graph = _get_graph(track_type, track_index, item_index)
        if graph is None:
            return _NO_GRAPH_MSG

        attr = _CACHE_MODES[canonical]
        mode_value = getattr(_conn().get_resolve(), attr, None)
        if mode_value is None:
            return (
                f"Cache mode '{canonical}' ({attr}) is not available in this "
                "Resolve version."
            )

        result = graph.SetNodeCacheMode(node_index, mode_value)
        return _ok(
            result,
            f"Node {node_index} cache mode set to '{canonical}'.",
            f"Failed to set cache mode on node {node_index}. Check that "
            "node_index is valid.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def get_node_cache_mode(
    node_index: int,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Get the render-cache mode of a node in a clip's color node graph, as JSON.

    Returns an object with the raw int ``mode`` and a decoded ``name``
    ("auto" = -1, "disabled" = 0, "enabled" = 1).

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
        mode = graph.GetNodeCacheMode(node_index)
        name = _CACHE_MODE_NAMES.get(mode, f"unknown({mode})")
        return json.dumps({"mode": mode, "name": name}, indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def get_tools_in_node(
    node_index: int,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Get the list of tool names used in a node of a clip's color node graph.

    Returns a JSON array of tool-name strings (empty when the node uses no
    tools).

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
        tools = graph.GetToolsInNode(node_index)
        return json.dumps(list(tools) if tools else [], indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def apply_arri_cdl_lut(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Apply the ARRI CDL and LUT to a clip's color node graph.

    Reads the ARRI CDL/LUT metadata carried by the clip's source media and
    applies it to the node graph. Requires the Color page to be active with
    the clip loaded, and the clip must carry ARRI CDL/LUT metadata.

    Parameters:
    - track_type: "video", "audio", or "subtitle" (default "video").
    - track_index: 1-based track index (default 1).
    - item_index: 0-based item index within that track (default 0).
    """
    try:
        _item, graph = _get_graph(track_type, track_index, item_index)
        if graph is None:
            return _NO_GRAPH_MSG
        result = graph.ApplyArriCdlLut()
        return _ok(
            result,
            "Applied the ARRI CDL and LUT to the node graph.",
            "Failed to apply the ARRI CDL and LUT. Make sure the clip carries "
            "ARRI CDL/LUT metadata and the Color page is active.",
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


# ── Grade transfer / LUT export / version rename ─────────────────────────


@mcp.tool()
def copy_grades(
    targets: str,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Copy a source clip's current grade onto one or more target clips.

    Locates the source clip via (track_type, track_index, item_index) and
    applies its grade to every clip named in ``targets`` using
    ``TimelineItem.CopyGrades``.

    Parameters:
    - targets: a JSON array of clip locators, each an object with keys
      "track_type" (default "video"), "track_index" (default 1), and
      "item_index" (default 0). Example:
      '[{"track_type": "video", "track_index": 1, "item_index": 1}]'.
    - track_type: source clip track type — "video", "audio", or "subtitle"
      (default "video").
    - track_index: source clip 1-based track index (default 1).
    - item_index: source clip 0-based item index within that track
      (default 0).
    """
    try:
        try:
            parsed = json.loads(targets)
        except (ValueError, TypeError) as e:
            return (
                f"Invalid targets JSON: {e}. Provide a JSON array of clip "
                'locators, e.g. [{"track_type": "video", "track_index": 1, '
                '"item_index": 1}].'
            )
        if not isinstance(parsed, list) or not parsed:
            return (
                "targets must be a non-empty JSON array of clip locators, "
                'e.g. [{"track_type": "video", "track_index": 1, '
                '"item_index": 1}].'
            )

        source = _get_timeline_item(track_type, track_index, item_index)

        target_items = []
        for i, entry in enumerate(parsed):
            if not isinstance(entry, dict):
                return (
                    f"targets[{i}] must be an object with keys track_type, "
                    "track_index, item_index."
                )
            try:
                target_items.append(
                    _get_timeline_item(
                        entry.get("track_type", "video"),
                        int(entry.get("track_index", 1)),
                        int(entry.get("item_index", 0)),
                    )
                )
            except (ValueError, TypeError) as e:
                return f"targets[{i}] has invalid index values: {e}."
            except Exception as e:  # noqa: BLE001
                return f"targets[{i}] could not be located: {e}"

        result = source.CopyGrades(target_items)
        return _ok(
            result,
            f"Copied grade to {len(target_items)} target clip(s).",
            "Failed to copy grade. Make sure the Color page is active and "
            "the source clip has a grade to copy.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def export_lut(
    export_type: str,
    path: str,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Export a clip's current grade as a LUT file.

    Parameters:
    - export_type: LUT format — one of "17ptcube", "33ptcube", "65ptcube",
      or "panasonicvlut" (case-insensitive).
    - path: output file path (an appropriate extension is appended if
      missing).
    - track_type: "video", "audio", or "subtitle" (default "video").
    - track_index: 1-based track index (default 1).
    - item_index: 0-based item index within that track (default 0).
    """
    try:
        canonical, err = _check_choice(
            export_type, tuple(_EXPORT_LUT_TYPES.keys()), "export_type"
        )
        if err:
            return err

        item = _get_timeline_item(track_type, track_index, item_index)

        attr = _EXPORT_LUT_TYPES[canonical]
        etype = getattr(_conn().get_resolve(), attr, None)
        if etype is None:
            return (
                f"LUT export type '{canonical}' ({attr}) is not available in "
                "this Resolve version."
            )

        result = item.ExportLUT(etype, path)
        return _ok(
            result,
            f"Exported {canonical} LUT to '{path}'.",
            f"Failed to export LUT to '{path}'. Check that the path is "
            "writable and the clip has a grade.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def rename_color_version(
    old_name: str,
    new_name: str,
    version_type: int = 0,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Rename a clip's color version from ``old_name`` to ``new_name``.

    Parameters:
    - old_name: current name of the version to rename.
    - new_name: new name for the version.
    - version_type: 0 = local, 1 = remote (default 0).
    - track_type: "video", "audio", or "subtitle" (default "video").
    - track_index: 1-based track index (default 1).
    - item_index: 0-based item index within that track (default 0).
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        result = item.RenameVersionByName(old_name, new_name, version_type)
        kind = _VERSION_TYPES.get(version_type, str(version_type))
        return _ok(
            result,
            f"Renamed {kind} color version '{old_name}' to '{new_name}'.",
            f"Failed to rename color version '{old_name}'. Check that it "
            f"exists for the given version_type and '{new_name}' is unused.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def reset_node_colors(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Reset the node color (label color) on every node of the active version.

    Requires DaVinci Resolve 20.2+; returns a clear message on older
    versions where ``TimelineItem.ResetAllNodeColors`` is not available.

    Parameters:
    - track_type: "video", "audio", or "subtitle" (default "video").
    - track_index: 1-based track index (default 1).
    - item_index: 0-based item index within that track (default 0).
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        if not hasattr(item, "ResetAllNodeColors"):
            return (
                "ResetAllNodeColors is not available in this Resolve "
                "version (needs 20.2+)."
            )
        result = item.ResetAllNodeColors()
        return _ok(
            result,
            "Reset node colors on all nodes of the active version.",
            "Failed to reset node colors.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Color groups ─────────────────────────────────────────────────────────
#
# This repo has no object-id registry, so ColorGroup objects are addressed
# BY NAME: scan Project.GetColorGroupsList() and match ColorGroup.GetName().


def _get_color_group_by_name(project, name: str):
    """Return the ``ColorGroup`` on ``project`` whose name is ``name``.

    Scans ``Project.GetColorGroupsList()`` and matches on
    ``ColorGroup.GetName()``; returns ``None`` when no group matches so
    callers can emit a clear "No color group named ..." message without
    touching Delete/Assign APIs.
    """
    groups = project.GetColorGroupsList()
    if not groups:
        return None
    for group in groups:
        if group.GetName() == name:
            return group
    return None


@mcp.tool()
def list_color_groups() -> str:
    """List the names of all color groups in the current project, as a JSON array.

    Returns an empty array when the project has no color groups.
    """
    try:
        project = _conn().get_project()
        groups = project.GetColorGroupsList()
        names = [g.GetName() for g in groups] if groups else []
        return json.dumps(names, indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def add_color_group(group_name: str) -> str:
    """Create a new color group in the current project.

    Parameters:
    - group_name: name for the new color group.
    """
    try:
        project = _conn().get_project()
        group = project.AddColorGroup(group_name)
        return _ok(
            group is not None,
            f"Created color group '{group_name}'.",
            f"Failed to create color group '{group_name}'. A group with that "
            "name may already exist.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def delete_color_group(group_name: str) -> str:
    """Delete a color group from the current project by name.

    Parameters:
    - group_name: name of the color group to delete.
    """
    try:
        project = _conn().get_project()
        group = _get_color_group_by_name(project, group_name)
        if group is None:
            return (
                f"No color group named '{group_name}'. Use list_color_groups "
                "to see the available groups."
            )
        result = project.DeleteColorGroup(group)
        return _ok(
            result,
            f"Deleted color group '{group_name}'.",
            f"Failed to delete color group '{group_name}'. It may still have "
            "clips assigned to it.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def assign_clip_to_color_group(
    group_name: str,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Assign a clip to a color group by group name.

    Parameters:
    - group_name: name of the color group to assign the clip to.
    - track_type: "video", "audio", or "subtitle" (default "video").
    - track_index: 1-based track index (default 1).
    - item_index: 0-based item index within that track (default 0).
    """
    try:
        project = _conn().get_project()
        group = _get_color_group_by_name(project, group_name)
        if group is None:
            return (
                f"No color group named '{group_name}'. Use list_color_groups "
                "to see the available groups, or add_color_group to create it."
            )
        item = _get_timeline_item(track_type, track_index, item_index)
        result = item.AssignToColorGroup(group)
        return _ok(
            result,
            f"Assigned clip to color group '{group_name}'.",
            f"Failed to assign clip to color group '{group_name}'.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def remove_clip_from_color_group(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Remove a clip from whatever color group it is currently assigned to.

    Parameters:
    - track_type: "video", "audio", or "subtitle" (default "video").
    - track_index: 1-based track index (default 1).
    - item_index: 0-based item index within that track (default 0).
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        result = item.RemoveFromColorGroup()
        return _ok(
            result,
            "Removed clip from its color group.",
            "Failed to remove clip from color group. The clip may not be "
            "assigned to any group.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def get_color_group_node_graph(group_name: str, stage: str = "pre") -> str:
    """Get a color group's pre-clip or post-clip node graph, as JSON.

    The pre-clip graph applies before each member clip's individual grade;
    the post-clip graph applies after.

    Parameters:
    - group_name: name of the color group.
    - stage: "pre" for the pre-clip node graph or "post" for the post-clip
      node graph (case-insensitive, default "pre").
    """
    try:
        canonical, err = _check_choice(stage, ("pre", "post"), "stage")
        if err:
            return err

        project = _conn().get_project()
        group = _get_color_group_by_name(project, group_name)
        if group is None:
            return (
                f"No color group named '{group_name}'. Use list_color_groups "
                "to see the available groups."
            )

        if canonical == "pre":
            graph = group.GetPreClipNodeGraph()
        else:
            graph = group.GetPostClipNodeGraph()
        if graph is None:
            return (
                f"No {canonical}-clip node graph available for color group "
                f"'{group_name}'."
            )
        return json.dumps(node_graph_to_dict(graph), indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"
