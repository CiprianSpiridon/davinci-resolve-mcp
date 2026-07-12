"""
davinci_resolve_mcp.tools.off_deliverable — offline ``deliverable``:
deliverable QC / compliance profiles (OFFLINE/advanced tool set).

Registers exactly one action-dispatch tool, ``deliverable``, with one action:

  * ``"qc"`` — run a NAMED compliance profile (see :data:`COMPLIANCE_PROFILES`)
    against caller-supplied numbers and report per-check pass/fail.

A compliance profile bundles two kinds of check, matching the module
description ("composes grading.qc broadcast-legal + spec checks"):

  1. A broadcast-legal / gamut check over sampled pixels, delegated
     verbatim to :func:`davinci_resolve_mcp.grading.qc.broadcast_legal`
     (this module never reimplements that math — it only selects the
     profile's ``standard``/``max_illegal_pct`` and shapes the result into
     one named check).
  2. A set of spec-field checks: every key present in the caller's `spec`
     dict is compared against the matching key in `actual` (numeric fields
     within a per-field tolerance, everything else by case-insensitive
     string equality), plus a completeness check that the profile's
     `required_spec_fields` are actually present in `spec`.

Like :mod:`davinci_resolve_mcp.grading.qc`, this module operates purely on
caller-supplied numbers/dicts — it never probes a media file itself (no
``ffprobe``/``ffmpeg`` shell-out, no image decode). This is the offline
tool set: a caller (or a live-Resolve-aware layer above this one) is
expected to have already sampled pixels / measured technical specs and
hand them in as plain data.

This module NEVER connects to DaVinci Resolve: no ``DaVinciResolveScript``
import, no live session, nothing at import time beyond registering the
tool, and it never opens/writes any file — every action here is a pure,
read-only computation over its arguments. Every code path returns a JSON
string; nothing here raises — the top-level ``deliverable`` dispatcher
catches every exception and returns an ``"Error: ..."`` string instead. An
unknown compliance profile never raises either — it returns the valid
profile list (mirrors :func:`~davinci_resolve_mcp.grading.qc.broadcast_legal`'s
unknown-standard contract) so a caller/LLM can self-correct. ``qc`` is a
MEASURE/report-only action (it never writes anything), so — unlike the
WRITE/grade actions elsewhere in the offline tool set — its result carries
no top-level ``"verified"`` field; the nested ``broadcastLegal`` check
result does carry `grading.qc.broadcast_legal`'s own ``"pass"``/``"warnings"``
(that function itself never returns a "verified" field — it is a pure
comparison, not a grade write).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..app import mcp
from ..grading import qc as qc_mod

_VALID_ACTIONS = ("qc",)

# ---------------------------------------------------------------------------
# Named compliance profiles.
#
# Each profile bundles a `grading.qc.broadcast_legal` standard/tolerance
# (None disables the gamut check entirely — e.g. a web/review proxy has no
# broadcast-legal gate) plus the deliverable-spec fields it requires the
# caller's `spec` dict to define, and any per-field numeric tolerances
# (falls back to `_DEFAULT_TOLERANCE` for a numeric field not listed here).
# ---------------------------------------------------------------------------
_DEFAULT_TOLERANCE = 0.0

COMPLIANCE_PROFILES: Dict[str, Dict[str, Any]] = {
    "broadcast_rec709": {
        "label": "Broadcast delivery — BT.709 legal range, standard technical spec",
        "broadcast_standard": "rec709",
        "max_illegal_pct": 0.0,
        "required_spec_fields": ("width", "height", "fps", "codec", "container"),
        "tolerances": {"fps": 0.01},
    },
    "broadcast_rec2020_hdr": {
        "label": "Broadcast HDR delivery — BT.2020 legal range, HDR technical spec",
        "broadcast_standard": "rec2020",
        "max_illegal_pct": 0.0,
        "required_spec_fields": (
            "width",
            "height",
            "fps",
            "codec",
            "container",
            "color_space",
            "transfer",
        ),
        "tolerances": {"fps": 0.01},
    },
    "streaming_delivery": {
        "label": "Streaming/OTT delivery — full-range video, standard technical spec",
        "broadcast_standard": "full_range",
        "max_illegal_pct": 1.0,
        "required_spec_fields": ("width", "height", "fps", "codec", "container"),
        "tolerances": {"fps": 0.01},
    },
    "web_proxy": {
        "label": "Web/review proxy — no broadcast-legal gate, spec-only",
        "broadcast_standard": None,
        "max_illegal_pct": 100.0,
        "required_spec_fields": ("width", "height", "container"),
        "tolerances": {},
    },
}


def list_compliance_profiles() -> List[str]:
    """Return the sorted list of valid `deliverable qc` `profile` names."""
    return sorted(COMPLIANCE_PROFILES)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _field_check(
    field: str,
    expected: Any,
    actual: Mapping[str, Any],
    tolerance: float,
) -> Dict[str, Any]:
    name = f"spec.{field}"
    if field not in actual:
        return {
            "name": name,
            "pass": False,
            "expected": expected,
            "actual": None,
            "tolerance": tolerance if _is_number(expected) else None,
            "reason": f"'actual' is missing field '{field}'",
        }
    actual_value = actual[field]
    if _is_number(expected) and _is_number(actual_value):
        diff = abs(float(actual_value) - float(expected))
        passed = diff <= tolerance
        return {
            "name": name,
            "pass": passed,
            "expected": expected,
            "actual": actual_value,
            "tolerance": tolerance,
            "diff": round(diff, 6),
            "reason": None if passed else f"{diff} exceeds tolerance {tolerance}",
        }
    passed = str(actual_value).strip().lower() == str(expected).strip().lower()
    return {
        "name": name,
        "pass": passed,
        "expected": expected,
        "actual": actual_value,
        "tolerance": None,
        "reason": None if passed else f"{actual_value!r} != {expected!r}",
    }


def _do_qc(
    profile: str,
    samples: Optional[Sequence[Any]],
    spec: Optional[Mapping[str, Any]],
    actual: Optional[Mapping[str, Any]],
    tolerances: Optional[Mapping[str, float]],
    scale: float,
) -> Dict[str, Any]:
    profile_key = (profile or "").strip()
    prof = COMPLIANCE_PROFILES.get(profile_key)
    if prof is None:
        return {
            "error": f"unknown compliance profile '{profile}'",
            "valid_profiles": list_compliance_profiles(),
        }

    spec = spec or {}
    actual = actual or {}
    if not isinstance(spec, Mapping):
        raise ValueError(f"deliverable qc: 'spec' must be a mapping, got {type(spec).__name__}")
    if not isinstance(actual, Mapping):
        raise ValueError(f"deliverable qc: 'actual' must be a mapping, got {type(actual).__name__}")
    tol_overrides = dict(tolerances) if isinstance(tolerances, Mapping) else {}

    checks: List[Dict[str, Any]] = []
    warnings: List[str] = []

    # 1. Broadcast-legal / gamut check (delegated to grading.qc, never
    #    reimplemented here).
    broadcast_legal_result: Optional[Dict[str, Any]] = None
    standard = prof["broadcast_standard"]
    if standard is None:
        checks.append({
            "name": "broadcast_legal",
            "pass": None,
            "reason": f"profile '{profile_key}' has no broadcast-legal gate",
        })
    elif not samples:
        checks.append({
            "name": "broadcast_legal",
            "pass": None,
            "reason": "skipped: no 'samples' supplied",
        })
    else:
        broadcast_legal_result = qc_mod.broadcast_legal(
            samples,
            standard,
            max_illegal_pct=prof["max_illegal_pct"],
            scale=scale,
        )
        checks.append({
            "name": "broadcast_legal",
            "pass": broadcast_legal_result["pass"],
            "standard": standard,
            "illegalPct": broadcast_legal_result["illegal_pct"],
            "maxIllegalPct": broadcast_legal_result["max_illegal_pct"],
            "clippedPct": broadcast_legal_result["clipped_pct"],
        })
        warnings.extend(broadcast_legal_result.get("warnings") or [])

    # 2. Profile completeness: every required spec field must actually be
    #    defined in the caller's `spec`.
    required_fields: Sequence[str] = prof["required_spec_fields"]
    missing_required = [f for f in required_fields if f not in spec]
    checks.append({
        "name": "spec.required_fields",
        "pass": not missing_required,
        "required": list(required_fields),
        "missing": missing_required,
        "reason": None
        if not missing_required
        else f"'spec' is missing required field(s) {missing_required} for profile '{profile_key}'",
    })

    # 3. Per-field spec checks — every key the caller put in `spec` is
    #    checked against `actual` (not just the required subset — a caller
    #    supplying extra fields gets them checked too).
    for field, expected in spec.items():
        tolerance = tol_overrides.get(field, prof["tolerances"].get(field, _DEFAULT_TOLERANCE))
        checks.append(_field_check(field, expected, actual, tolerance))

    failed_checks = [c["name"] for c in checks if c["pass"] is False]
    skipped_checks = [c["name"] for c in checks if c["pass"] is None]
    overall_pass = not failed_checks

    return {
        "action": "qc",
        "profile": profile_key,
        "label": prof["label"],
        "broadcastLegal": broadcast_legal_result,
        "checks": checks,
        "failedChecks": failed_checks,
        "skippedChecks": skipped_checks,
        "pass": overall_pass,
        "warnings": warnings,
    }


@mcp.tool()
def deliverable(
    action: str,
    profile: str = "",
    samples_json: str = "",
    spec_json: str = "",
    actual_json: str = "",
    tolerances_json: str = "",
    scale: float = 255.0,
) -> str:
    """Deliverable QC / compliance: run a named compliance profile and report per-check pass/fail. Never touches Resolve, never reads a file.

    Parameters:
    - action: only "qc" is implemented.
        - "qc": run `profile` (see below) against caller-supplied numbers
          and report per-check pass/fail. Composes two check kinds:
            1. broadcast-legal / gamut, delegated to
               `grading.qc.broadcast_legal` over `samples_json` using the
               profile's standard + tolerance (skipped, `"pass": null`, if
               the profile has no gate or `samples_json` is empty).
            2. spec checks: every key in `spec_json` is compared against
               `actual_json` (numeric within a per-field tolerance from
               `tolerances_json`/the profile default, everything else by
               case-insensitive string equality), plus a
               `"spec.required_fields"` completeness check that the
               profile's required fields are present in `spec_json`.
          Returns `{action, profile, label, broadcastLegal, checks,
          failedChecks, skippedChecks, pass, warnings}`. `checks` lists
          EVERY check run (`"pass": true/false/null` for skipped); a
          non-compliant sample lists every failed check name in
          `failedChecks` — nothing is silently dropped. `pass` is true iff
          `failedChecks` is empty (a skipped check never fails the
          overall result on its own). An unknown `profile` returns
          `{"error": ..., "valid_profiles": [...]}` instead of raising —
          call `deliverable` with any other/no profile to discover them.
      Any other action returns the list of valid actions instead of
      erroring.
    - profile: compliance profile name — one of "broadcast_rec709",
      "broadcast_rec2020_hdr", "streaming_delivery", "web_proxy" (see
      `broadcastLegal`/`checks` in an unknown-profile result for the
      current list, or call with an empty/unknown profile).
    - samples_json: JSON array of RGB samples for the broadcast-legal
      check, each `{"r":.., "g":.., "b":..}` or `[r, g, b]` (an optional
      `"id"` key is echoed back per-sample by `grading.qc.broadcast_legal`).
      Leave empty to skip the broadcast-legal check.
    - spec_json: JSON object of expected deliverable spec values, e.g.
      `{"width": 1920, "height": 1080, "fps": 23.976, "codec": "ProRes422HQ",
      "container": "mov"}`. Every key present here is checked; the
      profile's `required_spec_fields` must all be present here too (a
      missing one fails `"spec.required_fields"`).
    - actual_json: JSON object of the deliverable's actually-measured
      values, same field names as `spec_json`.
    - tolerances_json: JSON object of `{field: tolerance}` overrides for
      numeric spec-field checks (falls back to the profile's own
      tolerance, then 0.0 — an exact match).
    - scale: full-scale value for `samples_json` (255.0 default for 8-bit-
      style samples; pass 1.0 for normalized float samples) — forwarded to
      `grading.qc.broadcast_legal`.

    Malformed `samples_json`/`spec_json`/`actual_json`/`tolerances_json`
    (not valid JSON, or not the expected array/object shape) always comes
    back as an "Error: ..." string (never a raised exception), per the
    offline tool-set convention.
    """
    action_lower = action.strip().lower() if isinstance(action, str) else ""
    try:
        if action_lower == "qc":
            samples = _parse_json_arg(samples_json, "samples_json", expect=list)
            spec = _parse_json_arg(spec_json, "spec_json", expect=dict)
            actual = _parse_json_arg(actual_json, "actual_json", expect=dict)
            tolerances = _parse_json_arg(tolerances_json, "tolerances_json", expect=dict)
            return json.dumps(
                _do_qc(profile, samples, spec, actual, tolerances, scale),
                indent=2,
            )

        return json.dumps(
            {
                "error": f"Unknown action '{action}'.",
                "valid_actions": list(_VALID_ACTIONS),
            },
            indent=2,
        )
    except Exception as e:  # noqa: BLE001 - offline tools never raise
        return f"Error: {e}"


def _parse_json_arg(raw: str, label: str, *, expect: type) -> Any:
    """Parse an optional JSON string argument. Empty/whitespace-only input
    returns an empty instance of `expect` (`[]`/`{}`); non-empty input must
    parse to exactly `expect`'s type. Raises ValueError otherwise — caught
    by the top-level dispatcher and turned into an "Error: ..." string."""
    if raw is None or not str(raw).strip():
        return expect()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"deliverable: '{label}' is not valid JSON: {e}") from e
    if not isinstance(parsed, expect):
        raise ValueError(
            f"deliverable: '{label}' must be a JSON {expect.__name__}, got {type(parsed).__name__}"
        )
    return parsed
