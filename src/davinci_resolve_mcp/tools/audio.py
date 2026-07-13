"""Audio / Fairlight tools for the DaVinci Resolve MCP server.

This module owns the small set of audio-track tools that don't belong to
the generic track-management tools in ``tools/timeline.py``:

- Voice Isolation (Resolve Studio only): ``get_voice_isolation_state`` /
  ``set_voice_isolation_state`` (``Timeline.GetVoiceIsolationState`` /
  ``Timeline.SetVoiceIsolationState``).
- ``get_audio_track_count`` (``Timeline.GetTrackCount("audio")``).
- ``set_track_mute`` (``Timeline.SetTrackEnable("audio", ...)`` — Resolve's
  scripting API has no separate "mute" call; a track is muted precisely
  when it is disabled, so this tool is a thin, audio-scoped convenience
  wrapper around ``SetTrackEnable``).

Fairlight mixing / preset tools:

- ``set_audio_volume`` (``TimelineItem.SetAudioVolume``) — per-clip fader gain.
- ``set_track_volume`` (``Timeline.SetTrackVolume("audio", ...)``) — per-track
  fader gain.
- ``get_fairlight_presets`` (``Resolve.GetFairlightPresets``) — list of saved
  Fairlight presets.
- ``apply_fairlight_preset`` (``Project.ApplyFairlightPresetToCurrentTimeline``)
  — hasattr-guarded to degrade gracefully on Resolve Free / versions before
  20.2.2 that lack the API.
- ``insert_audio_at_playhead``
  (``Project.InsertAudioToCurrentTrackAtPlayhead``) — insert an audio file at
  the playhead on the selected Fairlight track.
- ``generate_speech`` (``Project.GenerateSpeech``) — Studio Neural-Engine
  text-to-speech (Resolve 20+). hasattr-guarded to degrade gracefully on
  Resolve Free / versions before 20 that lack the API.

All tools reach Resolve lazily via ``_conn()``/``_require_timeline()`` inside
their bodies — nothing here imports ``DaVinciResolveScript`` or touches a
live Resolve instance at import time.
"""

from __future__ import annotations

import json

from ..app import mcp
from ..helpers import _conn, _get_timeline_item, _ok, _require_timeline

# ── Voice Isolation ─────────────────────────────────────────────────────


@mcp.tool()
def get_voice_isolation_state(track_index: int) -> str:
    """Get the Voice Isolation state for an audio track.

    Requires DaVinci Resolve Studio; on Free (or older Resolve versions
    without this API) an explanatory string is returned instead of an error.

    Parameters:
    - track_index: 1-based audio track index.
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)
        if not hasattr(timeline, "GetVoiceIsolationState"):
            return (
                "Voice Isolation is not available in this Resolve version "
                "(requires DaVinci Resolve Studio)."
            )
        state = timeline.GetVoiceIsolationState(track_index)
        if state is None:
            return f"Error: Could not get Voice Isolation state for audio track {track_index}."
        return json.dumps(
            {
                "track_index": track_index,
                "enabled": bool(state.get("isEnabled", False)) if isinstance(state, dict) else None,
                "amount": state.get("amount") if isinstance(state, dict) else None,
                "raw": state,
            }
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def set_voice_isolation_state(track_index: int, enabled: bool, amount: int = 100) -> str:
    """Enable/disable Voice Isolation on an audio track (isolate speech from noise).

    Requires DaVinci Resolve Studio; on Free (or older Resolve versions
    without this API) an explanatory string is returned instead of an error.

    Parameters:
    - track_index: 1-based audio track index.
    - enabled: True to enable Voice Isolation, False to disable it.
    - amount: Isolation strength, 0-100 (default: 100). Clamped to range.
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)
        if not hasattr(timeline, "SetVoiceIsolationState"):
            return (
                "Voice Isolation is not available in this Resolve version "
                "(requires DaVinci Resolve Studio)."
            )
        clamped_amount = max(0, min(100, int(amount)))
        result = timeline.SetVoiceIsolationState(
            track_index, {"isEnabled": bool(enabled), "amount": clamped_amount}
        )
        state = "enabled" if enabled else "disabled"
        return _ok(
            result,
            f"Voice Isolation {state} (amount: {clamped_amount}) on audio track {track_index}.",
            f"Error: Failed to set Voice Isolation state on audio track {track_index}.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Audio track operations ──────────────────────────────────────────────


@mcp.tool()
def get_audio_track_count() -> str:
    """Get the number of audio tracks on the current timeline."""
    try:
        conn = _conn()
        timeline = _require_timeline(conn)
        count = timeline.GetTrackCount("audio")
        return json.dumps({"track_type": "audio", "count": count})
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def set_track_mute(track_index: int, mute: bool = True) -> str:
    """Mute or unmute an audio track on the current timeline.

    DaVinci Resolve's scripting API has no dedicated "mute" call — an audio
    track is muted precisely when it is disabled, so this wraps
    ``Timeline.SetTrackEnable("audio", track_index, not mute)``.

    Parameters:
    - track_index: 1-based audio track index.
    - mute: True to mute the track, False to unmute it (default: True).
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)
        result = timeline.SetTrackEnable("audio", track_index, not mute)
        state = "muted" if mute else "unmuted"
        return _ok(
            result,
            f"Audio track {track_index} {state}.",
            f"Error: Failed to {'mute' if mute else 'unmute'} audio track {track_index}.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Fairlight mixing ────────────────────────────────────────────────────


@mcp.tool()
def set_audio_volume(track_index: int, volume: float, item_index: int = 0) -> str:
    """Set the fader volume (gain) of a single audio clip on the timeline.

    Parameters:
    - track_index: 1-based audio track index.
    - volume: Fader gain to apply (``TimelineItem.SetAudioVolume``). In dB;
      0 is unity, positive boosts, negative attenuates.
    - item_index: 0-based index of the clip within the track (default: 0).

    An audio track with no items, or an out-of-range ``item_index``, surfaces
    the actionable ``_get_timeline_item`` error as a string.
    """
    try:
        item = _get_timeline_item("audio", track_index, item_index)
        result = item.SetAudioVolume(float(volume))
        return _ok(
            result,
            f"Audio volume set to {float(volume)} on audio track {track_index}, "
            f"item {item_index}.",
            f"Error: Failed to set audio volume on audio track {track_index}, "
            f"item {item_index}.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def set_track_volume(track_index: int, volume: float) -> str:
    """Set the fader volume (gain) of a whole audio track on the timeline.

    Parameters:
    - track_index: 1-based audio track index.
    - volume: Fader gain to apply to the track
      (``Timeline.SetTrackVolume("audio", ...)``). In dB; 0 is unity.
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)
        result = timeline.SetTrackVolume("audio", track_index, float(volume))
        return _ok(
            result,
            f"Audio track {track_index} volume set to {float(volume)}.",
            f"Error: Failed to set volume on audio track {track_index}.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Fairlight presets ───────────────────────────────────────────────────


@mcp.tool()
def get_fairlight_presets() -> str:
    """List the available Fairlight audio presets.

    Returns a JSON list of preset names from ``Resolve.GetFairlightPresets()``.
    On Resolve versions that lack this API, an explanatory string is returned
    instead of an error.
    """
    try:
        conn = _conn()
        resolve = conn.get_resolve()
        if not hasattr(resolve, "GetFairlightPresets"):
            return (
                "Fairlight presets are not available in this Resolve version "
                "(Resolve.GetFairlightPresets is missing)."
            )
        presets = resolve.GetFairlightPresets()
        return json.dumps({"presets": presets if presets else []})
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def apply_fairlight_preset(preset_name: str) -> str:
    """Apply a Fairlight preset to the current timeline.

    Uses ``Project.ApplyFairlightPresetToCurrentTimeline``. That API only
    exists on newer builds (roughly Resolve 20.2.2+); on older builds — or on
    Resolve Free without it — a graceful explanatory string is returned rather
    than raising.

    Parameters:
    - preset_name: Name of the Fairlight preset (see ``get_fairlight_presets``).
    """
    try:
        conn = _conn()
        project = conn.get_project()
        if not hasattr(project, "ApplyFairlightPresetToCurrentTimeline"):
            return (
                "Applying Fairlight presets is not available in this Resolve "
                "version (Project.ApplyFairlightPresetToCurrentTimeline is "
                "missing; requires Resolve 20.2.2+)."
            )
        result = project.ApplyFairlightPresetToCurrentTimeline(preset_name)
        return _ok(
            result,
            f"Fairlight preset '{preset_name}' applied to the current timeline.",
            f"Error: Failed to apply Fairlight preset '{preset_name}'.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def insert_audio_at_playhead(
    media_path: str,
    start_offset_in_samples: int = 0,
    duration_in_samples: int = 0,
) -> str:
    """Insert an audio file at the playhead on the selected Fairlight track.

    Uses ``Project.InsertAudioToCurrentTrackAtPlayhead``. Resolve must be on
    the Fairlight page with a track selected for this to succeed.

    Parameters:
    - media_path: Absolute path to the audio file to insert.
    - start_offset_in_samples: Start offset within the source, in audio
      samples (default: 0).
    - duration_in_samples: Duration to insert, in audio samples (default: 0,
      meaning the full clip).
    """
    try:
        conn = _conn()
        project = conn.get_project()
        if not hasattr(project, "InsertAudioToCurrentTrackAtPlayhead"):
            return (
                "Inserting audio at the playhead is not available in this "
                "Resolve version (Project.InsertAudioToCurrentTrackAtPlayhead "
                "is missing)."
            )
        result = project.InsertAudioToCurrentTrackAtPlayhead(
            media_path, int(start_offset_in_samples), int(duration_in_samples)
        )
        return _ok(
            result,
            f"Audio '{media_path}' inserted at the playhead on the selected "
            f"Fairlight track.",
            "Error: Failed to insert audio. Ensure the Fairlight page is "
            "active with a track selected.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── AI Neural-Engine speech synthesis ───────────────────────────────────


@mcp.tool()
def generate_speech(
    text_input: str,
    voice_model: str = "Female 1",
    speed: int = 0,
    pitch: int = 0,
    variation: int = 0,
    filename: str = "",
    add_to_timeline: bool = True,
    audio_track: int = 1,
    timecode: str = "00:00:00:00",
) -> str:
    """Synthesize speech (text-to-speech) via the DaVinci Neural Engine.

    Assembles a ``speechGenerationSettings`` dictionary and calls
    ``Project.GenerateSpeech(settings, timecode)``. The generated audio is
    imported as a MediaPoolItem and, when ``add_to_timeline`` is True, placed
    on the given audio track at ``timecode`` on the current timeline.

    Requires DaVinci Resolve Studio 20+; on Free (or older Resolve versions
    without ``Project.GenerateSpeech``) an explanatory string is returned
    instead of raising.

    Parameters:
    - text_input: The text to speak (maximum 350 characters).
    - voice_model: Voice preset, e.g. "Female 1", "Male 1", or a custom voice
      name (default: "Female 1").
    - speed: Speaking-rate adjustment (default: 0).
    - pitch: Pitch adjustment (default: 0).
    - variation: Expressive-variation amount (default: 0).
    - filename: Output filename for the generated audio (default: "", meaning
      Resolve chooses one).
    - add_to_timeline: True to place the clip on the timeline at ``timecode``
      (default: True).
    - audio_track: 1-based audio track for placement when adding to the
      timeline (default: 1).
    - timecode: Timeline timecode at which to add the clip (default:
      "00:00:00:00").
    """
    try:
        if not isinstance(text_input, str) or not text_input.strip():
            return "Error: text_input must be a non-empty string of speech text."
        if len(text_input) > 350:
            return (
                f"Error: text_input is {len(text_input)} characters — the "
                "Neural Engine limit is 350. Shorten the text and retry."
            )

        conn = _conn()
        project = conn.get_project()
        if not hasattr(project, "GenerateSpeech"):
            return (
                "Speech generation is not available in this Resolve version "
                "(Project.GenerateSpeech is missing; requires DaVinci Resolve "
                "Studio 20+)."
            )

        settings = {
            "TextInput": text_input,
            "VoiceModel": voice_model,
            "Speed": int(speed),
            "Pitch": int(pitch),
            "Variation": int(variation),
            "Filename": filename,
            "AddToTimeline": bool(add_to_timeline),
            "AudioTrack": int(audio_track),
        }
        item = project.GenerateSpeech(settings, timecode)
        if item is None:
            return (
                "Error: Speech generation failed (Project.GenerateSpeech "
                "returned None). Check the voice model, timecode, and that "
                "the Neural Engine is available."
            )

        name = item.GetName() if hasattr(item, "GetName") else None
        return json.dumps(
            {
                "generated": True,
                "media_pool_item": name,
                "voice_model": voice_model,
                "added_to_timeline": bool(add_to_timeline),
                "audio_track": int(audio_track),
                "timecode": timecode,
            }
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"
