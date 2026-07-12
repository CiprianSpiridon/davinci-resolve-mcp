"""
davinci_resolve_mcp.store.provenance — append-only provenance/audit ledger.

This module is the trust/defense layer of the offline tooling: it answers
"who did what, when, with what intent — and did the actual result match?".
It is a thin, deterministic layer *on top of* :mod:`davinci_resolve_mcp.store.db`
— it composes the ``provenance`` table already owned by :class:`~.db.Store`
rather than reimplementing any persistence.

Two responsibilities
---------------------
* :func:`record_event` — append one immutable provenance event and return its
  id. There is deliberately **no update path**: each call mints a fresh,
  monotonically-sequenced id, so a recorded event can never be mutated in place.
* :func:`episode_report` — render a one-page JSON readback for a project: the
  full lineage ordered by *recorded sequence* (not by wall-clock), with any
  ``intent != actual`` entries flagged as drift, plus a rollup summary.

Design invariants
------------------
* **No wall-clock in code.** This module never reads the current time; every
  timestamp is passed in by the caller (``event["at"]``). If a caller omits it,
  the stored ``at`` is simply ``None`` — the module does not invent one.
* **Append-only / immutable.** Ids embed a zero-padded insertion sequence so
  the ledger's recorded order is always recoverable even when timestamps are
  equal, missing, or out of order.
* **Offline only.** No DaVinci Resolve import, no network — pure SQLite via the
  ``db`` layer.

Event shape
-----------
An *event* is a plain mapping. Recognised keys (all optional):

    ``actor`` / ``who``       — who caused the event (default ``"system"``)
    ``tool``  / ``action`` / ``what`` — what produced/changed the artifact
    ``stage``                 — pipeline stage the event belongs to
    ``target``                — the artifact acted on (clip/grade/deliverable id)
    ``project_id``            — project the event belongs to
    ``run_id``                — pipeline run the event belongs to
    ``config_hash``           — hash of the tool config/spec used
    ``at`` / ``when``         — caller-supplied timestamp (string; not generated)
    ``intent``                — what the caller meant to happen
    ``actual`` / ``result``   — what actually happened

``intent`` and ``actual`` (plus any unrecognised keys, kept under ``detail``)
are serialised into the ledger's ``result`` column as a small JSON payload and
faithfully round-tripped by :func:`episode_report`.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Optional

from .db import Store, StoreError

__all__ = [
    "ProvenanceError",
    "record_event",
    "episode_report",
]

# Keys that map onto dedicated provenance columns; everything else on the event
# is preserved under the payload's ``detail`` object.
_STRUCTURAL_KEYS = frozenset(
    {
        "actor",
        "who",
        "tool",
        "action",
        "what",
        "stage",
        "target",
        "project_id",
        "run_id",
        "config_hash",
        "at",
        "when",
        "intent",
        "actual",
        "result",
        "event_id",
    }
)


class ProvenanceError(Exception):
    """Raised for a malformed provenance event or a corrupt ledger payload.

    Store-level failures still surface as :class:`~.db.StoreError`; this type is
    reserved for provenance-specific validation problems (e.g. an event that is
    not a mapping).
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Marker prefix + fixed-width sequence so ids sort by insertion order and the
# sequence is trivially recoverable in :func:`_event_seq`.
_ID_PREFIX = "ev"
_SEQ_WIDTH = 12


def _first(event: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    """Return the first present, non-``None`` value among ``keys``."""
    for key in keys:
        if key in event and event[key] is not None:
            return event[key]
    return default


def _content_hash(event: Mapping[str, Any]) -> str:
    """Short, stable digest of an event's content (for id uniqueness/trace)."""
    try:
        blob = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        blob = repr(sorted(event.items(), key=lambda kv: str(kv[0])))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:8]


def _make_event_id(seq: int, digest: str) -> str:
    """Build an insertion-ordered, collision-free event id."""
    return f"{_ID_PREFIX}-{seq:0{_SEQ_WIDTH}d}-{digest}"


def _event_seq(event_id: Optional[str]) -> int:
    """Recover the insertion sequence embedded in an event id.

    Ids not minted by :func:`_make_event_id` sort last (``-1`` would sort them
    first); we return a large sentinel so foreign ids trail well-formed ones
    while still keeping a stable order.
    """
    if not event_id:
        return 1 << 62
    parts = event_id.split("-")
    if len(parts) >= 3 and parts[0] == _ID_PREFIX:
        try:
            return int(parts[1])
        except ValueError:
            pass
    return 1 << 62


def _pack_payload(intent: Any, actual: Any, detail: Mapping[str, Any]) -> Optional[str]:
    """Serialise intent/actual/detail into the ledger's ``result`` column."""
    payload: dict[str, Any] = {}
    if intent is not None:
        payload["intent"] = intent
    if actual is not None:
        payload["actual"] = actual
    if detail:
        payload["detail"] = dict(detail)
    if not payload:
        return None
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise ProvenanceError(f"could not serialise provenance payload: {exc}") from exc


def _unpack_payload(text: Optional[str]) -> dict[str, Any]:
    """Decode a ``result`` column back into intent/actual/detail.

    Rows written outside this module (plain-text ``result``) degrade
    gracefully: the raw string is preserved under ``detail.raw``.
    """
    if text is None:
        return {"intent": None, "actual": None, "detail": {}}
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError):
        return {"intent": None, "actual": None, "detail": {"raw": text}}
    if not isinstance(decoded, dict):
        return {"intent": None, "actual": None, "detail": {"raw": decoded}}
    return {
        "intent": decoded.get("intent"),
        "actual": decoded.get("actual"),
        "detail": decoded.get("detail") or {},
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def record_event(db: Store, event: Mapping[str, Any]) -> str:
    """Append one immutable provenance event to the ledger and return its id.

    A fresh, insertion-ordered id is minted on every call; there is no path to
    update an already-recorded event, which keeps the ledger append-only.

    Args:
        db: An open :class:`~.db.Store`.
        event: The event mapping (see the module docstring for recognised keys).
            All timestamps are taken from the event (``at``/``when``) — this
            function never reads the wall clock.

    Returns:
        The generated ``event_id``.

    Raises:
        ProvenanceError: If ``event`` is not a mapping.
        StoreError: If the underlying insert fails.
    """
    if not isinstance(event, Mapping):
        raise ProvenanceError("event must be a mapping")

    actor = _first(event, "actor", "who", default="system")
    tool = _first(event, "tool", "action", "what")
    stage = _first(event, "stage")
    target = _first(event, "target")
    project_id = _first(event, "project_id")
    run_id = _first(event, "run_id")
    config_hash = _first(event, "config_hash")
    at = _first(event, "at", "when")  # caller-supplied; never generated here
    intent = _first(event, "intent")
    actual = _first(event, "actual", "result")

    detail = {k: v for k, v in event.items() if k not in _STRUCTURAL_KEYS}
    payload = _pack_payload(intent, actual, detail)

    # Insertion sequence = number of events already in the ledger. This makes
    # ids sort in recorded order and lets episode_report recover that order
    # regardless of the (caller-supplied, possibly missing) timestamps.
    seq = len(db.list_provenance())
    event_id = _make_event_id(seq, _content_hash(event))

    db.record_provenance(
        event_id=event_id,
        project_id=project_id,
        run_id=run_id,
        stage=stage,
        tool=tool,
        config_hash=config_hash,
        actor=str(actor) if actor is not None else "system",
        result=payload,
        target=target,
        now=at,
    )
    return event_id


def _lineage_entry(row: Mapping[str, Any]) -> dict[str, Any]:
    """Turn a raw provenance row into a lineage entry with drift flagged."""
    unpacked = _unpack_payload(row.get("result"))
    intent = unpacked["intent"]
    actual = unpacked["actual"]
    # Drift is only meaningful when both sides were recorded.
    has_both = intent is not None and actual is not None
    mismatch = bool(has_both and intent != actual)
    return {
        "event_id": row.get("event_id"),
        "seq": _event_seq(row.get("event_id")),
        "actor": row.get("actor"),
        "tool": row.get("tool"),
        "stage": row.get("stage"),
        "target": row.get("target"),
        "run_id": row.get("run_id"),
        "config_hash": row.get("config_hash"),
        "at": row.get("at"),
        "intent": intent,
        "actual": actual,
        "detail": unpacked["detail"],
        "mismatch": mismatch,
    }


def episode_report(db: Store, project_id: str) -> dict[str, Any]:
    """Render a one-page provenance readback for ``project_id``.

    The report lists the project's full lineage ordered by *recorded sequence*
    (the order events were appended, recovered from their ids — not by
    timestamp), and flags every entry whose recorded ``intent`` differs from its
    ``actual`` result as drift.

    Querying an unknown or empty project returns a well-formed **empty** report
    (zero events, empty lineage) rather than raising.

    Args:
        db: An open :class:`~.db.Store`.
        project_id: The project to report on.

    Returns:
        A JSON-serialisable dict with keys ``project_id``, ``event_count``,
        ``lineage`` (ordered), ``mismatches`` (the drifted subset), and
        ``summary`` (rollup counts / actors / tools / stages).
    """
    rows = db.list_provenance(project_id=project_id) if project_id else []
    entries = [_lineage_entry(row) for row in rows]
    # Order by recorded sequence, with event_id as a stable tiebreaker.
    entries.sort(key=lambda e: (e["seq"], e["event_id"] or ""))

    mismatches = [e for e in entries if e["mismatch"]]
    actors = sorted({e["actor"] for e in entries if e["actor"]})
    tools = sorted({e["tool"] for e in entries if e["tool"]})
    stages = sorted({e["stage"] for e in entries if e["stage"]})

    return {
        "project_id": project_id,
        "event_count": len(entries),
        "lineage": entries,
        "mismatches": mismatches,
        "summary": {
            "event_count": len(entries),
            "mismatch_count": len(mismatches),
            "clean": len(mismatches) == 0,
            "actors": actors,
            "tools": tools,
            "stages": stages,
        },
    }
