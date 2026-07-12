"""
davinci_resolve_mcp.grading — offline grade/QC math layer.

This subpackage provides the pure-Python numeric operations used by the
offline/advanced color tooling, operating on the parameter dicts produced
by the formats layer (no Resolve connection required):

    * cdl_ops     — ASC CDL math (compose, invert, apply to sample values)
    * white_balance — offline white/black balance estimation & correction
    * skin_match  — skin-tone matching heuristics across shots
    * qc          — legality/gamut and broadcast-safe QC checks

Modules in this package never connect to DaVinci Resolve. This file
intentionally contains no imports to avoid import-cycle risk.
"""
