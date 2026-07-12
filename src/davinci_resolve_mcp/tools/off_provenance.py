"""
davinci_resolve_mcp.tools.off_provenance — provenance / audit ledger tool
(OFFLINE/advanced tool set).

Single action-dispatch entry point (``provenance``) that is a thin wrapper
over :mod:`davinci_resolve_mcp.store.provenance` (itself layered on
:mod:`davinci_resolve_mcp.store.db`). It does not reimplement any ledger
logic — it only translates typed tool parameters into the store layer's
calls and serialises the results to JSON.

Actions
-------
* ``"record"`` — append one immutable provenance event to the ledger at
  ``db_path`` and return its generated ``event_id``. There is no update
  path: every call mints a fresh event (see
  :func:`davinci_resolve_mcp.store.provenance.record_event`).
* ``"report"`` — render the episode report (full lineage ordered by
  recorded sequence, with ``intent`` vs ``actual`` drift flagged) for one
  project. A ``project_id`` with no recorded events (including an unknown
  project) returns a well-formed **empty** report rather than an error.
* ``"list"`` — return the raw, un-rendered provenance rows for a project
  and/or run (a convenience readback under the same
  :meth:`~davinci_resolve_mcp.store.db.Store.list_provenance` the report
  is built from), for callers that want the ledger rows without the
  lineage/drift rendering ``"report"`` adds.

This module never connects to DaVinci Resolve: it only reads/writes the
local SQLite store. Every code path returns a JSON string; nothing here
raises — the top-level ``provenance`` dispatcher catches every exception
and returns an ``"Error: ..."`` string instead, per the offline tool-set
convention. No wall-clock is read anywhere in this module: a caller-supplied
``at`` is passed straight through to the store, and an omitted one is left
for the store layer to fill in (see
:mod:`davinci_resolve_mcp.store.provenance`'s module docstring).
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from ..app import mcp
from ..store.db import StoreError, open_db
from ..store.provenance import ProvenanceError, episode_report, record_event

_VALID_ACTIONS = ("record", "report", "list")


# ---------------------------------------------------------------------------
# 'record' action
# ---------------------------------------------------------------------------
def _do_record(
    db_path: str,
    project_id: str,
    run_id: str,
    actor: str,
    tool: str,
    stage: str,
    target: str,
    config_hash: str,
    at: str,
    intent: Any,
    actual: Any,
    detail: Any,
) -> str:
    if not db_path:
        raise ValueError("record requires 'db_path' (path to the local SQLite store)")
    if detail is not None and not isinstance(detail, dict):
        raise ValueError("detail must be a JSON object (dict) if provided")

    event: Dict[str, Any] = {}
    if project_id:
        event["project_id"] = project_id
    if run_id:
        event["run_id"] = run_id
    if actor:
        event["actor"] = actor
    if tool:
        event["tool"] = tool
    if stage:
        event["stage"] = stage
    if target:
        event["target"] = target
    if config_hash:
        event["config_hash"] = config_hash
    if at:
        event["at"] = at
    if intent is not None:
        event["intent"] = intent
    if actual is not None:
        event["actual"] = actual
    if detail:
        event.update(detail)

    try:
        store = open_db(db_path)
    except StoreError as e:
        raise ValueError(f"could not open store at {db_path!r}: {e}") from e
    try:
        try:
            event_id = record_event(store, event)
        except ProvenanceError as e:
            raise ValueError(f"invalid provenance event: {e}") from e
        except StoreError as e:
            raise ValueError(f"store error during record: {e}") from e
        recorded = store.list_provenance(project_id=project_id or None) if project_id else None
        recorded_row = None
        if recorded:
            recorded_row = next((r for r in recorded if r.get("event_id") == event_id), None)
        result = {
            "action": "record",
            "event_id": event_id,
            "event": recorded_row,
            "verified": False,
        }
        return json.dumps(result, indent=2, default=str)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 'report' action
# ---------------------------------------------------------------------------
def _do_report(db_path: str, project_id: str) -> str:
    if not db_path:
        raise ValueError("report requires 'db_path' (path to the local SQLite store)")
    if not project_id:
        raise ValueError("report requires 'project_id'")
    try:
        store = open_db(db_path)
    except StoreError as e:
        raise ValueError(f"could not open store at {db_path!r}: {e}") from e
    try:
        try:
            report = episode_report(store, project_id)
        except StoreError as e:
            raise ValueError(f"store error during report: {e}") from e
        report["action"] = "report"
        return json.dumps(report, indent=2, default=str)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 'list' action
# ---------------------------------------------------------------------------
def _do_list(db_path: str, project_id: str, run_id: str) -> str:
    if not db_path:
        raise ValueError("list requires 'db_path' (path to the local SQLite store)")
    try:
        store = open_db(db_path)
    except StoreError as e:
        raise ValueError(f"could not open store at {db_path!r}: {e}") from e
    try:
        try:
            rows = store.list_provenance(
                project_id=project_id or None,
                run_id=run_id or None,
            )
        except StoreError as e:
            raise ValueError(f"store error during list: {e}") from e
        result = {
            "action": "list",
            "project_id": project_id or None,
            "run_id": run_id or None,
            "event_count": len(rows),
            "events": rows,
        }
        return json.dumps(result, indent=2, default=str)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool entry point
# ---------------------------------------------------------------------------
@mcp.tool()
def provenance(
    action: str,
    db_path: str = "",
    project_id: str = "",
    run_id: str = "",
    actor: str = "",
    tool: str = "",
    stage: str = "",
    target: str = "",
    config_hash: str = "",
    at: str = "",
    intent: Optional[Any] = None,
    actual: Optional[Any] = None,
    detail: Optional[Any] = None,
) -> str:
    """Provenance / audit ledger for the OFFLINE/advanced tool set. Never touches Resolve.

    A thin wrapper over `davinci_resolve_mcp.store.provenance` (itself
    layered on the local SQLite store at `db_path`): it composes that
    layer's ledger, it does not reimplement it.

    Parameters:
    - action: one of
        - "record": append one immutable provenance event to the ledger.
          Requires `db_path`. All other fields are optional and map onto
          the ledger's recognised event keys: `project_id`, `run_id`,
          `actor` (who caused the event; defaults to "system" if omitted),
          `tool` (what produced/changed the artifact), `stage` (pipeline
          stage), `target` (the clip/grade/deliverable id acted on),
          `config_hash` (hash of the tool config/spec used), `at` (a
          caller-supplied timestamp string — this module never reads the
          wall clock, so an omitted `at` is left for the store layer to
          fill in), `intent` (what the caller meant to happen), and
          `actual` (what actually happened — compared against `intent` by
          "report" to flag drift). `detail` is an optional JSON object of
          any additional free-form keys, preserved verbatim and returned
          under the recorded event's `detail`. There is no update path:
          every call mints a fresh, insertion-ordered `event_id`, which is
          returned along with the persisted event row and
          `"verified": false`.
        - "report": render the episode report for `project_id` (required,
          along with `db_path`) — the project's full lineage ordered by
          *recorded* sequence (not wall-clock), with `event_count`,
          `lineage` (each entry's `actor`/`tool`/`stage`/`target`/`intent`/
          `actual`/`detail`/`mismatch`), `mismatches` (the drifted
          subset where `intent != actual`), and a `summary` rollup
          (`event_count`, `mismatch_count`, `clean`, `actors`, `tools`,
          `stages`). A `project_id` with no recorded events — including
          one that has never been indexed or graded — returns a
          well-formed **empty** report (`event_count: 0`, empty
          `lineage`) rather than an error.
        - "list": return the raw ledger rows (un-rendered, no drift
          analysis) for `db_path`, optionally filtered to `project_id`
          and/or `run_id`. Omitting both returns every event in the
          store. A convenience readback for callers that want the ledger
          rows directly rather than the "report" rendering.
    - db_path: path to the local SQLite store. Required for every action.
    - project_id / run_id: scope for "record" (attaches the event),
      "report" (required; the project to report on), and "list"
      (optional filters).
    - actor / tool / stage / target / config_hash / at / intent / actual /
      detail: "record" event fields (see above).

    Any other action returns the list of valid actions instead of erroring.
    Malformed/unreadable input never raises — it comes back as an
    "Error: ..." string, per the offline tool-set convention.
    """
    action_lower = action.strip().lower() if isinstance(action, str) else ""

    try:
        if action_lower == "record":
            return _do_record(
                db_path,
                project_id,
                run_id,
                actor,
                tool,
                stage,
                target,
                config_hash,
                at,
                intent,
                actual,
                detail,
            )

        if action_lower == "report":
            return _do_report(db_path, project_id)

        if action_lower == "list":
            return _do_list(db_path, project_id, run_id)

        return json.dumps(
            {
                "error": f"Unknown action '{action}'.",
                "valid_actions": list(_VALID_ACTIONS),
            },
            indent=2,
        )
    except Exception as e:  # noqa: BLE001 - offline tools never raise
        return f"Error: {e}"
