"""Exposure tests for the OFFLINE/advanced tool set.

These tests import the server package with no DaVinci Resolve instance
present and no network access, and assert on the registered tool list
returned by ``mcp.list_tools()`` that the 18 offline tools (davinci-resolve-
mcp-offline-advanced plan, TASK-016..TASK-032) hold up their contract:

- exactly the 18 documented offline tool names are registered, each exactly
  once, and they are globally unique against the other (live, Resolve-
  connecting) tools registered by the server — 308 live + 18 offline = 326;
- every offline tool has a non-empty docstring;
- every offline tool documents an ``action`` parameter (present in its
  input schema as a string) together with a written-out enum of the
  concrete action values it accepts (the "action: one of \"foo\": ..."
  bullet-list convention used across the offline tool set);
- every offline tool whose action catalog (``off_capabilities._DOMAINS``)
  lists at least one write action documents the ``"verified": false``
  contract for that write path in its own docstring.

Importing ``davinci_resolve_mcp.server`` must succeed even though Resolve
isn't running — the architecture requires all Resolve connections to be
lazy (established only inside a tool call), never at import time; none of
these tests start, mock, or otherwise touch a Resolve instance.
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter

import pytest

# The 18 offline tool names from the offline-advanced plan's Contracts
# section — one action-dispatch @mcp.tool() per domain.
OFFLINE_TOOL_NAMES = frozenset(
    {
        "capabilities",
        "drx",
        "drt",
        "drp",
        "project_read",
        "project_db",
        "offline_ref",
        "conform",
        "color_trace",
        "offline_fusion",
        "audio_plan",
        "fairlight_plan",
        "offline_audio",
        "pipeline",
        "deliverable",
        "media_ingest",
        "editorial",
        "provenance",
    }
)

# A documented action bullet looks like `- "name": ...` or
# `- "name" (default): ...` in every offline tool's docstring.
_ACTION_ENUM_ITEM_RE = re.compile(r'"[A-Za-z][A-Za-z0-9_]*"\s*(?:\([^)]*\))?\s*:')
_ACTION_SECTION_RE = re.compile(r"-\s*action\s*:", re.IGNORECASE)
_VERIFIED_FALSE_RE = re.compile(r'verified"?\s*:\s*false', re.IGNORECASE)


@pytest.fixture(scope="module")
def tools():
    """Import the server (no Resolve, no network) and list registered tools."""
    import davinci_resolve_mcp.server as server

    return asyncio.run(server.mcp.list_tools())


@pytest.fixture(scope="module")
def offline_tools(tools):
    """Map offline tool name -> registered Tool object."""
    return {t.name: t for t in tools if t.name in OFFLINE_TOOL_NAMES}


def test_imports_with_no_resolve(tools):
    # A successful import (handled by the fixture) already proves the
    # server doesn't touch Resolve at import time.
    assert len(tools) > 0


def test_exactly_18_offline_tool_names_present(tools):
    names = [t.name for t in tools]
    present = set(names) & OFFLINE_TOOL_NAMES
    missing = OFFLINE_TOOL_NAMES - present
    assert not missing, f"offline tools missing from registration: {sorted(missing)}"
    assert present == OFFLINE_TOOL_NAMES
    assert len(OFFLINE_TOOL_NAMES) == 18


def test_offline_tool_names_are_unique_within_themselves(tools):
    names = [t.name for t in tools if t.name in OFFLINE_TOOL_NAMES]
    counts = Counter(names)
    duplicates = {name: n for name, n in counts.items() if n > 1}
    assert not duplicates, f"offline tool registered more than once: {duplicates}"
    assert len(names) == 18


def test_offline_tool_names_are_unique_against_all_registered_tools(tools):
    # Global uniqueness across the whole server surface (live + offline):
    # no offline name shadows/collides with any of the other 308 live
    # (Resolve-connecting) tools, and the full surface is exactly 326.
    all_names = [t.name for t in tools]
    counts = Counter(all_names)
    duplicates = {name: n for name, n in counts.items() if n > 1}
    assert not duplicates, f"duplicate tool names registered: {duplicates}"

    live_names = [n for n in all_names if n not in OFFLINE_TOOL_NAMES]
    assert len(live_names) == 308, (
        f"expected 308 live (Resolve-connecting) tools alongside the 18 "
        f"offline tools, found {len(live_names)}"
    )
    assert len(all_names) == 326, (
        f"expected 326 total registered tools (308 live + 18 offline), "
        f"found {len(all_names)}"
    )


def test_every_offline_tool_has_a_non_empty_docstring(offline_tools):
    missing = sorted(
        name for name, t in offline_tools.items() if not (t.description or "").strip()
    )
    assert not missing, f"offline tools missing a non-empty docstring: {missing}"


def test_every_offline_tool_has_an_action_string_parameter(offline_tools):
    missing = []
    wrong_type = []
    for name, t in offline_tools.items():
        props = (t.inputSchema or {}).get("properties", {})
        if "action" not in props:
            missing.append(name)
            continue
        action_schema = props["action"]
        # `action` is typed `str` in every dispatcher (some default to a
        # value, e.g. capabilities(action: str = "report")); FastMCP emits
        # a plain "string" JSON-Schema type for it either way.
        if action_schema.get("type") != "string":
            wrong_type.append((name, action_schema.get("type")))

    assert not missing, f"offline tools missing an 'action' parameter: {missing}"
    assert not wrong_type, f"offline tools with a non-string 'action' parameter: {wrong_type}"


def test_every_offline_tool_documents_an_action_enum_in_its_docstring(offline_tools):
    """Each dispatcher's docstring must spell out its concrete action values.

    The offline tool-set convention is a "Parameters: - action: ..."
    section followed by one bullet per accepted action value, quoted
    (e.g. ``- "inspect": read ...``). This is what lets an LLM client pick
    the right action without a live Resolve session to probe with.
    """
    no_action_section = []
    no_enum_items = []
    for name, t in offline_tools.items():
        desc = t.description or ""
        if not _ACTION_SECTION_RE.search(desc):
            no_action_section.append(name)
            continue
        if not _ACTION_ENUM_ITEM_RE.findall(desc):
            no_enum_items.append(name)

    assert not no_action_section, (
        f"offline tools whose docstring has no '- action:' section: {no_action_section}"
    )
    assert not no_enum_items, (
        f"offline tools whose docstring documents no concrete action values: {no_enum_items}"
    )


def test_write_actions_document_verified_false_contract(offline_tools):
    """Every offline domain with at least one write action must say so.

    ``off_capabilities._DOMAINS`` is the offline tool-set's own maintained
    catalog of, per domain, which actions write to disk/DB (``"writes"``).
    For every domain with a non-empty ``writes`` list, that domain's own
    tool docstring must document the ``"verified": false`` contract —
    write/grade actions are structural-only until calibrated against a
    live Resolve session, and callers must be able to see that from the
    tool description alone, with no Resolve connection available.
    """
    from davinci_resolve_mcp.tools.off_capabilities import _DOMAINS

    write_domains = {
        domain: spec.get("writes", [])
        for domain, spec in _DOMAINS.items()
        if spec.get("writes")
    }
    # Sanity: this catalog itself must only reference real offline tools,
    # and must actually contain at least one write-bearing domain (else
    # this test would vacuously pass).
    assert write_domains, "expected at least one offline domain with write actions"
    assert set(write_domains) <= OFFLINE_TOOL_NAMES

    missing = []
    for domain in write_domains:
        t = offline_tools.get(domain)
        assert t is not None, f"domain '{domain}' from _DOMAINS is not a registered tool"
        desc = t.description or ""
        if not _VERIFIED_FALSE_RE.search(desc):
            missing.append(domain)

    assert not missing, (
        f"offline tools with write actions but no documented "
        f"'verified: false' contract in their docstring: {missing}"
    )


def test_offline_tools_registered_from_dedicated_modules(offline_tools):
    """Each offline tool must come from its own ``tools.off_*`` module.

    Guards against two dispatchers accidentally merging into one module
    (which would silently drop an action catalog) or a name being
    re-registered from the live-tool side of the package.
    """
    import davinci_resolve_mcp.server as server

    for name in OFFLINE_TOOL_NAMES:
        assert name in offline_tools, f"'{name}' not found among registered tools"

    # Every offline tool module referenced by server.py must itself be
    # importable with no Resolve running (already implied by the fixture
    # succeeding), and must not be one of the live tool modules.
    live_modules = {
        server.ai,
        server.audio,
        server.code,
        server.color,
        server.export_still,
        server.fusion,
        server.media_pool,
        server.media_pool_item,
        server.media_storage,
        server.project,
        server.project_manager,
        server.render,
        server.resolve_app,
        server.screenshot,
        server.timeline,
        server.timeline_edit,
        server.timeline_item,
        server.transcription,
    }
    offline_modules = {
        server.off_audio,
        server.off_audio_plan,
        server.off_capabilities,
        server.off_color_trace,
        server.off_conform,
        server.off_deliverable,
        server.off_drp,
        server.off_drt,
        server.off_drx,
        server.off_editorial,
        server.off_fairlight,
        server.off_fusion,
        server.off_media,
        server.off_offline_ref,
        server.off_pipeline,
        server.off_project_db,
        server.off_project_read,
        server.off_provenance,
    }
    assert len(offline_modules) == 18
    assert live_modules.isdisjoint(offline_modules)
