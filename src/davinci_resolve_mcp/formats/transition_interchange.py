"""
davinci_resolve_mcp.formats.transition_interchange — dissolve/crossfade writers.

Pure text/XML emitters that author a *portable, importable* timeline carrying
dissolves and audio cross-fades. The output of these functions is meant to be
fed straight to the existing ``import_timeline_from_file`` tool
(:mod:`davinci_resolve_mcp.tools.media_pool`) so a dissolve authored offline
lands in Resolve as a real SMPTE dissolve / Cross Dissolve transition.

Four interchange flavours are supported:

    * :func:`author_dissolve_otio`   — OpenTimelineIO ``.otio`` JSON, one
      ``Transition.1`` (``SMPTE_Dissolve``) per cut, centered with
      ``in_offset``/``out_offset`` each equal to half the dissolve duration.
      (``buildTransition`` / ``buildTrack``).
    * :func:`author_dissolve_fcpxml` — Final Cut Pro X ``.fcpxml`` (version 1.9)
      carrying a ``<transition>`` named ``Cross Dissolve``.
    * :func:`author_dissolve_edl`    — CMX3600 ``.edl`` with a classic
      ``D nnn`` dissolve event pair per cut.
    * :func:`author_audio_crossfade` — an OTIO audio-track cross-fade; the track
      ``kind`` is ``Audio`` (inferred from track placement).

The AAF video-transition parameter shape (a centered dissolve authored from a
pair of neighbouring clips) was cross-checked against
(``_transition_parameters``).

These are **pure functions** — no ``DaVinciResolveScript`` import, no network,
no filesystem side effects. Every writer takes a ``cuts`` list, where each cut
describes one dissolve between an outgoing (``before``) and incoming (``after``)
clip::

    {
        "before": {"name": "A.mov", "duration": 48, "start": 0, "url": "A.mov"},
        "after":  {"name": "B.mov", "duration": 48, "start": 0, "url": "B.mov"},
        "duration": 12,   # dissolve length, in frames
    }

Clip aliases accepted: ``before``/``outgoing``/``a`` and ``after``/``incoming``/
``b``; dissolve-length aliases: ``duration``/``dissolve``/``frames``. A clip may
also be given as a bare ``str`` (its name).

Public API
----------
``author_dissolve_otio(cuts, fps=24, kind="video") -> str``
``author_dissolve_fcpxml(cuts, fps=24, kind="video") -> str``
``author_dissolve_edl(cuts, fps=24, title="TIMELINE") -> str``
``author_audio_crossfade(cuts, fps=24, kind="audio") -> str``
``TransitionInterchangeError``
    Raised for bad input (empty cuts, or a cut missing its preceding/following
    clip) instead of emitting an empty or invalid timeline.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Sequence, Union

__all__ = [
    "TransitionInterchangeError",
    "author_dissolve_otio",
    "author_dissolve_fcpxml",
    "author_dissolve_edl",
    "author_audio_crossfade",
]

# Reasonable fallbacks when a clip/cut omits explicit frame counts.
_DEFAULT_CLIP_FRAMES = 48
_DEFAULT_DISSOLVE_FRAMES = 12


class TransitionInterchangeError(Exception):
    """Raised for invalid transition-authoring input (never a bare KeyError)."""


# --------------------------------------------------------------------------- #
# Numeric / time helpers
# --------------------------------------------------------------------------- #
def _num(value: Union[int, float]) -> Union[int, float]:
    """Collapse a whole float to int (so 6.0 serialises as ``6``)."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _rt(value: Union[int, float], fps: float) -> Dict[str, Any]:
    """An OTIO ``RationalTime.1`` in the timeline frame rate."""
    return {"OTIO_SCHEMA": "RationalTime.1", "rate": float(fps), "value": _num(value)}


def _tr(start: Union[int, float], duration: Union[int, float], fps: float) -> Dict[str, Any]:
    """An OTIO ``TimeRange.1``."""
    return {
        "OTIO_SCHEMA": "TimeRange.1",
        "start_time": _rt(start, fps),
        "duration": _rt(duration, fps),
    }


def _frames_to_tc(frames: int, fps: Union[int, float]) -> str:
    """Frames -> ``HH:MM:SS:FF`` (non-drop), rounding fps to the tc base."""
    base = max(1, int(round(fps)))
    frames = max(0, int(frames))
    ff = frames % base
    total_secs = frames // base
    ss = total_secs % 60
    mm = (total_secs // 60) % 60
    hh = (total_secs // 3600) % 24
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


# --------------------------------------------------------------------------- #
# Input normalisation / validation
# --------------------------------------------------------------------------- #
def _first(mapping: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _norm_clip(raw: Any, *, role: str, index: int) -> Optional[Dict[str, Any]]:
    """Normalise a clip spec to ``{name, duration, start, url}``; None if absent."""
    if raw is None:
        return None
    if isinstance(raw, str):
        name = raw.strip()
        if not name:
            return None
        return {"name": name, "duration": None, "start": 0, "url": None}
    if isinstance(raw, dict):
        name = _first(raw, "name", "clipName", "clip_name")
        duration = _first(raw, "duration", "frames", "length")
        start = _first(raw, "start", "sourceStart", "source_start", "startFrame")
        url = _first(raw, "url", "path", "targetUrl", "target_url", "file")
        return {
            "name": str(name) if name is not None else "",
            "duration": duration,
            "start": start if start is not None else 0,
            "url": str(url) if url is not None else None,
        }
    raise TransitionInterchangeError(
        f"cut #{index}: {role} clip must be a str or dict, got {type(raw).__name__}"
    )


def _norm_cut(raw: Any, index: int) -> Dict[str, Any]:
    """Validate & normalise one cut into ``{before, after, dissolve}``.

    Raises :class:`TransitionInterchangeError` if the cut lacks a preceding
    (``before``) or following (``after``) clip — a transition needs both.
    """
    if not isinstance(raw, dict):
        raise TransitionInterchangeError(
            f"cut #{index} must be a dict, got {type(raw).__name__}"
        )
    before = _norm_clip(_first(raw, "before", "outgoing", "a", "from"), role="preceding", index=index)
    after = _norm_clip(_first(raw, "after", "incoming", "b", "to"), role="following", index=index)
    if before is None:
        raise TransitionInterchangeError(
            f"cut #{index}: missing the preceding ('before'/'outgoing') clip; "
            "a dissolve requires a clip on both sides"
        )
    if after is None:
        raise TransitionInterchangeError(
            f"cut #{index}: missing the following ('after'/'incoming') clip; "
            "a dissolve requires a clip on both sides"
        )
    dissolve = _first(raw, "duration", "dissolve", "transitionDuration", "transition_duration", "frames")
    try:
        dissolve = int(dissolve) if dissolve is not None else _DEFAULT_DISSOLVE_FRAMES
    except (TypeError, ValueError):
        raise TransitionInterchangeError(
            f"cut #{index}: dissolve duration must be an integer frame count"
        )
    if dissolve <= 0:
        raise TransitionInterchangeError(
            f"cut #{index}: dissolve duration must be a positive number of frames"
        )
    return {"before": before, "after": after, "dissolve": dissolve}


def _normalize_cuts(cuts: Any) -> List[Dict[str, Any]]:
    if cuts is None or not isinstance(cuts, (list, tuple)):
        raise TransitionInterchangeError("cuts must be a non-empty list of cut dicts")
    if len(cuts) == 0:
        raise TransitionInterchangeError(
            "cuts is empty: refusing to author an empty/invalid timeline"
        )
    return [_norm_cut(cut, i) for i, cut in enumerate(cuts)]


def _clip_frames(clip: Dict[str, Any]) -> int:
    dur = clip.get("duration")
    try:
        dur = int(dur) if dur is not None else _DEFAULT_CLIP_FRAMES
    except (TypeError, ValueError):
        dur = _DEFAULT_CLIP_FRAMES
    return max(1, dur)


def _clip_start(clip: Dict[str, Any]) -> int:
    start = clip.get("start")
    try:
        return int(start) if start is not None else 0
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- #
# OTIO writers
# --------------------------------------------------------------------------- #
def _otio_clip(clip: Dict[str, Any], fps: float) -> Dict[str, Any]:
    start = _clip_start(clip)
    frames = _clip_frames(clip)
    url = clip.get("url")
    if url:
        media_ref: Dict[str, Any] = {
            "OTIO_SCHEMA": "ExternalReference.1",
            "target_url": url,
            "available_range": None,
            "metadata": {},
            "name": clip.get("name") or "",
        }
    else:
        media_ref = {
            "OTIO_SCHEMA": "MissingReference.1",
            "metadata": {},
            "name": clip.get("name") or "",
        }
    return {
        "OTIO_SCHEMA": "Clip.2",
        "name": clip.get("name") or "",
        "source_range": _tr(start, frames, fps),
        "media_references": {"DEFAULT_MEDIA": media_ref},
        "active_media_reference_key": "DEFAULT_MEDIA",
        "effects": [],
        "markers": [],
        "metadata": {},
        "enabled": True,
        "color": None,
    }


def _otio_transition(dissolve: int, fps: float, transition_type: str = "SMPTE_Dissolve") -> Dict[str, Any]:

    ``in_offset`` and ``out_offset`` are each half the dissolve duration, so the
    transition straddles the cut symmetrically.
    """
    half = dissolve / 2.0
    return {
        "OTIO_SCHEMA": "Transition.1",
        "name": transition_type,
        "transition_type": transition_type,
        "in_offset": _rt(half, fps),
        "out_offset": _rt(half, fps),
        "metadata": {},
    }


def _resolve_track_kind(kind: Optional[str]) -> str:
    return "Audio" if str(kind or "video").strip().lower() == "audio" else "Video"


def _build_otio(
    cuts: List[Dict[str, Any]],
    fps: float,
    kind: str,
    *,
    name: str,
    transition_type: str,
) -> Dict[str, Any]:
    """Assemble the full OTIO ``Timeline.1`` document (dict, ready to dump)."""
    track_kind = _resolve_track_kind(kind)
    children: List[Dict[str, Any]] = []
    for cut in cuts:
        children.append(_otio_clip(cut["before"], fps))
        children.append(_otio_transition(cut["dissolve"], fps, transition_type))
        children.append(_otio_clip(cut["after"], fps))
    track = {
        "OTIO_SCHEMA": "Track.1",
        "name": f"{track_kind} Track",
        "kind": track_kind,
        "children": children,
        "source_range": None,
        "effects": [],
        "markers": [],
        "metadata": {},
        "enabled": True,
        "color": None,
    }
    return {
        "OTIO_SCHEMA": "Timeline.1",
        "name": name,
        "global_start_time": _rt(0, fps),
        "tracks": {
            "OTIO_SCHEMA": "Stack.1",
            "name": "tracks",
            "children": [track],
            "source_range": None,
            "effects": [],
            "markers": [],
            "metadata": {},
            "enabled": True,
            "color": None,
        },
        "metadata": {},
    }


def author_dissolve_otio(
    cuts: Sequence[Any],
    fps: Union[int, float] = 24,
    kind: str = "video",
    *,
    name: str = "Dissolves",
) -> str:
    """Author an OpenTimelineIO ``.otio`` JSON string carrying dissolves.

    One ``Transition.1`` (``SMPTE_Dissolve``) is emitted per cut, centered so
    ``in_offset`` and ``out_offset`` are each half the dissolve duration.

    Raises :class:`TransitionInterchangeError` when ``cuts`` is empty or a cut
    lacks its preceding/following clip.
    """
    norm = _normalize_cuts(cuts)
    doc = _build_otio(norm, float(fps), kind, name=name, transition_type="SMPTE_Dissolve")
    return json.dumps(doc, indent=2)


def author_audio_crossfade(
    cuts: Sequence[Any],
    fps: Union[int, float] = 24,
    kind: str = "audio",
    *,
    name: str = "Audio Crossfade",
) -> str:
    """Author an OTIO audio-track cross-fade string.

    The transition kind is inferred from track placement: with ``kind='audio'``
    the emitted OTIO ``Track.1`` carries ``kind == 'Audio'``. A cross-fade is
    modelled as an ``SMPTE_Dissolve`` transition on the audio track (the same
    centered shape used for a video dissolve).

    Raises :class:`TransitionInterchangeError` on empty ``cuts`` or a cut
    missing a neighbouring clip.
    """
    norm = _normalize_cuts(cuts)
    # kind is inferred here: audio placement => Audio track.
    track_kind = "audio" if str(kind or "audio").strip().lower() != "video" else "audio"
    doc = _build_otio(norm, float(fps), track_kind, name=name, transition_type="SMPTE_Dissolve")
    return json.dumps(doc, indent=2)


# --------------------------------------------------------------------------- #
# FCPXML writer
# --------------------------------------------------------------------------- #
def _fcp_time(frames: int, fps: Union[int, float]) -> str:
    """FCPXML rational time, e.g. ``12/24s`` (``0s`` for zero)."""
    frames = int(frames)
    if frames == 0:
        return "0s"
    base = max(1, int(round(fps)))
    return f"{frames}/{base}s"


def author_dissolve_fcpxml(
    cuts: Sequence[Any],
    fps: Union[int, float] = 24,
    kind: str = "video",
    *,
    name: str = "Dissolves",
) -> str:
    """Author a well-formed FCPXML (v1.9) with ``<transition>`` Cross Dissolve.

    Raises :class:`TransitionInterchangeError` on empty ``cuts`` or a cut
    missing a neighbouring clip.
    """
    norm = _normalize_cuts(cuts)
    base = max(1, int(round(fps)))
    is_audio = _resolve_track_kind(kind) == "Audio"

    fcpxml = ET.Element("fcpxml", {"version": "1.9"})
    resources = ET.SubElement(fcpxml, "resources")
    ET.SubElement(
        resources,
        "format",
        {
            "id": "r1",
            "name": "FFVideoFormat1080p24",
            "frameDuration": f"1/{base}s",
            "width": "1920",
            "height": "1080",
        },
    )

    # One asset per distinct clip role, laid out on the spine.
    spine_items: List[Dict[str, Any]] = []
    asset_id = 1
    offset = 0
    for cut in norm:
        for role in ("before", "after"):
            clip = cut[role]
            frames = _clip_frames(clip)
            asset_id += 1
            aid = f"a{asset_id}"
            ET.SubElement(
                resources,
                "asset",
                {
                    "id": aid,
                    "name": clip.get("name") or "clip",
                    "start": _fcp_time(_clip_start(clip), fps),
                    "duration": _fcp_time(frames, fps),
                    "hasVideo": "0" if is_audio else "1",
                    "hasAudio": "1" if is_audio else "0",
                    "format": "r1",
                },
            )
            spine_items.append({"ref": aid, "clip": clip, "frames": frames, "offset": offset})
            offset += frames
        # Attach the dissolve to the BEFORE clip ([-2]); the "after" clip is
        # [-1]. The offset formula item["offset"] + item["frames"] - half then
        # centres the <transition> on the cut and emits it between the two
        # <asset-clip> elements.
        spine_items[-2]["dissolve"] = cut["dissolve"]

    library = ET.SubElement(fcpxml, "library")
    event = ET.SubElement(library, "event", {"name": name})
    project = ET.SubElement(event, "project", {"name": name})
    total = sum(item["frames"] for item in spine_items)
    sequence = ET.SubElement(
        project,
        "sequence",
        {"format": "r1", "duration": _fcp_time(total, fps), "tcStart": "0s", "tcFormat": "NDF"},
    )
    spine = ET.SubElement(sequence, "spine")

    for item in spine_items:
        ac_tag = "asset-clip"
        ac = ET.SubElement(
            spine,
            ac_tag,
            {
                "ref": item["ref"],
                "name": item["clip"].get("name") or "clip",
                "offset": _fcp_time(item["offset"], fps),
                "duration": _fcp_time(item["frames"], fps),
                "start": _fcp_time(_clip_start(item["clip"]), fps),
            },
        )
        dissolve = item.get("dissolve")
        if dissolve:
            half = dissolve // 2
            trans = ET.SubElement(
                spine,
                "transition",
                {
                    "name": "Cross Dissolve",
                    "offset": _fcp_time(max(0, item["offset"] + item["frames"] - half), fps),
                    "duration": _fcp_time(dissolve, fps),
                },
            )
            ET.SubElement(trans, "filter-video", {"name": "Cross Dissolve"})

    body = ET.tostring(fcpxml, encoding="unicode")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!DOCTYPE fcpxml>\n"
        + body
    )


# --------------------------------------------------------------------------- #
# CMX3600 EDL writer
# --------------------------------------------------------------------------- #
def _edl_reel(clip: Dict[str, Any], fallback: str) -> str:
    name = clip.get("name") or ""
    reel = "".join(ch for ch in name.upper() if ch.isalnum())[:8]
    return reel or fallback


def author_dissolve_edl(
    cuts: Sequence[Any],
    fps: Union[int, float] = 24,
    title: str = "TIMELINE",
) -> str:
    """Author a CMX3600 ``.edl`` string with ``D nnn`` dissolve event pairs.

    Each cut emits a cut (``C``) event for the outgoing clip followed by a
    dissolve (``D nnn``) event for the incoming clip, where ``nnn`` is the
    zero-padded dissolve length in frames.

    Raises :class:`TransitionInterchangeError` on empty ``cuts`` or a cut
    missing a neighbouring clip.
    """
    norm = _normalize_cuts(cuts)
    lines: List[str] = [f"TITLE: {title}", "FCM: NON-DROP FRAME", ""]
    event = 0
    rec = 0  # running record-side frame position
    for cut in norm:
        before = cut["before"]
        after = cut["after"]
        dissolve = cut["dissolve"]
        b_frames = _clip_frames(before)
        a_frames = _clip_frames(after)
        b_start = _clip_start(before)
        a_start = _clip_start(after)

        # Outgoing clip — hard cut.
        event += 1
        rec_in = rec
        rec_out = rec + b_frames
        lines.append(
            f"{event:03d}  {_edl_reel(before, 'AX'):<8} V     C        "
            f"{_frames_to_tc(b_start, fps)} {_frames_to_tc(b_start + b_frames, fps)} "
            f"{_frames_to_tc(rec_in, fps)} {_frames_to_tc(rec_out, fps)}"
        )

        # Incoming clip — dissolve over `dissolve` frames.
        event += 1
        d_rec_in = rec_out
        d_rec_out = rec_out + a_frames
        lines.append(
            f"{event:03d}  {_edl_reel(after, 'BX'):<8} V     D    {dissolve:03d} "
            f"{_frames_to_tc(a_start, fps)} {_frames_to_tc(a_start + a_frames, fps)} "
            f"{_frames_to_tc(d_rec_in, fps)} {_frames_to_tc(d_rec_out, fps)}"
        )
        rec = d_rec_out
    lines.append("")
    return "\n".join(lines)
