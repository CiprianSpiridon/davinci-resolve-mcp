"""
davinci_resolve_mcp.tools.off_color_trace — offline ``color_trace``: carry
grades across a re-conform by clip identity (OFFLINE/advanced tool set).

Registers exactly one action-dispatch tool, ``color_trace``, with one action:

  * ``"carry"`` — match every clip in a TARGET timeline (a re-conformed
    ``.drp``/``.drt``) to a clip in a SOURCE timeline (the graded project it
    replaced), and carry each matched clip's grade across the re-conform.

Resolve's built-in ColorTrace matches by timecode/name/order *within one
project* and breaks on renames, reorders, and retimes across a re-conform.
This instead reads clip lists straight from each project's timeline (offline,
via :mod:`davinci_resolve_mcp.formats.drp` / ``.drt``, no live Resolve, even
cross-project) and matches by a content key derived from the clip's linked
media filename — exact match first, then a normalized key (extension/path
stripped, separators collapsed, a trailing version token like ``_v2``
dropped) so a rename-only re-conform still traces.

Grade payloads
---------------
A ``.drt``/``.drp`` timeline clip carries its per-instance grade as a
``<Body>`` hex blob (the framed zstd/protobuf stream documented in
:mod:`davinci_resolve_mcp.formats.drx_codec`); see
:func:`davinci_resolve_mcp.formats.drt.parse_seq_container` /
``_extract_clips``, which locates but never decodes it. ``carry`` copies a
matched source clip's ``Body`` hex **byte-for-byte** into the plan output (and,
when ``output_dir`` is given, into a standalone ``.drx`` grade envelope per
matched clip) — it never round-trips the value through
:mod:`drx_codec`'s encode/decode to produce the copy, so there is no
re-encode drift. :func:`drx_codec.decode_fields` /
:func:`drx_codec.encode_fields` are used only to *report* a parameter count
and a best-effort round-trip check (``roundtripVerified``) alongside the
verbatim copy.

Every target clip is accounted for: a clip with no source match is reported
in both ``matches`` (``source: null``) and a dedicated ``unmatchedClips``
list — never silently dropped. A matched clip whose source has no grade gets
``gradeApply.status == "no-source-grade"`` rather than being treated as an
error.

This is a thin dispatch/IO layer over :mod:`davinci_resolve_mcp.formats.drp`,
:mod:`davinci_resolve_mcp.formats.drt`, and
:mod:`davinci_resolve_mcp.formats.drx_codec`. It does not reimplement
ZIP/XML/SeqContainer/protobuf parsing itself; it only resolves file paths,
calls into the formats layer,
matches clips, and shapes JSON responses / a minimal ``.drx`` envelope.

This module NEVER connects to DaVinci Resolve: no ``DaVinciResolveScript``
import, no live session, nothing at import time beyond registering the tool.
It only reads plain ``.drp``/``.drt`` files on disk and, only when
``output_dir`` is given, writes standalone ``.drx`` grade-envelope files
there — it never modifies ``source_path``/``target_path`` themselves. Every
code path returns a JSON string; nothing here raises — the top-level
``color_trace`` dispatcher catches every exception and returns an
``"Error: ..."`` string instead. Because ``carry`` produces grade payloads
(copied verbatim, but not calibration-checked against a live Resolve), its
result always carries a top-level ``"verified": false`` field.
"""

from __future__ import annotations

import json
import os
import re
import uuid as _uuid
from typing import Any, Dict, List, Optional, Tuple

from ..app import mcp
from ..formats import drp as drp_fmt
from ..formats import drt as drt_fmt
from ..formats import drx_codec

_VALID_ACTIONS = ("carry",)
_VALID_TRACK_TYPES = ("video", "audio")

_VERSION_SUFFIX_RE = re.compile(r"\s+v?\d{1,3}$", re.IGNORECASE)
_SEP_RE = re.compile(r"[_\-.\s]+")
_EXT_RE = re.compile(r"\.[^.]+$")


# ---------------------------------------------------------------------------
# Path / project loading (mirrors off_project_read's drp-first, drt-fallback
# convention — a .drp is a superset of a .drt)
# ---------------------------------------------------------------------------
def _require_path(path: Optional[str], label: str) -> str:
    if not path or not isinstance(path, str) or not path.strip():
        raise ValueError(f"color_trace: '{label}' is required")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"no such file: {path}")
    return path


def _load_project(path: str) -> Dict[str, Any]:
    drp_error: Optional[Exception] = None
    try:
        parsed = drp_fmt.read_drp(path)
        return {"kind": "drp", "path": path, **parsed}
    except drp_fmt.DrpFormatError as e:
        drp_error = e

    try:
        parsed = drt_fmt.parse_drt(path)
        return {"kind": "drt", "path": path, **parsed}
    except drt_fmt.DrtError as e:
        raise ValueError(
            f"color_trace: could not read '{path}' as a .drp ({drp_error}) or a .drt ({e})"
        ) from e


def _select_timeline(project: Dict[str, Any], timeline_name: Optional[str], side: str) -> Dict[str, Any]:
    timelines = project.get("timelines") or []
    if not timelines:
        raise ValueError(f"color_trace: {side} project '{project['path']}' has no timelines")
    name = (timeline_name or "").strip()
    if name:
        for t in timelines:
            if t.get("name") == name:
                return t
        found = [t.get("name") for t in timelines]
        raise ValueError(
            f"color_trace: {side} project has no timeline named {name!r} (found: {found!r})"
        )
    if len(timelines) == 1:
        return timelines[0]
    found = [t.get("name") for t in timelines]
    raise ValueError(
        f"color_trace: {side} project has {len(timelines)} timelines ({found!r}); "
        f"specify '{side}_timeline'"
    )


# ---------------------------------------------------------------------------
# Clip identity matching
# ---------------------------------------------------------------------------
def _clip_name(clip: Dict[str, Any]) -> str:
    """Best-effort clip identity name: the linked media's filename, falling
    back to the media reference / clip id when there is no media path."""
    path = clip.get("mediaFilePath") or ""
    if path:
        return os.path.basename(str(path))
    return str(clip.get("mediaRef") or clip.get("dbId") or "")


def _normalize_name(name: str) -> str:
    """Normalize a clip name to a match key: drop path/extension, lowercase,
    collapse separators, strip a trailing version token (``_v2``, ``.01``)."""
    n = os.path.basename(str(name or ""))
    n = _EXT_RE.sub("", n)
    n = n.lower()
    n = _SEP_RE.sub(" ", n).strip()
    n = _VERSION_SUFFIX_RE.sub("", n).strip()
    return n


def _track_clips(timeline: Dict[str, Any], track_type: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for track in timeline.get("tracks") or []:
        if str(track.get("type") or "").strip().lower() != track_type:
            continue
        out.extend(track.get("clips") or [])
    return out


def _build_index(clips: List[Dict[str, Any]]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    exact: Dict[str, Dict[str, Any]] = {}
    norm: Dict[str, Dict[str, Any]] = {}
    for c in clips:
        name = _clip_name(c)
        if not name:
            continue
        exact.setdefault(name, c)
        k = _normalize_name(name)
        if k:
            norm.setdefault(k, c)
    return exact, norm


def _match_clip(
    target_clip: Dict[str, Any],
    exact_idx: Dict[str, Dict[str, Any]],
    norm_idx: Dict[str, Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Optional[str], float]:
    name = _clip_name(target_clip)
    if name and name in exact_idx:
        return exact_idx[name], "exact-name", 1.0
    k = _normalize_name(name)
    if k and k in norm_idx:
        return norm_idx[k], "normalized-name", 0.85
    return None, None, 0.0


# ---------------------------------------------------------------------------
# .drx grade envelope authoring (minimal, matches the real Gallery_GyStill /
# ListMgt_LmVersion shape parsed by formats.drx_xml — new content, not a
# round-trip of parsed input, so it is built here rather than in drx_xml)
# ---------------------------------------------------------------------------
def _xml_escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _build_drx_envelope(label: str, body_hex: str) -> str:
    still_id = str(_uuid.uuid4())
    ver_id = str(_uuid.uuid4())
    esc_label = _xml_escape(label)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!--DbAppVer="21.0.0.0048" DbPrjVer="17"-->\n'
        f'<Gallery_GyStill DbId="{still_id}">\n'
        ' <FieldsBlob/>\n'
        f' <SrcHint>{esc_label}</SrcHint>\n'
        ' <SrcType>1</SrcType>\n'
        f' <Label>{esc_label}</Label>\n'
        ' <Width>1920</Width>\n'
        ' <Height>1080</Height>\n'
        ' <BitDepth>10</BitDepth>\n'
        ' <PAR>1</PAR>\n'
        ' <Endianship>1</Endianship>\n'
        ' <pClipFullVer>\n'
        f'  <ListMgt_LmVersion DbId="{ver_id}">\n'
        '   <FieldsBlob/>\n'
        '   <Name/>\n'
        '   <HasCorrection>true</HasCorrection>\n'
        '   <VerType>0</VerType>\n'
        '   <ImplVersion>1</ImplVersion>\n'
        f'   <Body>{body_hex}</Body>\n'
        '  </ListMgt_LmVersion>\n'
        ' </pClipFullVer>\n'
        '</Gallery_GyStill>\n'
    )


def _describe_grade(body_hex: str) -> Dict[str, Any]:
    """Decode ``body_hex`` (report-only) and check whether re-encoding the
    decoded dict reproduces it, without ever using the re-encoded value as the
    copy (the copy is always the verbatim ``body_hex``)."""
    decoded = drx_codec.decode_fields(body_hex)
    roundtrip_ok = False
    try:
        re_encoded = drx_codec.encode_fields(decoded)
        if isinstance(re_encoded, (bytes, bytearray)):
            re_encoded = re_encoded.hex()
        roundtrip_ok = isinstance(re_encoded, str) and re_encoded.lower() == "".join(
            str(body_hex).split()
        ).lower()
    except drx_codec.DrxCodecError:
        roundtrip_ok = False
    return {
        "paramCount": decoded.get("count"),
        "framing": decoded.get("framing"),
        "compressed": decoded.get("compressed"),
        "decodeError": decoded.get("error"),
        "roundtripVerified": roundtrip_ok,
    }


# ---------------------------------------------------------------------------
# action="carry"
# ---------------------------------------------------------------------------
def _do_carry(
    source_path: Optional[str],
    source_timeline: Optional[str],
    target_path: Optional[str],
    target_timeline: Optional[str],
    track_type: Optional[str],
    output_dir: Optional[str],
) -> Dict[str, Any]:
    track_type_norm = (track_type or "video").strip().lower()
    if track_type_norm not in _VALID_TRACK_TYPES:
        raise ValueError(
            f"color_trace carry: 'track_type' must be one of {_VALID_TRACK_TYPES!r} (got {track_type!r})"
        )

    src_resolved = _require_path(source_path, "source_path")
    tgt_resolved = _require_path(target_path, "target_path")

    source_project = _load_project(src_resolved)
    target_project = _load_project(tgt_resolved)

    src_tl = _select_timeline(source_project, source_timeline, "source")
    tgt_tl = _select_timeline(target_project, target_timeline, "target")

    src_clips = _track_clips(src_tl, track_type_norm)
    tgt_clips = _track_clips(tgt_tl, track_type_norm)

    if not src_clips:
        raise ValueError(
            f"color_trace carry: source timeline '{src_tl.get('name')}' has no {track_type_norm} clips"
        )
    if not tgt_clips:
        raise ValueError(
            f"color_trace carry: target timeline '{tgt_tl.get('name')}' has no {track_type_norm} clips"
        )

    exact_idx, norm_idx = _build_index(src_clips)

    out_dir: Optional[str] = None
    if output_dir and str(output_dir).strip():
        out_dir = str(output_dir).strip()
        os.makedirs(out_dir, exist_ok=True)

    by_method = {"exact-name": 0, "normalized-name": 0, "unmatched": 0}
    grades_ready = 0
    matches: List[Dict[str, Any]] = []
    unmatched_clips: List[Dict[str, Any]] = []

    for i, t in enumerate(tgt_clips):
        src_clip, method, confidence = _match_clip(t, exact_idx, norm_idx)
        by_method[method or "unmatched"] += 1

        target_entry = {
            "name": _clip_name(t),
            "dbId": t.get("dbId"),
            "start": t.get("start"),
            "duration": t.get("duration"),
            "mediaFilePath": t.get("mediaFilePath"),
        }

        if src_clip is None:
            unmatched_clips.append(target_entry)
            matches.append(
                {
                    "target": target_entry,
                    "source": None,
                    "method": None,
                    "confidence": 0.0,
                    "gradeApply": None,
                }
            )
            continue

        source_entry = {
            "name": _clip_name(src_clip),
            "dbId": src_clip.get("dbId"),
            "start": src_clip.get("start"),
            "duration": src_clip.get("duration"),
            "mediaFilePath": src_clip.get("mediaFilePath"),
        }

        body_hex = src_clip.get("bodyHex")
        if body_hex:
            grades_ready += 1
            grade_apply: Dict[str, Any] = {
                "status": "ready",
                "sourceClip": source_entry["name"],
                # Verbatim copy of the source Body hex — never re-encoded.
                "bodyHex": body_hex,
                **_describe_grade(body_hex),
            }
            if out_dir is not None:
                drx_path = os.path.join(out_dir, f"trace-{i:04d}.drx")
                label = source_entry["name"] or f"clip-{i}"
                with open(drx_path, "w", encoding="utf-8") as fh:
                    fh.write(_build_drx_envelope(label, body_hex))
                grade_apply["drxPath"] = drx_path
        else:
            grade_apply = {"status": "no-source-grade", "sourceClip": source_entry["name"]}

        matches.append(
            {
                "target": target_entry,
                "source": source_entry,
                "method": method,
                "confidence": confidence,
                "gradeApply": grade_apply,
            }
        )

    matched_count = len(tgt_clips) - len(unmatched_clips)

    return {
        "action": "carry",
        "source": {"path": src_resolved, "timeline": src_tl.get("name"), "clips": len(src_clips)},
        "target": {"path": tgt_resolved, "timeline": tgt_tl.get("name"), "clips": len(tgt_clips)},
        "trackType": track_type_norm,
        "summary": {
            "matched": matched_count,
            "unmatched": len(unmatched_clips),
            "gradesReady": grades_ready,
            "byMethod": by_method,
        },
        "matches": matches,
        "unmatchedClips": unmatched_clips,
        "outputDir": out_dir,
        "verified": False,
    }


# ---------------------------------------------------------------------------
# Public tool
# ---------------------------------------------------------------------------
@mcp.tool()
def color_trace(
    action: str,
    source_path: str = "",
    source_timeline: str = "",
    target_path: str = "",
    target_timeline: str = "",
    track_type: str = "video",
    output_dir: str = "",
) -> str:
    """A better ColorTrace, offline: carry grades across a re-conform by clip identity. Never touches Resolve.

    Parameters:
    - action: one of
        - "carry": match every clip in `target_timeline` of `target_path`
          (the re-conformed project/timeline, ``.drp`` or ``.drt``) to a
          clip in `source_timeline` of `source_path` (the graded project it
          replaced) by content identity — the linked media's filename,
          exact match first (confidence 1.0), then a normalized key with
          path/extension/case/separators/a trailing version token (``_v2``)
          stripped (confidence 0.85) — so a rename-only re-conform still
          traces. For each matched clip whose source carries a grade
          (a ``<Body>`` hex blob on the timeline clip), the grade is copied
          **byte-for-byte** into `gradeApply.bodyHex` (never round-tripped
          through the drx codec — `drx_codec.decode_fields`/`encode_fields`
          are only used to report `paramCount`/`framing`/`roundtripVerified`
          alongside the verbatim copy, so there is no re-encode drift).
          When `output_dir` is given, each grade-ready match also gets a
          standalone ``.drx`` grade envelope written to
          `output_dir/trace-<i>.drx` (`gradeApply.drxPath`) wrapping that
          same verbatim `Body` hex, ready for a live grade-apply tool.
          Returns `{source, target, trackType, summary, matches,
          unmatchedClips, outputDir, verified}`. `summary` is `{matched,
          unmatched, gradesReady, byMethod}` where
          `matched + unmatched == target.clips` — every target clip is
          accounted for. Every entry in `matches` carries a `target` (and,
          if matched, a `source`); a match with no source grade gets
          `gradeApply.status == "no-source-grade"` rather than being
          treated as an error, and every unmatched clip additionally
          appears in `unmatchedClips` (never silently dropped). The result
          always carries `"verified": false` — the byte-exact copy is
          structural-only until calibrated against a live Resolve.
      Any other action returns the list of valid actions instead of
      erroring.
    - source_path: the OLD (graded) project/timeline file (``.drp`` or
      ``.drt``), required.
    - source_timeline: timeline name to read source clips from. Optional
      when `source_path` has exactly one timeline; required (and must
      match a `<Name>` exactly) when it has more than one.
    - target_path: the NEW (re-conformed) project/timeline file, required.
    - target_timeline: timeline name to read target clips from, same rule
      as `source_timeline`.
    - track_type: which track's clips to match — "video" (default) or
      "audio".
    - output_dir: if set, write one standalone ``.drx`` grade envelope per
      grade-ready match into this directory (created if missing). Leave
      empty for a match-plan-only (no file writes) result.

    A missing/unreadable `source_path`/`target_path`, a project that is
    neither a valid ``.drp`` nor a valid ``.drt``, an unresolvable timeline
    name (or an ambiguous omitted one), or a timeline with no clips of
    `track_type` always comes back as an "Error: ..." string (never a
    raised exception), per the offline tool-set convention. `source_path`
    and `target_path` themselves are never modified — only files under
    `output_dir` (when given) are written.
    """
    action_lower = action.strip().lower() if isinstance(action, str) else ""
    try:
        if action_lower == "carry":
            return json.dumps(
                _do_carry(
                    source_path,
                    source_timeline,
                    target_path,
                    target_timeline,
                    track_type,
                    output_dir,
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
