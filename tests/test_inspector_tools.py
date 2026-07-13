"""Offline unit tests for the Video-tab Inspector schema (tools/inspector.py).

These tests import the server package with no DaVinci Resolve instance
present and no network access. They assert that the connection-free
``inspector_property_reference()`` tool exposes the whole documented
schema — every scalar property key and every enum — with a clean
name<->int round-trip, and that the panelled Inspector setters are all
registered on the live server surface.

Concretely they check:

- ``inspector_property_reference()`` runs with NO Resolve connection and
  returns well-formed JSON (it reads the in-module ``_ENUMS`` /
  ``_PROPERTY_SCHEMA`` constants straight from ``tools/inspector.py``);
- ``CompositeMode`` has exactly 32 entries (Normal..InvertedLum, codes
  0-31), and for a sample drawn from *every* enum a
  ``name -> int -> name`` round-trip returns the original name — the
  ``name_to_int`` and ``int_to_name`` tables in the reference are exact
  inverses;
- the Inspector setters (``set_transform``, ``set_cropping``,
  ``set_composite``, ``set_dynamic_zoom``, ``set_retime_and_scaling``,
  ``reset_transform``) plus the read/reference tools
  (``get_inspector_properties``, ``inspector_property_reference``) are all
  registered exactly once on ``mcp.list_tools()``;
- EDGE/FAILURE: the reference does NOT fabricate keys — a bogus property
  key, a bogus enum name, and a bogus enum member are all absent. This
  guards against a catch-all that would answer for any key handed to it.

Importing ``davinci_resolve_mcp.server`` must succeed even though Resolve
isn't running — the architecture requires all Resolve connections to be
lazy (established only inside a tool call), never at import time; none of
these tests start, mock, or otherwise touch a Resolve instance.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

import pytest

# Make the ``src/`` layout importable when the package isn't pip-installed
# (keeps ``python -m pytest tests/test_inspector_tools.py`` self-contained).
_SRC = Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# The panelled Inspector setters plus the read/reference tools that
# tools/inspector.py must register on the server surface.
INSPECTOR_SETTER_NAMES = frozenset(
    {
        "set_transform",
        "set_cropping",
        "set_composite",
        "set_dynamic_zoom",
        "set_retime_and_scaling",
        "reset_transform",
    }
)
INSPECTOR_READ_NAMES = frozenset(
    {
        "get_inspector_properties",
        "inspector_property_reference",
    }
)
INSPECTOR_TOOL_NAMES = INSPECTOR_SETTER_NAMES | INSPECTOR_READ_NAMES


@pytest.fixture(scope="module")
def tools():
    """Import the server (no Resolve, no network) and list registered tools."""
    import davinci_resolve_mcp.server as server

    return asyncio.run(server.mcp.list_tools())


@pytest.fixture(scope="module")
def reference():
    """The parsed JSON returned by inspector_property_reference() (no Resolve)."""
    import davinci_resolve_mcp.tools.inspector as inspector

    raw = inspector.inspector_property_reference()
    assert isinstance(raw, str), "inspector_property_reference must return a str"
    return json.loads(raw)


# ── Runs with no Resolve ─────────────────────────────────────────────────


def test_reference_runs_with_no_resolve_and_returns_json(reference):
    # Reaching this fixture at all proves the tool ran without a Resolve
    # connection (the fixture parsed its JSON output). Confirm the top-level
    # shape the schema promises.
    assert isinstance(reference, dict)
    assert "properties" in reference and isinstance(reference["properties"], dict)
    assert "enums" in reference and isinstance(reference["enums"], dict)
    assert reference["properties"], "property schema must not be empty"
    assert reference["enums"], "enum tables must not be empty"


# ── CompositeMode size + round-trip for a sample of each enum ─────────────


def test_composite_mode_has_32_entries(reference):
    name_to_int = reference["enums"]["CompositeMode"]["name_to_int"]
    assert len(name_to_int) == 32, (
        f"CompositeMode must have 32 entries (Normal..InvertedLum, 0-31), "
        f"found {len(name_to_int)}"
    )
    # The 32 codes must be exactly 0..31, each used once.
    codes = sorted(name_to_int.values())
    assert codes == list(range(32)), f"CompositeMode codes must be 0-31, got {codes}"
    # A concrete anchor from the UI: Multiply is code 4.
    assert name_to_int["Multiply"] == 4


def test_name_int_name_round_trips_for_a_sample_of_each_enum(reference):
    """For a sample drawn from every enum, name -> int -> name is identity.

    ``name_to_int`` and ``int_to_name`` must be exact inverses so a caller
    can map a stored integer back to its name (and vice versa) offline.
    """
    enums = reference["enums"]
    # Every documented enum must be represented.
    expected_enums = {
        "DynamicZoomEase",
        "CompositeMode",
        "RetimeProcess",
        "MotionEstimation",
        "Scaling",
        "ResizeFilter",
    }
    assert expected_enums <= set(enums), (
        f"missing enums from reference: {expected_enums - set(enums)}"
    )

    for enum_name, tables in enums.items():
        name_to_int = tables["name_to_int"]
        int_to_name = tables["int_to_name"]
        assert name_to_int, f"{enum_name} name_to_int is empty"

        # Sample = first, middle, and last members of each enum (a
        # representative sample of every enum, per the acceptance criterion).
        names = list(name_to_int.keys())
        sample = {names[0], names[len(names) // 2], names[-1]}
        for name in sample:
            code = name_to_int[name]
            # int_to_name is JSON, so its keys are strings.
            back = int_to_name[str(code)]
            assert back == name, (
                f"{enum_name}: round-trip failed for {name!r} "
                f"(-> {code} -> {back!r})"
            )

        # The two tables are exact inverses across ALL members, not just the
        # sample — no collisions, no dangling codes.
        rebuilt = {str(code): nm for nm, code in name_to_int.items()}
        assert rebuilt == int_to_name, (
            f"{enum_name}: int_to_name is not the exact inverse of name_to_int"
        )


# ── Setters + reference tool registered ──────────────────────────────────


def test_inspector_tools_are_registered_exactly_once(tools):
    counts = Counter(t.name for t in tools)
    for name in INSPECTOR_TOOL_NAMES:
        assert counts[name] == 1, (
            f"inspector tool {name!r} must be registered exactly once, "
            f"found {counts[name]}"
        )


def test_inspector_setters_are_registered(tools):
    names = {t.name for t in tools}
    missing = INSPECTOR_SETTER_NAMES - names
    assert not missing, f"inspector setters not registered: {sorted(missing)}"


# ── EDGE/FAILURE: no fabricated keys ─────────────────────────────────────


def test_reference_does_not_fabricate_an_unknown_property_key(reference):
    """A bogus property key must be absent — the reference is not a catch-all."""
    properties = reference["properties"]
    assert "BogusKey" not in properties
    assert "NotARealProperty" not in properties
    # And every key present is a documented, non-empty schema entry.
    for key, spec in properties.items():
        assert key.strip(), "property key must not be blank"
        assert isinstance(spec, dict) and spec, f"schema for {key!r} must be a dict"


def test_reference_does_not_fabricate_an_unknown_enum_or_member(reference):
    """Bogus enum names and bogus enum members must both be absent."""
    enums = reference["enums"]
    assert "BogusEnum" not in enums
    # A bogus member is not present in a real enum's name<->int tables, and
    # cannot be produced by the reference for a key it never documented.
    composite = enums["CompositeMode"]
    assert "BogusMode" not in composite["name_to_int"]
    assert "999" not in composite["int_to_name"]
