"""
Read-only MCP resources for the DaVinci Resolve MCP server.

Unlike ``tools/*.py`` modules (which register imperative actions via
``@mcp.tool()``), this module registers read-only, URI-addressable
snapshots of Resolve state via ``@mcp.resource(uri)``:

- ``resolve://project/info`` — current project summary (name, version,
  current page, timeline count, frame rate/resolution).
- ``resolve://timeline/current`` — current timeline summary (name,
  frame range, track counts, settings, current timecode, markers).
- ``resolve://mediapool/structure`` — Media Pool folder/clip tree.

Each resource function mirrors the JSON shape of its equivalent tool
(``get_project_info`` / ``get_current_timeline_info`` /
``get_media_pool_structure``) but is exposed as a resource for clients
that prefer to read state rather than invoke a tool call.

All resources reach Resolve lazily via ``_conn()`` inside their bodies —
nothing here imports ``DaVinciResolveScript`` or touches a live Resolve
instance at import time. Each resource degrades to a plain error string
(never raises) when Resolve isn't reachable, no project is open, or no
timeline is active — so this module always imports and every resource is
always readable, live Resolve or not.
"""

from __future__ import annotations

import json

from .app import mcp
from .helpers import _conn
from .resolve_utils import folder_to_dict, timeline_to_dict


@mcp.resource("resolve://project/info")
def project_info_resource() -> str:
    """Current DaVinci Resolve project summary, as JSON.

    Includes project name, Resolve version string, current page, timeline
    count, and (when available) frame rate / resolution settings. Returns
    an ``Error: ...`` string if Resolve isn't running or no project is
    open.
    """
    try:
        conn = _conn()
        resolve = conn.get_resolve()
        project = conn.get_project()

        info: dict = {
            "name": project.GetName(),
            "version": resolve.GetVersionString() if hasattr(resolve, "GetVersionString") else None,
            "current_page": resolve.GetCurrentPage() if hasattr(resolve, "GetCurrentPage") else None,
            "timeline_count": project.GetTimelineCount() if hasattr(project, "GetTimelineCount") else None,
        }

        for key in (
            "timelineFrameRate",
            "timelinePlaybackFrameRate",
            "timelineResolutionWidth",
            "timelineResolutionHeight",
        ):
            value = project.GetSetting(key) if hasattr(project, "GetSetting") else None
            if value:
                info[key] = value

        return json.dumps(info, indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.resource("resolve://timeline/current")
def timeline_current_resource() -> str:
    """Current DaVinci Resolve timeline summary, as JSON.

    Includes name, start/end frame, start timecode, per-track-type track
    counts, key timeline settings (frame rate, resolution), current
    timecode, and markers. Returns an explanatory ``Error: ...`` string if
    Resolve isn't running, no project is open, or no timeline is active.
    """
    try:
        conn = _conn()
        timeline = conn.get_current_timeline()
        if timeline is None:
            return "Error: No active timeline. Create or open a timeline first."
        return json.dumps(timeline_to_dict(timeline), indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.resource("resolve://mediapool/structure")
def mediapool_structure_resource() -> str:
    """DaVinci Resolve Media Pool folder/clip structure, as JSON.

    Recurses from the Media Pool root folder (up to a fixed depth of 3,
    truncating each folder's clip list at 50 entries) so responses stay a
    reasonable size. Returns an ``Error: ...`` string if Resolve isn't
    running or no project is open.
    """
    try:
        conn = _conn()
        media_pool = conn.get_media_pool()
        root = media_pool.GetRootFolder()
        if root is None:
            return "Error: Could not get the Media Pool root folder."
        structure = folder_to_dict(root, max_depth=3, max_clips=50)
        return json.dumps(structure, indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"
