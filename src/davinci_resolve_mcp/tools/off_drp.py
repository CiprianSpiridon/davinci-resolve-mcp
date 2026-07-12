"""
davinci_resolve_mcp.tools.off_drp — offline ``.drp`` (Resolve Project)
reader / author / Media-Pool surgeon (OFFLINE/advanced tool set).

Registers exactly one action-dispatch tool, ``drp``, with three actions:

  * ``"read"``   — parse a ``.drp`` project file on disk and return its
    project name, Media-Pool folder tree, clips (root-level and
    flattened), and timelines (reusing the ``.drt`` SeqContainer parser).
  * ``"author"`` — turn a JSON project spec into a brand-new ``.drp`` ZIP
    archive on disk (project.xml + MpFolder.xml + one SeqContainer member
    per timeline), then round-trip it back through ``read_drp`` before
    returning so the response reflects what was actually written.
  * ``"edit"``   — surgically ``add_clip``/``remove_clip`` a Media-Pool
    clip in an existing ``.drp``, writing the result to a new file while
    leaving every other clip and every other archive member byte-for-byte
    untouched.

This is a thin dispatch/IO layer over :mod:`davinci_resolve_mcp.formats.drp`
here, not copied: this module is original Python) — it does not
reimplement ZIP/XML/Media-Pool parsing itself, it only resolves file
paths, calls into the formats layer, and shapes JSON responses.

The Media-Pool-blob link invariant
-----------------------------------
Resolve links a Media-Pool clip to its media file by the path encoded in
that clip's binary ``<ClipPathBlob>`` field, not by the plain-text
``<MediaFilePath>``. ``formats.drp`` reads/writes that blob as the link of
record (see its module docstring for the exact byte layout); this tool
never bypasses that — ``author``/``edit`` always route clip media paths
through :func:`davinci_resolve_mcp.formats.drp.author_drp` /
:func:`davinci_resolve_mcp.formats.drp.edit_drp`, which encode the blob.

This module NEVER connects to DaVinci Resolve: no ``DaVinciResolveScript``
import, no live session, nothing at import time beyond registering the
tool. It only reads/writes plain files on disk (``.drp`` zip archives).
Every code path returns a JSON string; nothing here raises — the
top-level ``drp`` dispatcher catches every exception and returns an
``"Error: ..."`` string instead. Actions that write a file (``author``,
``edit``) always carry a top-level ``"verified": false`` field — this
only guarantees the written archive re-parses structurally offline, not
that a live Resolve will accept it.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

from ..app import mcp
from ..formats import drp as drp_fmt

_VALID_ACTIONS = ("read", "author", "edit")
_EDIT_ACTIONS = ("add_clip", "remove_clip")


def _require_path(path: Optional[str], label: str) -> str:
    if not path or not isinstance(path, str) or not path.strip():
        raise ValueError(f"drp: '{label}' is required")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"no such file: {path}")
    return path


def _require_output_path(output_path: Optional[str], action: str) -> str:
    if not output_path or not isinstance(output_path, str) or not output_path.strip():
        raise ValueError(f"drp: 'output_path' is required for action '{action}'")
    return output_path


def _parse_json_object(raw: Optional[str], label: str, required: bool) -> Optional[Dict[str, Any]]:
    text = (raw or "").strip()
    if not text:
        if required:
            raise ValueError(f"drp: '{label}' is required")
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"{label} is not valid JSON: {e}") from e
    if not isinstance(obj, dict):
        raise ValueError(f"{label} must decode to a JSON object")
    return obj


def _do_read(path: Optional[str]) -> Dict[str, Any]:
    resolved = _require_path(path, "path")
    parsed = drp_fmt.read_drp(resolved)
    return {"action": "read", "path": resolved, **parsed}


def _do_author(spec_json: Optional[str], output_path: Optional[str]) -> Dict[str, Any]:
    out = _require_output_path(output_path, "author")
    spec = _parse_json_object(spec_json, "spec_json", required=True)

    buf = drp_fmt.author_drp(spec)
    with open(out, "wb") as fh:
        fh.write(buf)

    # Offline round-trip check: re-parse what was just written.
    reparsed = drp_fmt.read_drp(buf)
    return {
        "action": "author",
        "output_path": out,
        "bytes": len(buf),
        "project_name": reparsed.get("name"),
        "clip_count": len(reparsed.get("allClips", [])),
        "folder_count": len(reparsed.get("folders", [])),
        "timeline_count": len(reparsed.get("timelines", [])),
        "verified": False,
    }


def _do_edit(
    path: Optional[str],
    output_path: Optional[str],
    edit_action: Optional[str],
    clip_json: Optional[str],
    name: Optional[str],
    db_id: Optional[str],
) -> Dict[str, Any]:
    resolved = _require_path(path, "path")
    out = _require_output_path(output_path, "edit")

    edit_action_lower = edit_action.strip().lower() if isinstance(edit_action, str) else ""
    if edit_action_lower not in _EDIT_ACTIONS:
        raise ValueError(
            f"drp edit: 'edit_action' must be one of {_EDIT_ACTIONS!r} (got {edit_action!r})"
        )

    clip: Optional[Dict[str, Any]] = None
    if edit_action_lower == "add_clip":
        clip = _parse_json_object(clip_json, "clip_json", required=True)

    with open(resolved, "rb") as fh:
        buf = fh.read()

    # formats.drp.edit_drp touches only the root <MediaVec> insertion point
    # (add_clip) or the single matching clip block (remove_clip); every
    # other archive member and every other clip is preserved byte-for-byte.
    out_buf = drp_fmt.edit_drp(
        buf,
        edit_action_lower,
        clip=clip,
        name=(name or None),
        dbId=(db_id or None),
    )

    with open(out, "wb") as fh:
        fh.write(out_buf)

    reparsed = drp_fmt.read_drp(out_buf)
    return {
        "action": "edit",
        "edit_action": edit_action_lower,
        "path": resolved,
        "output_path": out,
        "bytes": len(out_buf),
        "clip_count": len(reparsed.get("allClips", [])),
        "verified": False,
    }


@mcp.tool()
def drp(
    action: str,
    path: str = "",
    output_path: str = "",
    spec_json: str = "",
    edit_action: str = "",
    clip_json: str = "",
    name: str = "",
    db_id: str = "",
) -> str:
    """Offline ``.drp`` (Resolve Project) reader/author/Media-Pool surgeon. Never touches Resolve.

    Parameters:
    - action: one of
        - "read": parse `path` (a ``.drp`` file on disk) and return
          `{name, folders, clips, allClips, timelines, mediaPoolRoot,
          seqContainers, hasProjectXml}`. `clips` are the Master-level
          Media-Pool clips; `allClips` is every clip in the folder tree,
          flattened, each carrying `folderPath`. Each clip carries
          `mediaPath` recovered from the Media-Pool `<ClipPathBlob>` first
          (the link Resolve actually uses), falling back to the plain-text
          `<MediaFilePath>`/`<Name>` — `linkedByBlob` says which. `timelines`
          reuses the ``.drt`` SeqContainer parser over the same archive.
        - "author": build a brand-new ``.drp`` from `spec_json` (a JSON
          object string shaped `{"name", "timelines": [{"name",
          "frameRate", "startTimecode", "resolution", "tracks":[...]}],
          "clips": [{"name"?, "mediaPath", "kind"?, "frameRate"?}],
          "folders": [{"name","clips":[...],"folders":[...]}], "metadata"?
          }`) and write it to `output_path`. Every media clip's link is
          encoded into the Media-Pool `<ClipPathBlob>` (the invariant
          Resolve relies on), mirrored in plain-text `<MediaFilePath>`.
          Round-trips through `read_drp` before returning so
          `clip_count`/`folder_count`/`timeline_count` reflect what was
          actually written.
        - "edit": open `path` (an existing ``.drp``), apply one Media-Pool
          edit given by `edit_action`, and write the result to
          `output_path` — the *input* file is never modified in place.
          `edit_action` is one of:
            - "add_clip": insert the clip described by `clip_json` (a JSON
              object string shaped `{"name"?, "mediaPath", "kind"?,
              "frameRate"?}`) into the root Media Pool, with its media link
              written into the Media-Pool blob. Only the root `<MediaVec>`
              is touched — every existing clip is preserved byte-for-byte
              (documented invariant: editing never disturbs unrelated
              clips).
            - "remove_clip": delete the one Media-Pool clip matching
              `db_id` (preferred, exact) or `name` (first match), leaving
              every other clip byte-for-byte intact. Provide at least one
              of `db_id`/`name`.
          Round-trips through `read_drp` before returning; response
          includes the resulting `clip_count`.
      Any other action returns the list of valid actions instead of
      erroring.
    - path: source ``.drp`` file path (for "read" and "edit").
    - output_path: destination file path (for "author" and "edit"; "edit"
      always writes a new file rather than modifying `path` in place).
    - spec_json: JSON-encoded project spec (for "author").
    - edit_action: "add_clip" or "remove_clip" (for "edit").
    - clip_json: JSON-encoded clip object (for "edit" with
      `edit_action="add_clip"`).
    - name: Media-Pool clip name to match (for "edit" with
      `edit_action="remove_clip"`, if `db_id` is not given).
    - db_id: Media-Pool clip `DbId` to match (for "edit" with
      `edit_action="remove_clip"`; preferred over `name`).

    A missing/unreadable file, a non-``.drp`` archive missing its
    `MpFolder.xml` media-pool document, or bad JSON/edit input always
    comes back as an "Error: ..." string (never a raised exception), per
    the offline tool-set convention. Every write action ("author",
    "edit") carries a top-level "verified": false field — this only
    guarantees the written archive re-parses structurally offline, not
    that a live DaVinci Resolve will accept it.
    """
    action_lower = action.strip().lower() if isinstance(action, str) else ""
    try:
        if action_lower == "read":
            return json.dumps(_do_read(path), indent=2)
        if action_lower == "author":
            return json.dumps(_do_author(spec_json, output_path), indent=2)
        if action_lower == "edit":
            return json.dumps(
                _do_edit(path, output_path, edit_action, clip_json, name, db_id), indent=2
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
