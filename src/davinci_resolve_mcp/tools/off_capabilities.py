"""Offline capability-discovery tool.

Single action-dispatch entry point (``capabilities``) that lets a caller
introspect the OFFLINE/advanced tool set — the ``off_*`` modules under
``davinci_resolve_mcp.tools`` — without touching a running DaVinci Resolve
instance. It answers three questions:

1. Which optional third-party dependencies are available in this
   interpreter (``ffmpeg`` on ``PATH``, ``PyYAML``, ``sqlite3``,
   ``zstandard``)?
2. Which offline domains (action-dispatch tools such as ``drx``, ``drp``,
   ``pipeline``, ...) are actually importable in this build, and what
   actions does each one plan to expose?
3. Which write/grade paths are structurally-verified-only (i.e. not yet
   calibrated against a live Resolve session)?

This module is intentionally self-contained: it never imports
``DaVinciResolveScript``, never opens a network/Resolve connection, and
never imports the other ``off_*`` tool modules directly (several of them
may not exist yet in a given build) — it only *probes* for them via
``importlib.util.find_spec``, which resolves a module's location without
executing it. Every code path returns a string; nothing here raises.
"""

from __future__ import annotations

import importlib.util
import json
import shutil

from ..app import mcp

# ── Optional dependency probes ───────────────────────────────────────────
# Each entry maps a human-readable dependency name to a zero-arg callable
# that returns True/False. None of these ever raise: `importlib.util.find_spec`
# only resolves a module's location (it does not execute the module), and
# `shutil.which` is a plain PATH lookup.

def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        # A namespace/partially-installed package can make find_spec raise
        # ValueError; a broken installed .pth can make it raise ImportError.
        # Either way, treat the dependency as unavailable rather than crash.
        return False


def _has_exe(name: str) -> bool:
    return shutil.which(name) is not None


_DEPENDENCIES = {
    "ffmpeg": {
        "check": lambda: _has_exe("ffmpeg"),
        "kind": "executable",
        "used_by": ["offline_ref", "offline_audio"],
        "note": "Optional. Reference-frame extraction and loudness/level "
        "reads degrade to a clear 'requires ffmpeg' message when absent.",
    },
    "pyyaml": {
        "check": lambda: _has_module("yaml"),
        "kind": "python-package",
        "used_by": ["pipeline"],
        "note": "Optional. YAML project specs require it; JSON specs work "
        "either way.",
    },
    "sqlite3": {
        "check": lambda: _has_module("sqlite3"),
        "kind": "python-stdlib",
        "used_by": [
            "project_db",
            "conform",
            "pipeline",
            "media_ingest",
            "editorial",
            "provenance",
        ],
        "note": "Standard-library module backing store/db.py (the "
        "DB-as-truth store) and store/provenance.py (the audit ledger). "
        "Expected to always be available on a standard CPython build.",
    },
    "zstandard": {
        "check": lambda: _has_module("zstandard"),
        "kind": "python-package",
        "used_by": ["drx", "color_trace"],
        "note": "Required to decode/encode the .drx grade FieldsBlob "
        "(a length-prefixed zstd stream). Domains that touch grade bytes "
        "degrade to a clear error without it.",
    },
}

# ── Offline domain catalog ───────────────────────────────────────────────
# One entry per action-dispatch tool in the OFFLINE/advanced surface
# (davinci-resolve-mcp-offline-advanced plan, TASK-016..TASK-032). Each
# domain is implemented as `tools/off_<slug>.py` and registers exactly one
# `@mcp.tool()` named after the domain. `actions` documents the planned
# action vocabulary for that dispatcher; `writes` lists actions whose
# result carries `"verified": false` because they have not yet been
# calibrated against a live Resolve session (see TASK-038).

_DOMAINS = {
    "drx": {
        "module": "davinci_resolve_mcp.tools.off_drx",
        "summary": "Read/inspect/author .drx grade files: decode params, "
        "export/import ASC-CDL, attach a named LUT, apply grading-catalog "
        "ops, verify a grade against a fixture.",
        "actions": [
            "inspect",
            "decode_params",
            "export_cdl",
            "import_cdl",
            "attach_lut",
            "apply_catalog_op",
            "verify_grade",
            "write",
        ],
        "writes": ["import_cdl", "attach_lut", "apply_catalog_op", "write"],
    },
    "drt": {
        "module": "davinci_resolve_mcp.tools.off_drt",
        "summary": "Parse/author/validate .drt timeline bundles and "
        "inject/extract them from a .drp project.",
        "actions": ["parse", "author", "validate", "inject_into_drp", "extract_from_drp"],
        "writes": ["author", "inject_into_drp"],
    },
    "drp": {
        "module": "davinci_resolve_mcp.tools.off_drp",
        "summary": "Read/author/edit a .drp project file offline.",
        "actions": ["read", "author", "edit"],
        "writes": ["author", "edit"],
    },
    "project_read": {
        "module": "davinci_resolve_mcp.tools.off_project_read",
        "summary": "Read-only project/timeline/clip inspection across "
        ".drp/.drt: structural lint and clip_where predicate queries.",
        "actions": ["lint", "clip_where"],
        "writes": [],
    },
    "project_db": {
        "module": "davinci_resolve_mcp.tools.off_project_db",
        "summary": "DB-backed project operations, including tidying node "
        "graph layout while preserving grade FieldsBlob bytes, and "
        "indexing a project into the SQLite store.",
        "actions": ["relayout_node_graphs", "index"],
        "writes": ["relayout_node_graphs", "index"],
    },
    "offline_ref": {
        "module": "davinci_resolve_mcp.tools.off_offline_ref",
        "summary": "Display-referred reference-frame extraction and intent "
        "tagging. Frame extraction requires ffmpeg.",
        "actions": ["extract", "tag"],
        "writes": ["tag"],
    },
    "conform": {
        "module": "davinci_resolve_mcp.tools.off_conform",
        "summary": "Frame-oracle conform/relink QC against a media "
        "manifest, plus conform lineage persisted to the store.",
        "actions": ["qc", "lineage"],
        "writes": ["lineage"],
    },
    "color_trace": {
        "module": "davinci_resolve_mcp.tools.off_color_trace",
        "summary": "Carry grades across a re-conform by mapping source "
        "grades onto re-conformed clips by clip identity, byte-for-byte "
        "where identity matches.",
        "actions": ["carry"],
        "writes": ["carry"],
    },
    "offline_fusion": {
        "module": "davinci_resolve_mcp.tools.off_fusion",
        "summary": "Read/inspect/edit Fusion comp settings inside a "
        "project file offline.",
        "actions": ["inspect", "edit"],
        "writes": ["edit"],
    },
    "audio_plan": {
        "module": "davinci_resolve_mcp.tools.off_audio_plan",
        "summary": "Offline audio planning: derive a track/stem plan from "
        "a project spec.",
        "actions": ["plan"],
        "writes": [],
    },
    "fairlight_plan": {
        "module": "davinci_resolve_mcp.tools.off_fairlight",
        "summary": "Offline Fairlight bus-routing plan from a routing "
        "spec.",
        "actions": ["route"],
        "writes": [],
    },
    "offline_audio": {
        "module": "davinci_resolve_mcp.tools.off_audio",
        "summary": "Offline audio ops such as loudness/level reads via "
        "ffmpeg when present.",
        "actions": ["loudness"],
        "writes": [],
    },
    "pipeline": {
        "module": "davinci_resolve_mcp.tools.off_pipeline",
        "summary": "Compile a YAML/JSON project spec into the canonical "
        "SQLite DB and run stages with gates, provenance, and "
        "intent-vs-actual drift detection.",
        "actions": ["compile", "run"],
        "writes": ["compile", "run"],
    },
    "deliverable": {
        "module": "davinci_resolve_mcp.tools.off_deliverable",
        "summary": "Deliverable QC / compliance: run a named compliance "
        "profile (broadcast-legal + spec checks) and report per-check "
        "pass/fail.",
        "actions": ["qc"],
        "writes": [],
    },
    "media_ingest": {
        "module": "davinci_resolve_mcp.tools.off_media",
        "summary": "Media front-end ingest: scan a folder and build a "
        "hashed media manifest persisted to the store.",
        "actions": ["scan"],
        "writes": ["scan"],
    },
    "editorial": {
        "module": "davinci_resolve_mcp.tools.off_editorial",
        "summary": "Editorial integrity / changelist: diff two project "
        "versions (added/removed/moved clips) and flag dangling media "
        "references.",
        "actions": ["changelist", "integrity"],
        "writes": [],
    },
    "provenance": {
        "module": "davinci_resolve_mcp.tools.off_provenance",
        "summary": "Provenance / audit / episode report: a thin tool over "
        "the provenance ledger.",
        "actions": ["report", "record"],
        "writes": ["record"],
    },
}

_VALID_ACTIONS = ("report", "deps", "domains")


def _dependency_report() -> dict:
    report = {}
    for name, spec in _DEPENDENCIES.items():
        try:
            available = bool(spec["check"]())
        except Exception:  # noqa: BLE001 - a probe must never break the tool
            available = False
        report[name] = {
            "available": available,
            "kind": spec["kind"],
            "used_by": spec["used_by"],
            "note": spec["note"],
        }
    return report


def _domain_report() -> dict:
    report = {}
    for name, spec in _DOMAINS.items():
        implemented = _has_module(spec["module"])
        report[name] = {
            "tool_name": name,
            "module": spec["module"],
            "implemented": implemented,
            "summary": spec["summary"],
            "actions": spec["actions"],
            "write_actions": spec["writes"],
        }
    return report


@mcp.tool()
def capabilities(action: str = "report") -> str:
    """Report OFFLINE/advanced tool-set capabilities as JSON. Never touches Resolve.

    Parameters:
    - action: one of
        - "report" (default): full report — dependency availability, the
          offline domain/action catalog, and the write-path verification
          note.
        - "deps": only the optional-dependency availability section
          (ffmpeg executable, PyYAML, sqlite3, zstandard).
        - "domains": only the offline domain/action catalog, including
          whether each domain's tool module is importable in this build.

    Any other value returns the list of valid actions instead of erroring.
    Every offline write/grade action reports "verified": false — the
    OFFLINE tool set only guarantees structural correctness (file
    format round-trips) until it has been calibrated against a live
    DaVinci Resolve session (see the deferred live-calibration task);
    this tool's own report always reflects that until that task lands.
    """
    action_lower = action.strip().lower() if isinstance(action, str) else ""

    if action_lower == "deps":
        return json.dumps({"action": "deps", "dependencies": _dependency_report()}, indent=2)

    if action_lower == "domains":
        return json.dumps({"action": "domains", "domains": _domain_report()}, indent=2)

    if action_lower != "report":
        return json.dumps(
            {
                "error": f"Unknown action '{action}'.",
                "valid_actions": list(_VALID_ACTIONS),
            },
            indent=2,
        )

    try:
        report = {
            "action": "report",
            "resolve_connection": "none — offline tools never connect to Resolve",
            "dependencies": _dependency_report(),
            "domains": _domain_report(),
            "write_paths": {
                "verified": False,
                "note": "All offline drx/grade write actions are "
                "structural-only (parsed/round-tripped/re-encoded on disk) "
                "and have not been calibrated against a live DaVinci "
                "Resolve session. Every such action returns its own "
                "\"verified\": false field until the live-calibration "
                "harness validates round-trips against a running Resolve "
                "instance.",
            },
        }
        return json.dumps(report, indent=2)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"
