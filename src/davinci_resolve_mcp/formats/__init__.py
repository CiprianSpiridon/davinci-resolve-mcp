"""
davinci_resolve_mcp.formats — offline file-format layer.

This subpackage provides pure, file-based parsing/authoring for the formats
used by DaVinci Resolve's offline/advanced tooling:

    * drx_xml    — .drx XML envelope + node-graph structure
    * drx_codec  — .drx grade FieldsBlob/Body (zstd) parameter codec
    * cdl        — ASC CDL v1.2 (slope/offset/power/saturation) import/export
    * lut        — .cube / .3dl LUT header parsing + attach-reference records
    * drt        — .drt (Resolve timeline) zip/SeqContainer parsing/authoring
    * drp        — .drp (Resolve project) zip/SeqContainer parsing/authoring

Modules in this package never connect to DaVinci Resolve — they operate
solely on file paths/bytes and are safe to import and use with no Resolve
instance running. Individual format modules are imported lazily by callers;
this file intentionally contains no imports to avoid import-cycle risk.
"""
