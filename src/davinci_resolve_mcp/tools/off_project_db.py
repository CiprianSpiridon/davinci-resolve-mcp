"""
davinci_resolve_mcp.tools.off_project_db — DB-backed project operations
(OFFLINE/advanced tool set).

Single action-dispatch entry point (``project_db``) with two actions:

  * ``"relayout_node_graphs"`` — tidy the color-page node-editor layout
    (x/y position) of every graded correction record across a set of
    ``.drx`` grade files, without touching any grade content. This is the
    offline analogue of Resolve's "Cleanup Node Graph" command, which has
    no scripting-API equivalent: it rewrites *only* the node-position
    fields inside each correction's ``Body`` blob (a framed zstd protobuf,
    see :mod:`davinci_resolve_mcp.formats.drx_codec`) while leaving the
    ``FieldsBlob`` field byte-for-byte untouched, and every other byte of
    the ``Body`` (OFX/qualifier params, keyframes, LUT refs, labels...)
    verbatim.
  * ``"index"`` — populate the local SQLite store
    (:mod:`davinci_resolve_mcp.store.db`) with a project's clips (one per
    ``.drx`` still) and grades (one per correction record), keyed so that
    re-running "index" over the same inputs is idempotent (upsert, not
    insert).

Node-position layout note
--------------------------
:mod:`davinci_resolve_mcp.formats.drx_codec` decodes/encodes named *grade
parameters* (``F3{F1 id, F2{F1 float32}}`` entries), not the node-editor's
x/y layout fields (``F1`` container -> ``F7`` node message -> ``F4``/``F5``
varints). This module owns that narrow, additional protobuf walk itself —
composing :mod:`drx_codec`'s framing/zstd layer and
:mod:`davinci_resolve_mcp.formats.drx_xml`'s XML envelope/tree layer rather
than re-implementing either — since relayout is this tool's own concern and
no other offline tool needs the position fields.

This module never connects to DaVinci Resolve: it only reads/writes local
``.drx``/SQLite files. Every code path returns a JSON string; nothing here
raises — the top-level ``project_db`` dispatcher catches every exception and
returns an ``"Error: ..."`` string instead, per the offline tool-set
convention.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..app import mcp
from ..formats import drx_codec
from ..formats.drx_xml import DrxParseError, parse_drx, serialize_drx
from ..store.db import StoreError, open_db

_VALID_ACTIONS = ("relayout_node_graphs", "index")

# vendor/drx-codec/node-layout.js — measured from a live before/after
# Project.db diff on a 3-node graph).
_DEFAULT_ORIGIN_X = 290
_DEFAULT_ORIGIN_Y = 428
_DEFAULT_SPACING_X = 495

# ≤2px tolerance when deciding a graph is "already clean": native Cleanup
# rounds spacing slightly differently run to run — don't churn rows that
# are effectively already laid out.
_CLEAN_TOLERANCE_PX = 2


class _RelayoutError(Exception):
    """Raised for a single correction record that cannot be relaid out
    (e.g. a non-node-graph Body, or a positions/node-count mismatch).
    Callers catch this per-record and report it in ``skipped`` rather than
    failing the whole sweep."""


# ---------------------------------------------------------------------------
# Minimal protobuf varint / length-delimited walk (node x/y positions only)
# ---------------------------------------------------------------------------
_WT_VARINT = 0
_WT_FIXED64 = 1
_WT_LEN = 2
_WT_FIXED32 = 5

# Node message field numbers inside a decompressed grade Body:
# F1 = node-graph container (repeated), F7 = node message, F4 = xPos varint,
# F5 = yPos varint.
_F_CONTAINER = 1
_F_NODE = 7
_F_X = 4
_F_Y = 5


def _read_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    value = 0
    shift = 0
    n = len(buf)
    while pos < n:
        b = buf[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not (b & 0x80):
            return value, pos
        shift += 7
        if shift > 63:
            raise _RelayoutError("varint too long")
    raise _RelayoutError("truncated varint")


def _encode_varint(value: int) -> bytes:
    if value < 0:
        raise _RelayoutError("cannot encode a negative varint")
    out = bytearray()
    v = value
    while v >= 0x80:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v)
    return bytes(out)


def _walk_fields(buf: bytes) -> List[Dict[str, Any]]:
    """Flat, single-level protobuf walk: varint/fixed32/fixed64/length-delimited
    fields only (the wire types this codec ever emits). Each entry carries
    ``start``/``end`` (absolute offsets into ``buf``) so unrelated bytes can be
    copied through verbatim, plus ``value`` (varints) or ``payload`` (LEN)."""
    out: List[Dict[str, Any]] = []
    pos = 0
    n = len(buf)
    while pos < n:
        start = pos
        tag, pos = _read_varint(buf, pos)
        num = tag >> 3
        wt = tag & 0x07
        if wt == _WT_VARINT:
            value, pos = _read_varint(buf, pos)
            out.append({"num": num, "wt": wt, "start": start, "end": pos, "value": value})
        elif wt == _WT_FIXED64:
            pos += 8
            if pos > n:
                raise _RelayoutError("truncated fixed64 field")
            out.append({"num": num, "wt": wt, "start": start, "end": pos})
        elif wt == _WT_FIXED32:
            pos += 4
            if pos > n:
                raise _RelayoutError("truncated fixed32 field")
            out.append({"num": num, "wt": wt, "start": start, "end": pos})
        elif wt == _WT_LEN:
            length, dpos = _read_varint(buf, pos)
            end = dpos + length
            if end > n:
                raise _RelayoutError("truncated length-delimited field")
            out.append({"num": num, "wt": wt, "start": start, "end": end, "payload": buf[dpos:end]})
            pos = end
        else:
            raise _RelayoutError(f"unsupported protobuf wire type {wt}")
    return out


def _count_nodes(proto_body: bytes) -> int:
    count = 0
    for t in _walk_fields(proto_body):
        if t["num"] != _F_CONTAINER or t["wt"] != _WT_LEN:
            continue
        for g in _walk_fields(t["payload"]):
            if g["num"] == _F_NODE and g["wt"] == _WT_LEN:
                count += 1
    return count


def _read_node_positions(proto_body: bytes) -> List[Tuple[Optional[int], Optional[int]]]:
    """Decode each node's (x, y) layout position, in encounter order."""
    positions: List[Tuple[Optional[int], Optional[int]]] = []
    for t in _walk_fields(proto_body):
        if t["num"] != _F_CONTAINER or t["wt"] != _WT_LEN:
            continue
        for g in _walk_fields(t["payload"]):
            if g["num"] != _F_NODE or g["wt"] != _WT_LEN:
                continue
            x: Optional[int] = None
            y: Optional[int] = None
            for h in _walk_fields(g["payload"]):
                if h["num"] == _F_X and h["wt"] == _WT_VARINT:
                    x = h["value"]
                elif h["num"] == _F_Y and h["wt"] == _WT_VARINT:
                    y = h["value"]
            positions.append((x, y))
    return positions


def _clean_row_positions(n: int, origin_x: int, origin_y: int, spacing_x: int) -> List[Tuple[int, int]]:
    return [(origin_x + i * spacing_x, origin_y) for i in range(n)]


def _rewrite_node_message(node_buf: bytes, x: int, y: int) -> bytes:
    """Rewrite F4(x)/F5(y) in one node message; every other field/byte is
    copied through verbatim. Appends F4/F5 if either was absent."""
    parts: List[bytes] = []
    saw_x = False
    saw_y = False
    for h in _walk_fields(node_buf):
        if h["num"] == _F_X and h["wt"] == _WT_VARINT:
            saw_x = True
            parts.append(_encode_varint((_F_X << 3) | _WT_VARINT))
            parts.append(_encode_varint(x))
        elif h["num"] == _F_Y and h["wt"] == _WT_VARINT:
            saw_y = True
            parts.append(_encode_varint((_F_Y << 3) | _WT_VARINT))
            parts.append(_encode_varint(y))
        else:
            parts.append(node_buf[h["start"]:h["end"]])
    if not saw_x:
        parts.append(_encode_varint((_F_X << 3) | _WT_VARINT))
        parts.append(_encode_varint(x))
    if not saw_y:
        parts.append(_encode_varint((_F_Y << 3) | _WT_VARINT))
        parts.append(_encode_varint(y))
    return b"".join(parts)


def _relayout_proto_body(proto_body: bytes, positions: Sequence[Tuple[int, int]]) -> bytes:
    """Rebuild ``proto_body`` with every node's (x, y) rewritten to
    ``positions`` (encounter order); every other byte survives verbatim."""
    node_idx = 0
    rebuilt: List[bytes] = []
    for t in _walk_fields(proto_body):
        if t["num"] == _F_CONTAINER and t["wt"] == _WT_LEN:
            inner_parts: List[bytes] = []
            for g in _walk_fields(t["payload"]):
                if g["num"] == _F_NODE and g["wt"] == _WT_LEN:
                    x, y = positions[node_idx]
                    node_idx += 1
                    new_node = _rewrite_node_message(g["payload"], x, y)
                    inner_parts.append(_encode_varint((_F_NODE << 3) | _WT_LEN))
                    inner_parts.append(_encode_varint(len(new_node)))
                    inner_parts.append(new_node)
                else:
                    inner_parts.append(t["payload"][g["start"]:g["end"]])
            new_inner = b"".join(inner_parts)
            rebuilt.append(_encode_varint((_F_CONTAINER << 3) | _WT_LEN))
            rebuilt.append(_encode_varint(len(new_inner)))
            rebuilt.append(new_inner)
        else:
            rebuilt.append(proto_body[t["start"]:t["end"]])
    return b"".join(rebuilt)


def _relayout_body_hex(
    body_hex: str,
    origin_x: int,
    origin_y: int,
    spacing_x: int,
) -> Dict[str, Any]:
    """Relayout one correction's ``Body`` blob (hex text) to an evenly spaced
    clean row. Returns ``{"body_hex", "node_count", "before", "after"}``.
    Raises :class:`_RelayoutError` for a Body that isn't a rewritable node
    graph (missing/unsupported framing, malformed protobuf, or zero nodes)."""
    decoded = drx_codec.decode_body(body_hex)
    if decoded.get("error"):
        raise _RelayoutError(decoded["error"])
    proto_body = decoded["body"]
    if not proto_body:
        raise _RelayoutError("Body decoded to an empty protobuf body")

    before = _read_node_positions(proto_body)
    node_count = len(before)
    if node_count == 0:
        raise _RelayoutError("relayout refused: Body decodes to 0 nodes")

    after = _clean_row_positions(node_count, origin_x, origin_y, spacing_x)
    new_proto = _relayout_proto_body(proto_body, after)
    new_blob = drx_codec.encode_body(new_proto, decoded["framing"], decoded["compressed"])

    return {
        "body_hex": new_blob.hex(),
        "node_count": node_count,
        "before": before,
        "after": after,
    }


def _positions_clean(
    before: Sequence[Tuple[Optional[int], Optional[int]]],
    after: Sequence[Tuple[int, int]],
) -> bool:
    """True when ``before`` already matches ``after`` within
    :data:`_CLEAN_TOLERANCE_PX` on every node (nothing to rewrite)."""
    if len(before) != len(after):
        return False
    for (bx, by), (ax, ay) in zip(before, after):
        if bx is None or by is None:
            return False
        if abs(bx - ax) > _CLEAN_TOLERANCE_PX or abs(by - ay) > _CLEAN_TOLERANCE_PX:
            return False
    return True


# ---------------------------------------------------------------------------
# .drx tree mutation (composes formats.drx_xml — reads its order-preserving
# 'tree' dict and rewrites only a Body element's text; nothing else changes)
# ---------------------------------------------------------------------------
def _child_node(node: Dict[str, Any], tag: str) -> Optional[Dict[str, Any]]:
    for c in node.get("children") or []:
        if c.get("tag") == tag:
            return c
    return None


def _find_correction_elements(tree: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flat, document-order list of every ``ListMgt_LmVersion`` node dict in
    a parsed ``.drx`` tree (see :func:`drx_xml.parse_drx`'s ``tree`` key)."""
    out: List[Dict[str, Any]] = []

    def _walk(node: Dict[str, Any]) -> None:
        if node.get("tag") == "ListMgt_LmVersion":
            out.append(node)
        for c in node.get("children") or []:
            _walk(c)

    _walk(tree)
    return out


def _target_path(src_path: str, output_dir: str, overwrite: bool) -> str:
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        return os.path.join(output_dir, os.path.basename(src_path))
    if overwrite:
        return src_path
    raise ValueError(
        "relayout_node_graphs: writing requires 'output_dir' (write tidied "
        "copies alongside the originals) or 'overwrite': true (rewrite the "
        "source .drx files in place)"
    )


def _relayout_one_file(
    src_path: str,
    origin_x: int,
    origin_y: int,
    spacing_x: int,
    write: bool,
    output_dir: str,
    overwrite: bool,
) -> Dict[str, Any]:
    """Relayout every graded correction in one ``.drx`` file. Returns a
    per-file report dict; never raises (parse/relayout failures come back
    in the report's ``skipped``/``error`` fields)."""
    try:
        parsed = parse_drx(src_path)
    except DrxParseError as e:
        return {"path": src_path, "error": f"parse error: {e}", "skipped": True}

    elements = _find_correction_elements(parsed["tree"])
    corrections_meta = parsed["nodes"]
    if len(elements) != len(corrections_meta):
        return {
            "path": src_path,
            "error": (
                f"internal mismatch: {len(elements)} <ListMgt_LmVersion> tree "
                f"elements vs {len(corrections_meta)} parsed correction records"
            ),
            "skipped": True,
        }

    graded_versions = 0
    relaid = 0
    already_clean = 0
    changed_records: List[Dict[str, Any]] = []
    skipped_records: List[Dict[str, Any]] = []
    file_changed = False

    for element, meta in zip(elements, corrections_meta):
        if not meta.get("has_correction"):
            continue
        body_child = _child_node(element, "Body")
        body_desc = meta.get("body") or {}
        if body_child is None or not body_desc.get("present") or not body_desc.get("valid_hex"):
            continue

        graded_versions += 1
        body_hex = body_desc["hex"]
        try:
            result = _relayout_body_hex(body_hex, origin_x, origin_y, spacing_x)
        except _RelayoutError as e:
            skipped_records.append({"db_id": meta.get("db_id"), "reason": str(e)})
            continue

        if _positions_clean(result["before"], result["after"]):
            already_clean += 1
            continue

        relaid += 1
        file_changed = True
        changed_records.append({
            "db_id": meta.get("db_id"),
            "name": meta.get("name"),
            "node_count": result["node_count"],
            "before": result["before"],
            "after": result["after"],
        })
        if write:
            body_child["text"] = result["body_hex"]

    written_to = None
    if write and file_changed:
        new_xml = serialize_drx(parsed)
        target = _target_path(src_path, output_dir, overwrite)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(new_xml)
        written_to = target

    return {
        "path": src_path,
        "written_to": written_to,
        "graded_versions": graded_versions,
        "relaid_out" if write else "would_relayout": relaid,
        "already_clean": already_clean,
        "changed": changed_records,
        "skipped": skipped_records,
    }


def _do_relayout_node_graphs(
    drx_paths: Optional[List[str]],
    output_dir: str,
    overwrite: bool,
    dry_run: bool,
    origin_x: int,
    origin_y: int,
    spacing_x: int,
) -> str:
    if not drx_paths:
        raise ValueError(
            "relayout_node_graphs requires a non-empty 'drx_paths' list "
            "(the project's .drx grade files — this offline tool has no "
            "live Project.db to sweep)"
        )
    if not isinstance(drx_paths, list):
        raise ValueError("drx_paths must be a list of .drx file paths")

    write = not dry_run
    if write:
        # Validate the write target up-front so a bad call fails fast rather
        # than partway through a multi-file sweep.
        _target_path(drx_paths[0], output_dir, overwrite)

    files_report = [
        _relayout_one_file(p, origin_x, origin_y, spacing_x, write, output_dir, overwrite)
        for p in drx_paths
    ]

    total_graded = sum(f.get("graded_versions", 0) for f in files_report)
    total_relaid = sum(
        f.get("relaid_out", f.get("would_relayout", 0)) for f in files_report
    )
    total_clean = sum(f.get("already_clean", 0) for f in files_report)

    result = {
        "action": "relayout_node_graphs",
        "dry_run": dry_run,
        "layout": {"origin_x": origin_x, "origin_y": origin_y, "spacing_x": spacing_x},
        "files": len(drx_paths),
        "graded_versions": total_graded,
        "relaid_out" if write else "would_relayout": total_relaid,
        "already_clean": total_clean,
        "fields_blob_untouched": True,
        "results": files_report,
        "verified": False,
        "note": (
            "FieldsBlob is never read or written by this action — only each "
            "correction's Body node-position fields (F4/F5) change, so grade "
            "content and FieldsBlob bytes are preserved exactly."
            if write
            else "Dry run — nothing written. Re-run with dry_run:false plus "
            "output_dir (write tidied copies) or overwrite:true (rewrite in "
            "place)."
        ),
    }
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# 'index' action — populate the SQLite store (composes store.db)
# ---------------------------------------------------------------------------
def _do_index(
    db_path: str,
    project_id: str,
    project_name: str,
    resolve_ref: str,
    drx_paths: Optional[List[str]],
) -> str:
    if not db_path:
        raise ValueError("index requires 'db_path' (path to the local SQLite store)")
    if not project_id:
        raise ValueError("index requires 'project_id'")
    if drx_paths is not None and not isinstance(drx_paths, list):
        raise ValueError("drx_paths must be a list of .drx file paths")

    try:
        store = open_db(db_path)
    except StoreError as e:
        raise ValueError(f"could not open store at {db_path!r}: {e}") from e

    try:
        project = store.upsert_project(
            project_id=project_id,
            name=project_name or project_id,
            resolve_ref=resolve_ref or None,
        )

        clip_ids: List[str] = []
        grades_indexed = 0
        errors: List[Dict[str, Any]] = []

        for src_path in drx_paths or []:
            try:
                parsed = parse_drx(src_path)
            except DrxParseError as e:
                errors.append({"path": src_path, "reason": f"parse error: {e}"})
                continue

            for still in parsed["stills"]:
                still_key = still.get("db_id") or os.path.basename(src_path)
                clip_id = f"{project_id}:{still_key}"
                try:
                    clip = store.upsert_clip(
                        clip_id=clip_id,
                        project_id=project_id,
                        name=still.get("label") or os.path.basename(src_path),
                        source_path=src_path,
                        meta={
                            "width": still.get("width"),
                            "height": still.get("height"),
                            "bit_depth": still.get("bit_depth"),
                            "src_hint": still.get("src_hint"),
                        },
                    )
                except StoreError as e:
                    errors.append({"path": src_path, "reason": f"clip upsert failed: {e}"})
                    continue
                clip_ids.append(clip["clip_id"])

                for idx, correction in enumerate(still.get("corrections") or []):
                    body_desc = correction.get("body") or {}
                    body_hex = body_desc.get("hex") or ""
                    content_hash = (
                        hashlib.sha256(body_hex.encode("ascii")).hexdigest() if body_hex else None
                    )
                    grade_id = f"{clip_id}:{idx}:{correction.get('db_id') or 'grade'}"
                    try:
                        store.upsert_grade(
                            grade_id=grade_id,
                            clip_id=clip_id,
                            kind="drx",
                            payload={
                                "role": correction.get("role"),
                                "name": correction.get("name"),
                                "has_correction": correction.get("has_correction"),
                                "ver_type": correction.get("ver_type"),
                                "impl_version": correction.get("impl_version"),
                                "body_byte_length": body_desc.get("byte_length"),
                            },
                            content_hash=content_hash,
                            verified=False,
                        )
                    except StoreError as e:
                        errors.append({
                            "path": src_path,
                            "db_id": correction.get("db_id"),
                            "reason": f"grade upsert failed: {e}",
                        })
                        continue
                    grades_indexed += 1

        result = {
            "action": "index",
            "project": project,
            "clips_indexed": len(clip_ids),
            "clip_ids": clip_ids,
            "grades_indexed": grades_indexed,
            "errors": errors,
            "idempotent": True,
            "verified": False,
        }
        return json.dumps(result, indent=2)
    except StoreError as e:
        raise ValueError(f"store error: {e}") from e
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool entry point
# ---------------------------------------------------------------------------
@mcp.tool()
def project_db(
    action: str,
    drx_paths: Optional[List[str]] = None,
    output_dir: str = "",
    overwrite: bool = False,
    dry_run: bool = True,
    origin_x: int = _DEFAULT_ORIGIN_X,
    origin_y: int = _DEFAULT_ORIGIN_Y,
    spacing_x: int = _DEFAULT_SPACING_X,
    db_path: str = "",
    project_id: str = "",
    project_name: str = "",
    resolve_ref: str = "",
) -> str:
    """DB-backed project operations for the OFFLINE/advanced tool set. Never touches Resolve.

    Parameters:
    - action: one of
        - "relayout_node_graphs": tidy the color-page node-editor layout
          (x/y position only) of every graded correction across
          `drx_paths` (a required non-empty list of local `.drx` grade
          files — this offline tool has no live Project.db to sweep, so
          the caller supplies the set of files that make up a project's
          grades). For each `HasCorrection=true` record, decodes its
          `Body` blob (framed zstd protobuf), rewrites only the node
          x/y layout fields to an evenly spaced single row
          (`origin_x`/`origin_y`/`spacing_x`, defaulting to Resolve's own
          measured "Cleanup Node Graph" values: 290/428/495), and leaves
          every other byte of `Body` — and all of `FieldsBlob` — untouched.
          Records already within 2px of the clean layout are left alone
          and counted in `already_clean` rather than rewritten. With
          `dry_run` (default true) nothing is written; set `dry_run:false`
          plus either `output_dir` (write tidied copies there, same
          filenames) or `overwrite:true` (rewrite the source files in
          place) to actually write. Returns a per-file report
          (`graded_versions`, `relaid_out`/`would_relayout`,
          `already_clean`, `changed` with before/after positions per
          record, `skipped` records that were not a rewritable node
          graph) plus `"verified": false` — this only guarantees a
          structurally valid, round-trippable `.drx`, not pixel-identical
          rendering in a live Resolve node editor.
        - "index": populate the local SQLite store at `db_path`
          (:mod:`davinci_resolve_mcp.store.db`) with a project's clips
          (one per `.drx` still, from `drx_paths`) and grades (one per
          correction record). Requires `db_path` and `project_id`;
          `project_name` (defaults to `project_id`) and `resolve_ref` are
          optional project metadata. All rows are upserted on stable,
          deterministic ids derived from `project_id` + each still's
          `DbId` (or filename) + correction index/`DbId`, so re-running
          "index" over the same `drx_paths` is idempotent — it updates
          the same rows rather than duplicating them. Returns the
          project row plus `clips_indexed`/`grades_indexed` counts, any
          per-file `errors` (unreadable/malformed `.drx` files are
          reported here rather than failing the whole call), and
          `"verified": false`.
    - drx_paths: list of local `.drx` file paths — the project's grade
      exports. Required for both actions.
    - output_dir / overwrite / dry_run: "relayout_node_graphs" write
      controls (see above).
    - origin_x / origin_y / spacing_x: "relayout_node_graphs" clean-row
      layout tuning (pixel coordinates).
    - db_path / project_id / project_name / resolve_ref: "index" args.

    Any other action returns the list of valid actions instead of erroring.
    Malformed/unreadable input never raises — it comes back as an
    "Error: ..." string, per the offline tool-set convention.
    """
    action_lower = action.strip().lower() if isinstance(action, str) else ""

    try:
        if action_lower == "relayout_node_graphs":
            return _do_relayout_node_graphs(
                drx_paths, output_dir, overwrite, dry_run, origin_x, origin_y, spacing_x
            )

        if action_lower == "index":
            return _do_index(db_path, project_id, project_name, resolve_ref, drx_paths)

        return json.dumps(
            {
                "error": f"Unknown action '{action}'.",
                "valid_actions": list(_VALID_ACTIONS),
            },
            indent=2,
        )
    except Exception as e:  # noqa: BLE001 - offline tools never raise
        return f"Error: {e}"
