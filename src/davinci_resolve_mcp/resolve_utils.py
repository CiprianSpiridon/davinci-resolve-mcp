"""Serialization helpers for the DaVinci Resolve MCP server.

Converts DaVinci Resolve API objects (Folder, MediaPoolItem, Timeline,
TimelineItem, node graphs, ...) into plain, JSON-serializable dicts so tool
implementations can hand results straight back to the MCP client.

This module is stdlib-only and MUST NOT import DaVinciResolveScript or touch
a live Resolve connection at import time -- every function here only reads
attributes off objects that are passed in by the caller.
"""

from __future__ import annotations

import base64
import logging
import struct
import zlib
from typing import Any

logger = logging.getLogger("davinci_resolve_mcp.resolve_utils")


def _safe(fn, *args, default=None):
    """Call fn(*args), returning *default* if it raises or returns None.

    Resolve API objects can throw for all sorts of reasons (stale handles,
    unsupported calls on the current page, etc). Serializers should degrade
    gracefully rather than blow up the whole response.
    """
    try:
        value = fn(*args)
        return value if value is not None else default
    except Exception as exc:  # noqa: BLE001 - defensive by design
        logger.debug("Resolve API call failed (%s): %s", getattr(fn, "__name__", fn), exc)
        return default


def clip_to_dict_brief(clip) -> dict:
    """Summarize a MediaPoolItem: name plus a handful of headline properties."""
    result: dict[str, Any] = {"name": _safe(clip.GetName, default="(unnamed)")}

    props = _safe(clip.GetClipProperty)
    if isinstance(props, dict):
        for key in ("Duration", "FPS", "Resolution", "File Path", "Clip Color", "Type"):
            value = props.get(key)
            if value:
                result[key.lower().replace(" ", "_")] = value

    return result


def clip_to_dict(clip) -> dict:
    """Fully serialize a MediaPoolItem: properties, markers, flags, color."""
    result: dict[str, Any] = {"name": _safe(clip.GetName, default="(unnamed)")}

    media_id = _safe(clip.GetMediaId)
    if media_id:
        result["media_id"] = media_id

    props = _safe(clip.GetClipProperty)
    if isinstance(props, dict) and props:
        result["properties"] = props

    markers = _safe(clip.GetMarkers)
    if isinstance(markers, dict) and markers:
        result["markers"] = {str(k): v for k, v in markers.items()}

    flags = _safe(clip.GetFlagList)
    if flags:
        result["flags"] = flags

    color = _safe(clip.GetClipColor)
    if color:
        result["clip_color"] = color

    return result


def folder_to_dict(folder, max_depth: int = 3, max_clips: int = 50, _depth: int = 0) -> dict:
    """Serialize a media pool Folder, recursing into subfolders up to max_depth.

    Clip lists are truncated at max_clips (per folder) to keep responses small;
    a summary string is appended when more clips were omitted.
    """
    result: dict[str, Any] = {
        "name": _safe(folder.GetName, default="(unnamed)"),
        "clips": [],
        "subfolders": [],
    }

    clips = _safe(folder.GetClipList, default=[]) or []
    for i, clip in enumerate(clips):
        if i >= max_clips:
            result["clips"].append(f"... and {len(clips) - max_clips} more clips")
            break
        result["clips"].append(clip_to_dict_brief(clip))
    result["clip_count"] = len(clips)

    if _depth < max_depth:
        subfolders = _safe(folder.GetSubFolderList, default=[]) or []
        for sub in subfolders:
            result["subfolders"].append(
                folder_to_dict(sub, max_depth=max_depth, max_clips=max_clips, _depth=_depth + 1)
            )

    return result


def timeline_to_dict(timeline) -> dict:
    """Serialize a Timeline: frame range, track counts, settings, markers."""
    result: dict[str, Any] = {"name": _safe(timeline.GetName, default="(unnamed)")}

    start_frame = _safe(timeline.GetStartFrame)
    if start_frame is not None:
        result["start_frame"] = start_frame

    end_frame = _safe(timeline.GetEndFrame)
    if end_frame is not None:
        result["end_frame"] = end_frame

    start_tc = _safe(timeline.GetStartTimecode)
    if start_tc:
        result["start_timecode"] = start_tc

    for track_type in ("video", "audio", "subtitle"):
        result[f"{track_type}_track_count"] = _safe(timeline.GetTrackCount, track_type, default=0)

    for setting in ("timelineFrameRate", "timelineResolutionWidth", "timelineResolutionHeight"):
        value = _safe(timeline.GetSetting, setting)
        if value:
            result[setting] = value

    current_tc = _safe(timeline.GetCurrentTimecode)
    if current_tc:
        result["current_timecode"] = current_tc

    markers = _safe(timeline.GetMarkers)
    if isinstance(markers, dict) and markers:
        result["markers"] = {str(k): v for k, v in markers.items()}

    return result


def timeline_item_to_dict(item) -> dict:
    """Summarize a TimelineItem: position, offsets, color, enabled state."""
    result: dict[str, Any] = {"name": _safe(item.GetName, default="(unnamed)")}

    for getter, key in (
        (item.GetStart, "start"),
        (item.GetEnd, "end"),
        (item.GetDuration, "duration"),
        (item.GetLeftOffset, "left_offset"),
        (item.GetRightOffset, "right_offset"),
    ):
        value = _safe(getter)
        if value is not None:
            result[key] = value

    color = _safe(item.GetClipColor)
    if color:
        result["clip_color"] = color

    enabled = _safe(item.GetClipEnabled)
    if enabled is not None:
        result["enabled"] = enabled

    return result


def timeline_item_to_dict_full(item) -> dict:
    """Fully serialize a TimelineItem, layering on properties/markers/flags/Fusion."""
    result = timeline_item_to_dict(item)

    try:
        props = item.GetProperty()
        if isinstance(props, dict) and props:
            result["properties"] = props
    except Exception as exc:  # noqa: BLE001
        logger.debug("timeline_item_to_dict_full: GetProperty() failed: %s", exc)
        known_props: dict[str, Any] = {}
        for key in (
            "Pan", "Tilt", "ZoomX", "ZoomY", "ZoomGang", "RotationAngle", "Opacity",
            "AnchorPointX", "AnchorPointY", "CropLeft", "CropRight", "CropTop", "CropBottom",
            "CropSoftness", "CropRetain", "FlipX", "FlipY", "CompositeMode", "Scaling",
            "RetimeProcess",
        ):
            value = _safe(item.GetProperty, key)
            if value is not None:
                known_props[key] = value
        if known_props:
            result["properties"] = known_props

    markers = _safe(item.GetMarkers)
    if isinstance(markers, dict) and markers:
        result["markers"] = {str(k): v for k, v in markers.items()}

    flags = _safe(item.GetFlagList)
    if flags:
        result["flags"] = flags

    comp_count = _safe(item.GetFusionCompCount)
    if comp_count:
        result["fusion_comp_count"] = comp_count
        names = _safe(item.GetFusionCompNameList)
        if names:
            result["fusion_comp_names"] = names

    mpi = _safe(item.GetMediaPoolItem)
    if mpi:
        mpi_name = _safe(mpi.GetName)
        if mpi_name:
            result["media_pool_item"] = mpi_name

    return result


def node_graph_to_dict(graph) -> dict:
    """Serialize a color page node graph: node count, per-node label + LUT."""
    num_nodes = _safe(graph.GetNumNodes, default=0) or 0
    result: dict[str, Any] = {"num_nodes": num_nodes, "nodes": []}

    for i in range(1, num_nodes + 1):
        node_info: dict[str, Any] = {
            "index": i,
            "label": _safe(graph.GetNodeLabel, i, default=""),
        }
        lut = _safe(graph.GetLUT, i)
        if lut:
            node_info["lut"] = lut
        result["nodes"].append(node_info)

    return result


def _write_png_chunk(chunk_type: bytes, chunk_data: bytes) -> bytes:
    """Build one length-prefixed, CRC-terminated PNG chunk."""
    body = chunk_type + chunk_data
    crc = struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    return struct.pack(">I", len(chunk_data)) + body + crc


def _encode_png(width: int, height: int, rgb: bytes) -> bytes:
    """Encode raw, unfiltered 8-bit RGB pixel data as a minimal PNG (stdlib only)."""
    signature = b"\x89PNG\r\n\x1a\n"

    # Color type 2 = truecolor (RGB), bit depth 8, no interlacing.
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr = _write_png_chunk(b"IHDR", ihdr_data)

    row_size = width * 3
    raw_rows = bytearray()
    for y in range(height):
        raw_rows.append(0)  # filter type 0 (none) for every scanline
        raw_rows += rgb[y * row_size : (y + 1) * row_size]

    idat = _write_png_chunk(b"IDAT", zlib.compress(bytes(raw_rows)))
    iend = _write_png_chunk(b"IEND", b"")

    return signature + ihdr + idat + iend


def thumbnail_to_png_bytes(thumbnail_data: dict) -> bytes:
    """Convert a Resolve thumbnail dict ({width, height, data: base64 RGB}) to PNG bytes.

    ``data`` is the base64-encoded raw 8-bit RGB pixel buffer as returned by
    e.g. ``Timeline.GetCurrentClipThumbnailImage()``. Raises ValueError if the
    dict is missing required fields or the decoded buffer is too short for the
    stated dimensions.
    """
    width = thumbnail_data.get("width", 0) or 0
    height = thumbnail_data.get("height", 0) or 0
    data = thumbnail_data.get("data", "")

    if not width or not height or not data:
        raise ValueError(
            f"Invalid thumbnail data: width={width}, height={height}, data_len={len(data or '')}"
        )

    raw_rgb = base64.b64decode(data)
    expected_size = width * height * 3
    if len(raw_rgb) < expected_size:
        raise ValueError(
            f"Thumbnail data too short: got {len(raw_rgb)} bytes, "
            f"expected {expected_size} for {width}x{height} RGB"
        )

    return _encode_png(width, height, raw_rgb)


def safe_serialize(obj: Any) -> Any:
    """Recursively coerce *obj* into something json.dumps can handle.

    None, bool/int/float/str pass through unchanged. Dicts and lists/tuples
    are recursed into (dict keys are stringified). Anything else (Resolve API
    objects, custom classes, ...) falls back to str(), or a placeholder string
    if even that fails.
    """
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): safe_serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [safe_serialize(item) for item in obj]
    try:
        return str(obj)
    except Exception:  # noqa: BLE001
        return "<non-serializable>"
