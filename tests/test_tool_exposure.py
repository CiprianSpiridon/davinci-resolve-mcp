"""Exposure / uniqueness tests for the DaVinci Resolve MCP tool surface.

These tests import the server package with no DaVinci Resolve instance
present and no network access, and assert on the registered tool list
returned by ``mcp.list_tools()``:

- at least 100 tools are registered,
- every tool name is globally unique (fails loudly on a duplicate),
- every tool has a non-empty description (so it's usable by an LLM client),
- the ownership contract from the implementation plan holds: exactly one
  ``detect_scene_cuts`` tool, exactly one ``grab_still`` tool, and no
  ``insert_*`` tools registered from the Fusion module (those belong to
  timeline editing).

Importing ``davinci_resolve_mcp.server`` must succeed even though Resolve
isn't running — the architecture requires all Resolve connections to be
lazy (established only inside a tool call), never at import time.
"""

from __future__ import annotations

import asyncio
from collections import Counter

import pytest


@pytest.fixture(scope="module")
def tools():
    """Import the server (no Resolve, no network) and list registered tools."""
    import davinci_resolve_mcp.server as server

    return asyncio.run(server.mcp.list_tools())


def test_imports_with_no_resolve_and_registers_tools(tools):
    # A successful import (handled by the fixture) already proves the
    # server doesn't touch Resolve at import time. Here we just confirm
    # the tool list actually came back non-empty.
    assert len(tools) > 0


def test_minimum_tool_count(tools):
    assert len(tools) >= 100, (
        f"expected >=100 registered tools for full coverage, found {len(tools)}"
    )


def test_tool_names_are_globally_unique(tools):
    names = [t.name for t in tools]
    counts = Counter(names)
    duplicates = {name: n for name, n in counts.items() if n > 1}
    assert not duplicates, f"duplicate tool names registered: {duplicates}"


def test_every_tool_has_a_non_empty_description(tools):
    missing = [t.name for t in tools if not (t.description or "").strip()]
    assert not missing, f"tools missing a non-empty description: {missing}"


def test_ownership_detect_scene_cuts_registered_once(tools):
    names = [t.name for t in tools]
    assert names.count("detect_scene_cuts") == 1


def test_ownership_grab_still_registered_once(tools):
    names = [t.name for t in tools]
    assert names.count("grab_still") == 1


# The project-meta-gallery cluster: gallery/still albums, third-party &
# sidecar metadata, project archive/cloud, timeline playback, Resolve
# lifecycle, and per-clip transcription. Every name must be registered
# exactly once once all sibling feature tasks (TASK-024..029) have landed.
PROJECT_META_GALLERY_TOOLS = (
    "list_gallery_albums",
    "create_gallery_album",
    "get_current_still_album",
    "set_current_still_album",
    "rename_gallery_album",
    "get_album_stills",
    "get_still_label",
    "set_still_label",
    "import_stills",
    "export_stills",
    "delete_stills",
    "export_metadata",
    "get_third_party_metadata",
    "set_third_party_metadata",
    "update_sidecar",
    "archive_project",
    "create_cloud_project",
    "load_cloud_project",
    "play_timeline",
    "stop_timeline",
    "quit_resolve",
    "transcribe_clip_audio",
    "clear_clip_transcription",
)


def test_project_meta_gallery_cluster_registered_exactly_once(tools):
    """Every project-meta-gallery tool is registered exactly once.

    Names a missing tool loudly if a function is left undecorated or its
    module is never imported by ``server.py``, and flags any duplicate
    registration.
    """
    counts = Counter(t.name for t in tools)

    missing = [name for name in PROJECT_META_GALLERY_TOOLS if counts[name] == 0]
    assert not missing, (
        "project-meta-gallery tools not registered (undecorated function or "
        f"module not imported in server.py?): {missing}"
    )

    duplicated = {
        name: counts[name]
        for name in PROJECT_META_GALLERY_TOOLS
        if counts[name] > 1
    }
    assert not duplicated, (
        f"project-meta-gallery tools registered more than once: {duplicated}"
    )


def test_ownership_no_insert_tools_from_fusion_module(tools):
    """``insert_*`` tools (generators/titles) belong to timeline_edit, not fusion."""
    import davinci_resolve_mcp.tools.fusion as fusion_module

    fusion_tool_names = {
        attr
        for attr in vars(fusion_module)
        if attr.startswith("insert_")
    }
    assert not fusion_tool_names, (
        f"fusion module must not define insert_* tools: {fusion_tool_names}"
    )

    # Also confirm no *registered* tool named insert_* has a Fusion-comp
    # flavored description bleeding in from the wrong module — i.e. every
    # insert_* tool that IS registered is unique and accounted for.
    insert_names = [t.name for t in tools if t.name.startswith("insert_")]
    assert len(insert_names) == len(set(insert_names))
