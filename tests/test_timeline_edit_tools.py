"""Registration / uniqueness tests for the timeline-edit tool cluster.

These tests import ``davinci_resolve_mcp.server`` with no DaVinci Resolve
instance present and no network access, and assert on the registered tool
list returned by ``mcp.list_tools()`` that the 14 timeline-edit cluster
tools are wired up:

- every cluster tool is registered *exactly once* (fails loudly, naming the
  offending tool, on a missing or duplicated name), and
- every cluster tool carries a non-empty description (so it's usable by an
  LLM client).

The assertions here are strictly *additive* membership checks: they only
look at the 14 names owned by this cluster and never assert the exact total
tool count. That contract belongs to the count gate, so this file stays
green regardless of how many other clusters are registered.

Importing ``davinci_resolve_mcp.server`` must succeed even though Resolve
isn't running — the architecture requires all Resolve connections to be
lazy (established only inside a tool call), never at import time.
"""

from __future__ import annotations

import asyncio
from collections import Counter

import pytest

# The 14 tools owned by the timeline-edit cluster (TASK-031 / TASK-033).
TIMELINE_EDIT_TOOLS = [
    "get_timeline_mark_in_out",
    "set_timeline_mark_in_out",
    "clear_timeline_mark_in_out",
    "set_clips_linked",
    "import_into_timeline",
    "set_start_timecode",
    "get_track_sub_type",
    "get_linked_items",
    "get_track_type_and_index",
    "get_source_audio_channel_mapping",
    "get_output_cache_state",
    "set_color_output_cache",
    "set_fusion_output_cache",
    "get_stereo_params",
]


@pytest.fixture(scope="module")
def tools():
    """Import the server (no Resolve, no network) and list registered tools."""
    import davinci_resolve_mcp.server as server

    return asyncio.run(server.mcp.list_tools())


@pytest.mark.parametrize("name", TIMELINE_EDIT_TOOLS)
def test_cluster_tool_registered_exactly_once(tools, name):
    counts = Counter(t.name for t in tools)
    n = counts[name]
    assert n == 1, (
        f"timeline-edit tool {name!r} must be registered exactly once, "
        f"found {n} occurrence(s)"
    )


@pytest.mark.parametrize("name", TIMELINE_EDIT_TOOLS)
def test_cluster_tool_has_non_empty_description(tools, name):
    matches = [t for t in tools if t.name == name]
    assert matches, f"timeline-edit tool {name!r} is not registered"
    empty = [t.name for t in matches if not (t.description or "").strip()]
    assert not empty, (
        f"timeline-edit tool {name!r} must have a non-empty description"
    )


def test_all_cluster_tools_present_exactly_once(tools):
    """Aggregate membership check naming every missing/duplicated tool."""
    counts = Counter(t.name for t in tools)
    missing = [name for name in TIMELINE_EDIT_TOOLS if counts[name] == 0]
    duplicated = {
        name: counts[name] for name in TIMELINE_EDIT_TOOLS if counts[name] > 1
    }
    assert not missing, f"timeline-edit tools missing from registry: {missing}"
    assert not duplicated, (
        f"timeline-edit tools registered more than once: {duplicated}"
    )


def test_membership_assertions_are_additive_only(tools):
    """Guard the EDGE/FAILURE contract: never pin the exact total count.

    The cluster must be a subset of the registered tools, but this file must
    stay green no matter how many other tools exist, so we only assert a
    lower bound (all 14 cluster names present) — never an exact total.
    """
    registered = {t.name for t in tools}
    assert set(TIMELINE_EDIT_TOOLS).issubset(registered), (
        "timeline-edit cluster is not a subset of the registered tools: "
        f"{sorted(set(TIMELINE_EDIT_TOOLS) - registered)}"
    )
    # Sanity: there are at least as many tools as this cluster, but we make
    # no claim about the exact total (that belongs to the count gate).
    assert len(tools) >= len(TIMELINE_EDIT_TOOLS)
