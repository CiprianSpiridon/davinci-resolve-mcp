"""
davinci_resolve_mcp.grading.skin_match — within/cross-camera skin-tone match
(v2 "skin-line" metric).

The matcher gates pixels with the classic Kovac RGB rule and averages those
that pass. This codebase's offline layer has no image-decode pipeline (see
:mod:`davinci_resolve_mcp.grading.cdl_ops`, :mod:`davinci_resolve_mcp.grading.white_balance`),
so this module instead accepts a caller-supplied list of raw RGB pixel
*samples* — a sparse grid, a sampled skin-tone region, or a full raster
flattened to ``[{r,g,b}, ...]`` — and performs the same gate + mean in pure
Python. Frame extraction/sampling and grade APPLY remain the caller's job.

Two match metrics are available:

  * ``"mean"`` (legacy v1) — match the source clip's skin-gated mean RGB
    exactly to the reference's skin-gated mean RGB. Simple, but drags a
    darker face's brightness up/down to the reference's — the
    "calibrated-for-one-skin-tone" trap the v2 metric was built to avoid.
  * ``"chroma"`` (v2, default, "skin-line preserving") — match the source's
    skin CHROMATICITY (each channel normalized by luma) to the reference's,
    while keeping the source's OWN luma. This matches skin hue/chroma
    direction across cameras/framings without altering how bright the face
    already reads.

COLOR-SPACE CAVEAT: the skin gate assumes
display-referred samples (Rec.709 / sRGB). Log samples (SLog3 etc.) are flat
and desaturated, so almost nothing passes the gate — :func:`skin_stats`
raises a clear error rather than silently averaging zero pixels into NaN.

Every function here is pure (no I/O, no Resolve). :func:`skin_match` returns
a gain-only ASC CDL param dict (the shape consumed by
:func:`davinci_resolve_mcp.formats.cdl.export_cdl`) plus ``"verified": False``
— structural grade math, not calibrated against a live Resolve render (see
the offline/advanced tool set's WRITE-action convention).
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

__all__ = ["is_skin", "skin_stats", "skin_match"]

_CHANNELS = ("r", "g", "b")

# BT.709 luma weights (same convention used throughout this codebase's
# grading layer, e.g. cdl_ops._LUMA_WEIGHTS).
_LUMA_WEIGHTS: dict[str, float] = {"r": 0.2126, "g": 0.7152, "b": 0.0722}

# Canonical vectorscope skin-line angle (Rec.709 full-range chroma,
# atan2(cr, cb)).
_SKIN_LINE_DEG = 123.0

# Classic Kovac RGB skin-rule defaults (8-bit-scale channels), literature
# values, tunable via `skin_opts`.
_DEFAULT_SKIN_OPTS: dict[str, float] = {
    "min_r": 95.0,
    "min_g": 40.0,
    "min_b": 20.0,
    "min_spread": 15.0,
    "min_rg": 15.0,
}

_EPS = 1e-9


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _safe_divisor(value: float) -> float:
    """Push a near-zero denominator away from zero so a division stays
    finite; the result then always passes through `_clamp`, which is what
    actually bounds an otherwise-unreliable (near-black) gain."""
    if abs(value) < _EPS:
        return _EPS if value >= 0 else -_EPS
    return value


def _parse_rgb(value: Any, *, name: str) -> tuple[float, float, float]:
    if isinstance(value, Mapping):
        missing = [ch for ch in _CHANNELS if ch not in value]
        if missing:
            raise ValueError(f"skin_match: a '{name}' sample is missing channel(s) {missing}")
        raw: Sequence[Any] = (value["r"], value["g"], value["b"])
    elif isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(
            f"skin_match: a '{name}' sample must be a 3-element [r,g,b] sequence or "
            f"{{r,g,b}} mapping, got {value!r}"
        )
    else:
        raw = tuple(value)
        if len(raw) != 3:
            raise ValueError(f"skin_match: a '{name}' sample must have exactly 3 channels (R,G,B), got {len(raw)}")

    out: list[float] = []
    for ch, v in zip(_CHANNELS, raw):
        try:
            fv = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"skin_match: '{name}' sample channel '{ch}' is not numeric: {v!r}") from None
        if fv != fv or fv in (float("inf"), float("-inf")):
            raise ValueError(f"skin_match: '{name}' sample channel '{ch}' must be finite, got {v!r}")
        out.append(fv)
    return (out[0], out[1], out[2])


def _parse_samples(samples: Any, *, name: str) -> list[tuple[float, float, float]]:
    if samples is None or isinstance(samples, (str, bytes)) or not isinstance(samples, Sequence):
        raise ValueError(f"skin_match: '{name}' must be a non-empty sequence of RGB samples, got {samples!r}")
    if len(samples) == 0:
        raise ValueError(f"skin_match: '{name}' is empty — at least one RGB sample is required")
    return [_parse_rgb(s, name=name) for s in samples]


def _mean_rgb(samples: Sequence[tuple[float, float, float]]) -> dict[str, float]:
    n = len(samples)
    sums = [0.0, 0.0, 0.0]
    for s in samples:
        sums[0] += s[0]
        sums[1] += s[1]
        sums[2] += s[2]
    return {ch: sums[i] / n for i, ch in enumerate(_CHANNELS)}


def _luma(rgb: Mapping[str, float]) -> float:
    return sum(_LUMA_WEIGHTS[ch] * rgb[ch] for ch in _CHANNELS)


def _skin_line_deviation_deg(rgb: Mapping[str, float]) -> float:
    """Degrees off the canonical ~123° vectorscope skin line. Large
    deviations flag Kovac-rule false positives (fire/wood/orange content
    reads as "skin" under the plain RGB gate but sits far off the line)."""
    y = _luma(rgb)
    cb = (rgb["b"] - y) / 1.8556
    cr = (rgb["r"] - y) / 1.5748
    angle = math.degrees(math.atan2(cr, cb))
    return angle - _SKIN_LINE_DEG


def _chromaticity(rgb: Mapping[str, float]) -> dict[str, float]:
    """Per-channel value normalized by luma — the direction-only ("skin
    line") component of a color, independent of how bright it is."""
    y = _luma(rgb)
    y = y if abs(y) >= _EPS else _EPS
    return {ch: rgb[ch] / y for ch in _CHANNELS}


def _distance(a: Mapping[str, float], b: Mapping[str, float]) -> float:
    return math.sqrt(sum((a[ch] - b[ch]) ** 2 for ch in _CHANNELS))


def _gain_cdl(gain: Mapping[str, float]) -> dict[str, Any]:
    return {
        "slope": [gain["r"], gain["g"], gain["b"]],
        "offset": [0.0, 0.0, 0.0],
        "power": [1.0, 1.0, 1.0],
        "sat": 1.0,
    }


def is_skin(r: float, g: float, b: float, **opts: float) -> bool:
    """
    Classic Kovac RGB skin-tone rule for uniform daylight, on 8-bit-scale
    channels. Conservative by design: better to gate out a borderline pixel
    than to admit a background pixel that drags the skin mean off target.
    Params:
      r, g, b: channel values (0..255 scale by default; the rule's
        thresholds assume that scale — pass matching `min_*`/`min_spread`/
        `min_rg` overrides via `opts` for a different scale).
      opts: any of `min_r`, `min_g`, `min_b`, `min_spread`, `min_rg` to
        override the literature defaults.
    """
    min_r = opts.get("min_r", _DEFAULT_SKIN_OPTS["min_r"])
    min_g = opts.get("min_g", _DEFAULT_SKIN_OPTS["min_g"])
    min_b = opts.get("min_b", _DEFAULT_SKIN_OPTS["min_b"])
    min_spread = opts.get("min_spread", _DEFAULT_SKIN_OPTS["min_spread"])
    min_rg = opts.get("min_rg", _DEFAULT_SKIN_OPTS["min_rg"])
    mx = max(r, g, b)
    mn = min(r, g, b)
    return (
        r > min_r
        and g > min_g
        and b > min_b
        and (mx - mn) > min_spread
        and abs(r - g) > min_rg
        and r > g
        and r > b
    )


def skin_stats(
    samples: Any,
    *,
    name: str = "samples",
    skin_opts: Mapping[str, float] | None = None,
    min_skin_frac: float = 0.01,
) -> dict[str, Any]:
    """
    Gate a list of raw RGB pixel samples to skin-tone pixels (the Kovac rule
    via :func:`is_skin`) and return skin-only statistics.

    Params:
      samples: non-empty sequence of RGB samples, each ``{"r":..,"g":..,"b":..}``
        or ``[r,g,b]``/``(r,g,b)``, on the same 0..255-style scale `is_skin`
        expects.
      name: label used in error messages (e.g. "src_samples").
      skin_opts: overrides forwarded to :func:`is_skin`.
      min_skin_frac: minimum fraction of samples that must pass the skin
        gate to trust the mean (default 1%) — below this floor a
        `ValueError` is raised rather than returning an unreliable mean.

    Returns:
      {
        "mean": {"r":.., "g":.., "b":..},   # mean of the skin-gated samples
        "skin_frac": float,                  # n_skin / n_total, 0..1
        "n_skin": int,
        "n_total": int,
        "hue_deviation_deg": float,          # degrees off the ~123° skin line
      }

    Raises `ValueError` (never returns NaN) for an empty `samples` sequence,
    a malformed sample, zero pixels passing the skin gate, or a skin
    fraction below `min_skin_frac`.
    """
    parsed = _parse_samples(samples, name=name)
    opts = dict(skin_opts or {})
    skin_pixels = [s for s in parsed if is_skin(*s, **opts)]
    n_total = len(parsed)
    n_skin = len(skin_pixels)
    skin_frac = n_skin / n_total if n_total else 0.0

    if n_skin == 0:
        raise ValueError(
            f"skin_match: '{name}' has no skin-tone pixels ({n_total} sample(s) checked) — "
            "samples may be LOG/scene-referred (the skin gate assumes display-referred "
            "Rec.709/sRGB), or the region genuinely contains no skin."
        )
    if skin_frac < min_skin_frac:
        raise ValueError(
            f"skin_match: '{name}' skin fraction {100 * skin_frac:.2f}% is below the "
            f"{100 * min_skin_frac:.2f}% floor ({n_skin}/{n_total} pixels) — too few skin "
            "pixels to trust the mean."
        )

    mean = _mean_rgb(skin_pixels)
    return {
        "mean": mean,
        "skin_frac": skin_frac,
        "n_skin": n_skin,
        "n_total": n_total,
        "hue_deviation_deg": _skin_line_deviation_deg(mean),
    }


def skin_match(
    src_samples: Any,
    ref_samples: Any,
    *,
    metric: str = "chroma",
    clamp_gain: tuple[float, float] = (0.5, 2.0),
    min_skin_frac: float = 0.01,
    max_correction_warn: float = 0.25,
    skin_line_warn_deg: float = 12.0,
    skin_opts: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """
    Match a clip's skin tone to a reference/hero clip's skin tone via a
    per-channel gain, gated to skin-tone pixels on both sides.

    Params:
      src_samples: non-empty sequence of the clip-to-correct's RGB pixel
        samples (0..255-style scale), e.g. a sampled face region.
      ref_samples: non-empty sequence of the reference/hero clip's RGB pixel
        samples, same shape/scale.
      metric: ``"chroma"`` (default, v2 skin-line preserving — matches skin
        hue/chroma to the reference while keeping the source's own luma) or
        ``"mean"`` (legacy v1 — matches the raw skin-gated mean exactly).
      clamp_gain: (min, max) bounds applied to each channel's computed gain.
      min_skin_frac: minimum skin-pixel fraction required on each side
        (forwarded to :func:`skin_stats`) before the mean is trusted.
      max_correction_warn: a computed correction above this fraction
        (default 25%) adds a warning — cross-camera skin deltas run larger
        than same-camera drift, so this is advisory, not a hard cap.
      skin_line_warn_deg: a skin-gated mean sitting more than this many
        degrees off the ~123° vectorscope skin line adds a warning (likely
        a Kovac-rule false positive: fire/wood/orange content, not skin).
      skin_opts: overrides forwarded to :func:`is_skin`.

    Returns:
      {
        "cdl": {"slope":[r,g,b], "offset":[0,0,0], "power":[1,1,1], "sat":1.0},
        "gain": {"r":.., "g":.., "b":..},
        "metric": "chroma" | "mean",
        "src": <skin_stats(src_samples) result>,
        "ref": <skin_stats(ref_samples) result>,
        "distance_metric": "chroma" | "mean_rgb",
        "distance_before": float,   # src-vs-ref distance before correction
        "distance_after": float,    # src-vs-ref distance after correction
        "correction_pct": float,    # max |gain-1| across channels, as %
        "clamped": {"r":bool, "g":bool, "b":bool},
        "warnings": [str, ...],
        "verified": False,
      }

    `distance_after` is guaranteed <= `distance_before` whenever the applied
    gain is not clamp-limited: for `metric="mean"` the corrected mean exactly
    equals the reference mean (distance_after == 0); for `metric="chroma"`
    the corrected CHROMATICITY exactly equals the reference's chromaticity
    (distance_after == 0 in chromaticity space) while the source's own luma
    is preserved.

    Raises `ValueError` — never NaN — for empty/malformed sample sequences,
    an invalid `metric`/`clamp_gain`, or a side with zero/insufficient
    skin-tone pixels (see :func:`skin_stats`).
    """
    if metric not in ("chroma", "mean"):
        raise ValueError(f"skin_match: metric must be 'chroma' or 'mean', got {metric!r}")
    if clamp_gain[0] <= 0 or clamp_gain[1] < clamp_gain[0]:
        raise ValueError(f"skin_match: clamp_gain must be a valid (min>0, max>=min) pair, got {clamp_gain!r}")

    src = skin_stats(src_samples, name="src_samples", skin_opts=skin_opts, min_skin_frac=min_skin_frac)
    ref = skin_stats(ref_samples, name="ref_samples", skin_opts=skin_opts, min_skin_frac=min_skin_frac)
    src_mean, ref_mean = src["mean"], ref["mean"]

    warnings: list[str] = []
    for label, stat in (("src_samples", src), ("ref_samples", ref)):
        dev = stat["hue_deviation_deg"]
        if abs(dev) > skin_line_warn_deg:
            warnings.append(
                f"{label}: skin-gated mean sits {dev:.1f}° off the vectorscope skin line "
                "(≈123°) — likely non-skin content (fire/wood/orange); review "
                "before applying."
            )

    lo, hi = clamp_gain
    if metric == "mean":
        raw_gain = {ch: ref_mean[ch] / _safe_divisor(src_mean[ch]) for ch in _CHANNELS}
    else:  # "chroma" — v2 skin-line preserving: match hue/chroma, keep the clip's own luma
        src_luma = _luma(src_mean)
        ref_luma = _luma(ref_mean)
        src_luma = src_luma if abs(src_luma) >= _EPS else _EPS
        ref_luma = ref_luma if abs(ref_luma) >= _EPS else _EPS
        raw_gain = {
            ch: (ref_mean[ch] / ref_luma) / _safe_divisor(src_mean[ch] / src_luma) for ch in _CHANNELS
        }
    gain = {ch: _clamp(raw_gain[ch], lo, hi) for ch in _CHANNELS}
    clamped = {ch: gain[ch] != raw_gain[ch] for ch in _CHANNELS}

    corrected_mean = {ch: gain[ch] * src_mean[ch] for ch in _CHANNELS}
    if metric == "mean":
        distance_metric = "mean_rgb"
        distance_before = _distance(src_mean, ref_mean)
        distance_after = _distance(corrected_mean, ref_mean)
    else:
        distance_metric = "chroma"
        distance_before = _distance(_chromaticity(src_mean), _chromaticity(ref_mean))
        distance_after = _distance(_chromaticity(corrected_mean), _chromaticity(ref_mean))

    correction_pct = round(100.0 * max(abs(gain[ch] - 1.0) for ch in _CHANNELS), 4)
    if correction_pct / 100.0 > max_correction_warn:
        warnings.append(
            f"max skin correction {correction_pct:.1f}% exceeds the "
            f"{100 * max_correction_warn:.0f}% guard — large for a skin match; check "
            "hero/reference choice or mixed lighting before applying."
        )

    return {
        "cdl": _gain_cdl(gain),
        "gain": gain,
        "metric": metric,
        "src": src,
        "ref": ref,
        "distance_metric": distance_metric,
        "distance_before": distance_before,
        "distance_after": distance_after,
        "correction_pct": correction_pct,
        "clamped": clamped,
        "warnings": warnings,
        "verified": False,
    }
