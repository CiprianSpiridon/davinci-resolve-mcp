"""
davinci_resolve_mcp.grading.white_balance — neutral-patch WB + b-roll match.

Pure math on a sampled patch's mean RGB (e.g. the mean pixel color inside a
normalized rect over a gray card, white wall, or other known-neutral surface
in a still/frame). No file I/O, no image decoding, no DaVinci Resolve
connection — callers (offline tools) are responsible for sampling the patch
color from wherever it lives and handing this module plain numbers.

Two matching modes, mirroring the reference ``white-balance-match.mjs``:

  * ``neutralize`` (:func:`wb_from_patch`) — make a single patch read neutral
    (R == G == B) at its own luma. This is the everyday "gray card in shot,
    correct this clip" case.
  * ``hero`` / b-roll match (:func:`wb_match_to_reference`) — match a patch to
    another clip's ("hero") patch instead of to neutral, so a group of b-roll
    shots carry the same (possibly deliberately non-neutral) key as a
    reference shot.

Both modes compute a per-channel multiplicative gain and package it as a
gain-only ASC CDL param dict (``offset`` all zero, ``power`` all one, ``sat``
one) — the shape consumed by :func:`davinci_resolve_mcp.formats.cdl.export_cdl`.
Gain is clamped to a sane range and a channel sitting at/under the sensor's
black point or at/over its white point is flagged ``clipped`` rather than
being divided through (which would blow up or amplify noise).
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

_CHANNELS = ("r", "g", "b")


def _as_channels(rgb: Any, *, name: str) -> dict[str, float]:
    """Normalize an RGB-ish value ({r,g,b} mapping or [r,g,b]/(r,g,b) sequence) to floats."""
    if rgb is None:
        raise ValueError(f"white_balance: '{name}' is required")
    if isinstance(rgb, Mapping):
        missing = [ch for ch in _CHANNELS if ch not in rgb]
        if missing:
            raise ValueError(f"white_balance: '{name}' missing channel(s) {missing}")
        values = [rgb["r"], rgb["g"], rgb["b"]]
    elif isinstance(rgb, (str, bytes)) or not isinstance(rgb, Sequence):
        raise ValueError(f"white_balance: '{name}' must be a 3-element [r,g,b] sequence or {{r,g,b}} mapping, got {rgb!r}")
    else:
        values = list(rgb)
        if len(values) != 3:
            raise ValueError(f"white_balance: '{name}' must have exactly 3 channels (R,G,B), got {len(values)}")

    out: dict[str, float] = {}
    for ch, raw in zip(_CHANNELS, values):
        try:
            v = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"white_balance: '{name}.{ch}' is not numeric: {raw!r}") from None
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError(f"white_balance: '{name}.{ch}' must be finite, got {raw!r}")
        out[ch] = v
    return out


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _clip_flags(
    patch: Mapping[str, float], *, black_point: float, white_point: float, clip_margin: float
) -> dict[str, bool]:
    lo = black_point + clip_margin
    hi = white_point - clip_margin
    return {ch: (patch[ch] <= lo or patch[ch] >= hi) for ch in _CHANNELS}


def _safe_divisor(value: float, *, black_point: float, white_point: float, clip_margin: float) -> float:
    """
    Guard against divide-by-zero (and division by a near-zero denominator) on
    a clipped-black channel. The result is only ever used as a denominator
    that then gets clamped to `clamp_gain`, so pushing it away from zero just
    prevents an inf/NaN — the subsequent clamp is what actually bounds the
    (unreliable) gain for a clipped channel.
    """
    floor = black_point + max(clip_margin, 1e-6 * max(1.0, white_point - black_point))
    if value <= floor:
        return floor
    return value


def _gain_cdl(gain: Mapping[str, float]) -> dict[str, Any]:
    return {
        "slope": [gain["r"], gain["g"], gain["b"]],
        "offset": [0.0, 0.0, 0.0],
        "power": [1.0, 1.0, 1.0],
        "sat": 1.0,
    }


def _correction_pct(gain: Mapping[str, float]) -> float:
    return round(100.0 * max(abs(gain[ch] - 1.0) for ch in _CHANNELS), 4)


def wb_from_patch(
    rgb: Any,
    *,
    white_point: float = 255.0,
    black_point: float = 0.0,
    clamp_gain: tuple[float, float] = (0.5, 2.0),
    clip_margin: float = 1.0,
) -> dict[str, Any]:
    """
    Neutralize mode: compute a gain-only CDL that makes a sampled patch read
    neutral grey (R == G == B) at its own luma.

    Params:
      rgb: the patch's mean color, as {"r":..,"g":..,"b":..} or [r,g,b].
      white_point / black_point: the sensor/encoding's full-scale range
        (default 0..255 for 8-bit-style samples; pass e.g. 0.0/1.0 for
        normalized float samples).
      clamp_gain: (min, max) bounds applied to each channel's computed gain,
        so a wildly-off patch (or a clipped channel) can't produce an
        extreme correction.
      clip_margin: a channel value within this margin of black_point or
        white_point is treated as clipped: it is excluded from blowing up
        the division (a safe floor is substituted) and flagged in the
        returned "clipped" dict so callers can warn the colourist that the
        patch isn't reliable.

    Returns a dict:
      {
        "cdl": {"slope":[r,g,b], "offset":[0,0,0], "power":[1,1,1], "sat":1.0},
        "gain": {"r":.., "g":.., "b":..},
        "patch": {"r":.., "g":.., "b":..},
        "target": <target luma the patch was neutralized to>,
        "clipped": {"r":bool, "g":bool, "b":bool},
        "correction_pct": <max |gain-1| across channels, as a percentage>,
        "verified": False,
      }

    ``verified`` is always False: this is structural grade math on a caller-
    supplied patch sample, not calibrated against a live Resolve render (see
    the offline/advanced tool set's WRITE-action convention, matching
    :mod:`davinci_resolve_mcp.grading.cdl_ops`).

    An already-neutral patch (R == G == B) returns gain == {1,1,1} (identity
    CDL). Never raises for a clipped patch (0 or white_point in any channel)
    — clamp_gain and the clip floor keep the division finite; the caller
    should inspect "clipped" to decide whether to trust the result.
    """
    if clamp_gain[0] <= 0 or clamp_gain[1] < clamp_gain[0]:
        raise ValueError(f"white_balance: clamp_gain must be a valid (min>0, max>=min) pair, got {clamp_gain!r}")
    if white_point <= black_point:
        raise ValueError(f"white_balance: white_point ({white_point}) must be > black_point ({black_point})")

    patch = _as_channels(rgb, name="rgb")
    clipped = _clip_flags(patch, black_point=black_point, white_point=white_point, clip_margin=clip_margin)
    target = (patch["r"] + patch["g"] + patch["b"]) / 3.0
    lo, hi = clamp_gain

    gain: dict[str, float] = {}
    for ch in _CHANNELS:
        denom = _safe_divisor(patch[ch], black_point=black_point, white_point=white_point, clip_margin=clip_margin)
        gain[ch] = _clamp(target / denom, lo, hi)

    return {
        "cdl": _gain_cdl(gain),
        "gain": gain,
        "patch": patch,
        "target": target,
        "clipped": clipped,
        "correction_pct": _correction_pct(gain),
        "verified": False,
    }


def wb_match_to_reference(
    rgb: Any,
    reference_rgb: Any,
    *,
    white_point: float = 255.0,
    black_point: float = 0.0,
    clamp_gain: tuple[float, float] = (0.5, 2.0),
    clip_margin: float = 1.0,
) -> dict[str, Any]:
    """
    Hero / b-roll match mode: compute a gain-only CDL that matches a patch's
    color to a reference ("hero") clip's patch, instead of to neutral — for
    carrying a deliberate non-neutral key across a group of b-roll shots.

    Params:
      rgb: the b-roll clip's sampled patch color, {"r":..,"g":..,"b":..} or [r,g,b].
      reference_rgb: the hero clip's sampled patch color, same shape.
      white_point / black_point / clamp_gain / clip_margin: see wb_from_patch.

    Returns the same shape as wb_from_patch, plus "reference" (the parsed
    reference patch) and "target" set per-channel to the reference patch
    (unlike wb_from_patch's single scalar target luma). As with
    wb_from_patch, "verified" is always False — structural grade math, not
    calibrated against a live Resolve render (see the offline/advanced tool
    set's WRITE-action convention, matching
    :mod:`davinci_resolve_mcp.grading.cdl_ops`). A patch that already
    matches the reference returns gain == {1,1,1} (identity CDL). Clipped
    channels — on either the patch or the reference — are flagged in
    "clipped" and floored rather than dividing by (near) zero.
    """
    if clamp_gain[0] <= 0 or clamp_gain[1] < clamp_gain[0]:
        raise ValueError(f"white_balance: clamp_gain must be a valid (min>0, max>=min) pair, got {clamp_gain!r}")
    if white_point <= black_point:
        raise ValueError(f"white_balance: white_point ({white_point}) must be > black_point ({black_point})")

    patch = _as_channels(rgb, name="rgb")
    reference = _as_channels(reference_rgb, name="reference_rgb")
    clipped_patch = _clip_flags(patch, black_point=black_point, white_point=white_point, clip_margin=clip_margin)
    clipped_reference = _clip_flags(reference, black_point=black_point, white_point=white_point, clip_margin=clip_margin)
    clipped = {ch: (clipped_patch[ch] or clipped_reference[ch]) for ch in _CHANNELS}
    lo, hi = clamp_gain

    gain: dict[str, float] = {}
    for ch in _CHANNELS:
        denom = _safe_divisor(patch[ch], black_point=black_point, white_point=white_point, clip_margin=clip_margin)
        gain[ch] = _clamp(reference[ch] / denom, lo, hi)

    return {
        "cdl": _gain_cdl(gain),
        "gain": gain,
        "patch": patch,
        "reference": reference,
        "target": dict(reference),
        "clipped": clipped,
        "correction_pct": _correction_pct(gain),
        "verified": False,
    }
