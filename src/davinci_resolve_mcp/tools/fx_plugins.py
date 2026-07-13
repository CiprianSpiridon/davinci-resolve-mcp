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

import json
import os
import sys
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
from .fusion import _get_fusion_comp
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

        spec = _TEMPLATE_KINDS[canonical_kind]
        method_name = spec["method"]
        is_fusion = spec["fusion"]

        conn = _conn()
        timeline = _require_timeline(conn)

        insert = getattr(timeline, method_name, None)
        if insert is None:
            return (
                f"Error: this Resolve build exposes no '{method_name}' — "
                f"cannot insert a {canonical_kind} template."
            )

        used_name = name.strip()
        item = insert(used_name)

        # For Fusion titles/generators, a None result often means the display
        # name did not resolve; retry ONCE with the internal template-id form.
        retried_with: Optional[str] = None
        if item is None and is_fusion:
            template_id = _fusion_template_id(used_name)
            if template_id and template_id != used_name:
                retried_with = template_id
                item = insert(template_id)
            else:
                # No distinct id form; still make the single documented retry
                # so a transient None does not falsely error.
                retried_with = used_name
                item = insert(used_name)

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
        duration = int(end_frame) - int(start_frame)
        if duration < 4:
            return (
                f"Error: source duration {duration} frame(s) is too short — "
                f"need at least 4 frames (start_frame={start_frame}, "
                f"end_frame={end_frame}, end is exclusive). Not appending."
            )

        if track_index < 1:
            return f"Error: track_index must be >= 1 (got {track_index})."

        conn = _conn()
        timeline = _require_timeline(conn)

        try:
            track_count = timeline.GetTrackCount("video")
        except Exception:  # noqa: BLE001
            track_count = 0
        if track_index > (track_count or 0):
            return (
                f"Error: video track {track_index} does not exist — timeline has "
                f"{track_count or 0} video track(s). Add a track first. "
                f"Not appending."
            )

        clip = _find_media_pool_item(clip_name)
        if clip is None:
            return (
                f"Error: no Media Pool item named '{clip_name}' found. "
                f"Import it or check the name. Not appending."
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
            return (
                f"Error: AppendToTimeline placed nothing for '{clip_name}' on "
                f"video track {track_index} at frame {record_frame}. Check the "
                f"record frame does not collide and the track is unlocked."
            )

        item = placed[0]

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
def enumerate_templates(scope: str = "all") -> str:
    """List Fusion Edit templates (titles/generators/etc.) on this machine.

    Filesystem scan ONLY — no running Resolve required. Walks the
    ``Fusion/Templates`` tree(s) for the requested ``scope`` and reports every
    ``.setting`` / ``.drfx`` template found, with the ``Insert*`` name (the file
    basename minus its extension — the value you pass to
    :func:`insert_template_by_name`) and its category. Titles and Generators are
    flagged ``insertable: true`` (they can be dropped with a Timeline Insert*
    call); other categories are reported for discovery but flagged
    ``insertable: false``.

    Parameters:
    - scope: "all" (default), "shipped" (system install), or "user"
      (per-user templates).

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
                    category = _template_category(rel_parts)
                    insert_name = fname[: -len(ext)]
                    insertable = (
                        ext == ".setting"
                        and category.lower() in _INSERTABLE_TEMPLATE_CATEGORIES
                    )
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
