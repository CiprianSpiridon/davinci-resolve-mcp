"""
davinci_resolve_mcp.tools.off_offline_ref — display-referred reference-frame
extraction and shot-intent tagging (OFFLINE/advanced tool set).

Registers exactly one action-dispatch tool, ``offline_ref``, with two actions:

  * ``"extract"`` — pull a single display-referred still frame out of a
    source media file (at a timecode / seconds / frame offset) via the
    optional ``ffmpeg`` executable on ``PATH``. ffmpeg is intentionally
    optional here: when it is not installed, "extract" degrades to a clear
    JSON object whose ``error`` field says "requires ffmpeg" instead of
    raising or crashing the server.
  * ``"tag"`` — persist a ratified shot-intent tag (e.g. "motivated_warm",
    "low_key", "night") for a clip as an append-only provenance event in
    the local SQLite store (:mod:`davinci_resolve_mcp.store.db`), so
    downstream grading tools (white balance / skin match / QC) can look the
    tag up and exclude intentionally-graded shots from an automatic
    neutralize/level pass.

Intent-tagging model: deterministic per-shot SIGNALS (scope stats, WB/TOD metadata) are
turned into a human-readable PROPOSAL by the client (the LLM), a human
RATIFIES it, and only then does the ratified decision become a persisted
TAG the deterministic tools consume. This module owns only that last step
(persisting the ratified tag) — it never decides intent on its own.

This module never connects to DaVinci Resolve: "extract" only shells out to
the optional ``ffmpeg`` executable against a file already on disk, and
"tag" only touches a local SQLite file via ``store/db.py``. Every code path
returns a JSON string; nothing here raises — unexpected failures are caught
in the tool entry point and reported as ``"Error: {e}"``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from typing import Any

from ..app import mcp
from ..store.db import StoreError, open_db

_VALID_ACTIONS = ("extract", "tag")
_VALID_CONFIDENCE = ("low", "med", "high")

# Shot-intent tags that mark INTENTIONAL colour/exposure choices worth
# excluding from an automatic neutralize/white-balance/level pass. Any
# string may be passed as `tag`; tags outside this vocabulary are still
# stored, just flagged `"known_tag": false` in the returned record.
KNOWN_INTENT_TAGS = (
    "motivated_warm",
    "motivated_cool",
    "monochromatic",
    "low_key",
    "high_key",
    "neutral",
    "high_contrast",
    "low_contrast",
    "gelled",
    "neon",
    "day",
    "night",
)


def _now_iso() -> str:
    """UTC timestamp (ISO 8601, seconds) used to stamp tag records."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _err(action: str, message: str, **extra: Any) -> str:
    payload: dict[str, Any] = {"action": action, "error": message}
    payload.update(extra)
    return json.dumps(payload, indent=2)


def _extract(
    source_path: str,
    output_path: str,
    frame: int,
    timecode: str,
    seconds: float,
) -> str:
    """Extract a single still frame from ``source_path`` via ffmpeg.

    Seek priority: ``timecode`` (ffmpeg ``-ss`` syntax, e.g. "00:01:23.500")
    takes precedence over ``seconds`` (a float offset), which takes
    precedence over ``frame`` (an integer frame index, decoded via a
    ``select`` filter since ffmpeg has no direct "seek to frame N" flag).
    With none of those given, the first frame is grabbed.
    """
    if not _has_ffmpeg():
        return _err(
            "extract",
            "requires ffmpeg: no 'ffmpeg' executable found on PATH. "
            "Install ffmpeg (e.g. `brew install ffmpeg`, or the 'ffmpeg' "
            "package on your distro / package manager) to enable "
            "reference-frame extraction.",
            requires="ffmpeg",
        )

    if not source_path:
        return _err("extract", "source_path is required")
    if not output_path:
        return _err("extract", "output_path is required")
    if not os.path.isfile(source_path):
        return _err("extract", f"source_path not found: {source_path}")

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if timecode:
        cmd += ["-ss", timecode, "-i", source_path, "-frames:v", "1"]
        seek_desc = f"timecode {timecode}"
    elif seconds and seconds > 0:
        cmd += ["-ss", f"{seconds:.6f}", "-i", source_path, "-frames:v", "1"]
        seek_desc = f"{seconds:.3f}s"
    elif frame and frame > 0:
        cmd += [
            "-i",
            source_path,
            "-vf",
            f"select=eq(n\\,{int(frame)})",
            "-vsync",
            "vfr",
            "-frames:v",
            "1",
        ]
        seek_desc = f"frame {int(frame)}"
    else:
        cmd += ["-i", source_path, "-frames:v", "1"]
        seek_desc = "first frame"
    cmd.append(output_path)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return _err("extract", "ffmpeg timed out after 120s", command=cmd)
    except OSError as e:
        return _err("extract", f"could not run ffmpeg: {e}")

    if result.returncode != 0 or not os.path.isfile(output_path):
        stderr_tail = (result.stderr or "").strip()[-2000:]
        return _err(
            "extract",
            f"ffmpeg failed (exit {result.returncode}): {stderr_tail}",
            command=cmd,
        )

    return json.dumps(
        {
            "action": "extract",
            "source_path": source_path,
            "output_path": output_path,
            "seek": seek_desc,
        },
        indent=2,
    )


def _tag(
    db_path: str,
    clip_id: str,
    tag: str,
    confidence: str,
    evidence: str,
    project_id: str,
    actor: str,
) -> str:
    """Ratify + persist a shot-intent tag as a provenance event."""
    if not clip_id:
        return _err("tag", "clip_id is required")
    if not tag or not tag.strip():
        return _err("tag", "tag is required")
    if not db_path:
        return _err("tag", "db_path is required (path to the local SQLite store)")

    confidence_norm = (confidence or "med").strip().lower()
    if confidence_norm not in _VALID_CONFIDENCE:
        return _err(
            "tag",
            f"confidence must be one of {list(_VALID_CONFIDENCE)}, got {confidence!r}",
        )

    tag_norm = tag.strip()
    record = {
        "clip_id": clip_id,
        "tag": tag_norm,
        "confidence": confidence_norm,
        "evidence": evidence or "",
        "known_tag": tag_norm in KNOWN_INTENT_TAGS,
        "ratified_at": _now_iso(),
    }

    try:
        store = open_db(db_path)
    except StoreError as e:
        return _err("tag", f"could not open store at {db_path!r}: {e}")

    event_id = f"offline_ref:tag:{uuid.uuid4().hex}"
    try:
        event = store.record_provenance(
            event_id=event_id,
            project_id=project_id or None,
            stage="shot_intent_tag",
            tool="offline_ref",
            actor=actor or "human",
            result=json.dumps(record, sort_keys=True),
            target=clip_id,
        )
    except StoreError as e:
        return _err("tag", f"could not persist tag: {e}")
    finally:
        store.close()

    return json.dumps(
        {
            "action": "tag",
            "verified": False,
            "record": record,
            "provenance": event,
        },
        indent=2,
    )


@mcp.tool()
def offline_ref(
    action: str,
    source_path: str = "",
    output_path: str = "",
    frame: int = 0,
    timecode: str = "",
    seconds: float = 0.0,
    db_path: str = "",
    clip_id: str = "",
    tag: str = "",
    confidence: str = "med",
    evidence: str = "",
    project_id: str = "",
    actor: str = "human",
) -> str:
    """Display-referred reference-frame extraction and shot-intent tagging. Never touches Resolve.

    Parameters:
    - action: one of
        - "extract": pull a single still frame from `source_path` (a local
          media file already on disk) and write it to `output_path` via the
          optional `ffmpeg` executable. Seek priority is `timecode`
          (ffmpeg `-ss` syntax, e.g. "00:01:23.500") > `seconds` (a float
          offset) > `frame` (an integer frame index, decoded via an ffmpeg
          `select` filter) > first frame. When `ffmpeg` is not on PATH this
          returns a JSON object whose "error" field clearly says "requires
          ffmpeg" instead of raising.
        - "tag": ratify and persist a shot-intent tag (e.g.
          "motivated_warm", "motivated_cool", "monochromatic", "low_key",
          "high_key", "neutral", "high_contrast", "low_contrast", "gelled",
          "neon", "day", "night" — see KNOWN_INTENT_TAGS; any other string
          is also accepted but reported with `"known_tag": false`) for
          `clip_id`, persisted as an append-only provenance event in the
          local SQLite store at `db_path`. `confidence` is one of
          "low"/"med"/"high" (default "med"); `evidence` is a free-text
          justification (e.g. scope stats, WB metadata). `project_id` is an
          optional project scope for the event; `actor` defaults to
          "human" — the ratifying party — per the layered-intent model
          (deterministic signals -> LLM-reviewed proposal -> human
          ratification -> persisted tag). Returns the persisted record
          together with `"verified": false`: this tool records a
          human/LLM-ratified decision, it never decides intent on its own,
          and downstream grading tools should still confirm the tag against
          a live Resolve session before trusting it unconditionally.
    - source_path / output_path / frame / timecode / seconds: "extract" args.
    - db_path / clip_id / tag / confidence / evidence / project_id / actor:
      "tag" args.

    Any other action returns the list of valid actions instead of erroring.
    """
    action_lower = action.strip().lower() if isinstance(action, str) else ""

    try:
        if action_lower == "extract":
            return _extract(source_path, output_path, frame, timecode, seconds)

        if action_lower == "tag":
            return _tag(db_path, clip_id, tag, confidence, evidence, project_id, actor)

        return json.dumps(
            {
                "error": f"Unknown action '{action}'.",
                "valid_actions": list(_VALID_ACTIONS),
            },
            indent=2,
        )
    except Exception as e:  # noqa: BLE001 - a tool entry point must never raise
        return f"Error: {e}"
