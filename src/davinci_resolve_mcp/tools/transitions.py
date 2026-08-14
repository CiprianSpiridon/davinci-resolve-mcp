"""``transitions`` — place, interchange, and keystroke-hybrid transition tools.

This module owns the "author a transition" surface of the server and mixes
three delivery strategies behind five thin ``@mcp.tool()`` functions:

- Offline byte-surgery (``place_transition``) — injects a *native*
  ``<Sm2TiTransition>`` straight into a ``.drt``/``.drp`` SeqContainer via
  :mod:`davinci_resolve_mcp.formats.transitions` and writes a byte-patched
  archive. No Resolve is touched; the geometry (centred dissolve straddling
  the cut, half-duration in/out offsets, handle-media precondition) is the
  one enforced in the formats layer: only ``SMPTE_Dissolve`` is supported,
  the dissolve straddles the edit, and both sides must provide handle media.
- Interchange authoring (``author_transition_interchange`` /
  ``author_audio_crossfade_interchange``) — writes an *importable* timeline
  file (OpenTimelineIO ``.otio`` by default, or FCPXML/EDL) carrying dissolves
  or an audio cross-fade, using
  :mod:`davinci_resolve_mcp.formats.transition_interchange`. The result is
  meant to be fed to the live ``import_timeline_from_file`` tool. Still no
  Resolve required to author the file.
- Live keystroke hybrid (``add_default_transition_at_cut`` /
  ``add_default_audio_transition_at_cut``) — moves the LIVE playhead via
  ``Timeline.SetCurrentTimecode`` (reached lazily through ``_conn()``), then
  fires DaVinci Resolve's default "add transition" shortcut (Cmd/Ctrl+T for
  video, Shift+T for audio) through ``osascript`` (macOS) or ``pyautogui``.
  There is **no scripted undo** for this path and it requires the Edit page to
  have GUI focus. Each of these tools captures a pre/post timeline signature
  (per-track item count + the start/duration edit points around the cut, via
  ``Timeline.GetItemListInTrack``) and only reports ``verified: true`` when the
  signature actually changes; an unchanged signature comes back with
  ``verified: false`` and a "keystroke may not have registered" note rather
  than silently claiming success. See the Failure-Modes contract entry.

House pattern: ``from ..app import mcp``; helpers reached via
``..helpers`` (``_check_choice``/``_conn``); every tool returns a ``str`` and
catches every exception into an ``"Error: ..."`` string — nothing here raises
out of a tool, and nothing imports ``DaVinciResolveScript`` or connects at
import time.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from typing import Any, Dict, List, Optional, Tuple

from ..app import mcp
from ..formats import drt as drt_fmt
from ..formats import transition_interchange as ti_fmt
from ..formats import transitions as transitions_fmt
from ..helpers import _check_choice, _conn

# Friendly transition_type -> (native <TransitionType> string, display Name).
# The native surgeon only models a centred dissolve (AlignmentType=2); the
# ``TransitionType`` string is carried through verbatim so Resolve labels it.
_TRANSITION_TYPES: Dict[str, Tuple[str, str]] = {
    "cross-dissolve": ("SMPTE_Dissolve", "Cross Dissolve"),
    "dissolve": ("SMPTE_Dissolve", "Cross Dissolve"),
    "smpte-dissolve": ("SMPTE_Dissolve", "Cross Dissolve"),
    "additive-dissolve": ("SMPTE_Dissolve", "Additive Dissolve"),
    "film-dissolve": ("SMPTE_Dissolve", "Film Dissolve"),
    "dip-to-color": ("SMPTE_Dissolve", "Dip To Color Dissolve"),
}

# Interchange formats each writer flavour supports.
_VIDEO_FORMATS = ("otio", "fcpxml", "edl")
_AUDIO_FORMATS = ("otio",)
_ALIGNMENTS = ("center", "start", "end")


# --------------------------------------------------------------------------- #
# Small local helpers (no Resolve import; pure functions)
# --------------------------------------------------------------------------- #
def _default_output(file_path: str) -> str:
    """Derive a ``*.transition.<ext>`` output path next to ``file_path``."""
    base, ext = os.path.splitext(file_path)
    return f"{base}.transition{ext or '.drt'}"


def _motionvfx_default_output(file_path: str) -> str:
    """Derive a ``*-motionvfx.<ext>`` output path next to ``file_path``."""
    base, ext = os.path.splitext(file_path)
    return f"{base}-motionvfx{ext or '.drt'}"


def _parse_cuts(cuts: Any) -> List[Any]:
    """Coerce ``cuts`` (a JSON-array string, a single dict, or a list) to a list.

    Over MCP, structured arguments usually arrive as a JSON string; accept that,
    a bare list/tuple, or a single cut dict. Raises ``ValueError`` (caught by the
    tool body -> "Error: ...") on anything else; empty-list validation is left to
    the interchange writers so their richer message is preserved.
    """
    if cuts is None:
        raise ValueError("cuts is required (a JSON array of cut objects)")
    if isinstance(cuts, (list, tuple)):
        return list(cuts)
    if isinstance(cuts, dict):
        return [cuts]
    if isinstance(cuts, str):
        text = cuts.strip()
        if not text:
            raise ValueError("cuts is required (a JSON array of cut objects)")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"cuts is not valid JSON: {e}") from e
        if isinstance(data, dict):
            return [data]
        if not isinstance(data, list):
            raise ValueError("cuts must decode to a JSON array of cut objects")
        return data
    raise ValueError(
        f"cuts must be a JSON array string, a list, or a dict, got {type(cuts).__name__}"
    )


def _timeline_fps(timeline: Any) -> float:
    """Best-effort timeline frame rate (defaults to 24 when unavailable)."""
    try:
        fps = float(timeline.GetSetting("timelineFrameRate"))
    except Exception:  # noqa: BLE001
        fps = 0.0
    return fps if fps > 0 else 24.0


def _tc_to_frames(timecode: str, fps: float) -> int:
    base = max(1, int(round(fps)))
    parts = str(timecode).replace(";", ":").split(":")
    if len(parts) != 4:
        raise ValueError(f"malformed timecode {timecode!r} (expected HH:MM:SS:FF)")
    hh, mm, ss, ff = (int(p) for p in parts)
    return ((hh * 3600 + mm * 60 + ss) * base) + ff


def _frames_to_tc(frames: int, fps: float) -> str:
    base = max(1, int(round(fps)))
    frames = max(0, int(frames))
    ff = frames % base
    total = frames // base
    ss = total % 60
    mm = (total // 60) % 60
    hh = (total // 3600) % 24
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def _resolve_timecode(timeline: Any, value: Any) -> str:
    """Turn ``value`` into a timeline timecode string.

    A string containing ``:`` is treated as an already-formed timecode. A bare
    integer/float is treated as a timeline-relative frame and converted using the
    live timeline's start timecode + frame rate.
    """
    text = str(value).strip()
    if ":" in text:
        return text
    frame = int(float(text))
    fps = _timeline_fps(timeline)
    start_tc = None
    try:
        start_tc = timeline.GetStartTimecode()
    except Exception:  # noqa: BLE001
        start_tc = None
    start_frame = 0
    if start_tc:
        try:
            start_frame = _tc_to_frames(start_tc, fps)
        except Exception:  # noqa: BLE001
            start_frame = 0
    return _frames_to_tc(start_frame + frame, fps)


def _capture_signature(timeline: Any, track_types: Tuple[str, ...]) -> Dict[str, Any]:
    """Snapshot per-track item count + (start, duration) edit points.

    Used to tell whether the keystroke actually landed a transition/edit: a real
    default transition overlaps the abutting clips, which shifts their reported
    start/duration (and can change item counts), so a changed signature is the
    verification signal.
    """
    sig: Dict[str, Any] = {}
    for tt in track_types:
        try:
            count = int(timeline.GetTrackCount(tt) or 0)
        except Exception:  # noqa: BLE001
            count = 0
        tracks: Dict[int, Any] = {}
        for ti in range(1, count + 1):
            try:
                items = timeline.GetItemListInTrack(tt, ti) or []
            except Exception:  # noqa: BLE001
                items = []
            edits: List[Tuple[Optional[int], Optional[int]]] = []
            for it in items:
                try:
                    edits.append((it.GetStart(), it.GetDuration()))
                except Exception:  # noqa: BLE001
                    edits.append((None, None))
            tracks[ti] = {"count": len(items), "edits": edits}
        sig[tt] = tracks
    return sig


def _fire_default_transition_keystroke(is_audio: bool) -> str:
    """Fire Resolve's default add-transition shortcut; raise if no backend.

    Cmd+T (video) / Shift+T (audio) on macOS via ``osascript``; Ctrl+T / Shift+T
    elsewhere via ``pyautogui``. Raises ``RuntimeError`` with an actionable
    message when neither keystroke backend is available or the send fails, so the
    caller reports a clean "Error: ..." string.
    """
    if sys.platform == "darwin":
        modifier = "shift down" if is_audio else "command down"
        script = (
            'tell application "System Events" to keystroke "t" using {'
            f"{modifier}"
            "}"
        )
        try:
            proc = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError as e:
            raise RuntimeError(
                "no keystroke backend: 'osascript' not found on PATH"
            ) from e
        except subprocess.SubprocessError as e:
            raise RuntimeError(f"osascript keystroke failed to run: {e}") from e
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip() or f"exit {proc.returncode}"
            raise RuntimeError(
                "osascript keystroke was rejected "
                f"({detail}) — grant Accessibility permission to the controlling "
                "app and give the Edit page GUI focus"
            )
        return "osascript"

    try:
        import pyautogui  # type: ignore
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            "no keystroke backend available: 'pyautogui' is not installed and "
            "'osascript' is macOS-only — install pyautogui or run on macOS with "
            "an Edit-page-focused DaVinci Resolve"
        ) from e
    keys = ("shift", "t") if is_audio else ("ctrl", "t")
    try:
        pyautogui.hotkey(*keys)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"pyautogui keystroke failed: {e}") from e
    return "pyautogui"


# --------------------------------------------------------------------------- #
# (1) Offline byte-surgery
# --------------------------------------------------------------------------- #
@mcp.tool()
def place_transition(
    file_path: str = "",
    track: int = 1,
    at_frame: int = 0,
    duration_frames: int = 0,
    transition_type: str = "cross-dissolve",
    track_type: str = "video",
    output_path: str = "",
) -> str:
    """Inject a native transition into a ``.drt``/``.drp`` offline; write a byte-patched copy.

    Injects one ``<Sm2TiTransition>`` at ``at_frame`` (which must be an exact
    clip boundary on the target track — an outgoing clip ends and an incoming
    clip starts there) into a copy of ``file_path``, without moving any
    neighbouring clip's start/duration, and writes the rebuilt archive to
    ``output_path``. Never connects to DaVinci Resolve.

    Parameters:
    - file_path: source ``.drt``/``.drp`` file on disk.
    - track: 1-based video track index (Edit-page numbering). Converted to the
      0-based index the surgeon expects.
    - at_frame: timeline frame of the cut the transition straddles.
    - duration_frames: transition length in frames (>= 2); 0 means "auto"
      (one second of frames at the timeline rate).
    - transition_type: one of cross-dissolve, dissolve, smpte-dissolve,
      additive-dissolve, film-dissolve, dip-to-color (validated).
    - track_type: only ``"video"`` is supported for native injection; anything
      else comes back as an "Error: ..." string.
    - output_path: destination file; defaults to ``<file_path>.transition.<ext>``.

    Returns a JSON string carrying the output path, byte size, the offline
    re-validation result (the written file re-parses/validates via
    ``formats.drt``), and ``"verified": false`` — the byte patch is only
    guaranteed to round-trip offline, not that a live Resolve will accept it. A
    missing input file or an unsupported ``transition_type`` returns an
    "Error: ..." string; this tool never raises.
    """
    try:
        canon, err = _check_choice(transition_type, list(_TRANSITION_TYPES), "transition_type")
        if err:
            return f"Error: {err}"
        if not isinstance(file_path, str) or not file_path.strip():
            return "Error: file_path is required (a .drt/.drp file on disk)"
        if not os.path.isfile(file_path):
            return f"Error: no such file: {file_path}"

        native_type, native_name = _TRANSITION_TYPES[canon]

        try:
            track_idx = int(track)
        except (TypeError, ValueError):
            return f"Error: track must be an integer track index, got {track!r}"
        if track_idx < 1:
            return "Error: track must be a 1-based track index (>= 1)"

        try:
            at_frame_int = int(at_frame)
        except (TypeError, ValueError):
            return f"Error: at_frame must be an integer frame, got {at_frame!r}"

        try:
            dur_val = int(duration_frames)
        except (TypeError, ValueError):
            return f"Error: duration_frames must be an integer, got {duration_frames!r}"
        duration = dur_val if dur_val else None

        out = (
            output_path.strip()
            if isinstance(output_path, str) and output_path.strip()
            else _default_output(file_path)
        )

        with open(file_path, "rb") as fh:
            src = fh.read()

        patched = transitions_fmt.inject_native_transition(
            src,
            at_frame_int,
            track_index=track_idx - 1,
            track_type=track_type,
            duration_frames=duration,
            transition_type=native_type,
            name=native_name,
        )

        with open(out, "wb") as fh:
            fh.write(patched)

        # Offline re-validation of what was just written.
        validation = drt_fmt.validate_drt(patched)
        injected = transitions_fmt.parse_native_transitions(patched)

        return json.dumps(
            {
                "tool": "place_transition",
                "output_path": out,
                "bytes": len(patched),
                "at_frame": at_frame_int,
                "track": track_idx,
                "track_type": track_type,
                "transition_type": canon,
                "native_transition_type": native_type,
                "duration_frames": duration if duration is not None else "auto",
                "native_transition_count": len(injected),
                "revalidated_offline": bool(validation.get("valid")),
                "validation": validation,
                "verified": False,
                "note": (
                    "byte-patched offline; 'verified': false — the written file "
                    "re-parses/validates offline only, a live DaVinci Resolve is "
                    "the final authority"
                ),
            },
            indent=2,
            default=str,
        )
    except transitions_fmt.DrtError as e:
        return f"Error: {e}"
    except Exception as e:  # noqa: BLE001 - tools never raise
        return f"Error: {e}"


@mcp.tool()
def place_motionvfx_transition(
    name: str = "",
    cut_frame: int = 0,
    duration_frames: int = 0,
    drt_in: str = "",
    drt_out: str = "",
    library_drt: str = "",
) -> str:
    """Place a MotionVFX *named* transition into a ``.drt``/``.drp`` offline.

    A MotionVFX transition is a 2-``MediaIn`` clip Fusion comp that does **not**
    export standalone (``ExportFusionComp`` returns ``false``), so it cannot be
    rebuilt offline. Instead the real ``<Sm2TiTransition>`` element is harvested
    from a reference timeline and copied into the target ``.drt``: its ``<Name>``
    (template identity), ``<FieldsBlob>`` (which references the *installed*
    template) and empty ``<CompositionBA/>`` are kept verbatim, while ``<Start>``
    and ``<Duration>`` (plain XML frame tags) are set to the requested position
    and a fresh ``<DbId>`` is assigned. The element is injected into the target
    SeqContainer's video-track ``<Items>`` list (reusing the same byte-surgery as
    :func:`place_transition`). Never connects to DaVinci Resolve.

    This is a sibling of :func:`place_transition` (which builds a *native* SMPTE
    dissolve from scratch); this one places an *installed MotionVFX* transition by
    copying its element.

    Parameters:
    - name: the MotionVFX transition template name (e.g. ``"mTuber 3 Transition
      01"``); matched case-insensitively against the library's ``<Name>`` tags.
    - cut_frame: timeline frame written to the element's ``<Start>``.
    - duration_frames: transition length written to the element's ``<Duration>``.
    - drt_in: source ``.drt``/``.drp`` file the transition is injected into.
    - drt_out: destination file; defaults to ``<drt_in>-motionvfx.<ext>``.
    - library_drt: a reference ``.drt``/``.drp`` that contains the named
      ``<Sm2TiTransition>`` element. Defaults to the
      ``DAVINCI_MCP_TRANSITION_LIBRARY`` environment variable; if neither is set
      an "Error: ..." string asks for one (the reference elements embed
      proprietary MotionVFX data, so no library path is shipped as a default).

    Returns a JSON string carrying the output path, the matched template name, the
    fresh ``DbId``, the before/after ``<Sm2TiTransition>`` count on the target,
    the offline re-validation result, and ``"verified": false``. A missing input
    file, an absent library, or an unknown ``name`` (the message lists the
    available names) all come back as an "Error: ..." string; this tool never
    raises.
    """
    try:
        if not isinstance(name, str) or not name.strip():
            return "Error: name is required (a MotionVFX transition template name)"
        if not isinstance(drt_in, str) or not drt_in.strip():
            return "Error: drt_in is required (a .drt/.drp file on disk)"
        if not os.path.isfile(drt_in):
            return f"Error: no such file: {drt_in}"

        try:
            cut = int(cut_frame)
        except (TypeError, ValueError):
            return f"Error: cut_frame must be an integer frame, got {cut_frame!r}"

        try:
            dur = int(duration_frames)
        except (TypeError, ValueError):
            return f"Error: duration_frames must be an integer, got {duration_frames!r}"
        if dur < 1:
            return "Error: duration_frames must be >= 1"

        lib = (
            library_drt.strip()
            if isinstance(library_drt, str) and library_drt.strip()
            else os.environ.get("DAVINCI_MCP_TRANSITION_LIBRARY", "").strip()
        )
        if not lib:
            return (
                "Error: no transition library — pass library_drt=<a reference "
                ".drt/.drp that contains the <Sm2TiTransition> element> or set the "
                "DAVINCI_MCP_TRANSITION_LIBRARY environment variable (the MotionVFX "
                "reference elements are proprietary/local, so none is shipped)"
            )
        if not os.path.isfile(lib):
            return f"Error: no such library file: {lib}"

        elements = transitions_fmt.read_transition_elements(lib)
        if not elements:
            return f"Error: no <Sm2TiTransition> elements found in library {lib}"

        wanted = name.strip().lower()
        matched = next(
            (e for e in elements if (e["name"] or "").strip().lower() == wanted),
            None,
        )
        if matched is None:
            available = sorted({e["name"] for e in elements if e["name"]})
            return (
                f"Error: no MotionVFX transition named {name!r} in {lib}. "
                f"Available ({len(available)}): " + ", ".join(available)
            )

        # Copy the element and re-stamp it: fresh DbId(s), new Start/Duration.
        element = matched["element"]
        fresh_ids: List[str] = []

        def _fresh_dbid(_m: "re.Match[str]") -> str:
            new_id = str(uuid.uuid4())
            fresh_ids.append(new_id)
            return f'DbId="{new_id}"'

        element = re.sub(r'DbId="[^"]*"', _fresh_dbid, element)
        new_dbid = fresh_ids[0] if fresh_ids else str(uuid.uuid4())

        element, n_start = re.subn(
            r"<Start>[^<]*</Start>", f"<Start>{cut}</Start>", element, count=1
        )
        element, n_dur = re.subn(
            r"<Duration>[^<]*</Duration>",
            f"<Duration>{dur}</Duration>",
            element,
            count=1,
        )
        if n_start == 0 or n_dur == 0:
            return (
                "Error: library element for "
                f"{matched['name']!r} is missing a <Start>/<Duration> tag — "
                "cannot position it"
            )

        out = (
            drt_out.strip()
            if isinstance(drt_out, str) and drt_out.strip()
            else _motionvfx_default_output(drt_in)
        )

        with open(drt_in, "rb") as fh:
            src = fh.read()

        before = len(transitions_fmt.parse_native_transitions(src))
        patched = transitions_fmt.inject_transition_element(src, element, track_index=0)

        with open(out, "wb") as fh:
            fh.write(patched)

        # Offline re-validation of what was just written.
        validation = drt_fmt.validate_drt(patched)
        after = len(transitions_fmt.parse_native_transitions(patched))

        return json.dumps(
            {
                "tool": "place_motionvfx_transition",
                "output_path": out,
                "bytes": len(patched),
                "name": matched["name"],
                "cut_frame": cut,
                "duration_frames": dur,
                "track": 1,
                "track_type": "video",
                "db_id": new_dbid,
                "library_drt": lib,
                "transition_count_before": before,
                "transition_count_after": after,
                "injected": after == before + 1,
                "revalidated_offline": bool(validation.get("valid")),
                "validation": validation,
                "verified": False,
                "note": (
                    "live import (import_timeline_from_file) + confirming Resolve "
                    "honours the XML <Start>/<Duration> on import is not yet "
                    "verified"
                ),
            },
            indent=2,
            default=str,
        )
    except transitions_fmt.DrtError as e:
        return f"Error: {e}"
    except Exception as e:  # noqa: BLE001 - tools never raise
        return f"Error: {e}"


# --------------------------------------------------------------------------- #
# (2) + (3) Interchange authoring
# --------------------------------------------------------------------------- #
@mcp.tool()
def author_transition_interchange(
    cuts: str = "",
    output_path: str = "",
    format: str = "otio",
) -> str:
    """Author an importable timeline file carrying dissolves; no live Resolve.

    Writes a portable timeline (OpenTimelineIO ``.otio`` by default, or FCPXML /
    EDL) with one centred dissolve per cut, meant to be fed to the live
    ``import_timeline_from_file`` tool.

    Parameters:
    - cuts: a JSON array (string) of cut objects — each ``{"before": {...},
      "after": {...}, "duration": <frames>}`` — or a single cut object. A clip
      may be a bare name string or a dict with name/duration/start/url.
    - output_path: destination file path to write.
    - format: one of otio, fcpxml, edl (validated).

    Returns a success JSON string (with ``"verified": false`` — the file is
    valid interchange but its live import is not guaranteed). Empty ``cuts``, a
    cut missing a neighbouring clip, bad JSON, an unsupported ``format``, or a
    missing ``output_path`` all come back as an "Error: ..." string; never
    raises.
    """
    try:
        canon_fmt, err = _check_choice(format, _VIDEO_FORMATS, "format")
        if err:
            return f"Error: {err}"
        if not isinstance(output_path, str) or not output_path.strip():
            return "Error: output_path is required (destination file to write)"
        parsed_cuts = _parse_cuts(cuts)

        if canon_fmt == "otio":
            content = ti_fmt.author_dissolve_otio(parsed_cuts)
        elif canon_fmt == "fcpxml":
            content = ti_fmt.author_dissolve_fcpxml(parsed_cuts)
        else:  # edl
            content = ti_fmt.author_dissolve_edl(parsed_cuts)

        dest = output_path.strip()
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(content)

        return json.dumps(
            {
                "tool": "author_transition_interchange",
                "output_path": dest,
                "format": canon_fmt,
                "cut_count": len(parsed_cuts),
                "bytes": len(content.encode("utf-8")),
                "verified": False,
                "note": (
                    "importable interchange written offline; feed it to "
                    "import_timeline_from_file. 'verified': false — a live "
                    "DaVinci Resolve import is the final authority"
                ),
            },
            indent=2,
            default=str,
        )
    except ti_fmt.TransitionInterchangeError as e:
        return f"Error: {e}"
    except Exception as e:  # noqa: BLE001 - tools never raise
        return f"Error: {e}"


@mcp.tool()
def author_audio_crossfade_interchange(
    cuts: str = "",
    output_path: str = "",
    format: str = "otio",
) -> str:
    """Author an importable audio-crossfade timeline file; no live Resolve.

    Writes a portable OTIO timeline whose track ``kind`` is ``Audio``, modelling
    each cut as a centred cross-fade (``SMPTE_Dissolve`` on the audio track).

    Parameters:
    - cuts: a JSON array (string) of cut objects (same shape as
      ``author_transition_interchange``) or a single cut object.
    - output_path: destination file path to write.
    - format: only ``"otio"`` is supported (validated).

    Returns a success JSON string (with ``"verified": false``). Empty ``cuts``,
    a cut missing a neighbouring clip, bad JSON, an unsupported ``format``, or a
    missing ``output_path`` all come back as an "Error: ..." string; never
    raises.
    """
    try:
        canon_fmt, err = _check_choice(format, _AUDIO_FORMATS, "format")
        if err:
            return f"Error: {err}"
        if not isinstance(output_path, str) or not output_path.strip():
            return "Error: output_path is required (destination file to write)"
        parsed_cuts = _parse_cuts(cuts)

        content = ti_fmt.author_audio_crossfade(parsed_cuts)

        dest = output_path.strip()
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(content)

        return json.dumps(
            {
                "tool": "author_audio_crossfade_interchange",
                "output_path": dest,
                "format": canon_fmt,
                "cut_count": len(parsed_cuts),
                "bytes": len(content.encode("utf-8")),
                "track_kind": "Audio",
                "verified": False,
                "note": (
                    "importable audio-crossfade timeline written offline; feed "
                    "it to import_timeline_from_file. 'verified': false — a live "
                    "DaVinci Resolve import is the final authority"
                ),
            },
            indent=2,
            default=str,
        )
    except ti_fmt.TransitionInterchangeError as e:
        return f"Error: {e}"
    except Exception as e:  # noqa: BLE001 - tools never raise
        return f"Error: {e}"


# --------------------------------------------------------------------------- #
# (4) + (5) Live keystroke hybrid
# --------------------------------------------------------------------------- #
def _add_default_transition(timecode_or_frame: Any, align: str, is_audio: bool) -> str:
    """Shared body for the two keystroke-hybrid tools.

    Captures a pre/post timeline signature around the cut and only reports
    ``verified: true`` when it changes; otherwise ``verified: false`` with a
    "keystroke may not have registered" note. Mutates the LIVE project with no
    scripted undo. Every failure path (no Resolve, no active timeline, bad
    playhead, no keystroke backend) returns an "Error: ..." string.
    """
    try:
        canon_align, err = _check_choice(align, _ALIGNMENTS, "align")
        if err:
            return f"Error: {err}"
        if timecode_or_frame is None or not str(timecode_or_frame).strip():
            return (
                "Error: timecode_or_frame is required (a timeline timecode "
                "'HH:MM:SS:FF' or an integer frame)"
            )

        conn = _conn()  # raises ConnectionError -> caught below when no Resolve
        timeline = conn.get_current_timeline()
        if timeline is None:
            return "Error: no active timeline — open a timeline on the Edit page first"

        tc = _resolve_timecode(timeline, timecode_or_frame)
        track_types = ("audio",) if is_audio else ("video",)

        pre = _capture_signature(timeline, track_types)

        moved = timeline.SetCurrentTimecode(tc)
        if not moved:
            return (
                f"Error: could not move the playhead to {tc} — is it within the "
                "timeline range?"
            )

        backend = _fire_default_transition_keystroke(is_audio)  # raises if no backend

        post = _capture_signature(timeline, track_types)
        changed = pre != post

        keystroke = (
            "Shift+T"
            if is_audio
            else ("Cmd+T" if sys.platform == "darwin" else "Ctrl+T")
        )
        result: Dict[str, Any] = {
            "tool": (
                "add_default_audio_transition_at_cut"
                if is_audio
                else "add_default_transition_at_cut"
            ),
            "playhead": tc,
            "align": canon_align,
            "keystroke": keystroke,
            "keystroke_backend": backend,
            "signature_changed": changed,
            "verified": bool(changed),
        }
        if changed:
            result["note"] = (
                "a new edit/transition was detected at the cut (the pre/post "
                "timeline signature changed). This mutated the LIVE project with "
                "NO scripted undo."
            )
        else:
            result["note"] = (
                "keystroke may not have registered — needs Edit-page focus, no "
                "scripted undo. The pre/post timeline signature is unchanged, so "
                "no transition was confirmed."
            )
        return json.dumps(result, indent=2, default=str)
    except Exception as e:  # noqa: BLE001 - tools never raise
        return f"Error: {e}"


@mcp.tool()
def add_default_transition_at_cut(
    timecode_or_frame: str = "",
    align: str = "center",
) -> str:
    """Add Resolve's default VIDEO transition at a cut via a live keystroke (Cmd/Ctrl+T).

    Moves the LIVE playhead to ``timecode_or_frame`` and fires DaVinci Resolve's
    default add-transition shortcut. This path MUTATES the live project with NO
    scripted undo and requires the Edit page to have GUI focus.

    Parameters:
    - timecode_or_frame: a timeline timecode ``"HH:MM:SS:FF"`` or an integer
      timeline-relative frame.
    - align: advisory alignment, one of center/start/end (validated); the actual
      alignment of a default transition is decided by Resolve from available
      handle media.

    Captures a pre/post timeline signature (per-track item count + edit points)
    and returns ``"verified": true`` ONLY when a new edit/transition is detected;
    an unchanged signature returns ``"verified": false`` with a "keystroke may
    not have registered — needs Edit-page focus, no scripted undo" note. When no
    Resolve is reachable or no keystroke backend is available, returns an
    "Error: ..." string; never raises.
    """
    return _add_default_transition(timecode_or_frame, align, is_audio=False)


@mcp.tool()
def add_default_audio_transition_at_cut(timecode_or_frame: str = "") -> str:
    """Add Resolve's default AUDIO cross-fade at a cut via a live keystroke (Shift+T).

    Moves the LIVE playhead to ``timecode_or_frame`` and fires DaVinci Resolve's
    default add-audio-transition shortcut. This path MUTATES the live project
    with NO scripted undo and requires the Edit page to have GUI focus.

    Parameters:
    - timecode_or_frame: a timeline timecode ``"HH:MM:SS:FF"`` or an integer
      timeline-relative frame.

    Captures a pre/post AUDIO-track signature and returns ``"verified": true``
    ONLY when a new edit/transition is detected; an unchanged signature returns
    ``"verified": false`` with a "keystroke may not have registered — needs
    Edit-page focus, no scripted undo" note. When no Resolve is reachable or no
    keystroke backend is available, returns an "Error: ..." string; never raises.
    """
    return _add_default_transition(timecode_or_frame, "center", is_audio=True)
