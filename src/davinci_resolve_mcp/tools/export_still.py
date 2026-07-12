"""Timeline export / current-frame / thumbnail tools for the DaVinci Resolve MCP server.

Covers exporting the current timeline to an interchange or subtitle/text
file (``Timeline.Export``), exporting the current frame as a still image
(``Project.ExportCurrentFrameAsStill``), and grabbing a PNG thumbnail of the
current frame on the Color page (``Timeline.GetCurrentClipThumbnailImage``).

Per the ownership contract, this module owns ``export_timeline``,
``export_current_frame``, and ``get_current_thumbnail`` — no other module in
this server defines them. ``grab_still``/``grab_all_stills`` (gallery stills)
live in ``tools/color.py`` instead.

All tools reach Resolve lazily via ``_conn()`` inside their bodies — nothing
here imports ``DaVinciResolveScript`` or touches a live Resolve instance at
import time.
"""

from __future__ import annotations

from mcp.server.fastmcp import Image

from ..app import mcp
from ..helpers import _conn, _ok, _require_timeline
from ..resolve_utils import thumbnail_to_png_bytes

# Friendly export_type -> Resolve.EXPORT_* constant name.
_EXPORT_TYPE_MAP: dict[str, str] = {
    "aaf": "EXPORT_AAF",
    "drt": "EXPORT_DRT",
    "edl": "EXPORT_EDL",
    "fcp_7_xml": "EXPORT_FCP_7_XML",
    "fcpxml_1_8": "EXPORT_FCPXML_1_8",
    "fcpxml_1_9": "EXPORT_FCPXML_1_9",
    "fcpxml_1_10": "EXPORT_FCPXML_1_10",
    "hdr_10_profile_a": "EXPORT_HDR_10_PROFILE_A",
    "hdr_10_profile_b": "EXPORT_HDR_10_PROFILE_B",
    "csv": "EXPORT_TEXT_CSV",
    "tab": "EXPORT_TEXT_TAB",
    "otio": "EXPORT_OTIO",
    "ale": "EXPORT_ALE",
    "ale_cdl": "EXPORT_ALE_CDL",
}

# Friendly export_subtype -> Resolve.EXPORT_* constant name.
_EXPORT_SUBTYPE_MAP: dict[str, str] = {
    "none": "EXPORT_NONE",
    "aaf_new": "EXPORT_AAF_NEW",
    "aaf_existing": "EXPORT_AAF_EXISTING",
    "cdl": "EXPORT_CDL",
    "sdl": "EXPORT_SDL",
    "missing_clips": "EXPORT_MISSING_CLIPS",
}


@mcp.tool()
def export_timeline(
    file_path: str,
    export_type: str = "fcpxml_1_10",
    export_subtype: str = "none",
) -> str:
    """
    Export the current timeline to a file (AAF/EDL/XML/OTIO/ALE/etc).

    Parameters:
    - file_path: Destination file path for the exported file.
    - export_type: One of "aaf", "drt", "edl", "fcp_7_xml", "fcpxml_1_8",
      "fcpxml_1_9", "fcpxml_1_10", "hdr_10_profile_a", "hdr_10_profile_b",
      "csv", "tab", "otio", "ale", "ale_cdl". Defaults to "fcpxml_1_10".
    - export_subtype: Only meaningful for some export_type values — for
      "aaf": "aaf_new" or "aaf_existing"; for "edl": "cdl", "sdl",
      "missing_clips", or "none". Defaults to "none" (ignored otherwise).
    """
    try:
        conn = _conn()
        resolve = conn.get_resolve()
        timeline = _require_timeline(conn)

        type_const_name = _EXPORT_TYPE_MAP.get(export_type)
        if type_const_name is None:
            return (
                f"Unknown export_type '{export_type}'. Valid values: "
                f"{sorted(_EXPORT_TYPE_MAP.keys())}"
            )

        sub_const_name = _EXPORT_SUBTYPE_MAP.get(export_subtype)
        if sub_const_name is None:
            return (
                f"Unknown export_subtype '{export_subtype}'. Valid values: "
                f"{sorted(_EXPORT_SUBTYPE_MAP.keys())}"
            )

        export_type_const = getattr(resolve, type_const_name, None)
        if export_type_const is None:
            return (
                f"Export constant '{type_const_name}' is not available in "
                f"this Resolve version."
            )
        export_subtype_const = getattr(resolve, sub_const_name, None)

        if export_subtype_const is None:
            result = timeline.Export(file_path, export_type_const)
        else:
            result = timeline.Export(file_path, export_type_const, export_subtype_const)

        return _ok(
            result,
            f"Timeline exported to '{file_path}' (type={export_type}, subtype={export_subtype})",
            f"Failed to export timeline to '{file_path}'. Check the path is writable and the "
            f"export_type/export_subtype combination is valid for this timeline.",
        )
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def export_current_frame(file_path: str) -> str:
    """
    Export the current frame (playhead position) as a still image.

    Parameters:
    - file_path: Destination file path, including extension (e.g. .png,
      .jpg, .tif, .dpx, .exr).
    """
    try:
        conn = _conn()
        project = conn.get_project()
        if not hasattr(project, "ExportCurrentFrameAsStill"):
            return "ExportCurrentFrameAsStill is not available in this Resolve version."
        result = project.ExportCurrentFrameAsStill(file_path)
        return _ok(
            result,
            f"Current frame exported to '{file_path}'",
            f"Failed to export current frame to '{file_path}'. Check the file path/extension "
            f"and that a timeline is loaded with a valid current frame.",
        )
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_current_thumbnail() -> Image:
    """
    Get a PNG thumbnail of the current frame of the current timeline.

    Works best on the Color page with a clip loaded at the playhead;
    raises a clear error if no thumbnail is available.
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)

        if not hasattr(timeline, "GetCurrentClipThumbnailImage"):
            raise RuntimeError(
                "GetCurrentClipThumbnailImage is not available in this Resolve version."
            )

        thumbnail_data = timeline.GetCurrentClipThumbnailImage()
        if not thumbnail_data or not isinstance(thumbnail_data, dict):
            raise RuntimeError(
                "No thumbnail available. Make sure a timeline is open and the playhead is "
                "positioned on a clip (the Color page usually gives the most reliable results)."
            )

        png_bytes = thumbnail_to_png_bytes(thumbnail_data)
        return Image(data=png_bytes, format="png")
    except Exception as e:
        raise RuntimeError(f"Error getting thumbnail: {e}")
