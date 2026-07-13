"""Exposure / uniqueness tests for the media-pool + media_pool_item cluster.

This is a dedicated offline exposure test for the cluster of tools registered
by the ``media_pool`` and ``media_pool_item`` modules (colour-space, stereo,
audio-sync, matte, folder-import and Neural-Engine analysis tools), so the
cluster is validated in its own right and not only implicitly by the global
count gate (``tests/test_tool_exposure.py``). It closes the founder-review
test-coverage WARN and mirrors the patterns in ``tests/test_offline_exposure.py``.

It imports the server package with no DaVinci Resolve instance present and no
network access, and asserts on the registered tool list returned by
``mcp.list_tools()``:

- every tool the cluster registers appears in ``mcp.list_tools()``, exactly
  once, each with a non-empty description (so it's usable by an LLM client);
- the cluster's tool names are globally unique across the whole server surface
  (no name is registered more than once anywhere);
- EDGE/FAILURE: a bogus tool name is asserted ABSENT from the registration,
  guarding against a catch-all / wildcard registration that would make any
  assertion of presence vacuously true.

Importing ``davinci_resolve_mcp.server`` must succeed even though Resolve
isn't running — the architecture requires all Resolve connections to be lazy
(established only inside a tool call), never at import time; none of these
tests start, mock, or otherwise touch a Resolve instance.
"""

from __future__ import annotations

import asyncio
from collections import Counter

import pytest

# The tool names registered by the media-pool + media_pool_item cluster.
# Kept as a frozenset so membership/uniqueness checks read cleanly.
CLUSTER_TOOL_NAMES = frozenset(
    {
        "set_input_color_space",
        "auto_sync_audio",
        "convert_timeline_to_stereo",
        "create_stereo_clip",
        "get_selected_clips",
        "set_selected_clip",
        "delete_clip_mattes",
        "get_clip_matte_list",
        "get_timeline_matte_list",
        "import_folder_from_file",
        "analyze_for_intellisearch",
        "analyze_for_slate",
        "monitor_growing_file",
        "perform_audio_classification",
        "remove_motion_blur",
    }
)

# A name that must NEVER be registered — the EDGE/FAILURE guard against a
# catch-all registration silently exposing arbitrary tool names.
BOGUS_TOOL_NAME = "fusion_not_a_real_tool"


@pytest.fixture(scope="module")
def tools():
    """Import the server (no Resolve, no network) and list registered tools."""
    import davinci_resolve_mcp.server as server

    return asyncio.run(server.mcp.list_tools())


@pytest.fixture(scope="module")
def cluster_tools(tools):
    """Map cluster tool name -> registered Tool object."""
    return {t.name: t for t in tools if t.name in CLUSTER_TOOL_NAMES}


def test_imports_with_no_resolve(tools):
    # A successful import (handled by the fixture) already proves the server
    # doesn't touch Resolve at import time. Confirm the list came back non-empty.
    assert len(tools) > 0


def test_all_cluster_tools_are_registered(tools):
    names = {t.name for t in tools}
    present = names & CLUSTER_TOOL_NAMES
    missing = CLUSTER_TOOL_NAMES - present
    assert not missing, f"cluster tools missing from registration: {sorted(missing)}"
    assert present == CLUSTER_TOOL_NAMES
    assert len(CLUSTER_TOOL_NAMES) == 15


def test_every_cluster_tool_has_a_non_empty_description(cluster_tools):
    missing = sorted(
        name for name, t in cluster_tools.items() if not (t.description or "").strip()
    )
    assert not missing, f"cluster tools missing a non-empty description: {missing}"


def test_cluster_tools_registered_exactly_once(tools):
    names = [t.name for t in tools if t.name in CLUSTER_TOOL_NAMES]
    counts = Counter(names)
    duplicates = {name: n for name, n in counts.items() if n > 1}
    assert not duplicates, f"cluster tool registered more than once: {duplicates}"
    assert len(names) == 15


def test_cluster_tool_names_are_globally_unique(tools):
    """No cluster name collides with any other tool across the whole surface."""
    all_names = [t.name for t in tools]
    counts = Counter(all_names)
    # Restrict the duplicate report to names in our cluster so a failure
    # elsewhere in the server doesn't mask this cluster's contract; but a
    # cluster name appearing >1 time anywhere is what we forbid.
    cluster_duplicates = {
        name: n
        for name, n in counts.items()
        if n > 1 and name in CLUSTER_TOOL_NAMES
    }
    assert not cluster_duplicates, (
        f"cluster tool names registered more than once across the server: "
        f"{cluster_duplicates}"
    )


def test_bogus_tool_name_is_absent(tools):
    """EDGE/FAILURE: a made-up name must NOT be registered.

    If this fails, the server is registering tools via some catch-all path
    that would make every presence assertion above vacuously true.
    """
    names = {t.name for t in tools}
    assert BOGUS_TOOL_NAME not in names, (
        f"bogus tool name {BOGUS_TOOL_NAME!r} is registered — a catch-all "
        f"registration is exposing arbitrary tool names"
    )
