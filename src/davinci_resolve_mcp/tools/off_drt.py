"""
davinci_resolve_mcp.tools.off_drt — offline ``.drt`` (Resolve Timeline)
inspector / author / SeqContainer surgeon (OFFLINE/advanced tool set).

Registers exactly one action-dispatch tool, ``drt``, with five actions:

  * ``"parse"``            — read a ``.drt``/``.drp`` file and return its
    timelines (tracks/clips) + metadata.
  * ``"author"``            — turn a JSON timeline spec into a new ``.drt``
    file on disk.
  * ``"validate"``          — structural integrity check of a ``.drt``/
    ``.drp`` file (zip opens, has >=1 SeqContainer, no stray project.xml,
    well-formed SeqContainer schema).
  * ``"inject_into_drp"``   — graft every SeqContainer from a source
    ``.drt`` into a target ``.drp``, writing the result to a new file.
  * ``"extract_from_drp"``  — pull one SeqContainer out of a ``.drp`` and
    write it out as a standalone ``.drt``.

This is a thin dispatch/IO layer over :mod:`davinci_resolve_mcp.formats.drt`.
It does not reimplement ZIP/XML/SeqContainer parsing itself; it only resolves
file paths, calls into the formats layer,
and shapes JSON responses.

This module NEVER connects to DaVinci Resolve: no ``DaVinciResolveScript``
import, no live session, nothing at import time beyond registering the
tool. It only reads/writes plain files on disk (``.drt``/``.drp`` zip
archives). Every code path returns a JSON string; nothing here raises — the
top-level ``drt`` dispatcher catches every exception and returns an
``"Error: ..."`` string instead. Actions that write a file (``author``,
``inject_into_drp``, ``extract_from_drp``) always carry a top-level
``"verified": false`` field — this only guarantees the written archive
re-parses/re-validates structurally offline, not that a live Resolve will
accept it.
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from typing import Any, Dict, Optional

from ..app import mcp
from ..formats import drt as drt_fmt

_VALID_ACTIONS = ("parse", "author", "validate", "inject_into_drp", "extract_from_drp")


def _require_path(path: Optional[str], label: str) -> str:
    if not path or not isinstance(path, str) or not path.strip():
        raise ValueError(f"drt: '{label}' is required")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"no such file: {path}")
    return path


def _do_parse(path: Optional[str]) -> Dict[str, Any]:
    resolved = _require_path(path, "path")
    parsed = drt_fmt.parse_drt(resolved)
    return {"action": "parse", "path": resolved, **parsed}


def _do_validate(path: Optional[str]) -> Dict[str, Any]:
    resolved = _require_path(path, "path")
    result = drt_fmt.validate_drt(resolved)
    return {"action": "validate", "path": resolved, **result}


def _do_author(spec_json: Optional[str], output_path: Optional[str]) -> Dict[str, Any]:
    if not output_path or not isinstance(output_path, str) or not output_path.strip():
        raise ValueError("drt: 'output_path' is required for action 'author'")
    raw = (spec_json or "").strip()
    if not raw:
        raise ValueError("drt: 'spec_json' is required for action 'author'")
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"spec_json is not valid JSON: {e}") from e
    if not isinstance(spec, dict):
        raise ValueError("spec_json must decode to a JSON object")

    buf = drt_fmt.author_drt(spec)
    with open(output_path, "wb") as fh:
        fh.write(buf)

    # Offline round-trip check: re-parse what was just written.
    reparsed = drt_fmt.parse_drt(buf)
    return {
        "action": "author",
        "output_path": output_path,
        "bytes": len(buf),
        "timeline_count": len(reparsed.get("timelines", [])),
        "verified": False,
    }


def _do_inject_into_drp(drt_path: Optional[str], drp_path: Optional[str], output_path: Optional[str]) -> Dict[str, Any]:
    resolved_drt = _require_path(drt_path, "drt_path")
    resolved_drp = _require_path(drp_path, "drp_path")
    if not output_path or not isinstance(output_path, str) or not output_path.strip():
        raise ValueError("drt: 'output_path' is required for action 'inject_into_drp'")

    with open(resolved_drt, "rb") as fh:
        drt_bytes = fh.read()
    with open(resolved_drp, "rb") as fh:
        drp_bytes = fh.read()

    # parse_drt raises DrtError when the source carries no SeqContainer
    # members at all, so a truly empty .drt is reported as an error rather
    # than silently injecting nothing.
    source = drt_fmt.parse_drt(drt_bytes)
    seq_members = source.get("seqContainers") or []

    out_buf = drp_bytes
    injected = 0
    for member in seq_members:
        xml = drt_fmt.extract_seq_container(drt_bytes, member)
        out_buf = drt_fmt.inject_seq_container(out_buf, xml)
        injected += 1

    with open(output_path, "wb") as fh:
        fh.write(out_buf)

    return {
        "action": "inject_into_drp",
        "drt_path": resolved_drt,
        "drp_path": resolved_drp,
        "output_path": output_path,
        "bytes": len(out_buf),
        "seq_containers_injected": injected,
        "verified": False,
    }


def _do_extract_from_drp(drp_path: Optional[str], output_path: Optional[str], timeline_index: int) -> Dict[str, Any]:
    resolved_drp = _require_path(drp_path, "drp_path")
    if not output_path or not isinstance(output_path, str) or not output_path.strip():
        raise ValueError("drt: 'output_path' is required for action 'extract_from_drp'")

    with open(resolved_drp, "rb") as fh:
        drp_bytes = fh.read()

    # parse_drt raises DrtError when the source carries no SeqContainer
    # members at all (not a valid .drt/.drp).
    parsed = drt_fmt.parse_drt(drp_bytes)
    seq_members = parsed.get("seqContainers") or []

    idx = int(timeline_index or 0)
    if idx < 0 or idx >= len(seq_members):
        raise ValueError(
            f"timeline_index {idx} out of range (0..{len(seq_members) - 1}, found {len(seq_members)} SeqContainers)"
        )

    source_member = seq_members[idx]
    xml = drt_fmt.extract_seq_container(drp_bytes, source_member)

    out_stream = io.BytesIO()
    with zipfile.ZipFile(out_stream, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr("Primary1/SeqContainer1.xml", xml.encode("utf-8"))
        zf.writestr(
            "metadata.json",
            json.dumps(
                {
                    "source": "extract_from_drp",
                    "sourceDrp": resolved_drp,
                    "sourceSeqContainer": source_member,
                    "exportedFrom": "davinci-resolve-mcp off_drt.extract_from_drp",
                },
                indent=2,
            ).encode("utf-8"),
        )
    out_buf = out_stream.getvalue()

    with open(output_path, "wb") as fh:
        fh.write(out_buf)

    return {
        "action": "extract_from_drp",
        "drp_path": resolved_drp,
        "output_path": output_path,
        "bytes": len(out_buf),
        "source_seq_container": source_member,
        "timeline_index": idx,
        "seq_container_count": len(seq_members),
        "verified": False,
    }


@mcp.tool()
def drt(
    action: str,
    path: str = "",
    output_path: str = "",
    spec_json: str = "",
    drt_path: str = "",
    drp_path: str = "",
    timeline_index: int = 0,
) -> str:
    """Offline ``.drt``/``.drp`` timeline (SeqContainer) inspector/author/surgeon. Never touches Resolve.

    Parameters:
    - action: one of
        - "parse": read `path` (a ``.drt`` or ``.drp`` file on disk) and
          return `{timelines, metadata, seqContainers}` — each timeline
          carries name/frameRate/startTimecode/resolution and its
          video/audio tracks (`Sm2TiTrack`-shaped) with per-clip
          start/duration/in/out frame numbers and media paths.
        - "author": build a new ``.drt`` from `spec_json` (a JSON object
          string shaped `{"timelines": [{"name","frameRate",
          "startTimecode","resolution","tracks":[{"type":"video"|"audio",
          "clips":[...]}]}], "mediaPool"?, "metadata"?}`) and write it to
          `output_path`. Round-trips through `parse_drt` before returning
          so `timeline_count` reflects what was actually written.
        - "validate": structural integrity check of `path` — returns
          `{valid, errors}`; flags a missing/unreadable zip, zero
          SeqContainer members, a `.drt` that wrongly carries
          `project.xml`, and malformed SeqContainer schema (missing
          `<Sm2SequenceContainer>`/`<Name>`/`<FrameRate>`, or clips with an
          empty `<MediaFilePath>`).
        - "inject_into_drp": graft every SeqContainer XML member found in
          `drt_path` into `drp_path`, writing the merged archive to
          `output_path`. All other members of `drp_path` (project.xml,
          MpFolder.xml, etc.) are preserved byte-for-byte; injected
          SeqContainers are appended as new numbered members.
        - "extract_from_drp": pull the SeqContainer at `timeline_index`
          (0-based, default 0) out of `drp_path` and write it out as a
          standalone ``.drt`` at `output_path` (plus a `metadata.json`
          member recording the source). Feed the resulting ``.drt`` back
          through `parse`/`validate`, or into a live-Resolve timeline
          import tool.
      Any other action returns the list of valid actions instead of
      erroring.
    - path: source ``.drt``/``.drp`` file path (for "parse"/"validate").
    - output_path: destination file path (for "author",
      "inject_into_drp", "extract_from_drp").
    - spec_json: JSON-encoded timeline spec (for "author").
    - drt_path: source ``.drt`` file path (for "inject_into_drp").
    - drp_path: source/target ``.drp`` file path (for "inject_into_drp"
      and "extract_from_drp").
    - timeline_index: which SeqContainer to pull out of `drp_path`
      (for "extract_from_drp"; default 0).

    A missing/unreadable file for any file-reading action always comes
    back as an "Error: ..." string (never a raised exception), per the
    offline tool-set convention. Every write action ("author",
    "inject_into_drp", "extract_from_drp") carries a top-level
    "verified": false field — this only guarantees the written archive
    re-parses/re-validates structurally offline, not that a live
    DaVinci Resolve will accept it.
    """
    action_lower = action.strip().lower() if isinstance(action, str) else ""
    try:
        if action_lower == "parse":
            return json.dumps(_do_parse(path), indent=2)
        if action_lower == "validate":
            return json.dumps(_do_validate(path), indent=2)
        if action_lower == "author":
            return json.dumps(_do_author(spec_json, output_path), indent=2)
        if action_lower == "inject_into_drp":
            return json.dumps(_do_inject_into_drp(drt_path, drp_path, output_path), indent=2)
        if action_lower == "extract_from_drp":
            return json.dumps(_do_extract_from_drp(drp_path, output_path, timeline_index), indent=2)

        return json.dumps(
            {
                "error": f"Unknown action '{action}'.",
                "valid_actions": list(_VALID_ACTIONS),
            },
            indent=2,
        )
    except Exception as e:  # noqa: BLE001 - offline tools never raise
        return f"Error: {e}"
