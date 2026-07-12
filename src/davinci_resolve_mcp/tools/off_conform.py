"""
davinci_resolve_mcp.tools.off_conform — offline conform/relink QC + lineage
(OFFLINE/advanced tool set).

Registers exactly one action-dispatch tool, ``conform``, with two actions:

  * ``"qc"`` — frame-oracle relink QC. Parses a ``.drp``/``.drt`` project
    file (composing :mod:`davinci_resolve_mcp.formats.drp` and
    :mod:`davinci_resolve_mcp.formats.drt` — never reimplementing their
    SeqContainer parsing) and diffs every timeline clip's recorded media
    link against a caller-supplied *media manifest*: the on-disk truth
    for each source file (path / frame rate / duration-in-frames). A
    filename match alone is never treated as a conform — a clip must
    resolve to a manifest entry whose recorded path, frame rate, and
    available duration are all consistent with what the project uses.
    Reports every path / frame-rate / duration mismatch plus any manifest
    entries no clip in the project ever referenced; a clean conform (zero
    mismatches) reports ``"passed": true``.
  * ``"lineage"`` — persist one conform-QC snapshot into the local
    SQLite store (:mod:`davinci_resolve_mcp.store.db`) as a ``run`` + a
    ``stage`` + a ``provenance`` event, then return that run's full
    recorded lineage (its stage plus every provenance event on record for
    the project) so a caller can see how a project's conform state
    evolved over successive "lineage" calls. The snapshot persisted is
    either a QC report the caller already computed (``qc_result_json``)
    or one computed fresh from ``project_path`` + a media manifest (the
    same inputs as the "qc" action). Runs/stages/provenance are upserted
    on a deterministic id (derived from ``project_id`` + the snapshot's
    content hash), so re-recording an unchanged snapshot is idempotent.

This module never connects to DaVinci Resolve: no ``DaVinciResolveScript``
import, no live session, nothing at import time beyond registering the
tool. It only reads local ``.drp``/``.drt``/JSON files and a local SQLite
store. Every code path returns a JSON string; nothing here raises — the
top-level ``conform`` dispatcher catches every exception and returns an
``"Error: ..."`` string instead, per the offline tool-set convention. The
"lineage" action's persisted rows always carry a top-level ``"verified":
false`` field — the store records what the offline QC concluded, not a
live Resolve calibration.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Optional, Tuple

from ..app import mcp
from ..formats import drp as drp_fmt
from ..formats import drt as drt_fmt
from ..store.db import StoreError, open_db

_VALID_ACTIONS = ("qc", "lineage")

_DEFAULT_FRAME_RATE_TOLERANCE = 0.01
_DEFAULT_DURATION_TOLERANCE = 0


class _ConformError(Exception):
    """Raised for a bad conform-QC/lineage input (missing/unreadable
    project, missing/malformed media manifest, bad store args). Always
    caught by the top-level ``conform`` dispatcher and turned into an
    ``"Error: ..."`` string — it never escapes this module."""


# ---------------------------------------------------------------------------
# Project read (composes formats.drp + formats.drt — never reimplements
# either module's ZIP/XML/SeqContainer parsing)
# ---------------------------------------------------------------------------
def _read_project_timelines(project_path: str) -> Tuple[List[Dict[str, Any]], Optional[str], str]:
    """Return ``(timelines, project_name, kind)`` for a ``.drp`` or ``.drt``.

    Tries :func:`formats.drp.read_drp` first — a ``.drp`` is a superset of a
    ``.drt`` and recovers each Media-Pool clip's link from the authoritative
    binary blob where present. A file with no ``MpFolder.xml`` (a plain
    ``.drt``) raises :class:`formats.drp.DrpFormatError`, so this falls back
    to :func:`formats.drt.parse_drt`. Both return the same per-clip shape
    (``dbId``/``start``/``duration``/``in``/``out``/``mediaFilePath``/
    ``mediaRef``), sourced from the very same SeqContainer parser either way.
    """
    if not project_path or not isinstance(project_path, str) or not project_path.strip():
        raise _ConformError("conform: 'project_path' is required")
    if not os.path.isfile(project_path):
        raise _ConformError(f"no such project file: {project_path}")

    try:
        parsed = drp_fmt.read_drp(project_path)
        return list(parsed.get("timelines") or []), parsed.get("name"), "drp"
    except drp_fmt.DrpFormatError:
        pass  # not a .drp (no MpFolder.xml) — fall back to the .drt reader

    try:
        parsed = drt_fmt.parse_drt(project_path)
    except drt_fmt.DrtError as e:
        raise _ConformError(
            f"could not read {project_path!r} as a .drp or .drt project: {e}"
        ) from e

    project_name = None
    meta = parsed.get("metadata")
    if isinstance(meta, dict):
        project_name = meta.get("projectName") or meta.get("name")
    return list(parsed.get("timelines") or []), project_name, "drt"


def _flatten_clips(timelines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flatten every video/audio-track clip across every timeline into one
    list, each entry carrying its parent timeline's name and frame rate."""
    flat: List[Dict[str, Any]] = []
    for tl in timelines:
        if not isinstance(tl, dict):
            continue
        tl_name = tl.get("name") or "Timeline"
        tl_rate = tl.get("frameRate")
        for track in tl.get("tracks") or []:
            if not isinstance(track, dict):
                continue
            track_type = track.get("type") or "video"
            for clip in track.get("clips") or []:
                if not isinstance(clip, dict):
                    continue
                flat.append(
                    {
                        "timeline": tl_name,
                        "trackType": track_type,
                        "frameRate": tl_rate,
                        "dbId": clip.get("dbId"),
                        "mediaFilePath": clip.get("mediaFilePath"),
                        "mediaRef": clip.get("mediaRef"),
                        "in": clip.get("in"),
                        "out": clip.get("out"),
                        "duration": clip.get("duration"),
                    }
                )
    return flat


# ---------------------------------------------------------------------------
# Media manifest (the on-disk truth a project's relink is QC'd against)
# ---------------------------------------------------------------------------
def _normalize_manifest_entry(entry: Any, index: int) -> Dict[str, Any]:
    if not isinstance(entry, dict):
        raise _ConformError(f"media manifest entry #{index} must be an object")
    media_path = entry.get("path") or entry.get("filePath") or entry.get("mediaPath")
    if not media_path:
        raise _ConformError(f"media manifest entry #{index} is missing 'path'")

    frame_rate = entry.get("frameRate")
    if frame_rate is None:
        frame_rate = entry.get("fps")

    duration = entry.get("durationFrames")
    if duration is None:
        duration = entry.get("duration")
    if duration is None:
        duration = entry.get("frames")

    return {
        "path": str(media_path),
        "basename": os.path.basename(str(media_path)),
        "name": entry.get("name"),
        "frameRate": frame_rate,
        "durationFrames": duration,
    }


def _load_manifest(media_manifest_path: str, media_manifest_json: str) -> List[Dict[str, Any]]:
    """Load + normalize a media manifest from an inline JSON string or a JSON
    file. Raises :class:`_ConformError` (surfaced as ``"Error: ..."``) when
    neither is provided — a conform QC cannot run without a manifest."""
    path = (media_manifest_path or "").strip()
    inline = (media_manifest_json or "").strip()
    if not path and not inline:
        raise _ConformError(
            "conform qc: a media manifest is required — provide "
            "'media_manifest_path' (a JSON file on disk) or "
            "'media_manifest_json' (an inline JSON string)"
        )

    if inline:
        try:
            raw = json.loads(inline)
        except json.JSONDecodeError as e:
            raise _ConformError(f"media_manifest_json is not valid JSON: {e}") from e
    else:
        if not os.path.isfile(path):
            raise _ConformError(f"no such media manifest file: {path}")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            raise _ConformError(f"could not read media manifest {path!r}: {e}") from e

    if isinstance(raw, dict):
        entries = raw.get("media")
        if entries is None:
            entries = raw.get("entries")
        if entries is None:
            entries = raw.get("clips")
        if entries is None:
            raise _ConformError(
                "media manifest object must carry a 'media' (or 'entries'/"
                "'clips') list"
            )
    elif isinstance(raw, list):
        entries = raw
    else:
        raise _ConformError("media manifest must decode to a JSON array or object")

    if not isinstance(entries, list) or len(entries) == 0:
        raise _ConformError("media manifest must contain at least one entry")

    return [_normalize_manifest_entry(e, i) for i, e in enumerate(entries)]


# ---------------------------------------------------------------------------
# 'qc' action — relink QC: project clips vs. media manifest
# ---------------------------------------------------------------------------
def _compute_qc(
    timelines: List[Dict[str, Any]],
    manifest: List[Dict[str, Any]],
    frame_rate_tolerance: float,
    duration_tolerance: int,
) -> Dict[str, Any]:
    by_basename: Dict[str, Dict[str, Any]] = {}
    for entry in manifest:
        by_basename.setdefault(entry["basename"].lower(), entry)

    clips = _flatten_clips(timelines)
    mismatches: List[Dict[str, Any]] = []
    matched_basenames: set = set()

    for clip in clips:
        clip_ref = clip.get("dbId") or clip.get("mediaRef") or "(unknown clip)"
        media_path = clip.get("mediaFilePath") or ""

        if not media_path:
            mismatches.append(
                {
                    "kind": "path",
                    "timeline": clip["timeline"],
                    "clip": clip_ref,
                    "message": "clip has an empty MediaFilePath",
                }
            )
            continue

        entry = by_basename.get(os.path.basename(media_path).lower())
        if entry is None:
            mismatches.append(
                {
                    "kind": "unmatched_clip",
                    "timeline": clip["timeline"],
                    "clip": clip_ref,
                    "media_path": media_path,
                    "message": "no media-manifest entry for this clip's media",
                }
            )
            continue

        matched_basenames.add(entry["basename"].lower())

        if os.path.normpath(entry["path"]) != os.path.normpath(media_path):
            mismatches.append(
                {
                    "kind": "path",
                    "timeline": clip["timeline"],
                    "clip": clip_ref,
                    "expected": entry["path"],
                    "actual": media_path,
                }
            )

        entry_rate = entry.get("frameRate")
        clip_rate = clip.get("frameRate")
        if entry_rate is not None and clip_rate is not None:
            if abs(float(entry_rate) - float(clip_rate)) > frame_rate_tolerance:
                mismatches.append(
                    {
                        "kind": "frame_rate",
                        "timeline": clip["timeline"],
                        "clip": clip_ref,
                        "media_path": media_path,
                        "expected": entry_rate,
                        "actual": clip_rate,
                    }
                )

        entry_duration = entry.get("durationFrames")
        clip_out = clip.get("out")
        if entry_duration is not None and clip_out is not None:
            if int(clip_out) > int(entry_duration) - 1 + int(duration_tolerance):
                mismatches.append(
                    {
                        "kind": "duration",
                        "timeline": clip["timeline"],
                        "clip": clip_ref,
                        "media_path": media_path,
                        "clip_out_frame": clip_out,
                        "media_duration_frames": entry_duration,
                        "message": (
                            "clip references a source frame beyond the "
                            "manifest's recorded media duration"
                        ),
                    }
                )

    unmatched_manifest_entries = [
        {"path": e["path"], "basename": e["basename"]}
        for e in manifest
        if e["basename"].lower() not in matched_basenames
    ]

    return {
        "clips_checked": len(clips),
        "manifest_entries": len(manifest),
        "mismatches": mismatches,
        "mismatch_count": len(mismatches),
        "unmatched_manifest_entries": unmatched_manifest_entries,
        "passed": len(mismatches) == 0,
    }


def _do_qc(
    project_path: str,
    media_manifest_path: str,
    media_manifest_json: str,
    frame_rate_tolerance: float,
    duration_tolerance: int,
) -> Dict[str, Any]:
    timelines, project_name, project_kind = _read_project_timelines(project_path)
    manifest = _load_manifest(media_manifest_path, media_manifest_json)
    report = _compute_qc(timelines, manifest, frame_rate_tolerance, duration_tolerance)
    return {
        "action": "qc",
        "project_path": project_path,
        "project_name": project_name,
        "project_kind": project_kind,
        "timeline_count": len(timelines),
        **report,
    }


# ---------------------------------------------------------------------------
# 'lineage' action — persist + read back a conform-QC snapshot via store.db
# ---------------------------------------------------------------------------
def _content_hash(payload: Any) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _do_lineage(
    db_path: str,
    project_id: str,
    project_path: str,
    media_manifest_path: str,
    media_manifest_json: str,
    qc_result_json: str,
    frame_rate_tolerance: float,
    duration_tolerance: int,
    run_id: str,
    label: str,
    stage_name: str,
    status: str,
    profile: str,
    actor: str,
) -> Dict[str, Any]:
    if not db_path or not isinstance(db_path, str) or not db_path.strip():
        raise _ConformError("conform lineage: 'db_path' is required")
    if not project_id or not isinstance(project_id, str) or not project_id.strip():
        raise _ConformError("conform lineage: 'project_id' is required")

    if qc_result_json and qc_result_json.strip():
        try:
            qc_result = json.loads(qc_result_json)
        except json.JSONDecodeError as e:
            raise _ConformError(f"qc_result_json is not valid JSON: {e}") from e
        if not isinstance(qc_result, dict):
            raise _ConformError("qc_result_json must decode to a JSON object")
    else:
        # No pre-computed report supplied — compute one fresh (same inputs,
        # same rules as the "qc" action; a missing manifest still errors).
        qc_result = _do_qc(
            project_path,
            media_manifest_path,
            media_manifest_json,
            frame_rate_tolerance,
            duration_tolerance,
        )

    digest = _content_hash({"project_id": project_id, "label": label, "qc_result": qc_result})
    resolved_run_id = (run_id or "").strip() or f"conform:{project_id}:{digest}"
    resolved_stage = stage_name.strip() if isinstance(stage_name, str) and stage_name.strip() else "conform_qc"
    stage_status = "passed" if qc_result.get("passed") else "failed"

    try:
        store = open_db(db_path)
    except StoreError as e:
        raise _ConformError(f"could not open store at {db_path!r}: {e}") from e

    try:
        store.upsert_project(project_id=project_id, name=project_id)
        store.upsert_run(
            run_id=resolved_run_id,
            project_id=project_id,
            profile=(profile or "conform"),
            status=(status or "recorded"),
            spec_hash=digest,
        )
        store.upsert_stage(
            run_id=resolved_run_id,
            stage_index=0,
            stage=resolved_stage,
            status=stage_status,
            tool="conform",
            config_hash=digest,
            result=qc_result,
        )
        store.record_provenance(
            event_id=f"conform-lineage:{resolved_run_id}:0",
            run_id=resolved_run_id,
            project_id=project_id,
            stage=resolved_stage,
            tool="conform.lineage",
            config_hash=digest,
            actor=(actor or "system"),
            result=json.dumps(
                {
                    "intent": "conform_qc",
                    "actual": stage_status,
                    "detail": {
                        "mismatch_count": qc_result.get("mismatch_count"),
                        "clips_checked": qc_result.get("clips_checked"),
                    },
                },
                ensure_ascii=False,
            ),
            target=project_id,
        )
        run_full = store.get_run(resolved_run_id)
        provenance = store.list_provenance(project_id=project_id)
    except StoreError as e:
        raise _ConformError(f"store error: {e}") from e
    finally:
        store.close()

    return {
        "action": "lineage",
        "project_id": project_id,
        "run_id": resolved_run_id,
        "content_hash": digest,
        "qc_result": qc_result,
        "run": run_full,
        "provenance": provenance,
        "idempotent": True,
        "verified": False,
    }


# ---------------------------------------------------------------------------
# Tool entry point
# ---------------------------------------------------------------------------
@mcp.tool()
def conform(
    action: str,
    project_path: str = "",
    media_manifest_path: str = "",
    media_manifest_json: str = "",
    frame_rate_tolerance: float = _DEFAULT_FRAME_RATE_TOLERANCE,
    duration_tolerance: int = _DEFAULT_DURATION_TOLERANCE,
    db_path: str = "",
    project_id: str = "",
    run_id: str = "",
    label: str = "",
    stage_name: str = "conform_qc",
    status: str = "recorded",
    profile: str = "conform",
    actor: str = "system",
    qc_result_json: str = "",
) -> str:
    """Offline conform/relink QC + lineage engine (OFFLINE/advanced tool set). Never touches Resolve.

    Frame-math and the recorded media manifest are the truth here — a
    filename match by itself is never treated as a conform.

    Parameters:
    - action: one of
        - "qc": parse `project_path` (a local `.drp` or `.drt` file — tries
          the `.drp` Media-Pool-blob reader first, falls back to the plain
          `.drt` SeqContainer reader) and diff every timeline clip's
          recorded media link against a *media manifest* — the on-disk
          truth for each source file, given via `media_manifest_path` (a
          JSON file) or `media_manifest_json` (inline JSON). The manifest
          is either a JSON array of entries or an object carrying one under
          `"media"`/`"entries"`/`"clips"`; each entry is `{"path", "name"?,
          "frameRate"? (or "fps"), "durationFrames"? (or "duration"/
          "frames")}`. Clips are matched to manifest entries by media
          basename. For each matched clip this checks:
            - path: the clip's recorded `MediaFilePath` equals the
              manifest entry's `path` (normalized).
            - frame_rate: the clip's timeline edit rate is within
              `frame_rate_tolerance` of the manifest entry's `frameRate`
              (when both are known) — a mismatch flags a likely off-speed
              relink.
            - duration: the clip's source out-point does not exceed the
              manifest entry's `durationFrames` (+ `duration_tolerance`) —
              a mismatch flags a clip referencing a frame the real media
              doesn't have.
          A clip whose media basename has no manifest entry at all is
          reported as `"unmatched_clip"`; a clip with an empty
          `MediaFilePath` is reported as a `"path"` mismatch. Manifest
          entries no clip ever referenced are listed separately under
          `unmatched_manifest_entries` (informational — extra footage, not
          a conform failure). Returns `{project_name, project_kind,
          timeline_count, clips_checked, manifest_entries, mismatches,
          mismatch_count, unmatched_manifest_entries, passed}` — `passed`
          is true exactly when `mismatches` is empty (a clean conform).
          Missing/unreadable `project_path`, or no manifest at all
          (`media_manifest_path` and `media_manifest_json` both blank), is
          always reported as `"Error: ..."`.
        - "lineage": persist one conform-QC snapshot into the local SQLite
          store at `db_path` (:mod:`davinci_resolve_mcp.store.db`) under
          `project_id`, then return that project's full recorded lineage.
          The snapshot is either a QC report you already have
          (`qc_result_json`, a JSON object string — typically the output
          of a prior "qc" call) or, when that's omitted, one computed
          fresh from `project_path` + the same manifest inputs as "qc"
          (so a missing manifest errors here too). Writes one `run`
          (`run_id` — auto-derived from `project_id` + a content hash of
          the snapshot when not supplied, so re-recording an *unchanged*
          snapshot upserts the same row rather than duplicating it; pass
          `label` to force a distinct run for an otherwise-identical
          snapshot), one `stage` (`stage_name`, default "conform_qc",
          `status` "passed"/"failed" set from the QC result), and one
          `provenance` event (`tool="conform.lineage"`, `actor`). Returns
          `{project_id, run_id, content_hash, qc_result, run` (the run row
          with its stages attached) `, provenance` (every recorded
          provenance event for `project_id`, chronological) `, idempotent:
          true, verified: false}`.
      Any other action returns the list of valid actions instead of
      erroring.
    - project_path: local `.drp`/`.drt` project file ("qc"; "lineage" when
      `qc_result_json` is not supplied).
    - media_manifest_path / media_manifest_json: the media manifest, as a
      file path or inline JSON ("qc"; "lineage" when `qc_result_json` is
      not supplied). At least one is required whenever a fresh QC is
      computed.
    - frame_rate_tolerance: max allowed |manifest.frameRate -
      timeline.frameRate| before a clip is flagged (default 0.01).
    - duration_tolerance: extra frames of slack allowed past a manifest
      entry's `durationFrames` before a clip is flagged (default 0).
    - db_path: local SQLite store path ("lineage", required).
    - project_id: project key in the store ("lineage", required).
    - run_id: explicit run id to upsert ("lineage"; auto-derived when
      blank).
    - label: extra text folded into the auto-derived `run_id`'s content
      hash, so distinct labeled snapshots of the same project don't
      collide ("lineage", optional).
    - stage_name / status / profile / actor: lineage row metadata
      ("lineage", all optional — sensible defaults are used).
    - qc_result_json: a pre-computed "qc" report (JSON object string) to
      persist as-is instead of recomputing it ("lineage", optional).

    Malformed/unreadable input never raises — it always comes back as an
    "Error: ..." string, per the offline tool-set convention. The
    "lineage" action's persisted rows always carry a top-level
    "verified": false field.
    """
    action_lower = action.strip().lower() if isinstance(action, str) else ""

    try:
        if action_lower == "qc":
            return json.dumps(
                _do_qc(
                    project_path,
                    media_manifest_path,
                    media_manifest_json,
                    frame_rate_tolerance,
                    duration_tolerance,
                ),
                indent=2,
            )

        if action_lower == "lineage":
            return json.dumps(
                _do_lineage(
                    db_path,
                    project_id,
                    project_path,
                    media_manifest_path,
                    media_manifest_json,
                    qc_result_json,
                    frame_rate_tolerance,
                    duration_tolerance,
                    run_id,
                    label,
                    stage_name,
                    status,
                    profile,
                    actor,
                ),
                indent=2,
            )

        return json.dumps(
            {
                "error": f"Unknown action '{action}'.",
                "valid_actions": list(_VALID_ACTIONS),
            },
            indent=2,
        )
    except Exception as e:  # noqa: BLE001 - offline tools never raise
        return f"Error: {e}"
