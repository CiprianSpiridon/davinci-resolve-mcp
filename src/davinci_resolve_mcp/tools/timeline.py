"""Timeline core tools for the DaVinci Resolve MCP server.

Covers the "read / navigate / structure" half of timeline management:
inspecting the current timeline, listing and switching between timelines in
the current project, duplicating a timeline, getting/setting timeline
settings, track operations (count/name/add/delete/enable/lock), listing the
items on a given track, and reading/moving the playhead timecode. Object:
``Timeline`` (``Project.GetCurrentTimeline()``), plus ``Project`` for the
timeline list / switch operations.

Editing operations (insert/append/detect-scene-cuts), markers, and
export_timeline live in other modules (tools/timeline_edit.py,
tools/export_still.py) — this module intentionally does not define them.

All tools reach Resolve lazily via ``_conn()`` inside their bodies — nothing
here imports ``DaVinciResolveScript`` or touches a live Resolve instance at
import time.
"""

from __future__ import annotations

import json

from ..app import mcp
from ..helpers import (
    _VALID_TRACK_TYPES,
    _check_choice,
    _coerce_value,
    _conn,
    _get_timeline_item,
    _ok,
    _require_timeline,
)
from ..resolve_utils import timeline_item_to_dict, timeline_to_dict


def _timeline():
    """Shorthand: the current timeline, or raise a helpful error."""
    return _require_timeline(_conn())


def _validate_track_type(track_type: str) -> None:
    """Raise ``RuntimeError`` if *track_type* isn't a valid Resolve track type."""
    if track_type not in _VALID_TRACK_TYPES:
        raise RuntimeError(
            f"Invalid track_type '{track_type}'. Must be one of: "
            f"{', '.join(_VALID_TRACK_TYPES)}."
        )


# ── Timeline info & navigation ───────────────────────────────────────────


@mcp.tool()
def get_current_timeline_info() -> str:
    """Get detailed information about the current timeline, as JSON.

    Includes name, start/end frame, start timecode, per-track-type track
    counts, key timeline settings (frame rate, resolution), current
    timecode, and markers. Returns an explanatory string if no timeline is
    currently open.
    """
    try:
        conn = _conn()
        timeline = conn.get_current_timeline()
        if timeline is None:
            return "No active timeline. Create or open a timeline first."
        return json.dumps(timeline_to_dict(timeline), indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def get_timeline_list() -> str:
    """List every timeline in the current project, as JSON.

    Each entry has ``index`` (1-based, as used by ``Project.GetTimelineByIndex``)
    and ``name``; the entry matching the current timeline also has
    ``current: true``.
    """
    try:
        conn = _conn()
        project = conn.get_project()
        count = project.GetTimelineCount() or 0
        if count == 0:
            return "No timelines in the current project."

        current = conn.get_current_timeline()
        current_name = current.GetName() if current else None

        timelines = []
        for i in range(1, count + 1):
            tl = project.GetTimelineByIndex(i)
            if tl is None:
                continue
            name = tl.GetName()
            entry = {"index": i, "name": name}
            if current_name is not None and name == current_name:
                entry["current"] = True
            timelines.append(entry)

        return json.dumps(timelines, indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def set_current_timeline(timeline_name: str) -> str:
    """Make the named timeline the current (active) timeline.

    Parameters:
    - timeline_name: exact name of an existing timeline in the current
      project, as returned by get_timeline_list().
    """
    try:
        if not timeline_name or not timeline_name.strip():
            return "Error: timeline_name must be a non-empty string."
        conn = _conn()
        project = conn.get_project()
        count = project.GetTimelineCount() or 0

        target = None
        for i in range(1, count + 1):
            tl = project.GetTimelineByIndex(i)
            if tl is not None and tl.GetName() == timeline_name:
                target = tl
                break

        if target is None:
            return f"Error: No timeline named '{timeline_name}' found in the current project."

        result = project.SetCurrentTimeline(target)
        return _ok(
            result,
            f"Current timeline set to '{timeline_name}'.",
            f"Error: Failed to set current timeline to '{timeline_name}'.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def duplicate_timeline(new_name: str = "") -> str:
    """Duplicate the current timeline, returning info about the new one.

    Parameters:
    - new_name: name for the duplicate. Leave empty to let Resolve assign a
      default name (typically "<original> Copy").
    """
    try:
        timeline = _timeline()
        if not hasattr(timeline, "DuplicateTimeline"):
            return "Error: DuplicateTimeline is not available on this timeline object."
        new_timeline = (
            timeline.DuplicateTimeline(new_name) if new_name else timeline.DuplicateTimeline()
        )
        if new_timeline is None:
            return "Error: Failed to duplicate the current timeline."
        return json.dumps(timeline_to_dict(new_timeline), indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Timeline settings ─────────────────────────────────────────────────────


@mcp.tool()
def get_timeline_setting(setting_name: str = "") -> str:
    """Get one or all settings on the current timeline, as JSON.

    Parameters:
    - setting_name: setting key, e.g. "timelineFrameRate",
      "timelineResolutionWidth", "useCustomSettings". Leave empty to return
      every timeline setting as a dict (via ``Timeline.GetSetting('')``).
    """
    try:
        timeline = _timeline()
        if not hasattr(timeline, "GetSetting"):
            return "Error: GetSetting is not available on this timeline object."
        value = timeline.GetSetting(setting_name)
        if value is None or value == "":
            if not setting_name:
                return "Error: Could not retrieve the full settings dict from the current timeline."
            return f"Error: Setting '{setting_name}' not found or has no value."
        if not setting_name:
            return json.dumps(value, indent=2, default=str)
        return json.dumps({setting_name: value}, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def set_timeline_setting(setting_name: str, setting_value: str) -> str:
    """Set a single named setting on the current timeline.

    Parameters:
    - setting_name: setting key, e.g. "timelineFrameRate",
      "timelineResolutionWidth", "useCustomSettings". See
      get_timeline_setting() (called with an empty setting_name) for the
      full list of valid keys.
    - setting_value: new value as a string. Numeric-looking strings are
      coerced to int/float and "true"/"false" to bool before being passed
      to the Resolve API; anything else is passed through as a string.
    """
    try:
        if not setting_name or not setting_name.strip():
            return "Error: setting_name must be a non-empty string."
        timeline = _timeline()
        if not hasattr(timeline, "SetSetting"):
            return "Error: SetSetting is not available on this timeline object."
        coerced = _coerce_value(setting_value)
        result = timeline.SetSetting(setting_name, coerced)
        return _ok(
            result,
            f"Set timeline setting '{setting_name}' = {coerced!r}",
            f"Error: Failed to set timeline setting '{setting_name}' = {coerced!r}. "
            "The key may be invalid, read-only, or the value out of range.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Track operations ─────────────────────────────────────────────────────


@mcp.tool()
def get_track_count(track_type: str = "video") -> str:
    """Get the number of tracks of a given type on the current timeline.

    Parameters:
    - track_type: "video", "audio", or "subtitle" (default: "video").
    """
    try:
        _validate_track_type(track_type)
        timeline = _timeline()
        count = timeline.GetTrackCount(track_type)
        return json.dumps({"track_type": track_type, "count": count})
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def get_track_name(track_type: str, track_index: int) -> str:
    """Get the name of a specific track on the current timeline.

    Parameters:
    - track_type: "video", "audio", or "subtitle".
    - track_index: 1-based track index.
    """
    try:
        _validate_track_type(track_type)
        timeline = _timeline()
        name = timeline.GetTrackName(track_type, track_index)
        if name is None or name == "":
            return f"Error: Could not get name for {track_type} track {track_index}."
        return name
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def set_track_name(track_type: str, track_index: int, name: str) -> str:
    """Rename a specific track on the current timeline.

    Parameters:
    - track_type: "video", "audio", or "subtitle".
    - track_index: 1-based track index.
    - name: new track name. Must be non-empty.
    """
    try:
        if not name or not name.strip():
            return "Error: name must be a non-empty string."
        _validate_track_type(track_type)
        timeline = _timeline()
        result = timeline.SetTrackName(track_type, track_index, name)
        return _ok(
            result,
            f"{track_type.capitalize()} track {track_index} renamed to '{name}'.",
            f"Error: Failed to rename {track_type} track {track_index}.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def add_track(track_type: str = "video") -> str:
    """Add a new track of the given type to the current timeline.

    Parameters:
    - track_type: "video", "audio", or "subtitle" (default: "video"). The
      new track is appended after the existing tracks of that type.
    """
    try:
        _validate_track_type(track_type)
        timeline = _timeline()
        result = timeline.AddTrack(track_type)
        return _ok(
            result,
            f"Added a new {track_type} track.",
            f"Error: Failed to add a new {track_type} track.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def delete_track(track_type: str, track_index: int) -> str:
    """Delete a specific track from the current timeline.

    Parameters:
    - track_type: "video", "audio", or "subtitle".
    - track_index: 1-based track index of the track to delete.
    """
    try:
        _validate_track_type(track_type)
        timeline = _timeline()
        result = timeline.DeleteTrack(track_type, track_index)
        return _ok(
            result,
            f"Deleted {track_type} track {track_index}.",
            f"Error: Failed to delete {track_type} track {track_index}. "
            "It may be the last remaining track of that type.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def set_track_enable(track_type: str, track_index: int, enable: bool = True) -> str:
    """Enable or disable a specific track on the current timeline.

    Parameters:
    - track_type: "video", "audio", or "subtitle".
    - track_index: 1-based track index.
    - enable: True to enable the track, False to disable it (default: True).
    """
    try:
        _validate_track_type(track_type)
        timeline = _timeline()
        result = timeline.SetTrackEnable(track_type, track_index, enable)
        state = "enabled" if enable else "disabled"
        return _ok(
            result,
            f"{track_type.capitalize()} track {track_index} {state}.",
            f"Error: Failed to {'enable' if enable else 'disable'} {track_type} track {track_index}.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def get_track_enable(track_type: str, track_index: int) -> str:
    """Check whether a specific track on the current timeline is enabled.

    Parameters:
    - track_type: "video", "audio", or "subtitle".
    - track_index: 1-based track index.
    """
    try:
        _validate_track_type(track_type)
        timeline = _timeline()
        if not hasattr(timeline, "GetIsTrackEnabled"):
            return "Error: GetIsTrackEnabled is not available on this timeline object."
        enabled = timeline.GetIsTrackEnabled(track_type, track_index)
        return json.dumps({"track_type": track_type, "track_index": track_index, "enabled": bool(enabled)})
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def set_track_lock(track_type: str, track_index: int, lock: bool = True) -> str:
    """Lock or unlock a specific track on the current timeline.

    Parameters:
    - track_type: "video", "audio", or "subtitle".
    - track_index: 1-based track index.
    - lock: True to lock the track, False to unlock it (default: True).
    """
    try:
        _validate_track_type(track_type)
        timeline = _timeline()
        result = timeline.SetTrackLock(track_type, track_index, lock)
        state = "locked" if lock else "unlocked"
        return _ok(
            result,
            f"{track_type.capitalize()} track {track_index} {state}.",
            f"Error: Failed to {'lock' if lock else 'unlock'} {track_type} track {track_index}.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def get_track_lock(track_type: str, track_index: int) -> str:
    """Check whether a specific track on the current timeline is locked.

    Parameters:
    - track_type: "video", "audio", or "subtitle".
    - track_index: 1-based track index.
    """
    try:
        _validate_track_type(track_type)
        timeline = _timeline()
        if not hasattr(timeline, "GetIsTrackLocked"):
            return "Error: GetIsTrackLocked is not available on this timeline object."
        locked = timeline.GetIsTrackLocked(track_type, track_index)
        return json.dumps({"track_type": track_type, "track_index": track_index, "locked": bool(locked)})
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Timeline items ────────────────────────────────────────────────────────


@mcp.tool()
def get_timeline_items(track_type: str = "video", track_index: int = 1) -> str:
    """List all clips/items on a specific track of the current timeline, as JSON.

    Parameters:
    - track_type: "video", "audio", or "subtitle" (default: "video").
    - track_index: 1-based track index (default: 1).

    Each returned item dict includes an ``index`` field (0-based position
    within the track's item list, as expected by other timeline-item tools).
    """
    try:
        _validate_track_type(track_type)
        timeline = _timeline()

        items = timeline.GetItemListInTrack(track_type, track_index)
        if not items:
            return f"No items on {track_type} track {track_index}."

        result = []
        for i, item in enumerate(items):
            d = timeline_item_to_dict(item)
            d["index"] = i
            result.append(d)

        return json.dumps(result, indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Timecode / playhead ───────────────────────────────────────────────────


@mcp.tool()
def get_current_timecode() -> str:
    """Get the current playhead timecode on the current timeline."""
    try:
        timeline = _timeline()
        tc = timeline.GetCurrentTimecode()
        return tc or "Error: Could not read the current timecode."
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def set_current_timecode(timecode: str) -> str:
    """Move the playhead on the current timeline to a specific timecode.

    Parameters:
    - timecode: timecode string in "HH:MM:SS:FF" format.
    """
    try:
        if not timecode or not timecode.strip():
            return "Error: timecode must be a non-empty string."
        timeline = _timeline()
        result = timeline.SetCurrentTimecode(timecode)
        return _ok(
            result,
            f"Playhead moved to {timecode}.",
            f"Error: Failed to set timecode to {timecode}. Check the "
            "\"HH:MM:SS:FF\" format and that it falls within the timeline's range.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def get_start_timecode() -> str:
    """Get the start timecode of the current timeline (its frame-0 timecode)."""
    try:
        timeline = _timeline()
        tc = timeline.GetStartTimecode()
        return tc or "Error: Could not read the timeline's start timecode."
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def get_end_timecode() -> str:
    """Get the timecode of the last frame of the current timeline.

    Computed from the timeline's start timecode and end frame, since the
    Resolve scripting API does not expose an end-timecode getter directly.
    """
    try:
        timeline = _timeline()
        start_frame = timeline.GetStartFrame()
        end_frame = timeline.GetEndFrame()
        if start_frame is None or end_frame is None:
            return "Error: Could not read the timeline's frame range."

        # The Resolve scripting API has no direct end-timecode getter, and
        # deriving one via SetCurrentTimecode/GetCurrentTimecode would move
        # the user's playhead as a side effect — so report the raw frame
        # range alongside the start timecode for the caller to combine.
        start_tc = timeline.GetStartTimecode()
        return json.dumps(
            {
                "start_timecode": start_tc,
                "start_frame": start_frame,
                "end_frame": end_frame,
                "duration_frames": end_frame - start_frame + 1,
            }
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Range marks (mark in/out) & A/V clip linking ──────────────────────────

_MARK_TYPES = ("video", "audio", "all")


@mcp.tool()
def get_timeline_mark_in_out() -> str:
    """Get the current timeline's mark in/out range, as JSON.

    Returns the dict from ``Timeline.GetMarkInOut()`` — typically keyed by
    "video" and/or "audio", each mapping to a dict with "in" and "out" frame
    numbers. An empty dict / message means no in-out range is currently set.
    """
    try:
        timeline = _timeline()
        if not hasattr(timeline, "GetMarkInOut"):
            return "Error: GetMarkInOut is not available on this timeline object."
        marks = timeline.GetMarkInOut()
        if not marks:
            return "No mark in/out range is currently set on the timeline."
        return json.dumps(marks, indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def set_timeline_mark_in_out(mark_in: int, mark_out: int, mark_type: str = "all") -> str:
    """Set the mark in/out range on the current timeline.

    Parameters:
    - mark_in: in-point frame number.
    - mark_out: out-point frame number (must be >= mark_in).
    - mark_type: which range to set — "video", "audio", or "all"
      (default: "all").

    Wraps ``Timeline.SetMarkInOut(mark_in, mark_out, mark_type)``.
    """
    try:
        canonical, err = _check_choice(mark_type, _MARK_TYPES, "mark_type")
        if err:
            return f"Error: {err}"
        timeline = _timeline()
        if not hasattr(timeline, "SetMarkInOut"):
            return "Error: SetMarkInOut is not available on this timeline object."
        result = timeline.SetMarkInOut(mark_in, mark_out, canonical)
        return _ok(
            result,
            f"Set {canonical} mark in/out to {mark_in}-{mark_out}.",
            f"Error: Failed to set {canonical} mark in/out to {mark_in}-{mark_out}. "
            "Check that the frames fall within the timeline's range and that "
            "mark_out >= mark_in.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def clear_timeline_mark_in_out(mark_type: str = "all") -> str:
    """Clear the mark in/out range on the current timeline.

    Parameters:
    - mark_type: which range to clear — "video", "audio", or "all"
      (default: "all").

    Wraps ``Timeline.ClearMarkInOut(mark_type)``.
    """
    try:
        canonical, err = _check_choice(mark_type, _MARK_TYPES, "mark_type")
        if err:
            return f"Error: {err}"
        timeline = _timeline()
        if not hasattr(timeline, "ClearMarkInOut"):
            return "Error: ClearMarkInOut is not available on this timeline object."
        result = timeline.ClearMarkInOut(canonical)
        return _ok(
            result,
            f"Cleared {canonical} mark in/out on the timeline.",
            f"Error: Failed to clear {canonical} mark in/out (there may be no "
            "range set for that type).",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def set_clips_linked(clips: list[dict] | None = None, linked: bool = True) -> str:
    """Link or unlink two or more timeline items (e.g. an A/V clip pair).

    Linked clips move and trim together on the timeline. This wraps
    ``Timeline.SetClipsLinked([items], linked)``.

    Parameters:
    - clips: list of clip locator dicts, each with keys "track_type"
      ("video"/"audio"/"subtitle"), "track_index" (1-based), and
      "item_index" (0-based, as returned by get_timeline_items). At least
      two clips are required.
    - linked: True to link the clips together, False to unlink them
      (default: True).
    """
    try:
        if not clips or len(clips) < 2:
            return (
                "Error: set_clips_linked requires at least 2 clips "
                f"(received {len(clips) if clips else 0}). Provide a list of "
                "locator dicts with track_type/track_index/item_index."
            )

        items = []
        for i, loc in enumerate(clips):
            if not isinstance(loc, dict):
                return (
                    f"Error: clips[{i}] must be a dict with track_type, "
                    "track_index and item_index."
                )
            try:
                track_type = loc["track_type"]
                track_index = int(loc["track_index"])
                item_index = int(loc["item_index"])
            except (KeyError, TypeError, ValueError):
                return (
                    f"Error: clips[{i}] must have track_type (str), "
                    "track_index (int, 1-based) and item_index (int, 0-based)."
                )
            items.append(_get_timeline_item(track_type, track_index, item_index))

        timeline = _timeline()
        if not hasattr(timeline, "SetClipsLinked"):
            return "Error: SetClipsLinked is not available on this timeline object."
        result = timeline.SetClipsLinked(items, linked)
        action = "linked" if linked else "unlinked"
        return _ok(
            result,
            f"{action.capitalize()} {len(items)} timeline items.",
            f"Error: Failed to set {len(items)} timeline items as {action}.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"
