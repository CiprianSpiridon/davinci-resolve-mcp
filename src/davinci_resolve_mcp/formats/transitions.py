"""
davinci_resolve_mcp.formats.transitions — native ``<Sm2TiTransition>`` surgeon.

This module injects a *native* Resolve ``<Sm2TiTransition>`` element into the
``<Items>`` clip list of a target ``<Sm2TiTrack>`` inside a ``.drt`` timeline (or
a ``.drp`` project) SeqContainer, **without touching any neighbouring clip's
``<Start>`` / ``<Duration>``** and without disturbing any other archive member.

Like the rest of :mod:`davinci_resolve_mcp.formats`, it never connects to
DaVinci Resolve — it operates purely on file paths / bytes and is safe to import
and use with no Resolve instance running. All ZIP envelope work is delegated to
:mod:`davinci_resolve_mcp.formats.drt` (``_read_members`` / ``_write_members`` /
``_list_seq_members``) so a non-modified member's *decompressed* content
re-parses identically after a write (note: raw-zip bytes are **not** preserved —
``drt._write_members`` re-deflates every member at ``compresslevel=6``).

Transition geometry
-------------------
A centred SMPTE dissolve of ``duration_frames`` at cut frame ``at_frame``:

    half   = floor(duration_frames / 2)
    Start  = at_frame - half                # the transition straddles the cut
    Duration      = duration_frames         # one second of frames by default
    AlignmentType = 2                       # 0=end-on-cut, 1=start-on-cut, 2=centre
    InOffset  = half                        # frames consumed from the outgoing clip
    OutOffset = duration_frames - half      # frames consumed into the incoming clip

The half-duration in/out offsets keep the dissolve centered on the edit, and
the handle-media precondition ensures both adjacent clips can supply the
frames consumed by the transition.

Public API
----------
``inject_native_transition(data, at_frame, ...) -> bytes``
    Inject one ``<Sm2TiTransition>`` and return the rebuilt archive bytes.
``parse_native_transitions(data_or_xml, member=None) -> list[dict]``
    Read back the native transitions present on a SeqContainer.
``DrtError``
    Re-exported from :mod:`.drt`; raised for every bad-input / precondition
    failure (never a bare ``zipfile`` / ``re`` error escapes).
"""

from __future__ import annotations

import re
import zipfile
from typing import Any, Dict, List, Optional, Union

from . import drt as _drt
from .drt import DrtError

__all__ = [
    "DrtError",
    "inject_native_transition",
    "inject_transition_element",
    "parse_native_transitions",
    "read_transition_elements",
]

_Data = Union[str, bytes, bytearray]

# Reuse the Sm2TiTrack DbId shape from drt.py:377 (video/audio/generic track tags).
_TRACK_RE = re.compile(
    r'<(Sm2TiVideoTrack|Sm2TiAudioTrack|Sm2TiTrack)\b[^>]*?DbId="([^"]+)"[^>]*>([\s\S]*?)</\1>'
)
# An <Items> clip list is either empty (<Items/>) or wraps the clip elements.
_ITEMS_RE = re.compile(r"<Items\s*/>|<Items>([\s\S]*?)</Items>")
_VIDEO_VEC_RE = re.compile(r"<VideoTrackVec>([\s\S]*?)</VideoTrackVec>")
# A native transition element, for round-trip read-back.
_TRANSITION_RE = re.compile(
    r'<Sm2TiTransition\b[^>]*?DbId="([^"]+)"[^>]*>([\s\S]*?)</Sm2TiTransition>'
)


def _load_seq_xml(data_or_xml: _Data, member: Optional[str]) -> str:
    """Return the target SeqContainer XML from a path/bytes archive or raw XML.

    Accepts a ``.drt`` / ``.drp`` file path or bytes (the target SeqContainer is
    resolved via :func:`_resolve_member`) or the raw SeqContainer XML text itself.
    """
    if isinstance(data_or_xml, str) and "<Sm2SequenceContainer" in data_or_xml:
        return data_or_xml
    buf = _drt._load_bytes(data_or_xml)
    try:
        members = _drt._read_members(buf)
    except zipfile.BadZipFile as e:
        raise DrtError(f"not a valid .drt/.drp zip: {e}") from e
    target = _resolve_member(members, member)
    return members[target].decode("utf-8", errors="replace")


def _resolve_member(members: "Dict[str, bytes]", member: Optional[str]) -> str:
    """Pick the SeqContainer member to operate on."""
    seq_members = _drt._list_seq_members(members)
    if not seq_members:
        raise DrtError("no SeqContainer*.xml entries found — is this a .drt/.drp?")
    if member is None:
        if len(seq_members) == 1:
            return seq_members[0]
        raise DrtError(
            f"archive has {len(seq_members)} SeqContainers {seq_members}; "
            "pass member= to choose one"
        )
    if member in members:
        if member not in seq_members:
            raise DrtError(f"member {member!r} is not a SeqContainer")
        return member
    matches = [m for m in seq_members if m == member or m.rsplit("/", 1)[-1] == member]
    if not matches:
        raise DrtError(f"no SeqContainer member matching {member!r}")
    if len(matches) > 1:
        raise DrtError(f"ambiguous SeqContainer name {member!r}: {matches}")
    return matches[0]


def _build_transition_xml(
    db_id: str,
    start: int,
    duration: int,
    in_offset: int,
    out_offset: int,
    transition_type: str,
    name: str,
) -> str:
    rows = [
        f"   <Start>{start}</Start>",
        f"   <Duration>{duration}</Duration>",
        "   <AlignmentType>2</AlignmentType>",
        f"   <TransitionType>{_drt._escape_xml_text(transition_type)}</TransitionType>",
        f"   <Name>{_drt._escape_xml_text(name)}</Name>",
        f"   <InOffset>{in_offset}</InOffset>",
        f"   <OutOffset>{out_offset}</OutOffset>",
    ]
    inner = "\n".join(rows)
    return f'  <Sm2TiTransition DbId="{db_id}">\n{inner}\n  </Sm2TiTransition>'


def inject_native_transition(
    data: _Data,
    at_frame: int,
    *,
    track_index: int = 0,
    track_type: str = "video",
    duration_frames: Optional[int] = None,
    member: Optional[str] = None,
    transition_type: str = "SMPTE_Dissolve",
    name: str = "Cross Dissolve",
    require_handles: bool = True,
) -> bytes:
    """Inject one native ``<Sm2TiTransition>`` at a cut and return archive bytes.

    Parameters
    ----------
    data
        A ``.drt`` / ``.drp`` file path or its raw bytes.
    at_frame
        The timeline frame of the cut the transition straddles. Must be an exact
        clip boundary on the target track (a clip ends here *and* a clip starts
        here), else :class:`DrtError` is raised.
    track_index
        Zero-based index into the track vector of ``track_type``.
    track_type
        Must be ``"video"``. Audio transitions are rejected with
        :class:`DrtError` (this surgeon edits the video ``Sm2TiTrack`` only).
    duration_frames
        Transition length. Defaults to one second of frames (rounded frame rate
        of the SeqContainer).
    member
        SeqContainer member to edit (needed only for multi-timeline archives).
    require_handles
        When ``True`` (default) the abutting clips must each carry at least
        ``floor(duration_frames / 2)`` handle frames on the side facing the cut;
        otherwise :class:`DrtError` is raised and no archive is emitted.

    Returns
    -------
    bytes
        The rebuilt archive with the single new transition. Every other clip's
        ``Start`` / ``Duration`` and every other member re-parse identically.
    """
    if not isinstance(at_frame, int) or isinstance(at_frame, bool):
        raise DrtError("at_frame must be an int (timeline frame of the cut)")
    if str(track_type).lower() != "video":
        raise DrtError(
            f"native transitions are only supported on video tracks, not {track_type!r}"
        )
    if not isinstance(track_index, int) or track_index < 0:
        raise DrtError("track_index must be a non-negative int")

    buf = _drt._load_bytes(data)
    try:
        members = _drt._read_members(buf)
    except zipfile.BadZipFile as e:
        raise DrtError(f"not a valid .drt/.drp zip: {e}") from e

    target = _resolve_member(members, member)
    xml = members[target].decode("utf-8", errors="replace")

    parsed = _drt.parse_seq_container(xml, target)
    frame_rate = parsed.get("frameRate") or 24
    if duration_frames is None:
        duration_frames = max(1, round(float(frame_rate)))
    if not isinstance(duration_frames, int) or isinstance(duration_frames, bool):
        raise DrtError("duration_frames must be an int")
    if duration_frames < 2:
        raise DrtError("duration_frames must be >= 2 for a centred transition")

    video_tracks = parsed.get("videoTracks") or []
    if track_index >= len(video_tracks):
        raise DrtError(
            f"video track_index {track_index} out of range "
            f"(archive has {len(video_tracks)} video track(s))"
        )
    clips = video_tracks[track_index].get("clips") or []

    # Locate abutting clips: an outgoing clip ending exactly at the cut and an
    # incoming clip starting exactly at the cut. Never modify either.
    half = duration_frames // 2
    outgoing = None
    incoming = None
    for clip in clips:
        start = clip.get("start")
        duration = clip.get("duration")
        if start is None or duration is None:
            continue
        if start + duration == at_frame:
            outgoing = clip
        if start == at_frame:
            incoming = clip
    if outgoing is None or incoming is None:
        raise DrtError(
            f"frame {at_frame} is not a clip boundary on video track {track_index} "
            "(a transition needs an outgoing clip ending and an incoming clip "
            "starting at the cut)"
        )

    if require_handles:
        problems: List[str] = []
        # Each abutting clip must be long enough on the timeline to host the
        # overlap region without rippling into a third clip.
        if (outgoing.get("duration") or 0) < half:
            problems.append(
                f"outgoing clip is {outgoing.get('duration')} frames < {half} handle frames"
            )
        if (incoming.get("duration") or 0) < half:
            problems.append(
                f"incoming clip is {incoming.get('duration')} frames < {half} handle frames"
            )
        # The incoming clip must supply `half` frames of source media *before*
        # its in-point (leading handles); source frames start at 0.
        inc_in = incoming.get("in")
        if inc_in is not None and inc_in < half:
            problems.append(
                f"incoming clip in-point {inc_in} leaves < {half} leading handle frames"
            )
        if problems:
            raise DrtError(
                "insufficient handle media for a "
                f"{duration_frames}-frame transition at frame {at_frame}: "
                + "; ".join(problems)
            )

    # Find the target video track's raw block within VideoTrackVec (same order
    # as parse_seq_container extracted them), then splice the transition into its
    # <Items> list — nothing else in the document is rewritten.
    vec_m = _VIDEO_VEC_RE.search(xml)
    if not vec_m:
        raise DrtError("SeqContainer has no <VideoTrackVec>")
    vec_offset = vec_m.start(1)
    vec_inner = vec_m.group(1)

    track_matches = list(_TRACK_RE.finditer(vec_inner))
    if track_index >= len(track_matches):
        raise DrtError(f"could not locate raw XML for video track {track_index}")
    tm = track_matches[track_index]
    block = tm.group(0)
    abs_start = vec_offset + tm.start()
    abs_end = vec_offset + tm.end()

    items_m = _ITEMS_RE.search(block)
    if items_m is None or items_m.group(1) is None:
        raise DrtError(
            f"video track {track_index} has no populated <Items> clip list"
        )
    insert_at = items_m.end(1)  # just before the closing </Items>

    start_frame = at_frame - half
    transition_xml = _build_transition_xml(
        db_id=_drt._gen_uuid(),
        start=start_frame,
        duration=duration_frames,
        in_offset=half,
        out_offset=duration_frames - half,
        transition_type=str(transition_type),
        name=str(name),
    )
    new_block = block[:insert_at] + "\n" + transition_xml + block[insert_at:]
    new_xml = xml[:abs_start] + new_block + xml[abs_end:]

    members[target] = new_xml.encode("utf-8")
    return _drt._write_members(members)


def parse_native_transitions(
    data_or_xml: _Data,
    member: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return the native transitions on a SeqContainer.

    ``data_or_xml`` may be a ``.drt`` / ``.drp`` path or bytes, or the raw
    SeqContainer XML text itself. Each entry is
    ``{"dbId", "start", "duration", "alignmentType", "inOffset", "outOffset",
    "transitionType", "name"}``.
    """
    xml = _load_seq_xml(data_or_xml, member)

    out: List[Dict[str, Any]] = []
    for m in _TRANSITION_RE.finditer(xml):
        inner = m.group(2)
        out.append(
            {
                "dbId": m.group(1),
                "start": _drt._as_int(_drt._extract_scalar(inner, "Start")),
                "duration": _drt._as_int(_drt._extract_scalar(inner, "Duration")),
                "alignmentType": _drt._as_int(
                    _drt._extract_scalar(inner, "AlignmentType")
                ),
                "inOffset": _drt._as_int(_drt._extract_scalar(inner, "InOffset")),
                "outOffset": _drt._as_int(_drt._extract_scalar(inner, "OutOffset")),
                "transitionType": _drt._extract_scalar(inner, "TransitionType"),
                "name": _drt._extract_scalar(inner, "Name"),
            }
        )
    return out


def read_transition_elements(
    data_or_xml: _Data,
    member: Optional[str] = None,
) -> List[Dict[str, str]]:
    """Return every ``<Sm2TiTransition>`` on a SeqContainer as raw elements.

    Each entry is ``{"name": <Name text or "">, "dbId": <DbId attr>, "element":
    <verbatim <Sm2TiTransition>…</Sm2TiTransition> XML>}`` in document order.
    Unlike :func:`parse_native_transitions` (which decodes the scalar fields),
    this preserves the *verbatim* element — including the MotionVFX
    ``<FieldsBlob>`` / ``<CompositionTable>`` / empty ``<CompositionBA/>`` — so a
    named MotionVFX transition can be copied wholesale into another timeline's
    ``<Items>`` list (its 2-``MediaIn`` comp does not export standalone).

    ``data_or_xml`` may be a ``.drt`` / ``.drp`` path or bytes, or the raw
    SeqContainer XML text itself.
    """
    xml = _load_seq_xml(data_or_xml, member)
    out: List[Dict[str, str]] = []
    for m in _TRANSITION_RE.finditer(xml):
        inner = m.group(2)
        out.append(
            {
                "name": _drt._extract_scalar(inner, "Name") or "",
                "dbId": m.group(1),
                "element": m.group(0),
            }
        )
    return out


def inject_transition_element(
    data: _Data,
    element_xml: str,
    *,
    track_index: int = 0,
    member: Optional[str] = None,
) -> bytes:
    """Splice a pre-built ``<Sm2TiTransition>`` into a video track's ``<Items>``.

    Copies the target ``.drt`` / ``.drp`` archive and inserts ``element_xml``
    verbatim at the end of the ``track_index`` video track's populated
    ``<Items>`` clip list, then returns the rebuilt archive bytes. When the
    surrounding items use the real-Resolve ``<Element>`` wrapper convention the
    new element is wrapped to match; a tool-authored (bare-child) ``<Items>``
    gets a bare child. Every other clip's ``<Start>`` / ``<Duration>`` and every
    other archive member re-parse identically.

    Shares the ``VideoTrackVec`` / ``Sm2TiTrack`` / ``<Items>`` byte-surgery with
    :func:`inject_native_transition`; unlike that function it injects a
    caller-supplied element and performs **no** cut-boundary or handle-media
    checks — used for MotionVFX *named* transitions, whose 2-``MediaIn`` comp
    cannot be rebuilt offline, so a real element is copied from a reference
    timeline instead.

    Parameters
    ----------
    data
        A ``.drt`` / ``.drp`` file path or its raw bytes.
    element_xml
        A complete ``<Sm2TiTransition>…</Sm2TiTransition>`` element string.
    track_index
        Zero-based index into the video track vector to inject into.
    member
        SeqContainer member to edit (needed only for multi-timeline archives).
    """
    if not isinstance(element_xml, str) or "<Sm2TiTransition" not in element_xml:
        raise DrtError("element_xml must be a <Sm2TiTransition> element string")
    if (
        not isinstance(track_index, int)
        or isinstance(track_index, bool)
        or track_index < 0
    ):
        raise DrtError("track_index must be a non-negative int")

    buf = _drt._load_bytes(data)
    try:
        members = _drt._read_members(buf)
    except zipfile.BadZipFile as e:
        raise DrtError(f"not a valid .drt/.drp zip: {e}") from e

    target = _resolve_member(members, member)
    xml = members[target].decode("utf-8", errors="replace")

    vec_m = _VIDEO_VEC_RE.search(xml)
    if not vec_m:
        raise DrtError("SeqContainer has no <VideoTrackVec>")
    vec_offset = vec_m.start(1)
    vec_inner = vec_m.group(1)

    track_matches = list(_TRACK_RE.finditer(vec_inner))
    if track_index >= len(track_matches):
        raise DrtError(
            f"video track_index {track_index} out of range "
            f"(archive has {len(track_matches)} video track(s))"
        )
    tm = track_matches[track_index]
    block = tm.group(0)
    abs_start = vec_offset + tm.start()
    abs_end = vec_offset + tm.end()

    items_m = _ITEMS_RE.search(block)
    if items_m is None or items_m.group(1) is None:
        raise DrtError(
            f"video track {track_index} has no populated <Items> clip list"
        )
    items_inner = items_m.group(1)
    insert_at = items_m.end(1)  # just before the closing </Items>

    element = element_xml.strip()
    if "<Element>" in items_inner:
        payload = f"\n     <Element>\n      {element}\n     </Element>"
    else:
        payload = f"\n  {element}"
    new_block = block[:insert_at] + payload + block[insert_at:]
    new_xml = xml[:abs_start] + new_block + xml[abs_end:]

    members[target] = new_xml.encode("utf-8")
    return _drt._write_members(members)
