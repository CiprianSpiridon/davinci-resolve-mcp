"""MediaPool tools for the DaVinci Resolve MCP server.

Covers browsing/mutating the Media Pool's folder tree (bins), importing
media and timeline files, moving/deleting/relinking/unlinking clips, and
creating timelines from Media Pool clips. Object: ``MediaPool``
(``Project.GetMediaPool()``).

All tools reach Resolve lazily via ``_conn()`` inside their bodies — nothing
here imports ``DaVinciResolveScript`` or touches a live Resolve instance at
import time.

Several tools accept clip/folder *names* rather than opaque handles (since
handles can't cross the MCP JSON boundary). These are resolved against the
Media Pool's *current folder* (``MediaPool.GetCurrentFolder()``) — the same
folder the user sees selected in Resolve's Media Pool panel — unless noted
otherwise. Names that don't resolve are reported back distinctly as
``not_found`` rather than silently skipped or raised as errors.
"""

from __future__ import annotations

import json

from ..app import mcp
from ..helpers import _conn, _ok
from ..resolve_utils import folder_to_dict


def _media_pool():
    """Shorthand: the current project's MediaPool, or raise."""
    return _conn().get_media_pool()


def _current_folder():
    """Return the Media Pool's current folder, or raise a clear error."""
    mp = _media_pool()
    folder = mp.GetCurrentFolder()
    if folder is None:
        raise RuntimeError("Could not get the current Media Pool folder.")
    return folder


def _find_folder_by_name(folder, name: str):
    """Recursively search a Media Pool folder tree for a folder by name.

    Returns the first match in a depth-first search rooted at ``folder``
    (``folder`` itself is checked first), or ``None`` if no folder in the
    tree has that name.
    """
    try:
        if folder.GetName() == name:
            return folder
    except Exception:  # noqa: BLE001
        pass
    for sub in folder.GetSubFolderList() or []:
        found = _find_folder_by_name(sub, name)
        if found is not None:
            return found
    return None


def _resolve_clips_by_name(folder, clip_names: list[str]):
    """Resolve a list of clip names against ``folder``'s (non-recursive) clip list.

    Returns ``(clips, not_found)`` where ``clips`` is the list of matched
    ``MediaPoolItem`` objects (in the order names were given, duplicates
    collapsed to the first match) and ``not_found`` is the list of names
    that had no match in the folder's clip list.
    """
    clips = folder.GetClipList() or []
    name_to_clip: dict[str, object] = {}
    for clip in clips:
        try:
            n = clip.GetName()
        except Exception:  # noqa: BLE001
            continue
        if n and n not in name_to_clip:
            name_to_clip[n] = clip

    resolved = []
    not_found = []
    for name in clip_names:
        clip = name_to_clip.get(name)
        if clip is not None:
            resolved.append(clip)
        else:
            not_found.append(name)
    return resolved, not_found


# ── Browsing / current folder ────────────────────────────────────────────


@mcp.tool()
def get_media_pool_structure(max_depth: int = 3, max_clips: int = 50) -> str:
    """Get the folder/clip structure of the Media Pool, as JSON.

    Parameters:
    - max_depth: maximum folder recursion depth from the root (default: 3).
    - max_clips: maximum clips to list per folder before truncating
      (default: 50).
    """
    try:
        mp = _media_pool()
        root = mp.GetRootFolder()
        if root is None:
            return "Error: Could not get the Media Pool root folder."
        structure = folder_to_dict(root, max_depth=max_depth, max_clips=max_clips)
        return json.dumps(structure, indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def get_current_folder() -> str:
    """Get the name (and clip/subfolder listing) of the Media Pool's current folder, as JSON."""
    try:
        folder = _current_folder()
        return json.dumps(folder_to_dict(folder, max_depth=0), indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def set_current_folder(folder_name: str) -> str:
    """Set the Media Pool's current folder by name.

    Parameters:
    - folder_name: name of the target folder. The entire folder tree
      (starting at the root) is searched depth-first for the first folder
      with this name.
    """
    try:
        if not folder_name or not folder_name.strip():
            return "Error: folder_name must be a non-empty string."
        mp = _media_pool()
        root = mp.GetRootFolder()
        if root is None:
            return "Error: Could not get the Media Pool root folder."
        folder = _find_folder_by_name(root, folder_name)
        if folder is None:
            return f"Error: Folder '{folder_name}' not found in the Media Pool."
        result = mp.SetCurrentFolder(folder)
        return _ok(
            result,
            f"Current folder set to '{folder_name}'.",
            f"Error: Failed to set current folder to '{folder_name}'.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Bins (folders) ────────────────────────────────────────────────────────


@mcp.tool()
def create_bin(name: str) -> str:
    """Create a new bin (subfolder) under the Media Pool's current folder.

    Parameters:
    - name: name for the new bin.
    """
    try:
        if not name or not name.strip():
            return "Error: name must be a non-empty string."
        mp = _media_pool()
        parent = _current_folder()
        new_folder = mp.AddSubFolder(parent, name)
        if new_folder is None:
            return f"Error: Failed to create bin '{name}'. A bin with that name may already exist."
        return f"Created bin '{name}'."
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def delete_folders(folder_names: list[str]) -> str:
    """Delete one or more bins (subfolders) from the Media Pool, by name.

    Parameters:
    - folder_names: names of folders to delete. Each is resolved by
      searching the whole Media Pool folder tree; names that don't resolve
      are reported back as ``not_found`` rather than raising.
    """
    try:
        if not folder_names:
            return "Error: folder_names must be a non-empty list."
        mp = _media_pool()
        root = mp.GetRootFolder()
        if root is None:
            return "Error: Could not get the Media Pool root folder."

        folders = []
        not_found = []
        for name in folder_names:
            folder = _find_folder_by_name(root, name)
            if folder is not None:
                folders.append(folder)
            else:
                not_found.append(name)

        if not folders:
            return json.dumps({"deleted": 0, "not_found": not_found}, indent=2)

        result = mp.DeleteFolders(folders)
        output = {"success": bool(result), "requested": len(folders)}
        if not_found:
            output["not_found"] = not_found
        return json.dumps(output, indent=2)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def refresh_folders() -> str:
    """Refresh the Media Pool's folders from other stations in collaboration mode."""
    try:
        mp = _media_pool()
        result = mp.RefreshFolders()
        return _ok(result, "Media Pool folders refreshed.", "Error: Failed to refresh Media Pool folders.")
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Importing media ───────────────────────────────────────────────────────


@mcp.tool()
def import_media(file_paths: list[str]) -> str:
    """Import media files into the Media Pool's current folder, as JSON.

    Parameters:
    - file_paths: list of absolute file paths to import.
    """
    try:
        if not file_paths:
            return "Error: file_paths must be a non-empty list of absolute file paths."
        mp = _media_pool()
        items = mp.ImportMedia(list(file_paths))
        if not items:
            return (
                "No media was imported. Check that file paths exist and "
                "are supported formats."
            )
        names = []
        for item in items:
            try:
                names.append(item.GetName())
            except Exception:  # noqa: BLE001
                names.append("(unnamed)")
        return json.dumps({"imported": len(names), "clips": names}, indent=2)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def import_timeline_from_file(file_path: str, timeline_name: str = "") -> str:
    """Import a timeline from an AAF/EDL/XML/FCPXML/DRT/ADL/OTIO file, as JSON.

    Parameters:
    - file_path: absolute path to the timeline file to import.
    - timeline_name: optional name for the created timeline. Leave empty to
      use the name Resolve derives from the file.
    """
    try:
        if not file_path or not file_path.strip():
            return "Error: file_path must be a non-empty string."
        mp = _media_pool()
        import_options: dict = {}
        if timeline_name:
            import_options["timelineName"] = timeline_name
        timeline = mp.ImportTimelineFromFile(file_path, import_options) if import_options else mp.ImportTimelineFromFile(file_path)
        if timeline is None:
            return f"Error: Failed to import timeline from '{file_path}'."
        try:
            name = timeline.GetName()
        except Exception:  # noqa: BLE001
            name = timeline_name or "(unnamed)"
        return json.dumps({"success": True, "timeline_name": name}, indent=2)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Clips ─────────────────────────────────────────────────────────────────


@mcp.tool()
def delete_clips(clip_names: list[str]) -> str:
    """Delete one or more clips from the Media Pool's current folder, by name.

    Parameters:
    - clip_names: names of clips to delete. Names that don't resolve against
      the current folder's clip list are reported back as ``not_found``
      rather than raising.
    """
    try:
        if not clip_names:
            return "Error: clip_names must be a non-empty list."
        mp = _media_pool()
        folder = _current_folder()
        clips, not_found = _resolve_clips_by_name(folder, clip_names)

        if not clips:
            return json.dumps({"deleted": 0, "not_found": not_found}, indent=2)

        result = mp.DeleteClips(clips)
        output = {"success": bool(result), "requested": len(clips)}
        if not_found:
            output["not_found"] = not_found
        return json.dumps(output, indent=2)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def move_clips(clip_names: list[str], target_folder_name: str) -> str:
    """Move clips from the Media Pool's current folder into a target folder, by name.

    Parameters:
    - clip_names: names of clips (in the current folder) to move. Names that
      don't resolve are reported back as ``not_found``.
    - target_folder_name: name of the destination folder, found by searching
      the whole Media Pool folder tree.
    """
    try:
        if not clip_names:
            return "Error: clip_names must be a non-empty list."
        if not target_folder_name or not target_folder_name.strip():
            return "Error: target_folder_name must be a non-empty string."

        mp = _media_pool()
        source_folder = _current_folder()
        clips, not_found = _resolve_clips_by_name(source_folder, clip_names)

        root = mp.GetRootFolder()
        if root is None:
            return "Error: Could not get the Media Pool root folder."
        target_folder = _find_folder_by_name(root, target_folder_name)
        if target_folder is None:
            return f"Error: Target folder '{target_folder_name}' not found in the Media Pool."

        if not clips:
            return json.dumps({"moved": 0, "not_found": not_found}, indent=2)

        result = mp.MoveClips(clips, target_folder)
        output = {"success": bool(result), "requested": len(clips), "target_folder": target_folder_name}
        if not_found:
            output["not_found"] = not_found
        return json.dumps(output, indent=2)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def relink_clips(clip_names: list[str], folder_path: str) -> str:
    """Relink Media Pool clips to a new source folder location, by name.

    Parameters:
    - clip_names: names of clips (in the current folder) to relink. Names
      that don't resolve are reported back as ``not_found``.
    - folder_path: absolute filesystem folder path to relink the clips to.
    """
    try:
        if not clip_names:
            return "Error: clip_names must be a non-empty list."
        if not folder_path or not folder_path.strip():
            return "Error: folder_path must be a non-empty string."

        mp = _media_pool()
        folder = _current_folder()
        clips, not_found = _resolve_clips_by_name(folder, clip_names)

        if not clips:
            return json.dumps({"relinked": 0, "not_found": not_found}, indent=2)

        result = mp.RelinkClips(clips, folder_path)
        output = {"success": bool(result), "requested": len(clips), "folder_path": folder_path}
        if not_found:
            output["not_found"] = not_found
        return json.dumps(output, indent=2)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def unlink_clips(clip_names: list[str]) -> str:
    """Unlink Media Pool clips (mark them offline), by name.

    Parameters:
    - clip_names: names of clips (in the current folder) to unlink. Names
      that don't resolve are reported back as ``not_found``.
    """
    try:
        if not clip_names:
            return "Error: clip_names must be a non-empty list."
        mp = _media_pool()
        folder = _current_folder()
        clips, not_found = _resolve_clips_by_name(folder, clip_names)

        if not clips:
            return json.dumps({"unlinked": 0, "not_found": not_found}, indent=2)

        result = mp.UnlinkClips(clips)
        output = {"success": bool(result), "requested": len(clips)}
        if not_found:
            output["not_found"] = not_found
        return json.dumps(output, indent=2)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Timelines ─────────────────────────────────────────────────────────────


@mcp.tool()
def create_empty_timeline(name: str) -> str:
    """Create a new empty timeline with the given name in the current project.

    Parameters:
    - name: name for the new timeline.
    """
    try:
        if not name or not name.strip():
            return "Error: name must be a non-empty string."
        mp = _media_pool()
        timeline = mp.CreateEmptyTimeline(name)
        if timeline is None:
            return f"Error: Failed to create timeline '{name}'. A timeline with that name may already exist."
        return f"Created empty timeline '{name}'."
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def create_timeline_from_clips(name: str, clip_names: list[str]) -> str:
    """Create a new timeline with the given name, populated from Media Pool clips by name.

    Parameters:
    - name: name for the new timeline.
    - clip_names: names of clips (in the current Media Pool folder) to add
      to the new timeline, in order. Names that don't resolve are reported
      back as ``not_found`` rather than aborting the whole call.
    """
    try:
        if not name or not name.strip():
            return "Error: name must be a non-empty string."
        if not clip_names:
            return "Error: clip_names must be a non-empty list."

        mp = _media_pool()
        folder = _current_folder()
        clips, not_found = _resolve_clips_by_name(folder, clip_names)

        if not clips:
            return json.dumps(
                {"success": False, "not_found": not_found, "message": "No matching clips found."},
                indent=2,
            )

        timeline = mp.CreateTimelineFromClips(name, clips)
        if timeline is None:
            return f"Error: Failed to create timeline '{name}' from clips."

        output = {"success": True, "timeline_name": name, "clips_added": len(clips)}
        if not_found:
            output["not_found"] = not_found
        return json.dumps(output, indent=2)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def append_to_timeline(clip_names: list[str]) -> str:
    """Append Media Pool clips to the current timeline, by name.

    Parameters:
    - clip_names: names of clips to append. Each name is resolved against
      the Media Pool's current folder (``MediaPool.GetCurrentFolder()``);
      names that don't resolve there are reported back distinctly under
      ``not_found`` rather than raising or silently skipping.
    """
    try:
        if not clip_names:
            return "Error: clip_names must be a non-empty list."

        mp = _media_pool()
        folder = _current_folder()
        clips, not_found = _resolve_clips_by_name(folder, clip_names)

        if not clips:
            available = []
            for clip in (folder.GetClipList() or [])[:20]:
                try:
                    available.append(clip.GetName())
                except Exception:  # noqa: BLE001
                    continue
            return json.dumps(
                {
                    "success": False,
                    "not_found": not_found,
                    "message": "No matching clips found in the current Media Pool folder.",
                    "available_clips": available,
                },
                indent=2,
            )

        result = mp.AppendToTimeline(clips)
        output: dict = {"appended": len(clips)}
        if not_found:
            output["not_found"] = not_found
        if result:
            names = []
            for item in result:
                try:
                    names.append(item.GetName())
                except Exception:  # noqa: BLE001
                    continue
            output["timeline_items"] = names
        return json.dumps(output, indent=2)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"
