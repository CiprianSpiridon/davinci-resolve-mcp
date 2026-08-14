"""
davinci_resolve_mcp.tools.off_project_read — offline, read-only ``.drp``/``.drt``
project/timeline/clip inspector (OFFLINE/advanced tool set).

Registers exactly one action-dispatch tool, ``project_read``, with three
actions:

  * ``"read"``       — parse a ``.drp`` or ``.drt`` file and return its
    normalized shape (project name/folders/clips for a ``.drp``, timelines
    for either) plus a flat ``clipRecords`` list used by ``clip_where``.
  * ``"lint"``        — structural sanity checks: missing/duplicate clip
    ids, clips with no resolvable media path, timelines with no name/frame
    rate/tracks, a ``.drp`` missing its ``project.xml``.
  * ``"clip_where"``  — filter the project's clip records (timeline clips
    and, for a ``.drp``, Media-Pool clips) by one predicate (`field`/`op`/
    `value`) or several AND-combined predicates (`predicates_json`).

This is a thin dispatch/IO layer over :mod:`davinci_resolve_mcp.formats.drp`
and :mod:`davinci_resolve_mcp.formats.drt`. It does not reimplement
ZIP/XML/SeqContainer/Media-Pool parsing itself; it only resolves file paths,
calls into the formats layer,
flattens the result into clip records, and shapes JSON responses.

This module NEVER connects to DaVinci Resolve: no ``DaVinciResolveScript``
import, no live session, nothing at import time beyond registering the
tool. It only *reads* plain files on disk (``.drp``/``.drt`` zip archives)
— every action here is read-only, nothing is written back to disk. Every
code path returns a JSON string; nothing here raises — the top-level
``project_read`` dispatcher catches every exception and returns an
``"Error: ..."`` string instead. An empty/near-empty project (zero clips,
zero matches) is a normal, successful result — never an error.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from ..app import mcp
from ..formats import drp as drp_fmt
from ..formats import drt as drt_fmt

_VALID_ACTIONS = ("read", "lint", "clip_where")
_VALID_SCOPES = ("all", "timeline", "media_pool")
_VALID_OPS = (
    "eq",
    "neq",
    "contains",
    "icontains",
    "startswith",
    "endswith",
    "gt",
    "gte",
    "lt",
    "lte",
    "isnull",
    "notnull",
    "in",
)


# ---------------------------------------------------------------------------
# Path / project loading
# ---------------------------------------------------------------------------
def _require_path(path: Optional[str], label: str) -> str:
    if not path or not isinstance(path, str) or not path.strip():
        raise ValueError(f"project_read: '{label}' is required")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"no such file: {path}")
    return path


def _load_project(path: Optional[str]) -> Dict[str, Any]:
    """Read ``path`` as a ``.drp`` first, falling back to a bare ``.drt``.

    A ``.drp`` is a superset of a ``.drt`` (it additionally carries
    ``project.xml`` + ``MpFolder.xml``), so :func:`drp_fmt.read_drp` is
    tried first; a ``.drt`` timeline-only archive fails that (no
    ``MpFolder.xml``) and is then read with :func:`drt_fmt.parse_drt`.
    """
    resolved = _require_path(path, "path")

    drp_error: Optional[Exception] = None
    try:
        parsed = drp_fmt.read_drp(resolved)
        return {"kind": "drp", "path": resolved, **parsed}
    except drp_fmt.DrpFormatError as e:
        drp_error = e

    try:
        parsed = drt_fmt.parse_drt(resolved)
        return {"kind": "drt", "path": resolved, **parsed}
    except drt_fmt.DrtError as e:
        raise ValueError(
            f"project_read: could not read '{resolved}' as a .drp ({drp_error}) "
            f"or a .drt ({e})"
        ) from e


# ---------------------------------------------------------------------------
# Clip-record flattening (shared by "read"/"clip_where")
# ---------------------------------------------------------------------------
def _timeline_records(project: Dict[str, Any]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for timeline in project.get("timelines") or []:
        tl_name = timeline.get("name")
        for track in timeline.get("tracks") or []:
            for clip in track.get("clips") or []:
                records.append(
                    {
                        "source": "timeline",
                        "timelineName": tl_name,
                        "trackType": track.get("type"),
                        "trackDbId": track.get("dbId"),
                        "dbId": clip.get("dbId"),
                        "start": clip.get("start"),
                        "duration": clip.get("duration"),
                        "in": clip.get("in"),
                        "out": clip.get("out"),
                        "mediaFilePath": clip.get("mediaFilePath"),
                        "mediaRef": clip.get("mediaRef"),
                    }
                )
    return records


def _media_pool_records(project: Dict[str, Any]) -> List[Dict[str, Any]]:
    if project.get("kind") != "drp":
        return []
    records: List[Dict[str, Any]] = []
    for clip in project.get("allClips") or []:
        records.append(
            {
                "source": "media_pool",
                "kind": clip.get("kind"),
                "name": clip.get("name"),
                "dbId": clip.get("dbId"),
                "mediaPath": clip.get("mediaPath"),
                "linkedByBlob": clip.get("linkedByBlob"),
                "folderPath": "/".join(clip.get("folderPath") or []),
                "uniqueId": clip.get("uniqueId"),
            }
        )
    return records


def _clip_records(project: Dict[str, Any], scope: str) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    if scope in ("all", "timeline"):
        records.extend(_timeline_records(project))
    if scope in ("all", "media_pool"):
        records.extend(_media_pool_records(project))
    return records


# ---------------------------------------------------------------------------
# action="read"
# ---------------------------------------------------------------------------
def _do_read(path: Optional[str]) -> Dict[str, Any]:
    project = _load_project(path)
    records = _clip_records(project, "all")
    result: Dict[str, Any] = {
        "action": "read",
        "path": project["path"],
        "kind": project["kind"],
        "timelines": project.get("timelines") or [],
        "timelineCount": len(project.get("timelines") or []),
        "clipRecords": records,
        "clipRecordCount": len(records),
    }
    if project["kind"] == "drp":
        result["projectName"] = project.get("name")
        result["folders"] = project.get("folders") or []
        result["mediaPoolClips"] = project.get("clips") or []
        result["hasProjectXml"] = project.get("hasProjectXml")
    return result


# ---------------------------------------------------------------------------
# action="lint"
# ---------------------------------------------------------------------------
def _lint_timelines(project: Dict[str, Any]) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    timelines = project.get("timelines") or []
    seen_clip_ids: Dict[str, str] = {}

    for timeline in timelines:
        tl_name = timeline.get("name") or "<unnamed>"
        if not timeline.get("name"):
            issues.append({"code": "timeline-missing-name", "message": "a timeline has no <Name>"})
        if timeline.get("frameRate") is None:
            issues.append(
                {
                    "code": "timeline-missing-framerate",
                    "timeline": tl_name,
                    "message": f"timeline '{tl_name}' has no resolvable <FrameRate>",
                }
            )
        tracks = timeline.get("tracks") or []
        if not tracks:
            issues.append(
                {"code": "empty-timeline", "timeline": tl_name, "message": f"timeline '{tl_name}' has no tracks"}
            )
        for track in tracks:
            for clip in track.get("clips") or []:
                db_id = clip.get("dbId")
                if db_id:
                    if db_id in seen_clip_ids:
                        issues.append(
                            {
                                "code": "duplicate-clip-id",
                                "dbId": db_id,
                                "timeline": tl_name,
                                "message": f"clip DbId {db_id} appears more than once across timeline tracks",
                            }
                        )
                    else:
                        seen_clip_ids[db_id] = tl_name
                if not clip.get("mediaFilePath"):
                    issues.append(
                        {
                            "code": "orphan-media",
                            "dbId": db_id,
                            "timeline": tl_name,
                            "message": "clip has an empty MediaFilePath",
                        }
                    )
    return issues


def _lint_media_pool(project: Dict[str, Any]) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    if not project.get("hasProjectXml"):
        issues.append(
            {
                "code": "missing-project-xml",
                "message": "archive has no project.xml (not a full Resolve project export)",
            }
        )
    seen_clip_ids: Dict[str, bool] = {}
    for clip in project.get("allClips") or []:
        db_id = clip.get("dbId")
        if db_id:
            if db_id in seen_clip_ids:
                issues.append(
                    {
                        "code": "duplicate-media-pool-clip-id",
                        "dbId": db_id,
                        "message": f"media-pool clip DbId {db_id} appears more than once",
                    }
                )
            else:
                seen_clip_ids[db_id] = True
        if clip.get("kind") in ("video", "audio", "still") and not clip.get("mediaPath"):
            issues.append(
                {
                    "code": "unlinked-clip",
                    "dbId": db_id,
                    "name": clip.get("name"),
                    "message": "media-pool clip has no resolvable media path (blob and MediaFilePath both empty)",
                }
            )
    return issues


def _do_lint(path: Optional[str]) -> Dict[str, Any]:
    project = _load_project(path)
    issues = _lint_timelines(project)
    if project["kind"] == "drp":
        issues += _lint_media_pool(project)
        clip_count = len(project.get("allClips") or [])
    else:
        clip_count = sum(
            len(track.get("clips") or [])
            for timeline in project.get("timelines") or []
            for track in timeline.get("tracks") or []
        )

    return {
        "action": "lint",
        "path": project["path"],
        "kind": project["kind"],
        "valid": len(issues) == 0,
        "issueCount": len(issues),
        "issues": issues,
        "timelineCount": len(project.get("timelines") or []),
        "clipCount": clip_count,
    }


# ---------------------------------------------------------------------------
# action="clip_where"
# ---------------------------------------------------------------------------
def _normalize_predicates(
    field: Optional[str], op: Optional[str], value: Optional[str], predicates_json: Optional[str]
) -> List[Dict[str, Any]]:
    raw = (predicates_json or "").strip()
    if raw:
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"predicates_json is not valid JSON: {e}") from e
        if not isinstance(decoded, list):
            raise ValueError("predicates_json must decode to a JSON array of predicate objects")
        predicates: List[Dict[str, Any]] = []
        for i, item in enumerate(decoded):
            if not isinstance(item, dict) or not item.get("field"):
                raise ValueError(f"predicates_json[{i}] must be an object with a 'field' key")
            op_i = str(item.get("op") or "eq").strip().lower()
            if op_i not in _VALID_OPS:
                raise ValueError(f"predicates_json[{i}]: unknown op {item.get('op')!r} (expected one of {_VALID_OPS!r})")
            predicates.append({"field": str(item["field"]), "op": op_i, "value": item.get("value")})
        return predicates

    if field:
        op_norm = str(op or "eq").strip().lower()
        if op_norm not in _VALID_OPS:
            raise ValueError(f"unknown op {op!r} (expected one of {_VALID_OPS!r})")
        return [{"field": field, "op": op_norm, "value": value}]

    return []


def _coerce_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _predicate_match(record: Dict[str, Any], field: str, op: str, value: Any) -> bool:
    actual = record.get(field)

    if op == "isnull":
        return actual is None
    if op == "notnull":
        return actual is not None

    if op in ("gt", "gte", "lt", "lte"):
        a = _coerce_float(actual)
        b = _coerce_float(value)
        if a is None or b is None:
            return False
        if op == "gt":
            return a > b
        if op == "gte":
            return a >= b
        if op == "lt":
            return a < b
        return a <= b  # lte

    if op == "in":
        if actual is None:
            return False
        choices = [c.strip() for c in str(value).split(",")]
        return str(actual) in choices

    if actual is None:
        # A string-oriented comparison against a missing field is simply "no match"
        # (except eq/neq against an explicit None-ish empty value, handled below).
        if op == "eq":
            return value is None or str(value) == ""
        if op == "neq":
            return not (value is None or str(value) == "")
        return False

    a_str = str(actual)
    b_str = "" if value is None else str(value)
    if op == "eq":
        return a_str == b_str
    if op == "neq":
        return a_str != b_str
    if op == "contains":
        return b_str in a_str
    if op == "icontains":
        return b_str.lower() in a_str.lower()
    if op == "startswith":
        return a_str.startswith(b_str)
    if op == "endswith":
        return a_str.endswith(b_str)

    raise ValueError(f"unknown op {op!r}")  # pragma: no cover - guarded upstream


def _do_clip_where(
    path: Optional[str],
    field: Optional[str],
    op: Optional[str],
    value: Optional[str],
    predicates_json: Optional[str],
    scope: Optional[str],
) -> Dict[str, Any]:
    scope_norm = (scope or "all").strip().lower() or "all"
    if scope_norm not in _VALID_SCOPES:
        raise ValueError(f"clip_where: 'scope' must be one of {_VALID_SCOPES!r} (got {scope!r})")

    project = _load_project(path)
    records = _clip_records(project, scope_norm)
    predicates = _normalize_predicates(field, op, value, predicates_json)

    if predicates:
        matches = [
            r for r in records if all(_predicate_match(r, p["field"], p["op"], p.get("value")) for p in predicates)
        ]
    else:
        matches = list(records)

    return {
        "action": "clip_where",
        "path": project["path"],
        "kind": project["kind"],
        "scope": scope_norm,
        "predicates": predicates,
        "recordCount": len(records),
        "matchCount": len(matches),
        "matches": matches,
    }


# ---------------------------------------------------------------------------
# Public tool
# ---------------------------------------------------------------------------
@mcp.tool()
def project_read(
    action: str,
    path: str = "",
    field: str = "",
    op: str = "eq",
    value: str = "",
    predicates_json: str = "",
    scope: str = "all",
) -> str:
    """Offline, read-only ``.drp``/``.drt`` project/timeline/clip inspector. Never touches Resolve.

    Parameters:
    - action: one of
        - "read": parse `path` (a ``.drp`` or ``.drt`` file on disk — a
          ``.drp`` is tried first, falling back to a bare ``.drt``) and
          return `{kind, timelines, timelineCount, clipRecords,
          clipRecordCount}`; for a ``.drp`` it additionally returns
          `projectName`, `folders`, `mediaPoolClips`, `hasProjectXml`.
          `clipRecords` flattens every timeline clip (and, for a ``.drp``,
          every Media-Pool clip) into one list — the same shape
          `clip_where` filters over.
        - "lint": structural sanity checks over `path`, returning
          `{valid, issueCount, issues, timelineCount, clipCount}`. Checks:
          a timeline with no `<Name>`/`<FrameRate>`/tracks, a duplicate
          clip `DbId` within timeline tracks, a timeline clip with an
          empty `MediaFilePath`, and (for a ``.drp``) a missing
          `project.xml`, a duplicate Media-Pool clip `DbId`, and a
          Media-Pool clip with no resolvable media path (blob and
          `MediaFilePath` both empty). A project with zero issues returns
          `valid: true` and an empty `issues` list — never an error; a
          project with zero clips/timelines is likewise a normal
          (`clipCount: 0`) result, not an error.
        - "clip_where": filter the project's clip records by one predicate
          (`field`/`op`/`value`) or several AND-combined predicates
          (`predicates_json`, a JSON array of `{"field","op"?,"value"?}`
          objects — `op` defaults to `"eq"`). Returns `{scope, predicates,
          recordCount, matchCount, matches}`. With neither `field` nor
          `predicates_json` given, every record in `scope` is returned
          (no filter). An empty/no-match result is `matchCount: 0,
          matches: []` — never an error.
      Any other action returns the list of valid actions instead of
      erroring.
    - path: source ``.drp``/``.drt`` file path (required for every action).
    - field: record field to filter on (for "clip_where" single-predicate
      form). Timeline-clip records carry `source="timeline"`,
      `timelineName`, `trackType`, `trackDbId`, `dbId`, `start`,
      `duration`, `in`, `out`, `mediaFilePath`, `mediaRef`. Media-Pool
      clip records (``.drp`` only) carry `source="media_pool"`, `kind`,
      `name`, `dbId`, `mediaPath`, `linkedByBlob`, `folderPath`,
      `uniqueId`.
    - op: comparison operator for the single-predicate form — one of
      "eq", "neq", "contains", "icontains", "startswith", "endswith",
      "gt", "gte", "lt", "lte", "isnull", "notnull", "in" (comma-separated
      value list). Default "eq".
    - value: comparison value for the single-predicate form.
    - predicates_json: JSON array of `{"field","op"?,"value"?}` objects,
      AND-combined, for a multi-predicate "clip_where" query. Overrides
      `field`/`op`/`value` when given.
    - scope: which clip records to search for "clip_where" — "all"
      (default), "timeline" (timeline-track clips only), or "media_pool"
      (``.drp`` Media-Pool clips only; empty for a bare ``.drt``).

    A missing/unreadable file, an archive that is neither a valid ``.drp``
    nor a valid ``.drt``, or bad JSON/predicate input always comes back as
    an "Error: ..." string (never a raised exception), per the offline
    tool-set convention. Every action here is read-only — nothing is ever
    written back to `path`.
    """
    action_lower = action.strip().lower() if isinstance(action, str) else ""
    try:
        if action_lower == "read":
            return json.dumps(_do_read(path), indent=2)
        if action_lower == "lint":
            return json.dumps(_do_lint(path), indent=2)
        if action_lower == "clip_where":
            return json.dumps(_do_clip_where(path, field, op, value, predicates_json, scope), indent=2)

        return json.dumps(
            {
                "error": f"Unknown action '{action}'.",
                "valid_actions": list(_VALID_ACTIONS),
            },
            indent=2,
        )
    except Exception as e:  # noqa: BLE001 - offline tools never raise
        return f"Error: {e}"
