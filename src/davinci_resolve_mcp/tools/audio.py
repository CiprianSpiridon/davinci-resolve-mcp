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

All tools reach Resolve lazily via ``_conn()``/``_require_timeline()`` inside
their bodies — nothing here imports ``DaVinciResolveScript`` or touches a
live Resolve instance at import time.
"""

from __future__ import annotations

import json

from ..app import mcp
from ..helpers import _conn, _ok, _require_timeline

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
