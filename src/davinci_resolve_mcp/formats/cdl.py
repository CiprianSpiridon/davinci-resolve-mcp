"""
davinci_resolve_mcp.formats.cdl — ASC CDL v1.2 import/export.

Implements the ASC Color Decision List exchange format (``urn:ASC:CDL:v1.2``):
Slope / Offset / Power (SOP) plus Saturation (Sat). CDL is the industry-standard
way on-set colorists / DITs hand off a look — a ``.cc`` (single correction),
``.ccc`` (collection), or ``.cdl`` (decision list) file — to a downstream
grading system. This module is pure and offline: it only reads/writes XML
strings, never talks to DaVinci Resolve.

Internal grade param dict shape (the "CDL param dict" used across this
codebase to represent a single ASC CDL correction)::

    {
        "slope":  [r, g, b],   # per-channel multiplier, >= 0, unity = 1.0
        "offset": [r, g, b],   # per-channel additive term, unity = 0.0
        "power":  [r, g, b],   # per-channel exponent, > 0, unity = 1.0
        "sat":    float,       # saturation multiplier, >= 0, unity = 1.0
    }

Per the ASC CDL spec the SOP transform is applied per-channel as
``out = (in * slope + offset) ** power``, followed by a saturation matrix
applied to the clamped SOP result. ``slope``/``offset``/``power`` also accept
``{"r": .., "g": .., "b": ..}`` mappings on input for convenience; output
dicts from :func:`import_cdl` always use the 3-element list form.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any, Mapping, Sequence

CDL_NS = "urn:ASC:CDL:v1.2"
_CHANNEL_NAMES = ("R", "G", "B")

CDL_DEFAULTS: dict[str, Any] = {
    "slope": [1.0, 1.0, 1.0],
    "offset": [0.0, 0.0, 0.0],
    "power": [1.0, 1.0, 1.0],
    "sat": 1.0,
}

ET.register_namespace("", CDL_NS)


def _local(tag: str) -> str:
    """Strip an XML namespace (``{uri}Name`` -> ``Name``) for tolerant tag matching."""
    return tag.rsplit("}", 1)[-1]


def _is_finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


def _validate_channels(
    name: str,
    value: Any,
    *,
    min_value: float | None = None,
    exclusive_min: bool = False,
) -> list[float]:
    if value is None:
        raise ValueError(f"cdl: missing field '{name}'")
    if isinstance(value, Mapping):
        missing = [ch for ch in ("r", "g", "b") if ch not in value]
        if missing:
            raise ValueError(f"cdl: field '{name}' missing channel(s) {missing}")
        value = [value["r"], value["g"], value["b"]]
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"cdl: field '{name}' must be a 3-element [r,g,b] sequence, got {value!r}")
    values = list(value)
    if len(values) != 3:
        raise ValueError(f"cdl: field '{name}' must have exactly 3 channels (R,G,B), got {len(values)}")

    normalized: list[float] = []
    for channel_name, raw in zip(_CHANNEL_NAMES, values):
        try:
            channel_value = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"cdl: field '{name}.{channel_name}' is not numeric: {raw!r}") from None
        if not _is_finite(channel_value):
            raise ValueError(f"cdl: field '{name}.{channel_name}' must be finite, got {raw!r}")
        if min_value is not None:
            if exclusive_min and channel_value <= min_value:
                raise ValueError(f"cdl: field '{name}.{channel_name}'={channel_value} must be > {min_value}")
            if not exclusive_min and channel_value < min_value:
                raise ValueError(f"cdl: field '{name}.{channel_name}'={channel_value} must be >= {min_value}")
        normalized.append(channel_value)
    return normalized


def _validate_sat(value: Any) -> float:
    if value is None:
        raise ValueError("cdl: missing field 'sat'")
    try:
        sat = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"cdl: field 'sat' is not numeric: {value!r}") from None
    if not _is_finite(sat):
        raise ValueError(f"cdl: field 'sat' must be finite, got {value!r}")
    if sat < 0:
        raise ValueError(f"cdl: field 'sat'={sat} must be >= 0")
    return sat


def validate_cdl_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """
    Validate and normalize a CDL param dict ({slope, offset, power, sat}).

    Returns a fresh dict with each of slope/offset/power coerced to a
    3-element [r, g, b] float list and sat coerced to a float. Raises
    ValueError naming the offending field (e.g. "cdl: field 'power.B'=-0.5
    must be > 0") for missing channels, wrong-length channels, non-numeric
    values, non-finite values, or out-of-range values (slope/power must be
    >= 0 / > 0 respectively, sat must be >= 0).
    """
    if not isinstance(params, Mapping):
        raise ValueError(f"cdl: params must be a dict, got {type(params).__name__}")
    return {
        "slope": _validate_channels("slope", params.get("slope"), min_value=0.0),
        "offset": _validate_channels("offset", params.get("offset")),
        "power": _validate_channels("power", params.get("power"), min_value=0.0, exclusive_min=True),
        "sat": _validate_sat(params.get("sat")),
    }


def _fmt(value: float) -> str:
    return f"{value:.6f}"


def _build_color_correction(norm: Mapping[str, Any], *, cc_id: str, description: str | None) -> ET.Element:
    cc = ET.Element(f"{{{CDL_NS}}}ColorCorrection", {"id": cc_id})
    if description:
        desc_el = ET.SubElement(cc, f"{{{CDL_NS}}}Description")
        desc_el.text = description
    sop = ET.SubElement(cc, f"{{{CDL_NS}}}SOPNode")
    ET.SubElement(sop, f"{{{CDL_NS}}}Slope").text = " ".join(_fmt(v) for v in norm["slope"])
    ET.SubElement(sop, f"{{{CDL_NS}}}Offset").text = " ".join(_fmt(v) for v in norm["offset"])
    ET.SubElement(sop, f"{{{CDL_NS}}}Power").text = " ".join(_fmt(v) for v in norm["power"])
    sat_node = ET.SubElement(cc, f"{{{CDL_NS}}}SatNode")
    ET.SubElement(sat_node, f"{{{CDL_NS}}}Saturation").text = _fmt(norm["sat"])
    return cc


def _append_doc_descriptions(root: ET.Element, input_description: str | None, viewing_description: str | None) -> None:
    if input_description:
        ET.SubElement(root, f"{{{CDL_NS}}}InputDescription").text = input_description
    if viewing_description:
        ET.SubElement(root, f"{{{CDL_NS}}}ViewingDescription").text = viewing_description


def export_cdl(
    params: Mapping[str, Any],
    *,
    id: str = "ColorCorrection001",
    description: str | None = None,
    input_description: str | None = None,
    viewing_description: str | None = None,
    doc_type: str = "cdl",
) -> str:
    """
    Export a CDL param dict ({slope, offset, power, sat}) as ASC CDL v1.2 XML.

    Params:
      params: {"slope": [r,g,b], "offset": [r,g,b], "power": [r,g,b], "sat": float}
        (channels may also be given as {"r":..,"g":..,"b":..} mappings).
      id: id attribute placed on the <ColorCorrection> element.
      description: optional free-text <Description> child of the correction.
      input_description / viewing_description: optional document-level
        <InputDescription> / <ViewingDescription> elements (ignored for
        doc_type="cc", which has no document wrapper).
      doc_type: one of
        - "cdl" (default): a full <ColorDecisionList> wrapping one
          <ColorDecision><ColorCorrection> — a .cdl file.
        - "cc": a bare <ColorCorrection> element only — a .cc single-correction file.
        - "ccc": a <ColorCorrectionCollection> containing the one correction — a .ccc file.

    Returns the XML document as a string (UTF-8 declared, unicode returned).
    Raises ValueError naming the offending field if params are missing, the
    wrong shape, or out of range (see validate_cdl_params).
    """
    norm = validate_cdl_params(params)
    if doc_type not in ("cdl", "cc", "ccc"):
        raise ValueError(f"cdl: unknown doc_type {doc_type!r}, expected 'cdl', 'cc', or 'ccc'")

    cc = _build_color_correction(norm, cc_id=id, description=description)

    if doc_type == "cc":
        root = cc
    elif doc_type == "ccc":
        root = ET.Element(f"{{{CDL_NS}}}ColorCorrectionCollection")
        _append_doc_descriptions(root, input_description, viewing_description)
        root.append(cc)
    else:  # "cdl"
        root = ET.Element(f"{{{CDL_NS}}}ColorDecisionList")
        _append_doc_descriptions(root, input_description, viewing_description)
        decision = ET.SubElement(root, f"{{{CDL_NS}}}ColorDecision")
        decision.append(cc)

    ET.indent(root, space="  ")
    xml_body = ET.tostring(root, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{xml_body}\n'


def export_ccc(
    corrections: Sequence[Mapping[str, Any]],
    *,
    input_description: str | None = None,
    viewing_description: str | None = None,
) -> str:
    """
    Export multiple corrections as a single ASC CDL v1.2 <ColorCorrectionCollection> (.ccc).

    corrections: sequence of {"id": str, "description": str|None, "params": <CDL param dict>}.
    "id" defaults to "correction_NNN" (1-based) if omitted; "description" is optional.

    Returns the XML document as a string. Raises ValueError (naming the
    offending field, prefixed with the correction's id/index) if any
    correction's params are missing/out of range, or if `corrections` is empty.
    """
    if not corrections:
        raise ValueError("cdl: export_ccc requires at least one correction")

    root = ET.Element(f"{{{CDL_NS}}}ColorCorrectionCollection")
    _append_doc_descriptions(root, input_description, viewing_description)

    for index, entry in enumerate(corrections):
        if not isinstance(entry, Mapping):
            raise ValueError(f"cdl: corrections[{index}] must be a dict, got {type(entry).__name__}")
        cc_id = entry.get("id") or f"correction_{index + 1:03d}"
        try:
            norm = validate_cdl_params(entry.get("params") or {})
        except ValueError as e:
            raise ValueError(f"cdl: corrections[{index}] (id={cc_id!r}): {e}") from e
        root.append(_build_color_correction(norm, cc_id=cc_id, description=entry.get("description")))

    ET.indent(root, space="  ")
    xml_body = ET.tostring(root, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{xml_body}\n'


def _find_color_corrections(root: ET.Element) -> list[ET.Element]:
    if _local(root.tag) == "ColorCorrection":
        return [root]
    return [el for el in root.iter() if _local(el.tag) == "ColorCorrection"]


def _parse_sop_values(text: str, tag_name: str) -> list[float]:
    parts = (text or "").split()
    if len(parts) != 3:
        raise ValueError(f"cdl: <{tag_name}> must have exactly 3 values, got {text!r}")
    try:
        return [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"cdl: <{tag_name}> contains a non-numeric value: {text!r}") from None


def _parse_color_correction(cc: ET.Element) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "slope": list(CDL_DEFAULTS["slope"]),
        "offset": list(CDL_DEFAULTS["offset"]),
        "power": list(CDL_DEFAULTS["power"]),
        "sat": CDL_DEFAULTS["sat"],
    }
    description: str | None = None

    for child in cc:
        local = _local(child.tag)
        if local == "Description":
            text = (child.text or "").strip()
            description = text or None
        elif local == "SOPNode":
            for sop_child in child:
                sop_local = _local(sop_child.tag)
                if sop_local in ("Slope", "Offset", "Power"):
                    raw[sop_local.lower()] = _parse_sop_values(sop_child.text or "", sop_local)
        elif local == "SatNode":
            for sat_child in child:
                if _local(sat_child.tag) == "Saturation":
                    text = (sat_child.text or "").strip()
                    try:
                        raw["sat"] = float(text)
                    except ValueError:
                        raise ValueError(f"cdl: <Saturation> is not numeric: {text!r}") from None

    normalized = validate_cdl_params(raw)
    normalized["id"] = cc.get("id")
    if description:
        normalized["description"] = description
    return normalized


def import_cdl(xml_content: str, *, id: str | None = None) -> dict[str, Any]:
    """
    Import ASC CDL v1.2 XML into a CDL param dict.

    Accepts a bare <ColorCorrection>, a <ColorDecisionList> (one or more
    <ColorDecision><ColorCorrection>), or a <ColorCorrectionCollection> (one
    or more <ColorCorrection>) document.

    Params:
      xml_content: raw .cc/.cdl/.ccc XML text.
      id: if given, selects the <ColorCorrection id="..."> matching this id;
        otherwise the first <ColorCorrection> found in document order is used.

    Returns {"slope": [r,g,b], "offset": [r,g,b], "power": [r,g,b], "sat": float,
    "id": str|None, "description": str (if present)}.

    Raises ValueError if the XML is malformed, if no <ColorCorrection> is
    found (not a CDL/CC/CCC document), if `id` doesn't match any entry, or if
    a SOP/Sat value is malformed or out of range.
    """
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as e:
        raise ValueError(f"cdl: invalid XML: {e}") from e

    corrections = _find_color_corrections(root)
    if not corrections:
        raise ValueError("cdl: no <ColorCorrection> found - not a CDL/CC/CCC document")

    if id is not None:
        for cc in corrections:
            if cc.get("id") == id:
                return _parse_color_correction(cc)
        raise ValueError(f"cdl: no <ColorCorrection id={id!r}> found in document")

    return _parse_color_correction(corrections[0])


def import_cdl_all(xml_content: str) -> list[dict[str, Any]]:
    """
    Import every <ColorCorrection> in an ASC CDL document.

    Params:
      xml_content: raw .cc/.cdl/.ccc XML text.

    Returns a list of CDL param dicts (see import_cdl), one per
    <ColorCorrection> found, in document order. Raises ValueError under the
    same conditions as import_cdl (malformed XML, no corrections found, or a
    malformed/out-of-range SOP/Sat value).
    """
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as e:
        raise ValueError(f"cdl: invalid XML: {e}") from e

    corrections = _find_color_corrections(root)
    if not corrections:
        raise ValueError("cdl: no <ColorCorrection> found - not a CDL/CC/CCC document")

    return [_parse_color_correction(cc) for cc in corrections]
