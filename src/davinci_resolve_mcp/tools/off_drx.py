"""
davinci_resolve_mcp.tools.off_drx — offline ``.drx`` (per-clip grade)
inspector / codec / grading-catalog front door (OFFLINE/advanced tool set).

Registers exactly one action-dispatch tool, ``drx``, with eight actions:

  * ``"inspect"``       — parse a ``.drx`` file's XML envelope and decode
    every correction record's grade blob into named parameters.
  * ``"decode_params"`` — decode just one node's grade blob (from a file +
    node index, or a raw hex/bytes blob) into named parameters.
  * ``"export_cdl"``    — convert a node's (or a caller-supplied) decoded
    grade parameters into ASC CDL v1.2 XML (a ``.cc``/``.cdl``/``.ccc``).
  * ``"import_cdl"``    — parse ASC CDL XML into a CDL param dict
    (slope/offset/power/sat).
  * ``"attach_lut"``    — parse a ``.cube``/``.3dl`` LUT header and build a
    named-LUT attach-reference record (path + hash) for a ``.drx`` node.
  * ``"apply_op"``      — run one of the offline grading-catalog ops
    (contrast_normalize / saturation_match / black_balance) over CDL param
    dicts.
  * ``"verify_grade"``  — compare an intended grade parameter set against a
    decoded one and report a per-param diff + overall pass/fail.
  * ``"write"``         — patch named parameter values into a ``.drx``
    node's grade blob and write the result to a new file.

This is a thin dispatch/IO layer over
:mod:`davinci_resolve_mcp.formats.drx_xml`,
:mod:`davinci_resolve_mcp.formats.drx_codec`,
:mod:`davinci_resolve_mcp.formats.cdl`, :mod:`davinci_resolve_mcp.formats.lut`,
:mod:`davinci_resolve_mcp.grading.cdl_ops` and
:mod:`davinci_resolve_mcp.grading.qc`. It does not reimplement XML/zstd/protobuf parsing, CDL
XML, LUT header parsing, or grade-math itself — it only resolves file
paths/JSON payloads, calls into the formats/grading layers, and shapes JSON
responses.

This module NEVER connects to DaVinci Resolve: no ``DaVinciResolveScript``
import, no live session, nothing at import time beyond registering the
tool. It only reads/writes plain files on disk (``.drx`` XML, ``.cc``/
``.cdl``/``.ccc`` XML, ``.cube``/``.3dl`` LUTs) and JSON strings. Every
action returns a JSON string; nothing here raises — the top-level ``drx``
dispatcher catches every exception and returns an ``"Error: ..."`` string
instead. Actions that write a file, or synthesize a grade-affecting value
that has not been calibrated against a live Resolve instance
(``attach_lut``, ``apply_op``, ``write``), always carry a top-level
``"verified": false`` field.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..app import mcp
from ..formats import cdl as cdl_fmt
from ..formats import drx_codec
from ..formats import drx_xml
from ..formats import lut as lut_fmt
from ..grading import cdl_ops
from ..grading import qc as qc_mod

_VALID_ACTIONS = (
    "inspect",
    "decode_params",
    "export_cdl",
    "import_cdl",
    "attach_lut",
    "apply_op",
    "verify_grade",
    "write",
)

_VALID_OPS = ("contrast_normalize", "saturation_match", "black_balance")


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------
def _require_str(value: Optional[str], label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"drx: '{label}' is required")
    return value


def _load_json_dict(raw: Optional[str], label: str) -> Dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        raise ValueError(f"drx: '{label}' is required (JSON object string)")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"drx: '{label}' is not valid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise ValueError(f"drx: '{label}' must decode to a JSON object")
    return parsed


def _correction_nodes(parsed_drx: Dict[str, Any]) -> List[Dict[str, Any]]:
    return parsed_drx.get("nodes") or []


def _effective_index(node_index: int) -> int:
    """Map the tool's node_index sentinel (-1 == "unset") onto a real index.

    -1 (the tool default) resolves to node 0 for actions that operate on a
    single node; a caller-supplied value (including 0) is used as-is.
    """
    return 0 if node_index is None or node_index < 0 else node_index


def _node_blob_hex(node: Dict[str, Any]) -> Optional[str]:
    """Return the hex payload of a correction record's grade blob.

    Prefers ``Body`` (the common location); falls back to ``FieldsBlob``.
    Returns ``None`` when the node carries no blob at all (e.g. an empty
    correction record).
    """
    for key in ("body", "fields_blob"):
        blob = node.get(key)
        if isinstance(blob, dict) and blob.get("present") and blob.get("hex"):
            return blob["hex"]
    return None


def _decode_node(path: str, node_index: int) -> Dict[str, Any]:
    parsed = drx_xml.parse_drx(path)
    nodes = _correction_nodes(parsed)
    if not nodes:
        raise ValueError(f"drx: '{path}' contains no correction (LmVersion) records")
    if node_index < 0 or node_index >= len(nodes):
        raise ValueError(
            f"drx: node_index {node_index} out of range (0..{len(nodes) - 1}, found {len(nodes)} nodes)"
        )
    node = nodes[node_index]
    blob_hex = _node_blob_hex(node)
    if blob_hex is None:
        raise ValueError(f"drx: node {node_index} in '{path}' carries no Body/FieldsBlob grade payload")
    return drx_codec.decode_fields(blob_hex)


# ---------------------------------------------------------------------------
# action: inspect
# ---------------------------------------------------------------------------
def _do_inspect(path: str, node_index: Optional[int]) -> Dict[str, Any]:
    resolved = _require_str(path, "path")
    parsed = drx_xml.parse_drx(resolved)
    nodes = _correction_nodes(parsed)

    decoded_nodes: List[Dict[str, Any]] = []
    indices = range(len(nodes)) if node_index is None else [node_index]
    for i in indices:
        if i < 0 or i >= len(nodes):
            raise ValueError(
                f"drx: node_index {i} out of range (0..{len(nodes) - 1}, found {len(nodes)} nodes)"
            )
        node = nodes[i]
        blob_hex = _node_blob_hex(node)
        decoded = drx_codec.decode_fields(blob_hex) if blob_hex is not None else None
        decoded_nodes.append({
            "index": i,
            "db_id": node.get("db_id"),
            "role": node.get("role"),
            "name": node.get("name"),
            "has_correction": node.get("has_correction"),
            "params": (decoded or {}).get("params", []),
            "named": (decoded or {}).get("named", {}),
            "count": (decoded or {}).get("count", 0),
            "unparsed": (decoded or {}).get("unparsed", ""),
            "decode_error": (decoded or {}).get("error"),
            "has_blob": blob_hex is not None,
        })

    return {
        "action": "inspect",
        "path": resolved,
        "version": parsed.get("version"),
        "still_count": len(parsed.get("stills") or []),
        "node_count": len(nodes),
        "nodes": decoded_nodes,
    }


# ---------------------------------------------------------------------------
# action: decode_params
# ---------------------------------------------------------------------------
def _do_decode_params(path: Optional[str], node_index: int, blob_hex: Optional[str]) -> Dict[str, Any]:
    if blob_hex and blob_hex.strip():
        decoded = drx_codec.decode_fields(blob_hex.strip())
        source: Dict[str, Any] = {"source": "blob_hex"}
    else:
        resolved = _require_str(path, "path")
        decoded = _decode_node(resolved, node_index)
        source = {"source": "path", "path": resolved, "node_index": node_index}

    return {
        "action": "decode_params",
        **source,
        "params": decoded.get("params", []),
        "named": decoded.get("named", {}),
        "count": decoded.get("count", 0),
        "framing": decoded.get("framing"),
        "compressed": decoded.get("compressed"),
        "body_len": decoded.get("body_len"),
        "unparsed": decoded.get("unparsed", ""),
        "error": decoded.get("error"),
    }


# ---------------------------------------------------------------------------
# action: export_cdl
# ---------------------------------------------------------------------------
def _named_to_cdl(named: Dict[str, float]) -> Dict[str, Any]:
    """Map decoded DRX primary-corrector params onto an ASC CDL param dict.

    This is a *model conversion*, not a lossless readout: the CDL SOP/Sat
    model has no direct DRX equivalent for lift/contrast/pivot/etc., so only
    the primary gain/offset/gamma/saturation controls are carried across
    (matching the reference tool's ``drxToCDL``/``generateCDL`` intent).
    Missing controls default to the ASC CDL neutral values.
    """
    return {
        "slope": [
            named.get("gain.r", 1.0),
            named.get("gain.g", 1.0),
            named.get("gain.b", 1.0),
        ],
        "offset": [
            named.get("offset.r", 0.0),
            named.get("offset.g", 0.0),
            named.get("offset.b", 0.0),
        ],
        "power": [
            named.get("gamma.r", 1.0),
            named.get("gamma.g", 1.0),
            named.get("gamma.b", 1.0),
        ],
        "sat": named.get("saturation", 1.0),
    }


def _do_export_cdl(
    path: Optional[str],
    node_index: int,
    params_json: Optional[str],
    output_path: Optional[str],
    cdl_id: str,
    description: Optional[str],
    doc_type: str,
) -> Dict[str, Any]:
    if params_json and params_json.strip():
        named = _load_json_dict(params_json, "params_json")
        source: Dict[str, Any] = {"source": "params_json"}
    else:
        resolved = _require_str(path, "path")
        decoded = _decode_node(resolved, node_index)
        named = decoded.get("named", {})
        source = {"source": "path", "path": resolved, "node_index": node_index}

    cdl_params = _named_to_cdl(named)
    xml_text = cdl_fmt.export_cdl(
        cdl_params,
        id=cdl_id or "ColorCorrection001",
        description=description or None,
        doc_type=doc_type or "cdl",
    )

    result: Dict[str, Any] = {
        "action": "export_cdl",
        **source,
        "doc_type": doc_type or "cdl",
        "cdl_params": cdl_params,
        "cdl_note": (
            "ASC CDL is a model conversion of the primary corrector "
            "(slope=gain, offset=offset, power=gamma, sat=saturation); it is "
            "approximate by design, not a 1:1 readout of the full grade."
        ),
    }
    if output_path and output_path.strip():
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(xml_text)
        result["output_path"] = output_path
        result["bytes"] = len(xml_text.encode("utf-8"))
    else:
        result["cdl_xml"] = xml_text
    return result


# ---------------------------------------------------------------------------
# action: import_cdl
# ---------------------------------------------------------------------------
def _do_import_cdl(cdl_path: Optional[str], cdl_xml: Optional[str], cc_id: Optional[str]) -> Dict[str, Any]:
    if cdl_xml and cdl_xml.strip():
        xml_text = cdl_xml
        source: Dict[str, Any] = {"source": "cdl_xml"}
    else:
        resolved = _require_str(cdl_path, "cdl_path")
        with open(resolved, "r", encoding="utf-8") as fh:
            xml_text = fh.read()
        source = {"source": "cdl_path", "cdl_path": resolved}

    parsed = cdl_fmt.import_cdl(xml_text, id=(cc_id or None))
    return {"action": "import_cdl", **source, "cdl_params": parsed}


# ---------------------------------------------------------------------------
# action: attach_lut
# ---------------------------------------------------------------------------
def _do_attach_lut(
    lut_path: str,
    drx_path: Optional[str],
    node_index: int,
    output_path: Optional[str],
) -> Dict[str, Any]:
    resolved_lut = _require_str(lut_path, "lut_path")
    header = lut_fmt.parse_lut(resolved_lut)
    ref = lut_fmt.attach_ref(resolved_lut)

    result: Dict[str, Any] = {
        "action": "attach_lut",
        "lut_path": resolved_lut,
        "lut_header": header,
        "lut_ref": ref,
        "node_index": node_index,
        "note": (
            "structural named-LUT reference only (path + sha256 + header) — this "
            "codec does not patch the byte-level LUT-slot parameter into the .drx "
            "grade blob, so attaching still requires a live Resolve apply/import"
        ),
        "verified": False,
    }
    if drx_path and drx_path.strip():
        parsed = drx_xml.parse_drx(drx_path)
        nodes = _correction_nodes(parsed)
        if node_index < 0 or node_index >= len(nodes):
            raise ValueError(
                f"drx: node_index {node_index} out of range (0..{len(nodes) - 1}, found {len(nodes)} nodes)"
            )
        result["drx_path"] = drx_path
        result["node_db_id"] = nodes[node_index].get("db_id")

    if output_path and output_path.strip():
        manifest = {k: v for k, v in result.items() if k != "note"}
        manifest["note"] = result["note"]
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
        result["output_path"] = output_path

    return result


# ---------------------------------------------------------------------------
# action: apply_op
# ---------------------------------------------------------------------------
def _do_apply_op(op: str, src_json: str, ref_json: Optional[str], kwargs_json: Optional[str]) -> Dict[str, Any]:
    if op not in _VALID_OPS:
        return {"error": f"unknown op '{op}'", "valid_ops": list(_VALID_OPS)}

    src = _load_json_dict(src_json, "src_json")
    extra: Dict[str, Any] = {}
    if kwargs_json and kwargs_json.strip():
        extra = _load_json_dict(kwargs_json, "kwargs_json")

    if op == "black_balance":
        result = cdl_ops.black_balance(src, **_clamp_kwargs(extra, ("cast_threshold", "clamp_offset")))
    else:
        ref = _load_json_dict(ref_json, "ref_json")
        if op == "contrast_normalize":
            result = cdl_ops.contrast_normalize(src, ref, **_clamp_kwargs(extra, ("clamp_gain", "clamp_offset")))
        else:  # saturation_match
            result = cdl_ops.saturation_match(src, ref, **_clamp_kwargs(extra, ("clamp_scale",)))

    return {"action": "apply_op", "op": op, **result}


def _clamp_kwargs(extra: Dict[str, Any], allowed: tuple) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in allowed:
        if key not in extra:
            continue
        value = extra[key]
        out[key] = tuple(value) if isinstance(value, list) else value
    return out


# ---------------------------------------------------------------------------
# action: verify_grade
# ---------------------------------------------------------------------------
def _resolve_param_set(
    label: str,
    path: Optional[str],
    node_index: int,
    params_json: Optional[str],
) -> Any:
    if params_json and params_json.strip():
        return _load_json_dict(params_json, f"{label}_json")
    resolved = _require_str(path, f"{label}_path")
    return _decode_node(resolved, node_index)


def _do_verify_grade(
    intended_path: Optional[str],
    intended_node_index: int,
    intended_json: Optional[str],
    decoded_path: Optional[str],
    decoded_node_index: int,
    decoded_json: Optional[str],
    tolerance: float,
) -> Dict[str, Any]:
    intended = _resolve_param_set("intended", intended_path, intended_node_index, intended_json)
    decoded = _resolve_param_set("decoded", decoded_path, decoded_node_index, decoded_json)
    result = qc_mod.verify_grade(intended, decoded, tolerance=tolerance)
    return {"action": "verify_grade", **result}


# ---------------------------------------------------------------------------
# action: write
# ---------------------------------------------------------------------------
def _walk_tree_for_tag(node: Dict[str, Any], tag: str, out: List[Dict[str, Any]]) -> None:
    if node.get("tag") == tag:
        out.append(node)
    for child in node.get("children") or []:
        _walk_tree_for_tag(child, tag, out)


def _find_blob_child(correction_tree_node: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    children = correction_tree_node.get("children") or []
    for child in children:
        if child.get("tag") == "Body":
            return child
    for child in children:
        if child.get("tag") == "FieldsBlob":
            return child
    return None


def _apply_overrides(decoded: Dict[str, Any], overrides: Dict[str, Any]) -> tuple:
    changed = 0
    unmatched: List[str] = []
    params = decoded.get("params") or []
    for key, raw_value in overrides.items():
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            unmatched.append(key)
            continue

        target = None
        for p in params:
            if p.get("name") == key:
                target = p
                break
        if target is None:
            try:
                pid = int(str(key), 0)
            except (TypeError, ValueError):
                pid = None
            if pid is not None:
                for p in params:
                    if p.get("id") == pid:
                        target = p
                        break

        if target is None:
            unmatched.append(key)
            continue
        target["value"] = value
        changed += 1

    return changed, unmatched


def _do_write(
    path: str,
    node_index: int,
    overrides_json: str,
    output_path: str,
) -> Dict[str, Any]:
    resolved = _require_str(path, "path")
    resolved_out = _require_str(output_path, "output_path")
    overrides = _load_json_dict(overrides_json, "overrides_json")
    if not overrides:
        raise ValueError("drx: 'overrides_json' must contain at least one {name_or_id: value} entry")

    parsed = drx_xml.parse_drx(resolved)
    stills = parsed.get("stills") or []
    if len(stills) != 1:
        raise ValueError(
            f"drx: 'write' only supports single-still .drx grade files (found {len(stills)} stills in '{resolved}')"
        )

    nodes = _correction_nodes(parsed)
    if node_index < 0 or node_index >= len(nodes):
        raise ValueError(
            f"drx: node_index {node_index} out of range (0..{len(nodes) - 1}, found {len(nodes)} nodes)"
        )
    blob_hex = _node_blob_hex(nodes[node_index])
    if blob_hex is None:
        raise ValueError(f"drx: node {node_index} in '{resolved}' carries no Body/FieldsBlob grade payload")

    decoded = drx_codec.decode_fields(blob_hex)
    changed, unmatched = _apply_overrides(decoded, overrides)

    new_hex = drx_codec.encode_fields(decoded)
    if isinstance(new_hex, bytes):
        new_hex = new_hex.hex()

    correction_tree_nodes: List[Dict[str, Any]] = []
    _walk_tree_for_tag(parsed["tree"], "ListMgt_LmVersion", correction_tree_nodes)
    if len(correction_tree_nodes) != len(nodes):
        raise ValueError(
            "drx: internal node-count mismatch between parsed correction records "
            f"({len(nodes)}) and the XML tree ({len(correction_tree_nodes)}) — refusing to write"
        )
    blob_child = _find_blob_child(correction_tree_nodes[node_index])
    if blob_child is None:
        raise ValueError(f"drx: node {node_index} has no Body/FieldsBlob element to patch")
    blob_child["text"] = new_hex

    xml_text = drx_xml.serialize_drx(parsed)
    with open(resolved_out, "w", encoding="utf-8") as fh:
        fh.write(xml_text)

    # Offline self-check: re-parse + re-decode what was just written.
    reparsed = drx_xml.parse_drx(resolved_out)
    recheck_nodes = _correction_nodes(reparsed)
    recheck_hex = _node_blob_hex(recheck_nodes[node_index]) if node_index < len(recheck_nodes) else None
    redecoded = drx_codec.decode_fields(recheck_hex) if recheck_hex else {"named": {}, "count": 0}

    return {
        "action": "write",
        "path": resolved,
        "output_path": resolved_out,
        "node_index": node_index,
        "params_changed": changed,
        "unmatched_overrides": unmatched,
        "written_param_count": redecoded.get("count", 0),
        "written_named": redecoded.get("named", {}),
        "note": (
            "value patch is well-formed (re-parses and re-decodes) but its numeric "
            "UI calibration is structural-only, not verified against a live Resolve "
            "instance"
        ),
        "verified": False,
    }


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
@mcp.tool()
def drx(
    action: str,
    path: str = "",
    node_index: int = -1,
    blob_hex: str = "",
    params_json: str = "",
    output_path: str = "",
    cdl_id: str = "",
    description: str = "",
    doc_type: str = "cdl",
    cdl_path: str = "",
    cdl_xml: str = "",
    lut_path: str = "",
    drx_path: str = "",
    op: str = "",
    src_json: str = "",
    ref_json: str = "",
    kwargs_json: str = "",
    intended_path: str = "",
    intended_node_index: int = 0,
    intended_json: str = "",
    decoded_path: str = "",
    decoded_node_index: int = 0,
    decoded_json: str = "",
    tolerance: float = 1e-3,
    overrides_json: str = "",
) -> str:
    """Offline ``.drx`` (per-clip grade) inspector / codec / grading-catalog front door. Never touches Resolve.

    Parameters:
    - action: one of
        - "inspect": read `path` (a ``.drx`` file on disk) and return every
          correction record's decoded grade parameters. Set `node_index` to
          limit to one node (0-based); omit to decode all nodes.
        - "decode_params": decode a single node's grade blob into named
          parameters — either from `path` + `node_index`, or directly from a
          hex/`blob_hex` string (no file needed).
        - "export_cdl": convert a node's decoded primary-corrector params
          (from `path` + `node_index`, or a `params_json` `{name: value}`
          object) into ASC CDL v1.2 XML. `doc_type` is one of
          "cdl"/"cc"/"ccc" (default "cdl"); `cdl_id`/`description` are
          carried into the `<ColorCorrection>`. Writes to `output_path` if
          given, else returns the XML in `cdl_xml`.
        - "import_cdl": parse ASC CDL XML — either `cdl_path` (a
          ``.cc``/``.cdl``/``.ccc`` file) or raw `cdl_xml` text — into a CDL
          param dict (`slope`/`offset`/`power`/`sat`). `cdl_id` selects a
          specific `<ColorCorrection id=...>` in a multi-correction document.
        - "attach_lut": parse `lut_path` (a ``.cube``/``.3dl`` file) and
          build a named-LUT attach-reference record (header + sha256 +
          size). Optionally pass `drx_path` + `node_index` to record which
          node the reference targets, and `output_path` to write the
          reference as a JSON manifest. Structural reference only — this
          codec does not patch the LUT-slot bytes into the grade blob.
        - "apply_op": run an offline grading-catalog op from
          `davinci_resolve_mcp.grading.cdl_ops` — `op` is one of
          "contrast_normalize", "saturation_match", "black_balance" — over
          `src_json` (a CDL param dict `{slope,offset,power,sat}`, JSON).
          "contrast_normalize"/"saturation_match" also require `ref_json`
          (the reference CDL param dict to match toward). `kwargs_json` may
          supply the op's optional clamp bounds
          (e.g. `{"clamp_gain": [0.5, 2.0]}`).
        - "verify_grade": compare an intended grade parameter set against a
          decoded one. Each side is given either as `{intended,decoded}_path`
          + `{intended,decoded}_node_index` (decoded from a ``.drx`` file) or
          as `{intended,decoded}_json` (a `{name: value}` object). `tolerance`
          is the max allowed absolute per-param difference (default 1e-3).
        - "write": patch parameter values into `path`'s node at `node_index`
          and write the result to `output_path`. `overrides_json` is a JSON
          object mapping a parameter name (e.g. "gain.r") or numeric/hex id
          (e.g. "100663325" or "0x6000019") to its new float value. Only
          supports single-still ``.drx`` grade files.
      Any other action returns the list of valid actions instead of
      erroring.
    - path: source ``.drx`` file path (for "inspect"/"decode_params"/
      "export_cdl"/"write").
    - node_index: 0-based correction-record index within `path` (default -1
      == unset; "inspect" decodes every node when unset, other actions fall
      back to node 0 when unset).
    - blob_hex: a raw grade-blob hex string (alternative to `path` for
      "decode_params").
    - params_json: JSON `{name: value}` object (alternative to `path` for
      "export_cdl").
    - output_path: destination file path (for "export_cdl", "attach_lut",
      "write").
    - cdl_id / description / doc_type: CDL export options (for "export_cdl").
    - cdl_path / cdl_xml: CDL source (for "import_cdl"; give one).
    - lut_path: ``.cube``/``.3dl`` file path (for "attach_lut").
    - drx_path: optional ``.drx`` context path (for "attach_lut").
    - op / src_json / ref_json / kwargs_json: grading-catalog op selection
      and inputs (for "apply_op").
    - intended_path / intended_node_index / intended_json,
      decoded_path / decoded_node_index / decoded_json / tolerance: grade
      comparison inputs (for "verify_grade").
    - overrides_json: `{name_or_id: value}` parameter patch (for "write").

    A missing/unreadable file, malformed JSON, an out-of-range `node_index`,
    or any other structural problem always comes back as an "Error: ..."
    string (never a raised exception), per the offline tool-set convention.
    "attach_lut", "apply_op", and "write" always carry a top-level
    "verified": false field — they are structural-only until calibrated
    against a live DaVinci Resolve instance.
    """
    action_lower = action.strip().lower() if isinstance(action, str) else ""
    try:
        if action_lower == "inspect":
            idx = None if node_index is None or node_index < 0 else node_index
            return json.dumps(_do_inspect(path, idx), indent=2)
        if action_lower == "decode_params":
            return json.dumps(_do_decode_params(path, _effective_index(node_index), blob_hex), indent=2)
        if action_lower == "export_cdl":
            return json.dumps(
                _do_export_cdl(
                    path, _effective_index(node_index), params_json, output_path, cdl_id, description, doc_type
                ),
                indent=2,
            )
        if action_lower == "import_cdl":
            return json.dumps(_do_import_cdl(cdl_path, cdl_xml, cdl_id), indent=2)
        if action_lower == "attach_lut":
            return json.dumps(_do_attach_lut(lut_path, drx_path, _effective_index(node_index), output_path), indent=2)
        if action_lower == "apply_op":
            return json.dumps(_do_apply_op(op, src_json, ref_json, kwargs_json), indent=2)
        if action_lower == "verify_grade":
            return json.dumps(
                _do_verify_grade(
                    intended_path, intended_node_index, intended_json,
                    decoded_path, decoded_node_index, decoded_json,
                    tolerance,
                ),
                indent=2,
            )
        if action_lower == "write":
            return json.dumps(_do_write(path, _effective_index(node_index), overrides_json, output_path), indent=2)

        return json.dumps(
            {
                "error": f"Unknown action '{action}'.",
                "valid_actions": list(_VALID_ACTIONS),
            },
            indent=2,
        )
    except Exception as e:  # noqa: BLE001 - offline tools never raise
        return f"Error: {e}"
