"""
davinci_resolve_mcp.formats.drt — .drt (Resolve Timeline) zip/SeqContainer codec.

A DaVinci Resolve ``.drt`` (and its sibling ``.drp`` project) is a ZIP archive.
A ``.drt`` carries **timeline-only** data: one or more ``SeqContainer`` XML
documents (each describing a timeline — its name, frame rate, start timecode,
resolution and per-track clip lists), an optional ``MpFolder.xml`` media-pool
folder, an optional ``metadata.json``, and — crucially — **no** ``project.xml``
(the presence of ``project.xml`` is what distinguishes a ``.drp`` project from a
``.drt`` timeline archive).

There are two on-disk conventions for the SeqContainer members, both handled:

    * tool-authored:       ``Primary1/SeqContainer1.xml`` (numbered)
    * real Resolve export: ``SeqContainer/<uuid>.xml``     (named by DbId)

This module is deliberately self-contained: it parses/authors the ZIP envelope
and the SeqContainer XML *structure* (tracks + clips, in frames), and it locates
(but does not decode) any grade ``Body`` blob on a clip. It never connects to
DaVinci Resolve — it operates purely on bytes / file paths and is safe to import
with no Resolve instance running.

Public API
----------
``parse_drt(data) -> dict``
    Parse a ``.drt`` (path or bytes) into
    ``{"timelines": [...], "metadata": ..., "seqContainers": [...]}``.
``author_drt(spec) -> bytes``
    Author a ``.drt`` ZIP from a spec ``{"timelines": [...], ...}`` such that
    :func:`parse_drt` reads it back to the same timeline shape (offline
    round-trip).
``validate_drt(data) -> dict``
    Structural integrity check; returns ``{"valid": bool, "errors": [...]}``.
    Flags a ``.drt`` that wrongly contains ``project.xml``, missing
    SeqContainers, and malformed SeqContainer schema.
``inject_seq_container(data, xml, name=None) -> bytes``
    Add/replace one SeqContainer member, preserving all other members' content
    byte-for-byte.
``extract_seq_container(data, name) -> str``
    Return the raw XML text of a single SeqContainer member.
``DrtError``
    Raised for bad input to the authoring/injection helpers (never a bare
    ``zipfile`` / ``struct`` error).
"""

from __future__ import annotations

import io
import os
import re
import struct
import uuid as _uuid
import zipfile
from typing import Any, Dict, List, Optional, Union

__all__ = [
    "DrtError",
    "parse_drt",
    "author_drt",
    "validate_drt",
    "inject_seq_container",
    "extract_seq_container",
    "build_seq_container_xml",
    "parse_seq_container",
]


class DrtError(Exception):
    """Raised when a ``.drt`` cannot be authored/injected from the given input."""


# ---------------------------------------------------------------------------
# SeqContainer member matching
# ---------------------------------------------------------------------------
# Two conventions:
#   - tool-authored:  <folder>/SeqContainer<N>.xml  (e.g. Primary1/SeqContainer1.xml)
#   - real Resolve:   SeqContainer/<uuid>.xml
# Never match MpFolder.xml / project.xml / Gallery.xml.
_SEQ_RE = re.compile(r"(?:^|/)SeqContainer(?:\d*\.xml|/[^/]+\.xml)$")
_PROJECT_RE = re.compile(r"(?:^|/)project\.xml$")

# In/Out points carry a trailing hex metadata field: "frame|hexdouble".
_DEFAULT_IO_HEX = "949999999999c93f"

# Clip tag conventions, keyed by track type. The first entry is the tag emitted
# by :func:`author_drt`; the others are accepted for real Resolve exports.
_CLIP_TAGS = {
    "video": ("Sm2TiVideoClip", "Sm2VideoClip"),
    "audio": ("Sm2TiAudioClip", "Sm2AudioClip"),
}

# Frame rates in a .drt/.drp are stored as a hex-encoded IEEE-754 double
# (little-endian) — e.g. 24.0 -> "0000000000003840". We encode/decode directly
# with ``struct`` so the value round-trips exactly, including fractional rates
# such as 23.976 and 29.97.


# ---------------------------------------------------------------------------
# Small encode/decode helpers
# ---------------------------------------------------------------------------
def _gen_uuid() -> str:
    """A Resolve-style 32-hex-char id (16 random bytes)."""
    return _uuid.uuid4().hex


def _escape_xml_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_xml_attr(value: str) -> str:
    return _escape_xml_text(value).replace('"', "&quot;")


def _unescape_xml(value: str) -> str:
    return (
        value.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
        .replace("&amp;", "&")
    )


def encode_frame_rate(fps: Union[int, float, str]) -> str:
    """Encode a frame rate as Resolve's hex-encoded little-endian double."""
    try:
        f = float(fps)
    except (TypeError, ValueError):
        return "0000000000003840"  # 24.0 fallback
    return struct.pack("<d", f).hex()


def decode_frame_rate(text: Optional[str]) -> Optional[Union[int, float]]:
    """Decode a ``<FrameRate>`` value.

    Accepts either a plain numeric string (``"24"``) or Resolve's 16-hex-char
    little-endian double. Returns a number (int when integral) or ``None``.
    """
    if text is None:
        return None
    s = text.strip()
    if s == "":
        return None
    if re.fullmatch(r"[0-9a-fA-F]{16}", s):
        try:
            val = struct.unpack("<d", bytes.fromhex(s))[0]
        except (ValueError, struct.error):
            return None
    else:
        try:
            val = float(s)
        except ValueError:
            return None
    rounded = round(val, 3)
    if rounded == int(rounded):
        return int(rounded)
    return rounded


def _timecode_to_frames(timecode: str, fps: Union[int, float]) -> int:
    parts = str(timecode).split(":")
    if len(parts) != 4:
        return 0
    try:
        h, m, s, f = (int(p) for p in parts)
    except ValueError:
        return 0
    rate = max(1, round(float(fps)))
    return ((h * 3600 + m * 60 + s) * rate) + f


def _frames_to_timecode(frames: int, fps: Union[int, float]) -> str:
    rate = max(1, round(float(fps)))
    frames = max(0, int(frames))
    f = frames % rate
    total_secs = frames // rate
    s = total_secs % 60
    m = (total_secs // 60) % 60
    h = total_secs // 3600
    return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


def _as_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    v = value.strip()
    if v == "":
        return None
    # In/Out points look like "123|<hex>"; keep only the frame part.
    if "|" in v:
        v = v.split("|", 1)[0]
    try:
        return int(v, 10)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# SeqContainer XML authoring
# ---------------------------------------------------------------------------
def _clip_xml(clip: Dict[str, Any], track_type: str, frame_rate: Union[int, float]) -> str:
    clip_id = str(clip.get("dbId") or clip.get("clipId") or _gen_uuid())
    start = int(clip.get("start") or 0)
    duration = int(clip.get("duration") or 0)
    in_frame = int(clip.get("in") or clip.get("in_frame") or 0)
    out_frame = clip.get("out")
    if out_frame is None:
        out_frame = in_frame + duration
    out_frame = int(out_frame)
    media_path = str(clip.get("mediaFilePath") or clip.get("media_file_path") or "")
    media_ref = str(clip.get("mediaRef") or clip.get("media_ref") or _gen_uuid())
    media_start = int(clip.get("mediaStartTime") or 0)
    media_fps = clip.get("mediaFrameRate") or frame_rate

    rows = [
        f"   <Start>{start}</Start>",
        f"   <Duration>{duration}</Duration>",
        f"   <In>{in_frame}|{_DEFAULT_IO_HEX}</In>",
        f"   <Out>{out_frame}|{_DEFAULT_IO_HEX}</Out>",
        f"   <MediaRef>{_escape_xml_text(media_ref)}</MediaRef>",
        f"   <MediaFilePath>{_escape_xml_text(media_path)}</MediaFilePath>",
        f"   <MediaStartTime>{media_start}</MediaStartTime>",
        f"   <MediaFrameRate>{encode_frame_rate(media_fps)}</MediaFrameRate>",
        "   <IsMarkedForCaching>false</IsMarkedForCaching>",
        "   <IsForceConformed>true</IsForceConformed>",
    ]

    body = clip.get("body") or clip.get("bodyHex")
    if isinstance(body, dict):
        body = body.get("hex")
    if body:
        body_hex = "".join(str(body).split())
        rows.append(f"   <Body>{body_hex}</Body>")

    tag = _CLIP_TAGS[track_type][0]
    inner = "\n".join(rows)
    return f'  <{tag} DbId="{clip_id}">\n{inner}\n  </{tag}>'


def _track_xml(track: Dict[str, Any], track_type: str, frame_rate: Union[int, float]) -> str:
    track_id = str(track.get("dbId") or track.get("trackId") or _gen_uuid())
    type_val = "0" if track_type == "video" else "1"
    clips = track.get("clips") or []
    clip_xml = "\n".join(_clip_xml(c, track_type, frame_rate) for c in clips)
    items = f"   <Items>\n{clip_xml}\n   </Items>" if clip_xml else "   <Items/>"
    rows = [
        "  <FieldsBlob/>",
        f"  <Type>{type_val}</Type>",
        "  <SubType>0</SubType>",
        "  <Flags>0</Flags>",
        f"  <LayersVec>\n{items}\n  </LayersVec>",
    ]
    inner = "\n".join(rows)
    return f' <Sm2TiTrack DbId="{track_id}">\n{inner}\n </Sm2TiTrack>'


def _normalize_tracks(timeline: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Return ``{"video": [...], "audio": [...]}`` from a timeline spec.

    Accepts either a generic ``tracks`` list (each item carrying a ``type``) or
    the explicit ``videoTracks`` / ``audioTracks`` lists (as produced by
    :func:`parse_drt`).
    """
    video: List[Dict[str, Any]] = []
    audio: List[Dict[str, Any]] = []

    if isinstance(timeline.get("tracks"), list):
        for track in timeline["tracks"]:
            if not isinstance(track, dict):
                continue
            ttype = str(track.get("type") or track.get("trackType") or "video").lower()
            if ttype.startswith("audio"):
                audio.append(track)
            else:
                video.append(track)

    for track in timeline.get("videoTracks") or []:
        if isinstance(track, dict):
            video.append(track)
    for track in timeline.get("audioTracks") or []:
        if isinstance(track, dict):
            audio.append(track)

    return {"video": video, "audio": audio}


def build_seq_container_xml(timeline: Dict[str, Any]) -> str:
    """Build a complete SeqContainer XML document for one timeline spec."""
    if not isinstance(timeline, dict):
        raise DrtError("timeline entry must be an object")

    container_id = _gen_uuid()
    frame_rate = timeline.get("frameRate") or timeline.get("frame_rate") or 24
    start_tc = timeline.get("startTimecode") or timeline.get("start_timecode") or "01:00:00:00"
    resolution = timeline.get("resolution") or "1920x1080"

    res_match = re.match(r"\s*(\d+)\s*x\s*(\d+)\s*", str(resolution))
    if res_match:
        width, height = int(res_match.group(1)), int(res_match.group(2))
    else:
        width, height = 1920, 1080

    start_frame = timeline.get("startFrame")
    if start_frame is None:
        start_frame = _timecode_to_frames(start_tc, frame_rate)

    tracks = _normalize_tracks(timeline)
    video_xml = "\n".join(_track_xml(t, "video", frame_rate) for t in tracks["video"])
    audio_xml = "\n".join(_track_xml(t, "audio", frame_rate) for t in tracks["audio"])
    video_vec = f"\n{video_xml}\n " if video_xml else ""
    audio_vec = f"\n{audio_xml}\n " if audio_xml else ""

    name = str(timeline.get("name") or "Timeline 1")

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<Sm2SequenceContainer DbId="{container_id}">',
        f" <Name>{_escape_xml_text(name)}</Name>",
        f" <UniqueId>{container_id}</UniqueId>",
        f" <StartFrame>{int(start_frame)}</StartFrame>",
        f" <StartTC>{_escape_xml_text(str(start_tc))}</StartTC>",
        f" <FrameRate>{encode_frame_rate(frame_rate)}</FrameRate>",
        f" <ResolutionWidth>{width}</ResolutionWidth>",
        f" <ResolutionHeight>{height}</ResolutionHeight>",
        f" <VideoTrackVec>{video_vec}</VideoTrackVec>",
        f" <AudioTrackVec>{audio_vec}</AudioTrackVec>",
        "</Sm2SequenceContainer>",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# SeqContainer XML parsing (regex based, tolerant of Resolve's blob-heavy XML)
# ---------------------------------------------------------------------------
def _extract_scalar(xml: str, tag: str) -> Optional[str]:
    m = re.search(rf"<{tag}>([\s\S]*?)</{tag}>", xml)
    return _unescape_xml(m.group(1).strip()) if m else None


def _extract_clips(track_xml: str, track_type: str) -> List[Dict[str, Any]]:
    clips: List[Dict[str, Any]] = []
    tags = "|".join(re.escape(t) for t in _CLIP_TAGS[track_type])
    clip_re = re.compile(
        rf'<({tags})\b[^>]*?DbId="([^"]+)"[^>]*>([\s\S]*?)</\1>'
    )
    for m in clip_re.finditer(track_xml):
        inner = m.group(3)
        body_hex = None
        body_m = re.search(r"<Body>([0-9a-fA-F\s]*)</Body>", inner)
        if body_m:
            stripped = re.sub(r"[^0-9a-fA-F]", "", body_m.group(1))
            if stripped:
                body_hex = stripped
        clips.append(
            {
                "dbId": m.group(2),
                "start": _as_int(_extract_scalar(inner, "Start")),
                "duration": _as_int(_extract_scalar(inner, "Duration")),
                "in": _as_int(_extract_scalar(inner, "In")),
                "out": _as_int(_extract_scalar(inner, "Out")),
                "mediaFilePath": _extract_scalar(inner, "MediaFilePath"),
                "mediaRef": _extract_scalar(inner, "MediaRef"),
                "bodyHex": body_hex,
            }
        )
    return clips


def _extract_tracks(seq_xml: str, vec_tag: str, track_type: str) -> List[Dict[str, Any]]:
    tracks: List[Dict[str, Any]] = []
    vec_m = re.search(rf"<{vec_tag}>([\s\S]*?)</{vec_tag}>", seq_xml)
    if not vec_m:
        return tracks
    vec_xml = vec_m.group(1)
    track_re = re.compile(
        r'<(Sm2TiVideoTrack|Sm2TiAudioTrack|Sm2TiTrack)\b[^>]*?DbId="([^"]+)"[^>]*>([\s\S]*?)</\1>'
    )
    for tm in track_re.finditer(vec_xml):
        tracks.append(
            {
                "dbId": tm.group(2),
                "type": track_type,
                "clips": _extract_clips(tm.group(3), track_type),
            }
        )
    return tracks


def parse_seq_container(seq_xml: str, member_name: Optional[str] = None) -> Dict[str, Any]:
    """Parse one SeqContainer XML document into a normalized timeline dict."""
    width = _as_int(_extract_scalar(seq_xml, "ResolutionWidth"))
    height = _as_int(_extract_scalar(seq_xml, "ResolutionHeight"))
    resolution = f"{width}x{height}" if width is not None and height is not None else None

    video_tracks = _extract_tracks(seq_xml, "VideoTrackVec", "video")
    audio_tracks = _extract_tracks(seq_xml, "AudioTrackVec", "audio")

    return {
        "name": _extract_scalar(seq_xml, "Name") or member_name or "Timeline 1",
        "member": member_name,
        "frameRate": decode_frame_rate(_extract_scalar(seq_xml, "FrameRate")),
        "startTimecode": _extract_scalar(seq_xml, "StartTC"),
        "startFrame": _as_int(_extract_scalar(seq_xml, "StartFrame")),
        "resolution": resolution,
        "tracks": [*video_tracks, *audio_tracks],
        "videoTracks": video_tracks,
        "audioTracks": audio_tracks,
    }


# ---------------------------------------------------------------------------
# ZIP helpers
# ---------------------------------------------------------------------------
def _load_bytes(data: Union[str, bytes, bytearray]) -> bytes:
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, str):
        if not os.path.isfile(data):
            raise DrtError(f"no such .drt file: {data}")
        with open(data, "rb") as fh:
            return fh.read()
    raise DrtError("expected a file path (str) or raw bytes")


def _read_members(buf: bytes) -> "Dict[str, bytes]":
    """Return an ordered mapping of member name -> content bytes."""
    members: Dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(buf), "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            members[info.filename] = zf.read(info.filename)
    return members


def _write_members(members: "Dict[str, bytes]") -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return out.getvalue()


def _list_seq_members(members: "Dict[str, bytes]") -> List[str]:
    return sorted(name for name in members if _SEQ_RE.search(name))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def author_drt(spec: Dict[str, Any]) -> bytes:
    """Author a ``.drt`` ZIP archive from a spec.

    Spec shape::

        {
          "timelines": [ { "name", "frameRate", "startTimecode", "resolution",
                           "tracks": [ { "type": "video"|"audio",
                                         "clips": [ {start, duration, in,
                                                     mediaFilePath, ...} ] } ],
                           # or explicit "videoTracks"/"audioTracks"
                         }, ... ],
          "mediaPool": {...} | None,   # -> Primary1/MpFolder.xml
          "metadata": {...},           # -> metadata.json
        }

    Returns the archive as ``bytes``. A ``.drt`` never contains ``project.xml``.
    """
    if not isinstance(spec, dict):
        raise DrtError("author_drt: spec must be an object")
    timelines = spec.get("timelines")
    if not isinstance(timelines, list) or len(timelines) == 0:
        raise DrtError("author_drt: spec.timelines must contain at least one timeline")

    members: Dict[str, bytes] = {}

    metadata = spec.get("metadata")
    if isinstance(metadata, dict) and metadata:
        import json

        members["metadata.json"] = json.dumps(metadata, indent=2).encode("utf-8")

    for idx, timeline in enumerate(timelines):
        xml = build_seq_container_xml(timeline)
        members[f"Primary1/SeqContainer{idx + 1}.xml"] = xml.encode("utf-8")

    media_pool = spec.get("mediaPool")
    if isinstance(media_pool, dict) and media_pool:
        import json

        mp_id = _gen_uuid()
        mp_json = _escape_xml_text(json.dumps(media_pool))
        mp_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<Sm2MpFolder DbId="{mp_id}">\n'
            f" <Name>{_escape_xml_text(str(media_pool.get('name', 'Master')))}</Name>\n"
            f" <ClipsJson>{mp_json}</ClipsJson>\n"
            "</Sm2MpFolder>\n"
        )
        members["Primary1/MpFolder.xml"] = mp_xml.encode("utf-8")

    return _write_members(members)


def parse_drt(data: Union[str, bytes, bytearray]) -> Dict[str, Any]:
    """Parse a ``.drt`` archive (path or bytes) into a normalized dict.

    Returns::

        {
          "timelines": [ <timeline dict from parse_seq_container>, ... ],
          "metadata": <metadata.json contents> | None,
          "seqContainers": [ <member path>, ... ],
        }

    Raises :class:`DrtError` when the archive is unreadable or has no
    SeqContainer members.
    """
    buf = _load_bytes(data)
    try:
        members = _read_members(buf)
    except zipfile.BadZipFile as e:
        raise DrtError(f"not a valid .drt/.drp zip: {e}") from e

    seq_members = _list_seq_members(members)
    if not seq_members:
        raise DrtError("no SeqContainer*.xml entries found — is this a .drt/.drp?")

    timelines: List[Dict[str, Any]] = []
    for name in seq_members:
        xml = members[name].decode("utf-8", errors="replace")
        timelines.append(parse_seq_container(xml, name))

    metadata: Optional[Any] = None
    if "metadata.json" in members:
        import json

        try:
            metadata = json.loads(members["metadata.json"].decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            metadata = None

    return {
        "timelines": timelines,
        "metadata": metadata,
        "seqContainers": seq_members,
    }


def validate_drt(data: Union[str, bytes, bytearray]) -> Dict[str, Any]:
    """Structural integrity check for a ``.drt`` archive.

    Returns ``{"valid": bool, "errors": [{"code", "path"?, "message"}]}``.
    Rules: opens as zip; has >= 1 SeqContainer; must NOT contain ``project.xml``;
    each SeqContainer XML has a ``<Sm2SequenceContainer>`` root with ``<Name>``
    and ``<FrameRate>``; flags clips with empty ``<MediaFilePath>``.
    """
    errors: List[Dict[str, Any]] = []
    try:
        buf = _load_bytes(data)
    except DrtError as e:
        return {"valid": False, "errors": [{"code": "read-failed", "message": str(e)}]}

    try:
        members = _read_members(buf)
    except zipfile.BadZipFile as e:
        return {"valid": False, "errors": [{"code": "zip-open", "message": f"failed to open zip: {e}"}]}

    seq_members = _list_seq_members(members)
    if not seq_members:
        errors.append({"code": "no-seq", "message": "must contain at least one SeqContainer*.xml"})

    if any(_PROJECT_RE.search(name) for name in members):
        errors.append(
            {
                "code": "has-project",
                "message": "a .drt must NOT contain project.xml (use the .drp parser for a project archive)",
            }
        )

    for name in seq_members:
        xml = members[name].decode("utf-8", errors="replace")
        if not re.search(r"<Sm2SequenceContainer\b", xml):
            errors.append({"code": "seq-schema", "path": name, "message": "missing <Sm2SequenceContainer> root"})
            continue
        if not re.search(r"<Name>[\s\S]*?</Name>", xml):
            errors.append({"code": "seq-schema", "path": name, "message": "missing required <Name> field"})
        if not re.search(r"<FrameRate>[\s\S]*?</FrameRate>", xml):
            errors.append({"code": "seq-schema", "path": name, "message": "missing required <FrameRate> field"})
        empty = sum(1 for m in re.finditer(r"<MediaFilePath>([\s\S]*?)</MediaFilePath>", xml) if m.group(1).strip() == "")
        if empty:
            errors.append(
                {"code": "orphan-media", "path": name, "message": f"{empty} clip(s) reference empty MediaFilePath"}
            )

    return {"valid": len(errors) == 0, "errors": errors}


def extract_seq_container(data: Union[str, bytes, bytearray], name: str) -> str:
    """Return the raw XML text of a single SeqContainer member.

    ``name`` may be the exact member path (``Primary1/SeqContainer1.xml``) or a
    bare basename that uniquely matches one SeqContainer member.
    """
    buf = _load_bytes(data)
    try:
        members = _read_members(buf)
    except zipfile.BadZipFile as e:
        raise DrtError(f"not a valid .drt/.drp zip: {e}") from e

    if name in members:
        return members[name].decode("utf-8", errors="replace")

    matches = [m for m in _list_seq_members(members) if m == name or m.rsplit("/", 1)[-1] == name]
    if not matches:
        raise DrtError(f"no SeqContainer member matching {name!r}")
    if len(matches) > 1:
        raise DrtError(f"ambiguous SeqContainer name {name!r}: {matches}")
    return members[matches[0]].decode("utf-8", errors="replace")


def inject_seq_container(
    data: Union[str, bytes, bytearray],
    xml: Union[str, bytes],
    name: Optional[str] = None,
) -> bytes:
    """Add or replace one SeqContainer member, preserving every other member.

    All non-target members are copied through with their content bytes intact.
    ``name`` is the member path to write; when omitted a new numbered member
    (``Primary1/SeqContainer<N+1>.xml``) is appended.
    """
    buf = _load_bytes(data)
    try:
        members = _read_members(buf)
    except zipfile.BadZipFile as e:
        raise DrtError(f"not a valid .drt/.drp zip: {e}") from e

    if isinstance(xml, str):
        content = xml.encode("utf-8")
    elif isinstance(xml, (bytes, bytearray)):
        content = bytes(xml)
    else:
        raise DrtError("inject_seq_container: xml must be str or bytes")

    if name is None:
        existing = _list_seq_members(members)
        name = f"Primary1/SeqContainer{len(existing) + 1}.xml"
    elif not _SEQ_RE.search(name):
        raise DrtError(
            f"member name {name!r} is not a valid SeqContainer path "
            "(expected .../SeqContainer<N>.xml or SeqContainer/<id>.xml)"
        )

    members[name] = content
    return _write_members(members)
