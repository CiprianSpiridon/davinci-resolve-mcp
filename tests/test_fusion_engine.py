"""Exposure test for the Fusion node-engine + comp-authoring + keyframe cluster.

This test imports the server package with no DaVinci Resolve instance present
and no network access, then asserts — purely from the registered tool list
returned by ``mcp.list_tools()`` — that the Fusion engine cluster holds up its
contract:

- every tool the cluster registers is present in ``mcp.list_tools()``, each
  exactly once, with a non-empty description;
- the cluster's tool names are globally unique across the *whole* server
  surface (live + offline), so no name is double-registered or shadowed by a
  different tool;
- a bogus, never-registered tool name is ABSENT, guarding against a catch-all
  registration that would make any name "pass".

The cluster spans Fusion node authoring (``fusion_add_tool``,
``fusion_connect_input``, ``fusion_set_input``, ``fusion_get_input``,
``fusion_list_inputs``), comp/title authoring (``apply_macro_to_clip``,
``execute_fusion_lua``, ``set_title_text``) and keyframe control
(``fusion_add_keyframe``, ``add_transform_keyframe``,
``delete_transform_keyframe``, ``get_transform_keyframes``). It exists so this
cluster is validated on its own, not only by the global count gate — closing
the founder-review test-coverage WARN. It mirrors ``tests/test_offline_exposure.py``.

Importing ``davinci_resolve_mcp.server`` must succeed even though Resolve isn't
running — the architecture requires every Resolve connection to be lazy
(established only inside a tool call), never at import time. This test never
starts, mocks, or otherwise touches a Resolve instance.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import Counter

import pytest

# Make the ``src`` layout importable without an editable install, mirroring how
# the repo's other exposure tests are run (PYTHONPATH=src). Self-contained so
# the task's plain validate command works as-is.
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# The Fusion node-engine + comp-authoring + keyframe cluster (TASK-034).
FUSION_ENGINE_TOOL_NAMES = frozenset(
    {
        # Fusion node authoring
        "fusion_add_tool",
        "fusion_connect_input",
        "fusion_set_input",
        "fusion_get_input",
        "fusion_list_inputs",
        # comp / title authoring
        "apply_macro_to_clip",
        "execute_fusion_lua",
        "set_title_text",
        # keyframe control
        "fusion_add_keyframe",
        "add_transform_keyframe",
        "delete_transform_keyframe",
        "get_transform_keyframes",
    }
)

# A name that must never be registered — guards against a catch-all/wildcard
# registration that would let *any* string resolve to a tool.
BOGUS_TOOL_NAME = "fusion_not_a_real_tool"


@pytest.fixture(scope="module")
def tools():
    """Import the server (no Resolve, no network) and list registered tools."""
    import davinci_resolve_mcp.server as server

    return asyncio.run(server.mcp.list_tools())


@pytest.fixture(scope="module")
def cluster_tools(tools):
    """Map Fusion-engine tool name -> registered Tool object."""
    return {t.name: t for t in tools if t.name in FUSION_ENGINE_TOOL_NAMES}


def test_imports_with_no_resolve(tools):
    # A successful import (handled by the fixture) already proves the server
    # doesn't touch Resolve at import time.
    assert len(tools) > 0


def test_all_cluster_tools_registered(tools):
    names = {t.name for t in tools}
    missing = FUSION_ENGINE_TOOL_NAMES - names
    assert not missing, f"Fusion engine tools missing from registration: {sorted(missing)}"


def test_cluster_tools_registered_exactly_once(tools):
    names = [t.name for t in tools if t.name in FUSION_ENGINE_TOOL_NAMES]
    counts = Counter(names)
    duplicates = {name: n for name, n in counts.items() if n > 1}
    assert not duplicates, f"Fusion engine tool registered more than once: {duplicates}"
    assert len(names) == len(FUSION_ENGINE_TOOL_NAMES)


def test_every_cluster_tool_has_a_non_empty_description(cluster_tools):
    missing = FUSION_ENGINE_TOOL_NAMES - set(cluster_tools)
    assert not missing, f"Fusion engine tools not found among registered tools: {sorted(missing)}"

    empty = sorted(
        name for name, t in cluster_tools.items() if not (t.description or "").strip()
    )
    assert not empty, f"Fusion engine tools missing a non-empty description: {empty}"


def test_cluster_tool_names_are_globally_unique(tools):
    # Global uniqueness across the whole server surface (live + offline): no
    # cluster name may collide with any other registered tool, and none of the
    # cluster names may be double-registered.
    all_names = [t.name for t in tools]
    counts = Counter(all_names)
    cluster_dupes = {
        name: n
        for name, n in counts.items()
        if n > 1 and name in FUSION_ENGINE_TOOL_NAMES
    }
    assert not cluster_dupes, (
        f"Fusion engine tool names collide/duplicate on the server surface: {cluster_dupes}"
    )


def test_bogus_tool_name_is_absent(tools):
    # EDGE/FAILURE: a never-registered name must NOT appear — otherwise a
    # catch-all registration would make this whole exposure test vacuous.
    names = {t.name for t in tools}
    assert BOGUS_TOOL_NAME not in names, (
        f"bogus tool name {BOGUS_TOOL_NAME!r} is registered — a catch-all "
        f"registration would make this exposure test meaningless"
    )
    assert BOGUS_TOOL_NAME not in FUSION_ENGINE_TOOL_NAMES
