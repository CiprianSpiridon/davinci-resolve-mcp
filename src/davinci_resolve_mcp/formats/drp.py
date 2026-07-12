"""
davinci_resolve_mcp.formats.drp — .drp (Resolve Project) read / author / edit.

A DaVinci Resolve ``.drp`` is a ZIP archive.  It is a *superset* of a ``.drt``:
it carries the same ``SeqContainer`` timeline documents **plus** a top-level
``project.xml`` (``SM_Project``) and a Media Pool folder tree stored in
``MpFolder.xml`` (real exports place it at ``MediaPool/Master/MpFolder.xml``).
The presence of ``project.xml`` is what distinguishes a ``.drp`` project from a
``.drt`` timeline archive.

This module composes :mod:`davinci_resolve_mcp.formats.drt` (it reuses that
module's SeqContainer parse/author and frame-rate codec instead of
re-implementing them) and adds the project + Media-Pool layer on top.  It never
connects to DaVinci Resolve — it operates purely on bytes / file paths and is
safe to import with no Resolve instance running.

The Media-Pool-blob invariant
-----------------------------
``relink-media`` reference): **Resolve links a clip to its media by the path
stored inside the Media Pool clip's binary "Clip" blob, not by the timeline
clip's plain-text ``<MediaFilePath>``**.  Resolve also does *not* reconform on
import, so that cached path is authoritative.  Accordingly:

* :func:`read_drp` recovers each Media-Pool clip's media path from its
  length-delimited *Clip-path blob* first, only then falling back to the
  plain-text ``<MediaFilePath>`` or the clip ``<Name>``.
* :func:`author_drp` / :func:`edit_drp` write the media link **into the
  Media-Pool blob** (``<ClipPathBlob>``, hex) — the plain-text path is written
  too, but the blob is the link of record.

The blob framing mirrors Resolve's Clip blob:
``[4-byte tag][4-byte big-endian payload length][payload]`` where the payload
holds two protobuf-style length-delimited fields — ``0x0a <varint> <dir>`` and
``0x12 <varint> <filename>``.

Public API
----------
``read_drp(data) -> dict``
    Parse a ``.drp`` (path or bytes) into
    ``{"name", "folders", "clips", "timelines", ...}``.
``author_drp(spec) -> bytes``
    Author a ``.drp`` ZIP from a spec such that :func:`read_drp` reads it back
    to the same project shape (offline round-trip).
``edit_drp(data, action, ...) -> bytes``
    ``add_clip`` / ``remove_clip`` on the Media Pool, preserving every other
    member and every other clip byte-for-byte.
``encode_clip_path_blob`` / ``decode_clip_path_blob``
    The Media-Pool Clip-path blob codec.
``DrpFormatError``
    Raised for malformed archives (e.g. a ``.drp`` missing ``MpFolder.xml``) and
    bad authoring/editing input — never a bare ``zipfile`` / ``struct`` error.
"""

from __future__ import annotations

import io
import os
import posixpath as _pp
import re
import struct
import uuid as _uuid
import zipfile
from typing import Any, Dict, List, Optional, Tuple, Union

from . import drt

__all__ = [
    "DrpFormatError",
    "read_drp",
    "author_drp",
    "edit_drp",
    "encode_clip_path_blob",
    "decode_clip_path_blob",
    "build_mp_folder_xml",
    "build_project_xml",
    "parse_mp_folder",
]


class DrpFormatError(Exception):
    """Raised when a ``.drp`` is malformed or cannot be authored/edited."""


# Media-Pool clip element tags → normalized clip kind.
_CLIP_TAG_KIND = {
    "Sm2MpVideoClip": "video",
    "Sm2MpAudioClip": "audio",
    "Sm2MpTimelineClip": "timeline",
    "Sm2MpStillClip": "still",
    "Sm2MpCompoundClip": "compound",
}
_KIND_CLIP_TAG = {"video": "Sm2MpVideoClip", "audio": "Sm2MpAudioClip", "timeline": "Sm2MpTimelineClip"}
_CLIP_TAG_RE = re.compile(r"<(Sm2Mp\w*Clip)(?:\s[^>]*?)?(/?)>")

_FOLDER_TAG = "Sm2MpFolder"
_DEFAULT_COLOR_TAG = "FOLDER_COLOR_NONE"


# ---------------------------------------------------------------------------
# Small helpers (escaping / ids delegate to the drt codec where shared)
# ---------------------------------------------------------------------------
def _gen_uuid() -> str:
    return _uuid.uuid4().hex


def _escape_xml_text(value: str) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _unescape_xml(value: str) -> str:
    return (
        value.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
        .replace("&amp;", "&")
    )


def _load_bytes(data: Union[str, bytes, bytearray]) -> bytes:
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, str):
        if not os.path.isfile(data):
            raise DrpFormatError(f"no such .drp file: {data}")
        with open(data, "rb") as fh:
            return fh.read()
    raise DrpFormatError("expected a file path (str) or raw bytes")


def _read_members(buf: bytes) -> "Dict[str, bytes]":
    try:
        with zipfile.ZipFile(io.BytesIO(buf), "r") as zf:
            return {info.filename: zf.read(info.filename) for info in zf.infolist() if not info.is_dir()}
    except zipfile.BadZipFile as e:
        raise DrpFormatError(f"not a valid .drp zip: {e}") from e


def _write_members(members: "Dict[str, bytes]") -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return out.getvalue()


def _basename(member: str) -> str:
    return member.rsplit("/", 1)[-1]


def _find_mp_folder_member(members: "Dict[str, bytes]") -> Optional[str]:
    """Return the member path of the *root* MpFolder.xml, if present."""
    candidates = [n for n in members if _basename(n) == "MpFolder.xml"]
    if not candidates:
        return None
    # Prefer the canonical real-Resolve location, then the shallowest path.
    for pref in ("MediaPool/Master/MpFolder.xml", "Primary1/MpFolder.xml"):
        if pref in members:
            return pref
    return min(candidates, key=lambda n: (n.count("/"), len(n)))


# ---------------------------------------------------------------------------
# Media-Pool Clip-path blob codec (the link-of-record)
# ---------------------------------------------------------------------------
def _varint_encode(n: int) -> bytes:
    out = bytearray()
    v = int(n)
    while v > 0x7F:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v & 0x7F)
    return bytes(out)


def _varint_decode(buf: bytes, i: int) -> Tuple[int, int]:
    shift = 0
    val = 0
    while True:
        byte = buf[i]
        i += 1
        val |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return val, i
        shift += 7


def encode_clip_path_blob(path: str) -> str:
    """Encode a media path as a Media-Pool Clip-path blob (returns hex).

    Layout: ``[00 00 00 01][BE u32 payload-len][0x0a <varint> dir][0x12 <varint>
    filename]`` — the directory and filename as protobuf length-delimited fields.
    This is the path Resolve links by (see module docstring).
    """
    p = str(path)
    directory = _pp.dirname(p)
    filename = _pp.basename(p)
    dir_b = directory.encode("utf-8")
    name_b = filename.encode("utf-8")
    payload = b"\x0a" + _varint_encode(len(dir_b)) + dir_b + b"\x12" + _varint_encode(len(name_b)) + name_b
    blob = b"\x00\x00\x00\x01" + struct.pack(">I", len(payload)) + payload
    return blob.hex()


def decode_clip_path_blob(hexstr: str) -> Optional[str]:
    """Decode a Media-Pool Clip-path blob (hex) back to a media path, or ``None``."""
    try:
        blob = bytes.fromhex(re.sub(r"[^0-9a-fA-F]", "", hexstr or ""))
    except ValueError:
        return None
    if len(blob) < 8:
        return None
    payload = blob[8:]
    directory: Optional[str] = None
    filename: Optional[str] = None
    i = 0
    try:
        while i < len(payload):
            tag = payload[i]
            i += 1
            length, i = _varint_decode(payload, i)
            value = payload[i : i + length]
            i += length
            if len(value) != length:
                return None
            if tag == 0x0A:
                directory = value.decode("utf-8", "replace")
            elif tag == 0x12:
                filename = value.decode("utf-8", "replace")
            else:
                # Unknown field — bail rather than mis-read.
                return None
    except IndexError:
        return None
    if directory is None and filename is None:
        return None
    if directory and filename:
        return _pp.join(directory, filename)
    return filename or directory


# ---------------------------------------------------------------------------
# Tolerant, depth-aware XML block extraction
# ---------------------------------------------------------------------------
# Resolve's MpFolder.xml is NOT well-formed for a strict parser (tags like
# ``ListMgt::LmVersionTable`` and embedded control bytes), so we scan it with
# depth-aware regex the same way :mod:`drt` scans SeqContainer XML.
def _extract_element(
    xml: str, tag: str, start: int = 0
) -> Optional[Tuple[int, int, int, int, str, str]]:
    """Find the first ``<tag>`` at/after ``start``, matching nesting.

    Returns ``(open_start, content_start, content_end, close_end, content,
    attrs)`` or ``None``. Handles a self-closing ``<tag/>`` (content == "").
    """
    open_re = re.compile(r"<" + re.escape(tag) + r"(\s[^>]*?)?(/?)>")
    m = open_re.search(xml, start)
    if not m:
        return None
    attrs = m.group(1) or ""
    if m.group(2) == "/":
        return (m.start(), m.end(), m.end(), m.end(), "", attrs)

    open_scan = re.compile(r"<" + re.escape(tag) + r"(?:\s[^>]*?)?(/?)>")
    close_scan = re.compile(r"</" + re.escape(tag) + r">")
    depth = 1
    pos = m.end()
    while depth > 0:
        nxt_open = open_scan.search(xml, pos)
        nxt_close = close_scan.search(xml, pos)
        if nxt_close is None:
            return None
        if nxt_open is not None and nxt_open.start() < nxt_close.start():
            if nxt_open.group(1) != "/":
                depth += 1
            pos = nxt_open.end()
        else:
            depth -= 1
            pos = nxt_close.end()
            if depth == 0:
                return (m.start(), m.end(), nxt_close.start(), nxt_close.end(), xml[m.end() : nxt_close.start()], attrs)
    return None


def _attr(attrs: str, name: str) -> Optional[str]:
    m = re.search(name + r'="([^"]*)"', attrs)
    return m.group(1) if m else None


def _scalar(xml: str, tag: str) -> Optional[str]:
    m = re.search(rf"<{tag}>([\s\S]*?)</{tag}>", xml)
    return _unescape_xml(m.group(1).strip()) if m else None


def _child_vecs(inner: str) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(folder_vec_content, media_vec_content)`` for a folder body.

    The MediaVec is located *after* the FolderVec so a nested folder's own
    MediaVec is never mistaken for this folder's.
    """
    fv = _extract_element(inner, "FolderVec")
    fv_content = fv[4] if fv else None
    search_from = fv[3] if fv else 0
    mv = _extract_element(inner, "MediaVec", start=search_from)
    mv_content = mv[4] if mv else None
    return fv_content, mv_content


def _iter_child_blocks(content: str, tag_re: re.Pattern):
    """Yield ``(tag, attrs, block_xml, inner)`` for each top-level element in
    ``content`` whose opening tag matches ``tag_re`` (depth-aware)."""
    pos = 0
    while True:
        m = tag_re.search(content, pos)
        if not m:
            return
        tag = m.group(1)
        el = _extract_element(content, tag, m.start())
        if el is None:
            return
        open_start, _cs, _ce, close_end, inner, attrs = el
        yield tag, attrs, content[open_start:close_end], inner
        pos = close_end


# ---------------------------------------------------------------------------
# Media-Pool parsing
# ---------------------------------------------------------------------------
def _parse_clip(tag: str, attrs: str, inner: str) -> Dict[str, Any]:
    blob_path = None
    blob_hex = _scalar(inner, "ClipPathBlob")
    if blob_hex:
        blob_path = decode_clip_path_blob(blob_hex)
    media_path = blob_path or _scalar(inner, "MediaFilePath")
    return {
        "kind": _CLIP_TAG_KIND.get(tag, "unknown"),
        "tag": tag,
        "dbId": _attr(attrs, "DbId"),
        "name": _scalar(inner, "Name"),
        "mediaPath": media_path,
        "linkedByBlob": blob_path is not None,
        "uniqueId": _scalar(inner, "UniqueMediaPoolItemId"),
    }


def _parse_folder(attrs: str, inner: str) -> Dict[str, Any]:
    fv_content, mv_content = _child_vecs(inner)

    folders: List[Dict[str, Any]] = []
    if fv_content:
        for _t, f_attrs, _block, f_inner in _iter_child_blocks(
            fv_content, re.compile(r"<(" + _FOLDER_TAG + r")(?:\s[^>]*?)?(/?)>")
        ):
            folders.append(_parse_folder(f_attrs, f_inner))

    clips: List[Dict[str, Any]] = []
    if mv_content:
        for c_tag, c_attrs, _block, c_inner in _iter_child_blocks(mv_content, _CLIP_TAG_RE):
            clips.append(_parse_clip(c_tag, c_attrs, c_inner))

    return {
        "name": _scalar(inner, "Name"),
        "dbId": _attr(attrs, "DbId"),
        "colorTag": _scalar(inner, "ColorTag"),
        "folders": folders,
        "clips": clips,
    }


def parse_mp_folder(xml: str) -> Dict[str, Any]:
    """Parse an ``MpFolder.xml`` document into the root folder dict."""
    root = _extract_element(xml, _FOLDER_TAG)
    if root is None:
        raise DrpFormatError("MpFolder.xml has no <Sm2MpFolder> root element")
    _os, _cs, _ce, _clse, inner, attrs = root
    return _parse_folder(attrs, inner)


# ---------------------------------------------------------------------------
# Authoring: project.xml + MpFolder.xml
# ---------------------------------------------------------------------------
def build_project_xml(name: str, media_pool_db_id: Optional[str] = None) -> str:
    """Build a minimal ``SM_Project`` project.xml carrying the project name.

    This is a structural scaffold for offline round-tripping through
    :func:`read_drp` — it is intentionally minimal and is *not* claimed to
    import into a live Resolve (that calibration lives in the tool layer, which
    reports ``verified: false``).
    """
    db_id = _gen_uuid()
    mp_id = media_pool_db_id or _gen_uuid()
    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<!--DbAppVer="21.0.0.0048" DbPrjVer="17"-->',
            f'<SM_Project DbId="{db_id}">',
            " <FieldsBlob/>",
            f" <ProjectName>{_escape_xml_text(name)}</ProjectName>",
            f" <MediaPoolRef>{mp_id}</MediaPoolRef>",
            " <UpToDate>true</UpToDate>",
            "</SM_Project>",
        ]
    )


def _build_clip_xml(clip: Dict[str, Any]) -> str:
    if not isinstance(clip, dict):
        raise DrpFormatError("media-pool clip entry must be an object")
    kind = str(clip.get("kind") or clip.get("type") or "video").lower()
    tag = _KIND_CLIP_TAG.get(kind, "Sm2MpVideoClip")
    db_id = str(clip.get("dbId") or _gen_uuid())
    unique_id = str(clip.get("uniqueId") or _gen_uuid())
    media_path = clip.get("mediaPath") or clip.get("filePath") or clip.get("sourceFile") or ""
    name = clip.get("name") or (_pp.basename(media_path) if media_path else "Clip")

    rows = [f"  <Name>{_escape_xml_text(str(name))}</Name>"]
    if media_path:
        # The Media-Pool blob is the link of record; the plain text mirrors it.
        rows.append(f"  <ClipPathBlob>{encode_clip_path_blob(str(media_path))}</ClipPathBlob>")
        rows.append(f"  <MediaFilePath>{_escape_xml_text(str(media_path))}</MediaFilePath>")
    rows.append(f"  <UniqueMediaPoolItemId>{unique_id}</UniqueMediaPoolItemId>")
    frame_rate = clip.get("frameRate") or clip.get("fps")
    if frame_rate is not None:
        rows.append(f"  <MediaFrameRate>{drt.encode_frame_rate(frame_rate)}</MediaFrameRate>")
    rows.append("  <FieldsBlob>0000000000000000</FieldsBlob>")
    inner = "\n".join(rows)
    return f' <{tag} DbId="{db_id}">\n{inner}\n </{tag}>'


def _build_folder_xml(folder: Dict[str, Any], indent: int = 0) -> str:
    if not isinstance(folder, dict):
        raise DrpFormatError("media-pool folder entry must be an object")
    db_id = str(folder.get("dbId") or _gen_uuid())
    unique_id = str(folder.get("uniqueId") or _gen_uuid())
    name = str(folder.get("name") or "Folder")
    color_tag = str(folder.get("colorTag") or _DEFAULT_COLOR_TAG)

    sub = folder.get("folders") or folder.get("subfolders") or []
    folder_children = "\n".join(_build_folder_xml(f) for f in sub if isinstance(f, dict))
    folder_vec = f"  <FolderVec>\n{folder_children}\n  </FolderVec>" if folder_children else "  <FolderVec/>"

    clips = folder.get("clips") or []
    clip_children = "\n".join(_build_clip_xml(c) for c in clips)
    media_vec = f"  <MediaVec>\n{clip_children}\n  </MediaVec>" if clip_children else "  <MediaVec/>"

    rows = [
        f"  <Name>{_escape_xml_text(name)}</Name>",
        f"  <UniqueMediaPoolItemId>{unique_id}</UniqueMediaPoolItemId>",
        f"  <ColorTag>{_escape_xml_text(color_tag)}</ColorTag>",
        f"  <Folded>{'true' if folder.get('folded') else 'false'}</Folded>",
        folder_vec,
        media_vec,
    ]
    inner = "\n".join(rows)
    return f'<{_FOLDER_TAG} DbId="{db_id}">\n{inner}\n</{_FOLDER_TAG}>'


def build_mp_folder_xml(media_pool: Dict[str, Any]) -> str:
    """Build a complete ``MpFolder.xml`` document from a media-pool spec.

    Spec: ``{"name": "Master", "clips": [...], "folders": [...]}`` where each
    clip is ``{"name"?, "mediaPath", "kind"?, "frameRate"?}`` and each folder is
    the same shape recursively.
    """
    if not isinstance(media_pool, dict):
        media_pool = {}
    root = dict(media_pool)
    root.setdefault("name", "Master")
    body = _build_folder_xml(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n"


# ---------------------------------------------------------------------------
# Public API — author
# ---------------------------------------------------------------------------
def author_drp(spec: Dict[str, Any]) -> bytes:
    """Author a ``.drp`` ZIP archive from a spec (offline round-trip target).

    Spec shape::

        {
          "name": "My Project",
          "timelines": [ { <drt timeline spec> }, ... ],
          "clips":   [ { "name"?, "mediaPath", "kind"?, "frameRate"? }, ... ],
          "folders": [ { "name", "clips": [...], "folders": [...] }, ... ],
          "metadata": {...},          # -> metadata.json
        }

    Timelines are authored via :func:`drt.build_seq_container_xml`; each is also
    surfaced in the Media Pool as a timeline clip.  Media clips carry the media
    link in the Media-Pool blob (:func:`encode_clip_path_blob`).

    Returns the archive as ``bytes``.
    """
    if not isinstance(spec, dict):
        raise DrpFormatError("author_drp: spec must be an object")

    name = str(spec.get("name") or "Untitled Project")
    timelines = spec.get("timelines") or []
    if not isinstance(timelines, list):
        raise DrpFormatError("author_drp: spec.timelines must be a list")

    members: "Dict[str, bytes]" = {}

    mp_root_id = _gen_uuid()
    members["project.xml"] = build_project_xml(name, mp_root_id).encode("utf-8")

    # Timelines -> SeqContainer members (drt convention read back by drt.parse_drt).
    for idx, timeline in enumerate(timelines):
        xml = drt.build_seq_container_xml(timeline)
        members[f"Primary1/SeqContainer{idx + 1}.xml"] = xml.encode("utf-8")

    # Media Pool root: user clips/folders + one timeline clip per timeline.
    root_clips: List[Dict[str, Any]] = []
    for clip in spec.get("clips") or []:
        if isinstance(clip, dict):
            root_clips.append(clip)
    for timeline in timelines:
        if isinstance(timeline, dict):
            tl_name = timeline.get("name") or timeline.get("timelineName") or "Timeline 1"
            root_clips.append({"kind": "timeline", "name": tl_name})

    media_pool = {
        "name": (spec.get("mediaPool") or {}).get("name", "Master") if isinstance(spec.get("mediaPool"), dict) else "Master",
        "dbId": mp_root_id,
        "clips": root_clips,
        "folders": spec.get("folders") or (spec.get("mediaPool") or {}).get("folders") or [],
    }
    members["MediaPool/Master/MpFolder.xml"] = build_mp_folder_xml(media_pool).encode("utf-8")

    metadata = spec.get("metadata")
    meta_obj: Dict[str, Any] = dict(metadata) if isinstance(metadata, dict) else {}
    meta_obj.setdefault("projectName", name)
    import json

    members["metadata.json"] = json.dumps(meta_obj, indent=2).encode("utf-8")

    return _write_members(members)


# ---------------------------------------------------------------------------
# Public API — read
# ---------------------------------------------------------------------------
def _project_name(members: "Dict[str, bytes]", mp_root: Dict[str, Any]) -> Optional[str]:
    if "metadata.json" in members:
        import json

        try:
            meta = json.loads(members["metadata.json"].decode("utf-8"))
            if isinstance(meta, dict):
                nm = meta.get("projectName") or meta.get("name")
                if nm:
                    return str(nm)
        except (ValueError, UnicodeDecodeError):
            pass
    if "project.xml" in members:
        proj = members["project.xml"].decode("utf-8", "replace")
        nm = _scalar(proj, "ProjectName")
        if nm:
            return nm
    return mp_root.get("name")


def _flatten_clips(folder: Dict[str, Any], path: List[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    here = path + ([folder["name"]] if folder.get("name") else [])
    for clip in folder.get("clips") or []:
        entry = dict(clip)
        entry["folderPath"] = list(here)
        out.append(entry)
    for sub in folder.get("folders") or []:
        out.extend(_flatten_clips(sub, here))
    return out


def read_drp(data: Union[str, bytes, bytearray]) -> Dict[str, Any]:
    """Parse a ``.drp`` archive (path or bytes) into a normalized dict.

    Returns::

        {
          "name": <project name>,
          "folders": [ <subfolder tree under the Master folder> ],
          "clips":   [ <Master-level media-pool clips> ],
          "allClips": [ <every media-pool clip, flattened, with folderPath> ],
          "timelines": [ <drt timeline dicts> ],
          "mediaPoolRoot": { name, dbId, colorTag, folders, clips },
          "seqContainers": [ <member paths> ],
          "hasProjectXml": bool,
        }

    Raises :class:`DrpFormatError` when the archive is unreadable or is missing
    its ``MpFolder.xml`` media-pool document.
    """
    buf = _load_bytes(data)
    members = _read_members(buf)

    mp_member = _find_mp_folder_member(members)
    if mp_member is None:
        raise DrpFormatError(
            "not a valid .drp: no MpFolder.xml media-pool document found "
            "(a .drp must carry its Media Pool)"
        )

    mp_xml = members[mp_member].decode("utf-8", "replace")
    mp_root = parse_mp_folder(mp_xml)

    # Timelines: reuse the drt SeqContainer parser over the same archive bytes.
    try:
        timelines = drt.parse_drt(buf).get("timelines", [])
    except drt.DrtError:
        timelines = []

    seq_members = sorted(n for n in members if drt._SEQ_RE.search(n))

    return {
        "name": _project_name(members, mp_root),
        "folders": mp_root.get("folders", []),
        "clips": mp_root.get("clips", []),
        "allClips": _flatten_clips(mp_root, []),
        "timelines": timelines,
        "mediaPoolRoot": mp_root,
        "mpFolderMember": mp_member,
        "seqContainers": seq_members,
        "hasProjectXml": any(_basename(n) == "project.xml" for n in members),
    }


# ---------------------------------------------------------------------------
# Public API — edit (surgical, preserves other clips/members)
# ---------------------------------------------------------------------------
def _root_media_vec_close(xml: str) -> Optional[Tuple[int, int, bool]]:
    """Locate the *root* folder's MediaVec insertion point.

    The root ``<Sm2MpFolder>`` is the outermost element, so its ``MediaVec`` is
    the last one in the document (it follows the root ``FolderVec`` that
    contains every nested folder).  Returns ``(insert_pos, close_len,
    self_closing)`` — for a self-closing root ``<MediaVec/>`` the whole tag is at
    ``insert_pos``.
    """
    closes = list(re.finditer(r"</MediaVec>", xml))
    if closes:
        last = closes[-1]
        return (last.start(), len("</MediaVec>"), False)
    selfclose = list(re.finditer(r"<MediaVec/>", xml))
    if selfclose:
        last = selfclose[-1]
        return (last.start(), len("<MediaVec/>"), True)
    return None


def _find_clip_block(xml: str, name: Optional[str], db_id: Optional[str]) -> Optional[Tuple[int, int]]:
    for m in _CLIP_TAG_RE.finditer(xml):
        el = _extract_element(xml, m.group(1), m.start())
        if el is None:
            continue
        open_start, _cs, _ce, close_end, inner, attrs = el
        if db_id is not None and _attr(attrs, "DbId") == db_id:
            return (open_start, close_end)
        if name is not None and _scalar(inner, "Name") == name:
            return (open_start, close_end)
    return None


def edit_drp(
    data: Union[str, bytes, bytearray],
    action: str,
    clip: Optional[Dict[str, Any]] = None,
    name: Optional[str] = None,
    dbId: Optional[str] = None,
) -> bytes:
    """Add or remove a Media-Pool clip, preserving every other member and clip.

    Actions:

    * ``add_clip`` — insert ``clip`` (``{"name"?, "mediaPath", "kind"?,
      "frameRate"?}``) into the root Media Pool.  The media link is written into
      the Media-Pool blob (the documented invariant), not just the plain-text
      ``<MediaFilePath>``.  Only the root ``MediaVec`` is touched, so all other
      clips are left byte-for-byte intact.
    * ``remove_clip`` — delete the clip matching ``dbId`` (preferred) or
      ``name`` from the Media Pool, leaving every other clip intact.

    Returns the edited archive as ``bytes``.
    """
    buf = _load_bytes(data)
    members = _read_members(buf)
    mp_member = _find_mp_folder_member(members)
    if mp_member is None:
        raise DrpFormatError("edit_drp: archive has no MpFolder.xml to edit")

    mp_xml = members[mp_member].decode("utf-8", "replace")

    if action == "add_clip":
        if not isinstance(clip, dict):
            raise DrpFormatError("edit_drp add_clip: 'clip' object is required")
        clip_xml = _build_clip_xml(clip)
        target = _root_media_vec_close(mp_xml)
        if target is None:
            raise DrpFormatError("edit_drp add_clip: could not locate root <MediaVec> in MpFolder.xml")
        pos, close_len, self_closing = target
        if self_closing:
            new_xml = mp_xml[:pos] + f"<MediaVec>\n{clip_xml}\n  </MediaVec>" + mp_xml[pos + close_len :]
        else:
            new_xml = mp_xml[:pos] + f"{clip_xml}\n " + mp_xml[pos:]
        members[mp_member] = new_xml.encode("utf-8")
        return _write_members(members)

    if action == "remove_clip":
        if not name and not dbId:
            raise DrpFormatError("edit_drp remove_clip: provide 'dbId' or 'name'")
        block = _find_clip_block(mp_xml, name, dbId)
        if block is None:
            raise DrpFormatError(
                f"edit_drp remove_clip: no media-pool clip matching "
                f"{'dbId=' + dbId if dbId else 'name=' + str(name)}"
            )
        start, end = block
        # Trim a trailing newline/indent left behind, keeping other clips intact.
        tail = end
        while tail < len(mp_xml) and mp_xml[tail] in "\r\n":
            tail += 1
        new_xml = mp_xml[:start] + mp_xml[tail:]
        members[mp_member] = new_xml.encode("utf-8")
        return _write_members(members)

    raise DrpFormatError(f"edit_drp: unknown action {action!r} (expected 'add_clip' or 'remove_clip')")
