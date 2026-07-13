"""Exposure + fake-object unit tests for the color-cluster tools.

Mirrors ``tests/test_tool_exposure.py``: a module-scoped fixture imports
``davinci_resolve_mcp.server`` with no DaVinci Resolve instance present and
no network access, then lists the registered tools via
``asyncio.run(mcp.list_tools())``. Importing the server must succeed even
though Resolve isn't running — the architecture requires every Resolve
connection to be lazy (established only inside a tool call), never at import
time.

Two kinds of assertion live here:

- Exposure: all 16 color-cluster tools added in the full-studio-coverage
  build are registered, globally unique, and each carries a non-empty
  description so an LLM client can use them.
- Fake-object behavior: monkeypatching
  ``davinci_resolve_mcp.tools.color._get_timeline_item`` / ``._conn`` lets us
  drive a tool body against fakes (no Resolve) and assert on the exact string
  it returns — the ``_ok`` success message for a happy path, and the
  ``_check_choice`` error string for an invalid-choice failure path.
"""

from __future__ import annotations

import asyncio
from collections import Counter

import pytest

import davinci_resolve_mcp.tools.color as color


# The 16 color-cluster tools this task covers.
COLOR_CLUSTER_TOOLS = (
    "copy_grades",
    "export_lut",
    "rename_color_version",
    "reset_node_colors",
    "set_node_cache_mode",
    "get_node_cache_mode",
    "get_tools_in_node",
    "apply_arri_cdl_lut",
    "list_color_groups",
    "add_color_group",
    "delete_color_group",
    "assign_clip_to_color_group",
    "remove_clip_from_color_group",
    "get_color_group_node_graph",
    "analyze_dolby_vision",
    "get_timeline_node_graph",
)


@pytest.fixture(scope="module")
def tools():
    """Import the server (no Resolve, no network) and list registered tools."""
    import davinci_resolve_mcp.server as server

    return asyncio.run(server.mcp.list_tools())


# ── Exposure ─────────────────────────────────────────────────────────────


def test_all_color_cluster_tools_are_registered(tools):
    names = {t.name for t in tools}
    missing = [name for name in COLOR_CLUSTER_TOOLS if name not in names]
    assert not missing, f"color-cluster tools not registered: {missing}"


def test_color_cluster_tools_are_unique(tools):
    counts = Counter(t.name for t in tools)
    duplicates = {
        name: counts[name] for name in COLOR_CLUSTER_TOOLS if counts[name] > 1
    }
    assert not duplicates, f"duplicate color-cluster tools: {duplicates}"


def test_color_cluster_tools_have_non_empty_descriptions(tools):
    by_name = {t.name: t for t in tools}
    missing = [
        name
        for name in COLOR_CLUSTER_TOOLS
        if not (by_name[name].description or "").strip()
    ]
    assert not missing, f"color-cluster tools missing a description: {missing}"


# ── Fake objects ─────────────────────────────────────────────────────────


class _FakeItem:
    """Minimal stand-in for a Resolve ``TimelineItem``.

    Records the argument passed to ``CopyGrades`` and returns ``copy_result``
    (default ``True``) so we can assert the tool's success path.
    """

    def __init__(self, copy_result=True):
        self._copy_result = copy_result
        self.copy_grades_arg = None

    def CopyGrades(self, targets):  # noqa: N802 - mirrors Resolve's API
        self.copy_grades_arg = list(targets)
        return self._copy_result


def test_copy_grades_happy_path_returns_success_string(monkeypatch):
    """copy_grades with a truthy CopyGrades result returns the _ok success msg."""
    source = _FakeItem(copy_result=True)
    target = _FakeItem()
    handed_out = [source, target]

    def fake_get_timeline_item(track_type, track_index, item_index):
        # First call locates the source, subsequent calls locate targets.
        return handed_out.pop(0)

    monkeypatch.setattr(color, "_get_timeline_item", fake_get_timeline_item)
    # copy_grades doesn't reach _conn, but the contract patches it too so a
    # stray connection attempt can never touch a real Resolve.
    monkeypatch.setattr(
        color, "_conn", lambda: pytest.fail("copy_grades must not call _conn")
    )

    result = color.copy_grades(
        targets='[{"track_type": "video", "track_index": 1, "item_index": 1}]'
    )

    assert result == "Copied grade to 1 target clip(s)."
    # The source clip's CopyGrades received exactly the one located target.
    assert source.copy_grades_arg == [target]


# ── Edge / failure: invalid-choice paths return the _check_choice string ──


def test_export_lut_invalid_export_type_returns_choice_error(monkeypatch):
    """An invalid export_type is rejected before any Resolve access."""
    monkeypatch.setattr(
        color,
        "_get_timeline_item",
        lambda *a, **k: pytest.fail("must not locate an item on a bad choice"),
    )
    monkeypatch.setattr(
        color, "_conn", lambda: pytest.fail("must not connect on a bad choice")
    )

    result = color.export_lut(export_type="not-a-lut-type", path="/tmp/out")

    assert result == (
        "Invalid export_type 'not-a-lut-type'. Must be one of: "
        "17ptcube, 33ptcube, 65ptcube, panasonicvlut."
    )


def test_get_color_group_node_graph_invalid_stage_returns_choice_error(
    monkeypatch,
):
    """An invalid stage is rejected before any Resolve access."""
    monkeypatch.setattr(
        color, "_conn", lambda: pytest.fail("must not connect on a bad choice")
    )

    result = color.get_color_group_node_graph(
        group_name="Sky", stage="sideways"
    )

    assert result == (
        "Invalid stage 'sideways'. Must be one of: pre, post."
    )
