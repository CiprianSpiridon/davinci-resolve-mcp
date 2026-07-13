"""
tests/test_transitions.py — offline tests for the transitions cluster.

Exercises the three offline surfaces of the transitions cluster with no
DaVinci Resolve instance and no network access, per the OFFLINE tool-set
architecture invariant:

- ``formats.transitions.inject_native_transition`` — a native
  ``<Sm2TiTransition>`` byte-surgeon: injecting a centred dissolve into a
  ``.drt`` SeqContainer adds exactly one transition at the centred Start and
  leaves every OTHER archive member's *decompressed* content identical
  (re-parse equality, not raw-zip bytes, because ``drt._write_members``
  re-deflates every member at ``compresslevel=6``), and every neighbouring
  clip's ``Start`` / ``Duration`` untouched.
- ``formats.transition_interchange.author_dissolve_otio`` — emits a
  ``json.loads``-parseable OpenTimelineIO ``Transition.1`` of type
  ``SMPTE_Dissolve`` (schema per
  ``SMPTE_Dissolve`` is supported, dissolve straddles the edit, per
- Tool exposure — the five ``@mcp.tool()`` transition tools are registered,
  globally unique, and each carries a non-empty description.

Plus the tool error-string contract: a tool never raises out of its body; a
bad-input call returns an ``"Error: ..."`` ``str`` instead.

Everything here reads/writes bytes/strings in-process only — no Resolve, no
network — following ``tests/test_offline_formats.py`` and
``tests/test_tool_exposure.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections import Counter

import pytest

from davinci_resolve_mcp.formats import drt
from davinci_resolve_mcp.formats import transition_interchange as ti
from davinci_resolve_mcp.formats import transitions as transitions_fmt
from davinci_resolve_mcp.tools.transitions import (
    author_transition_interchange,
    place_transition,
)

# A minimal two-clip video timeline: an outgoing clip ending exactly at frame
# 48 and an incoming clip starting there, so frame 48 is a real cut. The
# incoming clip's in-point (100) supplies ample leading handle media for a
# 12-frame centred dissolve (half = 6).
_DRT_SPEC = {
    "timelines": [
        {
            "name": "Scene 010",
            "frameRate": 24,
            "startTimecode": "01:00:00:00",
            "resolution": "1920x1080",
            "tracks": [
                {
                    "type": "video",
                    "clips": [
                        {"start": 0, "duration": 48, "in": 0, "out": 48, "mediaFilePath": "/media/a.mov"},
                        {"start": 48, "duration": 24, "in": 100, "out": 124, "mediaFilePath": "/media/b.mov"},
                    ],
                },
                {"type": "audio", "clips": []},
            ],
        }
    ],
    "metadata": {"createdBy": "test"},
}

_CUT_FRAME = 48
_DISSOLVE_FRAMES = 12
_HALF = _DISSOLVE_FRAMES // 2  # 6 — centred Start = cut - half


# ---------------------------------------------------------------------------
# formats.transitions — native <Sm2TiTransition> injection round-trip
# ---------------------------------------------------------------------------
def _build_fixture_drt() -> bytes:
    return drt.author_drt(_DRT_SPEC)


def test_inject_native_transition_adds_exactly_one_at_centered_start():
    blob = _build_fixture_drt()

    # Baseline: the authored timeline carries no native transitions yet.
    assert transitions_fmt.parse_native_transitions(blob) == []

    patched = transitions_fmt.inject_native_transition(
        blob,
        _CUT_FRAME,
        track_index=0,
        duration_frames=_DISSOLVE_FRAMES,
    )

    injected = transitions_fmt.parse_native_transitions(patched)
    assert len(injected) == 1, "exactly one <Sm2TiTransition> must be injected"
    tr = injected[0]
    assert tr["start"] == _CUT_FRAME - _HALF  # centred: straddles the cut
    assert tr["duration"] == _DISSOLVE_FRAMES
    assert tr["alignmentType"] == 2  # 2 == centre
    assert tr["inOffset"] == _HALF
    assert tr["outOffset"] == _DISSOLVE_FRAMES - _HALF
    assert tr["transitionType"] == "SMPTE_Dissolve"


def test_inject_native_transition_preserves_other_members_decompressed():
    blob = _build_fixture_drt()
    before = drt._read_members(blob)
    seq_members = set(drt._list_seq_members(before))
    assert seq_members, "fixture must contain a SeqContainer member to edit"

    patched = transitions_fmt.inject_native_transition(
        blob,
        _CUT_FRAME,
        track_index=0,
        duration_frames=_DISSOLVE_FRAMES,
    )
    after = drt._read_members(patched)

    # Same member set, no additions/removals to the archive.
    assert set(after) == set(before)

    # Every member that is NOT the edited SeqContainer must have identical
    # DECOMPRESSED content (re-parse equality, not raw-zip bytes).
    for name, content in before.items():
        if name in seq_members:
            continue
        assert after[name] == content, f"member {name} was not preserved (decompressed)"

    # The edited SeqContainer really did change, and gained exactly one
    # <Sm2TiTransition> element.
    (edited,) = list(seq_members)
    before_xml = before[edited].decode("utf-8", "replace")
    after_xml = after[edited].decode("utf-8", "replace")
    assert after_xml != before_xml
    assert before_xml.count("<Sm2TiTransition") == 0
    assert after_xml.count("<Sm2TiTransition") == 1


def test_inject_native_transition_leaves_neighbor_clip_geometry_untouched():
    blob = _build_fixture_drt()
    patched = transitions_fmt.inject_native_transition(
        blob,
        _CUT_FRAME,
        track_index=0,
        duration_frames=_DISSOLVE_FRAMES,
    )

    # The patched archive still re-parses and validates as a well-formed .drt.
    reparsed = drt.parse_drt(patched)
    assert drt.validate_drt(patched) == {"valid": True, "errors": []}

    clips = reparsed["timelines"][0]["videoTracks"][0]["clips"]
    geometry = [(c["start"], c["duration"]) for c in clips]
    # Neither abutting clip's start/duration moved — the transition overlaps
    # without rippling the timeline.
    assert geometry == [(0, 48), (48, 24)]


# ---------------------------------------------------------------------------
# formats.transition_interchange — OTIO Transition.1 SMPTE_Dissolve emission
# ---------------------------------------------------------------------------
_OTIO_CUTS = [
    {
        "before": {"name": "A.mov", "duration": 48, "start": 0, "url": "A.mov"},
        "after": {"name": "B.mov", "duration": 48, "start": 100, "url": "B.mov"},
        "duration": _DISSOLVE_FRAMES,
    }
]


def _otio_transitions(doc: dict) -> list:
    children = doc["tracks"]["children"][0]["children"]
    return [c for c in children if c.get("OTIO_SCHEMA") == "Transition.1"]


def test_author_dissolve_otio_emits_parseable_smpte_dissolve():
    text = ti.author_dissolve_otio(_OTIO_CUTS)
    # Contract: the emitter produces a json.loads-parseable OTIO document.
    doc = json.loads(text)
    assert doc["OTIO_SCHEMA"] == "Timeline.1"

    transitions = _otio_transitions(doc)
    assert len(transitions) == 1
    tr = transitions[0]
    assert tr["OTIO_SCHEMA"] == "Transition.1"
    # Only SMPTE_Dissolve is modelled (aaf_writer.py:649).
    assert tr["transition_type"] == "SMPTE_Dissolve"
    # Centred: in_offset == out_offset == half the dissolve (writer.ts:171).
    assert tr["in_offset"]["OTIO_SCHEMA"] == "RationalTime.1"
    assert tr["in_offset"]["value"] == _HALF
    assert tr["out_offset"]["value"] == _HALF


def test_author_dissolve_otio_empty_cuts_raises_typed_error():
    with pytest.raises(ti.TransitionInterchangeError):
        ti.author_dissolve_otio([])


# ---------------------------------------------------------------------------
# Tool exposure — the five transition tools are registered / unique / described
# ---------------------------------------------------------------------------
_EXPECTED_TOOLS = (
    "place_transition",
    "author_transition_interchange",
    "author_audio_crossfade_interchange",
    "add_default_transition_at_cut",
    "add_default_audio_transition_at_cut",
)


@pytest.fixture(scope="module")
def tools():
    import davinci_resolve_mcp.server as server

    return asyncio.run(server.mcp.list_tools())


def test_five_transition_tools_are_registered(tools):
    names = {t.name for t in tools}
    missing = [name for name in _EXPECTED_TOOLS if name not in names]
    assert not missing, f"transition tools not registered: {missing}"


def test_transition_tools_are_unique_and_described(tools):
    by_name = Counter(t.name for t in tools)
    for name in _EXPECTED_TOOLS:
        assert by_name[name] == 1, f"{name} registered {by_name[name]} times (must be unique)"
    for tool in tools:
        if tool.name in _EXPECTED_TOOLS:
            assert (tool.description or "").strip(), f"{tool.name} has an empty description"


# ---------------------------------------------------------------------------
# Tool error-string contract — never raise out of a tool body
# ---------------------------------------------------------------------------
def test_place_transition_on_missing_file_returns_error_string():
    result = place_transition(
        file_path="/nonexistent/path/does-not-exist.drt",
        at_frame=_CUT_FRAME,
    )
    assert isinstance(result, str)
    assert result.startswith("Error:")


def test_author_transition_interchange_empty_cuts_returns_error_string(tmp_path):
    dest = tmp_path / "out.otio"
    result = author_transition_interchange(
        cuts=[],
        output_path=str(dest),
    )
    assert isinstance(result, str)
    assert result.startswith("Error:")
    # An invalid (empty) request must not leave a written interchange file.
    assert not dest.exists()
