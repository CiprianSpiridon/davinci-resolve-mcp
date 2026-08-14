"""
davinci_resolve_mcp.tools.off_audio — offline audio ops (loudness / level
reads) via the optional ``ffmpeg`` executable (OFFLINE/advanced tool set).

Registers exactly one action-dispatch tool, ``offline_audio``, with two
actions:

  * ``"loudness"`` — measure integrated loudness (LUFS), true peak (dBTP)
    and loudness range (LRA) for a local audio/video file via ffmpeg's
    ``ebur128`` filter, optionally checked against a target spec
    (``target_integrated_lufs``/``integrated_tolerance``,
    ``target_true_peak_max``, ``target_lra_max``).
  * ``"level"`` — measure peak and mean signal level (dBFS) via ffmpeg's
    ``volumedetect`` filter — a lighter-weight read than a full loudness
    pass, useful for a quick "is there audio / is it clipping" check.

Both actions shell out to the OPTIONAL ``ffmpeg`` executable on ``PATH``;
when it is not installed they degrade to a clear JSON object whose
``"error"`` field says "requires ffmpeg" instead of raising or crashing the
server. This module never connects to DaVinci Resolve — it only shells out
to ffmpeg against a file already on disk. Every code path returns a JSON
string; nothing here raises — unexpected failures are caught in the tool
entry point and reported as ``"Error: {e}"``. This is a read-only
measurement domain (no ``.drx``/grade bytes are written), so no action here
carries a ``"verified"`` field.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Any

from ..app import mcp

_VALID_ACTIONS = ("loudness", "level")


def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _err(action: str, message: str, **extra: Any) -> str:
    payload: dict[str, Any] = {"action": action, "error": message}
    payload.update(extra)
    return json.dumps(payload, indent=2)


def _requires_ffmpeg(action: str) -> str:
    return _err(
        action,
        "requires ffmpeg: no 'ffmpeg' executable found on PATH. Install "
        "ffmpeg (e.g. `brew install ffmpeg`, or the 'ffmpeg' package on "
        "your distro / package manager) to enable audio loudness/level "
        "reads.",
        requires="ffmpeg",
    )


def _run_ffmpeg(args: list[str], timeout: int = 120) -> tuple[int | None, str]:
    """Run ffmpeg with the given filter args, returning ``(returncode, stderr)``.

    ffmpeg's measurement filters (``ebur128``, ``volumedetect``) print their
    results to stderr; a ``-f null -`` sink lets ffmpeg fully decode the
    file without writing any real output. A ``returncode`` of ``None``
    means ffmpeg itself could not be launched/timed out — ``stderr`` then
    carries a human-readable reason rather than ffmpeg's own stderr.
    """
    cmd = ["ffmpeg", "-nostats", "-hide_banner", "-y"] + args
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"ffmpeg timed out after {timeout}s"
    except OSError as e:
        return None, f"could not run ffmpeg: {e}"
    return result.returncode, result.stderr or ""


def _parse_ebur128(stderr: str) -> dict[str, float | None]:
    """Parse ffmpeg's ``ebur128`` filter Summary block from stderr.

    The block looks like::

        Summary:

          Integrated loudness:
            I:         -23.0 LUFS
            Threshold: -33.2 LUFS

          Loudness range:
            LRA:         5.4 LU

          True peak:
            Peak:       -2.1 dBFS

    Only the LAST ``Summary:`` block in stderr is scanned (ebur128 also
    streams per-second readings before it).
    """
    summary_idx = stderr.rfind("Summary:")
    block = stderr[summary_idx:] if summary_idx != -1 else stderr

    def grab(label: str) -> float | None:
        m = re.search(rf"{label}:\s*(-?\d+(?:\.\d+)?)", block)
        return float(m.group(1)) if m else None

    return {
        "integrated_lufs": grab("I"),
        "threshold_lufs": grab("Threshold"),
        "lra": grab("LRA"),
        "true_peak_dbtp": grab("Peak"),
    }


def _parse_volumedetect(stderr: str) -> dict[str, float | None]:
    """Parse ffmpeg's ``volumedetect`` filter output from stderr.

    Lines look like::

        [Parsed_volumedetect_0 @ ...] mean_volume: -18.4 dB
        [Parsed_volumedetect_0 @ ...] max_volume: -3.2 dB
    """

    def grab(label: str) -> float | None:
        m = re.search(rf"{label}:\s*(-?\d+(?:\.\d+)?)\s*dB", stderr)
        return float(m.group(1)) if m else None

    return {
        "mean_volume_db": grab("mean_volume"),
        "max_volume_db": grab("max_volume"),
    }


def _loudness(
    source_path: str,
    target_integrated_lufs: float | None,
    integrated_tolerance: float,
    target_true_peak_max: float | None,
    target_lra_max: float | None,
) -> str:
    if not _has_ffmpeg():
        return _requires_ffmpeg("loudness")

    if not source_path:
        return _err("loudness", "source_path is required")
    if not os.path.isfile(source_path):
        return _err("loudness", f"source_path not found: {source_path}")

    returncode, stderr = _run_ffmpeg(["-i", source_path, "-af", "ebur128=peak=true", "-f", "null", "-"])
    if returncode is None:
        return _err("loudness", stderr, source_path=source_path)

    measured = _parse_ebur128(stderr)
    if measured["integrated_lufs"] is None:
        stderr_tail = stderr.strip()[-2000:]
        return _err(
            "loudness",
            f"could not measure loudness on '{source_path}' (no audio "
            f"stream? ffmpeg exit {returncode}, ebur128 produced no "
            "Summary block)",
            source_path=source_path,
            ffmpeg_stderr_tail=stderr_tail,
        )

    result: dict[str, Any] = {
        "action": "loudness",
        "source_path": source_path,
        "measured": measured,
    }

    checks: list[dict[str, Any]] = []
    if target_integrated_lufs is not None:
        diff = abs(measured["integrated_lufs"] - target_integrated_lufs)
        checks.append(
            {
                "field": "integrated_lufs",
                "target": target_integrated_lufs,
                "tolerance": integrated_tolerance,
                "actual": measured["integrated_lufs"],
                "pass": diff <= integrated_tolerance,
            }
        )
    if target_true_peak_max is not None:
        tp = measured["true_peak_dbtp"]
        checks.append(
            {
                "field": "true_peak_dbtp",
                "target": f"<= {target_true_peak_max}",
                "actual": tp,
                "pass": tp is not None and tp <= target_true_peak_max,
            }
        )
    if target_lra_max is not None:
        lra = measured["lra"]
        checks.append(
            {
                "field": "lra",
                "target": f"<= {target_lra_max}",
                "actual": lra,
                "pass": lra is not None and lra <= target_lra_max,
            }
        )

    if checks:
        result["checks"] = checks
        result["pass"] = all(c["pass"] for c in checks)

    return json.dumps(result, indent=2)


def _level(source_path: str) -> str:
    if not _has_ffmpeg():
        return _requires_ffmpeg("level")

    if not source_path:
        return _err("level", "source_path is required")
    if not os.path.isfile(source_path):
        return _err("level", f"source_path not found: {source_path}")

    returncode, stderr = _run_ffmpeg(["-i", source_path, "-af", "volumedetect", "-f", "null", "-"])
    if returncode is None:
        return _err("level", stderr, source_path=source_path)

    measured = _parse_volumedetect(stderr)
    if measured["max_volume_db"] is None and measured["mean_volume_db"] is None:
        stderr_tail = stderr.strip()[-2000:]
        return _err(
            "level",
            f"could not measure level on '{source_path}' (no audio "
            f"stream? ffmpeg exit {returncode}, volumedetect produced no "
            "reading)",
            source_path=source_path,
            ffmpeg_stderr_tail=stderr_tail,
        )

    return json.dumps(
        {"action": "level", "source_path": source_path, "measured": measured},
        indent=2,
    )


@mcp.tool()
def offline_audio(
    action: str,
    source_path: str = "",
    target_integrated_lufs: float | None = None,
    integrated_tolerance: float = 1.0,
    target_true_peak_max: float | None = None,
    target_lra_max: float | None = None,
) -> str:
    """Offline audio loudness/level reads via the optional ffmpeg executable. Never touches Resolve.

    Parameters:
    - action: one of
        - "loudness": measure integrated loudness (LUFS), true peak (dBTP)
          and loudness range (LRA) for `source_path` via ffmpeg's
          `ebur128` filter (a full decode pass; the ebur128 Summary block
          printed to ffmpeg's stderr is parsed). When `target_integrated_lufs`
          is given, an additional pass/fail check is added
          (`integrated_tolerance` LU either side, default 1.0);
          `target_true_peak_max` (dBTP) and `target_lra_max` (LU) each add
          their own pass/fail check — common delivery targets are -24 LUFS
          (ATSC A/85), -23 LUFS (EBU R128), or -16 LUFS (streaming/
          podcast). When `ffmpeg` is not on PATH this returns a JSON
          object whose "error" field clearly says "requires ffmpeg"
          instead of raising.
        - "level": a lighter-weight read of peak and mean signal level
          (dBFS) for `source_path` via ffmpeg's `volumedetect` filter —
          useful for a quick "is there audio / is it clipping" check.
          Same "requires ffmpeg" degradation when ffmpeg is absent.
    - source_path: local audio/video file already on disk (both actions).
    - target_integrated_lufs / integrated_tolerance / target_true_peak_max
      / target_lra_max: optional pass/fail target for "loudness" (see
      above); ignored by "level".

    Any other action returns the list of valid actions instead of erroring.
    This is a read-only measurement domain — no `.drx`/grade bytes are
    written — so no action returns a "verified" field.
    """
    action_lower = action.strip().lower() if isinstance(action, str) else ""

    try:
        if action_lower == "loudness":
            return _loudness(
                source_path,
                target_integrated_lufs,
                integrated_tolerance,
                target_true_peak_max,
                target_lra_max,
            )

        if action_lower == "level":
            return _level(source_path)

        return json.dumps(
            {
                "error": f"Unknown action '{action}'.",
                "valid_actions": list(_VALID_ACTIONS),
            },
            indent=2,
        )
    except Exception as e:  # noqa: BLE001 - a tool entry point must never raise
        return f"Error: {e}"
