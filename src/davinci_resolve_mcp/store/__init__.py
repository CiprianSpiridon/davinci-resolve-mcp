"""
davinci_resolve_mcp.store — offline persistence layer.

This subpackage owns the on-disk SQLite database used as the source of
truth for the offline/advanced tooling (project snapshots, pipeline runs,
conform mappings, editorial state, etc.) and the append-only provenance
ledger that records what produced/modified each artifact:

    * db          — SQLite schema, migrations, and connection helpers
    * provenance  — append-only provenance/lineage ledger built on db

Modules in this package never connect to DaVinci Resolve — they operate
solely on local files/SQLite. This file intentionally contains no imports
to avoid import-cycle risk.
"""
