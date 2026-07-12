"""
davinci_resolve_mcp.formats.lut — .cube / .3dl LUT header parsing + attach-reference.

Parses just enough of a Resolve-compatible LUT file to identify it (type,
size, title) without decoding the full lattice into memory, and produces an
"attach reference" record (path + sha256 + file size) suitable for pointing a
.drx node's LUT slot at a named LUT (see :mod:`davinci_resolve_mcp.formats.drx_codec`
for the FieldsBlob attach path). This module never bakes/evaluates a LUT — it
is metadata + reference only, and never talks to DaVinci Resolve.

Supported formats:

    * ``.cube`` — Adobe/Iridas cube LUT. Header keywords: ``TITLE``,
      ``LUT_1D_SIZE`` / ``LUT_3D_SIZE``, ``DOMAIN_MIN`` / ``DOMAIN_MAX``.
      Data rows are ``N``/``N**3`` lines of 3 floats each (not parsed here).
    * ``.3dl`` — Autodesk/Lustre 3D LUT. Two header shapes are recognized:

        - Legacy Lustre: the first non-comment line is a "shaper" line of
          ascending integers (input-code values); its element count is the
          per-axis LUT size, and there is no explicit title.
        - Nuke-style ``3DMESH``: a ``3DMESH`` line followed by a
          ``Mesh <inSize> <outSize>`` line, then the shaper line; ``inSize``
          is the per-axis LUT size.

Both formats' data rows (``size**3`` or ``size`` triplets of R G B values)
are intentionally never parsed by :func:`parse_lut` — only the header is
read, keeping this cheap even for huge (e.g. 65x65x65) 3D LUTs.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, TextIO

_CUBE_EXTENSIONS = (".cube",)
_THREE_DL_EXTENSIONS = (".3dl",)

# Chunk size used when streaming a file for hashing — keeps attach_ref()
# memory-bounded regardless of LUT size (a 65^3 .cube can be tens of MB).
_HASH_CHUNK_SIZE = 1024 * 1024


class LutParseError(ValueError):
    """
    Raised when a .cube/.3dl file cannot be parsed.

    The message always names the offending line (1-based line number plus
    its raw text, truncated) so a caller can locate the malformed input.
    """


def _fail(path: str, line_no: int, line_text: str, reason: str) -> "LutParseError":
    snippet = line_text.strip()
    if len(snippet) > 80:
        snippet = snippet[:77] + "..."
    return LutParseError(
        f"lut: {os.path.basename(path)}: line {line_no}: {reason} (got {snippet!r})"
    )


def _strip_comment(line: str) -> str:
    # Both .cube and .3dl treat '#' as a start-of-comment marker.
    idx = line.find("#")
    return line if idx < 0 else line[:idx]


def _detect_type(path: str, first_nonblank_line: str | None) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in _CUBE_EXTENSIONS:
        return "cube"
    if ext in _THREE_DL_EXTENSIONS:
        return "3dl"
    # Fall back to content sniffing for extensionless/unknown-extension paths.
    if first_nonblank_line is not None:
        upper = first_nonblank_line.strip().upper()
        if upper.startswith("TITLE") or upper.startswith("LUT_1D_SIZE") or upper.startswith("LUT_3D_SIZE"):
            return "cube"
        if upper.startswith("3DMESH"):
            return "3dl"
        # A line of ascending integers only (no floats/keywords) reads as a
        # legacy Lustre .3dl shaper row.
        tokens = first_nonblank_line.split()
        if tokens and all(_is_int_token(t) for t in tokens):
            return "3dl"
    raise LutParseError(
        f"lut: {os.path.basename(path)}: cannot detect LUT type from extension or content "
        "(expected .cube or .3dl)"
    )


def _is_int_token(token: str) -> bool:
    try:
        int(token)
        return True
    except ValueError:
        return False


def _parse_cube_header(path: str, fh: TextIO) -> dict[str, Any]:
    title: str | None = None
    size: int | None = None
    lut_kind: str | None = None  # "1D" or "3D"
    domain_min: list[float] | None = None
    domain_max: list[float] | None = None

    for line_no, raw_line in enumerate(fh, start=1):
        line = _strip_comment(raw_line).strip()
        if not line:
            continue
        parts = line.split()
        keyword = parts[0].upper()

        if keyword == "TITLE":
            rest = line[len(parts[0]):].strip()
            if len(rest) < 2 or rest[0] != '"' or rest[-1] != '"':
                raise _fail(path, line_no, raw_line, "TITLE must be a quoted string")
            title = rest[1:-1]
        elif keyword in ("LUT_1D_SIZE", "LUT_3D_SIZE"):
            if len(parts) != 2:
                raise _fail(path, line_no, raw_line, f"{keyword} requires exactly one integer argument")
            try:
                size = int(parts[1])
            except ValueError:
                raise _fail(path, line_no, raw_line, f"{keyword} argument must be an integer") from None
            if size <= 0:
                raise _fail(path, line_no, raw_line, f"{keyword} must be positive")
            lut_kind = "1D" if keyword == "LUT_1D_SIZE" else "3D"
            # Header is fully determined once we have the size; the rest of
            # the file is data rows we intentionally never read here.
            break
        elif keyword == "DOMAIN_MIN":
            domain_min = _parse_float_triplet(path, line_no, raw_line, parts[1:], keyword)
        elif keyword == "DOMAIN_MAX":
            domain_max = _parse_float_triplet(path, line_no, raw_line, parts[1:], keyword)
        elif keyword in ("LUT_1D_INPUT_RANGE", "LUT_3D_INPUT_RANGE"):
            continue  # recognized-but-unused metadata; not needed for header identity
        else:
            # A data row (numbers) appearing before a *_SIZE keyword means
            # the file is missing its size header entirely.
            raise _fail(
                path, line_no, raw_line,
                "expected a .cube header keyword (TITLE/LUT_1D_SIZE/LUT_3D_SIZE/DOMAIN_MIN/DOMAIN_MAX) "
                "before any data row",
            )

    if size is None or lut_kind is None:
        raise LutParseError(
            f"lut: {os.path.basename(path)}: missing required LUT_1D_SIZE/LUT_3D_SIZE header"
        )

    result: dict[str, Any] = {"type": "cube", "kind": lut_kind, "size": size, "title": title}
    if domain_min is not None:
        result["domain_min"] = domain_min
    if domain_max is not None:
        result["domain_max"] = domain_max
    return result


def _parse_float_triplet(path: str, line_no: int, raw_line: str, tokens: list[str], keyword: str) -> list[float]:
    if len(tokens) != 3:
        raise _fail(path, line_no, raw_line, f"{keyword} requires exactly 3 numeric values")
    try:
        return [float(t) for t in tokens]
    except ValueError:
        raise _fail(path, line_no, raw_line, f"{keyword} values must be numeric") from None


def _parse_3dl_header(path: str, fh: TextIO) -> dict[str, Any]:
    is_nuke_style = False
    mesh_in_size: int | None = None
    shaper_size: int | None = None

    for line_no, raw_line in enumerate(fh, start=1):
        line = _strip_comment(raw_line).strip()
        if not line:
            continue
        parts = line.split()
        upper_first = parts[0].upper()

        if upper_first == "3DMESH":
            is_nuke_style = True
            continue

        if is_nuke_style and upper_first == "MESH":
            if len(parts) != 3:
                raise _fail(path, line_no, raw_line, "Mesh line requires exactly 2 integer arguments")
            try:
                mesh_in_size = int(parts[1])
            except ValueError:
                raise _fail(path, line_no, raw_line, "Mesh <inSize> must be an integer") from None
            continue

        # First remaining non-blank/non-directive line is the shaper
        # (input-code-value) row: N ascending integers, N == per-axis size.
        if not all(_is_int_token(t) for t in parts):
            raise _fail(
                path, line_no, raw_line,
                "expected the .3dl shaper line (ascending integers) before any data row",
            )
        shaper_values = [int(t) for t in parts]
        if len(shaper_values) < 2:
            raise _fail(path, line_no, raw_line, "shaper line must list at least 2 values")
        if any(b <= a for a, b in zip(shaper_values, shaper_values[1:])):
            raise _fail(path, line_no, raw_line, "shaper line values must be strictly ascending")
        shaper_size = len(shaper_values)
        break

    if shaper_size is None:
        raise LutParseError(
            f"lut: {os.path.basename(path)}: missing .3dl shaper (input-code-value) line"
        )

    size = mesh_in_size if (is_nuke_style and mesh_in_size is not None) else shaper_size
    result: dict[str, Any] = {"type": "3dl", "kind": "3D", "size": size, "title": None}
    if is_nuke_style:
        result["style"] = "nuke"
    else:
        result["style"] = "lustre"
    return result


def parse_lut(path: str) -> dict[str, Any]:
    """
    Parse a .cube or .3dl LUT header without loading its full data lattice.

    Params:
      path: filesystem path to a .cube or .3dl file. The format is chosen
        from the file extension; unknown/missing extensions fall back to
        sniffing the first non-blank line.

    Returns a dict with (at minimum):
      - "type": "cube" | "3dl"
      - "kind": "1D" | "3D"
      - "size": int — the LUT's per-axis size (e.g. 33 for a 33x33x33 cube)
      - "title": str | None — the TITLE header for .cube (.3dl has no title
        concept and always reports None)

    .cube results additionally include "domain_min"/"domain_max" ([r,g,b]
    floats) when present in the header.
    .3dl results additionally include "style": "lustre" | "nuke".

    Only the header is read; data rows (size or size**3 RGB triplets) are
    never parsed, so this is cheap even for very large 3D LUTs.

    Raises LutParseError (naming the offending 1-based line number and its
    text) if the file's type cannot be detected, or its header is malformed
    or incomplete. Raises OSError if the file cannot be opened.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        first_line_pos = fh.tell()
        first_nonblank: str | None = None
        for raw_line in fh:
            stripped = _strip_comment(raw_line).strip()
            if stripped:
                first_nonblank = raw_line
                break
        fh.seek(first_line_pos)

        lut_type = _detect_type(path, first_nonblank)
        if lut_type == "cube":
            return _parse_cube_header(path, fh)
        return _parse_3dl_header(path, fh)


def attach_ref(path: str) -> dict[str, Any]:
    """
    Build a named-LUT attach-reference record for pointing a .drx node's LUT
    slot at a LUT file, without loading the LUT's contents into memory.

    Params:
      path: filesystem path to the LUT file (.cube, .3dl, or any other LUT
        the encoder may reference by path — this function does not require
        the file to be a valid/parseable LUT header, only that it exists).

    Returns {"path": str, "sha256": str, "size": int} where "size" is the
    file's byte size (os.stat) and "sha256" is computed by streaming the
    file in fixed-size chunks (never reading it whole into memory), so this
    stays cheap for arbitrarily large LUT files.

    Raises OSError if the file cannot be opened/stat'd.
    """
    file_size = os.stat(path).st_size
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_HASH_CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
    return {"path": path, "sha256": hasher.hexdigest(), "size": file_size}
