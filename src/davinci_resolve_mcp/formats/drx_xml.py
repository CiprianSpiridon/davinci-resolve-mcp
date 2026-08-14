"""
davinci_resolve_mcp.formats.drx_xml — .drx XML envelope + node-tree parser.

A DaVinci Resolve ``.drx`` grade export is a small XML document whose root is a
``Gallery_GyStill`` element (a gallery still).  The still carries display
metadata (label, resolution, bit depth) plus one or more *correction records*
(``ListMgt_LmVersion`` elements, nested under ``pClipFullVer`` / ``pTrackVer``).
Each correction record owns a ``<Body>`` payload — a hex-encoded, single-byte
framed zstd stream that, once decoded, expands to the actual colour-node graph.

This module is deliberately narrow: it parses the XML **envelope** and the
node/correction *tree structure*, and it *locates* (but never decodes) the
``FieldsBlob`` / ``Body`` payloads.  Decoding the framed zstd + protobuf grade
body is the job of :mod:`davinci_resolve_mcp.formats.drx_codec`.

Public API
----------
``parse_drx(path) -> dict``
    Parse a ``.drx`` file into ``{"version", "stills", "nodes", "tree", ...}``.
``parse_drx_content(xml) -> dict``
    Same, from an in-memory XML string.
``serialize_drx(parsed) -> str``
    Reconstruct the XML document from a parsed dict, preserving structure and
    the exact ``FieldsBlob`` / ``Body`` blob bytes (round-trips ``parse_drx``).
``DrxParseError``
    Raised for unreadable files, malformed XML, or documents that are not a
    ``Gallery_GyStill`` envelope (never a bare ``ElementTree`` error).

This module never connects to DaVinci Resolve; it operates purely on
files/strings and is safe to import with no Resolve instance running.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

__all__ = [
    "DrxParseError",
    "parse_drx",
    "parse_drx_content",
    "serialize_drx",
]

# Framing byte that prefixes a .drx correction Body:
#   0x81 -> the remaining bytes are a zstd frame (magic 28 b5 2f fd)
#   0x80 -> STORED: the remaining bytes are the raw (uncompressed) protobuf
_FRAME_ZSTD = 0x81
_FRAME_STORED = 0x80

# Real DaVinci Resolve .drx files use C++-namespaced element names
# (``Gallery::GyStill``, ``ListMgt::LmVersion``). Python's xml.etree treats
# ``::`` as an (invalid) namespace separator and refuses to parse. Since ``::``
# only ever appears inside element tag names in these documents (never in
# attribute values, comments, or the hex/base64 blob payloads), we transform it
# to a reversible sentinel around ElementTree, and restore the real ``::`` on
# serialize — so documents stay byte-exact and written .drx re-import into Resolve.
_NS_TOKEN = "__DRXNS__"


class DrxParseError(Exception):
    """Raised when a ``.drx`` document cannot be parsed as a grade envelope."""


# ---------------------------------------------------------------------------
# Small local tag helpers (fixtures carry no XML namespaces, but stay defensive)
# ---------------------------------------------------------------------------
def _localname(tag: str) -> str:
    """Return the local part of a (possibly namespaced) element tag."""
    if isinstance(tag, str) and "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _is_still(tag: str) -> bool:
    return _localname(tag).endswith("GyStill")


def _is_correction(tag: str) -> bool:
    # e.g. ListMgt_LmVersion
    return _localname(tag).endswith("LmVersion")


def _text_or_none(el: Optional[ET.Element]) -> Optional[str]:
    if el is None:
        return None
    return el.text


def _child(parent: ET.Element, local: str) -> Optional[ET.Element]:
    for c in parent:
        if _localname(c.tag) == local:
            return c
    return None


def _child_text(parent: ET.Element, local: str) -> Optional[str]:
    return _text_or_none(_child(parent, local))


def _as_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return None


def _as_bool(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    v = value.strip().lower()
    if v in ("true", "1", "yes"):
        return True
    if v in ("false", "0", "no"):
        return False
    return None


# ---------------------------------------------------------------------------
# Generic, order-preserving XML <-> dict tree (used for lossless round-trip)
# ---------------------------------------------------------------------------
def _elem_to_node(el: ET.Element) -> Dict[str, Any]:
    """Convert an ElementTree element into a JSON-friendly, ordered node dict.

    ``text`` (content before the first child) and ``tail`` (content after this
    element's close tag) are preserved verbatim so whitespace layout and blob
    payloads survive a serialize/parse round-trip.
    """
    return {
        "tag": _localname(el.tag),
        "attrib": dict(el.attrib),
        "text": el.text,
        "tail": el.tail,
        "children": [_elem_to_node(c) for c in list(el)],
    }


def _escape_text(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _escape_attr(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _node_to_xml(node: Dict[str, Any]) -> str:
    """Serialize a node dict (from :func:`_elem_to_node`) back to XML text.

    Empty, childless, text-less elements render self-closing (``<Tag/>``) to
    match Resolve's own serialization.  The element's ``tail`` is appended after
    the close tag, mirroring ElementTree semantics.
    """
    tag = node["tag"]
    attrib = node.get("attrib") or {}
    attr_str = "".join(f' {k}="{_escape_attr(str(v))}"' for k, v in attrib.items())

    text = node.get("text")
    children = node.get("children") or []

    inner = ""
    if text:
        inner += _escape_text(text)
    for child in children:
        inner += _node_to_xml(child)

    if not inner:
        out = f"<{tag}{attr_str}/>"
    else:
        out = f"<{tag}{attr_str}>{inner}</{tag}>"

    tail = node.get("tail")
    if tail:
        out += _escape_text(tail)
    return out


# ---------------------------------------------------------------------------
# Body / FieldsBlob payload location (NO decoding here)
# ---------------------------------------------------------------------------
def _describe_blob(hex_body: Optional[str]) -> Optional[Dict[str, Any]]:
    """Locate a framed blob payload without decoding it.

    Returns a descriptor exposing the raw hex, the leading framing byte, and the
    post-framing payload hex for :mod:`drx_codec` to (de)compress.  Returns
    ``None`` when there is no blob content.
    """
    if hex_body is None:
        return None
    cleaned = "".join(hex_body.split())
    if cleaned == "":
        return None

    frame_byte: Optional[int] = None
    payload_hex = ""
    valid_hex = True
    try:
        raw = bytes.fromhex(cleaned)
    except ValueError:
        raw = b""
        valid_hex = False

    if valid_hex and len(raw) >= 1:
        frame_byte = raw[0]
        payload_hex = cleaned[2:]

    return {
        "present": True,
        "hex": cleaned,
        "byte_length": len(raw) if valid_hex else None,
        "valid_hex": valid_hex,
        "framing_byte": frame_byte,
        "compressed": frame_byte == _FRAME_ZSTD,
        "stored": frame_byte == _FRAME_STORED,
        "payload_hex": payload_hex,
    }


# ---------------------------------------------------------------------------
# Semantic extraction
# ---------------------------------------------------------------------------
def _extract_correction(el: ET.Element, container_tag: Optional[str],
                        still_db_id: Optional[str]) -> Dict[str, Any]:
    """Build a per-node correction record from a ``ListMgt_LmVersion`` element."""
    container = _localname(container_tag) if container_tag else None
    if container == "pClipFullVer":
        role = "clip"
    elif container == "pTrackVer":
        role = "track"
    elif container:
        role = container
    else:
        role = "unknown"

    return {
        "db_id": el.attrib.get("DbId"),
        "still_db_id": still_db_id,
        "role": role,
        "container": container,
        "name": _child_text(el, "Name") or "",
        "has_correction": _as_bool(_child_text(el, "HasCorrection")),
        "ver_type": _as_int(_child_text(el, "VerType")),
        "impl_version": _as_int(_child_text(el, "ImplVersion")),
        "fields_blob": _describe_blob(_child_text(el, "FieldsBlob")),
        "body": _describe_blob(_child_text(el, "Body")),
    }


def _extract_still(still: ET.Element) -> Dict[str, Any]:
    """Build a still record (metadata + its correction records) from a
    ``Gallery_GyStill`` element."""
    db_id = still.attrib.get("DbId")

    # Parent lookup for role assignment of nested correction records.
    parent_map = {child: parent for parent in still.iter() for child in list(parent)}

    corrections: List[Dict[str, Any]] = []
    for el in still.iter():
        if el is still:
            continue
        if _is_correction(el.tag):
            parent = parent_map.get(el)
            container_tag = parent.tag if parent is not None else None
            corrections.append(_extract_correction(el, container_tag, db_id))

    # Collect scalar metadata fields (direct children with text, no sub-elements),
    # excluding FieldsBlob which is surfaced separately as a blob descriptor.
    fields: Dict[str, Any] = {}
    for c in still:
        local = _localname(c.tag)
        if local in ("FieldsBlob", "pClipFullVer", "pTrackVer"):
            continue
        if len(list(c)) == 0 and c.text is not None and c.text.strip() != "":
            fields[local] = c.text

    return {
        "db_id": db_id,
        "label": _child_text(still, "Label") or "",
        "width": _as_int(_child_text(still, "Width")),
        "height": _as_int(_child_text(still, "Height")),
        "bit_depth": _as_int(_child_text(still, "BitDepth")),
        "src_type": _as_int(_child_text(still, "SrcType")),
        "src_hint": _child_text(still, "SrcHint"),
        "fields_blob": _describe_blob(_child_text(still, "FieldsBlob")),
        "fields": fields,
        "corrections": corrections,
    }


def _split_prolog_epilog(xml: str, root_tag: str) -> tuple[str, str]:
    """Return (prolog, epilog): the exact text before the root's open tag and
    after its close tag, so declaration + comment + trailing whitespace survive
    the round-trip verbatim."""
    open_marker = "<" + root_tag
    start = xml.find(open_marker)
    prolog = xml[:start] if start != -1 else ""

    close_marker = "</" + root_tag + ">"
    idx = xml.rfind(close_marker)
    epilog = xml[idx + len(close_marker):] if idx != -1 else ""
    return prolog, epilog


def _parse_prolog_metadata(prolog: str) -> Dict[str, Any]:
    """Pull the XML declaration and the DbAppVer/DbPrjVer comment out of the
    document prolog for a convenience ``version`` view."""
    meta: Dict[str, Any] = {
        "xml_declaration": None,
        "db_app_ver": None,
        "db_prj_ver": None,
    }
    for line in prolog.splitlines():
        s = line.strip()
        if s.startswith("<?xml"):
            meta["xml_declaration"] = s
        elif s.startswith("<!--"):
            for key, attr in (("DbAppVer", "db_app_ver"), ("DbPrjVer", "db_prj_ver")):
                token = key + '="'
                pos = s.find(token)
                if pos != -1:
                    end = s.find('"', pos + len(token))
                    if end != -1:
                        meta[attr] = s[pos + len(token):end]
    return meta


def parse_drx_content(xml: str) -> Dict[str, Any]:
    """Parse ``.drx`` XML content into a structured dict.

    Returns a dict with keys:
        ``version``  — document version metadata (declaration, DbAppVer,
                       DbPrjVer, plus the correction-record / LmVersion db-ids).
        ``stills``   — list of still records (``Gallery_GyStill``), each with
                       metadata and its ``corrections`` (LmVersion records).
        ``nodes``    — flat list of every correction record (``ListMgt_LmVersion``)
                       across all stills, with located ``FieldsBlob`` / ``Body``
                       blob payloads (decoding deferred to ``drx_codec``).
        ``tree``     — an order-preserving generic representation of the root
                       element, used by :func:`serialize_drx` for lossless
                       round-tripping.
        ``_prolog`` / ``_epilog`` — exact document head/tail text.

    Raises :class:`DrxParseError` on malformed XML or a non-still envelope.
    """
    if not isinstance(xml, str):
        try:
            xml = xml.decode("utf-8")  # tolerate bytes input
        except Exception as e:  # noqa: BLE001
            raise DrxParseError(f"drx content is not decodable text: {e}") from e

    # Make the real ``Gallery::GyStill`` schema parseable by ElementTree. All
    # downstream processing (tree, prolog/epilog, node scan) runs on this
    # token form; serialize_drx() restores the literal ``::`` before emitting.
    xml = xml.replace("::", _NS_TOKEN)

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise DrxParseError(f"malformed .drx XML: {e}") from e

    if not _is_still(root.tag):
        raise DrxParseError(
            f"not a .drx grade envelope: root element is "
            f"<{_localname(root.tag).replace(_NS_TOKEN, '::')}>, "
            f"expected <Gallery::GyStill>"
        )

    # All stills in the document (root is one; be tolerant of nested siblings).
    still_elems = [el for el in root.iter() if _is_still(el.tag)]
    stills = [_extract_still(s) for s in still_elems]

    nodes: List[Dict[str, Any]] = []
    for s in stills:
        nodes.extend(s["corrections"])

    root_tag = _localname(root.tag)
    prolog, epilog = _split_prolog_epilog(xml, root_tag)
    version = _parse_prolog_metadata(prolog)
    version["lm_version_ids"] = [n["db_id"] for n in nodes if n.get("db_id")]
    version["still_ids"] = [s["db_id"] for s in stills if s.get("db_id")]

    return {
        "version": version,
        "stills": stills,
        "nodes": nodes,
        "tree": _elem_to_node(root),
        "_prolog": prolog,
        "_epilog": epilog,
    }


def parse_drx(path: str) -> Dict[str, Any]:
    """Parse a ``.drx`` file from disk.

    See :func:`parse_drx_content` for the returned structure.  Raises
    :class:`DrxParseError` for unreadable files, malformed XML, or documents that
    are not a ``Gallery_GyStill`` envelope.
    """
    if not isinstance(path, str) or path == "":
        raise DrxParseError("parse_drx requires a non-empty file path")
    if not os.path.isfile(path):
        raise DrxParseError(f"no such .drx file: {path}")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            xml = fh.read()
    except OSError as e:
        raise DrxParseError(f"could not read .drx file {path}: {e}") from e

    parsed = parse_drx_content(xml)
    parsed["path"] = path
    return parsed


def serialize_drx(parsed: Dict[str, Any]) -> str:
    """Reconstruct ``.drx`` XML text from a dict produced by :func:`parse_drx`.

    Uses the order-preserving ``tree`` plus the captured document prolog/epilog
    so structure and the exact ``FieldsBlob`` / ``Body`` blob bytes are
    preserved.  ``serialize_drx(parse_drx(x))`` round-trips the envelope.
    """
    if not isinstance(parsed, dict):
        raise DrxParseError("serialize_drx expects a parsed .drx dict")
    tree = parsed.get("tree")
    if not isinstance(tree, dict) or "tag" not in tree:
        raise DrxParseError("parsed dict is missing its 'tree' structure")

    prolog = parsed.get("_prolog") or ""
    epilog = parsed.get("_epilog") or ""
    # Restore the literal ``::`` namespaced tag names so the emitted .drx is a
    # real DaVinci Resolve document (importable), not the internal token form.
    return (prolog + _node_to_xml(tree) + epilog).replace(_NS_TOKEN, "::")
