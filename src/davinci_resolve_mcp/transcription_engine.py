"""
Local speech-to-text transcription engine for the DaVinci Resolve MCP server.

Prefers ``mlx-whisper`` (fast, Apple-Silicon-only) and falls back to
``openai-whisper`` (slower, cross-platform) when mlx-whisper isn't
installed or the platform doesn't support it. Neither package is imported
at module import time — both are imported lazily, inside function bodies,
so this module (and anything that merely imports it, such as
``tools/transcription.py``) always imports cleanly even with no whisper
backend installed at all.

Long audio/video files are split into ``CHUNK_SECONDS``-long pieces with
``ffmpeg`` before being handed to whisper, so a single transcription call
never runs long enough to trip an MCP client's request timeout. Chunk
results are stitched back together with corrected (offset) timestamps.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional

logger = logging.getLogger("davinci_resolve_mcp.transcription_engine")

# ── Models ───────────────────────────────────────────────────────────────
# Friendly model name -> HuggingFace repo path used by mlx-whisper. The
# openai-whisper backend uses the friendly name directly (see
# ``_resolve_model_name``).
WHISPER_MODELS: Dict[str, str] = {
    "tiny": "mlx-community/whisper-tiny",
    "base": "mlx-community/whisper-base",
    "small": "mlx-community/whisper-small",
    "medium": "mlx-community/whisper-medium",
    "large": "mlx-community/whisper-large-v3",
    "turbo": "mlx-community/whisper-large-v3-turbo",
}

DEFAULT_MODEL = "turbo"

# Each chunk is this many seconds — short enough that a single whisper call
# never risks tripping an MCP client's request timeout.
CHUNK_SECONDS = 300  # 5 minutes

# Cache of loaded openai-whisper model objects, keyed by model name, so
# repeated transcribe() calls in the same process don't reload weights.
_openai_model_cache: Dict[str, Any] = {}


# ── Backend selection ───────────────────────────────────────────────────

def _select_backend():
    """Pick a local whisper backend.

    Returns a ``(backend_name, module)`` tuple where ``backend_name`` is
    ``"mlx"`` or ``"openai"``. Tries ``mlx_whisper`` first (fast, Apple
    Silicon only), then falls back to ``openai-whisper``. Raises
    ``ImportError`` with install instructions for both options if neither
    is available.
    """
    try:
        import mlx_whisper  # type: ignore

        return "mlx", mlx_whisper
    except ImportError:
        pass

    try:
        import whisper  # type: ignore

        return "openai", whisper
    except ImportError:
        pass

    raise ImportError(
        "No local Whisper backend is installed. Install one of:\n"
        "  - Apple Silicon (recommended, fastest): "
        "pip install 'davinci-resolve-mcp[transcription]' "
        "or: pip install 'mlx-whisper>=0.4.3'\n"
        "  - Any other platform: pip install openai-whisper\n"
        "ffmpeg must also be installed and available on PATH "
        "(brew install ffmpeg / apt install ffmpeg)."
    )


def _resolve_model_name(model: str, backend: str) -> str:
    """Resolve a friendly model name (or full path) for the given backend."""
    if backend == "mlx":
        if "/" in model:
            # Already a full HuggingFace repo path.
            return model
        repo = WHISPER_MODELS.get(model)
        if repo is None:
            raise ValueError(
                f"Unknown model '{model}'. Choose from: "
                f"{', '.join(WHISPER_MODELS.keys())} or pass a full "
                f"HuggingFace repo path."
            )
        return repo

    # openai-whisper backend: takes the short model name directly.
    if "/" in model:
        # Best-effort: use the final path segment as the model name.
        return model.rsplit("/", 1)[-1]
    if model not in WHISPER_MODELS:
        raise ValueError(
            f"Unknown model '{model}'. Choose from: "
            f"{', '.join(WHISPER_MODELS.keys())}."
        )
    return model


# ── ffmpeg helpers ──────────────────────────────────────────────────────

def _get_duration(path: str) -> float:
    """Get duration in seconds via ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        path,
    ]
    out = subprocess.check_output(cmd, text=True).strip()
    return float(out)


def _extract_chunk(src: str, start: float, duration: float, dst: str) -> None:
    """Extract a chunk of audio with ffmpeg, resampled for whisper (16kHz mono)."""
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-t", str(duration),
        "-i", src,
        "-vn",                # drop video
        "-acodec", "pcm_s16le",
        "-ar", "16000",       # whisper expects 16 kHz
        "-ac", "1",           # mono
        dst,
    ]
    subprocess.run(cmd, capture_output=True, check=True)


def _split_audio(path: str, chunk_sec: int, tmp_dir: str) -> List[Dict[str, Any]]:
    """Split into <=chunk_sec WAV files. Returns list of {path, offset}."""
    total = _get_duration(path)
    chunks: List[Dict[str, Any]] = []
    offset = 0.0
    idx = 0
    while offset < total:
        chunk_path = os.path.join(tmp_dir, f"chunk_{idx:04d}.wav")
        _extract_chunk(path, offset, chunk_sec, chunk_path)
        chunks.append({"path": chunk_path, "offset": offset})
        offset += chunk_sec
        idx += 1
    return chunks


# ── Per-backend single-file transcription ───────────────────────────────

def _get_openai_model(module: Any, model_name: str) -> Any:
    if model_name not in _openai_model_cache:
        logger.info("Loading openai-whisper model '%s' (first use downloads it)...", model_name)
        _openai_model_cache[model_name] = module.load_model(model_name)
    return _openai_model_cache[model_name]


def _transcribe_chunk(
    backend: str,
    module: Any,
    audio_path: str,
    model_name: str,
    language: Optional[str],
    word_timestamps: bool,
    initial_prompt: Optional[str],
) -> Dict[str, Any]:
    """Run a single whisper call (on one file/chunk) with the selected backend."""
    if backend == "mlx":
        decode_options: Dict[str, Any] = {}
        if language:
            decode_options["language"] = language
        return module.transcribe(
            audio_path,
            path_or_hf_repo=model_name,
            word_timestamps=word_timestamps,
            initial_prompt=initial_prompt,
            verbose=False,
            **decode_options,
        )

    # openai-whisper
    whisper_model = _get_openai_model(module, model_name)
    kwargs: Dict[str, Any] = {"word_timestamps": word_timestamps, "verbose": False}
    if language:
        kwargs["language"] = language
    if initial_prompt:
        kwargs["initial_prompt"] = initial_prompt
    return whisper_model.transcribe(audio_path, **kwargs)


# ── Core transcription ───────────────────────────────────────────────────

def transcribe(
    audio_path: str,
    model: str = DEFAULT_MODEL,
    language: Optional[str] = None,
    word_timestamps: bool = False,
    initial_prompt: Optional[str] = None,
    chunk_seconds: int = CHUNK_SECONDS,
) -> Dict[str, Any]:
    """
    Transcribe an audio/video file locally using mlx-whisper (preferred, Apple
    Silicon) or openai-whisper (fallback, any platform).

    Files longer than *chunk_seconds* are automatically split with ffmpeg so
    each whisper call completes quickly and never risks an MCP timeout.

    Parameters:
    - audio_path: absolute path to the audio/video file to transcribe.
    - model: "tiny", "base", "small", "medium", "large", "turbo" (default),
      or a full HuggingFace repo path (mlx backend only).
    - language: language code (e.g. "en", "fr") or None to auto-detect.
    - word_timestamps: include word-level timestamps in the result.
    - initial_prompt: optional text to guide the model's vocabulary/style.
    - chunk_seconds: max seconds per chunk before splitting (default 300).

    Returns a dict with keys "language", "text", and "segments" (each
    segment a dict with "start", "end", "text" in seconds/timeline-relative
    seconds).

    Raises ``ImportError`` if no whisper backend is installed, and
    ``FileNotFoundError`` if *audio_path* doesn't exist.
    """
    backend, module = _select_backend()

    if not os.path.isfile(audio_path):
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    model_name = _resolve_model_name(model, backend)
    duration = _get_duration(audio_path)

    if duration <= chunk_seconds:
        logger.info(
            "Transcribing '%s' (%.0fs) with %s backend, model '%s'",
            audio_path, duration, backend, model_name,
        )
        return _transcribe_chunk(
            backend, module, audio_path, model_name, language, word_timestamps, initial_prompt
        )

    # Long file -> chunk, transcribe each, stitch results back together.
    logger.info(
        "Splitting '%s' (%.0fs) into %d-second chunks",
        audio_path, duration, chunk_seconds,
    )
    tmp_dir = tempfile.mkdtemp(prefix="resolve_whisper_")
    try:
        chunks = _split_audio(audio_path, chunk_seconds, tmp_dir)
        logger.info("Created %d chunks", len(chunks))

        all_segments: List[Dict[str, Any]] = []
        all_text_parts: List[str] = []
        detected_language: Optional[str] = None
        prompt = initial_prompt

        for i, chunk in enumerate(chunks):
            logger.info(
                "Transcribing chunk %d/%d (offset %.0fs)...",
                i + 1, len(chunks), chunk["offset"],
            )

            result = _transcribe_chunk(
                backend, module, chunk["path"], model_name, language, word_timestamps, prompt
            )

            if detected_language is None:
                detected_language = result.get("language")

            offset = chunk["offset"]
            for seg in result.get("segments", []):
                all_segments.append({
                    "start": seg["start"] + offset,
                    "end": seg["end"] + offset,
                    "text": seg["text"],
                })

            text = result.get("text", "")
            if text:
                all_text_parts.append(text.strip())
                # Use the tail of the last chunk's text as prompt for the
                # next chunk to maintain context continuity across chunk
                # boundaries.
                prompt = text.strip()[-200:]

        return {
            "language": detected_language or "unknown",
            "text": " ".join(all_text_parts),
            "segments": all_segments,
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ── SRT helpers ───────────────────────────────────────────────────────────

def segments_to_srt(segments: List[Dict[str, Any]]) -> str:
    """Render a list of {"start", "end", "text"} segments as SRT subtitle text."""
    lines = []
    for i, seg in enumerate(segments, 1):
        start = _seconds_to_srt_time(seg["start"])
        end = _seconds_to_srt_time(seg["end"])
        text = str(seg["text"]).strip()
        lines.append(f"{i}\n{start} --> {end}\n{text}\n")
    return "\n".join(lines)


def _seconds_to_srt_time(seconds: float) -> str:
    """Format a seconds offset as SRT timestamp "HH:MM:SS,mmm"."""
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds % 1) * 1000))
    if ms == 1000:
        ms = 0
        s += 1
        if s == 60:
            s = 0
            m += 1
            if m == 60:
                m = 0
                h += 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
