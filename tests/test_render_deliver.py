"""Exposure + no-Resolve failure-path tests for the render-deliver tools.

These tests import the server package with no DaVinci Resolve instance
present and no network access, then assert on the registered tool surface
returned by ``mcp.list_tools()`` and on the house connection-error path.

Two things are checked, mirroring ``tests/test_tool_exposure.py``:

- Each of the seven render-deliver tools (``quick_export``,
  ``get_quick_export_presets``, ``import_burn_in_preset``,
  ``export_burn_in_preset``, ``load_burn_in_preset``, ``save_render_preset``,
  ``delete_render_preset``) is registered exactly once and carries a
  non-empty description usable by an LLM client.
- The no-Resolve failure path: calling ``render.quick_export('YouTube')`` and
  ``render.save_render_preset('X')`` with no Resolve reachable returns a
  ``str`` beginning ``"Error:"`` and raises nothing. Every tool lazily
  connects inside its body and wraps any connection failure as an ``Error:``
  string rather than raising.

Importing the server must succeed even though Resolve isn't running: all
Resolve connections are lazy (established only inside a tool call), never at
import time. This module does NOT assert the global exact tool count.
"""

from __future__ import annotations

import asyncio
from collections import Counter

import pytest


RENDER_DELIVER_TOOLS = [
    "quick_export",
    "get_quick_export_presets",
    "import_burn_in_preset",
    "export_burn_in_preset",
    "load_burn_in_preset",
    "save_render_preset",
    "delete_render_preset",
]


@pytest.fixture(scope="module")
def tools():
    """Import the server (no Resolve, no network) and list registered tools."""
    import davinci_resolve_mcp.server as server

    return asyncio.run(server.mcp.list_tools())


@pytest.mark.parametrize("name", RENDER_DELIVER_TOOLS)
def test_render_deliver_tool_registered_exactly_once(tools, name):
    counts = Counter(t.name for t in tools)
    assert counts[name] == 1, (
        f"expected render-deliver tool {name!r} registered exactly once, "
        f"found {counts[name]}"
    )


@pytest.mark.parametrize("name", RENDER_DELIVER_TOOLS)
def test_render_deliver_tool_has_non_empty_description(tools, name):
    by_name = {t.name: t for t in tools}
    tool = by_name.get(name)
    assert tool is not None, f"render-deliver tool {name!r} not registered"
    assert (tool.description or "").strip(), (
        f"render-deliver tool {name!r} must have a non-empty description"
    )


def test_quick_export_no_resolve_returns_error_string():
    """quick_export('YouTube') offline returns an 'Error:' str, raises nothing."""
    import davinci_resolve_mcp.server  # noqa: F401  (ensure tools are registered)
    import davinci_resolve_mcp.tools.render as render

    result = render.quick_export("YouTube")
    assert isinstance(result, str)
    assert result.startswith("Error:"), result


def test_save_render_preset_no_resolve_returns_error_string():
    """save_render_preset('X') offline returns an 'Error:' str, raises nothing."""
    import davinci_resolve_mcp.server  # noqa: F401  (ensure tools are registered)
    import davinci_resolve_mcp.tools.render as render

    result = render.save_render_preset("X")
    assert isinstance(result, str)
    assert result.startswith("Error:"), result
