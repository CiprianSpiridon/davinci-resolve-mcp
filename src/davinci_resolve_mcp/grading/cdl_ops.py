"""
davinci_resolve_mcp.grading.cdl_ops — pure ASC CDL param-dict grade math.

This module reimplements, as pure/offline functions over CDL param dicts
(see :mod:`davinci_resolve_mcp.formats.cdl`), the matching heuristics from
``contrast-normalize.mjs``, ``saturation-match.mjs`` and ``black-balance.mjs``.

The reference implementations measure robust (1st/99th percentile) pixel
statistics from *rendered PNG frames* (via the optional ``sharp`` dependency)
and emit a brand-new gain/offset (or saturation) DRX node to correct a clip
toward a hero/reference. This codebase's offline layer has no image
pipeline, so these functions instead treat an existing CDL param dict's own
SOP (Slope/Offset/Power) fields as the "measurement": a CDL's *implicit*
per-channel black point is its ``offset`` and its *implicit* white point is
``slope + offset`` (i.e. the value the channel maps to at input 0 and input
1 respectively, holding ``power`` fixed as an untouched separate gamma
axis — exactly as the reference never touches gamma/power when matching
contrast or black point). ``sat`` is used directly as the CDL's saturation
multiplier for :func:`saturation_match`.

Each function is pure (no I/O, no Resolve), takes one or two CDL param
dicts (validated via :func:`~davinci_resolve_mcp.formats.cdl.validate_cdl_params`,
so channel-mapping dicts ``{"r":..,"g":..,"b":..}`` are also accepted on
input), and returns a new CDL param dict (``slope``/``offset``/``power``/
``sat``) plus bookkeeping fields:

    * ``clamped``          — True if any computed correction hit a clamp
      bound, or a degenerate (near-zero span/saturation) input forced a
      safe no-op instead of a division.
    * ``correction_pct``   — magnitude of the applied correction, 0..100+.
    * ``warnings``         — human-readable notes (clamped channels,
      degenerate input, already-neutral shadows, ...).
    * ``verified``         — always False: this is structural grade math,
      not calibrated against a live Resolve render (see the offline/advanced
      tool set's WRITE-action convention).

Because the returned dict carries only the four ASC CDL fields plus these
informational extras, the result of one op can be fed directly as the
``src`` (or ``ref``) of another — :func:`~davinci_resolve_mcp.formats.cdl.validate_cdl_params`
ignores unknown keys.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..formats.cdl import validate_cdl_params

__all__ = ["apply_cdl", "contrast_normalize", "saturation_match", "black_balance"]

# BT.709 luma weights, used only to decide whether a src CDL's overall tonal
# range is degenerate (near-flat) before attempting any per-channel division.
_LUMA_WEIGHTS = (0.2126, 0.7152, 0.0722)

# Below this magnitude a span/saturation denominator is treated as "zero" —
# division is skipped in favour of an explicit safe (identity) result.
_EPS = 1e-9


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _luma(rgb: tuple[float, float, float]) -> float:
    wr, wg, wb = _LUMA_WEIGHTS
    r, g, b = rgb
    return wr * r + wg * g + wb * b


def apply_cdl(cdl: Mapping[str, Any], rgb: Any) -> list[float]:
    """
    Apply an ASC CDL SOP+Sat transform to one RGB sample.

    Params:
      cdl: a CDL param dict ({"slope","offset","power","sat"}, channels may
        also be given as {"r":..,"g":..,"b":..} mappings — see
        :func:`~davinci_resolve_mcp.formats.cdl.validate_cdl_params`).
      rgb: a 3-element [r, g, b] sequence, each channel typically in 0..1.

    Per the ASC CDL v1.2 spec: each channel is transformed independently as
    ``out = clamp01(max(0, in*slope + offset) ** power)``, then a
    luma-preserving saturation matrix is applied to the clamped SOP result:
    ``out_c = clamp01(luma + (sop_c - luma) * sat)`` where ``luma`` is the
    BT.709 weighted mean of the three (already SOP-transformed) channels.

    Returns the resulting [r, g, b] list, each channel clamped to 0..1.
    Raises ValueError if `cdl` fails validation or `rgb` is not length 3.
    """
    norm = validate_cdl_params(cdl)
    values = list(rgb)
    if len(values) != 3:
        raise ValueError(f"cdl_ops: rgb must have exactly 3 values, got {len(values)}")

    sop: list[float] = []
    for slope, offset, power, value in zip(norm["slope"], norm["offset"], norm["power"], values):
        base = max(0.0, float(value) * slope + offset)
        sop.append(_clamp(base**power, 0.0, 1.0))

    luma = _luma((sop[0], sop[1], sop[2]))
    sat = norm["sat"]
    return [_clamp(luma + (c - luma) * sat, 0.0, 1.0) for c in sop]


def contrast_normalize(
    src: Mapping[str, Any],
    ref: Mapping[str, Any],
    *,
    clamp_gain: tuple[float, float] = (0.5, 2.0),
    clamp_offset: tuple[float, float] = (-0.5, 0.5),
) -> dict[str, Any]:
    """
    Match src's implicit black/white point (per channel) onto ref's.

    Params:
      src: the CDL param dict to correct.
      ref: the "hero"/reference CDL param dict to match toward.
      clamp_gain: (lo, hi) bound on the per-channel multiplicative
        correction (mirrors contrast-normalize.mjs's default [0.5, 2]).
      clamp_offset: (lo, hi) bound on the per-channel additive correction
        (mirrors contrast-normalize.mjs's default [-0.5, 0.5]).

    For each channel, fits an affine ``scale``/``shift`` so that src's
    implicit black (``src.offset``) and white (``src.offset + src.slope``)
    points land on ref's, then composes that correction onto src's existing
    slope/offset (``power`` and ``sat`` pass through unchanged — this
    mirrors the reference, which never touches gamma when matching
    contrast). When src == ref this is the identity (scale=1, shift=0 per
    channel, within the clamp bounds), so no drift is introduced.

    Degenerate handling: if src's overall (BT.709-weighted) tonal span is
    ~zero (a flat/degenerate SOP, ``slope`` ~ [0,0,0] in luma terms), or a
    single channel's span is ~zero, no division is attempted for the
    affected channel(s) — the scale is held at 1.0 and ``clamped`` is set
    True instead.

    Returns a CDL param dict plus ``scale``, ``shift`` (the per-channel
    correction applied), ``clamped``, ``correction_pct``, ``warnings``, and
    ``verified: False``.
    """
    s = validate_cdl_params(src)
    r = validate_cdl_params(ref)
    glo, ghi = clamp_gain
    olo, ohi = clamp_offset
    warnings: list[str] = []

    # src.slope IS (white - black) per channel under this module's black =
    # offset / white = offset + slope model, so a luma-weighted "span" check
    # on slope alone tells us whether src's tonal range is usably nonzero.
    src_luma_span = _luma((s["slope"][0], s["slope"][1], s["slope"][2]))
    if abs(src_luma_span) < _EPS:
        warnings.append(
            "contrast_normalize: src has ~zero overall tonal range (degenerate/flat SOP) — returned unchanged"
        )
        return {
            **s,
            "scale": [1.0, 1.0, 1.0],
            "shift": [0.0, 0.0, 0.0],
            "clamped": True,
            "correction_pct": 0.0,
            "warnings": warnings,
            "verified": False,
        }

    scale = [1.0, 1.0, 1.0]
    shift = [0.0, 0.0, 0.0]
    new_slope = [0.0, 0.0, 0.0]
    new_offset = [0.0, 0.0, 0.0]
    clamped = False

    for i in range(3):
        span_src = s["slope"][i]
        if abs(span_src) < _EPS:
            sc = 1.0
            clamped = True
            warnings.append(f"contrast_normalize: channel {i} has ~zero span in src — scale held at 1.0")
        else:
            raw_scale = r["slope"][i] / span_src
            sc = _clamp(raw_scale, glo, ghi)
            if sc != raw_scale:
                clamped = True
                warnings.append(f"contrast_normalize: channel {i} gain clamped to {clamp_gain}")

        raw_shift = r["offset"][i] - sc * s["offset"][i]
        sh = _clamp(raw_shift, olo, ohi)
        if sh != raw_shift:
            clamped = True
            warnings.append(f"contrast_normalize: channel {i} offset clamped to {clamp_offset}")

        scale[i] = sc
        shift[i] = sh
        new_slope[i] = sc * s["slope"][i]
        new_offset[i] = sh + sc * s["offset"][i]

    correction = max(max(abs(scale[i] - 1.0), abs(shift[i])) for i in range(3))

    return {
        "slope": new_slope,
        "offset": new_offset,
        "power": list(s["power"]),
        "sat": s["sat"],
        "scale": scale,
        "shift": shift,
        "clamped": clamped,
        "correction_pct": round(100.0 * correction, 2),
        "warnings": warnings,
        "verified": False,
    }


def saturation_match(
    src: Mapping[str, Any],
    ref: Mapping[str, Any],
    *,
    clamp_scale: tuple[float, float] = (0.5, 2.0),
) -> dict[str, Any]:
    """
    Scale src's saturation toward ref's.

    Params:
      src: the CDL param dict to correct.
      ref: the "hero"/reference CDL param dict to match toward.
      clamp_scale: (lo, hi) bound on the multiplicative saturation
        correction (mirrors saturation-match.mjs's default [0.5, 2]).

    Computes ``scale = clamp(ref.sat / src.sat, *clamp_scale)`` and returns
    src with ``sat`` replaced by ``src.sat * scale`` (``slope``/``offset``/
    ``power`` pass through unchanged). When unclamped this lands exactly on
    ``ref.sat`` — a full match, which is idempotent when src == ref
    (scale = 1, no change).

    Degenerate handling: if src.sat is ~zero, no finite ratio can be formed
    (0 saturation can't be scaled up); the input is returned unchanged with
    ``clamped: True`` and a warning, rather than dividing by zero.

    Returns a CDL param dict plus ``scale`` (the multiplier applied),
    ``clamped``, ``correction_pct``, ``warnings``, and ``verified: False``.
    """
    s = validate_cdl_params(src)
    r = validate_cdl_params(ref)
    lo, hi = clamp_scale
    warnings: list[str] = []

    if s["sat"] < _EPS:
        warnings.append(
            "saturation_match: src saturation ~0 — cannot compute a finite match ratio, returned unchanged"
        )
        return {
            **s,
            "scale": 1.0,
            "clamped": True,
            "correction_pct": 0.0,
            "warnings": warnings,
            "verified": False,
        }

    raw_scale = r["sat"] / s["sat"]
    scale = _clamp(raw_scale, lo, hi)
    clamped = scale != raw_scale
    if clamped:
        warnings.append(f"saturation_match: scale clamped to {clamp_scale}")

    return {
        "slope": list(s["slope"]),
        "offset": list(s["offset"]),
        "power": list(s["power"]),
        "sat": s["sat"] * scale,
        "scale": scale,
        "clamped": clamped,
        "correction_pct": round(100.0 * abs(scale - 1.0), 2),
        "warnings": warnings,
        "verified": False,
    }


def black_balance(
    src: Mapping[str, Any],
    *,
    cast_threshold: float = 0.004,
    clamp_offset: tuple[float, float] = (-0.2, 0.2),
) -> dict[str, Any]:
    """
    Neutralize a per-channel shadow (black point) cast, offset-only.

    Params:
      src: the CDL param dict to correct.
      cast_threshold: minimum spread (max - min implicit black point across
        channels) before a correction is attempted; below this the shadows
        are already neutral (mirrors black-balance.mjs's default 0.004,
        ~1/255). No hero/reference is needed — src is self-corrected.
      clamp_offset: (lo, hi) bound on the per-channel additive correction
        (mirrors black-balance.mjs's default [-0.2, 0.2]).

    Aligns every channel's implicit black point (``src.offset``, see the
    module docstring) DOWN to the lowest channel's black point — this
    removes a shadow colour cast without lifting or crushing the shadows
    (``slope``/``power``/``sat`` are never touched, only ``offset``). If the
    spread across channels is already below `cast_threshold`, returns src
    unchanged (skip-not-fake) with ``already_neutral: True``.

    Returns a CDL param dict plus ``shift`` (the per-channel offset delta
    applied), ``already_neutral``, ``clamped``, ``cast_spread_pct``,
    ``warnings``, and ``verified: False``.
    """
    s = validate_cdl_params(src)
    olo, ohi = clamp_offset

    black_points = list(s["offset"])
    target = min(black_points)
    spread = max(black_points) - target

    if spread < cast_threshold:
        return {
            **s,
            "shift": [0.0, 0.0, 0.0],
            "already_neutral": True,
            "clamped": False,
            "cast_spread_pct": round(100.0 * spread, 2),
            "warnings": [],
            "verified": False,
        }

    shift = [0.0, 0.0, 0.0]
    new_offset = [0.0, 0.0, 0.0]
    clamped = False
    warnings: list[str] = []

    for i in range(3):
        raw_shift = target - black_points[i]
        sh = _clamp(raw_shift, olo, ohi)
        if sh != raw_shift:
            clamped = True
            warnings.append(
                f"black_balance: channel {i} shift clamped to {clamp_offset} — shadow cast not fully removed"
            )
        shift[i] = sh
        new_offset[i] = black_points[i] + sh

    return {
        "slope": list(s["slope"]),
        "offset": new_offset,
        "power": list(s["power"]),
        "sat": s["sat"],
        "shift": shift,
        "already_neutral": False,
        "clamped": clamped,
        "cast_spread_pct": round(100.0 * spread, 2),
        "warnings": warnings,
        "verified": False,
    }
