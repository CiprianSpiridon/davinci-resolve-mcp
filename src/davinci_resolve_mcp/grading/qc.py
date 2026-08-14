"""
davinci_resolve_mcp.grading.qc — offline scopes, gamut/broadcast-legal QC,
verify-grade, and match-to-reference orchestration.

Pure math on caller-supplied numbers — no file I/O, no image decoding, no
DaVinci Resolve connection. This codebase's offline layer has no image-decode pipeline, so every function here operates
on plain numeric samples/statistics the caller has already extracted
(sampled RGB pixels, or pre-measured per-channel mean/std) rather than
decoding image files itself.

Four building blocks:

    * :func:`read_scopes`       — waveform/vectorscope-style per-channel +
      luma summary statistics (min/max/mean/std + a coarse histogram) over a
      list of sampled RGB pixels. The scope-read half of a QC pass.
    * :func:`broadcast_legal`   — flags out-of-gamut / illegal levels
      against a named broadcast standard, with full per-sample detail
    * :func:`verify_grade`      — compares an *intended* grade parameter set
      against a *decoded* one (e.g. the output of
      :func:`~davinci_resolve_mcp.formats.drx_codec.decode_fields`) and
      reports a per-param diff plus an overall pass/fail.
    * :func:`match_to_reference` — an affine mean/std color match operating on pre-measured per-channel
      mean/std statistics instead of decoding pixels itself.

All "grade" outputs (``match_to_reference``; the grade side of
``verify_grade``) carry ``"verified": False`` — this is structural QC math,
not calibrated against a live Resolve render (see the offline/advanced tool
set's WRITE-action convention, matching
:mod:`davinci_resolve_mcp.grading.cdl_ops` and
:mod:`davinci_resolve_mcp.grading.white_balance`). An unknown broadcast
standard never raises — :func:`broadcast_legal` (and
:func:`list_broadcast_standards`) return the valid standard list instead so
a caller (or a tool built on this module) can surface it directly rather
than crashing on a typo'd profile name.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

__all__ = [
    "BROADCAST_STANDARDS",
    "list_broadcast_standards",
    "read_scopes",
    "broadcast_legal",
    "verify_grade",
    "match_to_reference",
]

_CHANNELS = ("r", "g", "b")

# BT.709 luma weights — used for the luma scope readout, and for the
# luma-preserve step of match_to_reference.
_LUMA_WEIGHTS = (0.2126, 0.7152, 0.0722)

# Below this magnitude a denominator is treated as "zero" for division-guard
# purposes (a degenerate/near-zero std or intended value).
_EPS = 1e-9

# ---------------------------------------------------------------------------
# Broadcast-legal standards registry.
#
# Each entry's "low"/"high" are fractions of full-scale (0..1) so they are
# independent of the caller's sample scale (8-bit 0..255, or normalized
# 0..1 — see the `scale` parameter on broadcast_legal). Values mirror
# the conventional default 8-bit legal range (16-235/255).
# ---------------------------------------------------------------------------
BROADCAST_STANDARDS: dict[str, dict[str, Any]] = {
    "rec709": {
        "low": 16 / 255,
        "high": 235 / 255,
        "label": "ITU-R BT.709 broadcast-legal video range (8-bit 16-235)",
    },
    "rec2020": {
        "low": 16 / 255,
        "high": 235 / 255,
        "label": "ITU-R BT.2020 broadcast-legal video range (digital legal "
        "footroom/headroom mirrors BT.709: 8-bit 16-235)",
    },
    "full_range": {
        "low": 0.0,
        "high": 1.0,
        "label": "Full/extended (data) range — no footroom/headroom reserved; "
        "only hard 0/full-scale clipping is illegal",
    },
}


def list_broadcast_standards() -> list[str]:
    """Return the sorted list of valid :func:`broadcast_legal` standard names."""
    return sorted(BROADCAST_STANDARDS)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _luma(rgb: tuple[float, float, float]) -> float:
    wr, wg, wb = _LUMA_WEIGHTS
    r, g, b = rgb
    return wr * r + wg * g + wb * b


def _as_channels(rgb: Any, *, name: str) -> dict[str, float]:
    """Normalize an RGB-ish sample ({r,g,b}-mapping or [r,g,b] sequence) to
    floats. Extra keys (e.g. a caller-supplied "id") on a mapping are ignored.
    Raises ValueError on malformed input — a *structural* error, distinct
    from an unknown-standard name, which never raises (see module docstring).
    """
    if rgb is None:
        raise ValueError(f"qc: '{name}' is required")
    if isinstance(rgb, Mapping):
        missing = [ch for ch in _CHANNELS if ch not in rgb]
        if missing:
            raise ValueError(f"qc: '{name}' missing channel(s) {missing}")
        values = [rgb["r"], rgb["g"], rgb["b"]]
    elif isinstance(rgb, (str, bytes)) or not isinstance(rgb, Sequence):
        raise ValueError(
            f"qc: '{name}' must be a 3-element [r,g,b] sequence or {{r,g,b}} mapping, got {rgb!r}"
        )
    else:
        values = list(rgb)
        if len(values) != 3:
            raise ValueError(f"qc: '{name}' must have exactly 3 channels (R,G,B), got {len(values)}")

    out: dict[str, float] = {}
    for ch, raw in zip(_CHANNELS, values):
        try:
            v = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"qc: '{name}.{ch}' is not numeric: {raw!r}") from None
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError(f"qc: '{name}.{ch}' must be finite, got {raw!r}")
        out[ch] = v
    return out


def _sample_id(sample: Any, index: int) -> Any:
    if isinstance(sample, Mapping) and "id" in sample:
        return sample["id"]
    return index


# ---------------------------------------------------------------------------
# Scope reads
# ---------------------------------------------------------------------------
def read_scopes(
    samples: Sequence[Any],
    *,
    scale: float = 255.0,
    buckets: int = 10,
) -> dict[str, Any]:
    """
    Waveform/vectorscope-style summary statistics over sampled pixels.

    Params:
      samples: a non-empty sequence of RGB samples, each ``{"r":..,"g":..,
        "b":..}`` (extra keys such as ``"id"`` are ignored) or ``[r, g, b]``.
        Typically a sparse set of pixels/patches sampled from a rendered
        frame by the caller (this module never decodes images itself).
      scale: the samples' full-scale value (255.0 for 8-bit-style samples,
        the default; pass 1.0 for normalized float samples).
      buckets: number of equal-width histogram buckets per channel, spanning
        ``0..scale``.

    Returns a dict:
      {
        "n_total": <sample count>,
        "scale": <scale>,
        "channels": {
          "r"/"g"/"b": {
            "min", "max", "mean", "std",
            "mean_pct": <mean as a % of scale>,
            "histogram": [<bucket counts>, ...],
          }, ...
        },
        "luma": {"min", "max", "mean", "mean_pct", "std"},  # BT.709-weighted
      }

    Raises ValueError if `samples` is empty, `scale` <= 0, `buckets` < 1, or
    a sample is malformed (see :func:`_as_channels`) — a structural input
    error, distinct from an unknown broadcast-legal standard (which never
    raises; see :func:`broadcast_legal`).
    """
    if not samples:
        raise ValueError("qc: 'samples' must be a non-empty sequence")
    if scale <= 0:
        raise ValueError(f"qc: 'scale' must be > 0, got {scale!r}")
    if buckets < 1:
        raise ValueError(f"qc: 'buckets' must be >= 1, got {buckets!r}")

    parsed = [_as_channels(s, name=f"samples[{i}]") for i, s in enumerate(samples)]
    n = len(parsed)

    channels: dict[str, Any] = {}
    for ch in _CHANNELS:
        vals = [p[ch] for p in parsed]
        mean = sum(vals) / n
        variance = sum((v - mean) ** 2 for v in vals) / n
        hist = [0] * buckets
        for v in vals:
            idx = int(_clamp(v, 0.0, scale) / scale * buckets)
            if idx >= buckets:
                idx = buckets - 1
            hist[idx] += 1
        channels[ch] = {
            "min": min(vals),
            "max": max(vals),
            "mean": round(mean, 4),
            "std": round(variance**0.5, 4),
            "mean_pct": round(100.0 * mean / scale, 2),
            "histogram": hist,
        }

    luma_vals = [_luma((p["r"], p["g"], p["b"])) for p in parsed]
    luma_mean = sum(luma_vals) / n
    luma_var = sum((v - luma_mean) ** 2 for v in luma_vals) / n

    return {
        "n_total": n,
        "scale": scale,
        "channels": channels,
        "luma": {
            "min": min(luma_vals),
            "max": max(luma_vals),
            "mean": round(luma_mean, 4),
            "mean_pct": round(100.0 * luma_mean / scale, 2),
            "std": round(luma_var**0.5, 4),
        },
    }


# ---------------------------------------------------------------------------
# Broadcast-legal / clipping QC
# ---------------------------------------------------------------------------
def broadcast_legal(
    samples: Sequence[Any],
    standard: str = "rec709",
    *,
    max_illegal_pct: float = 1.0,
    scale: float = 255.0,
) -> dict[str, Any]:
    """
    Flag out-of-gamut / illegal levels in sampled pixels against a named
    broadcast-legal standard (see :data:`BROADCAST_STANDARDS`).

    Params:
      samples: a non-empty sequence of RGB samples, each ``{"r":..,"g":..,
        "b":..}`` (an optional ``"id"`` key is echoed back per-sample) or
        ``[r, g, b]``.
      standard: one of :func:`list_broadcast_standards` (default "rec709",
        8-bit 16-235).
      max_illegal_pct: tolerance — the % of samples allowed to sit outside
        legal range before the overall result fails (default 1.0%).
      scale: the samples' full-scale value (255.0 default; pass 1.0 for
        normalized float samples). The standard's legal low/high are
        fractions of this scale.

    Returns, on an unknown `standard`, a dict ``{"error": ..., "valid_standards":
    [...]}`` — this function NEVER raises for a bad standard name, so a tool
    built on it can surface the valid list to the caller/LLM instead of
    crashing. On a valid standard, returns:
      {
        "standard": <name>, "low": <legal low, in `scale` units>,
        "high": <legal high>, "scale": <scale>, "max_illegal_pct": <tol>,
        "n_total": <sample count>,
        "illegal_pct": <% of samples with >=1 out-of-legal channel>,
        "clipped_pct": <% of samples with >=1 hard-clipped (<=0 or >=scale) channel>,
        "channels": {
          "r"/"g"/"b": {"min","max","below_pct","above_pct","crushed_pct","blown_pct"}
        },
        "samples": [
          {"index", "id", "rgb", "illegal", "illegal_channels", "clipped",
           "clipped_channels"}, ...
        ],
        "pass": <illegal_pct <= max_illegal_pct>,
        "warnings": [...],
      }

    A frame whose samples all sit within ``[low, high]`` has ``illegal_pct
    == 0.0`` and always passes. Raises ValueError only for malformed sample
    data or a non-positive `scale`/`max_illegal_pct` — structural input
    errors, distinct from the never-raising unknown-standard path above.
    """
    if standard not in BROADCAST_STANDARDS:
        return {
            "error": f"unknown QC standard '{standard}'",
            "valid_standards": list_broadcast_standards(),
        }
    if not samples:
        raise ValueError("qc: 'samples' must be a non-empty sequence")
    if scale <= 0:
        raise ValueError(f"qc: 'scale' must be > 0, got {scale!r}")
    if max_illegal_pct < 0:
        raise ValueError(f"qc: 'max_illegal_pct' must be >= 0, got {max_illegal_pct!r}")

    spec = BROADCAST_STANDARDS[standard]
    low = spec["low"] * scale
    high = spec["high"] * scale

    n = len(samples)
    acc = {ch: {"min": scale, "max": 0.0, "below": 0, "above": 0, "crushed": 0, "blown": 0} for ch in _CHANNELS}
    sample_rows: list[dict[str, Any]] = []
    illegal_count = 0
    clipped_count = 0

    for i, raw in enumerate(samples):
        rgb = _as_channels(raw, name=f"samples[{i}]")
        illegal_channels: list[str] = []
        clipped_channels: list[str] = []
        for ch in _CHANNELS:
            v = rgb[ch]
            a = acc[ch]
            if v < a["min"]:
                a["min"] = v
            if v > a["max"]:
                a["max"] = v
            if v < low:
                a["below"] += 1
                illegal_channels.append(ch)
            elif v > high:
                a["above"] += 1
                illegal_channels.append(ch)
            if v <= 0:
                a["crushed"] += 1
                clipped_channels.append(ch)
            elif v >= scale:
                a["blown"] += 1
                clipped_channels.append(ch)
        is_illegal = bool(illegal_channels)
        is_clipped = bool(clipped_channels)
        if is_illegal:
            illegal_count += 1
        if is_clipped:
            clipped_count += 1
        sample_rows.append({
            "index": i,
            "id": _sample_id(raw, i),
            "rgb": rgb,
            "illegal": is_illegal,
            "illegal_channels": illegal_channels,
            "clipped": is_clipped,
            "clipped_channels": clipped_channels,
        })

    def pct(count: int) -> float:
        return round(100.0 * count / n, 3) if n else 0.0

    channels_report = {
        ch: {
            "min": acc[ch]["min"],
            "max": acc[ch]["max"],
            "below_pct": pct(acc[ch]["below"]),
            "above_pct": pct(acc[ch]["above"]),
            "crushed_pct": pct(acc[ch]["crushed"]),
            "blown_pct": pct(acc[ch]["blown"]),
        }
        for ch in _CHANNELS
    }

    illegal_pct = pct(illegal_count)
    clipped_pct = pct(clipped_count)
    passed = illegal_pct <= max_illegal_pct

    warnings: list[str] = []
    if not passed:
        warnings.append(
            f"broadcast_legal: {illegal_pct}% of samples out-of-legal (>{max_illegal_pct}% tolerance) "
            f"[{standard}: low {low}/high {high}] — review before broadcast-legal delivery."
        )

    return {
        "standard": standard,
        "label": spec["label"],
        "low": low,
        "high": high,
        "scale": scale,
        "max_illegal_pct": max_illegal_pct,
        "n_total": n,
        "illegal_pct": illegal_pct,
        "clipped_pct": clipped_pct,
        "channels": channels_report,
        "samples": sample_rows,
        "pass": passed,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Verify-grade: intended vs decoded parameter comparison
# ---------------------------------------------------------------------------
def _flatten_params(value: Any, *, label: str) -> dict[str, float]:
    """
    Normalize a grade-parameter set into a flat ``{name: value}`` dict.

    Accepts:
      * a plain mapping of ``{name: value}`` (non-numeric / underscore-
        prefixed keys — e.g. the ``_body_hex``/``_blob_hex`` bookkeeping
        fields on a :func:`~davinci_resolve_mcp.formats.drx_codec.decode_fields`
        result — are skipped),
      * a :func:`~davinci_resolve_mcp.formats.drx_codec.decode_fields`-shaped
        dict (has a ``"named"`` mapping — used directly), or
      * a ``"params"``/bare list of ``{"name":.., "value":..}`` entries (the
        ``decode_fields()["params"]`` shape, or a hand-built list).
    """
    if isinstance(value, Mapping):
        if "named" in value and isinstance(value["named"], Mapping):
            return {str(k): float(v) for k, v in value["named"].items() if isinstance(v, (int, float))}
        if "params" in value and isinstance(value["params"], list):
            value = value["params"]
        else:
            out: dict[str, float] = {}
            for k, v in value.items():
                if isinstance(k, str) and k.startswith("_"):
                    continue
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    out[k] = float(v)
            return out
    if isinstance(value, list):
        out = {}
        for entry in value:
            if isinstance(entry, Mapping) and "name" in entry and "value" in entry:
                out[str(entry["name"])] = float(entry["value"])
        return out
    raise ValueError(
        f"qc: '{label}' must be a {{name: value}} mapping, a decode_fields()-shaped dict, "
        f"or a list of {{'name','value'}} entries, got {type(value).__name__}"
    )


def verify_grade(
    intended: Any,
    decoded: Any,
    *,
    tolerance: float = 1e-3,
) -> dict[str, Any]:
    """
    Compare an INTENDED grade parameter set against a DECODED one (e.g. the
    result of round-tripping through
    :func:`~davinci_resolve_mcp.formats.drx_codec.decode_fields` /
    :func:`~davinci_resolve_mcp.formats.drx_codec.encode_fields`) and report
    a per-parameter diff plus an overall pass/fail.

    Params:
      intended: the grade parameters the caller meant to set — a
        ``{name: value}`` mapping, a ``decode_fields()``-shaped dict (uses
        its ``"named"`` map), or a list of ``{"name","value"}`` entries.
      decoded: the grade parameters actually decoded back out of a written
        ``.drx`` (same accepted shapes as `intended`).
      tolerance: maximum allowed absolute difference (``decoded - intended``)
        for a parameter to be considered matching (default 1e-3, comfortably
        above float32 round-trip noise).

    Returns:
      {
        "params": {
          <name>: {"intended", "decoded", "diff", "pct_diff", "within_tolerance"}, ...
        },
        "missing": [<names present in `intended` but absent from `decoded`>],
        "extra_params": [<names present in `decoded` but not in `intended`>],
        "tolerance": <tolerance>,
        "pass": <True iff every intended param is present in `decoded` and
                  within `tolerance`>,
        "verified": False,
      }

    A parameter missing from `decoded` is recorded with ``"decoded": None``,
    ``"within_tolerance": False``, and fails the overall result — it is
    listed in both ``"params"`` and ``"missing"``. Extra parameters present
    only in `decoded` do not affect ``"pass"`` (they're informational — the
    decoded blob commonly carries default/untouched params `intended` never
    mentioned). ``"verified"`` is always False: this is a structural
    comparison of decoded numbers, not a calibration check against a live
    Resolve render (matching the offline/advanced tool set's WRITE-action
    convention). Raises ValueError only if `intended`/`decoded` are not one
    of the accepted shapes.
    """
    intended_flat = _flatten_params(intended, label="intended")
    decoded_flat = _flatten_params(decoded, label="decoded")

    params: dict[str, Any] = {}
    missing: list[str] = []
    overall_pass = True

    for name, iv in intended_flat.items():
        if name not in decoded_flat:
            missing.append(name)
            overall_pass = False
            params[name] = {
                "intended": iv,
                "decoded": None,
                "diff": None,
                "pct_diff": None,
                "within_tolerance": False,
            }
            continue
        dv = decoded_flat[name]
        diff = dv - iv
        pct_diff = round(100.0 * diff / iv, 4) if abs(iv) > _EPS else None
        within = abs(diff) <= tolerance
        if not within:
            overall_pass = False
        params[name] = {
            "intended": iv,
            "decoded": dv,
            "diff": round(diff, 6),
            "pct_diff": pct_diff,
            "within_tolerance": within,
        }

    extra_params = sorted(set(decoded_flat) - set(intended_flat))

    return {
        "params": params,
        "missing": missing,
        "extra_params": extra_params,
        "tolerance": tolerance,
        "pass": overall_pass,
        "verified": False,
    }


# ---------------------------------------------------------------------------
# match_to_reference: affine mean/std color match orchestration
# ---------------------------------------------------------------------------
def _mean_std(value: Any, *, name: str) -> dict[str, dict[str, float]]:
    if not isinstance(value, Mapping) or "mean" not in value or "std" not in value:
        raise ValueError(
            f"qc: '{name}' must be a mapping with 'mean' and 'std' RGB entries, got {value!r}"
        )
    return {
        "mean": _as_channels(value["mean"], name=f"{name}.mean"),
        "std": _as_channels(value["std"], name=f"{name}.std"),
    }


def match_to_reference(
    target: Any,
    reference: Any,
    *,
    luma_preserve: bool = True,
    clamp_gain: tuple[float, float] = (0.5, 2.0),
    clamp_offset: tuple[float, float] = (-0.5, 0.5),
    warn_threshold: float = 0.35,
) -> dict[str, Any]:
    """
    Affine mean/std ("Reinhard-lite") color match: move `target`'s sampled
    statistics toward `reference`'s, as a gain+offset CDL. It takes
    already-measured per-channel mean/std (this module never samples pixels
    itself — see :func:`read_scopes` / the caller's own frame sampling for
    that half of the pipeline).

    Params:
      target / reference: ``{"mean": {"r":..,"g":..,"b":..}, "std": {...}}``,
        each channel normalized 0..1 (a target patch/frame to correct and the
        hero reference to match toward).
      luma_preserve: when True (default), the
        per-channel affine is followed by a uniform offset shift so the
        transformed luma equals `target`'s own (BT.709-weighted) mean luma —
        a chroma/look match, not an exposure move.
      clamp_gain: (lo, hi) bound on the per-channel multiplicative gain
        (``std_ref / std_target``); default ``[0.5, 2]``.
      clamp_offset: (lo, hi) bound on the per-channel additive offset;
        default ``[-0.5, 0.5]``.
      warn_threshold: correction magnitude (max over channels of
        ``max(|gain-1|, |offset|)``) above which a "large correction" warning
        is added — default 0.35.

    Returns:
      {
        "gain": {"r","g","b"}, "offset": {"r","g","b"},
        "cdl": {"slope":[r,g,b], "offset":[r,g,b], "power":[1,1,1], "sat":1.0},
        "correction_pct": <max(|gain-1|,|offset|) across channels, as a %>,
        "luma_preserve": <bool>,
        "clamped": <True iff any channel's gain/offset hit a clamp bound>,
        "warnings": [...],
        "verified": False,
      }

    A target whose mean/std already equal the reference's returns
    gain == {1,1,1}, offset == {0,0,0} (identity CDL). A near-zero
    `target` std (degenerate/flat patch) is floored rather than divided
    through — the resulting (unreliable) gain is then clamped like any
    other, and the affected channel(s) are noted in ``warnings``.
    ``"verified"`` is always False — structural grade math on caller-supplied
    statistics, not calibrated against a live Resolve render (matching
    :mod:`davinci_resolve_mcp.grading.cdl_ops` /
    :mod:`davinci_resolve_mcp.grading.white_balance`). Raises ValueError for
    malformed `target`/`reference` or invalid clamp bounds.
    """
    if clamp_gain[0] <= 0 or clamp_gain[1] < clamp_gain[0]:
        raise ValueError(f"qc: clamp_gain must be a valid (min>0, max>=min) pair, got {clamp_gain!r}")
    if clamp_offset[1] < clamp_offset[0]:
        raise ValueError(f"qc: clamp_offset must be a valid (min<=max) pair, got {clamp_offset!r}")

    tgt = _mean_std(target, name="target")
    ref = _mean_std(reference, name="reference")
    glo, ghi = clamp_gain
    olo, ohi = clamp_offset
    warnings: list[str] = []
    clamped = False

    gain: dict[str, float] = {}
    offset: dict[str, float] = {}
    for ch in _CHANNELS:
        std_t = tgt["std"][ch]
        denom = std_t if abs(std_t) > _EPS else _EPS
        raw_gain = ref["std"][ch] / denom
        g = _clamp(raw_gain, glo, ghi)
        if g != raw_gain:
            clamped = True
            warnings.append(f"match_to_reference: channel '{ch}' gain clamped to {clamp_gain}")
        raw_offset = ref["mean"][ch] - g * tgt["mean"][ch]
        o = _clamp(raw_offset, olo, ohi)
        if o != raw_offset:
            clamped = True
            warnings.append(f"match_to_reference: channel '{ch}' offset clamped to {clamp_offset}")
        gain[ch] = g
        offset[ch] = o

    if luma_preserve:
        tgt_luma = _luma((tgt["mean"]["r"], tgt["mean"]["g"], tgt["mean"]["b"]))
        out_luma = sum(
            w * (gain[ch] * tgt["mean"][ch] + offset[ch]) for ch, w in zip(_CHANNELS, _LUMA_WEIGHTS)
        )
        delta = tgt_luma - out_luma
        for ch in _CHANNELS:
            raw = offset[ch] + delta
            adjusted = _clamp(raw, olo, ohi)
            if adjusted != raw:
                clamped = True
                warnings.append(
                    f"match_to_reference: channel '{ch}' luma-preserve offset clamped to {clamp_offset}"
                )
            offset[ch] = adjusted

    correction = max(max(abs(gain[ch] - 1.0), abs(offset[ch])) for ch in _CHANNELS)
    correction_pct = round(100.0 * correction, 2)
    if correction > warn_threshold:
        warnings.append(
            f"match_to_reference: large correction {correction_pct}% — target/reference content may "
            "differ too much; review (a trim, not a cross-IDT reconciler)."
        )

    return {
        "gain": gain,
        "offset": offset,
        "cdl": {
            "slope": [gain["r"], gain["g"], gain["b"]],
            "offset": [offset["r"], offset["g"], offset["b"]],
            "power": [1.0, 1.0, 1.0],
            "sat": 1.0,
        },
        "correction_pct": correction_pct,
        "luma_preserve": luma_preserve,
        "clamped": clamped,
        "warnings": warnings,
        "verified": False,
    }
