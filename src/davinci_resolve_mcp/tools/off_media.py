"""
davinci_resolve_mcp.tools.off_media — offline media-ingest / assistant-editor
front end (OFFLINE/advanced tool set).

Registers exactly one action-dispatch tool, ``media_ingest``, with three
actions:

  * ``"scan"``     — walk a folder on disk, compute a size/hash/(optional
    ffprobe-derived) spec record for every file found, and persist the
    result into the local SQLite store (:mod:`davinci_resolve_mcp.store.db`)
    as one ``clips`` row per file, keyed under a project. Re-running "scan"
    against an unchanged folder is idempotent: the same deterministic
    ``clip_id`` (derived from ``project_id`` + absolute path) is re-upserted
    rather than duplicated, and an unchanged file's ``hash``/``size`` come
    back identical.
  * ``"manifest"`` — read back the manifest previously persisted for a
    project (its ``clips`` rows) without touching the filesystem.
  * ``"diff"``     — compare a *fresh* scan of a folder against the manifest
    already stored for a project (without persisting anything) and report
    new / missing / changed / unchanged files by content hash — useful for
    detecting what changed on a card/drive since the last ingest.

This is deliberately narrow in scope: it is a *front-end ingest* tool (hashes/specs -> the
local DB-as-truth store), not the sync/consistency-report tool.

Media spec probing (fps/codec/resolution/colour/duration) uses the optional
``ffprobe`` executable on ``PATH`` when present; when it is absent, "scan"
and "diff" still succeed on size/hash alone and simply carry
``"probe": null`` per item plus a top-level ``"ffprobe_available": false``.

This module NEVER connects to DaVinci Resolve: no ``DaVinciResolveScript``
import, no live session — it only reads plain files from the local
filesystem (to hash/probe them) and reads/writes the local SQLite store via
``store/db.py``. Every code path returns a JSON string; nothing here raises
— the top-level ``media_ingest`` dispatcher catches every exception and
returns an ``"Error: ..."`` string instead (e.g. a non-existent folder
comes back as ``"Error: no such folder: ..."``).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from typing import Any, Dict, Optional, Set, Tuple

from ..app import mcp
from ..store.db import open_db

_VALID_ACTIONS = ("scan", "manifest", "diff")

# Chunk size used when streaming a file through a hashlib digest.
_CHUNK_SIZE = 1024 * 1024


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """UTC timestamp (ISO 8601, seconds) used to stamp scanned items."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _has_ffprobe() -> bool:
    return shutil.which("ffprobe") is not None


def _slugify(text: str) -> str:
    """Turn arbitrary text into a stable ``[a-z0-9_]+`` id fragment."""
    out = []
    prev_us = False
    for ch in text.strip().lower():
        if ch.isalnum():
            out.append(ch)
            prev_us = False
        elif not prev_us:
            out.append("_")
            prev_us = True
    slug = "".join(out).strip("_")
    return slug or "media_ingest"


def _resolve_project(project_id: str, project_name: str, folder_abs: str) -> Tuple[str, str]:
    """Derive a stable ``(project_id, project_name)`` pair for a scan/diff.

    Priority: an explicit ``project_id`` wins outright; otherwise an
    explicit ``project_name`` is slugified into an id; with neither given,
    both are derived from the scanned folder's basename so repeated scans
    of the same folder land on the same project.
    """
    pid = (project_id or "").strip()
    pname = (project_name or "").strip()
    if not pid and not pname:
        pname = os.path.basename(folder_abs.rstrip(os.sep)) or "media_ingest"
    if not pid:
        pid = _slugify(pname)
    if not pname:
        pname = pid
    return pid, pname


def _clip_id(project_id: str, path: str) -> str:
    """Deterministic clip id for ``(project_id, path)`` so re-scans upsert."""
    digest = hashlib.sha256(f"{project_id}\x1f{path}".encode("utf-8")).hexdigest()[:24]
    return f"media:{digest}"


def _parse_extensions(extensions: str) -> Set[str]:
    """Parse a comma-separated ``extensions`` string into a lowercase set.

    An empty/blank string means "no filtering" (every regular file is
    included), signalled by returning an empty set.
    """
    if not extensions or not extensions.strip():
        return set()
    parts = [p.strip().lstrip(".").lower() for p in extensions.split(",")]
    return {p for p in parts if p}


def _iter_files(folder_abs: str, recursive: bool, exts: Set[str], skip_hidden: bool) -> list[str]:
    """Return a sorted list of absolute file paths under ``folder_abs``.

    ``exts`` (lowercase, no leading dot) filters by extension when
    non-empty; ``skip_hidden`` drops dotfiles/dot-directories.
    """
    paths: list[str] = []
    if recursive:
        for root, dirnames, filenames in os.walk(folder_abs):
            if skip_hidden:
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fname in filenames:
                if skip_hidden and fname.startswith("."):
                    continue
                if exts and os.path.splitext(fname)[1].lstrip(".").lower() not in exts:
                    continue
                paths.append(os.path.join(root, fname))
    else:
        try:
            entries = os.listdir(folder_abs)
        except OSError as e:
            raise OSError(f"could not list folder {folder_abs!r}: {e}") from e
        for fname in entries:
            full = os.path.join(folder_abs, fname)
            if not os.path.isfile(full):
                continue
            if skip_hidden and fname.startswith("."):
                continue
            if exts and os.path.splitext(fname)[1].lstrip(".").lower() not in exts:
                continue
            paths.append(full)
    paths.sort()
    return paths


def _hash_file(path: str, algo: str, max_bytes: int) -> Tuple[str, str]:
    """Stream-hash ``path`` with ``algo``; returns ``(hex_digest, scope)``.

    ``max_bytes <= 0`` hashes the whole file (``scope == "full"``);
    ``max_bytes > 0`` only reads that many leading bytes (``scope ==
    "first_<n>_bytes"``) — a documented, opt-in speed/accuracy trade-off for
    very large media on slow storage.
    """
    try:
        hasher = hashlib.new(algo)
    except (ValueError, TypeError) as e:
        raise ValueError(f"unsupported hash_algo {algo!r}: {e}") from e

    total = 0
    with open(path, "rb") as fh:
        while max_bytes <= 0 or total < max_bytes:
            want = _CHUNK_SIZE if max_bytes <= 0 else min(_CHUNK_SIZE, max_bytes - total)
            chunk = fh.read(want)
            if not chunk:
                break
            hasher.update(chunk)
            total += len(chunk)

    scope = "full" if max_bytes <= 0 else f"first_{max_bytes}_bytes"
    return hasher.hexdigest(), scope


def _ratio(value: Any) -> float:
    """Parse an ffprobe ``"a/b"`` rational string into a float (0 on bad input)."""
    try:
        n_str, d_str = str(value or "0/1").split("/")
        n, d = float(n_str), float(d_str)
        return n / d if d else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def _probe_file(path: str, timeout: int = 30) -> Optional[Dict[str, Any]]:
    """Best-effort ffprobe read of ``path``; ``None`` on any failure/absence.

    Never raises: a missing ``ffprobe``, a timeout, a non-media file, or
    malformed ffprobe JSON all simply degrade "scan"/"diff" to size/hash
    only for that item, matching the offline tool-set's optional-dependency
    convention (see ``off_offline_ref``/``off_audio``).
    """
    if not _has_ffprobe():
        return None
    cmd = ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None

    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    video_info: Optional[Dict[str, Any]] = None
    if video:
        fps = _ratio(video.get("r_frame_rate"))
        video_info = {
            "codec": video.get("codec_name") or "unknown",
            "width": video.get("width") or 0,
            "height": video.get("height") or 0,
            "fps": round(fps, 4),
            "pix_fmt": video.get("pix_fmt") or "unknown",
            "color_primaries": video.get("color_primaries") or "unknown",
            "color_transfer": video.get("color_transfer") or "unknown",
            "color_space": video.get("color_space") or "unknown",
        }
    audio_info = [
        {
            "codec": a.get("codec_name") or "unknown",
            "channels": a.get("channels") or 0,
            "sample_rate": int(a.get("sample_rate") or 0),
        }
        for a in audio_streams
    ]

    duration = float(fmt.get("duration") or 0.0)
    if not duration and video and video.get("duration"):
        try:
            duration = float(video["duration"])
        except (TypeError, ValueError):
            duration = 0.0

    return {
        "video": video_info,
        "audio": audio_info,
        "container": (fmt.get("format_name") or "").split(",")[0] or "unknown",
        "duration": round(duration, 4),
        "bit_rate": int(fmt.get("bit_rate") or 0),
        "timecode": (fmt.get("tags") or {}).get("timecode"),
    }


def _stat_file(path: str) -> Tuple[Optional[os.stat_result], Optional[str]]:
    try:
        return os.stat(path), None
    except OSError as e:
        return None, str(e)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _do_scan(
    folder: str,
    db_path: str,
    project_id: str,
    project_name: str,
    extensions: str,
    recursive: bool,
    skip_hidden: bool,
    probe: bool,
    hash_algo: str,
    max_hash_bytes: int,
) -> Dict[str, Any]:
    if not folder or not folder.strip():
        raise ValueError("media_ingest: 'folder' is required for action 'scan'")
    folder_abs = os.path.abspath(folder)
    if not os.path.isdir(folder_abs):
        raise FileNotFoundError(f"no such folder: {folder}")
    if not db_path or not db_path.strip():
        raise ValueError("media_ingest: 'db_path' is required (path to the local SQLite store)")

    algo = (hash_algo or "sha256").strip().lower()
    try:
        hashlib.new(algo)
    except (ValueError, TypeError) as e:
        raise ValueError(f"unsupported hash_algo {hash_algo!r}: {e}") from e

    exts = _parse_extensions(extensions)
    pid, pname = _resolve_project(project_id, project_name, folder_abs)
    paths = _iter_files(folder_abs, recursive, exts, skip_hidden)

    store = open_db(db_path)
    try:
        store.upsert_project(pid, pname, meta={"source_folder": folder_abs, "tool": "media_ingest"})

        items: list[Dict[str, Any]] = []
        skipped: list[Dict[str, Any]] = []
        for path in paths:
            st, err = _stat_file(path)
            if st is None:
                skipped.append({"path": path, "reason": err or "stat failed"})
                continue
            try:
                digest, scope = _hash_file(path, algo, max_hash_bytes)
            except OSError as e:
                skipped.append({"path": path, "reason": f"could not read file: {e}"})
                continue

            probe_info = _probe_file(path) if probe else None
            ext = os.path.splitext(path)[1].lstrip(".").lower()
            meta = {
                "size": st.st_size,
                "mtime": st.st_mtime,
                "hash": digest,
                "hash_algo": algo,
                "hash_scope": scope,
                "ext": ext,
                "probe": probe_info,
                "scanned_at": _now_iso(),
            }

            frame_start: Optional[int] = None
            frame_end: Optional[int] = None
            if probe_info and probe_info.get("video"):
                fps = probe_info["video"].get("fps") or 0
                duration = probe_info.get("duration") or 0.0
                if fps and duration:
                    frame_start = 0
                    frame_end = int(round(duration * fps))

            clip_id = _clip_id(pid, path)
            store.upsert_clip(
                clip_id=clip_id,
                project_id=pid,
                name=os.path.basename(path),
                source_path=path,
                frame_start=frame_start,
                frame_end=frame_end,
                meta=meta,
            )
            items.append(
                {
                    "clip_id": clip_id,
                    "path": path,
                    "name": os.path.basename(path),
                    "size": st.st_size,
                    "hash": digest,
                    "hash_algo": algo,
                    "hash_scope": scope,
                    "probe": probe_info,
                }
            )
    finally:
        store.close()

    return {
        "action": "scan",
        "project_id": pid,
        "project_name": pname,
        "folder": folder_abs,
        "db_path": db_path,
        "count": len(items),
        "items": items,
        "skipped": skipped,
        "ffprobe_available": _has_ffprobe(),
        "verified": False,
    }


def _do_manifest(db_path: str, project_id: str) -> Dict[str, Any]:
    if not db_path or not db_path.strip():
        raise ValueError("media_ingest: 'db_path' is required for action 'manifest'")
    if not project_id or not project_id.strip():
        raise ValueError("media_ingest: 'project_id' is required for action 'manifest'")

    store = open_db(db_path)
    try:
        project = store.get_project(project_id)
        if project is None:
            raise ValueError(f"unknown project_id: {project_id}")
        clips = store.list_clips(project_id)
    finally:
        store.close()

    return {
        "action": "manifest",
        "project": project,
        "count": len(clips),
        "items": clips,
    }


def _do_diff(
    folder: str,
    db_path: str,
    project_id: str,
    project_name: str,
    extensions: str,
    recursive: bool,
    skip_hidden: bool,
    hash_algo: str,
    max_hash_bytes: int,
) -> Dict[str, Any]:
    if not folder or not folder.strip():
        raise ValueError("media_ingest: 'folder' is required for action 'diff'")
    folder_abs = os.path.abspath(folder)
    if not os.path.isdir(folder_abs):
        raise FileNotFoundError(f"no such folder: {folder}")
    if not db_path or not db_path.strip():
        raise ValueError("media_ingest: 'db_path' is required for action 'diff'")

    algo = (hash_algo or "sha256").strip().lower()
    try:
        hashlib.new(algo)
    except (ValueError, TypeError) as e:
        raise ValueError(f"unsupported hash_algo {hash_algo!r}: {e}") from e

    exts = _parse_extensions(extensions)
    pid, pname = _resolve_project(project_id, project_name, folder_abs)

    store = open_db(db_path)
    try:
        # An unknown/never-scanned project simply has no rows -- list_clips
        # returns [] rather than raising, so "diff" against a fresh project
        # cleanly reports every file on disk as "new".
        stored = {c["source_path"]: c for c in store.list_clips(pid) if c.get("source_path")}
    finally:
        store.close()

    paths = _iter_files(folder_abs, recursive, exts, skip_hidden)
    fresh: Dict[str, str] = {}
    skipped: list[Dict[str, Any]] = []
    for path in paths:
        try:
            digest, _scope = _hash_file(path, algo, max_hash_bytes)
        except OSError as e:
            skipped.append({"path": path, "reason": f"could not read file: {e}"})
            continue
        fresh[path] = digest

    stored_hashes = {
        path: ((clip.get("meta_json") or {}).get("hash")) for path, clip in stored.items()
    }

    new_files = sorted(p for p in fresh if p not in stored)
    missing_files = sorted(p for p in stored if p not in fresh)
    changed_files = sorted(
        p
        for p in fresh
        if p in stored and stored_hashes.get(p) is not None and stored_hashes.get(p) != fresh[p]
    )
    unchanged_files = sorted(
        p for p in fresh if p in stored and stored_hashes.get(p) == fresh[p]
    )

    return {
        "action": "diff",
        "project_id": pid,
        "project_name": pname,
        "folder": folder_abs,
        "db_path": db_path,
        "new": new_files,
        "missing": missing_files,
        "changed": changed_files,
        "unchanged": unchanged_files,
        "skipped": skipped,
        "summary": {
            "new": len(new_files),
            "missing": len(missing_files),
            "changed": len(changed_files),
            "unchanged": len(unchanged_files),
        },
    }


# ---------------------------------------------------------------------------
# Tool entry point
# ---------------------------------------------------------------------------


@mcp.tool()
def media_ingest(
    action: str,
    folder: str = "",
    db_path: str = "",
    project_id: str = "",
    project_name: str = "",
    extensions: str = "",
    recursive: bool = True,
    skip_hidden: bool = True,
    probe: bool = True,
    hash_algo: str = "sha256",
    max_hash_bytes: int = 0,
) -> str:
    """Offline media-ingest / assistant-editor front end. Never touches Resolve.

    Parameters:
    - action: one of
        - "scan": walk `folder` on disk and persist one manifest record per
          file into the local SQLite store at `db_path` (a `clips` row per
          file, upserted under a project). Each record carries `size`,
          content `hash` (algorithm `hash_algo`, default "sha256"), and —
          when the optional `ffprobe` executable is on PATH — a `probe`
          block (`video`: codec/width/height/fps/pix_fmt/colour tags;
          `audio`: codec/channels/sample_rate per track; plus
          container/duration/bit_rate/timecode). Without `ffprobe`, `probe`
          is `null` per item and the top-level `ffprobe_available` is
          `false`, but size/hash are still recorded. `clip_id` is
          deterministic from `(project_id, absolute_path)`, so re-running
          "scan" against an unchanged folder is idempotent: the same rows
          are re-upserted (same hash/size) rather than duplicated, and a
          changed file's row is simply updated in place. Being a
          store-writing action, the result always carries a top-level
          `"verified": false` (structural-only until live Resolve
          calibration), matching the offline tool-set's write-action
          convention.
        - "manifest": read back the manifest already persisted for
          `project_id` from `db_path` (no filesystem access) — returns the
          `project` row plus its `items` (the stored `clips` rows). Errors
          if `project_id` is unknown to the store.
        - "diff": compare a *fresh* scan of `folder` against the manifest
          already stored for `project_id`/`project_name` (or the folder's
          basename, same resolution rule as "scan") WITHOUT persisting
          anything, and report `new` / `missing` / `changed` / `unchanged`
          file paths by content hash (`changed` = same path, different
          hash; `missing` = stored but no longer found on disk). A
          never-scanned project reports every file as `new`.
      Any other action returns the list of valid actions instead of
      erroring.
    - folder: source directory to scan (for "scan"/"diff"). Must exist.
    - db_path: path to the local SQLite store (all actions).
    - project_id / project_name: identify the project the manifest belongs
      to (for "scan"/"diff"/"manifest"). If both are omitted (for "scan"/
      "diff"), a stable id/name are derived from `folder`'s basename so
      repeated scans of the same folder land on the same project.
    - extensions: comma-separated list of extensions to include (no dots,
      e.g. "mov,mp4,mxf,wav"; case-insensitive). Empty (default) means no
      filtering — every regular file under `folder` is included.
    - recursive: walk subdirectories when true (default). When false, only
      `folder`'s immediate contents are scanned.
    - skip_hidden: skip dotfiles/dot-directories (default true).
    - probe: attempt an `ffprobe` spec read per file (default true, for
      "scan" only; degrades to `null` when `ffprobe` is absent or a file
      fails to probe — never raises).
    - hash_algo: digest algorithm, e.g. "sha256" (default), "sha1", "md5",
      "sha512" — any name `hashlib.new()` accepts.
    - max_hash_bytes: when > 0, only the first N bytes of each file are
      hashed (documented `hash_scope` records this trade-off) — a speed
      opt-in for very large media on slow storage. Default 0 hashes the
      whole file.

    A non-existent `folder` always comes back as an "Error: no such
    folder: ..." string (never a raised exception), per the offline
    tool-set convention.
    """
    action_lower = action.strip().lower() if isinstance(action, str) else ""

    try:
        if action_lower == "scan":
            result = _do_scan(
                folder,
                db_path,
                project_id,
                project_name,
                extensions,
                recursive,
                skip_hidden,
                probe,
                hash_algo,
                max_hash_bytes,
            )
            return json.dumps(result, indent=2, default=str)

        if action_lower == "manifest":
            return json.dumps(_do_manifest(db_path, project_id), indent=2, default=str)

        if action_lower == "diff":
            result = _do_diff(
                folder,
                db_path,
                project_id,
                project_name,
                extensions,
                recursive,
                skip_hidden,
                hash_algo,
                max_hash_bytes,
            )
            return json.dumps(result, indent=2, default=str)

        return json.dumps(
            {
                "error": f"Unknown action '{action}'.",
                "valid_actions": list(_VALID_ACTIONS),
            },
            indent=2,
        )
    except Exception as e:  # noqa: BLE001 - offline tools never raise
        return f"Error: {e}"
