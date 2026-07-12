"""
tests/test_pipeline_provenance.py — pipeline (DB-as-truth) + provenance tests.

Exercises the `pipeline` action-dispatch tool (compile a JSON project spec
into the canonical SQLite store, then run its stages) together with the
`store.provenance` append-only ledger it composes: a full compile+run round
trip persists project/run/stage rows AND provenance events, a gated stage
that drifts (recorded `intent` != `actual`) halts the run and is reported,
`episode_report` flags the drift, and re-running an already-halted (or
already-completed) pipeline is idempotent — no duplicate provenance events,
no double-counted stage rows.

None of this touches DaVinci Resolve or the network: `pipeline` only reads a
spec (passed in-process as a dict — JSON, not YAML, so PyYAML is never
required) and reads/writes a local SQLite file. Every `now=` timestamp below
is caller-supplied per the "no wall-clock" invariant, which also makes the
assertions on recorded event count/order deterministic.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from davinci_resolve_mcp.store.db import open_db
from davinci_resolve_mcp.store.provenance import episode_report
from davinci_resolve_mcp.tools.off_pipeline import pipeline


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "store.sqlite3")


def _passing_spec(project_id: str = "proj_pass") -> dict:
    """A spec whose gated stage does NOT drift — the whole pipeline completes."""
    return {
        "project_id": project_id,
        "name": "Passing Project",
        "grade": {"color_science": "davinci_yrgb"},
        "deliverables": [{"id": "d1", "video": {"codec": "h264"}}],
        "pipeline": [
            {"stage": "ingest"},
            {"stage": "conform", "gate": "pass"},
            {"stage": "grade"},
        ],
    }


def _failing_spec(project_id: str = "proj_fail") -> dict:
    """A spec whose gated `conform` stage drifts (intent != actual) -> halts."""
    return {
        "project_id": project_id,
        "name": "Failing Project",
        "grade": {"color_science": "davinci_yrgb"},
        "deliverables": [{"id": "d1", "video": {"codec": "h264"}}],
        "pipeline": [
            {"stage": "ingest"},
            {
                "stage": "conform",
                "gate": "pass",
                "intent": "conform:ok",
                "actual": "conform:mismatch",
            },
            {"stage": "grade"},
        ],
    }


def _run(action: str, **kwargs) -> dict:
    """Call the `pipeline` tool and decode its JSON result (never raises)."""
    raw = pipeline(action=action, **kwargs)
    assert not raw.startswith("Error:"), f"pipeline({action}) returned an error: {raw}"
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Runs offline: no Resolve, no PyYAML required
# ---------------------------------------------------------------------------
def test_runs_with_no_resolve_module_imported():
    import sys

    assert "DaVinciResolveScript" not in sys.modules


def test_json_spec_does_not_require_pyyaml(db_path, monkeypatch):
    import davinci_resolve_mcp.tools.off_pipeline as off_pipeline

    monkeypatch.setattr(off_pipeline, "_yaml", None)
    result = _run(
        "compile",
        db_path=db_path,
        spec=_passing_spec("proj_no_yaml"),
        now="2026-01-01T00:00:00Z",
    )
    assert result["project"]["project_id"] == "proj_no_yaml"
    assert result["verified"] is False


# ---------------------------------------------------------------------------
# compile+run persists rows and provenance events
# ---------------------------------------------------------------------------
def test_compile_persists_project_row_and_provenance_event(db_path):
    result = _run(
        "compile",
        db_path=db_path,
        spec=_passing_spec(),
        now="2026-01-01T00:00:00Z",
    )
    assert result["action"] == "compile"
    assert result["changed"] is True
    assert len(result["stages"]) == 3

    store = open_db(db_path)
    try:
        project = store.get_project("proj_pass")
        assert project is not None
        assert project["name"] == "Passing Project"
        events = store.list_provenance(project_id="proj_pass")
        assert len(events) == 1
        assert events[0]["tool"] == "pipeline.compile"
    finally:
        store.close()


def test_run_completes_all_stages_and_records_provenance_per_stage(db_path):
    _run("compile", db_path=db_path, spec=_passing_spec(), now="2026-01-01T00:00:00Z")
    result = _run(
        "run",
        db_path=db_path,
        project_id="proj_pass",
        now="2026-01-01T00:01:00Z",
    )
    assert result["status"] == "done"
    assert result["first_time"] is True
    assert result["halted"] is None
    assert result["drift_count"] == 0
    assert [p["stage"] for p in result["progressed"]] == ["ingest", "conform", "grade"]
    assert all(p["status"] == "done" for p in result["progressed"])
    assert result["verified"] is False

    store = open_db(db_path)
    try:
        run_row = store.get_run(result["run_id"], with_stages=True)
        assert run_row is not None
        assert run_row["status"] == "done"
        assert len(run_row["stages"]) == 3
        assert all(s["status"] == "done" for s in run_row["stages"])

        events = store.list_provenance(project_id="proj_pass")
        # 1 compile event + 3 per-stage run events.
        assert len(events) == 4
        stage_events = [e for e in events if e["tool"] == "pipeline.run"]
        assert len(stage_events) == 3
    finally:
        store.close()


# ---------------------------------------------------------------------------
# A gate failure halts the run and is reported with the failing gate
# ---------------------------------------------------------------------------
def test_gate_failure_halts_run_and_reports_failing_gate(db_path):
    _run("compile", db_path=db_path, spec=_failing_spec(), now="2026-01-01T00:00:00Z")
    result = _run(
        "run",
        db_path=db_path,
        project_id="proj_fail",
        now="2026-01-01T00:01:00Z",
    )
    assert result["status"] == "halted"
    assert result["halted"] is not None
    assert result["halted"]["stage"] == "conform"
    assert result["halted"]["gate"] == "pass"
    assert result["halted"]["reason"] == "gate_failed"

    # The run stopped at conform: ingest done, conform halted, grade never ran.
    stages = result["progressed"]
    assert [s["stage"] for s in stages] == ["ingest", "conform"]
    assert stages[-1]["status"] == "halted"

    store = open_db(db_path)
    try:
        run_row = store.get_run(result["run_id"], with_stages=True)
        assert run_row["status"] == "halted"
        # grade (stage_index 2) never got a row written.
        stage_indices = {s["stage_index"] for s in run_row["stages"]}
        assert stage_indices == {0, 1}
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Drift (intent != actual) is flagged
# ---------------------------------------------------------------------------
def test_drift_is_flagged_in_run_result_and_episode_report(db_path):
    _run("compile", db_path=db_path, spec=_failing_spec(), now="2026-01-01T00:00:00Z")
    result = _run(
        "run",
        db_path=db_path,
        project_id="proj_fail",
        now="2026-01-01T00:01:00Z",
    )
    assert result["drift_count"] == 1
    assert result["drift"][0]["stage"] == "conform"
    assert result["drift"][0]["intent"] == "conform:ok"
    assert result["drift"][0]["actual"] == "conform:mismatch"

    report = _run("report", db_path=db_path, project_id="proj_fail")
    assert report["summary"]["clean"] is False
    assert report["summary"]["mismatch_count"] == 1
    assert len(report["mismatches"]) == 1
    mismatch = report["mismatches"][0]
    assert mismatch["stage"] == "conform"
    assert mismatch["intent"] == "conform:ok"
    assert mismatch["actual"] == "conform:mismatch"
    assert mismatch["mismatch"] is True

    # A passing project's report is clean (no drift anywhere in its lineage).
    _run("compile", db_path=db_path, spec=_passing_spec(), now="2026-01-01T00:00:00Z")
    _run("run", db_path=db_path, project_id="proj_pass", now="2026-01-01T00:01:00Z")
    clean_report = _run("report", db_path=db_path, project_id="proj_pass")
    assert clean_report["summary"]["clean"] is True
    assert clean_report["mismatches"] == []


def test_episode_report_for_unknown_project_is_empty_not_an_error(db_path):
    # No compile/run ever happened for this project_id against this store.
    report = _run("report", db_path=db_path, project_id="does-not-exist")
    assert report["event_count"] == 0
    assert report["lineage"] == []
    assert report["mismatches"] == []
    assert report["summary"]["clean"] is True


# ---------------------------------------------------------------------------
# Re-run is idempotent (no double-count in provenance or stage rows)
# ---------------------------------------------------------------------------
def test_rerun_of_completed_pipeline_is_idempotent(db_path):
    _run("compile", db_path=db_path, spec=_passing_spec(), now="2026-01-01T00:00:00Z")
    first = _run(
        "run", db_path=db_path, project_id="proj_pass", now="2026-01-01T00:01:00Z"
    )
    assert first["first_time"] is True
    assert first["status"] == "done"

    second = _run(
        "run", db_path=db_path, project_id="proj_pass", now="2026-01-01T00:05:00Z"
    )
    assert second["first_time"] is False
    assert second["run_id"] == first["run_id"]
    assert second["status"] == "done"
    # Every stage short-circuits from cache — none are re-executed.
    assert all(p.get("cached") for p in second["progressed"])

    store = open_db(db_path)
    try:
        events = store.list_provenance(project_id="proj_pass")
        # Still exactly 1 compile + 3 stage events: the second run() call
        # appended nothing new.
        assert len(events) == 4

        run_row = store.get_run(first["run_id"], with_stages=True)
        assert len(run_row["stages"]) == 3  # no duplicate stage rows either
    finally:
        store.close()

    third_report = episode_report(open_db(db_path), "proj_pass")
    assert third_report["event_count"] == 4
    assert third_report["summary"]["clean"] is True


def test_rerun_of_halted_pipeline_stays_halted_and_does_not_double_count(db_path):
    _run("compile", db_path=db_path, spec=_failing_spec(), now="2026-01-01T00:00:00Z")
    first = _run(
        "run", db_path=db_path, project_id="proj_fail", now="2026-01-01T00:01:00Z"
    )
    assert first["status"] == "halted"

    second = _run(
        "run", db_path=db_path, project_id="proj_fail", now="2026-01-01T00:05:00Z"
    )
    assert second["status"] == "halted"
    assert second["run_id"] == first["run_id"]
    assert second["first_time"] is False
    assert second["halted"] == first["halted"]
    # The halted stage (and everything before it) short-circuits from cache.
    assert all(p.get("cached") for p in second["progressed"])

    store = open_db(db_path)
    try:
        events = store.list_provenance(project_id="proj_fail")
        # 1 compile + 2 stage events (ingest done, conform halted); the second
        # run() call appended no new events despite still being "halted".
        assert len(events) == 3
    finally:
        store.close()


def test_recompiling_unchanged_spec_does_not_duplicate_provenance(db_path):
    spec = _passing_spec("proj_recompile")
    _run("compile", db_path=db_path, spec=spec, now="2026-01-01T00:00:00Z")
    second = _run(
        "compile", db_path=db_path, spec=spec, now="2026-01-01T00:10:00Z"
    )
    assert second["changed"] is False

    store = open_db(db_path)
    try:
        events = store.list_provenance(project_id="proj_recompile")
        assert len(events) == 1
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Lineage ordering
# ---------------------------------------------------------------------------
def test_episode_report_lineage_is_ordered_by_recorded_sequence(db_path):
    _run("compile", db_path=db_path, spec=_passing_spec(), now="2026-01-01T00:00:00Z")
    _run("run", db_path=db_path, project_id="proj_pass", now="2026-01-01T00:01:00Z")

    report = _run("report", db_path=db_path, project_id="proj_pass")
    seqs = [entry["seq"] for entry in report["lineage"]]
    assert seqs == sorted(seqs)
    stages_in_order = [entry["stage"] for entry in report["lineage"]]
    assert stages_in_order == ["compile", "ingest", "conform", "grade"]


# ---------------------------------------------------------------------------
# Malformed input never raises
# ---------------------------------------------------------------------------
def test_run_without_prior_compile_returns_error_string_not_raise(db_path):
    raw = pipeline(action="run", db_path=db_path, project_id="does-not-exist")
    assert raw.startswith("Error:")


def test_unknown_action_returns_valid_actions_list(db_path):
    result = _run("bogus-action", db_path=db_path)
    assert "compile" in result["valid_actions"]
    assert "run" in result["valid_actions"]
