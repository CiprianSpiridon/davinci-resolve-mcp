"""Local speech-to-text transcription tools for the DaVinci Resolve MCP server.

Wraps :mod:`davinci_resolve_mcp.transcription_engine` (mlx-whisper on Apple
Silicon, with an openai-whisper fallback on other platforms) with four
tools:

- ``transcribe_audio``: transcribe a file and return the transcript inline,
  also saving an SRT file next to the source.
- ``transcribe_and_add_subtitles``: transcribe a file and drop a timeline
  marker per segment on the current Resolve timeline.
- ``export_srt``: transcribe a file and save the result as a standalone SRT
  file at a caller-chosen path.
- ``list_whisper_models``: list available model names — does not import
  either whisper backend, so it always works even with no backend
  installed.

Per the ownership contract, this module owns every transcription-related
tool in this server. Neither this module nor
``davinci_resolve_mcp.transcription_engine`` imports ``mlx_whisper`` /
``whisper`` / ``DaVinciResolveScript`` at import time — whisper backends are
imported lazily inside ``transcription_engine.transcribe()``, and Resolve is
only reached lazily via ``_conn()`` inside tool bodies. If no whisper
backend is installed, every transcription tool catches the resulting
``ImportError`` and returns install instructions instead of raising.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from ..app import mcp
from ..helpers import _conn, _require_timeline
from ..transcription_engine import (
    DEFAULT_MODEL,
    WHISPER_MODELS,
    segments_to_srt,
)
from ..transcription_engine import transcribe as _transcribe

_INSTALL_HINT = (
    "No local Whisper backend is installed. Install one of:\n"
    "  - Apple Silicon (recommended, fastest): "
    "pip install 'davinci-resolve-mcp[transcription]' "
    "or: pip install 'mlx-whisper>=0.4.3'\n"
    "  - Any other platform: pip install openai-whisper\n"
    "ffmpeg must also be installed and available on PATH "
    "(brew install ffmpeg / apt install ffmpeg)."
)


@mcp.tool()
def transcribe_audio(
    file_path: str,
    model: str = DEFAULT_MODEL,
    language: Optional[str] = None,
    word_timestamps: bool = False,
    initial_prompt: Optional[str] = None,
) -> str:
    """
    Transcribe an audio/video file locally (mlx-whisper on Apple Silicon,
    openai-whisper elsewhere). Long files are automatically split into
    5-minute chunks with ffmpeg so this never times out.

    Returns all segments with timestamps inline (compact format) and also
    saves an SRT file next to the source file for import into Resolve.

    Parameters:
    - file_path: absolute path to an audio/video file (mp3, wav, m4a, mp4,
      mov, etc).
    - model: "tiny" (fastest), "base", "small", "medium", "large" (most
      accurate), or "turbo" (best speed/quality, default). A full
      HuggingFace repo path also works with the mlx backend.
    - language: language code (e.g. "en", "fr", "de", "ja"). None = auto-detect.
    - word_timestamps: include word-level timestamps in the output.
    - initial_prompt: optional text to guide the model's vocabulary/style.
    """
    try:
        result = _transcribe(
            audio_path=file_path,
            model=model,
            language=language,
            word_timestamps=word_timestamps,
            initial_prompt=initial_prompt,
        )

        segments = result.get("segments", [])

        # Write SRT file next to the source for Resolve import.
        base = os.path.splitext(file_path)[0]
        srt_path = base + ".srt"
        srt_content = segments_to_srt(segments)
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(srt_content)

        # Compact timestamped transcript: one line per segment.
        lines = []
        for seg in segments:
            t0 = seg["start"]
            t1 = seg["end"]
            m0, s0 = int(t0 // 60), int(t0 % 60)
            m1, s1 = int(t1 // 60), int(t1 % 60)
            lines.append(f"[{m0:02d}:{s0:02d}-{m1:02d}:{s1:02d}] {str(seg['text']).strip()}")

        transcript_block = "\n".join(lines)

        return (
            f"Language: {result.get('language', 'unknown')}\n"
            f"Segments: {len(segments)}\n"
            f"SRT saved: {srt_path}\n"
            f"\n{transcript_block}"
        )
    except ImportError:
        return _INSTALL_HINT
    except FileNotFoundError as e:
        return f"Error: {e}"
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def transcribe_and_add_subtitles(
    file_path: str,
    model: str = DEFAULT_MODEL,
    language: Optional[str] = None,
    initial_prompt: Optional[str] = None,
) -> str:
    """
    Transcribe an audio/video file locally and add a marker per segment to
    the current Resolve timeline (so the transcript is visible/navigable in
    the timeline). Long files are auto-chunked so this works on any length.

    Parameters:
    - file_path: absolute path to the audio/video file to transcribe.
    - model: whisper model size ("tiny", "base", "small", "medium",
      "large", "turbo").
    - language: language code (e.g. "en", "fr"). None = auto-detect.
    - initial_prompt: optional text to guide recognition vocabulary.
    """
    try:
        result = _transcribe(
            audio_path=file_path,
            model=model,
            language=language,
            initial_prompt=initial_prompt,
        )

        segments = result.get("segments", [])
        if not segments:
            return "Transcription produced no segments. Check that the file contains speech."

        conn = _conn()
        timeline = _require_timeline(conn)

        fps_str = timeline.GetSetting("timelineFrameRate")
        fps = float(fps_str) if fps_str else 24.0
        timeline_start = timeline.GetStartFrame() or 0

        added = 0
        for seg in segments:
            frame_pos = timeline_start + int(seg["start"] * fps)
            duration_frames = max(1, int((seg["end"] - seg["start"]) * fps))
            text = str(seg["text"]).strip()

            if timeline.AddMarker(frame_pos, "Cream", text, text, duration_frames, ""):
                added += 1

        srt_content = segments_to_srt(segments)

        return json.dumps({
            "language": result.get("language", "unknown"),
            "total_segments": len(segments),
            "markers_added": added,
            "srt_preview": srt_content[:2000],
            "note": (
                f"Added {added} timeline marker(s). "
                "Use export_srt() to save an SRT file for import as a subtitle track."
            ),
        }, indent=2, ensure_ascii=False)
    except ImportError:
        return _INSTALL_HINT
    except FileNotFoundError as e:
        return f"Error: {e}"
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def export_srt(
    file_path: str,
    output_path: str,
    model: str = DEFAULT_MODEL,
    language: Optional[str] = None,
    initial_prompt: Optional[str] = None,
) -> str:
    """
    Transcribe an audio/video file locally and save the result as an SRT
    subtitle file. Import the SRT into Resolve via File > Import > Subtitle.

    Parameters:
    - file_path: absolute path to the audio/video file to transcribe.
    - output_path: destination path for the .srt file.
    - model: whisper model size ("tiny", "base", "small", "medium",
      "large", "turbo").
    - language: language code, or None to auto-detect.
    - initial_prompt: optional text to guide vocabulary/style.
    """
    try:
        result = _transcribe(
            audio_path=file_path,
            model=model,
            language=language,
            initial_prompt=initial_prompt,
        )

        segments = result.get("segments", [])
        if not segments:
            return "Transcription produced no segments."

        srt = segments_to_srt(segments)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(srt)

        return json.dumps({
            "language": result.get("language", "unknown"),
            "segments": len(segments),
            "output_path": output_path,
            "note": "SRT saved. Import into Resolve: File > Import > Subtitle.",
        }, indent=2)
    except ImportError:
        return _INSTALL_HINT
    except FileNotFoundError as e:
        return f"Error: {e}"
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def list_whisper_models() -> str:
    """
    List available local Whisper model names, as JSON, along with the
    default model. Does not require (or import) any whisper backend to be
    installed — it only reports the static model catalog.
    """
    return json.dumps({
        "models": dict(WHISPER_MODELS),
        "default": DEFAULT_MODEL,
        "note": (
            "Model names work with either backend (mlx-whisper on Apple "
            "Silicon, openai-whisper elsewhere). First use of each model "
            "triggers a one-time download. No whisper backend is imported "
            "by this listing."
        ),
    }, indent=2)
