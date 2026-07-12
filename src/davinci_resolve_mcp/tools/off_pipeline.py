"""
davinci_resolve_mcp.tools.off_pipeline — DB-as-truth pipeline orchestration
(OFFLINE/advanced tool set).

Single action-dispatch entry point (``pipeline``) that turns an authored
project *spec* (YAML or JSON) into rows in the canonical SQLite store, then
drives that project's pipeline stage-by-stage with gates, provenance, and
intent-vs-actual drift detection. It composes the storage layer
(:mod:`davinci_resolve_mcp.store.db` for projects/runs/stages,
:mod:`davinci_resolve_mcp.store.provenance` for the append-only lineage
``project-db.mjs``). It never reimplements persistence and never connects to
DaVinci Resolve — it only reads a local spec file and reads/writes a local
SQLite database.

Actions
-------
* ``"compile"`` — parse a spec (``spec`` object/JSON-string or ``spec_path``
  file), resolve its inheritance (``parent`` chains, deep-merge semantics, and
  intra-list ``deliverables[].inherits`` reuse), validate the fully-resolved
  config, and upsert the resulting project row into the DB. The DB then holds
  the *resolved* pipeline so the runner never merges at run time — it reads
  resolved rows. Idempotent: re-compiling an unchanged spec updates the same
  project row and does not append a duplicate provenance event.
* ``"run"`` — plan + execute the compiled project's pipeline. Creates a run
  (keyed deterministically on the project + spec hash, so re-running is
  idempotent), then walks the stages in order. Each stage records provenance
  with its ``intent`` and ``actual`` outcome; a stage whose recorded ``actual``
  differs from its ``intent`` is flagged as *drift*. A **gated** stage that
  drifts (or is explicitly marked failed) fails its gate, which **halts** the
  run and reports the failing gate + stage. Re-running an already-complete run
  short-circuits every stage (all terminal) and appends no new provenance —
  no double-count.
* ``"status"`` — read back a run's current stage table + status.
* ``"report"`` — render the project's provenance episode report (full lineage
  in recorded order, every ``intent != actual`` entry flagged as drift).

Convention: every code path returns a JSON string; nothing raises — the
dispatcher catches every exception and returns an ``"Error: ..."`` string.
Because ``run`` executes/changes pipeline state, its result carries
``"verified": false`` (structural-only until live Resolve calibration).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from ..app import mcp
from ..store.db import StoreError, open_db
from ..store.provenance import episode_report, record_event

try:  # PyYAML is optional — JSON specs always work without it.
    import yaml as _yaml  # type: ignore
except Exception:  # pragma: no cover - optional dep absent
    _yaml = None

_VALID_ACTIONS = ("compile", "run", "status", "report")

# KNOWN_STAGES / gate whitelist). Validation is lenient elsewhere (specs
# evolve) but strict on these, which have historically failed silently.
_KNOWN_STAGES = frozenset(
    {
        "ingest",
        "conform",
        "offline_ref",
        "color_groups",
        "leveling",
        "grade",
        "audio_sync",
        "qc",
        "deliver",
    }
)
_KNOWN_GATES = frozenset({"review", "pass"})
_KNOWN_SCIENCE = frozenset(
    {
        "acescct",
        "acescc",
        "davinci_yrgb",
        "davinci_wide_gamut",
        "rec709",
        "cineon",
    }
)

# Stage statuses that are terminal for idempotency purposes: a stage in one of
# these is never re-executed on a subsequent run() call.
_TERMINAL_STATUSES = frozenset({"done", "halted"})


# ---------------------------------------------------------------------------
# Spec parsing
# ---------------------------------------------------------------------------
def _load_spec(spec: Any, spec_path: str) -> Dict[str, Any]:
    """Return the spec as a dict from either an in-line ``spec`` (dict or
    JSON/YAML string) or a ``spec_path`` file (``.json``/``.yaml``/``.yml``)."""
    if spec is not None:
        if isinstance(spec, dict):
            return spec
        if isinstance(spec, str):
            return _parse_text(spec, hint="")
        raise ValueError("'spec' must be a mapping or a JSON/YAML string")
    if spec_path:
        if not os.path.isfile(spec_path):
            raise ValueError(f"spec_path not found: {spec_path!r}")
        with open(spec_path, "r", encoding="utf-8") as fh:
            text = fh.read()
        return _parse_text(text, hint=spec_path)
    raise ValueError("compile requires either 'spec' (object/JSON) or 'spec_path'")


def _parse_text(text: str, hint: str) -> Dict[str, Any]:
    """Parse spec text: try JSON first, then YAML (if PyYAML is installed)."""
    stripped = text.strip()
    is_yaml_ext = hint.lower().endswith((".yaml", ".yml"))
    # A .yaml/.yml file is parsed as YAML first; otherwise prefer JSON and fall
    # back to YAML. YAML is a JSON superset so this is safe either way.
    if not is_yaml_ext:
        try:
            doc = json.loads(stripped)
            return _as_dict(doc)
        except (ValueError, TypeError):
            pass
    if _yaml is not None:
        try:
            doc = _yaml.safe_load(stripped)
        except Exception as exc:  # noqa: BLE001 - surface a clean message
            raise ValueError(f"could not parse spec as YAML: {exc}") from exc
        return _as_dict(doc)
    # No YAML available and JSON already failed (or a .yaml path was given).
    if is_yaml_ext:
        raise ValueError(
            "spec looks like YAML but PyYAML is not installed; install PyYAML "
            "or provide a JSON spec"
        )
    try:
        doc = json.loads(stripped)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"could not parse spec as JSON: {exc}") from exc
    return _as_dict(doc)


def _as_dict(doc: Any) -> Dict[str, Any]:
    if not isinstance(doc, dict):
        raise ValueError("spec must resolve to a mapping/object at the top level")
    return doc


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
def _is_obj(v: Any) -> bool:
    return isinstance(v, dict)


def _clone(v: Any) -> Any:
    return json.loads(json.dumps(v)) if v is not None else None


def _deep_merge(base: Any, override: Any) -> Any:
    """Deep-merge ``override`` onto ``base``. Maps deep-merge; scalars/lists
    replace; a key ``+foo`` in override APPENDS its list to ``base.foo``."""
    if not _is_obj(base):
        return _clone(override)
    if not _is_obj(override):
        return _clone(base) if override is None else override
    out = _clone(base)
    for k, v in override.items():
        if isinstance(k, str) and k.startswith("+"):
            real = k[1:]
            base_arr = out[real] if isinstance(out.get(real), list) else []
            add = v if isinstance(v, list) else [v]
            out[real] = base_arr + add
        elif _is_obj(v) and _is_obj(out.get(k)):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = _clone(v)
    return out


def _resolve_deliverable_inherits(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve ``inherits:`` WITHIN a ``deliverables`` list — a list item may
    inherit a sibling by its ``id`` (merge the sibling under the item, drop
    ``inherits``)."""
    delivs = cfg.get("deliverables")
    if not isinstance(delivs, list):
        return cfg
    by_id = {d["id"]: d for d in delivs if isinstance(d, dict) and d.get("id")}
    memo: Dict[str, Any] = {}
    visiting: set = set()

    def resolve_item(d: Any) -> Any:
        if not isinstance(d, dict) or not d.get("id"):
            return d
        did = d["id"]
        if did in memo:
            return memo[did]
        if not d.get("inherits"):
            memo[did] = d
            return d
        if did in visiting:
            raise ValueError(f"deliverable inherits cycle at '{did}'")
        parent = by_id.get(d["inherits"])
        if parent is None:
            raise ValueError(f"deliverable '{did}' inherits unknown '{d['inherits']}'")
        visiting.add(did)
        rest = {k: v for k, v in d.items() if k != "inherits"}
        merged = _deep_merge(resolve_item(parent), rest)
        visiting.discard(did)
        memo[did] = merged
        return merged

    resolved = {**cfg, "deliverables": [resolve_item(d) for d in delivs]}
    return resolved


def _resolve_config(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve a spec's ``parent`` chain (deep-merged, parent-first) and its
    intra-``deliverables`` inheritance into one fully-resolved config dict."""
    base: Dict[str, Any] = {}
    chain = _parent_chain(spec)
    for cfg in chain:
        base = _deep_merge(base, cfg)
    merged = _deep_merge(base, _spec_config(spec))
    return _resolve_deliverable_inherits(merged if _is_obj(merged) else {})


def _spec_config(spec: Dict[str, Any]) -> Dict[str, Any]:
    """The config portion of a spec: everything except identity/parent keys."""
    return {
        k: v
        for k, v in spec.items()
        if k not in ("slug", "kind", "parent", "inherits", "resolve_ref", "project_id")
    }


def _parent_chain(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the ordered list of ancestor configs (root-first) declared inline
    via nested ``parent`` mappings. A ``parent`` may be a full spec dict
    (inline authoring) — string parent references need a DB the compiler does
    not read here, so only inline parents are followed, mirroring the offline,
    single-spec surface."""
    parents: List[Dict[str, Any]] = []
    node = spec.get("parent")
    guard = 0
    while _is_obj(node):
        guard += 1
        if guard > 64:
            raise ValueError("parent inheritance chain too deep (cycle?)")
        parents.append(_spec_config(node))
        node = node.get("parent")
    parents.reverse()  # root ancestor first so nearer parents override
    return parents


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
def _normalize_stage(st: Any) -> Dict[str, Any]:
    """Normalise a pipeline entry (string or mapping) to a stage dict."""
    if isinstance(st, str):
        return {"stage": st, "gate": None}
    if _is_obj(st):
        out = dict(st)
        out.setdefault("stage", st.get("stage"))
        out.setdefault("gate", st.get("gate"))
        return out
    raise ValueError(f"pipeline entry must be a string or mapping, got {type(st).__name__}")


def _validate_resolved(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Validate a resolved config; return the normalised pipeline. Raises
    :class:`ValueError` with a clear message on a hard schema failure."""
    science = None
    grade = cfg.get("grade")
    if _is_obj(grade):
        science = grade.get("color_science")
    if science is None and _is_obj(cfg.get("color")):
        science = cfg["color"].get("science")
    if science and str(science).lower() not in _KNOWN_SCIENCE:
        raise ValueError(
            f"color science '{science}' unknown ({'|'.join(sorted(_KNOWN_SCIENCE))})"
        )

    delivs = cfg.get("deliverables")
    if isinstance(delivs, list):
        for d in delivs:
            if not (_is_obj(d) and _is_obj(d.get("video")) and d["video"].get("codec")):
                did = d.get("id", "?") if _is_obj(d) else "?"
                raise ValueError(f"deliverable '{did}' requires video.codec")

    pipeline = cfg.get("pipeline")
    normalized: List[Dict[str, Any]] = []
    if pipeline is not None:
        if not isinstance(pipeline, list):
            raise ValueError("pipeline: must be a list of stages")
        for entry in pipeline:
            st = _normalize_stage(entry)
            name = st.get("stage")
            if not name:
                raise ValueError(f"pipeline entry missing a stage name ({json.dumps(entry, default=str)})")
            if name not in _KNOWN_STAGES:
                raise ValueError(
                    f"pipeline stage '{name}' unknown ({'|'.join(sorted(_KNOWN_STAGES))})"
                )
            gate = st.get("gate")
            if gate is not None and gate not in _KNOWN_GATES:
                raise ValueError(
                    f"stage '{name}' gate '{gate}' unknown ({'|'.join(sorted(_KNOWN_GATES))})"
                )
            normalized.append(st)
    return normalized


# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------
def _hash(obj: Any) -> str:
    """Stable short digest of a JSON-able object (spec/config identity)."""
    blob = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _slugify(name: str) -> str:
    out = "".join(c.lower() if c.isalnum() else "_" for c in name).strip("_")
    while "__" in out:
        out = out.replace("__", "_")
    return out or "project"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _project_identity(spec: Dict[str, Any], cfg: Dict[str, Any], project_id: str) -> Tuple[str, str]:
    """Derive ``(project_id, name)`` from the spec/resolved config or an
    explicit override."""
    name = (
        cfg.get("name")
        or cfg.get("project")
        or spec.get("slug")
        or spec.get("project_id")
        or "project"
    )
    name = str(name)
    pid = project_id or spec.get("project_id") or _slugify(name)
    return str(pid), name


# ---------------------------------------------------------------------------
# compile
# ---------------------------------------------------------------------------
def _do_compile(
    db_path: str,
    spec: Any,
    spec_path: str,
    project_id: str,
    now: str,
) -> str:
    if not db_path:
        raise ValueError("compile requires 'db_path' (path to the local SQLite store)")
    parsed = _load_spec(spec, spec_path)
    resolved = _resolve_config(parsed)
    pipeline = _validate_resolved(resolved)
    resolved["pipeline"] = pipeline  # normalised form is what the runner reads

    pid, name = _project_identity(parsed, resolved, project_id)
    resolve_ref = parsed.get("resolve_ref") or resolved.get("resolve_ref")
    content_hash = _hash(resolved)
    ts = now or _now_iso()

    try:
        store = open_db(db_path)
    except StoreError as e:
        raise ValueError(f"could not open store at {db_path!r}: {e}") from e
    try:
        prev = store.get_project(pid)
        prev_hash = None
        if prev and isinstance(prev.get("meta_json"), dict):
            prev_hash = prev["meta_json"].get("content_hash")

        project = store.upsert_project(
            project_id=pid,
            name=name,
            resolve_ref=str(resolve_ref) if resolve_ref else None,
            meta={
                "content_hash": content_hash,
                "resolved": resolved,
                "stage_count": len(pipeline),
            },
            now=ts,
        )

        changed = prev_hash != content_hash
        # Idempotent: only append a compile provenance event when the resolved
        # spec actually changed, so re-compiling an unchanged spec is a no-op
        # in the ledger.
        if changed:
            record_event(
                store,
                {
                    "project_id": pid,
                    "stage": "compile",
                    "tool": "pipeline.compile",
                    "actor": "system",
                    "config_hash": content_hash,
                    "intent": "compiled",
                    "actual": "compiled",
                    "at": ts,
                    "stage_count": len(pipeline),
                },
            )

        result = {
            "action": "compile",
            "project": {
                "project_id": project["project_id"],
                "name": project["name"],
                "resolve_ref": project["resolve_ref"],
            },
            "content_hash": content_hash,
            "changed": changed,
            "idempotent": True,
            "stages": [
                {"stage_index": i, "stage": s["stage"], "gate": s.get("gate")}
                for i, s in enumerate(pipeline)
            ],
            "resolved": resolved,
            "verified": False,
        }
        return json.dumps(result, indent=2)
    except StoreError as e:
        raise ValueError(f"store error during compile: {e}") from e
    finally:
        store.close()


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
def _stage_outcome(stage_spec: Dict[str, Any]) -> Tuple[str, str, bool, bool]:
    """Compute a stage's ``(intent, actual, drift, failed)``.

    Offline, a stage's ``intent`` and ``actual`` come from the resolved stage
    spec (a live executor would supply ``actual`` at run time). Defaults keep a
    plain stage clean: ``actual`` defaults to ``intent`` (no drift). A stage may
    author ``intent``/``actual`` to model an expected-vs-observed outcome, or
    ``fail: true`` to force a failure. A gated stage that drifts fails its gate.
    """
    name = stage_spec.get("stage")
    intent = stage_spec.get("intent")
    if intent is None:
        intent = f"{name}:complete"
    actual = stage_spec.get("actual")
    if actual is None:
        actual = intent
    intent_s = str(intent)
    actual_s = str(actual)
    drift = actual_s != intent_s
    explicit_fail = bool(stage_spec.get("fail"))
    gate = stage_spec.get("gate")
    # A gate fails when its stage drifted or was explicitly failed.
    gate_failed = bool(gate) and (drift or explicit_fail)
    failed = explicit_fail or gate_failed
    return intent_s, actual_s, drift, failed


def _do_run(
    db_path: str,
    project_id: str,
    profile: str,
    now: str,
) -> str:
    if not db_path:
        raise ValueError("run requires 'db_path' (path to the local SQLite store)")
    if not project_id:
        raise ValueError("run requires 'project_id' (a compiled project)")

    try:
        store = open_db(db_path)
    except StoreError as e:
        raise ValueError(f"could not open store at {db_path!r}: {e}") from e
    try:
        project = store.get_project(project_id)
        if project is None:
            raise ValueError(
                f"no compiled project '{project_id}' — run action='compile' first"
            )
        meta = project.get("meta_json") or {}
        resolved = meta.get("resolved") or {}
        pipeline = resolved.get("pipeline") or []
        if not isinstance(pipeline, list) or not pipeline:
            raise ValueError(f"project '{project_id}' has no pipeline: to run")

        spec_hash = _hash({"pipeline": pipeline, "profile": profile or None})
        # Deterministic run id → re-running the same compiled pipeline reuses
        # the same run (idempotent), rather than spawning a fresh one each call.
        run_id = f"run:{project_id}:{spec_hash}"
        ts = now or _now_iso()

        existing_run = store.get_run(run_id, with_stages=True)
        first_time = existing_run is None
        if first_time:
            store.upsert_run(
                run_id=run_id,
                project_id=project_id,
                profile=profile or None,
                status="planned",
                spec_hash=spec_hash,
                now=ts,
            )
        store.set_run_status(run_id, "running", ts)

        progressed: List[Dict[str, Any]] = []
        drift_stages: List[Dict[str, Any]] = []
        halted_at: Optional[Dict[str, Any]] = None

        for idx, entry in enumerate(pipeline):
            stage_spec = _normalize_stage(entry)
            name = stage_spec.get("stage")
            gate = stage_spec.get("gate")
            config_hash = _hash(stage_spec)

            prior = store.get_stage(run_id, idx)
            if prior and prior.get("status") in _TERMINAL_STATUSES:
                # Idempotent short-circuit: an already-terminal stage is neither
                # re-executed nor re-recorded — no double-count in provenance.
                status = prior["status"]
                prev_result = prior.get("result_json") or {}
                if prev_result.get("drift"):
                    drift_stages.append(
                        {"stage_index": idx, "stage": name, **_drift_view(prev_result)}
                    )
                if status == "halted":
                    halted_at = {
                        "stage_index": idx,
                        "stage": name,
                        "gate": prior.get("gate"),
                        "reason": prev_result.get("reason", "gate_failed"),
                    }
                    progressed.append({"stage_index": idx, "stage": name, "status": status, "cached": True})
                    break
                progressed.append({"stage_index": idx, "stage": name, "status": status, "cached": True})
                continue

            intent, actual, drift, failed = _stage_outcome(stage_spec)
            stage_result = {
                "intent": intent,
                "actual": actual,
                "drift": drift,
                "gate": gate,
            }

            if failed:
                reason = "gate_failed" if gate else "stage_failed"
                stage_result["reason"] = reason
                store.upsert_stage(
                    run_id=run_id,
                    stage_index=idx,
                    stage=name,
                    status="halted",
                    gate=gate,
                    tool="pipeline.run",
                    config_hash=config_hash,
                    result=stage_result,
                    now=ts,
                )
                record_event(
                    store,
                    {
                        "project_id": project_id,
                        "run_id": run_id,
                        "stage": name,
                        "tool": "pipeline.run",
                        "actor": "system",
                        "config_hash": config_hash,
                        "target": f"stage:{idx}",
                        "intent": intent,
                        "actual": actual,
                        "result": reason,
                        "at": ts,
                    },
                )
                if drift:
                    drift_stages.append({"stage_index": idx, "stage": name, "intent": intent, "actual": actual})
                halted_at = {"stage_index": idx, "stage": name, "gate": gate, "reason": reason}
                progressed.append({"stage_index": idx, "stage": name, "status": "halted", "reason": reason})
                break

            # Stage passes (ungated drift is allowed through but still flagged).
            store.upsert_stage(
                run_id=run_id,
                stage_index=idx,
                stage=name,
                status="done",
                gate=gate,
                tool="pipeline.run",
                config_hash=config_hash,
                result=stage_result,
                now=ts,
            )
            record_event(
                store,
                {
                    "project_id": project_id,
                    "run_id": run_id,
                    "stage": name,
                    "tool": "pipeline.run",
                    "actor": "system",
                    "config_hash": config_hash,
                    "target": f"stage:{idx}",
                    "intent": intent,
                    "actual": actual,
                    "result": "done",
                    "at": ts,
                },
            )
            if drift:
                drift_stages.append({"stage_index": idx, "stage": name, "intent": intent, "actual": actual})
            progressed.append({"stage_index": idx, "stage": name, "status": "done", "drift": drift})

        if halted_at is not None:
            run_status = "halted"
        else:
            run_status = "done"
        store.set_run_status(run_id, run_status, ts)

        result = {
            "action": "run",
            "run_id": run_id,
            "project_id": project_id,
            "profile": profile or None,
            "spec_hash": spec_hash,
            "status": run_status,
            "first_time": first_time,
            "stages_total": len(pipeline),
            "progressed": progressed,
            "drift": drift_stages,
            "drift_count": len(drift_stages),
            "halted": halted_at,
            "idempotent": True,
            "verified": False,
        }
        return json.dumps(result, indent=2)
    except StoreError as e:
        raise ValueError(f"store error during run: {e}") from e
    finally:
        store.close()


def _drift_view(stage_result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "intent": stage_result.get("intent"),
        "actual": stage_result.get("actual"),
    }


# ---------------------------------------------------------------------------
# status / report
# ---------------------------------------------------------------------------
def _do_status(db_path: str, project_id: str, run_id: str) -> str:
    if not db_path:
        raise ValueError("status requires 'db_path'")
    try:
        store = open_db(db_path)
    except StoreError as e:
        raise ValueError(f"could not open store at {db_path!r}: {e}") from e
    try:
        if run_id:
            run = store.get_run(run_id, with_stages=True)
            if run is None:
                raise ValueError(f"no run '{run_id}'")
            runs = [run]
        elif project_id:
            runs = [store.get_run(r["run_id"], with_stages=True) for r in store.list_runs(project_id)]
        else:
            raise ValueError("status requires 'run_id' or 'project_id'")
        return json.dumps(
            {"action": "status", "project_id": project_id or None, "runs": runs},
            indent=2,
            default=str,
        )
    except StoreError as e:
        raise ValueError(f"store error during status: {e}") from e
    finally:
        store.close()


def _do_report(db_path: str, project_id: str) -> str:
    if not db_path:
        raise ValueError("report requires 'db_path'")
    if not project_id:
        raise ValueError("report requires 'project_id'")
    try:
        store = open_db(db_path)
    except StoreError as e:
        raise ValueError(f"could not open store at {db_path!r}: {e}") from e
    try:
        report = episode_report(store, project_id)
        report["action"] = "report"
        return json.dumps(report, indent=2, default=str)
    except StoreError as e:
        raise ValueError(f"store error during report: {e}") from e
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool entry point
# ---------------------------------------------------------------------------
@mcp.tool()
def pipeline(
    action: str,
    db_path: str = "",
    spec: Any = None,
    spec_path: str = "",
    project_id: str = "",
    run_id: str = "",
    profile: str = "",
    now: str = "",
) -> str:
    """DB-as-truth pipeline orchestration for the OFFLINE/advanced tool set. Never touches Resolve.

    Compiles an authored project spec into the canonical local SQLite store,
    then runs the project's pipeline stage-by-stage with gates, an append-only
    provenance ledger, and intent-vs-actual drift detection. Composes
    `store.db` (projects/runs/stages) and `store.provenance` (lineage). Reads a
    local spec file and reads/writes a local SQLite DB only — no DaVinci Resolve
    connection.

    Parameters:
    - action: one of
        - "compile": parse a spec and write the fully-resolved project into the
          DB. Provide the spec via `spec` (a mapping, or a JSON/YAML string) or
          `spec_path` (a `.json`/`.yaml`/`.yml` file; YAML needs PyYAML, JSON
          always works). The spec's inheritance is resolved (inline `parent`
          chains deep-merged parent-first; maps deep-merge, scalars/lists
          replace, a `+key` appends to a list; `deliverables[].inherits` reuses
          a sibling by `id`), then the resolved config is validated (pipeline
          stage names must be one of ingest|conform|offline_ref|color_groups|
          leveling|grade|audio_sync|qc|deliver; a stage `gate` must be
          review|pass; any `grade.color_science`/`color.science` and each
          deliverable's `video.codec` are checked). The resolved project is
          upserted keyed on `project_id` (explicit, else the spec's
          `project_id`/`slug`/name slug). Idempotent: re-compiling an unchanged
          spec updates the same row and appends no duplicate provenance event.
          Requires `db_path`.
        - "run": plan + execute a compiled project's pipeline. Creates a run
          keyed deterministically on the project + resolved-pipeline hash (+
          `profile`), then walks the stages in order. Each stage records
          provenance with its `intent` and `actual`; a stage whose `actual`
          differs from its `intent` is flagged as drift. A GATED stage that
          drifts (or authors `fail: true`) fails its gate, which HALTS the run
          and is reported under `halted` with the failing gate + stage index;
          ungated drift is flagged but does not halt. Re-running an
          already-complete run short-circuits every terminal stage and appends
          no new provenance (no double-count). Requires `db_path` and
          `project_id`; `profile` is optional.
        - "status": read back a run's stage table + status. Requires `db_path`
          and either `run_id` (one run) or `project_id` (all of a project's
          runs).
        - "report": render the project's provenance episode report — the full
          lineage in recorded order with every intent!=actual entry flagged as
          drift. Requires `db_path` and `project_id`.
    - db_path: path to the local SQLite store (created on first use). Required
      for every action.
    - spec / spec_path: the authored spec for "compile" (supply one).
    - project_id: the compiled project's id (derived on compile; required for
      run/report and for status without a run_id).
    - run_id: a specific run to inspect ("status").
    - profile: optional run profile label ("run").
    - now: optional caller-supplied ISO timestamp (else UTC now) — lets callers
      make runs deterministic in tests.

    Any other action returns the list of valid actions. `run` results carry
    `"verified": false` (structural-only until live Resolve calibration).
    Malformed input never raises — it comes back as an "Error: ..." string.
    """
    action_lower = action.strip().lower() if isinstance(action, str) else ""

    try:
        if action_lower == "compile":
            return _do_compile(db_path, spec, spec_path, project_id, now)
        if action_lower == "run":
            return _do_run(db_path, project_id, profile, now)
        if action_lower == "status":
            return _do_status(db_path, project_id, run_id)
        if action_lower == "report":
            return _do_report(db_path, project_id)
        return json.dumps(
            {"error": f"Unknown action '{action}'.", "valid_actions": list(_VALID_ACTIONS)},
            indent=2,
        )
    except Exception as e:  # noqa: BLE001 - offline tools never raise
        return f"Error: {e}"
