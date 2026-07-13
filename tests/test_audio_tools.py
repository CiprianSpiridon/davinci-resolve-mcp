"""Exposure tests for the new Fairlight / audio tools.

These mirror the exposure fixture in ``tests/test_tool_exposure.py`` — import
``davinci_resolve_mcp.server`` with no DaVinci Resolve instance present and no
network access, then assert on the registered tool list returned by
``mcp.list_tools()``.

The six audio tools added in TASK-022 must each be registered exactly once,
with a non-empty description so an LLM client can use them:

- ``set_audio_volume``      (``TimelineItem.SetAudioVolume``)
- ``set_track_volume``      (``Timeline.SetTrackVolume("audio", ...)``)
- ``get_fairlight_presets`` (``Resolve.GetFairlightPresets``)
- ``apply_fairlight_preset``(``Project.ApplyFairlightPresetToCurrentTimeline``)
- ``insert_audio_at_playhead`` (``Project.InsertAudioToCurrentTrackAtPlayhead``)
- ``generate_speech``       (``Project.GenerateSpeech``)

Importing ``davinci_resolve_mcp.server`` (and ``davinci_resolve_mcp.tools.audio``
directly) must succeed even though Resolve isn't running — the architecture
requires every Resolve connection to be lazy (established only inside a tool
call), never at import time.
"""

from __future__ import annotations

import asyncio
from collections import Counter

import pytest


# The six new Fairlight / audio tools from TASK-022.
NEW_AUDIO_TOOLS = (
    "set_audio_volume",
    "set_track_volume",
    "get_fairlight_presets",
    "apply_fairlight_preset",
    "insert_audio_at_playhead",
    "generate_speech",
)


@pytest.fixture(scope="module")
def tools():
    """Import the server (no Resolve, no network) and list registered tools."""
    import davinci_resolve_mcp.server as server

    return asyncio.run(server.mcp.list_tools())


def test_audio_module_imports_with_no_resolve():
    """The audio tools module must import lazily, without a live Resolve."""
    import davinci_resolve_mcp.tools.audio as audio_module

    # A successful import already proves nothing at module scope touched
    # Resolve; confirm the module actually exposes the tool functions.
    for name in NEW_AUDIO_TOOLS:
        assert hasattr(audio_module, name), (
            f"audio module is missing the {name!r} function"
        )


def test_all_six_new_audio_tools_are_registered(tools):
    names = {t.name for t in tools}
    missing = [name for name in NEW_AUDIO_TOOLS if name not in names]
    assert not missing, (
        f"new audio tools not found in mcp.list_tools(): {missing}"
    )


def test_each_new_audio_tool_registered_exactly_once(tools):
    counts = Counter(t.name for t in tools)
    duplicates = {name: counts[name] for name in NEW_AUDIO_TOOLS if counts[name] != 1}
    assert not duplicates, (
        f"new audio tools not registered exactly once: {duplicates}"
    )


def test_each_new_audio_tool_has_a_non_empty_description(tools):
    by_name = {t.name: t for t in tools}
    missing = [
        name
        for name in NEW_AUDIO_TOOLS
        if not (by_name.get(name) and (by_name[name].description or "").strip())
    ]
    assert not missing, (
        f"new audio tools missing a non-empty description: {missing}"
    )
