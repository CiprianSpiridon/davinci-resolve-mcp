"""
davinci_resolve_mcp.tools.off_editorial — offline editorial integrity /
changelist tool (OFFLINE/advanced tool set).

Registers exactly one action-dispatch tool, ``editorial``, with two actions:

  * ``"changelist"`` — diff two ``.drp``/``.drt`` project files ("project
    A" vs. "project B") and report every clip that was added, removed, or
    moved between them. Clips are matched across the two projects by their
    SeqContainer ``DbId`` — the identity DaVinci Resolve itself preserves
    for a clip across successive saves/exports of the same underlying
    project (see the module notes below). A clip present in B but not A is
    ``added``; present in A but not B is ``removed``; present in both but
    at a different timeline/track/start position is ``moved``. Diffing a
    project against itself (same file, or two byte-identical copies)
    always yields an empty changelist — matching ``dbId``s at unchanged
    positions produce no report entries.
  * ``"integrity"`` — flag dangling media references in a single project:
    timeline clips with an empty ``MediaFilePath`` and, by default,
    non-empty paths that do not resolve to a file on the local filesystem
    (a purely local disk check — this never touches DaVinci Resolve).

This is a thin dispatch/IO layer over :mod:`davinci_resolve_mcp.formats.drp`
and :mod:`davinci_resolve_mcp.formats.drt` (it never reimplements ZIP/XML/
but re-read for shape, not copied: this module is original Python.

Matching notes (``changelist``)
--------------------------------
A SeqContainer clip's ``DbId`` is the stable identity Resolve keeps for a
clip across saves of the *same* project (an unedited clip keeps its DbId
even if the project around it changes); this module leans on that fact
rather than inventing a fuzzier content-based heuristic, so the diff stays
deterministic and free of false "moved"/"added" pairs when two exports of
one project are compared. Two independently authored files that happen to
carry the same clip content but different (freshly generated) DbIds will
report as a full add/remove rather than a match — that is a real limitation
of DbId-based identity, not a bug: this module never guesses.

This module NEVER connects to DaVinci Resolve: no ``DaVinciResolveScript``
import, no live session, nothing at import time beyond registering the
tool. It only *reads* plain ``.drp``/``.drt`` files and the local
filesystem (for the "integrity" disk check) — every action here is
read-only, nothing is written back to disk anywhere. Every code path
returns a JSON string; nothing here raises — the top-level ``editorial``
dispatcher catches every exception and returns an ``"Error: ..."`` string
instead, per the offline tool-set convention. Neither action writes
anything, so neither carries a ``"verified"`` field.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from ..app import mcp
from ..formats import drp as drp_fmt
from ..formats import drt as drt_fmt

_VALID_ACTIONS = ("changelist", "integrity")


class _EditorialError(Exception):
    """Raised for bad ``editorial`` input (missing/unreadable project file,
    etc). Always caught by the top-level ``editorial`` dispatcher and
    turned into an ``"Error: ..."`` string — it never escapes this module."""


# ---------------------------------------------------------------------------
# Project read (composes formats.drp + formats.drt — never reimplements
# either module's ZIP/XML/SeqContainer parsing)
# ---------------------------------------------------------------------------
def _read_project_timelines(project_path: str, *, label: str = "project_path") -> Tuple[List[Dict[str, Any]], Optional[str], str]:
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
        raise _EditorialError(f"editorial: '{label}' is required")
    if not os.path.isfile(project_path):
        raise _EditorialError(f"no such project file: {project_path}")

    try:
        parsed = drp_fmt.read_drp(project_path)
        return list(parsed.get("timelines") or []), parsed.get("name"), "drp"
    except drp_fmt.DrpFormatError:
        pass  # not a .drp (no MpFolder.xml) — fall back to the .drt reader

    try:
        parsed = drt_fmt.parse_drt(project_path)
    except drt_fmt.DrtError as e:
        raise _EditorialError(
            f"could not read {project_path!r} as a .drp or .drt project: {e}"
        ) from e

    project_name = None
    meta = parsed.get("metadata")
    if isinstance(meta, dict):
        project_name = meta.get("projectName") or meta.get("name")
    return list(parsed.get("timelines") or []), project_name, "drt"


def _flatten_clips_indexed(timelines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flatten every clip across every timeline, tagged with its position.

    Each entry carries the clip's identity (``dbId``), its media link, and
    a deterministic position tuple (``timeline``, ``trackType``,
    ``trackIndex``, ``start``) — ``trackIndex`` is the clip's track's
    0-based position within its own type's track list (``Video 1`` == 0,
    ``Video 2`` == 1, ...), following SeqContainer document order, which is
    stable across re-parses of the same bytes.
    """
    out: List[Dict[str, Any]] = []
    for tl in timelines:
        if not isinstance(tl, dict):
            continue
        tl_name = tl.get("name") or "Timeline"
        for track_type, tracks_key in (("video", "videoTracks"), ("audio", "audioTracks")):
            tracks = tl.get(tracks_key) or []
            for track_index, track in enumerate(tracks):
                if not isinstance(track, dict):
                    continue
                for clip in track.get("clips") or []:
                    if not isinstance(clip, dict):
                        continue
                    db_id = clip.get("dbId")
                    if not db_id:
                        continue
                    out.append(
                        {
                            "dbId": db_id,
                            "timeline": tl_name,
                            "trackType": track_type,
                            "trackIndex": track_index,
                            "start": clip.get("start"),
                            "duration": clip.get("duration"),
                            "in": clip.get("in"),
                            "out": clip.get("out"),
                            "mediaFilePath": clip.get("mediaFilePath"),
                            "mediaRef": clip.get("mediaRef"),
                        }
                    )
    return out


def _position(clip: Dict[str, Any]) -> Tuple[Any, Any, Any, Any]:
    return (clip["timeline"], clip["trackType"], clip["trackIndex"], clip["start"])


def _clip_summary(clip: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "dbId": clip["dbId"],
        "timeline": clip["timeline"],
        "trackType": clip["trackType"],
        "trackIndex": clip["trackIndex"],
        "start": clip["start"],
        "duration": clip["duration"],
        "mediaFilePath": clip.get("mediaFilePath"),
    }


# ---------------------------------------------------------------------------
# 'changelist' action — diff two projects by clip DbId identity
# ---------------------------------------------------------------------------
def _diff_projects(clips_a: List[Dict[str, Any]], clips_b: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_id_a: Dict[str, Dict[str, Any]] = {c["dbId"]: c for c in clips_a}
    by_id_b: Dict[str, Dict[str, Any]] = {c["dbId"]: c for c in clips_b}

    added = [
        _clip_summary(by_id_b[db_id])
        for db_id in sorted(set(by_id_b) - set(by_id_a))
    ]
    removed = [
        _clip_summary(by_id_a[db_id])
        for db_id in sorted(set(by_id_a) - set(by_id_b))
    ]

    moved: List[Dict[str, Any]] = []
    for db_id in sorted(set(by_id_a) & set(by_id_b)):
        clip_a = by_id_a[db_id]
        clip_b = by_id_b[db_id]
        if _position(clip_a) != _position(clip_b):
            moved.append(
                {
                    "dbId": db_id,
                    "mediaFilePath": clip_b.get("mediaFilePath") or clip_a.get("mediaFilePath"),
                    "from": _clip_summary(clip_a),
                    "to": _clip_summary(clip_b),
                }
            )

    return {"added": added, "removed": removed, "moved": moved}


def _do_changelist(project_a_path: str, project_b_path: str) -> Dict[str, Any]:
    timelines_a, name_a, kind_a = _read_project_timelines(project_a_path, label="project_a_path")
    timelines_b, name_b, kind_b = _read_project_timelines(project_b_path, label="project_b_path")

    clips_a = _flatten_clips_indexed(timelines_a)
    clips_b = _flatten_clips_indexed(timelines_b)

    diff = _diff_projects(clips_a, clips_b)
    changed = bool(diff["added"] or diff["removed"] or diff["moved"])

    return {
        "action": "changelist",
        "project_a": {
            "path": project_a_path,
            "name": name_a,
            "kind": kind_a,
            "timeline_count": len(timelines_a),
            "clip_count": len(clips_a),
        },
        "project_b": {
            "path": project_b_path,
            "name": name_b,
            "kind": kind_b,
            "timeline_count": len(timelines_b),
            "clip_count": len(clips_b),
        },
        "added": diff["added"],
        "removed": diff["removed"],
        "moved": diff["moved"],
        "added_count": len(diff["added"]),
        "removed_count": len(diff["removed"]),
        "moved_count": len(diff["moved"]),
        "changed": changed,
    }


# ---------------------------------------------------------------------------
# 'integrity' action — dangling media references in one project
# ---------------------------------------------------------------------------
def _do_integrity(project_path: str, check_disk: bool) -> Dict[str, Any]:
    timelines, project_name, project_kind = _read_project_timelines(project_path)
    clips = _flatten_clips_indexed(timelines)

    dangling: List[Dict[str, Any]] = []
    for clip in clips:
        path = clip.get("mediaFilePath")
        if not path or not str(path).strip():
            dangling.append(
                {
                    "kind": "empty_path",
                    "dbId": clip["dbId"],
                    "timeline": clip["timeline"],
                    "trackType": clip["trackType"],
                    "trackIndex": clip["trackIndex"],
                    "start": clip["start"],
                    "message": "clip has an empty MediaFilePath",
                }
            )
            continue
        if check_disk and not os.path.isfile(str(path)):
            dangling.append(
                {
                    "kind": "missing_file",
                    "dbId": clip["dbId"],
                    "timeline": clip["timeline"],
                    "trackType": clip["trackType"],
                    "trackIndex": clip["trackIndex"],
                    "start": clip["start"],
                    "mediaFilePath": path,
                    "message": "clip's MediaFilePath does not resolve to a file on disk",
                }
            )

    return {
        "action": "integrity",
        "project_path": project_path,
        "project_name": project_name,
        "project_kind": project_kind,
        "timeline_count": len(timelines),
        "clips_checked": len(clips),
        "checked_disk": bool(check_disk),
        "dangling": dangling,
        "dangling_count": len(dangling),
        "passed": len(dangling) == 0,
    }


# ---------------------------------------------------------------------------
# Tool entry point
# ---------------------------------------------------------------------------
@mcp.tool()
def editorial(
    action: str,
    project_a_path: str = "",
    project_b_path: str = "",
    project_path: str = "",
    check_disk: bool = True,
) -> str:
    """Offline editorial integrity / changelist engine (OFFLINE/advanced tool set). Never touches Resolve.

    Parameters:
    - action: one of
        - "changelist": parse `project_a_path` and `project_b_path` (each a
          local `.drp` or `.drt` file — tries the `.drp` Media-Pool reader
          first, falls back to the plain `.drt` SeqContainer reader) and
          diff every timeline clip between them. Clips are matched by their
          SeqContainer `DbId` — the identity Resolve itself preserves for a
          clip across successive saves of the same project. A clip's `DbId`
          present only in B is reported under `added`; present only in A is
          reported under `removed`; present in both but at a different
          `(timeline, trackType, trackIndex, start)` position is reported
          under `moved` (with `from`/`to` position snapshots). Diffing a
          project against itself (same path twice, or two byte-identical
          files) always yields empty `added`/`removed`/`moved` lists.
          Returns `{project_a, project_b` (each `{path, name, kind,
          timeline_count, clip_count}`) `, added, removed, moved,
          added_count, removed_count, moved_count, changed}` where
          `changed` is true exactly when any of the three lists is
          non-empty. Missing/unreadable `project_a_path` or
          `project_b_path` is always reported as `"Error: ..."`.
        - "integrity": parse `project_path` and flag dangling media
          references: every clip with an empty `MediaFilePath` (kind
          `"empty_path"`), plus — when `check_disk` is true (default) — every
          clip whose non-empty `MediaFilePath` does not resolve to a file on
          the local filesystem (kind `"missing_file"`; a purely local disk
          check, never a Resolve/media-pool lookup). Returns
          `{project_name, project_kind, timeline_count, clips_checked,
          checked_disk, dangling, dangling_count, passed}` — `passed` is
          true exactly when `dangling` is empty. Missing/unreadable
          `project_path` is always reported as `"Error: ..."`.
      Any other action returns the list of valid actions instead of
      erroring.
    - project_a_path / project_b_path: local `.drp`/`.drt` project files
      being compared ("changelist", both required).
    - project_path: local `.drp`/`.drt` project file to inspect
      ("integrity", required).
    - check_disk: when true (default), also flag clips whose recorded
      media path does not exist on the local filesystem ("integrity";
      set false to check only for empty paths, e.g. when the media
      volume isn't mounted on this machine).

    Malformed/unreadable input never raises — it always comes back as an
    "Error: ..." string, per the offline tool-set convention. Neither
    action writes anything, so neither response carries a "verified"
    field.
    """
    action_lower = action.strip().lower() if isinstance(action, str) else ""

    try:
        if action_lower == "changelist":
            return json.dumps(_do_changelist(project_a_path, project_b_path), indent=2)

        if action_lower == "integrity":
            return json.dumps(_do_integrity(project_path, check_disk), indent=2)

        return json.dumps(
            {
                "error": f"Unknown action '{action}'.",
                "valid_actions": list(_VALID_ACTIONS),
            },
            indent=2,
        )
    except Exception as e:  # noqa: BLE001 - offline tools never raise
        return f"Error: {e}"
