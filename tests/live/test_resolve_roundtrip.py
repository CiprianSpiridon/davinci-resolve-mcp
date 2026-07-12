"""
tests/live/test_resolve_roundtrip.py — live-calibration round-trip harness
(TASK-038: DEFERRED-until-Resolve verification).

The OFFLINE/advanced tool set (``formats/``, ``store/``, ``grading/``,
``tools/off_*.py``) was built entirely from reverse-engineered file formats
``.drx`` fixtures in ``tests/fixtures/drx/``) and validated *offline* — every
write action ships with a ``"verified": false`` field because nothing in
this repo's test suite has ever driven a running copy of DaVinci Resolve.

This module is the harness that closes that gap, WHEN a machine with Resolve
installed and running is available. It exercises the three round trips that
cannot be proven any other way:

  1. **.drp round trip** — author a ``.drp`` offline, import it into Resolve,
     and check the live project/timeline/Media-Pool state matches what was
     authored (``TestDrpRoundTrip``).
  2. **.drx write + CDL panel readback** — patch a ``.drx`` node's
     ``saturation`` parameter offline, apply the grade to a live clip via
     ``ApplyGradeFromDRX``, and read the CDL back through
     ``TimelineItem.GetCDL()`` to calibrate the codec's numeric scale for the
     one grade parameter the Resolve scripting API can read back
     (``TestDrxWriteCdlReadback``).
  3. **non-CDL grade-write scaling** — the same write→apply path for a
     Log-wheel parameter (``log_midtone.*``), which Resolve's scripting API
     has *no* getter for at all. This can only prove Resolve accepts the
     written blob without erroring; the numeric scale stays unverified until
     a human compares the Color page panel by eye (see ``README.md``)
     (``TestNonCdlGradeParamsRemainUncalibrated``).

Every test is marked ``requires_resolve``. All of them share the
``resolve_conn`` fixture, which attempts
``davinci_resolve_mcp.connection.get_resolve_connection()`` and calls
``pytest.skip(...)`` (never fails) when Resolve is not reachable — the
default state for CI and for any environment without Resolve installed.
Nothing in this file is exercised at collection time (``--co``): connecting
only happens when a test actually runs, inside the fixture body.

Every Resolve-side test works inside a throwaway, uuid-suffixed scratch
project (created by ``scratch_project`` and always deleted on teardown) so
running this harness never touches, renames, or leaves artifacts behind in
a real project the user has open.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, List

import pytest

from davinci_resolve_mcp import connection as conn_mod
from davinci_resolve_mcp.helpers import _get_timeline_item
from davinci_resolve_mcp.tools import color as color_tools
from davinci_resolve_mcp.tools import media_pool as media_pool_tools
from davinci_resolve_mcp.tools import project as project_tools
from davinci_resolve_mcp.tools import project_manager as pm_tools
from davinci_resolve_mcp.tools import resolve_app as resolve_app_tools
from davinci_resolve_mcp.tools import timeline as timeline_tools
from davinci_resolve_mcp.tools.off_drp import drp as drp_tool
from davinci_resolve_mcp.tools.off_drx import drx as drx_tool

pytestmark = pytest.mark.requires_resolve

_FIXTURES_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, "fixtures", "drx")
)
# A fixture whose node 0 carries CDL-mappable parameters (saturation/
# contrast/pivot/...) — see off_drx "inspect" output for the full list.
_CDL_FIXTURE = os.path.join(_FIXTURES_DIR, "colorslice-grid.drx")
# A fixture whose node 0 carries Log-wheel parameters, which have no
# scripting readback path at all.
_LOG_WHEEL_FIXTURE = os.path.join(_FIXTURES_DIR, "log-wheels-grid.drx")

_SCRATCH_PREFIX = "mcp-live-roundtrip-"


# ---------------------------------------------------------------------------
# Connection / scratch-project fixtures
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
    never executes fixture bodies, so this harness stays collectable
    without Resolve installed anywhere on the machine running the test
    runner.
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

    Yields ``(project, project_name)``. Every grade-write test in this
    module works inside its own scratch project rather than whatever
    project the user has open, and the project is always closed + deleted
    afterward — even if the test body raises.
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


def _flatten_clip_names(folder: Dict[str, Any]) -> List[str]:
    """Recursively collect every clip ``"name"`` out of a
    ``media_pool.get_media_pool_structure()``-shaped dict."""
    names: List[str] = []
    for clip in folder.get("clips", []) or []:
        if isinstance(clip, dict) and "name" in clip:
            names.append(clip["name"])
    for sub in folder.get("subfolders", []) or []:
        if isinstance(sub, dict):
            names.extend(_flatten_clip_names(sub))
    return names


def _create_gradeable_clip(project) -> None:
    """Build an empty timeline with one "Solid Color" generator clip and
    switch to the Color page, so a node graph is reachable at
    (track_type="video", track_index=1, item_index=0).

    Uses a built-in Resolve generator rather than importing real media so
    this harness has no external media-file dependency. Skips (rather than
    fails) if Resolve refuses any step here, since that reflects the local
    Resolve install/version, not the offline codec under test.
    """
    media_pool = project.GetMediaPool()
    if media_pool is None:
        pytest.skip("Resolve's scratch project has no MediaPool.")

    timeline = media_pool.CreateEmptyTimeline("MCP Grade Calibration")
    if timeline is None:
        pytest.skip("Resolve refused to create an empty timeline in the scratch project.")
    project.SetCurrentTimeline(timeline)

    inserted = timeline.InsertGeneratorIntoTimeline("Solid Color")
    if not inserted:
        pytest.skip(
            "Resolve refused InsertGeneratorIntoTimeline('Solid Color') — "
            "cannot build a gradeable clip without real media."
        )

    open_result = resolve_app_tools.open_page("color")
    assert "Error" not in open_result, open_result


# ---------------------------------------------------------------------------
# 1. .drp author -> Resolve import -> live readback
# ---------------------------------------------------------------------------
class TestDrpRoundTrip:
    """``.drp`` author -> Resolve import -> live readback (unproven path #1).

    Exact procedure:

      1. Author a brand-new ``.drp`` OFFLINE (``off_drp.drp``,
         action="author") from a JSON spec describing one Media-Pool clip
         and one timeline.
      2. Read that ``.drp`` back OFFLINE (action="read") to record what was
         actually authored (name/timelines/clips).
      3. Import the file into Resolve under a throwaway, uuid-suffixed
         project name (``project_manager.import_project`` ->
         ``ProjectManager.ImportProject``) so it can never collide with a
         real project.
      4. Load the imported project and read its name / timeline list /
         Media-Pool structure back through the *live* Resolve API
         (``project.get_project_name``, ``timeline.get_timeline_list``,
         ``media_pool.get_media_pool_structure``).
      5. Assert the live values match what was authored.
      6. Always close + delete the imported project.

    The referenced media file is intentionally left non-existent on disk —
    this test verifies the project/timeline/clip *naming and structure*
    round trip through Resolve's importer, not media linking/relinking
    (Resolve will simply show the clip as offline media, which is fine).
    """

    def test_author_import_readback(self, resolve_conn, tmp_path):
        media_path = str(tmp_path / "roundtrip_clip.mov")
        spec = {
            "name": "MCP Live Roundtrip Source",
            "timelines": [
                {
                    "name": "Roundtrip Timeline",
                    "frameRate": 24.0,
                    "startTimecode": "01:00:00:00",
                    "resolution": {"width": 1920, "height": 1080},
                    "tracks": [],
                }
            ],
            "clips": [
                {"name": "roundtrip_clip", "mediaPath": media_path, "kind": "video"}
            ],
            "folders": [],
        }
        drp_path = str(tmp_path / "roundtrip.drp")

        # 1. Author offline.
        authored = json.loads(
            drp_tool(action="author", spec_json=json.dumps(spec), output_path=drp_path)
        )
        assert "error" not in authored, authored
        assert authored.get("verified") is False, authored
        assert authored["timeline_count"] == 1, authored

        # 2. Read the authored file back offline — this is the "intent".
        offline = json.loads(drp_tool(action="read", path=drp_path))
        assert offline.get("name") == spec["name"], offline
        offline_timelines = sorted(t["name"] for t in offline.get("timelines", []))
        offline_clips = sorted(c["name"] for c in offline.get("allClips", []))
        assert offline_timelines == ["Roundtrip Timeline"], offline_timelines
        assert "roundtrip_clip" in offline_clips, offline_clips

        # 3. Import into Resolve under a throwaway name.
        project_name = _unique_project_name()
        try:
            import_result = pm_tools.import_project(drp_path, project_name)
            if "successfully" not in import_result.lower():
                pytest.skip(f"Resolve rejected ImportProject: {import_result}")

            # 4. Load it and read back through the live API.
            load_result = pm_tools.load_project(project_name)
            assert "Error" not in load_result, load_result

            live_name = project_tools.get_project_name()
            assert live_name in (project_name, spec["name"]), (
                f"live project name {live_name!r} matches neither the import "
                f"alias {project_name!r} nor the authored name {spec['name']!r}"
            )

            timelines_raw = timeline_tools.get_timeline_list()
            assert "Error" not in timelines_raw, timelines_raw
            live_timeline_names = (
                []
                if timelines_raw.startswith("No timelines")
                else sorted(t["name"] for t in json.loads(timelines_raw))
            )
            assert live_timeline_names == offline_timelines, (
                f"live timelines {live_timeline_names!r} != authored "
                f"timelines {offline_timelines!r} — the .drt SeqContainer "
                "authoring path needs recalibration"
            )

            mp_raw = media_pool_tools.get_media_pool_structure(max_depth=5, max_clips=200)
            assert "Error" not in mp_raw, mp_raw
            live_clip_names = sorted(_flatten_clip_names(json.loads(mp_raw)))
            assert "roundtrip_clip" in live_clip_names, (
                f"authored clip 'roundtrip_clip' missing from live Media Pool "
                f"{live_clip_names!r} — the ClipPathBlob authoring path needs "
                "recalibration"
            )
        finally:
            try:
                pm_tools.close_project(save_before_close=False)
            except Exception:
                pass
            try:
                pm_tools.delete_project(project_name)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 2. .drx write -> Resolve panel readback (the one CDL-mapped parameter)
# ---------------------------------------------------------------------------
class TestDrxWriteCdlReadback:
    """``.drx`` write -> Resolve panel readback (unproven path #2).

    Exact procedure:

      1. Build a scratch project with one "Solid Color" generator clip on
         the Color page (``_create_gradeable_clip``).
      2. Patch node 0's ``saturation`` parameter in ``colorslice-grid.drx``
         OFFLINE to a known test value (``off_drx.drx``, action="write").
      3. Apply the written ``.drx`` to the live clip
         (``color.apply_grade_from_drx`` -> ``Graph.ApplyGradeFromDRX``).
      4. Read the applied grade back through
         ``TimelineItem.GetCDL()["Saturation"]`` — the *only* grade field
         Resolve's scripting API exposes a getter for.
      5. Assert the live value matches the written value within a
         documented tolerance — this is the numeric UI-unit scale-factor
         calibration for ``saturation`` specifically. All other grade
         parameters (contrast, pivot, gain/lift/gamma, log wheels, curves,
         ...) remain uncalibrated even after this test passes; see
         ``TestNonCdlGradeParamsRemainUncalibrated`` and README.md.
    """

    def test_write_then_get_cdl_matches(self, resolve_conn, scratch_project, tmp_path):
        project, _project_name = scratch_project
        _create_gradeable_clip(project)

        target_saturation = 0.65
        written_path = str(tmp_path / "calibration.drx")
        written = json.loads(
            drx_tool(
                action="write",
                path=_CDL_FIXTURE,
                node_index=0,
                overrides_json=json.dumps({"saturation": target_saturation}),
                output_path=written_path,
            )
        )
        assert "error" not in written, written
        assert written.get("verified") is False, written
        assert written["params_changed"] == 1, written
        assert written["written_named"]["saturation"] == pytest.approx(
            target_saturation, abs=1e-4
        ), written["written_named"]

        apply_result = color_tools.apply_grade_from_drx(
            written_path, grade_mode=0, track_type="video", track_index=1, item_index=0
        )
        assert "Error" not in apply_result, apply_result

        item = _get_timeline_item("video", 1, 0)
        if not hasattr(item, "GetCDL"):
            pytest.skip(
                "This Resolve API build does not expose TimelineItem.GetCDL() "
                "— read the applied grade back by hand from the Color page "
                "primaries panel instead (see README.md)."
            )
        live_cdl = item.GetCDL() or {}
        live_saturation = live_cdl.get("Saturation")
        assert live_saturation is not None, f"GetCDL() returned no Saturation field: {live_cdl!r}"
        assert float(live_saturation) == pytest.approx(target_saturation, abs=0.02), (
            f"written .drx saturation {target_saturation} != live panel "
            f"readback {live_saturation} — drx_codec's saturation scale "
            "factor needs recalibration against this delta"
        )


# ---------------------------------------------------------------------------
# 3. .drx write for parameters with NO scripting readback path
# ---------------------------------------------------------------------------
class TestNonCdlGradeParamsRemainUncalibrated:
    """``.drx`` write scaling for parameters Resolve cannot read back
    through scripting at all (unproven path #3 — documents what
    ``verified: false`` still means after this whole module passes).

    Resolve's scripting API only exposes CDL (Slope/Offset/Power/Saturation
    via ``TimelineItem.GetCDL()``/``SetCDL()``) and LUT get/set for a
    node's grade. It has **no** scripted getter for:

      - Log-wheel values (``log_shadow.*`` / ``log_midtone.*`` /
        ``log_highlight.*``),
      - classic primary-wheel values (``lift.*`` / ``gain.*`` /
        ``gamma.*`` / ``offset.*``),
      - or the curve/qualifier controls (``contrast``, ``pivot``,
        ``color_boost``, ``midtone_detail``, ``temperature``, ``tint``,
        HSL qualifier fields, power-window geometry, ...).

    This test can therefore only prove the *structural* half of the round
    trip: Resolve accepts a ``.drx`` with a patched Log-wheel value and
    applies it without erroring. It grabs a still afterward so a human can
    compare the Color page panel against the intended value by eye — that
    manual comparison (documented in README.md) is the only way left to
    calibrate these parameters' numeric scale; automating it would require
    driving the Resolve UI directly, which is out of scope for the
    scripting API this server is built on.
    """

    def test_apply_succeeds_numeric_scale_stays_unverified(
        self, resolve_conn, scratch_project, tmp_path
    ):
        project, _project_name = scratch_project
        _create_gradeable_clip(project)

        target_log_midtone = 0.4
        written_path = str(tmp_path / "log_wheel_calibration.drx")
        written = json.loads(
            drx_tool(
                action="write",
                path=_LOG_WHEEL_FIXTURE,
                node_index=0,
                overrides_json=json.dumps(
                    {
                        "log_midtone.r": target_log_midtone,
                        "log_midtone.g": target_log_midtone,
                        "log_midtone.b": target_log_midtone,
                    }
                ),
                output_path=written_path,
            )
        )
        assert "error" not in written, written
        assert written.get("verified") is False, written
        assert written["params_changed"] == 3, written

        apply_result = color_tools.apply_grade_from_drx(
            written_path, grade_mode=0, track_type="video", track_index=1, item_index=0
        )
        assert "Error" not in apply_result, (
            f"Resolve rejected a .drx with patched log_midtone values "
            f"(structural round trip failed): {apply_result}"
        )

        # No scripted readback exists for log-wheel parameters — grab a
        # still so a human can compare the Color page panel to
        # target_log_midtone=0.4 by eye (see README.md's manual checklist).
        still_result = color_tools.grab_still()
        assert "Error" not in still_result, still_result
