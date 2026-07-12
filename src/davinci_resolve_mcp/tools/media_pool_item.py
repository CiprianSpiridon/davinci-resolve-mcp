"""Media pool clip (``MediaPoolItem``) tools for the DaVinci Resolve MCP server.

Covers per-clip property and metadata get/set, clip markers (get/add/
delete-by-color/update custom data), flags (get/add/clear), clip color
(get/set/clear), proxy media link/unlink, replacing a clip's source media,
and mark in/out points (get/set/clear).

Clips are looked up by name within the media pool's *current* folder (the
folder the user last navigated to in the Resolve UI, via
``MediaPool.GetCurrentFolder()``) — the same scoping used by
``append_to_timeline``-style tools elsewhere in this server. An unknown name
returns a clear, actionable string listing the clips that *are* available in
that folder, rather than raising.

All tools reach Resolve lazily via ``_conn()`` inside their bodies — nothing
here imports ``DaVinciResolveScript`` or touches a live Resolve instance at
import time. Object: ``MediaPoolItem`` (see ``resolve_utils.clip_to_dict``,
resolve_utils.py:52-76, for the read-only serializer used elsewhere).
"""

from __future__ import annotations

import json

from ..app import mcp
from ..helpers import _coerce_value, _conn, _ok


def _find_clip(clip_name: str):
    """Look up a MediaPoolItem by name in the media pool's current folder.

    Raises ``RuntimeError`` with a clear message (including the names of
    clips that *are* available) if the current folder is unreachable or the
    named clip isn't in it.
    """
    conn = _conn()
    media_pool = conn.get_media_pool()
    folder = media_pool.GetCurrentFolder()
    if folder is None:
        raise RuntimeError("Could not get the current media pool folder.")

    clips = folder.GetClipList() or []
    for clip in clips:
        if clip.GetName() == clip_name:
            return clip

    available = sorted({c.GetName() for c in clips if c.GetName()})
    raise RuntimeError(
        f"Clip '{clip_name}' not found in the current media pool folder. "
        f"Available clips: {available}"
    )


# ── Clip properties ─────────────────────────────────────────────────────────


@mcp.tool()
def get_clip_property(clip_name: str, property_name: str = "") -> str:
    """Get one or all properties of a media pool clip, as JSON.

    Parameters:
    - clip_name: name of the clip, as it appears in the current media pool
      folder.
    - property_name: specific property key (e.g. "Clip Name", "Duration",
      "FPS", "File Path", "Comments"). Leave empty to get all properties.
    """
    try:
        clip = _find_clip(clip_name)
        if not hasattr(clip, "GetClipProperty"):
            return "Error: GetClipProperty is not available on this clip object."
        if property_name:
            value = clip.GetClipProperty(property_name)
            if value is None or value == "":
                return f"Error: Property '{property_name}' not found or has no value on clip '{clip_name}'."
            return json.dumps({property_name: value}, default=str)
        props = clip.GetClipProperty()
        if not isinstance(props, dict):
            return f"Error: Could not retrieve properties for clip '{clip_name}'."
        return json.dumps(props, indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def set_clip_property(clip_name: str, property_name: str, property_value: str) -> str:
    """Set a property on a media pool clip.

    Parameters:
    - clip_name: name of the clip, as it appears in the current media pool
      folder.
    - property_name: property key to set (e.g. "Clip Name", "Comments").
    - property_value: new value as a string. Numeric-looking strings are
      coerced to int/float and "true"/"false" to bool before being passed to
      the Resolve API; anything else is passed through as a string.
    """
    try:
        clip = _find_clip(clip_name)
        if not hasattr(clip, "SetClipProperty"):
            return "Error: SetClipProperty is not available on this clip object."
        coerced = _coerce_value(property_value)
        result = clip.SetClipProperty(property_name, coerced)
        return _ok(
            result,
            f"Set property '{property_name}' = {coerced!r} on clip '{clip_name}'.",
            f"Error: Failed to set property '{property_name}' = {coerced!r} on "
            f"clip '{clip_name}'. The key may be invalid or read-only.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Clip metadata ────────────────────────────────────────────────────────


@mcp.tool()
def get_clip_metadata(clip_name: str, metadata_type: str = "") -> str:
    """Get one or all metadata fields of a media pool clip, as JSON.

    Parameters:
    - clip_name: name of the clip, as it appears in the current media pool
      folder.
    - metadata_type: specific metadata key (e.g. "Camera Type", "Shot", "Scene").
      Leave empty to get all metadata fields.
    """
    try:
        clip = _find_clip(clip_name)
        if not hasattr(clip, "GetMetadata"):
            return "Error: GetMetadata is not available on this clip object."
        if metadata_type:
            value = clip.GetMetadata(metadata_type)
            if value is None or value == "":
                return f"Error: Metadata field '{metadata_type}' not found or has no value on clip '{clip_name}'."
            return json.dumps({metadata_type: value}, default=str)
        metadata = clip.GetMetadata()
        if not isinstance(metadata, dict):
            return f"Error: Could not retrieve metadata for clip '{clip_name}'."
        return json.dumps(metadata, indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def set_clip_metadata(clip_name: str, metadata_type: str, metadata_value: str) -> str:
    """Set a metadata field on a media pool clip.

    Parameters:
    - clip_name: name of the clip, as it appears in the current media pool
      folder.
    - metadata_type: metadata key to set (e.g. "Camera Type", "Shot", "Scene").
    - metadata_value: new value as a string. Numeric-looking strings are
      coerced to int/float and "true"/"false" to bool before being passed to
      the Resolve API; anything else is passed through as a string.
    """
    try:
        clip = _find_clip(clip_name)
        if not hasattr(clip, "SetMetadata"):
            return "Error: SetMetadata is not available on this clip object."
        coerced = _coerce_value(metadata_value)
        result = clip.SetMetadata(metadata_type, coerced)
        return _ok(
            result,
            f"Set metadata '{metadata_type}' = {coerced!r} on clip '{clip_name}'.",
            f"Error: Failed to set metadata '{metadata_type}' = {coerced!r} on "
            f"clip '{clip_name}'. The key may be invalid or read-only.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Clip markers ─────────────────────────────────────────────────────────


@mcp.tool()
def get_clip_markers(clip_name: str) -> str:
    """Get all markers on a media pool clip, as JSON keyed by frame number.

    Parameters:
    - clip_name: name of the clip, as it appears in the current media pool
      folder.
    """
    try:
        clip = _find_clip(clip_name)
        if not hasattr(clip, "GetMarkers"):
            return "Error: GetMarkers is not available on this clip object."
        markers = clip.GetMarkers()
        if not markers:
            return json.dumps({})
        return json.dumps({str(k): v for k, v in markers.items()}, indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def add_clip_marker(
    clip_name: str,
    frame_id: int,
    color: str,
    name: str,
    note: str = "",
    duration: int = 1,
    custom_data: str = "",
) -> str:
    """Add a marker to a media pool clip.

    Parameters:
    - clip_name: name of the clip, as it appears in the current media pool
      folder.
    - frame_id: frame number (relative to the clip) for the marker.
    - color: marker color, e.g. "Blue", "Cyan", "Green", "Yellow", "Red",
      "Pink", "Purple", "Fuchsia", "Rose", "Lavender", "Sky", "Mint",
      "Lemon", "Sand", "Cocoa", "Cream".
    - name: marker name.
    - note: optional marker note.
    - duration: marker duration in frames (default 1).
    - custom_data: optional custom data string, retrievable/searchable later
      via the marker's custom data field.
    """
    try:
        clip = _find_clip(clip_name)
        if not hasattr(clip, "AddMarker"):
            return "Error: AddMarker is not available on this clip object."
        result = clip.AddMarker(frame_id, color, name, note, duration, custom_data)
        return _ok(
            result,
            f"Added {color} marker '{name}' at frame {frame_id} on clip '{clip_name}'.",
            f"Error: Failed to add marker at frame {frame_id} on clip '{clip_name}'. "
            "A marker may already exist at that frame, or the color/frame is invalid.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def delete_clip_markers_by_color(clip_name: str, color: str) -> str:
    """Delete all markers of a given color from a media pool clip.

    Parameters:
    - clip_name: name of the clip, as it appears in the current media pool
      folder.
    - color: marker color to delete (e.g. "Blue"), or "All" to delete every
      marker on the clip.
    """
    try:
        clip = _find_clip(clip_name)
        if not hasattr(clip, "DeleteMarkersByColor"):
            return "Error: DeleteMarkersByColor is not available on this clip object."
        result = clip.DeleteMarkersByColor(color)
        return _ok(
            result,
            f"Deleted {color} markers from clip '{clip_name}'.",
            f"Error: Failed to delete {color} markers from clip '{clip_name}'. "
            "There may be no markers of that color.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def update_clip_marker_custom_data(clip_name: str, frame_id: int, custom_data: str) -> str:
    """Update the custom data string of an existing marker on a media pool clip.

    Parameters:
    - clip_name: name of the clip, as it appears in the current media pool
      folder.
    - frame_id: frame number of the existing marker to update.
    - custom_data: new custom data string for that marker.
    """
    try:
        clip = _find_clip(clip_name)
        if not hasattr(clip, "UpdateMarkerCustomData"):
            return "Error: UpdateMarkerCustomData is not available on this clip object."
        result = clip.UpdateMarkerCustomData(frame_id, custom_data)
        return _ok(
            result,
            f"Updated custom data for marker at frame {frame_id} on clip '{clip_name}'.",
            f"Error: Failed to update custom data for marker at frame {frame_id} "
            f"on clip '{clip_name}'. No marker may exist at that frame.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Clip flags ───────────────────────────────────────────────────────────


@mcp.tool()
def get_clip_flags(clip_name: str) -> str:
    """Get the list of flag colors set on a media pool clip, as JSON.

    Parameters:
    - clip_name: name of the clip, as it appears in the current media pool
      folder.
    """
    try:
        clip = _find_clip(clip_name)
        if not hasattr(clip, "GetFlagList"):
            return "Error: GetFlagList is not available on this clip object."
        flags = clip.GetFlagList()
        return json.dumps(flags or [], default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def add_clip_flag(clip_name: str, color: str) -> str:
    """Add a flag of the given color to a media pool clip.

    Parameters:
    - clip_name: name of the clip, as it appears in the current media pool
      folder.
    - color: flag color, e.g. "Blue", "Cyan", "Green", "Yellow", "Red",
      "Pink", "Purple", "Fuchsia", "Rose", "Lavender", "Sky", "Mint",
      "Lemon", "Sand", "Cocoa", "Cream".
    """
    try:
        clip = _find_clip(clip_name)
        if not hasattr(clip, "AddFlag"):
            return "Error: AddFlag is not available on this clip object."
        result = clip.AddFlag(color)
        return _ok(
            result,
            f"Added {color} flag to clip '{clip_name}'.",
            f"Error: Failed to add {color} flag to clip '{clip_name}'.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def clear_clip_flags(clip_name: str, color: str = "All") -> str:
    """Clear flags of a given color (or all flags) from a media pool clip.

    Parameters:
    - clip_name: name of the clip, as it appears in the current media pool
      folder.
    - color: flag color to clear. Defaults to "All", clearing every flag on
      the clip.
    """
    try:
        clip = _find_clip(clip_name)
        if not hasattr(clip, "ClearFlags"):
            return "Error: ClearFlags is not available on this clip object."
        result = clip.ClearFlags(color)
        return _ok(
            result,
            f"Cleared {color} flags from clip '{clip_name}'.",
            f"Error: Failed to clear {color} flags from clip '{clip_name}'.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Clip color ───────────────────────────────────────────────────────────


@mcp.tool()
def get_clip_color(clip_name: str) -> str:
    """Get the clip color (as shown in the media pool UI) of a media pool clip.

    Parameters:
    - clip_name: name of the clip, as it appears in the current media pool
      folder.

    Returns the color name, or an explanatory string if no clip color is set.
    """
    try:
        clip = _find_clip(clip_name)
        if not hasattr(clip, "GetClipColor"):
            return "Error: GetClipColor is not available on this clip object."
        color = clip.GetClipColor()
        if not color:
            return f"Clip '{clip_name}' has no clip color set."
        return color
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def set_clip_color(clip_name: str, color: str) -> str:
    """Set the clip color (as shown in the media pool UI) of a media pool clip.

    Parameters:
    - clip_name: name of the clip, as it appears in the current media pool
      folder.
    - color: color name, e.g. "Orange", "Apricot", "Yellow", "Lime", "Olive",
      "Green", "Teal", "Navy", "Blue", "Purple", "Violet", "Pink", "Tan",
      "Beige", "Brown", "Chocolate".
    """
    try:
        clip = _find_clip(clip_name)
        if not hasattr(clip, "SetClipColor"):
            return "Error: SetClipColor is not available on this clip object."
        result = clip.SetClipColor(color)
        return _ok(
            result,
            f"Set clip color of '{clip_name}' to {color}.",
            f"Error: Failed to set clip color of '{clip_name}' to {color}. "
            "The color name may be invalid.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def clear_clip_color(clip_name: str) -> str:
    """Clear the clip color of a media pool clip.

    Parameters:
    - clip_name: name of the clip, as it appears in the current media pool
      folder.
    """
    try:
        clip = _find_clip(clip_name)
        if not hasattr(clip, "ClearClipColor"):
            return "Error: ClearClipColor is not available on this clip object."
        result = clip.ClearClipColor()
        return _ok(
            result,
            f"Cleared clip color of '{clip_name}'.",
            f"Error: Failed to clear clip color of '{clip_name}'.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Proxy media ──────────────────────────────────────────────────────────


@mcp.tool()
def link_proxy_media(clip_name: str, proxy_file_path: str) -> str:
    """Link a proxy media file to a media pool clip.

    Parameters:
    - clip_name: name of the clip, as it appears in the current media pool
      folder.
    - proxy_file_path: absolute path to the proxy media file.
    """
    try:
        clip = _find_clip(clip_name)
        if not hasattr(clip, "LinkProxyMedia"):
            return "Error: LinkProxyMedia is not available on this clip object."
        result = clip.LinkProxyMedia(proxy_file_path)
        return _ok(
            result,
            f"Linked proxy media '{proxy_file_path}' to clip '{clip_name}'.",
            f"Error: Failed to link proxy media '{proxy_file_path}' to clip "
            f"'{clip_name}'. Check that the file path exists and is a valid "
            "media file.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def unlink_proxy_media(clip_name: str) -> str:
    """Unlink the proxy media file from a media pool clip.

    Parameters:
    - clip_name: name of the clip, as it appears in the current media pool
      folder.
    """
    try:
        clip = _find_clip(clip_name)
        if not hasattr(clip, "UnlinkProxyMedia"):
            return "Error: UnlinkProxyMedia is not available on this clip object."
        result = clip.UnlinkProxyMedia()
        return _ok(
            result,
            f"Unlinked proxy media from clip '{clip_name}'.",
            f"Error: Failed to unlink proxy media from clip '{clip_name}'. "
            "The clip may not have a proxy linked.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Replace clip ─────────────────────────────────────────────────────────


@mcp.tool()
def replace_clip(clip_name: str, replacement_file_path: str) -> str:
    """Replace a media pool clip's underlying source media with another file.

    Parameters:
    - clip_name: name of the clip to replace, as it appears in the current
      media pool folder.
    - replacement_file_path: absolute path to the replacement media file.
    """
    try:
        clip = _find_clip(clip_name)
        if not hasattr(clip, "ReplaceClip"):
            return "Error: ReplaceClip is not available on this clip object."
        result = clip.ReplaceClip(replacement_file_path)
        return _ok(
            result,
            f"Replaced clip '{clip_name}' with '{replacement_file_path}'.",
            f"Error: Failed to replace clip '{clip_name}' with "
            f"'{replacement_file_path}'. Check that the file path exists and "
            "is a valid media file.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Mark in/out ──────────────────────────────────────────────────────────


@mcp.tool()
def get_clip_mark_in_out(clip_name: str) -> str:
    """Get the mark in/out points of a media pool clip, as JSON.

    Parameters:
    - clip_name: name of the clip, as it appears in the current media pool
      folder.
    """
    try:
        clip = _find_clip(clip_name)
        if not hasattr(clip, "GetMarkInOut"):
            return "Error: GetMarkInOut is not available on this clip object."
        result = clip.GetMarkInOut()
        if not result:
            return f"Clip '{clip_name}' has no mark in/out set."
        return json.dumps(result, indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def set_clip_mark_in_out(clip_name: str, mark_in: int, mark_out: int) -> str:
    """Set the mark in/out points of a media pool clip.

    Parameters:
    - clip_name: name of the clip, as it appears in the current media pool
      folder.
    - mark_in: in-point frame number (relative to the clip).
    - mark_out: out-point frame number (relative to the clip). Must be
      greater than mark_in.
    """
    try:
        clip = _find_clip(clip_name)
        if not hasattr(clip, "SetMarkInOut"):
            return "Error: SetMarkInOut is not available on this clip object."
        result = clip.SetMarkInOut(mark_in, mark_out)
        return _ok(
            result,
            f"Set mark in/out on clip '{clip_name}' to [{mark_in}, {mark_out}].",
            f"Error: Failed to set mark in/out on clip '{clip_name}' to "
            f"[{mark_in}, {mark_out}]. mark_out must be greater than mark_in "
            "and within the clip's frame range.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def clear_clip_mark_in_out(clip_name: str) -> str:
    """Clear the mark in/out points of a media pool clip.

    Parameters:
    - clip_name: name of the clip, as it appears in the current media pool
      folder.
    """
    try:
        clip = _find_clip(clip_name)
        if not hasattr(clip, "ClearMarkInOut"):
            return "Error: ClearMarkInOut is not available on this clip object."
        result = clip.ClearMarkInOut()
        return _ok(
            result,
            f"Cleared mark in/out on clip '{clip_name}'.",
            f"Error: Failed to clear mark in/out on clip '{clip_name}'.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"
