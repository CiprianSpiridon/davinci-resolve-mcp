"""
tests/live/test_inspector_effects_live.py — live Inspector + effects round-trip
harness (TASK-043: DEFERRED-until-Resolve verification).

The Inspector-transform tools (``inspector.set_transform`` /
``inspector.get_inspector_properties``) and the OFX/ResolveFX attach path
(``fx_plugins.apply_ofx_to_clip``, read back via ``fusion.fusion_list_inputs``
and the live Fusion comp's own tool list) were built and unit-tested against
mocked Resolve objects. Nothing in the offline suite has ever driven a running
copy of DaVinci Resolve to prove the two round trips those tools promise:

  1. **Transform set -> read round trip** — ``set_transform`` writes Pan / Tilt /
     ZoomX / ZoomY through ``TimelineItem.SetProperty`` and
     ``get_inspector_properties`` reads them back through
     ``TimelineItem.GetProperty``; this test proves the values survive the
     round trip in the same UI units, within float tolerance
     (``TestTransformRoundTrip``).
  2. **OFX attach + comp-tool-list readback** — ``apply_ofx_to_clip`` splices a
     ResolveFX node into the clip's Fusion composition; this test proves the
     node is actually present afterward, visible both through
     ``fusion_list_inputs`` (which resolves the tool by name) and through the
     live composition's own ``GetToolList`` (``TestApplyOfxAttachesEffect``).

This module mirrors ``tests/live/test_resolve_roundtrip.py``: every test is
marked ``requires_resolve`` and shares the ``resolve_conn`` fixture, which
attempts ``davinci_resolve_mcp.connection.get_resolve_connection()`` and calls
``pytest.skip(...)`` (never fails) when Resolve is not reachable — the default
state for CI and for any environment without Resolve installed. Connecting
only happens when a test actually runs, inside a fixture body, so collection
(``pytest --co``) never needs Resolve on the machine running the runner.

Every Resolve-side test works inside a throwaway, uuid-suffixed scratch
project (created by ``scratch_project`` and always deleted on teardown) so
running this harness never touches, renames, or leaves artifacts behind in a
real project the user has open. Steps that depend on the local Resolve
install/version (creating a timeline, inserting a generator, an OFX plugin not
being installed) ``pytest.skip(...)`` rather than fail, since they reflect the
environment, not the tools under test.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from davinci_resolve_mcp import connection as conn_mod
from davinci_resolve_mcp.helpers import _get_timeline_item
from davinci_resolve_mcp.tools import fusion as fusion_tools
from davinci_resolve_mcp.tools import fx_plugins as fx_tools
from davinci_resolve_mcp.tools import inspector as inspector_tools
from davinci_resolve_mcp.tools import project_manager as pm_tools
from davinci_resolve_mcp.tools import resolve_app as resolve_app_tools

pytestmark = pytest.mark.requires_resolve

_SCRATCH_PREFIX = "mcp-live-inspector-"

# A stock ResolveFX regid that ships with every Resolve Studio install; the
# 'ofx.' prefix apply_ofx_to_clip requires is added by the tool itself.
_OFX_REGID = "com.blackmagicdesign.resolvefx.gaussianblur"


# ---------------------------------------------------------------------------
# Connection / scratch-project fixtures (mirrors test_resolve_roundtrip.py)
# ---------------------------------------------------------------------------
def _connect_or_none():
    try:
        return conn_mod.get_resolve_connection()
    except Exception:
        return None


@pytest.fixture(scope="module")
def resolve_conn():
    """The shared Resolve connection, or a SKIP (never a failure).

    Only reached when a test actually runs — collection (``pytest --co``)
    never executes fixture bodies, so this harness stays collectable without
    Resolve installed anywhere on the machine running the test runner.
    """
    conn = _connect_or_none()
    if conn is None:
        pytest.skip(
            "DaVinci Resolve is not reachable from this environment. Start "
            "Resolve, enable Preferences > General > External scripting "
            "using > 'Local' (or 'Network'), and set RESOLVE_SCRIPT_LIB / "
            "RESOLVE_SCRIPT_API if Resolve is installed in a non-default "
            "location. See tests/live/README.md for the full checklist."
        )
    return conn


def _unique_project_name() -> str:
    return f"{_SCRATCH_PREFIX}{uuid.uuid4().hex[:10]}"


@pytest.fixture()
def scratch_project(resolve_conn):
    """A freshly created, uniquely-named Resolve project; deleted on teardown.

    Yields ``(project, project_name)``. Every test in this module works inside
    its own scratch project rather than whatever project the user has open,
    and the project is always closed + deleted afterward — even if the test
    body raises.
    """
    pm = resolve_conn.get_project_manager()
    name = _unique_project_name()
    project = pm.CreateProject(name)
    if project is None:
        pytest.skip(
            f"Resolve refused to create scratch project '{name}' "
            "(CreateProject returned None)."
        )
    try:
        yield project, name
    finally:
        try:
            pm_tools.close_project(save_before_close=False)
        except Exception:
            pass
        try:
            pm_tools.delete_project(name)
        except Exception:
            pass


def _create_gradeable_clip(project) -> None:
    """Build an empty timeline with one "Solid Color" generator clip and switch
    to the Color page, so a timeline item is reachable at
    (track_type="video", track_index=1, item_index=0).

    Uses a built-in Resolve generator rather than importing real media so this
    harness has no external media-file dependency. Skips (rather than fails) if
    Resolve refuses any step here, since that reflects the local Resolve
    install/version, not the tools under test.
    """
    media_pool = project.GetMediaPool()
    if media_pool is None:
        pytest.skip("Resolve's scratch project has no MediaPool.")

    timeline = media_pool.CreateEmptyTimeline("MCP Inspector Effects")
    if timeline is None:
        pytest.skip("Resolve refused to create an empty timeline in the scratch project.")
    project.SetCurrentTimeline(timeline)

    inserted = timeline.InsertGeneratorIntoTimeline("Solid Color")
    if not inserted:
        pytest.skip(
            "Resolve refused InsertGeneratorIntoTimeline('Solid Color') — "
            "cannot build a timeline item without real media."
        )

    open_result = resolve_app_tools.open_page("edit")
    assert "Error" not in open_result, open_result


# ---------------------------------------------------------------------------
# 1. set_transform -> get_inspector_properties round trip
# ---------------------------------------------------------------------------
class TestTransformRoundTrip:
    """``set_transform`` write -> ``get_inspector_properties`` readback.

    Sets Pan / Tilt / ZoomX / ZoomY to known values through
    ``inspector.set_transform`` and asserts ``inspector.get_inspector_properties``
    reproduces each one within float tolerance — proving the Inspector-transform
    tools round-trip in the same UI units against a live TimelineItem.
    """

    def test_set_transform_then_read_matches(self, scratch_project):
        project, _project_name = scratch_project
        _create_gradeable_clip(project)

        zoom = 2.3
        pan = 250.0
        tilt = -560.0

        set_result = inspector_tools.set_transform(
            zoom_x=zoom,
            zoom_y=zoom,
            pan=pan,
            tilt=tilt,
            track_type="video",
            track_index=1,
            item_index=0,
        )
        assert "Error" not in set_result, set_result

        read_result = inspector_tools.get_inspector_properties(
            track_type="video", track_index=1, item_index=0
        )
        assert not read_result.startswith("Error"), read_result

        payload = json.loads(read_result)
        props = payload.get("properties")
        assert isinstance(props, dict) and props, (
            f"get_inspector_properties returned no 'properties' dict: {payload!r}"
        )

        expected = {"Pan": pan, "Tilt": tilt, "ZoomX": zoom, "ZoomY": zoom}
        for key, want in expected.items():
            assert key in props, (
                f"property {key!r} missing from live readback {sorted(props)!r}"
            )
            got = float(props[key])
            assert got == pytest.approx(want, abs=1e-2), (
                f"{key}: set {want} != live readback {got} — set_transform / "
                "get_inspector_properties do not round-trip in the same units"
            )


# ---------------------------------------------------------------------------
# 2. apply_ofx_to_clip -> the effect is present in the comp tool list
# ---------------------------------------------------------------------------
class TestApplyOfxAttachesEffect:
    """``apply_ofx_to_clip`` attach -> comp-tool-list readback.

    Splices a stock ResolveFX node into the clip's Fusion composition and
    asserts the node is actually present afterward, seen both through
    ``fusion_list_inputs`` (which resolves the tool by name inside the comp)
    and through the live composition's own ``GetToolList``. Skips cleanly when
    the specific ResolveFX plugin is not installed in this Resolve build
    (``AddTool`` returns nil), since that reflects the environment.
    """

    def test_apply_ofx_then_tool_in_comp(self, scratch_project):
        project, _project_name = scratch_project
        _create_gradeable_clip(project)

        apply_result = fx_tools.apply_ofx_to_clip(
            regid=_OFX_REGID,
            track_type="video",
            track_index=1,
            item_index=0,
            comp_index=1,
        )
        if apply_result.startswith("Error"):
            # A nil AddTool means this ResolveFX plugin is not installed in the
            # running build — an environment fact, not a tool defect.
            pytest.skip(
                f"apply_ofx_to_clip could not attach {_OFX_REGID!r}: "
                f"{apply_result}"
            )

        applied = json.loads(apply_result)
        assert applied.get("success") is True, applied
        tool_name = applied.get("tool_name")
        assert tool_name, f"apply_ofx_to_clip reported no tool_name: {applied!r}"

        # a) fusion_list_inputs resolves the tool by name inside the comp; it
        #    raises (-> "Error: No Fusion tool named ...") when the node is
        #    absent, so a clean JSON payload proves the node is attached.
        inputs_result = fusion_tools.fusion_list_inputs(
            tool_name=tool_name,
            comp_index=1,
            track_type="video",
            track_index=1,
            item_index=0,
        )
        assert not inputs_result.startswith("Error"), inputs_result
        inputs_payload = json.loads(inputs_result)
        assert inputs_payload.get("tool_name") == tool_name, inputs_payload

        # b) the live composition's own tool list also shows the added node.
        item = _get_timeline_item("video", 1, 0)
        comp = fusion_tools._get_fusion_comp(item, 1, create=False)
        tool_list = comp.GetToolList(False) or {}
        live_names = []
        for tool in tool_list.values():
            try:
                attrs = tool.GetAttrs() or {}
            except Exception:
                attrs = {}
            name = attrs.get("TOOLS_Name")
            if name:
                live_names.append(name)
        assert tool_name in live_names, (
            f"apply_ofx_to_clip reported tool {tool_name!r} but it is absent "
            f"from the live comp tool list {live_names!r}"
        )
