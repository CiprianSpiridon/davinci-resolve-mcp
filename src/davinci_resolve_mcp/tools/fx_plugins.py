"""Live OFX / ResolveFX + template/macro apply tools for the DaVinci Resolve
MCP server.

Two families of tool live here:

1. :func:`apply_ofx_to_clip` drops an OFX / ResolveFX plugin node onto a
   timeline clip's Fusion composition and splices it into the clip's render
   chain, so the effect actually touches the picture instead of sitting
   orphaned on the flow. It gets (or creates) the clip's Fusion comp, wraps the
   mutation in ``comp.Lock()`` + ``StartUndo``/``EndUndo`` so it is reversible,
   adds the plugin via ``comp.AddTool('ofx.' + regid, ...)`` (the ``'ofx.'``
   prefix is REQUIRED by Fusion's registry — the ``.drx`` wireId carries none,
   so it is added here), splices the new node between ``MediaOut1``'s current
   upstream tool and ``MediaOut1`` itself (``Source`` in, ``Input`` out),
   applies any requested inputs, and returns the node's ``TOOLS_RegID``.

2. Template / macro placement:
   - :func:`insert_template_by_name` inserts a title / generator template at the
     playhead via ``Timeline.Insert{Title,FusionTitle,Generator,FusionGenerator}
     IntoTimeline``, dispatched by ``kind``. Every Insert* return is None-checked
     and, for Fusion titles/generators, a None result triggers a single retry via
     the internal template-id form before the tool reports failure — a None item
     is NEVER reported as a success.
   - :func:`append_template_with_placement` places a Media Pool item onto a track
     with explicit source/record framing via ``MediaPool.AppendToTimeline`` and
     (optionally) sets a Text+/Template ``StyledText`` input on the placed item.
   - :func:`attach_fusion_comp` imports a saved Fusion comp (``.comp``/``.setting``)
     onto an existing timeline item via ``TimelineItem.ImportFusionComp``.

3. Plugin / template / ResolveFX discovery (no Resolve needed except the last):
   - :func:`enumerate_templates` filesystem-scans the shipped and/or user
     ``Fusion/Templates`` trees for ``.setting`` / ``.drfx`` templates, reporting
     the ``Insert*`` name (basename minus the extension) and flagging Titles /
     Generators as insertable.
   - :func:`enumerate_ofx` filesystem-scans the system and user ``OFX/Plugins``
     dirs for ``*.ofx.bundle`` entries.
     ``data/resolvefx-registry.json`` via a package-relative path (so it works
     pip-installed) and serves each ResolveFX entry with its ``wireId`` and the
     ``'ofx.' + wireId`` Fusion RegID.
   These three run with NO Resolve running. Only:
   - :func:`discover_regid` needs a live Resolve: it opens the Fusion page and
     reads a tool's ``GetAttrs()['TOOLS_RegID']`` for LIVE ground-truth RegIDs.

Low-level comp/node access is REUSED from ``tools/fusion.py`` (``_get_fusion_comp``)
and Media Pool access from ``tools/media_pool.py`` (``_media_pool``) rather than
redefined here. Macro apply to a clip is owned by ``tools/fusion.apply_macro_to_clip``
and is NOT redefined in this module. Nothing here imports ``DaVinciResolveScript``
or touches a live Resolve instance at import time — the connection is reached
lazily inside each tool body via ``_conn``/``_get_timeline_item``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import zipfile
from typing import Any, Dict, List, Optional, Tuple

from ..app import mcp
from ..helpers import (
    _check_choice,
    _coerce_value,
    _conn,
    _get_timeline_item,
    _ok,
    _require_timeline,
)
from ..formats.kenburns import build_kenburns_comp
from .fusion import _get_fusion_comp, _resolve_tool, _set_tool_input
from .media_pool import _media_pool

# The template kinds :func:`insert_template_by_name` can dispatch, mapped to the
# Timeline Insert* method name and whether the kind is Fusion-backed (Fusion
# titles/generators get the template-id retry when the primary insert returns
# None; standard title/generator templates do not).
_TEMPLATE_KINDS: Dict[str, Dict[str, Any]] = {
    "title": {"method": "InsertTitleIntoTimeline", "fusion": False},
    "fusion_title": {"method": "InsertFusionTitleIntoTimeline", "fusion": True},
    "generator": {"method": "InsertGeneratorIntoTimeline", "fusion": False},
    "fusion_generator": {
        "method": "InsertFusionGeneratorIntoTimeline",
        "fusion": True,
    },
}


def _fusion_template_id(name: str) -> str:
    """Best-effort map a Fusion template DISPLAY name to its internal id.

    Fusion title/generator templates can be addressed either by their display
    name or by the internal template id — which, for file-backed macros, is the
    template's base name without its directory or ``.setting``/``.comp``/``.drfx``
    extension. When ``Timeline.InsertFusion*IntoTimeline`` returns None for a
    display name, retrying with this id form often resolves a user macro.
    """
    base = os.path.basename((name or "").strip())
    for ext in (".setting", ".comp", ".drfx"):
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
            break
    return base


def _find_media_pool_item(name: str):
    """Return the first Media Pool item named ``name`` (recursive), or None.

    Searches the current project's Media Pool depth-first from the root folder
    so a clip can be placed by name regardless of which bin is selected.
    """
    target = (name or "").strip()
    if not target:
        return None
    mp = _media_pool()
    root = mp.GetRootFolder()
    if root is None:
        return None

    stack = [root]
    while stack:
        folder = stack.pop()
        for clip in folder.GetClipList() or []:
            try:
                if clip.GetName() == target:
                    return clip
            except Exception:  # noqa: BLE001
                continue
        for sub in folder.GetSubFolderList() or []:
            stack.append(sub)
    return None


@mcp.tool()
def apply_ofx_to_clip(
    regid: str,
    params: str = "",
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
    comp_index: int = 1,
) -> str:
    """Apply an OFX / ResolveFX plugin to a timeline clip via its Fusion comp.

    The plugin node is created inside the clip's Fusion composition and spliced
    into the render chain between ``MediaOut1``'s current upstream tool and
    ``MediaOut1`` (the new node's ``Source`` input takes the old upstream, and
    ``MediaOut1``'s ``Input`` takes the new node), so the effect is actually in
    the picture path. The whole mutation is wrapped in ``StartUndo``/``EndUndo``
    and ``comp.Lock()``/``Unlock`` so it is a single reversible step.

    Parameters:
    - regid: the OFX / ResolveFX registry id WITHOUT the ``'ofx.'`` prefix
      (e.g. ``"com.blackmagicdesign.resolvefx.gaussianblur"``). The ``'ofx.'``
      prefix Fusion requires is prepended automatically; a value that already
      carries it is used as-is.
    - params: optional JSON object of input id -> value to set on the new node
      AFTER it is wired, e.g. ``'{"Blend": 0.5, "FilterType": 1}'``. Values are
      coerced from strings to int/float/bool where they look numeric/boolean.
      Leave empty to add the plugin without setting any inputs.
    - track_type: "video" (default), "audio", or "subtitle".
    - track_index: 1-based track index (default: 1).
    - item_index: 0-based index of the item within that track (default: 0).
    - comp_index: 1-based Fusion composition index on the item (default: 1).

    Returns a JSON object with the new node's ``TOOLS_RegID`` on success, or an
    ``Error: ...`` string on failure (including when the RegID is unregistered
    or the plugin is not installed, in which case ``AddTool`` returns ``None``).
    """
    try:
        # Parse the optional params JSON up front so a bad payload fails before
        # we mutate the comp.
        parsed: Dict[str, Any] = {}
        if params and params.strip():
            try:
                parsed = json.loads(params)
            except (ValueError, TypeError) as e:
                return (
                    f"Invalid params JSON: {e}. Provide a JSON object of input "
                    'id -> value, e.g. {"Blend": 0.5, "FilterType": 1}.'
                )
            if not isinstance(parsed, dict):
                return (
                    "params must be a JSON object of input id -> value, e.g. "
                    '{"Blend": 0.5, "FilterType": 1}.'
                )

        item = _get_timeline_item(track_type, track_index, item_index)
        comp = _get_fusion_comp(item, comp_index)

        # KEEP the 'ofx.' prefix Fusion's registry requires; the .drx wireId has
        # none, so add it here (but never double it).
        reg_id = regid if regid.startswith("ofx.") else f"ofx.{regid}"

        media_out = comp.FindTool("MediaOut1")
        if media_out is None:
            return (
                "Error: No 'MediaOut1' in the clip's Fusion comp; cannot splice "
                "the effect into the render chain."
            )

        comp.Lock()
        comp.StartUndo("apply_ofx_to_clip")
        try:
            tool = comp.AddTool(reg_id, -32768, -32768)
            if tool is None:
                # Unregistered RegID / plugin not installed: bail out WITHOUT
                # reporting success. EndUndo/Unlock still run in the finally.
                return (
                    f"Error: AddTool nil: RegID '{reg_id}' "
                    f"unregistered/plugin not installed. Check the OFX/ResolveFX "
                    f"registry id and that the plugin is installed."
                )

            # Find MediaOut1's current upstream tool so we can splice in front of
            # it: the new node's Source takes that upstream, MediaOut1's Input
            # takes the new node. Fall back to MediaIn1 when nothing is wired.
            src = None
            try:
                main_in = media_out.FindMainInput(1)
                if main_in is not None:
                    connected = main_in.GetConnectedOutput()
                    if connected is not None:
                        src = connected.GetTool()
            except Exception:
                src = None
            if src is None:
                src = comp.FindTool("MediaIn1")

            if src is not None:
                tool.ConnectInput("Source", src)
            media_out.ConnectInput("Input", tool)

            # Apply requested inputs AFTER wiring.
            for input_id, value in parsed.items():
                try:
                    tool.SetInput(str(input_id), _coerce_value(value))
                except Exception:
                    # A single bad input id/value should not abort the whole
                    # (already-wired) apply; report it and keep going.
                    pass

            attrs = tool.GetAttrs() or {}
            return json.dumps({
                "success": True,
                "reg_id": attrs.get("TOOLS_RegID", reg_id),
                "tool_name": attrs.get("TOOLS_Name", ""),
                "spliced_upstream": (
                    (src.GetAttrs() or {}).get("TOOLS_Name", "") if src else None
                ),
                "params_applied": list(parsed.keys()),
                "comp_index": comp_index,
            }, indent=2, default=str)
        finally:
            try:
                comp.EndUndo(True)
            except Exception:
                pass
            try:
                comp.Unlock()
            except Exception:
                pass
    except Exception as e:
        return f"Error: {e}"


def _insert_template_item(timeline, canonical_kind: str, used_name: str):
    """Insert a title/generator template at the playhead; return ``(item, retried_template_id)``.

    Core of :func:`insert_template_by_name`, reused by :func:`cache_template_comp`.
    Dispatches to the Timeline Insert* method for ``canonical_kind`` and, for a Fusion
    title/generator, retries ONCE with the internal template-id form (see
    :func:`_fusion_template_id`) when the display name returns None. ``item`` is None
    when the name did not resolve (the caller reports that); raises ``RuntimeError``
    when this Resolve build exposes no matching Insert* method — the caller's
    ``try/except`` turns that into the same ``"Error: ..."`` string as before.
    """
    spec = _TEMPLATE_KINDS[canonical_kind]
    method_name = spec["method"]
    is_fusion = spec["fusion"]

    insert = getattr(timeline, method_name, None)
    if insert is None:
        raise RuntimeError(
            f"this Resolve build exposes no '{method_name}' — "
            f"cannot insert a {canonical_kind} template."
        )

    item = insert(used_name)

    # For Fusion titles/generators, a None result often means the display name
    # did not resolve; retry ONCE with the internal template-id form.
    retried_with: Optional[str] = None
    if item is None and is_fusion:
        template_id = _fusion_template_id(used_name)
        if template_id and template_id != used_name:
            retried_with = template_id
            item = insert(template_id)
        else:
            # No distinct id form; still make the single documented retry so a
            # transient None does not falsely error.
            retried_with = used_name
            item = insert(used_name)

    return item, retried_with


@mcp.tool()
def insert_template_by_name(kind: str, name: str) -> str:
    """Insert a title / generator template at the playhead of the current timeline.

    Dispatches by ``kind`` to the matching Timeline Insert* call:
    - "title"            -> ``InsertTitleIntoTimeline``
    - "fusion_title"     -> ``InsertFusionTitleIntoTimeline``
    - "generator"        -> ``InsertGeneratorIntoTimeline``
    - "fusion_generator" -> ``InsertFusionGeneratorIntoTimeline``

    The returned item is ALWAYS None-checked. For a Fusion title/generator whose
    primary insert returns None, the tool retries ONCE using the internal
    template-id form of the name (see :func:`_fusion_template_id`) before giving
    up. A None item is never reported as success — an unresolvable name returns a
    clear ``Error: ... template not resolved`` string.

    Parameters:
    - kind: one of "title", "fusion_title", "generator", "fusion_generator".
    - name: the template's display name (e.g. "Text+", "Lower Third",
      "Solid Color"). For Fusion templates the internal template id (a file's
      base name) is tried automatically on a None result.

    Returns a JSON object describing the inserted item on success, or an
    ``Error: ...`` string on failure.
    """
    try:
        canonical_kind, err = _check_choice(
            kind, tuple(_TEMPLATE_KINDS.keys()), "kind"
        )
        if err:
            return f"Error: {err}"

        if not name or not name.strip():
            return "Error: name is required (the template's display name)."

        conn = _conn()
        timeline = _require_timeline(conn)

        used_name = name.strip()
        item, retried_with = _insert_template_item(
            timeline, canonical_kind, used_name
        )

        if item is None:
            hint = (
                f" (also tried template id '{retried_with}')"
                if retried_with is not None
                else ""
            )
            return (
                f"Error: template not resolved — '{used_name}' did not insert as "
                f"a {canonical_kind}{hint}. Check the template name exists in the "
                f"Edit page Effects/Titles/Generators list."
            )

        try:
            item_name = item.GetName()
        except Exception:  # noqa: BLE001
            item_name = ""

        return json.dumps(
            {
                "success": True,
                "kind": canonical_kind,
                "requested_name": used_name,
                "retried_template_id": retried_with,
                "item_name": item_name,
            },
            indent=2,
            default=str,
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


def _append_video_carrier(
    clip,
    start_frame: int,
    end_frame: int,
    track_index: int,
    record_frame: int,
    label: str = "",
):
    """Append a video-only carrier (``mediaType:1``) for ``clip`` onto ``track_index``.

    Shared placement core of :func:`append_template_with_placement` and
    :func:`place_overlay_title`: guards that the source duration is >= 4 frames and
    that the target video track exists, builds ONE ``AppendToTimeline`` clipInfo
    ``{mediaPoolItem, startFrame, endFrame, trackIndex, recordFrame, mediaType: 1}``,
    and returns the placed ``TimelineItem``. Raises ``RuntimeError`` (which the calling
    tool's ``try/except`` turns into an ``"Error: ..."`` string identical to the prior
    inline guards) on any guard failure or when ``AppendToTimeline`` places nothing;
    ``label`` only names ``clip`` in the empty-result message.
    """
    duration = int(end_frame) - int(start_frame)
    if duration < 4:
        raise RuntimeError(
            f"source duration {duration} frame(s) is too short — "
            f"need at least 4 frames (start_frame={start_frame}, "
            f"end_frame={end_frame}, end is exclusive). Not appending."
        )

    if track_index < 1:
        raise RuntimeError(f"track_index must be >= 1 (got {track_index}).")

    conn = _conn()
    timeline = _require_timeline(conn)

    try:
        track_count = timeline.GetTrackCount("video")
    except Exception:  # noqa: BLE001
        track_count = 0
    if track_index > (track_count or 0):
        raise RuntimeError(
            f"video track {track_index} does not exist — timeline has "
            f"{track_count or 0} video track(s). Add a track first. "
            f"Not appending."
        )

    clip_info = {
        "mediaPoolItem": clip,
        "startFrame": int(start_frame),
        "endFrame": int(end_frame),
        "trackIndex": int(track_index),
        "recordFrame": int(record_frame),
        "mediaType": 1,
    }

    mp = _media_pool()
    placed = mp.AppendToTimeline([clip_info]) or []
    if not placed:
        raise RuntimeError(
            f"AppendToTimeline placed nothing for '{label}' on "
            f"video track {track_index} at frame {record_frame}. Check the "
            f"record frame does not collide and the track is unlocked."
        )
    return placed[0]


@mcp.tool()
def append_template_with_placement(
    clip_name: str,
    start_frame: int,
    end_frame: int,
    track_index: int = 1,
    record_frame: int = 0,
    text: str = "",
) -> str:
    """Place a Media Pool item onto a video track with explicit framing.

    Builds a single ``AppendToTimeline`` clipInfo
    ``{mediaPoolItem, startFrame, endFrame, trackIndex, recordFrame,
    mediaType: 1}`` (mediaType 1 = video) and appends it to the current timeline.
    When ``text`` is provided, the placed item's Fusion comp is asked for a
    ``Template`` tool (the Text+/template node) and its ``StyledText`` input is set.

    Guards BEFORE calling ``AppendToTimeline``:
    - the source duration (``end_frame - start_frame``) must be >= 4 frames;
    - the target video track (``track_index``) must already exist on the timeline.

    Parameters:
    - clip_name: name of the Media Pool item to place (searched recursively).
    - start_frame: source in-point (native clip frames), inclusive.
    - end_frame: source out-point (native clip frames), EXCLUSIVE.
    - track_index: 1-based video track to place on (default: 1).
    - record_frame: timeline frame to place the clip at (default: 0).
    - text: optional StyledText to set on the placed item's Template tool.

    Returns a JSON object describing the placement on success, or an
    ``Error: ...`` string (including the duration/track guards) on failure.
    """
    try:
        clip = _find_media_pool_item(clip_name)
        if clip is None:
            return (
                f"Error: no Media Pool item named '{clip_name}' found. "
                f"Import it or check the name. Not appending."
            )

        item = _append_video_carrier(
            clip, start_frame, end_frame, track_index, record_frame,
            label=clip_name,
        )
        duration = int(end_frame) - int(start_frame)

        styled_text_set = False
        if text:
            try:
                comp = _get_fusion_comp(item, 1)
                template_tool = comp.FindTool("Template")
                if template_tool is not None:
                    # StyledText is a text string; pass it verbatim rather than
                    # coercing (which would turn "123" -> int, "true" -> bool).
                    template_tool.SetInput("StyledText", text)
                    styled_text_set = True
            except Exception:  # noqa: BLE001
                # A failed StyledText set must not undo the (successful)
                # placement; report it via the flag instead of raising.
                styled_text_set = False

        try:
            item_name = item.GetName()
        except Exception:  # noqa: BLE001
            item_name = ""

        return json.dumps(
            {
                "success": True,
                "clip_name": clip_name,
                "item_name": item_name,
                "track_index": track_index,
                "record_frame": record_frame,
                "start_frame": start_frame,
                "end_frame": end_frame,
                "duration": duration,
                "styled_text_set": styled_text_set if text else None,
            },
            indent=2,
            default=str,
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def attach_fusion_comp(
    track_type: str,
    track_index: int,
    item_index: int,
    comp_path: str,
) -> str:
    """Import a saved Fusion composition onto an existing timeline item.

    Wraps ``TimelineItem.ImportFusionComp(comp_path)`` — the item's Fusion comp
    is replaced/created from the ``.comp``/``.setting`` file on disk.

    Parameters:
    - track_type: "video" (default typical), "audio", or "subtitle".
    - track_index: 1-based track index.
    - item_index: 0-based index of the item within that track.
    - comp_path: absolute path to the Fusion comp file to import.

    Returns a success string, or an ``Error: ...`` string when the path is
    missing/not a file or the import returns a falsy result.
    """
    try:
        if not comp_path or not comp_path.strip():
            return "Error: comp_path is required (path to a Fusion .comp/.setting)."
        if not os.path.isfile(comp_path):
            return (
                f"Error: comp_path '{comp_path}' is not an existing file. "
                f"Provide an absolute path to a saved Fusion composition."
            )

        item = _get_timeline_item(track_type, track_index, item_index)
        result = item.ImportFusionComp(comp_path)
        return _ok(
            result,
            f"Imported Fusion comp '{comp_path}' onto item "
            f"{item_index} of {track_type} track {track_index}.",
            f"Error: ImportFusionComp returned no comp for '{comp_path}'. "
            f"Check the file is a valid Fusion composition.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Plugin / template / ResolveFX discovery ─────────────────────────────────
# The path helpers below reach the filesystem ONLY (no Resolve). Each returns
# per-platform default install locations; absent directories are simply skipped
# by the callers so a scan of a machine without them returns an empty list
# rather than an error.

# Template categories that can be placed with a Timeline Insert* call. Only
# Titles and Generators are insertable this way (Transitions/Effects/Motion
# Graphics are applied differently), so only those are flagged insertable.
_INSERTABLE_TEMPLATE_CATEGORIES = ("titles", "title", "generators", "generator")

# Template file extensions we report. ``.setting`` is a loose Fusion macro/
# template; ``.drfx`` is a packaged DaVinci Resolve effects bundle.
_TEMPLATE_EXTS = (".setting", ".drfx")


def _blackmagic_support_bases() -> Dict[str, List[str]]:
    """Return per-scope Blackmagic 'DaVinci Resolve' support base directories.

    Keys are ``"shipped"`` (system-wide install) and ``"user"`` (per-user).
    The ``Fusion/Templates`` tree hangs off each of these. Paths are the
    per-platform defaults; callers skip any that do not exist on disk.
    """
    home = os.path.expanduser("~")
    if sys.platform.startswith("darwin"):
        app_support = "Library/Application Support/Blackmagic Design/DaVinci Resolve"
        return {
            "shipped": [os.path.join("/", app_support)],
            "user": [os.path.join(home, app_support)],
        }
    if sys.platform.startswith("win") or sys.platform.startswith("cygwin"):
        program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        app_data = os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming"))
        tail = os.path.join("Blackmagic Design", "DaVinci Resolve", "Support")
        return {
            "shipped": [os.path.join(program_data, tail)],
            "user": [os.path.join(app_data, tail)],
        }
    # Linux / other POSIX.
    return {
        "shipped": ["/opt/resolve", "/home/resolve/.local/share/DaVinciResolve"],
        "user": [os.path.join(home, ".local/share/DaVinciResolve")],
    }


def _template_roots(scope: str) -> List[Tuple[str, str]]:
    """Return ``(scope_label, templates_root)`` pairs for the given scope.

    ``scope`` is one of ``"all"``, ``"shipped"``, ``"user"``. The templates
    root for each base is ``<base>/Fusion/Templates``. Non-existent roots are
    included in the returned list (the caller decides how to treat them); the
    order is shipped-then-user for ``"all"``.
    """
    bases = _blackmagic_support_bases()
    wanted = ("shipped", "user") if scope == "all" else (scope,)
    roots: List[Tuple[str, str]] = []
    for label in wanted:
        for base in bases.get(label, []):
            roots.append((label, os.path.join(base, "Fusion", "Templates")))
    return roots


def _ofx_plugin_roots() -> List[str]:
    """Return the OFX plugin search directories for this platform."""
    home = os.path.expanduser("~")
    if sys.platform.startswith("darwin"):
        return ["/Library/OFX/Plugins", os.path.join(home, "Library/OFX/Plugins")]
    if sys.platform.startswith("win") or sys.platform.startswith("cygwin"):
        common = os.environ.get(
            "COMMONPROGRAMFILES", r"C:\Program Files\Common Files"
        )
        return [os.path.join(common, "OFX", "Plugins")]
    return ["/usr/OFX/Plugins", "/usr/local/OFX/Plugins",
            os.path.join(home, "OFX/Plugins")]


def _template_category(rel_parts: List[str]) -> str:
    """Best-effort template category from a path relative to Templates root.

    Templates live under ``Edit/<Category>/.../<name>.setting``; the category is
    the path segment immediately after an ``Edit`` segment. When there is no
    ``Edit`` segment (e.g. a top-level ``.drfx``), the first directory segment
    (if any) is used as the category.
    """
    lowered = [p.lower() for p in rel_parts]
    if "edit" in lowered:
        idx = lowered.index("edit")
        # The segment right after 'Edit' is the category, but only when it is a
        # directory (i.e. not the filename, which is the last element).
        if idx + 1 < len(rel_parts) - 1:
            return rel_parts[idx + 1]
    # Fallback: first directory segment before the filename, if any.
    if len(rel_parts) > 1:
        return rel_parts[0]
    return ""


def _drfx_templates(drfx_path: str) -> List[Tuple[str, str]]:
    """Expand a ``.drfx`` package into the templates it contains.

    A ``.drfx`` is a ZIP archive whose real, insertable templates live inside as
    ``Edit/<Category>/.../<name>.setting`` entries (a single pack commonly holds
    dozens across Titles/Transitions/Effects/Generators). The pack's category is
    NOT derivable from where the ``.drfx`` file sits on disk — it must be read
    from inside the archive. Returns ``(insert_name, category)`` pairs; an
    unreadable/oddly-structured archive yields ``[]`` (caller falls back).
    """
    out: List[Tuple[str, str]] = []
    try:
        with zipfile.ZipFile(drfx_path) as zf:
            for entry in zf.namelist():
                if "__MACOSX" in entry or not entry.lower().endswith(".setting"):
                    continue
                parts = entry.split("/")
                lowered = [p.lower() for p in parts]
                category = ""
                if "edit" in lowered:
                    idx = lowered.index("edit")
                    if idx + 1 < len(parts):
                        category = parts[idx + 1]
                insert_name = os.path.basename(entry)[: -len(".setting")]
                if insert_name:
                    out.append((insert_name, category))
    except (zipfile.BadZipFile, OSError, RuntimeError):
        return []
    return out


def _registry_json_path() -> str:

    Resolved via a PACKAGE-RELATIVE path so it works when the package is
    pip-installed (never a cwd-relative path). Prefers
    ``importlib.resources.files('davinci_resolve_mcp')`` and falls back to a
    ``__file__``-relative path if importlib.resources is unavailable.
    """
    try:
        from importlib.resources import files

        return str(files("davinci_resolve_mcp") / "data" / "resolvefx-registry.json")
    except Exception:  # noqa: BLE001 - fall back to a __file__-relative path
        pkg_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(pkg_dir, "data", "resolvefx-registry.json")


@mcp.tool()
def enumerate_templates(scope: str = "all", expand_drfx: bool = True) -> str:
    """List Fusion Edit templates (titles/generators/etc.) on this machine.

    Filesystem scan ONLY — no running Resolve required. Walks the
    ``Fusion/Templates`` tree(s) for the requested ``scope``. Loose ``.setting``
    templates are reported directly. A ``.drfx`` is a ZIP PACK (e.g. a purchased
    MotionVFX pack) that holds many templates across Titles/Transitions/Effects;
    with ``expand_drfx`` (default) it is opened and each internal template is
    reported with its true, in-archive category and an ``Insert*`` name (the
    value you pass to :func:`insert_template_by_name`). Titles and Generators are
    flagged ``insertable: true`` (droppable via a Timeline Insert* call);
    other categories are reported for discovery, ``insertable: false``. Each
    expanded row also carries the source ``pack`` filename.

    Parameters:
    - scope: "all" (default), "shipped" (system install), or "user"
      (per-user templates).
    - expand_drfx: when True (default), expand each ``.drfx`` pack into its
      internal templates. When False, report one row per ``.drfx`` file (its
      category comes from the on-disk path, so it may be empty for a top-level
      pack).

    Returns a JSON object with a ``templates`` list. A scanned directory that
    does not exist contributes nothing (NOT an error), so a machine with no
    templates simply returns an empty list.
    """
    try:
        canonical, err = _check_choice(scope, ("all", "shipped", "user"), "scope")
        if err:
            return f"Error: {err}"

        templates: List[Dict[str, Any]] = []
        scanned_roots: List[str] = []
        for scope_label, root in _template_roots(canonical):
            if not os.path.isdir(root):
                continue
            scanned_roots.append(root)
            for dirpath, _dirnames, filenames in os.walk(root):
                for fname in filenames:
                    lower = fname.lower()
                    ext = next(
                        (e for e in _TEMPLATE_EXTS if lower.endswith(e)), None
                    )
                    if ext is None:
                        continue
                    full = os.path.join(dirpath, fname)
                    rel = os.path.relpath(full, root)
                    rel_parts = rel.split(os.sep)
                    # A .drfx is a ZIP PACK, not a single template: expand it
                    # into the templates it carries, each with its true category
                    # read from INSIDE the archive (a top-level .drfx has no
                    # on-disk category, which is why packs used to come back
                    # category="" / insertable=false).
                    if ext == ".drfx" and expand_drfx:
                        contents = _drfx_templates(full)
                        if contents:
                            for insert_name, category in contents:
                                templates.append(
                                    {
                                        "name": insert_name,
                                        "insert_name": insert_name,
                                        "category": category,
                                        "type": "drfx",
                                        "scope": scope_label,
                                        "insertable": category.lower()
                                        in _INSERTABLE_TEMPLATE_CATEGORIES,
                                        "pack": fname,
                                        "path": full,
                                    }
                                )
                            continue
                        # Unreadable/empty archive: fall through to a summary row.

                    category = _template_category(rel_parts)
                    insert_name = fname[: -len(ext)]
                    # Insertability is decided by CATEGORY (Titles/Generators can
                    # be dropped via a Timeline Insert* call), independent of the
                    # file extension — a .setting OR a .drfx-internal template both
                    # qualify.
                    insertable = category.lower() in _INSERTABLE_TEMPLATE_CATEGORIES
                    templates.append(
                        {
                            "name": insert_name,
                            "insert_name": insert_name,
                            "category": category,
                            "type": ext.lstrip("."),
                            "scope": scope_label,
                            "insertable": insertable,
                            "path": full,
                        }
                    )

        return json.dumps(
            {
                "success": True,
                "scope": canonical,
                "count": len(templates),
                "scanned_roots": scanned_roots,
                "templates": templates,
            },
            indent=2,
            default=str,
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def enumerate_ofx() -> str:
    """List installed OFX plugin bundles (``*.ofx.bundle``) on this machine.

    Filesystem scan ONLY — no running Resolve required. Scans the platform's
    system and user ``OFX/Plugins`` directories (e.g. ``/Library/OFX/Plugins``
    and ``~/Library/OFX/Plugins`` on macOS) for ``*.ofx.bundle`` entries.

    Returns a JSON object with a ``plugins`` list, each entry carrying the
    bundle name and its absolute path. A scanned directory that does not exist
    contributes nothing (NOT an error), so a machine with no OFX plugins simply
    returns an empty list.
    """
    try:
        plugins: List[Dict[str, str]] = []
        scanned_roots: List[str] = []
        seen: set = set()
        for root in _ofx_plugin_roots():
            if not os.path.isdir(root):
                continue
            scanned_roots.append(root)
            for dirpath, dirnames, _filenames in os.walk(root):
                # A .ofx.bundle is a directory; record it and do NOT descend
                # into its internals.
                matched = [d for d in dirnames if d.lower().endswith(".ofx.bundle")]
                for d in matched:
                    full = os.path.join(dirpath, d)
                    if full in seen:
                        continue
                    seen.add(full)
                    plugins.append({"name": d, "path": full})
                # Prune matched bundles from further traversal.
                dirnames[:] = [
                    d for d in dirnames if not d.lower().endswith(".ofx.bundle")
                ]

        return json.dumps(
            {
                "success": True,
                "count": len(plugins),
                "scanned_roots": scanned_roots,
                "plugins": plugins,
            },
            indent=2,
            default=str,
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Template control schema (drive an installed template's Inspector controls) ──
# A Fusion template (title/generator/effect) publishes its editable UI as
# InstanceInputs on a MacroOperator inside its .setting; the published input keys
# differ per template so they must be READ, never generalized.
_TEXT_SRC = re.compile(r"StyledText|Text\d*|WordsText")
_COLOR_SRC = re.compile(r"(Red|Green|Blue|Alpha)\d")
_KIND_LABEL = {
    "text": "Text", "color": "Color", "scale": "Scale", "position": "Position",
    "rotation": "Rotation", "opacity": "Opacity/Enable", "font": "Font",
    "style": "Style", "value": "Value",
}


def _field(body: str, key: str) -> str:
    """Extract a scalar field value from a Fusion InstanceInput body."""
    m = re.search(rf'{key}\s*=\s*(?:"([^"]*)"|([^,\n}}]+))', body)
    if not m:
        return ""
    return (m.group(1) if m.group(1) is not None else m.group(2)).strip()


def _control_kind(source: str) -> str:
    """Classify an InstanceInput ``Source`` into a logical control kind."""
    if _TEXT_SRC.fullmatch(source):
        return "text"
    if _COLOR_SRC.match(source):
        return "color"
    if source == "Size":
        return "scale"
    if source == "Center":
        return "position"
    if source == "Angle":
        return "rotation"
    if source == "Blend":
        return "opacity"
    if source == "Font":
        return "font"
    if source == "Style":
        return "style"
    if source == "Value":
        return "value"
    return "other"


def _node_body(txt: str, node: str, cache: Dict[str, str]) -> str:
    """Brace-balanced body of a tool node's definition in the .setting (cached)."""
    if node in cache:
        return cache[node]
    m = re.search(r"\b" + re.escape(node) + r"\s*=\s*\w+\s*\{", txt)
    body = ""
    if m:
        i, depth = m.end() - 1, 0
        for j in range(i, len(txt)):
            if txt[j] == "{":
                depth += 1
            elif txt[j] == "}":
                depth -= 1
                if depth == 0:
                    body = txt[i + 1:j]
                    break
    cache[node] = body
    return body


def _node_input_value(body: str, source: str) -> str:
    """Static default Value of ``source`` on a node (``Input { Value = … }`` or shorthand).

    Returns "" when the input is driven/computed (no static default) — which is why ~1/3 of
    controls correctly have no resolved default.
    """
    m = re.search(r"\b" + re.escape(source) + r"\s*=\s*Input\s*\{(.*?)\}", body, re.S)
    if m:
        v = re.search(r'Value\s*=\s*(?:"([^"]*)"|([^,\n}]+))', m.group(1))
        if v:
            return v.group(1) if v.group(1) is not None else v.group(2).strip()
        return ""
    m2 = re.search(r"\b" + re.escape(source) + r'\s*=\s*(?:"([^"]*)"|([^,\n{}]+))\s*,', body)
    if m2:
        val = m2.group(1) if m2.group(1) is not None else m2.group(2).strip()
        return val if val != "Input" else ""
    return ""


def _parse_template_controls(txt: str) -> Dict[str, Any]:
    """Comprehensive, deduped control schema from a Fusion .setting's published InstanceInputs.

    Colors (R/G/B/A) collapse into ONE logical control with its 4 keys; scale/
    position/etc. stay one control per (node, label). Section headers
    (``CustomLabels`` / ``'<X> Controls'``) are dropped. Keys are the published
    input IDs on the macro — set via ``fusion_set_input(macro_tool, <key>, value)``
    on the placed item. ``text_fields`` is the text subset for convenience.
    """
    m = re.search(r"(\w+)\s*=\s*MacroOperator\s*\{", txt)
    macro_tool = m.group(1) if m else ""
    blocks = re.findall(r"(\w+)\s*=\s*InstanceInput\s*\{(.*?)\}", txt, re.S)
    text_fields: List[Dict[str, str]] = []
    order: List[Any] = []
    groups: Dict[Any, Dict[str, Any]] = {}
    node_cache: Dict[str, str] = {}
    for key, body in blocks:
        source, node = _field(body, "Source"), _field(body, "SourceOp")
        name = _field(body, "Name")
        # Resolved default: the InstanceInput's own Default if present, else the node-level
        # static input value (recovers text placeholders like "Cinematographer"; "" for a
        # driven/computed input, so ~1/3 of controls correctly have no static default).
        default = _field(body, "Default") or _node_input_value(
            _node_body(txt, node, node_cache), source
        )
        if node == "CustomLabels" or name.endswith("Controls"):
            continue  # section header, not a control
        kind = _control_kind(source)
        if kind == "text":
            text_fields.append({"input": key, "node": node, "default": default})
        label = name or (f"Text ({node})" if kind == "text" else _KIND_LABEL.get(kind, source))
        gk = (node, label, kind)
        if gk not in groups:
            groups[gk] = {"name": label, "node": node, "kind": kind, "keys": [], "default": default}
            order.append(gk)
        groups[gk]["keys"].append(key)
    # main_inputs = count of published MainInput image inputs. NOT unique to transitions:
    # titles/effects can also expose 2 image inputs, so the "2-input transition" verdict must be
    # gated on the template's CATEGORY (done in get_template_controls, which knows the path).
    main_inputs = len(re.findall(r"MainInput\d*\s*=\s*InstanceInput", txt))
    return {"macro_tool": macro_tool, "n_controls": len(blocks),
            "main_inputs": main_inputs,
            "text_fields": text_fields, "options": [groups[gk] for gk in order]}


def _find_template_setting(name: str, pack: str = "", scope: str = "all"):
    """Return ``(setting_text, pack_filename, member_path)`` for a template by insert name.

    Searches the same ``Fusion/Templates`` roots as :func:`enumerate_templates`;
    opens ``.drfx`` packs to find the matching internal ``<name>.setting``, and
    also matches loose ``.setting`` files. Returns ``(None, "", "")`` when not found.
    """
    target = name.lower() + ".setting"
    for _scope_label, root in _template_roots(scope):
        if not os.path.isdir(root):
            continue
        for dirpath, _dn, filenames in os.walk(root):
            for fname in filenames:
                low = fname.lower()
                if low == target and not pack:
                    with open(os.path.join(dirpath, fname), encoding="utf-8", errors="ignore") as fh:
                        return fh.read(), "", os.path.join(dirpath, fname)
                if low.endswith(".drfx") and (not pack or fname == pack):
                    try:
                        with zipfile.ZipFile(os.path.join(dirpath, fname)) as zf:
                            for mem in zf.namelist():
                                if "__MACOSX" in mem or not mem.lower().endswith(".setting"):
                                    continue
                                if os.path.basename(mem)[: -len(".setting")] == name:
                                    return zf.read(mem).decode("utf-8", "ignore"), fname, mem
                    except (zipfile.BadZipFile, OSError):
                        continue
    return None, "", ""


@mcp.tool()
def get_template_controls(name: str, pack: str = "", scope: str = "all") -> str:
    """Get the exposed Inspector controls of an installed Fusion template (offline; no Resolve).

    A template (title/generator/effect, e.g. a MotionVFX .drfx entry) publishes its UI controls as
    InstanceInputs on a MacroOperator. This returns the addressing you need to configure it:
      - macro_tool: the tool name once placed (== the fusion_set_input/get_input tool_name).
      - text_fields: [{input, node}] for every editable text line.
      - options: comprehensive deduped controls [{name, kind, node, keys, default}] — colors group
        their R/G/B/A into one entry's keys; kinds: text/color/scale/position/rotation/opacity/font/
        style/value/other. Set any via fusion_set_input(macro_tool, <key>, value) on the placed item
        (or set several at once with set_template_fields).
      - main_inputs / two_input_transition: the count of published MainInput image inputs. For a
        TRANSITION, two (two_input_transition=true) means a true edit-point transition Resolve
        auto-wires to both adjacent clips at a cut — GUI-only, no scripting API. Fewer than two
        means a single-input OVERLAY transition (whoosh/zoom/glitch) — scriptable: drop it on an
        upper track spanning the cut, or apply_macro_to_clip onto the incoming clip.

    Parameters:
    - name: template insert name as from enumerate_templates (e.g. "mTuber 4 Lower 01").
    - pack: optional .drfx filename to disambiguate a name present in multiple packs.
    - scope: "all" (default), "shipped", or "user".
    """
    try:
        canonical, err = _check_choice(scope, ("all", "shipped", "user"), "scope")
        if err:
            return f"Error: {err}"
        txt, found_pack, member = _find_template_setting(name, pack, canonical)
        if txt is None:
            return (f"Error: template {name!r} not found in {canonical} templates. Use "
                    "enumerate_templates() for exact names (and pack= to disambiguate).")
        data = _parse_template_controls(txt)
        # A "2-input transition" (auto-wired to both clips at a cut -> GUI-only) is ONLY a real
        # verdict inside the Transitions category: titles/effects also expose 2 image inputs, so
        # gate the flag on category (derived from the member path). category != Transitions, or
        # main_inputs < 2 -> single-input OVERLAY (scriptable: place on an upper track / macro).
        category = _template_category(member.replace(os.sep, "/").split("/"))
        two_input_transition = (
            category.lower() == "transitions" and data.get("main_inputs", 0) >= 2
        )
        return json.dumps({"success": True, "name": name, "pack": found_pack, "member": member,
                           "category": category, "two_input_transition": two_input_transition,
                           **data}, indent=2, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def set_template_fields(
    fields: str,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
    comp_index: int = 1,
    name: str = "",
) -> str:
    """Set several controls on a PLACED Fusion template at once (text lines, colors, scale, …).

    Operates on an already-inserted template (see insert_template_by_name) — it does NOT insert.
    ``fields`` is a JSON object mapping a **logical control name** (case-insensitive, e.g.
    "Text (Header)", "Header Color") OR a **published key** (e.g. "Input7") -> value. Logical names
    are resolved to published keys via the template's schema (get_template_controls). A color
    control accepts a comma value "r,g,b" or "r,g,b,a" spread across its keys, or you may target one
    channel by its key. Returns per-field {key, ok, readback}.

    Parameters:
    - fields: JSON object {control_name_or_key: value}.
    - track_type / track_index / item_index: locate the placed template item.
    - comp_index: 1-based Fusion composition index on the item (default 1).
    - name: template insert name for schema lookup; defaults to the placed item's name.
    """
    try:
        try:
            mapping = json.loads(fields) if isinstance(fields, str) else dict(fields)
            if not isinstance(mapping, dict):
                raise ValueError
        except Exception:
            return "Error: fields must be a JSON object of {control_name_or_key: value}."

        item = _get_timeline_item(track_type, track_index, item_index)
        tpl_name = name or item.GetName()
        txt, _pack, _member = _find_template_setting(tpl_name)
        if txt is None:
            return (f"Error: could not find the template schema for placed item {tpl_name!r}. "
                    "Pass name= with the template's insert name (see enumerate_templates).")
        schema = _parse_template_controls(txt)
        macro = schema["macro_tool"]
        comp = _get_fusion_comp(item, comp_index)
        tool = _resolve_tool(comp, macro)

        by_label = {o["name"].lower(): o for o in schema["options"]}
        results: List[Dict[str, Any]] = []
        for field, value in mapping.items():
            opt = by_label.get(str(field).lower())
            keys = opt["keys"] if opt else ([field] if re.fullmatch(r"\w+", str(field)) else [])
            if not keys:
                results.append({"field": field, "ok": False, "error": "unknown control name/key"})
                continue
            is_color = bool(opt) and opt["kind"] == "color" and "," in str(value)
            vals = [v.strip() for v in str(value).split(",")] if is_color else [str(value)]
            for i, key in enumerate(keys):
                v = vals[i] if i < len(vals) else vals[-1]
                res = _set_tool_input(tool, key, v)
                results.append({"field": field, "key": key, **res})
        return json.dumps({"success": True, "template": tpl_name, "macro_tool": macro,
                           "results": results}, indent=2, default=str, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── MotionVFX comp cache + placement (titles/generators, effects, classifier) ──
# The MediaIn count of a placed element's Fusion comp is the ONE classifier: 0 =
# title/generator (carrier on an upper track V2+), 1 = effect (on the clip's own
# track), 2 = transition (offline .drt injection). A raw .drfx .setting renders
# BLACK (bare MacroOperator, no output node) — so a title/generator comp must be
# captured by exporting a NATIVELY-inserted item (which gains the real
# ``MediaOut1 = Saver``) and an effect comp by exporting a GUI-placed reference
# clip (which carries the wired ``MediaSource`` + ``MediaOut1``). Both are cached
# to a per-package cache dir keyed by name and re-imported onto a carrier / clip.


def _comp_cache_dir() -> str:
    """Return (creating if needed) the Fusion comp cache directory.

    ``$DAVINCI_MCP_CACHE_DIR`` overrides the default
    ``~/.davinci-resolve-mcp/comp-cache``.
    """
    cache_dir = os.environ.get("DAVINCI_MCP_CACHE_DIR") or os.path.expanduser(
        "~/.davinci-resolve-mcp/comp-cache"
    )
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def _comp_cache_path(name: str) -> str:
    """Absolute cache path for a template/effect comp keyed by ``name``.

    The filename is ``name`` with every character outside ``[A-Za-z0-9._-]``
    replaced by ``_``, followed by a short hash of the EXACT ``name`` and a
    ``.comp`` suffix; a pre-existing (non-empty) file means the comp is already
    cached (the cache is idempotent). The hash disambiguates distinct names
    (e.g. ``"mTuber 4 / Chapter"`` vs ``"mTuber-4-Chapter"``) that would sanitise
    to the same slug, so they never collide onto one cache file.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return os.path.join(_comp_cache_dir(), f"{safe}-{digest}.comp")


def _atomic_export_comp(item, comp_path: str) -> bool:
    """Export ``item``'s Fusion comp to ``comp_path`` atomically.

    ``ExportFusionComp`` is written to a temp file in the same cache dir and only
    ``os.replace``-d onto ``comp_path`` when the export returned truthy — so an
    interrupted or falsy export never leaves a truncated/poisoned file at the
    final path (which the existence-based idempotency check would then trust,
    causing BLACK renders). Returns the export's truthiness.
    """
    tmp_path = f"{comp_path}.{os.getpid()}.tmp"
    try:
        exported = bool(item.ExportFusionComp(tmp_path, 1))
        if exported and os.path.isfile(tmp_path) and os.path.getsize(tmp_path) > 0:
            os.replace(tmp_path, comp_path)
            return True
        return False
    finally:
        if os.path.isfile(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _get_or_create_scratch_timeline(project, media_pool, tl_name: str):
    """Return the named scratch timeline, reusing it if present else creating one.

    Used by :func:`cache_template_comp` so the one-time native insert+export never
    disturbs a real edit. Returns None only when ``CreateEmptyTimeline`` fails.
    """
    try:
        count = project.GetTimelineCount() or 0
        for i in range(1, int(count) + 1):
            tl = project.GetTimelineByIndex(i)
            try:
                if tl is not None and tl.GetName() == tl_name:
                    return tl
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return media_pool.CreateEmptyTimeline(tl_name)


def _first_video_carrier(min_frames: int):
    """First Media Pool VIDEO clip whose source length >= ``min_frames`` (recursive), or None.

    The carrier's own media is irrelevant — the imported comp generates the graphic
    with alpha — so any long-enough video clip works. Audio-only items (no
    resolution / an audio ``Type``) are skipped.
    """
    mp = _media_pool()
    root = mp.GetRootFolder()
    if root is None:
        return None
    stack = [root]
    while stack:
        folder = stack.pop()
        for clip in folder.GetClipList() or []:
            try:
                frames = clip.GetClipProperty("Frames")
                frames = int(frames) if str(frames).strip() else 0
            except Exception:  # noqa: BLE001
                frames = 0
            if frames < int(min_frames):
                continue
            try:
                res = clip.GetClipProperty("Resolution")
            except Exception:  # noqa: BLE001
                res = ""
            try:
                ctype = clip.GetClipProperty("Type") or ""
            except Exception:  # noqa: BLE001
                ctype = ""
            if str(res).strip() or "video" in str(ctype).lower():
                return clip
        for sub in folder.GetSubFolderList() or []:
            stack.append(sub)
    return None


def _item_index_on_track(track_type: str, track_index: int, item) -> int:
    """0-based index of ``item`` on its track (matched by timeline start), or -1.

    Lets :func:`place_overlay_title` address a freshly-appended carrier via the same
    ``(track_type, track_index, item_index)`` locator :func:`attach_fusion_comp` /
    :func:`set_template_fields` take. Items on one video track cannot overlap, so the
    timeline start frame is a unique key.
    """
    conn = _conn()
    timeline = _require_timeline(conn)
    items = timeline.GetItemListInTrack(track_type, track_index) or []
    try:
        target_start = item.GetStart()
    except Exception:  # noqa: BLE001
        target_start = None
    for idx, it in enumerate(items):
        try:
            if it.GetStart() == target_start:
                return idx
        except Exception:  # noqa: BLE001
            continue
    return -1


def _maybe_json(text: str):
    """Return ``text`` parsed as JSON when it is a JSON payload, else the raw string."""
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


def _count_media_in_in_comp_text(txt: str) -> int:
    r"""Count ``MediaIn`` tool declarations in raw Fusion comp/setting text (offline classifier).

    Returns ``len(re.findall(r"= MediaIn \{", txt))`` — the same 0/1/2 lane signal
    :func:`classify_timeline_element` derives live from ``GetToolList``, but from file
    text so the offline fixture corpus can validate the MediaIn rule with no Resolve
    running (0 = title/generator, 1 = effect, 2 = transition).
    """
    return len(re.findall(r"= MediaIn \{", txt))


@mcp.tool()
def cache_template_comp(name: str, pack: str = "") -> str:
    """Capture (once) a correctly-wired title/generator Fusion comp for a template.

    A raw ``.drfx`` ``.setting`` is a bare ``MacroOperator`` with no output node and
    renders BLACK when imported. Resolve's native insert wraps it with a real output
    (``MediaOut1 = Saver``, ``Source="Output"``) — so this inserts the template
    natively on a hidden scratch timeline (``_mcp_comp_cache (scratch)``) and exports
    that item's comp, giving a comp that actually renders.

    Idempotent: if the cache file already exists it is returned without re-exporting
    (``cached: true``). Otherwise the template is inserted (via the same resolution +
    Insert* path as :func:`insert_template_by_name`, ``kind="fusion_title"``),
    ``ExportFusionComp`` writes the cache, the scratch item is cleaned up, and the
    result carries ``cached: false``. LIVE-ONLY — returns ``Error: ...`` when Resolve,
    the insert, or the export is unavailable.

    Parameters:
    - name: the template insert/display name (as from ``enumerate_templates``).
    - pack: optional source ``.drfx`` (advisory; the native insert resolves by name).

    Returns JSON ``{success, name, comp_path, cached}`` or an ``Error: ...`` string.
    """
    try:
        if not name or not name.strip():
            return "Error: name is required (the template's insert/display name)."

        comp_path = _comp_cache_path(name)
        if os.path.isfile(comp_path) and os.path.getsize(comp_path) > 0:
            return json.dumps(
                {"success": True, "name": name, "comp_path": comp_path,
                 "cached": True},
                indent=2,
            )

        conn = _conn()
        project = conn.get_project()
        media_pool = _media_pool()

        # Capture the caller's current timeline BEFORE creating the scratch:
        # _get_or_create_scratch_timeline ends in CreateEmptyTimeline, which makes
        # the NEW (scratch) timeline current, so capturing afterwards on a first-ever
        # call records the scratch ITSELF and the finally-restore becomes a no-op that
        # strands the caller on the 1-track scratch (breaking place_overlay_title).
        prev_timeline = None
        try:
            prev_timeline = project.GetCurrentTimeline()
        except Exception:  # noqa: BLE001
            prev_timeline = None

        scratch_name = "_mcp_comp_cache (scratch)"
        scratch = _get_or_create_scratch_timeline(project, media_pool, scratch_name)
        if scratch is None:
            return (
                f"Error: could not create the scratch timeline '{scratch_name}' "
                f"for the one-time comp capture."
            )

        exported = False
        try:
            project.SetCurrentTimeline(scratch)
            item, _retried = _insert_template_item(
                scratch, "fusion_title", name.strip()
            )
            if item is None:
                return (
                    f"Error: template not resolved — '{name}' did not insert as a "
                    f"fusion_title. Check the name exists in the Edit page "
                    f"Titles/Generators list."
                )

            # The native export gains the real MediaOut1 = Saver the raw .setting
            # lacks (a raw .setting renders BLACK). Atomic: only lands on
            # comp_path when the export succeeded, so a partial write can't poison
            # the existence-based cache.
            exported = _atomic_export_comp(item, comp_path)

            # Clean up the scratch item (best effort — the export is what matters).
            try:
                scratch.DeleteClips([item])
            except Exception:  # noqa: BLE001
                pass
        finally:
            # Restore whatever timeline was current before the capture.
            if prev_timeline is not None:
                try:
                    project.SetCurrentTimeline(prev_timeline)
                except Exception:  # noqa: BLE001
                    pass

        if not exported:
            return (
                f"Error: ExportFusionComp returned falsy for '{name}' — no comp "
                f"cached. Confirm the template inserted on the scratch timeline."
            )

        return json.dumps(
            {"success": True, "name": name, "comp_path": comp_path,
             "cached": False},
            indent=2,
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def place_overlay_title(
    name: str,
    track_index: int,
    record_frame: int,
    duration_frames: int,
    fields: str = "",
    carrier_clip: str = "",
    pack: str = "",
) -> str:
    """Place a MotionVFX title/generator as an overlay on an upper track (V2+).

    Full chain: ensure the template's comp is cached (:func:`cache_template_comp`),
    place a video-only carrier of ``duration_frames`` on ``track_index`` at
    ``record_frame`` (the ``AppendToTimeline`` ``mediaType:1`` mechanism shared with
    :func:`append_template_with_placement`), import the cached comp onto that carrier
    (:func:`attach_fusion_comp`), and — when ``fields`` is given — apply them via
    :func:`set_template_fields` (``name=`` is passed because the carrier item's name
    is NOT the template name). Overlays NEVER go on V1, so ``track_index`` must be
    >= 2; ``duration_frames`` must be >= 4.

    Parameters:
    - name: template insert/display name.
    - track_index: 1-based OVERLAY video track (>= 2, never V1).
    - record_frame: timeline frame to start the overlay at.
    - duration_frames: on-screen length in frames (>= 4).
    - fields: optional JSON ``{control_name_or_key: value}`` for the template.
    - carrier_clip: optional Media Pool clip to use as the carrier; when empty the
      first video clip with a long-enough source is auto-picked.
    - pack: optional source ``.drfx`` (forwarded to cache_template_comp; advisory).

    Returns JSON with the placed locator + a ``verify_hint``, or an ``Error: ...``
    string. (Confirming V1/A1 item counts are unchanged is the caller's check.)
    """
    try:
        if track_index < 2:
            return (
                f"Error: track_index must be >= 2 — overlays never go on V1 "
                f"(the footage track). Got {track_index}."
            )
        if duration_frames < 4:
            return f"Error: duration_frames must be >= 4 (got {duration_frames})."

        # place_overlay_title has no timeline arg — it operates on whatever is
        # current. Capture the caller's timeline up front so we can re-assert it
        # after cache_template_comp (which switches to a scratch timeline to do
        # its one-time capture) and guarantee the carrier lands on the caller's
        # timeline even if caching left current elsewhere. Best-effort; never raise.
        cur = None
        try:
            cur = _conn().get_project().GetCurrentTimeline()
        except Exception:  # noqa: BLE001
            cur = None

        cache_result = cache_template_comp(name, pack)
        if cache_result.startswith("Error"):
            return cache_result
        try:
            comp_path = json.loads(cache_result)["comp_path"]
        except (ValueError, KeyError, TypeError):
            return (
                f"Error: could not obtain a cached comp path for '{name}': "
                f"{cache_result}"
            )

        # Re-assert the caller's timeline before appending the carrier, so the
        # carrier can never land on the comp-cache scratch timeline.
        if cur is not None:
            try:
                _conn().get_project().SetCurrentTimeline(cur)
            except Exception:  # noqa: BLE001
                pass

        if carrier_clip and carrier_clip.strip():
            clip = _find_media_pool_item(carrier_clip.strip())
            if clip is None:
                return (
                    f"Error: carrier_clip '{carrier_clip}' not found in the Media "
                    f"Pool. Import it or check the name."
                )
        else:
            clip = _first_video_carrier(duration_frames)
            if clip is None:
                return (
                    f"Error: no Media Pool video clip with a source length >= "
                    f"{duration_frames} frames found to use as a carrier. Pass "
                    f"carrier_clip= with a long-enough clip."
                )

        item = _append_video_carrier(
            clip, 0, duration_frames, track_index, record_frame, label=name
        )

        item_index = _item_index_on_track("video", track_index, item)
        if item_index < 0:
            return (
                f"Error: placed the carrier but could not locate it on video "
                f"track {track_index} to import the comp."
            )

        attach_result = attach_fusion_comp(
            "video", track_index, item_index, comp_path
        )
        if attach_result.startswith("Error"):
            return attach_result

        fields_result = None
        fields_ok = True
        if fields and fields.strip():
            # name= is REQUIRED: the carrier item's name != the template name.
            fields_result = _maybe_json(
                set_template_fields(
                    fields, "video", track_index, item_index, 1, name
                )
            )
            # set_template_fields returns an "Error: ..." string on schema/control
            # failure; surface that so a caller doesn't read placement-only as
            # fully configured.
            fields_ok = not (
                isinstance(fields_result, str)
                and fields_result.startswith("Error")
            )

        try:
            carrier_name = clip.GetName()
        except Exception:  # noqa: BLE001
            carrier_name = ""

        return json.dumps(
            {
                "success": True,
                "name": name,
                "comp_path": comp_path,
                "track_type": "video",
                "track_index": track_index,
                "item_index": item_index,
                "record_frame": record_frame,
                "duration_frames": duration_frames,
                "carrier_clip": carrier_name,
                "fields_result": fields_result,
                "fields_ok": fields_ok,
                "verify_hint": (
                    "export_current_frame at a hold frame (titles are kinetic — "
                    "mid-sweep frames may look empty)."
                ),
            },
            indent=2,
            default=str,
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def animate_clip_transform(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
    zoom_from: float = 1.0,
    zoom_to: float = 1.08,
    center_from: str = "0.5,0.5",
    center_to: str = "0.5,0.5",
    carrier_track: int = 0,
) -> str:
    """Animate a Ken-Burns / motivated push-in on a clip (the ONLY scriptable path).

    Two Resolve API walls block the "obvious" route: ``TimelineItem.AddKeyframe`` does
    not exist, and ``AddFusionComp``/``ImportFusionComp`` FAIL on a base footage clip
    with linked audio — so animated transform keyframes cannot be attached to the
    footage clip itself. The bypass: build a keyframed ``.comp`` whose keyframes live
    INSIDE the file (:func:`build_kenburns_comp`), place a VIDEO-ONLY carrier of the
    same source range on an upper track, and import the comp onto the carrier. Resolve
    rebinds ``MediaIn1`` to the carrier's footage, so the keyframed ``Transform`` zooms
    the real frames while the base clip's audio is untouched (``mediaType:1``).

    This adds ONE extra video layer per animated clip. When no MOTION is wanted, a
    STATIC punch-in is cheaper: :func:`set_transform` / ``SetProperty("ZoomX"/"ZoomY")``
    directly on the clip needs no carrier.

    Parameters:
    - track_type/track_index/item_index: locate the target clip to animate.
    - zoom_from/zoom_to: Transform ``Size`` at start/end (1.0 = none; 1.08 = +8%).
    - center_from/center_to: ``"cx,cy"`` (0..1) pan endpoints (0.5,0.5 = centre).
    - carrier_track: 1-based video track for the carrier; 0 = auto-pick the first free
      upper video track above the clip (adding one if needed). NEVER the clip's own track.

    Returns JSON ``{success, carrier_track, carrier_item_index, zoom_from, zoom_to,
    verify_hint}`` or an ``Error: ...`` string.
    """
    try:
        def _parse_center(s: str, label: str):
            parts = str(s).split(",")
            if len(parts) != 2:
                raise RuntimeError(
                    f"{label} must be 'cx,cy' (two floats), got {s!r}."
                )
            return float(parts[0].strip()), float(parts[1].strip())

        cx_from, cy_from = _parse_center(center_from, "center_from")
        cx_to, cy_to = _parse_center(center_to, "center_to")

        item = _get_timeline_item(track_type, track_index, item_index)
        if item is None:
            return (
                f"Error: no timeline item at {track_type} track {track_index} "
                f"index {item_index}."
            )

        # Source range + timeline placement of the target clip.
        try:
            src = int(item.GetLeftOffset())
        except Exception:  # noqa: BLE001
            src = 0
        try:
            start = int(item.GetStart())
        except Exception:  # noqa: BLE001
            start = 0
        try:
            dur = int(item.GetDuration())
        except Exception:  # noqa: BLE001
            dur = 0
        if dur <= 0:
            try:
                dur = int(item.GetEnd()) - start
            except Exception:  # noqa: BLE001
                dur = 0
        if dur < 4:
            return (
                f"Error: target clip duration {dur} frame(s) is too short to "
                f"animate (need >= 4)."
            )

        # The carrier MUST show the SAME frames as the target clip, so use the
        # target's OWN Media Pool source.
        carrier_source = None
        get_mpi = getattr(item, "GetMediaPoolItem", None)
        if callable(get_mpi):
            try:
                carrier_source = get_mpi()
            except Exception:  # noqa: BLE001
                carrier_source = None
        if carrier_source is None:
            return (
                "Error: could not get the target clip's Media Pool item "
                "(GetMediaPoolItem unavailable) — cannot build a matching carrier."
            )

        # Build the keyframed comp text and write it to the comp-cache dir.
        comp_text = build_kenburns_comp(
            dur, zoom_from, zoom_to, cx_from, cy_from, cx_to, cy_to
        )
        comp_name = (
            f"kenburns-t{track_index}-i{item_index}-s{start}-"
            f"{zoom_from}-{zoom_to}"
        )
        comp_path = os.path.join(
            _comp_cache_dir(),
            re.sub(r"[^A-Za-z0-9._-]", "_", comp_name) + ".comp",
        )
        with open(comp_path, "w", encoding="utf-8") as fh:
            fh.write(comp_text)

        conn = _conn()
        timeline = _require_timeline(conn)
        try:
            vtracks = int(timeline.GetTrackCount("video") or 0)
        except Exception:  # noqa: BLE001
            vtracks = 0

        def _upper_track_free(track_no: int) -> bool:
            """True if no item on video ``track_no`` overlaps [start, start+dur)."""
            try:
                items = timeline.GetItemListInTrack("video", track_no) or []
            except Exception:  # noqa: BLE001
                return False
            end = start + dur
            for it in items:
                try:
                    if int(it.GetStart()) < end and int(it.GetEnd()) > start:
                        return False
                except Exception:  # noqa: BLE001
                    return False
            return True

        # Determine the carrier track. Must be an UPPER track above the clip.
        if carrier_track >= 1:
            target_track = int(carrier_track)
            if target_track <= track_index:
                return (
                    f"Error: carrier_track {carrier_track} must be an UPPER video "
                    f"track ABOVE the clip's own track ({track_index}); a same or "
                    f"lower track would be occluded by the clip."
                )
            while vtracks < target_track:
                if not timeline.AddTrack("video"):
                    return (
                        f"Error: could not add video tracks up to carrier_track "
                        f"{target_track} (timeline has {vtracks})."
                    )
                vtracks += 1
        else:
            # Auto-pick the first FREE upper video track above the clip's track;
            # if none of the existing upper tracks is free, add a fresh one on top.
            target_track = 0
            for t in range(track_index + 1, vtracks + 1):
                if _upper_track_free(t):
                    target_track = t
                    break
            if target_track == 0:
                if not timeline.AddTrack("video"):
                    return (
                        f"Error: could not add an upper video track for the carrier "
                        f"(timeline has {vtracks})."
                    )
                vtracks += 1
                target_track = vtracks

        # Place a video-only carrier of the SAME source range on the carrier track.
        carrier = _append_video_carrier(
            carrier_source, src, src + dur, target_track, start,
            label=f"kenburns carrier (item {item_index})",
        )
        carrier_item_index = _item_index_on_track("video", target_track, carrier)
        if carrier_item_index < 0:
            return (
                f"Error: placed the carrier but could not locate it on video "
                f"track {target_track} to import the comp."
            )

        attach_result = attach_fusion_comp(
            "video", target_track, carrier_item_index, comp_path
        )
        if attach_result.startswith("Error"):
            return attach_result

        return json.dumps(
            {
                "success": True,
                "carrier_track": target_track,
                "carrier_item_index": carrier_item_index,
                "zoom_from": zoom_from,
                "zoom_to": zoom_to,
                "verify_hint": (
                    "export_current_frame at start vs end — footage should zoom "
                    f"{zoom_from}->{zoom_to}"
                ),
            },
            indent=2,
            default=str,
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def cache_effect_comp(
    name: str,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Capture a 1-MediaIn effect comp from a GUI-placed reference clip.

    A MotionVFX effect placed on a clip is a 1-MediaIn Fusion comp
    (``MediaSource`` -> macro -> ``MediaOut1 = Saver``). Exporting that already-wired
    comp yields a reusable effect :func:`apply_clip_effect` can import onto any clip —
    the raw ``.setting`` macro alone renders BLACK. ``ExportFusionComp`` returns FALSE
    for a 2-MediaIn transition (it cannot carry both neighbour feeds), reported as a
    clear error. Cached keyed by ``name``.

    Parameters:
    - name: effect name to key the cache by (the macro/template name).
    - track_type / track_index / item_index: locate the GUI-placed reference clip.

    Returns JSON ``{success, name, comp_path}`` or an ``Error: ...`` string.
    """
    try:
        if not name or not name.strip():
            return "Error: name is required (the effect name to key the cache by)."

        comp_path = _comp_cache_path(name)
        item = _get_timeline_item(track_type, track_index, item_index)
        exported = _atomic_export_comp(item, comp_path)
        if not exported:
            return (
                f"Error: '{name}' did not export (a 2-MediaIn transition can't "
                f"export standalone — use place_motionvfx_transition / the .drt "
                f"route)."
            )
        return json.dumps(
            {"success": True, "name": name, "comp_path": comp_path}, indent=2
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def apply_clip_effect(
    name: str,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
    fields: str = "",
) -> str:
    """Apply a cached effect comp onto a target clip (on the clip's OWN track).

    An effect processes the clip it is ON — so it is imported onto the target clip's
    own track, NOT an upper carrier (that is the title/generator lane). Looks up the
    cached ``.comp`` for ``name`` (run :func:`cache_effect_comp` first if missing),
    imports it via :func:`attach_fusion_comp`, and — when ``fields`` is given — applies
    them via :func:`set_template_fields`.

    Parameters:
    - name: the cached effect name (see :func:`cache_effect_comp`).
    - track_type / track_index / item_index: locate the TARGET clip.
    - fields: optional JSON ``{control_name_or_key: value}`` for the effect's controls.

    Returns JSON with the applied locator + a ``verify_hint``, or an ``Error: ...``
    string.
    """
    try:
        if not name or not name.strip():
            return "Error: name is required (the cached effect name)."

        comp_path = _comp_cache_path(name)
        if not os.path.isfile(comp_path):
            return (
                f"Error: no cached effect comp for '{name}' at {comp_path}. Run "
                f"cache_effect_comp('{name}', ...) from a GUI-placed reference "
                f"clip first."
            )

        attach_result = attach_fusion_comp(
            track_type, track_index, item_index, comp_path
        )
        if attach_result.startswith("Error"):
            return attach_result

        fields_result = None
        fields_ok = True
        if fields and fields.strip():
            fields_result = _maybe_json(
                set_template_fields(
                    fields, track_type, track_index, item_index, 1, name
                )
            )
            # An "Error: ..." string means field-setting did nothing; flag it so
            # a caller doesn't read attach-only as fully configured.
            fields_ok = not (
                isinstance(fields_result, str)
                and fields_result.startswith("Error")
            )

        return json.dumps(
            {
                "success": True,
                "name": name,
                "comp_path": comp_path,
                "track_type": track_type,
                "track_index": track_index,
                "item_index": item_index,
                "fields_result": fields_result,
                "fields_ok": fields_ok,
                "verify_hint": (
                    "export_current_frame (this render path is not yet "
                    "live-verified for a 1-MediaIn effect)."
                ),
            },
            indent=2,
            default=str,
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def classify_timeline_element(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Classify a placed timeline element by its Fusion comp's MediaIn count.

    The universal MotionVFX classifier: every placed element is a clip Fusion comp;
    counting its ``MediaIn`` tools picks the placement lane — 0 = title/generator
    (upper-track carrier, V2+), 1 = effect (apply on the clip's own track), 2 =
    transition (offline ``.drt`` injection). Reads ONLY ``GetToolList`` + per-tool
    ``GetAttrs`` (+ the macro's ``.Name``); it never walks tool input lists, which
    hangs Resolve. An item with no Fusion comp is reported as ``has_comp: false``.

    Parameters:
    - track_type / track_index / item_index: locate the timeline element.

    Returns JSON ``{success, has_comp, macro, media_in, lane}`` (or ``{success,
    has_comp: false, note}`` when there is no comp), or an ``Error: ...`` string.
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)

        comp = None
        try:
            comp = item.GetFusionCompByIndex(1)
        except Exception:  # noqa: BLE001
            comp = None
        if comp is None:
            return json.dumps(
                {"success": True, "has_comp": False,
                 "note": "not a template element"},
                indent=2,
            )

        tools = comp.GetToolList(False) or {}
        macro: Optional[str] = None
        media_in = 0
        for tool in tools.values():
            try:
                reg_id = tool.GetAttrs("TOOLS_RegID")
            except Exception:  # noqa: BLE001
                reg_id = None
            if reg_id == "MacroOperator":
                if macro is None:
                    try:
                        macro = tool.Name
                    except Exception:  # noqa: BLE001
                        macro = None
            elif reg_id == "MediaIn":
                media_in += 1

        lane = {
            0: "title_generator (place on an upper-track carrier, V2+)",
            1: "effect (apply on the clip's own track)",
            2: "transition (offline .drt injection)",
        }.get(media_in, "unknown")

        return json.dumps(
            {
                "success": True,
                "has_comp": True,
                "macro": macro,
                "media_in": media_in,
                "lane": lane,
            },
            indent=2,
            default=str,
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def get_resolvefx_registry() -> str:

    path (so it works pip-installed, NOT cwd-relative) and returns one entry per
    ResolveFX plugin. Each entry carries:
    - ``name``   — the registry key (short plugin name);
    - ``wireId`` — the ``.drx`` wire id (no ``ofx.`` prefix);
    - ``regid``  — the Fusion RegID ``'ofx.' + wireId`` that
      :func:`apply_ofx_to_clip` / ``comp.AddTool`` require;
    - ``pluginId``, ``candidateQuality`` — passthrough when present.

    No running Resolve required. The purely-informational ``_meta`` block of the
    file (if present) is skipped and surfaced separately under ``meta``.

    Returns a JSON object with a ``plugins`` list, or an ``Error: ...`` string
    """
    try:
        path = _registry_json_path()
        if not os.path.isfile(path):
            return (
                f"The data/resolvefx-registry.json file should ship with the "
                f"package."
            )
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)

        if not isinstance(data, dict):
            return (
                "Error: resolvefx-registry.json is not a JSON object of "
                "name -> entry."
            )

        meta = data.get("_meta")
        plugins: List[Dict[str, Any]] = []
        for name, entry in data.items():
            if name == "_meta" or not isinstance(entry, dict):
                continue
            wire_id = entry.get("wireId", "")
            reg_id = (
                wire_id
                if str(wire_id).startswith("ofx.")
                else f"ofx.{wire_id}"
            ) if wire_id else ""
            plugins.append(
                {
                    "name": name,
                    "wireId": wire_id,
                    "regid": reg_id,
                    "pluginId": entry.get("pluginId", wire_id),
                    "candidateQuality": entry.get("candidateQuality"),
                }
            )

        return json.dumps(
            {
                "success": True,
                "count": len(plugins),
                "source": path,
                "meta": meta,
                "plugins": plugins,
            },
            indent=2,
            default=str,
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def discover_regid(tool_name: str = "") -> str:
    """Read a LIVE Fusion tool's ``TOOLS_RegID`` (ground-truth RegID).

    Resolve running), this reads ground truth from a running Resolve: it opens
    the Fusion page (``resolve.OpenPage('fusion')``), gets the current Fusion
    composition, locates a tool, and returns its ``GetAttrs()['TOOLS_RegID']``.

    Parameters:
    - tool_name: name of a tool in the current comp to read (e.g. ``"Blur1"``).
      When empty, the comp's active tool is used, falling back to the first tool
      in the comp.

    Returns a JSON object with the tool's ``reg_id`` (and the ``ofx.``-stripped
    ``wireId`` when it is an OFX RegID), or a clear ``Error: ...`` string —
    including a "no Fusion comp / open the Fusion page first" message when
    Resolve is unreachable, Fusion is unavailable, or there is no current comp.
    """
    try:
        try:
            resolve = _conn().get_resolve()
        except Exception as e:  # noqa: BLE001 - Resolve unreachable
            return (
                "Error: no Fusion comp — could not reach DaVinci Resolve "
                f"({e}). Open the Fusion page in a running Resolve first."
            )
        if resolve is None:
            return (
                "Error: no Fusion comp — DaVinci Resolve is not reachable. "
                "Open the Fusion page in a running Resolve first."
            )

        try:
            resolve.OpenPage("fusion")
        except Exception:  # noqa: BLE001 - opening the page is best-effort
            pass

        fusion = None
        try:
            fusion = resolve.Fusion()
        except Exception:  # noqa: BLE001
            fusion = None
        if fusion is None:
            return (
                "Error: no Fusion comp — Fusion is not available. Open the "
                "Fusion page in a running Resolve first."
            )

        comp = None
        try:
            comp = fusion.GetCurrentComp()
        except Exception:  # noqa: BLE001
            comp = None
        if comp is None:
            return (
                "Error: no Fusion comp — there is no current Fusion "
                "composition. Open the Fusion page first (select a clip and "
                "switch to Fusion)."
            )

        tool = None
        wanted = (tool_name or "").strip()
        if wanted:
            try:
                tool = comp.FindTool(wanted)
            except Exception:  # noqa: BLE001
                tool = None
            if tool is None:
                return (
                    f"Error: no tool named '{wanted}' in the current Fusion "
                    f"comp. Check the tool name or leave it empty to read the "
                    f"active tool."
                )
        else:
            try:
                tool = comp.ActiveTool
            except Exception:  # noqa: BLE001
                tool = None
            if tool is None:
                try:
                    tools = comp.GetToolList() or {}
                except Exception:  # noqa: BLE001
                    tools = {}
                for candidate in tools.values():
                    tool = candidate
                    break
            if tool is None:
                return (
                    "Error: the current Fusion comp has no tools to read a "
                    "RegID from. Add a node first."
                )

        attrs = {}
        try:
            attrs = tool.GetAttrs() or {}
        except Exception:  # noqa: BLE001
            attrs = {}
        reg_id = attrs.get("TOOLS_RegID", "")
        if not reg_id:
            return (
                "Error: could not read TOOLS_RegID from the selected Fusion "
                "tool."
            )

        wire_id = reg_id[len("ofx."):] if str(reg_id).startswith("ofx.") else None
        return json.dumps(
            {
                "success": True,
                "reg_id": reg_id,
                "wireId": wire_id,
                "tool_name": attrs.get("TOOLS_Name", wanted or ""),
                "is_ofx": bool(wire_id is not None),
            },
            indent=2,
            default=str,
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Custom effect authoring / install (filesystem writes; no Resolve) ────────
# install_dctl / install_fuse drop a DCTL or Fuse SOURCE STRING into the exact
# directory Resolve/Fusion scans, so a custom shader/plugin becomes available
# without a running Resolve. They deliberately do NOT compile GLSL/DCTL/Lua —
# a minimal non-empty source guard is all that runs; the follow-up step
# (refresh_luts for a regular DCTL, a Resolve restart for ACES DCTLs and new
# Fuses) is reported so the caller knows how to make Resolve pick the file up.
# 23392, fuse @ 23156) and its utils/platform.get_resolve_plugin_paths.

# A Fuse's on-disk name IS its class identifier — the Fuse SDK requires a valid
# identifier, and a bad name produces comps that will not reopen.
_FUSE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# DCTL filenames are looser than Fuse identifiers but must still be filesystem-
# safe: no path separators, no leading dot, nothing shell-hostile.
_DCTL_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ \-]{0,127}$")

# The DCTL categories install_dctl accepts and the file extensions it writes.
_DCTL_CATEGORIES = ("lut", "aces_idt", "aces_odt")
_DCTL_VALID_EXTS = (".dctl", ".dctle")


def _plugin_install_paths() -> Dict[str, str]:
    """Return the per-user install directories for Fuses and DCTLs.

    Keys: ``fuses_dir`` (Fusion Fuses), ``dctl_lut_dir`` (regular DCTL/LUT
    folder, picked up by RefreshLUTList), ``aces_idt_dir`` / ``aces_odt_dir``
    (ACES Transforms trees, scanned only at Resolve startup). Paths are the
    per-platform user defaults; the directory is created on write, not here.

    NOTE: the Fuse SDK doc lists Fuses under ``Support/Fusion/Fuses`` on macOS,
    but the directory Resolve actually scans is the sibling ``Fusion/Fuses``
    (no ``Support``) — matching the canonical Fusion user-data layout where
    Macros/Templates/Scripts/Fuses all hang directly off the Fusion user root.
    """
    home = os.path.expanduser("~")
    if sys.platform.startswith("darwin"):
        support = os.path.join(
            home, "Library", "Application Support",
            "Blackmagic Design", "DaVinci Resolve",
        )
        fuses_dir = os.path.join(support, "Fusion", "Fuses")
        dctl_lut_dir = os.path.join(support, "LUT")
        aces_root = os.path.join(support, "ACES Transforms")
    elif sys.platform.startswith("win") or sys.platform.startswith("cygwin"):
        appdata = os.environ.get(
            "APPDATA", os.path.join(home, "AppData", "Roaming")
        )
        support = os.path.join(
            appdata, "Blackmagic Design", "DaVinci Resolve", "Support"
        )
        fuses_dir = os.path.join(support, "Fusion", "Fuses")
        dctl_lut_dir = os.path.join(support, "LUT")
        aces_root = os.path.join(support, "ACES Transforms")
    else:  # Linux / other POSIX
        base = os.path.join(home, ".local", "share", "DaVinciResolve")
        fuses_dir = os.path.join(base, "Fusion", "Fuses")
        dctl_lut_dir = os.path.join(base, "LUT")
        aces_root = os.path.join(base, "ACES Transforms")

    return {
        "fuses_dir": fuses_dir,
        "dctl_lut_dir": dctl_lut_dir,
        "aces_idt_dir": os.path.join(aces_root, "IDT"),
        "aces_odt_dir": os.path.join(aces_root, "ODT"),
    }


def _dctl_root(category: str) -> str:
    """Return the install root directory for a validated DCTL ``category``."""
    paths = _plugin_install_paths()
    if category == "lut":
        return paths["dctl_lut_dir"]
    if category == "aces_idt":
        return paths["aces_idt_dir"]
    return paths["aces_odt_dir"]  # aces_odt


def _resolve_subdir(subdir: Optional[str]) -> Optional[str]:
    """Validate an optional ``subdir`` and return it as a safe relative path.

    Returns None for no subdir. Raises ``ValueError`` on any traversal, hidden,
    or separator-bearing segment so a caller can never escape the install root.
    """
    if not subdir or not str(subdir).strip():
        return None
    normalized = str(subdir).replace("\\", "/")
    parts = [p.strip() for p in normalized.split("/") if p.strip()]
    if not parts:
        return None
    for part in parts:
        if part in (".", "..") or "/" in part or "\\" in part:
            raise ValueError(f"Unsafe subdir segment: '{part}'")
        if part.startswith("."):
            raise ValueError(f"Hidden subdir not allowed: '{part}'")
    return os.path.join(*parts)


@mcp.tool()
def install_dctl(
    name: str,
    source: str,
    category: str = "lut",
    subdir: str = "",
    ext: str = ".dctl",
    overwrite: bool = False,
) -> str:
    """Write a DCTL source file into Resolve's LUT or ACES Transforms tree.

    Filesystem write ONLY — no running Resolve required. The DCTL ``source`` is
    written verbatim (NO GLSL/DCTL compilation is attempted); only a minimal
    non-empty guard runs. Where it lands and what makes Resolve pick it up
    depends on ``category``:
    - "lut"      -> the regular LUT directory; call ``refresh_luts`` afterwards
      and it appears in the LUT browser / Clip-Node LUT picker / ResolveFX DCTL.
    - "aces_idt" -> ``ACES Transforms/IDT`` (a separate tree scanned ONLY at
      Resolve startup — a Resolve RESTART is required, not a LUT refresh).
    - "aces_odt" -> ``ACES Transforms/ODT`` (same restart caveat as IDT).

    Parameters:
    - name: filesystem-safe identifier — must match
      ``[A-Za-z0-9_][A-Za-z0-9_ -]{0,127}`` (no path separators, no leading
      dot). An unsafe or empty name is rejected and nothing is written.
    - source: the DCTL source as a string. An empty/whitespace value is
      rejected BEFORE any file is created.
    - category: "lut" (default), "aces_idt", or "aces_odt".
    - subdir: optional folder under the install root (each segment validated;
      no ``..``/hidden/separator segments). Empty = install at the root.
    - ext: ".dctl" (default) or ".dctle" (encrypted).
    - overwrite: when False (default) an existing target file is left untouched
      and an error is returned; pass True to replace it.

    Returns a JSON object ``{success, path, category, next_step}`` on success,
    or an ``Error: ...`` string on any failure — writing NOTHING when the name
    is unsafe, the source is empty, or the target exists and overwrite is False.
    """
    try:
        if not name or not _DCTL_NAME_RE.match(name):
            return (
                f"Error: invalid DCTL name '{name}'. Must match "
                f"[A-Za-z0-9_][A-Za-z0-9_ -]{{0,127}} — no path separators, no "
                f"leading dot. Nothing written."
            )

        if not isinstance(source, str) or not source.strip():
            return (
                "Error: source is required and must be a non-empty DCTL string. "
                "Nothing written."
            )

        canonical_cat, err = _check_choice(category, _DCTL_CATEGORIES, "category")
        if err:
            return f"Error: {err}"

        chosen_ext, ext_err = _check_choice(ext, _DCTL_VALID_EXTS, "ext")
        if ext_err:
            return f"Error: {ext_err}"

        try:
            safe_subdir = _resolve_subdir(subdir)
        except ValueError as e:
            return f"Error: {e}. Nothing written."

        root = _dctl_root(canonical_cat)
        target_dir = root if safe_subdir is None else os.path.join(root, safe_subdir)
        path = os.path.join(target_dir, f"{name}{chosen_ext}")

        # Existence guard BEFORE creating any directory or file so an
        # overwrite=False conflict truly writes nothing.
        if os.path.exists(path) and not overwrite:
            return (
                f"Error: DCTL '{name}{chosen_ext}' already exists at {path}. "
                f"Pass overwrite=True to replace it. Nothing written."
            )

        os.makedirs(target_dir, exist_ok=True)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(source)
        except OSError as e:
            return f"Error: failed to write DCTL to {path}: {e}"

        if canonical_cat == "lut":
            next_step = (
                "Call refresh_luts to make Resolve pick up the new DCTL "
                "(no restart needed)."
            )
        else:
            next_step = (
                "ACES DCTLs are scanned only at Resolve startup — RESTART "
                "DaVinci Resolve before this transform appears (a LUT refresh "
                "is not enough)."
            )

        return json.dumps(
            {
                "success": True,
                "path": path,
                "category": canonical_cat,
                "ext": chosen_ext,
                "next_step": next_step,
            },
            indent=2,
            default=str,
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def install_fuse(name: str, source: str, overwrite: bool = False) -> str:
    """Write a Fusion Fuse (.fuse) source file into Fusion's Fuses directory.

    Filesystem write ONLY — no running Resolve required. The Fuse ``source`` is
    written verbatim (NO Lua/GLSL compilation is attempted); only a minimal
    non-empty guard runs. A NEW Fuse is registered by Fusion at STARTUP, so a
    Resolve RESTART is required before the Fuse becomes usable — the MCP cannot
    trigger the Inspector reload that edits of an existing Fuse would use.

    Parameters:
    - name: the Fuse class identifier — must match ``[A-Za-z_][A-Za-z0-9_]*``
      (the Fuse SDK requirement; a bad name produces comps that will not
      reopen). An unsafe or empty name is rejected and nothing is written.
    - source: the full Fuse source (Lua, or Lua+GLSL for a view LUT). An
      empty/whitespace value is rejected BEFORE any file is created.
    - overwrite: when False (default) an existing target file is left untouched
      and an error is returned; pass True to replace it.

    Returns a JSON object ``{success, path, next_step}`` on success, or an
    ``Error: ...`` string on any failure — writing NOTHING when the name is
    unsafe, the source is empty, or the target exists and overwrite is False.
    """
    try:
        if not name or not _FUSE_NAME_RE.match(name):
            return (
                f"Error: invalid Fuse name '{name}'. Must match "
                f"[A-Za-z_][A-Za-z0-9_]* (Fuse SDK requirement; bad names "
                f"produce comps that will not reopen). Nothing written."
            )

        if not isinstance(source, str) or not source.strip():
            return (
                "Error: source is required and must be a non-empty Fuse string. "
                "Nothing written."
            )

        fuses_dir = _plugin_install_paths()["fuses_dir"]
        path = os.path.join(fuses_dir, f"{name}.fuse")

        # Existence guard BEFORE creating any directory or file so an
        # overwrite=False conflict truly writes nothing.
        if os.path.exists(path) and not overwrite:
            return (
                f"Error: Fuse '{name}' already exists at {path}. Pass "
                f"overwrite=True to replace it. Nothing written."
            )

        os.makedirs(fuses_dir, exist_ok=True)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(source)
        except OSError as e:
            return f"Error: failed to write Fuse to {path}: {e}"

        return json.dumps(
            {
                "success": True,
                "path": path,
                "next_step": (
                    "RESTART DaVinci Resolve to register the new Fuse — Fusion "
                    "scans the Fuses directory only at startup."
                ),
            },
            indent=2,
            default=str,
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"
