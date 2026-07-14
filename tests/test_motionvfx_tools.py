"""Offline tests for the 7 MotionVFX automation tools (no DaVinci Resolve).

Covers the tools added by the MotionVFX Implement phase:

- fx_plugins:      cache_template_comp, place_overlay_title, cache_effect_comp,
                   apply_clip_effect, classify_timeline_element
- media_pool_item: set_super_scale
- transitions:     place_motionvfx_transition

Three groups, all with no Resolve running and no network:

(a) registration — every new tool name is present in ``mcp.list_tools()``
    (proves each ``@mcp.tool()`` registered at import time, lazily, with no
    Resolve connection);
(b) the SCALABLE offline MediaIn classifier
    (``fx_plugins._count_media_in_in_comp_text``) returns 0 for every
    generator/title fixture and 1 for every effect fixture — the 0/1/2 lane
    signal that ``classify_timeline_element`` derives live from GetToolList;
(c) pure-argument guards — the tools return an ``Error: ...`` string (never
    raise) for bad arguments, reachable with no Resolve session.
"""

from __future__ import annotations

import asyncio
import glob
import os

import pytest

# be absent in CI; the fixture test is skipped when the directory is missing).
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_TESTS_DIR)
_FIXTURES_DIR = os.path.join(_REPO_ROOT, "examples", "motionvfx-fixtures")
_GENERATORS_DIR = os.path.join(_FIXTURES_DIR, "generators_0-MediaIn")
_EFFECTS_DIR = os.path.join(_FIXTURES_DIR, "effects_1-MediaIn")

NEW_TOOL_NAMES = frozenset(
    {
        "cache_template_comp",
        "place_overlay_title",
        "cache_effect_comp",
        "apply_clip_effect",
        "classify_timeline_element",
        "set_super_scale",
        "place_motionvfx_transition",
    }
)

_PROVEN_COMP = os.path.join(_FIXTURES_DIR, "kenburns-animated-zoom_PROVEN.comp")


# ---------------------------------------------------------------------------
# (a) registration
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tool_names():
    """Import the server (no Resolve, no network) and list registered names."""
    import davinci_resolve_mcp.server as server

    tools = asyncio.run(server.mcp.list_tools())
    return [t.name for t in tools]


def test_all_seven_new_tools_registered(tool_names):
    present = set(tool_names) & NEW_TOOL_NAMES
    missing = NEW_TOOL_NAMES - present
    assert not missing, f"new MotionVFX tools missing from registration: {sorted(missing)}"
    assert present == NEW_TOOL_NAMES


def test_new_tools_registered_exactly_once(tool_names):
    for name in NEW_TOOL_NAMES:
        count = tool_names.count(name)
        assert count == 1, f"tool '{name}' registered {count} times, expected exactly 1"


# ---------------------------------------------------------------------------
# (b) SCALABLE MediaIn classifier against the fixture corpus
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.path.isdir(_GENERATORS_DIR),
    reason="motionvfx generator fixtures absent (gitignored; not present in CI)",
)
def test_generators_have_zero_mediain():
    from davinci_resolve_mcp.tools.fx_plugins import _count_media_in_in_comp_text

    comps = sorted(glob.glob(os.path.join(_GENERATORS_DIR, "*.comp")))
    assert comps, f"no .comp fixtures found under {_GENERATORS_DIR}"
    for path in comps:
        with open(path, encoding="utf-8", errors="replace") as fh:
            txt = fh.read()
        assert _count_media_in_in_comp_text(txt) == 0, (
            f"generator/title fixture should have 0 MediaIn (lane 0): "
            f"{os.path.basename(path)}"
        )


@pytest.mark.skipif(
    not os.path.isdir(_EFFECTS_DIR),
    reason="motionvfx effect fixtures absent (gitignored; not present in CI)",
)
def test_effects_have_one_mediain():
    from davinci_resolve_mcp.tools.fx_plugins import _count_media_in_in_comp_text

    comps = sorted(glob.glob(os.path.join(_EFFECTS_DIR, "*.comp")))
    assert comps, f"no .comp fixtures found under {_EFFECTS_DIR}"
    for path in comps:
        with open(path, encoding="utf-8", errors="replace") as fh:
            txt = fh.read()
        assert _count_media_in_in_comp_text(txt) == 1, (
            f"effect fixture should have exactly 1 MediaIn (lane 1): "
            f"{os.path.basename(path)}"
        )


# ---------------------------------------------------------------------------
# (c) pure-argument guards (return "Error: ...", never raise, no Resolve)
# ---------------------------------------------------------------------------


def test_set_super_scale_rejects_out_of_range_mode():
    from davinci_resolve_mcp.tools.media_pool_item import set_super_scale

    result = set_super_scale("some_clip", 5)
    assert isinstance(result, str)
    assert result.startswith("Error")
    # Mentions the valid values (1-4) so the caller can correct the argument.
    assert "1" in result and "4" in result


def test_place_overlay_title_rejects_v1_track():
    from davinci_resolve_mcp.tools.fx_plugins import place_overlay_title

    result = place_overlay_title("SomeTitle", track_index=1, record_frame=0, duration_frames=10)
    assert isinstance(result, str)
    assert result.startswith("Error")
    # Guard is about overlays never going on V1 / track must be >= 2.
    assert "V1" in result or ">= 2" in result


def test_place_overlay_title_rejects_short_duration():
    from davinci_resolve_mcp.tools.fx_plugins import place_overlay_title

    result = place_overlay_title(
        "SomeTitle", track_index=2, record_frame=0, duration_frames=2
    )
    assert isinstance(result, str)
    assert result.startswith("Error")
    assert "duration_frames" in result


def test_place_motionvfx_transition_requires_name():
    from davinci_resolve_mcp.tools.transitions import place_motionvfx_transition

    result = place_motionvfx_transition(name="", drt_in="whatever.drt")
    assert isinstance(result, str)
    assert result.startswith("Error")
    assert "name is required" in result


def test_place_motionvfx_transition_requires_drt_in():
    from davinci_resolve_mcp.tools.transitions import place_motionvfx_transition

    result = place_motionvfx_transition(name="Some Transition", drt_in="")
    assert isinstance(result, str)
    assert result.startswith("Error")
    assert "drt_in is required" in result


def test_place_motionvfx_transition_missing_input_file(tmp_path):
    from davinci_resolve_mcp.tools.transitions import place_motionvfx_transition

    missing = str(tmp_path / "does-not-exist.drt")
    result = place_motionvfx_transition(
        name="Some Transition", drt_in=missing, duration_frames=30
    )
    assert isinstance(result, str)
    assert result.startswith("Error")
    assert "no such file" in result


def test_place_motionvfx_transition_missing_library(tmp_path, monkeypatch):
    """With a valid input but no library (arg or env), the tool errors offline."""
    from davinci_resolve_mcp.tools.transitions import place_motionvfx_transition

    monkeypatch.delenv("DAVINCI_MCP_TRANSITION_LIBRARY", raising=False)
    drt_in = tmp_path / "target.drt"
    drt_in.write_text("<xml/>", encoding="utf-8")
    result = place_motionvfx_transition(
        name="Some Transition",
        drt_in=str(drt_in),
        duration_frames=30,
        library_drt="",
    )
    assert isinstance(result, str)
    assert result.startswith("Error")
    assert "library" in result.lower()


# ---------------------------------------------------------------------------
# (d) Ken-Burns / animated-zoom .comp generator + animate_clip_transform tool
# ---------------------------------------------------------------------------


def test_build_kenburns_comp_no_pan():
    """No-pan (center from==to): keyframe only Size, emit a static Center, no PolyPath."""
    from davinci_resolve_mcp.formats.kenburns import build_kenburns_comp

    dur = 90
    comp = build_kenburns_comp(
        dur_frames=dur,
        zoom_from=1.0,
        zoom_to=1.08,
        cx_from=0.5,
        cy_from=0.5,
        cx_to=0.5,
        cy_to=0.5,
    )
    assert isinstance(comp, str)
    assert comp.startswith("Composition {")
    # Top-level tool graph MediaIn1 -> Transform1 -> MediaOut1 with a Size spline.
    assert "MediaIn1 = MediaIn" in comp
    assert "Transform1 = Transform" in comp
    assert "MediaOut1 = MediaOut" in comp
    assert "Transform1Size = BezierSpline" in comp
    # The [0] key carries zoom_from and the [dur-1] key carries zoom_to.
    assert f"[0] = {{ {1.0}" in comp
    assert f"[{dur - 1}] = {{ {1.08}" in comp
    # Static center means no PolyPath center animation.
    assert "PolyPath" not in comp


def test_build_kenburns_comp_with_pan():
    """Pan (center from != to): emit a Transform1CenterPath PolyPath with both X values."""
    from davinci_resolve_mcp.formats.kenburns import build_kenburns_comp

    comp = build_kenburns_comp(
        dur_frames=90,
        zoom_from=1.0,
        zoom_to=1.08,
        cx_from=0.3,
        cy_from=0.5,
        cx_to=0.7,
        cy_to=0.5,
    )
    assert "Transform1CenterPath = PolyPath" in comp
    assert "X = 0.3" in comp
    assert "X = 0.7" in comp


def test_animate_clip_transform_registered(tool_names):
    assert "animate_clip_transform" in tool_names
    assert tool_names.count("animate_clip_transform") == 1


@pytest.mark.skipif(
    not os.path.isfile(_PROVEN_COMP),
    reason="kenburns PROVEN fixture absent (gitignored; not present in CI)",
)
def test_build_kenburns_comp_matches_proven_toolset():
    """Generated comp exposes the same top-level tools as the live-proven comp."""
    from davinci_resolve_mcp.formats.kenburns import build_kenburns_comp

    with open(_PROVEN_COMP, encoding="utf-8", errors="replace") as fh:
        proven = fh.read()
    generated = build_kenburns_comp(dur_frames=1745, zoom_from=1.0, zoom_to=1.1)

    for tool in ("MediaIn1 = MediaIn", "Transform1 = Transform", "MediaOut1 = MediaOut"):
        assert tool in proven, f"proven fixture missing {tool!r}"
        assert tool in generated, f"generated comp missing {tool!r}"
