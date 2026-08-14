"""
davinci_resolve_mcp.formats.drx_codec — .drx grade FieldsBlob/Body codec.

A DaVinci Resolve ``.drx`` correction record (see :mod:`drx_xml`) carries its
colour-node graph in a ``<Body>`` (or ``<FieldsBlob>``) payload.  On disk that
payload is hex text; decoded it is a **framed zstd stream**:

    <frame byte> <zstd frame ...>

where the frame byte is ``0x81`` (the remaining bytes are a zstd frame, magic
``28 b5 2f fd``) or ``0x80`` (STORED — the remaining bytes are the raw
protobuf, uncompressed).  The decompressed stream is a small protobuf grade
document.  Individual grade parameters live at the leaves of that tree as::

    F3 {                       # one parameter entry (repeated)
        F1: <param id>         # varint
        F2 { F1: <value> }     # nested msg, value is a little-endian float32
    }

This module owns the whole stack — framing, zstd (de)compression, and the
protobuf walk — and exposes it as a byte-exact codec:

``decode_fields(blob) -> dict``
    Decode a framed blob (``bytes`` or hex ``str``) into a dict of named grade
    parameters plus the metadata needed to re-encode it losslessly.  Never
    raises on malformed/short input — it returns a partial dict whose
    ``"unparsed"`` field holds the undecodable remainder.
``encode_fields(decoded) -> bytes | str``
    Re-encode a dict produced by :func:`decode_fields` back to a framed blob.
    ``encode_fields(decode_fields(b)) == b`` byte-for-byte for supported
    documents (Resolve 19 uses zstd level 1; recompression reproduces the frame
    exactly when possible, and the original blob is retained as a verbatim fallback).

The value-editing path (patching a parameter's float in place) is structural
only — it produces a well-formed blob, but the numeric UI calibration is not
verified against a live Resolve instance (deferred to the grading/QC layer).
This module never connects to DaVinci Resolve; it operates purely on
bytes/strings and is safe to import with no Resolve instance running.
"""

from __future__ import annotations

import struct
from typing import Any, Dict, List, Optional, Tuple, Union

try:  # zstandard is a hard dependency of the package (see pyproject)
    import zstandard as _zstd
except Exception:  # pragma: no cover - import guard only
    _zstd = None  # type: ignore[assignment]

__all__ = [
    "DrxCodecError",
    "decode_fields",
    "encode_fields",
    "decode_body",
    "encode_body",
    "param_name",
    "ZSTD_MAGIC",
    "FRAME_ZSTD",
    "FRAME_STORED",
]


class DrxCodecError(Exception):
    """Raised only for programmer errors in :func:`encode_fields` (e.g. a dict
    with no reconstructable body).  :func:`decode_fields` never raises."""


# ---------------------------------------------------------------------------
# Framing constants
# ---------------------------------------------------------------------------
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
FRAME_ZSTD = 0x81   # payload after this byte is a zstd frame
FRAME_STORED = 0x80  # payload after this byte is the raw (uncompressed) protobuf

# Resolve 19 writes its grade frames with zstd level 1; recompressing the
# decompressed body at this level reproduces the on-disk frame byte-exactly for
# captured Resolve documents. If a future frame does not reproduce, the original
# blob (retained on decode) is emitted verbatim instead.
_ZSTD_LEVEL = 1


# ---------------------------------------------------------------------------
# Wire types
# ---------------------------------------------------------------------------
_WT_VARINT = 0
_WT_FIXED64 = 1
_WT_LEN = 2
_WT_FIXED32 = 5


# ---------------------------------------------------------------------------
# Parameter id -> semantic name map (documented subset; see
# DRX-VALUE-SCALING.md). Unknown ids fall
# back to ``param_<id>`` so nothing is dropped.
# ---------------------------------------------------------------------------
# (control, channel) per id.
_PARAM_INFO: Dict[int, Tuple[str, Optional[str]]] = {
    # Primary corrector (0x06xxxxxx group)
    100663301: ("saturation", None),
    100663320: ("lift", "r"),
    100663321: ("lift", "g"),
    100663322: ("lift", "b"),
    100663323: ("lift", "master"),
    100663325: ("gain", "r"),
    100663326: ("gain", "g"),
    100663327: ("gain", "b"),
    100663328: ("gain", "master"),
    100663330: ("gamma", "r"),
    100663331: ("gamma", "g"),
    100663332: ("gamma", "b"),
    100663333: ("gamma", "master"),
    100663421: ("offset", "r"),
    100663422: ("offset", "g"),
    100663423: ("offset", "b"),
    # HSL qualifier (0x0830xxxx group)
    137363470: ("qualifier", "hue_center"),
    137363471: ("qualifier", "hue_width"),
    137363472: ("qualifier", "hue_sym"),
    137363473: ("qualifier", "hue_soft"),
    137363474: ("qualifier", "sat_high"),
    137363475: ("qualifier", "sat_low"),
    137363476: ("qualifier", "sat_high_soft"),
    137363477: ("qualifier", "sat_low_soft"),
    137363478: ("qualifier", "lum_high"),
    137363479: ("qualifier", "lum_low"),
    137363480: ("qualifier", "lum_high_soft"),
    137363481: ("qualifier", "lum_low_soft"),
    # Log wheels / secondary (0x86xxxxxx group)
    2248147136: ("pivot", None),
    2248147137: ("contrast", None),
    2248147138: ("log_shadow", "r"),
    2248147139: ("log_shadow", "g"),
    2248147140: ("log_shadow", "b"),
    2248147141: ("log_midtone", "r"),
    2248147142: ("log_midtone", "g"),
    2248147143: ("log_midtone", "b"),
    2248147144: ("log_highlight", "r"),
    2248147145: ("log_highlight", "g"),
    2248147146: ("log_highlight", "b"),
    2248147218: ("color_boost", None),
    2248147219: ("midtone_detail", None),
    2248147221: ("temperature", None),
    2248147222: ("tint", None),
}


def param_name(param_id: int) -> str:
    """Return the semantic name (``control`` or ``control.channel``) for a DRX
    parameter id, or ``param_<id>`` when the id is not in the documented map."""
    info = _PARAM_INFO.get(param_id)
    if info is None:
        return f"param_{param_id}"
    control, channel = info
    return control if channel is None else f"{control}.{channel}"


# ---------------------------------------------------------------------------
# Protobuf varint primitives
# ---------------------------------------------------------------------------
def _read_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    """Read an unsigned varint at ``pos``; return ``(value, new_pos)``.

    Raises ``ValueError`` if the buffer ends mid-varint (caught by the tolerant
    walker so a truncated blob degrades to an ``unparsed`` remainder).
    """
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
            raise ValueError("varint too long")
    raise ValueError("truncated varint")


def _encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("cannot encode negative varint")
    out = bytearray()
    v = value & 0xFFFFFFFFFFFFFFFF
    while v >= 0x80:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v)
    return bytes(out)


# ---------------------------------------------------------------------------
# Generic, position-tracking protobuf walk (lossless enough for extraction).
# ---------------------------------------------------------------------------
def _parse_fields(buf: bytes, base: int) -> List[Dict[str, Any]]:
    """Parse ``buf`` as a flat protobuf message.

    Returns a list of field dicts, each::

        {"num", "wt", "value", "abs"}

    where ``abs`` is the absolute byte offset (``base`` + local) of the field's
    *value*: the float bytes for FIXED32/FIXED64, or the payload start for
    LEN.  LEN fields additionally carry ``"payload"`` (the raw bytes).

    Raises ``ValueError`` on a malformed/truncated buffer.
    """
    fields: List[Dict[str, Any]] = []
    pos = 0
    n = len(buf)
    while pos < n:
        tag, pos = _read_varint(buf, pos)
        num = tag >> 3
        wt = tag & 0x07
        if wt == _WT_VARINT:
            value, pos = _read_varint(buf, pos)
            fields.append({"num": num, "wt": wt, "value": value, "abs": None})
        elif wt == _WT_FIXED64:
            if pos + 8 > n:
                raise ValueError("truncated fixed64")
            fields.append({"num": num, "wt": wt, "value": buf[pos:pos + 8], "abs": base + pos})
            pos += 8
        elif wt == _WT_FIXED32:
            if pos + 4 > n:
                raise ValueError("truncated fixed32")
            fields.append({"num": num, "wt": wt, "value": buf[pos:pos + 4], "abs": base + pos})
            pos += 4
        elif wt == _WT_LEN:
            length, pos = _read_varint(buf, pos)
            if pos + length > n:
                raise ValueError("truncated length-delimited field")
            payload = buf[pos:pos + length]
            fields.append({
                "num": num, "wt": wt, "value": length,
                "abs": base + pos, "payload": payload,
            })
            pos += length
        else:  # wire types 3/4 (groups) are obsolete and never appear here
            raise ValueError(f"unsupported wire type {wt}")
    return fields


def _looks_like_param(field: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """If a LEN field is a grade-parameter entry ``F3{F1 id, F2{F1 f32}}``,
    return ``{"id", "value", "value_abs"}``; otherwise ``None``.

    Matching on structure (varint id + a nested ``F2`` holding exactly one
    little-endian float32) rather than on nesting depth makes extraction robust
    across the different node layouts Resolve emits.
    """
    if field.get("wt") != _WT_LEN:
        return None
    try:
        inner = _parse_fields(field["payload"], field["abs"])
    except ValueError:
        return None
    pid: Optional[int] = None
    value: Optional[float] = None
    value_abs: Optional[int] = None
    for sub in inner:
        if sub["num"] == 1 and sub["wt"] == _WT_VARINT:
            pid = sub["value"]
        elif sub["num"] == 2 and sub["wt"] == _WT_LEN:
            try:
                leaf = _parse_fields(sub["payload"], sub["abs"])
            except ValueError:
                return None
            for lf in leaf:
                if lf["num"] == 1 and lf["wt"] == _WT_FIXED32:
                    value = struct.unpack("<f", lf["value"])[0]
                    value_abs = lf["abs"]
    if pid is None or value is None or value_abs is None:
        return None
    return {"id": pid, "value": value, "value_abs": value_abs}


def _extract_params(buf: bytes, base: int, out: List[Dict[str, Any]]) -> None:
    """Recursively walk ``buf`` collecting every grade-parameter entry into
    ``out`` (in document order).

    The walk is incremental and tolerant: fields are parsed one at a time, so a
    truncated or corrupt tail stops the walk at that point while still yielding
    every parameter reachable before it (partial decode, never a raise)."""
    pos = 0
    n = len(buf)
    while pos < n:
        try:
            tag, npos = _read_varint(buf, pos)
        except ValueError:
            return
        num = tag >> 3
        wt = tag & 0x07
        if wt == _WT_VARINT:
            try:
                _, pos = _read_varint(buf, npos)
            except ValueError:
                return
        elif wt == _WT_FIXED64:
            pos = npos + 8
            if pos > n:
                return
        elif wt == _WT_FIXED32:
            pos = npos + 4
            if pos > n:
                return
        elif wt == _WT_LEN:
            try:
                length, dpos = _read_varint(buf, npos)
            except ValueError:
                return
            if dpos + length > n:
                return
            payload = buf[dpos:dpos + length]
            payload_abs = base + dpos
            field = {"num": num, "wt": wt, "abs": payload_abs, "payload": payload}
            handled = False
            if num == 3:
                hit = _looks_like_param(field)
                if hit is not None:
                    out.append(hit)
                    handled = True  # a param leaf has no further params inside
            if not handled:
                _extract_params(payload, payload_abs, out)
            pos = dpos + length
        else:  # unsupported wire type (e.g. groups / corruption) — stop here
            return


# ---------------------------------------------------------------------------
# Framing / compression layer
# ---------------------------------------------------------------------------
def _as_bytes(blob: Union[bytes, bytearray, str]) -> Tuple[bytes, bool]:
    """Normalise input to ``bytes``; return ``(data, was_hex)``.

    Accepts raw bytes or a (whitespace-tolerant) hex string.  A non-hex string
    yields empty bytes so the caller degrades gracefully rather than raising.
    """
    if isinstance(blob, (bytes, bytearray)):
        return bytes(blob), False
    if isinstance(blob, str):
        cleaned = "".join(blob.split())
        try:
            return bytes.fromhex(cleaned), True
        except ValueError:
            return b"", True
    return b"", False


def decode_body(blob: Union[bytes, bytearray, str]) -> Dict[str, Any]:
    """Strip the frame byte and decompress a ``.drx`` grade blob.

    Returns ``{"framing", "compressed", "stored", "body", "error"}`` where
    ``body`` is the decompressed protobuf bytes (``b""`` on failure).  Never
    raises; a decompression failure is reported via ``error``.
    """
    data, _ = _as_bytes(blob)
    result: Dict[str, Any] = {
        "framing": None, "compressed": False, "stored": False,
        "body": b"", "error": None,
    }
    if not data:
        result["error"] = "empty or non-hex blob"
        return result

    frame = data[0]
    if frame == FRAME_ZSTD:
        result["framing"] = FRAME_ZSTD
        result["compressed"] = True
        payload = data[1:]
    elif frame == FRAME_STORED:
        result["framing"] = FRAME_STORED
        result["stored"] = True
        result["body"] = data[1:]
        return result
    elif data[:4] == ZSTD_MAGIC:
        # No frame byte — the blob is a bare zstd stream.
        result["compressed"] = True
        payload = data
    else:
        # Assume the blob is already a raw (unframed) protobuf body.
        result["body"] = data
        return result

    if _zstd is None:
        result["error"] = "zstandard not available"
        return result
    if payload[:4] != ZSTD_MAGIC:
        result["error"] = "framed blob is not a zstd stream"
        return result
    try:
        result["body"] = _zstd.ZstdDecompressor().decompress(payload)
    except Exception as e:  # noqa: BLE001 - surface any zstd error as text
        result["error"] = f"zstd decompress failed: {e}"
    return result


def encode_body(body: bytes, framing: Optional[int], compressed: bool) -> bytes:
    """Re-frame/compress a decompressed protobuf ``body`` into a blob.

    ``framing`` selects the leading frame byte (``0x81``/``0x80``/``None``).
    ``compressed`` controls whether the body is zstd-compressed (level 1, which
    reproduces Resolve's on-disk frames).  Raises :class:`DrxCodecError` if a
    compressed frame is requested without the ``zstandard`` package.
    """
    if compressed:
        if _zstd is None:
            raise DrxCodecError("zstandard not available for compression")
        payload = _zstd.ZstdCompressor(level=_ZSTD_LEVEL).compress(body)
    else:
        payload = body
    if framing is None:
        return payload
    return bytes([framing]) + payload


# ---------------------------------------------------------------------------
# Public codec
# ---------------------------------------------------------------------------
def decode_fields(blob: Union[bytes, bytearray, str]) -> Dict[str, Any]:
    """Decode a framed ``.drx`` grade blob into named parameters.

    ``blob`` may be raw ``bytes`` or a hex ``str`` (whitespace tolerated), and
    may be framed (``0x81``/``0x80`` prefix), a bare zstd stream, or an already
    decompressed protobuf body — the framing is auto-detected.

    Returns a dict with:
        ``params``    — list of ``{"id", "hex_id", "name", "control",
                        "channel", "value"}`` in document order.
        ``named``     — convenience ``{name: value}`` (last write wins on
                        duplicate names).
        ``count``     — number of parameters decoded.
        ``framing`` / ``compressed`` / ``stored`` — framing metadata.
        ``body_len``  — length of the decompressed protobuf body.
        ``unparsed``  — hex of any trailing bytes that could not be parsed as
                        protobuf (``""`` when the body parses cleanly); a short
                        or corrupt blob yields a partial dict with this set
                        rather than raising.
        ``error``     — a human-readable string when decompression failed,
                        else ``None``.
        ``_body_hex`` / ``_blob_hex`` / ``_hex_input`` — internal state used by
                        :func:`encode_fields` for a byte-exact round-trip.

    This function never raises.
    """
    data, was_hex = _as_bytes(blob)
    framed = decode_body(data)
    body = framed["body"]

    params: List[Dict[str, Any]] = []
    named: Dict[str, float] = {}
    hits: List[Dict[str, Any]] = []
    if body:
        _extract_params(body, 0, hits)
    for hit in hits:
        pid = hit["id"]
        control, channel = _PARAM_INFO.get(pid, (None, None))
        name = param_name(pid)
        params.append({
            "id": pid,
            "hex_id": f"0x{pid:x}",
            "name": name,
            "control": control,
            "channel": channel,
            "value": hit["value"],
            "_value_abs": hit["value_abs"],
        })
        named[name] = hit["value"]

    # Determine any trailing bytes the top-level protobuf walk could not consume
    # (e.g. a truncated body).  A clean body reports "".
    unparsed = _trailing_unparsed(body)

    return {
        "params": params,
        "named": named,
        "count": len(params),
        "framing": framed["framing"],
        "compressed": framed["compressed"],
        "stored": framed["stored"],
        "body_len": len(body),
        "unparsed": unparsed,
        "error": framed["error"],
        "_body_hex": body.hex(),
        "_blob_hex": data.hex(),
        "_hex_input": was_hex,
    }


def _trailing_unparsed(body: bytes) -> str:
    """Return the hex of the byte range the top-level protobuf parse could not
    consume, or ``""`` if the whole body parses.  Used to expose a remainder
    for short/corrupt blobs instead of raising."""
    if not body:
        return ""
    pos = 0
    n = len(body)
    while pos < n:
        start = pos
        try:
            tag, pos = _read_varint(body, pos)
            wt = tag & 0x07
            if wt == _WT_VARINT:
                _, pos = _read_varint(body, pos)
            elif wt == _WT_FIXED64:
                pos += 8
            elif wt == _WT_FIXED32:
                pos += 4
            elif wt == _WT_LEN:
                length, pos = _read_varint(body, pos)
                pos += length
            else:
                return body[start:].hex()
            if pos > n:
                return body[start:].hex()
        except ValueError:
            return body[start:].hex()
    return ""


def encode_fields(decoded: Dict[str, Any]) -> Union[bytes, str]:
    """Re-encode a dict from :func:`decode_fields` back into a framed blob.

    Reconstructs the decompressed protobuf body from ``_body_hex`` and writes
    each parameter's current ``value`` back into its float32 slot (so edits to
    ``decoded["params"][i]["value"]`` are reflected), then re-applies the
    original framing/compression.

    The return type mirrors the original input: ``str`` (hex) if the blob was
    decoded from a hex string, else ``bytes``.

    Byte-exact guarantee: when no parameter value has changed, the output equals
    the input exactly — recompression at zstd level 1 reproduces Resolve's frame
    for supported Resolve documents, and the original blob is emitted verbatim as a
    fallback should a frame ever fail to reproduce.  Editing a value produces a
    well-formed blob whose numeric calibration is structural-only (unverified
    against live Resolve).

    Raises :class:`DrxCodecError` only if ``decoded`` carries no reconstructable
    body.
    """
    if not isinstance(decoded, dict):
        raise DrxCodecError("encode_fields expects a dict from decode_fields")
    body_hex = decoded.get("_body_hex")
    if body_hex is None:
        raise DrxCodecError("decoded dict is missing '_body_hex' (cannot re-encode)")

    was_hex = bool(decoded.get("_hex_input"))
    try:
        body = bytearray.fromhex(body_hex)
    except ValueError as e:
        raise DrxCodecError(f"'_body_hex' is not valid hex: {e}") from e

    orig_body = bytes(body)

    # Write current parameter values back into their float32 slots.
    for param in decoded.get("params") or []:
        abs_off = param.get("_value_abs")
        if abs_off is None:
            continue
        if not (0 <= abs_off + 4 <= len(body)):
            continue
        try:
            val = float(param.get("value"))
        except (TypeError, ValueError):
            continue
        body[abs_off:abs_off + 4] = struct.pack("<f", val)

    body_bytes = bytes(body)
    unchanged = body_bytes == orig_body

    framing = decoded.get("framing")
    compressed = bool(decoded.get("compressed"))

    # Fast path: nothing changed and we still hold the original framed blob —
    # emit it verbatim for a guaranteed byte-exact round-trip.
    blob_hex = decoded.get("_blob_hex")
    if unchanged and blob_hex:
        out = bytes.fromhex(blob_hex)
        return out.hex() if was_hex else out

    out = encode_body(body_bytes, framing, compressed)

    # Safety net: if the framed body was expected to be byte-exact (unchanged)
    # but recompression diverged, prefer the retained original blob.
    if unchanged and blob_hex:
        original = bytes.fromhex(blob_hex)
        if out != original:
            out = original

    return out.hex() if was_hex else out
