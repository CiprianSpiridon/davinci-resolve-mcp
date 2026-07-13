"""``TimelineItem`` property, marker, clip-attribute, and take tools.

This module owns the "operate on a single clip instance placed on the
timeline" surface of the server. All tools locate their target item via
``helpers._get_timeline_item(track_type, track_index, item_index)`` — a
1-based track index paired with a 0-based item index within that track's
item list (as returned by ``timeline.get_timeline_items``) — and return a
clear, actionable error string when the locator or the underlying Resolve
call fails, rather than raising.

Covers:

- ``get_timeline_item_properties`` / ``set_timeline_item_property``
  (``TimelineItem.GetProperty`` / ``SetProperty``) — the generic transform/
  crop/composite property bag (Pan, Tilt, ZoomX/Y, RotationAngle, Opacity,
  Crop*, Flip*, CompositeMode, Scaling, RetimeProcess, ...).
- Item-level markers: ``get_item_markers`` / ``add_item_marker`` /
  ``delete_item_marker_at_frame`` (``TimelineItem.GetMarkers`` /
  ``AddMarker`` / ``DeleteMarkerAtFrame``) — distinct from the *timeline*
  markers owned by ``tools/timeline_edit.py``.
- Clip color: ``get_item_clip_color`` / ``set_item_clip_color``
  (``TimelineItem.GetClipColor`` / ``SetClipColor``) — distinct from the
  *media pool clip* color tools in ``tools/media_pool_item.py``
  (``get_clip_color`` / ``set_clip_color``), which operate on a different
  object (``MediaPoolItem``).
- Enabled state: ``get_item_enabled`` / ``set_item_enabled``
  (``TimelineItem.GetClipEnabled`` / ``SetClipEnabled``).
- Source (trim) range: ``get_item_source_range``
  (``TimelineItem.GetSourceStartFrame`` / ``GetSourceEndFrame`` and the
  timeline-relative ``GetLeftOffset`` / ``GetRightOffset``).
- Takes: ``get_take_count`` / ``add_take`` / ``select_take`` /
  ``finalize_take`` / ``delete_take`` (``TimelineItem.GetTakesCount`` /
  ``GetSelectedTakeIndex`` / ``AddTake`` / ``SelectTakeByIndex`` /
  ``FinalizeTake`` / ``DeleteTakeByIndex``).
- Linkage & audio routing (read-only): ``get_linked_items`` /
  ``get_track_type_and_index`` / ``get_source_audio_channel_mapping``
  (``TimelineItem.GetLinkedItems`` / ``GetTrackTypeAndIndex`` /
  ``GetSourceAudioChannelMapping``).

Per the ownership contract, ``get_timeline_item_properties`` and
``set_timeline_item_property`` are defined here and nowhere else in this
server; ``detect_scene_cuts`` / ``insert_*`` live in ``tools/timeline_edit.py``
and ``grab_still`` lives in ``tools/color.py``.

All tools reach Resolve lazily via ``_conn()`` / ``_get_timeline_item()``
inside their bodies — nothing here imports ``DaVinciResolveScript`` or
touches a live Resolve instance at import time. Object: ``TimelineItem``
(see ``resolve_utils.timeline_item_to_dict_full``, resolve_utils.py:133-211,
for the read-only serializer this module reuses).
"""

from __future__ import annotations

import json

from ..app import mcp
from ..helpers import (
    CLIP_COLORS,
    MARKER_COLORS,
    _check_choice,
    _coerce_value,
    _conn,
    _get_timeline_item,
    _ok,
)
from ..resolve_utils import timeline_item_to_dict, timeline_item_to_dict_full


def _find_media_pool_clip(clip_name: str):
    """Look up a MediaPoolItem by name in the media pool's current folder.

    Used by :func:`add_take` to resolve a clip name into the MediaPoolItem
    object the Resolve API's ``AddTake`` call expects. Raises
    ``RuntimeError`` with a clear message (including the names of clips
    that *are* available) if the current folder is unreachable or the
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


# ── Properties ───────────────────────────────────────────────────────────


@mcp.tool()
def get_timeline_item_properties(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Get all known properties, markers, flags, and Fusion/clip info for a timeline item, as JSON.

    Parameters:
    - track_type: one of "video", "audio", "subtitle". Defaults to "video".
    - track_index: 1-based track index (as shown in the Edit page).
    - item_index: 0-based index of the item within that track's item list
      (as returned by get_timeline_items).
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        return json.dumps(timeline_item_to_dict_full(item), indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def set_timeline_item_property(
    property_key: str,
    property_value: str,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Set one property on a timeline item.

    Parameters:
    - property_key: property name, e.g. "Pan", "Tilt", "ZoomX", "ZoomY",
      "ZoomGang", "RotationAngle", "Opacity", "AnchorPointX", "AnchorPointY",
      "CropLeft", "CropRight", "CropTop", "CropBottom", "CropSoftness",
      "FlipX", "FlipY", "CompositeMode", "Scaling", "RetimeProcess".
    - property_value: value to set, as a string. Auto-coerced to int/float/
      bool where the string looks numeric/boolean (e.g. "1.0" -> 1,
      "true" -> True); otherwise passed through as a string.
    - track_type: one of "video", "audio", "subtitle". Defaults to "video".
    - track_index: 1-based track index.
    - item_index: 0-based index of the item within that track's item list.
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        value = _coerce_value(property_value)
        result = item.SetProperty(property_key, value)
        return _ok(
            result,
            f"Set {property_key} = {value!r} on {track_type} track "
            f"{track_index} item {item_index}.",
            f"Error: Failed to set {property_key}={value!r}. Check that "
            f"'{property_key}' is a valid property for this item's type and "
            "that the value is within the accepted range.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Item markers ─────────────────────────────────────────────────────────
# Distinct from the timeline-level markers in tools/timeline_edit.py — these
# live on the TimelineItem itself, with frame_id relative to the clip's
# source media rather than the overall timeline.


@mcp.tool()
def get_item_markers(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Get all markers on a timeline item, as JSON keyed by (source) frame number.

    Parameters:
    - track_type: one of "video", "audio", "subtitle". Defaults to "video".
    - track_index: 1-based track index.
    - item_index: 0-based index of the item within that track's item list.
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        markers = item.GetMarkers()
        if not markers:
            return json.dumps({})
        return json.dumps({str(k): v for k, v in markers.items()}, indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def add_item_marker(
    frame_id: int,
    color: str,
    name: str,
    note: str = "",
    duration: int = 1,
    custom_data: str = "",
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Add a marker to a timeline item.

    Parameters:
    - frame_id: frame position, relative to the item's source media, for
      the marker.
    - color: marker color, e.g. "Blue", "Cyan", "Green", "Yellow", "Red",
      "Pink", "Purple", "Fuchsia", "Rose", "Lavender", "Sky", "Mint",
      "Lemon", "Sand", "Cocoa", "Cream".
    - name: marker name.
    - note: optional marker note.
    - duration: marker duration in frames (default 1).
    - custom_data: optional custom data string, retrievable via get_item_markers.
    - track_type: one of "video", "audio", "subtitle". Defaults to "video".
    - track_index: 1-based track index.
    - item_index: 0-based index of the item within that track's item list.
    """
    try:
        color, err = _check_choice(color, MARKER_COLORS, "marker color")
        if err:
            return err
        item = _get_timeline_item(track_type, track_index, item_index)
        result = item.AddMarker(frame_id, color, name, note, duration, custom_data)
        return _ok(
            result,
            f"Added {color} marker '{name}' at frame {frame_id} on "
            f"{track_type} track {track_index} item {item_index}.",
            f"Error: Failed to add marker at frame {frame_id}. A marker may "
            "already exist at that frame, or the color/frame is invalid.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def delete_item_marker_at_frame(
    frame_id: int,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Delete the marker at a specific (source) frame on a timeline item.

    Parameters:
    - frame_id: frame number of the marker to delete.
    - track_type: one of "video", "audio", "subtitle". Defaults to "video".
    - track_index: 1-based track index.
    - item_index: 0-based index of the item within that track's item list.
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        if not hasattr(item, "DeleteMarkerAtFrame"):
            return "Error: DeleteMarkerAtFrame is not available on this timeline item."
        result = item.DeleteMarkerAtFrame(frame_id)
        return _ok(
            result,
            f"Deleted marker at frame {frame_id}.",
            f"Error: Failed to delete marker at frame {frame_id}. No marker "
            "may exist at that frame.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Clip color / enabled state ──────────────────────────────────────────


@mcp.tool()
def get_item_clip_color(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Get the clip color of a timeline item.

    Parameters:
    - track_type: one of "video", "audio", "subtitle". Defaults to "video".
    - track_index: 1-based track index.
    - item_index: 0-based index of the item within that track's item list.
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        color = item.GetClipColor()
        return color or "No clip color is set on this item."
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def set_item_clip_color(
    color: str,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Set the clip color of a timeline item.

    Parameters:
    - color: clip color name, e.g. "Orange", "Apricot", "Yellow", "Lime",
      "Olive", "Green", "Teal", "Navy", "Blue", "Purple", "Violet",
      "Pink", "Tan", "Beige", "Brown", "Chocolate".
    - track_type: one of "video", "audio", "subtitle". Defaults to "video".
    - track_index: 1-based track index.
    - item_index: 0-based index of the item within that track's item list.
    """
    try:
        color, err = _check_choice(color, CLIP_COLORS, "clip color")
        if err:
            return err
        item = _get_timeline_item(track_type, track_index, item_index)
        result = item.SetClipColor(color)
        return _ok(
            result,
            f"Set clip color to '{color}' on {track_type} track "
            f"{track_index} item {item_index}.",
            f"Error: Failed to set clip color to '{color}'. Check it's a "
            "valid Resolve clip color name.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def get_item_enabled(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Get whether a timeline item is enabled (not disabled/muted), as JSON.

    Parameters:
    - track_type: one of "video", "audio", "subtitle". Defaults to "video".
    - track_index: 1-based track index.
    - item_index: 0-based index of the item within that track's item list.
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        enabled = item.GetClipEnabled()
        return json.dumps({"enabled": bool(enabled)})
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def set_item_enabled(
    enabled: bool = True,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Enable or disable (mute) a timeline item.

    Parameters:
    - enabled: True to enable the item, False to disable it.
    - track_type: one of "video", "audio", "subtitle". Defaults to "video".
    - track_index: 1-based track index.
    - item_index: 0-based index of the item within that track's item list.
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        result = item.SetClipEnabled(bool(enabled))
        return _ok(
            result,
            f"{'Enabled' if enabled else 'Disabled'} {track_type} track "
            f"{track_index} item {item_index}.",
            "Error: Failed to set the item's enabled state.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Source (trim) range ──────────────────────────────────────────────────


@mcp.tool()
def get_item_source_range(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Get the source-media trim range of a timeline item, as JSON.

    Includes source_start_frame / source_end_frame (frames within the
    underlying source media) alongside left_offset / right_offset (frames
    trimmed off the start/end relative to the item's untrimmed duration),
    when the running Resolve version exposes them.

    Parameters:
    - track_type: one of "video", "audio", "subtitle". Defaults to "video".
    - track_index: 1-based track index.
    - item_index: 0-based index of the item within that track's item list.
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        result: dict = {}

        if hasattr(item, "GetSourceStartFrame"):
            result["source_start_frame"] = item.GetSourceStartFrame()
        if hasattr(item, "GetSourceEndFrame"):
            result["source_end_frame"] = item.GetSourceEndFrame()
        if hasattr(item, "GetLeftOffset"):
            result["left_offset"] = item.GetLeftOffset()
        if hasattr(item, "GetRightOffset"):
            result["right_offset"] = item.GetRightOffset()
        if hasattr(item, "GetDuration"):
            result["duration"] = item.GetDuration()

        if not result:
            return "Error: Source range info is not available on this timeline item."
        return json.dumps(result, indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Takes ────────────────────────────────────────────────────────────────


@mcp.tool()
def get_take_count(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Get the number of takes on a timeline item and the currently selected take index, as JSON.

    Parameters:
    - track_type: one of "video", "audio", "subtitle". Defaults to "video".
    - track_index: 1-based track index.
    - item_index: 0-based index of the item within that track's item list.
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        if not hasattr(item, "GetTakesCount"):
            return "Error: Takes are not available on this timeline item."
        count = item.GetTakesCount() or 0
        selected = item.GetSelectedTakeIndex() if hasattr(item, "GetSelectedTakeIndex") else None
        return json.dumps({"take_count": count, "selected_take_index": selected})
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def add_take(
    clip_name: str,
    start_frame: int = 0,
    end_frame: int = 0,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Add a take to a timeline item, sourced from a media pool clip.

    Parameters:
    - clip_name: name of the MediaPoolItem (looked up in the media pool's
      current folder) to add as a new take.
    - start_frame: source start frame for the take. Leave 0 (with
      end_frame also 0) to use the clip's full range.
    - end_frame: source end frame for the take.
    - track_type: one of "video", "audio", "subtitle". Defaults to "video".
    - track_index: 1-based track index.
    - item_index: 0-based index of the item within that track's item list.
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        if not hasattr(item, "AddTake"):
            return "Error: Takes are not available on this timeline item."
        clip = _find_media_pool_clip(clip_name)
        if start_frame or end_frame:
            result = item.AddTake(clip, start_frame, end_frame)
        else:
            result = item.AddTake(clip)
        return _ok(
            result,
            f"Added take from clip '{clip_name}' to {track_type} track "
            f"{track_index} item {item_index}.",
            f"Error: Failed to add take from clip '{clip_name}'. Check the "
            "clip is compatible with this item's type and the frame range "
            "is valid.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def select_take(
    take_index: int,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Select a take on a timeline item by index.

    Parameters:
    - take_index: 1-based take index (see get_take_count for the valid range).
    - track_type: one of "video", "audio", "subtitle". Defaults to "video".
    - track_index: 1-based track index.
    - item_index: 0-based index of the item within that track's item list.
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        if not hasattr(item, "SelectTakeByIndex"):
            return "Error: Takes are not available on this timeline item."
        result = item.SelectTakeByIndex(take_index)
        return _ok(
            result,
            f"Selected take {take_index}.",
            f"Error: Failed to select take {take_index}. Check the take "
            "index is valid (1-based; see get_take_count).",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def finalize_take(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Finalize the currently selected take on a timeline item, replacing the item with that take.

    Parameters:
    - track_type: one of "video", "audio", "subtitle". Defaults to "video".
    - track_index: 1-based track index.
    - item_index: 0-based index of the item within that track's item list.
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        if not hasattr(item, "FinalizeTake"):
            return "Error: Takes are not available on this timeline item."
        result = item.FinalizeTake()
        return _ok(
            result,
            "Finalized the selected take.",
            "Error: Failed to finalize take. Ensure a take is selected first "
            "(see select_take).",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def delete_take(
    take_index: int,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Delete a take from a timeline item by index.

    Parameters:
    - take_index: 1-based take index (see get_take_count for the valid range).
    - track_type: one of "video", "audio", "subtitle". Defaults to "video".
    - track_index: 1-based track index.
    - item_index: 0-based index of the item within that track's item list.
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        if not hasattr(item, "DeleteTakeByIndex"):
            return "Error: Takes are not available on this timeline item."
        result = item.DeleteTakeByIndex(take_index)
        return _ok(
            result,
            f"Deleted take {take_index}.",
            f"Error: Failed to delete take {take_index}. Check the take "
            "index is valid (1-based; see get_take_count).",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Linkage & audio routing (read-only) ──────────────────────────────────
# Introspection into how a timeline item relates to its siblings: which other
# items are linked to it (e.g. the audio companions of a video clip), which
# track type/index it lives on, and how its source audio channels are routed.


@mcp.tool()
def get_linked_items(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Get the timeline items linked to a timeline item (e.g. its audio companions), as a JSON list.

    Returns a JSON array of summarized linked items (name, position, offsets,
    clip color, enabled state). Returns an empty JSON list ("[]") when the
    item has no linked items.

    Parameters:
    - track_type: one of "video", "audio", "subtitle". Defaults to "video".
    - track_index: 1-based track index.
    - item_index: 0-based index of the item within that track's item list.
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        linked = item.GetLinkedItems()
        if not linked:
            return json.dumps([])
        return json.dumps(
            [timeline_item_to_dict(li) for li in linked], indent=2, default=str
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def get_track_type_and_index(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Get the track type and 1-based track index a timeline item lives on, as JSON.

    Reports what Resolve itself considers the item's placement (returned by
    ``TimelineItem.GetTrackTypeAndIndex`` as ``[trackType, trackIndex]``),
    which may differ from the locator you passed if the item was resolved
    across linked media.

    Parameters:
    - track_type: one of "video", "audio", "subtitle". Defaults to "video".
    - track_index: 1-based track index.
    - item_index: 0-based index of the item within that track's item list.
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        info = item.GetTrackTypeAndIndex()
        if not info:
            return "Error: Track type and index are not available on this timeline item."
        return json.dumps(
            {"track_type": info[0], "track_index": info[1]}, default=str
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def get_source_audio_channel_mapping(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Get the source audio channel mapping of a timeline item, as JSON.

    Returns the routing Resolve exposes via
    ``TimelineItem.GetSourceAudioChannelMapping`` (which channels of the
    source media feed which output channels). The API returns a JSON string;
    it is parsed and re-serialized as pretty JSON. If the value is not valid
    JSON, the raw string is returned unchanged.

    Parameters:
    - track_type: one of "video", "audio", "subtitle". Defaults to "video".
    - track_index: 1-based track index.
    - item_index: 0-based index of the item within that track's item list.
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        mapping = item.GetSourceAudioChannelMapping()
        try:
            return json.dumps(json.loads(mapping), indent=2, default=str)
        except (json.JSONDecodeError, TypeError):
            return mapping if mapping else "No source audio channel mapping is available."
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"
