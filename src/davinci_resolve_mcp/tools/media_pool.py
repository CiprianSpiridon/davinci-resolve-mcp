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
from ..helpers import _check_choice, _conn, _ok, _require_timeline
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


@mcp.tool()
def export_metadata(file_path: str, clip_names: list[str] = []) -> str:
    """Export clip metadata from the Media Pool to a CSV file, as JSON.

    Wraps ``MediaPool.ExportMetadata(fileName, [clips])``. With an empty
    ``clip_names`` list, metadata for *all* clips in the Media Pool is
    exported; otherwise only the named clips (resolved against the current
    folder) are exported. Names that don't resolve are reported back distinctly
    under ``not_found`` and abort the export rather than silently exporting a
    partial set.

    Parameters:
    - file_path: absolute path for the output CSV file.
    - clip_names: optional list of clip names (in the current folder) to
      export. Leave empty (the default) to export every clip in the Media Pool.
    """
    try:
        if not file_path or not file_path.strip():
            return "Error: file_path must be a non-empty string."
        mp = _media_pool()

        clips: list = []
        if clip_names:
            folder = _current_folder()
            clips, not_found = _resolve_clips_by_name(folder, clip_names)
            if not_found:
                return json.dumps(
                    {
                        "success": False,
                        "not_found": not_found,
                        "message": (
                            "Could not resolve these clip name(s) in the current "
                            "Media Pool folder; nothing was exported."
                        ),
                    },
                    indent=2,
                )

        # An empty list tells Resolve to export metadata for all clips.
        result = mp.ExportMetadata(file_path, clips)
        return _ok(
            result,
            json.dumps(
                {
                    "success": True,
                    "file_path": file_path,
                    "exported": "all clips" if not clips else len(clips),
                },
                indent=2,
            ),
            f"Error: Failed to export metadata to '{file_path}'. Check that the "
            "path is writable and the Media Pool has clips to export.",
        )
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


# ── Audio sync, stereoscopic 3D & UI selection ────────────────────────────


# Resolve exposes the AutoSyncAudio settings keys/values as string constants
# on the top-level Resolve object (e.g. ``resolve.AUDIO_SYNC_MODE``,
# ``resolve.AUDIO_SYNC_TIMECODE``). We look them up via ``getattr`` so the
# real constant is used when available, falling back to the literal name on
# older Resolve versions that don't expose it.
_AUDIO_SYNC_MODE_CONSTS = {
    "waveform": "AUDIO_SYNC_WAVEFORM",
    "timecode": "AUDIO_SYNC_TIMECODE",
}


@mcp.tool()
def auto_sync_audio(
    clip_names: list[str],
    mode: str = "timecode",
    channel_number: int | None = None,
    retain_embedded_audio: bool = False,
    retain_video_metadata: bool = False,
) -> str:
    """Auto-sync audio across Media Pool clips (needs a video + audio pair), as JSON.

    Resolves every name against the Media Pool's current folder
    (``MediaPool.GetCurrentFolder()``) and calls
    ``MediaPool.AutoSyncAudio(clips, audioSyncSettings)``. At least two clips
    must resolve — typically one video clip and one audio clip.

    Parameters:
    - clip_names: names of at least two clips (video + audio) to sync. Names
      that don't resolve are reported back under ``not_found`` rather than
      raising.
    - mode: sync mode, one of "waveform" or "timecode" (default: "timecode").
    - channel_number: optional audio channel offset for waveform mode
      (-1 = automatic, -2 = mix, or a 1-based channel number).
    - retain_embedded_audio: keep the video clip's embedded audio
      (default: False).
    - retain_video_metadata: keep the video clip's metadata (default: False).
    """
    try:
        if not clip_names:
            return "Error: clip_names must be a non-empty list."

        mode, err = _check_choice(mode, ("waveform", "timecode"), "audio sync mode")
        if err:
            return f"Error: {err}"

        mp = _media_pool()
        folder = _current_folder()
        clips, not_found = _resolve_clips_by_name(folder, clip_names)

        if len(clips) < 2:
            return json.dumps(
                {
                    "success": False,
                    "message": (
                        "Audio sync needs at least 2 clips (at least one video "
                        "and one audio clip)."
                    ),
                    "resolved": len(clips),
                    "not_found": not_found,
                },
                indent=2,
            )

        resolve = _conn().get_resolve()
        settings: dict = {}
        mode_key = getattr(resolve, "AUDIO_SYNC_MODE", "AUDIO_SYNC_MODE")
        mode_val_name = _AUDIO_SYNC_MODE_CONSTS[mode]
        settings[mode_key] = getattr(resolve, mode_val_name, mode_val_name)
        if channel_number is not None:
            ch_key = getattr(
                resolve, "AUDIO_SYNC_CHANNEL_NUMBER", "AUDIO_SYNC_CHANNEL_NUMBER"
            )
            settings[ch_key] = channel_number
        if retain_embedded_audio:
            settings[
                getattr(
                    resolve,
                    "AUDIO_SYNC_RETAIN_EMBEDDED_AUDIO",
                    "AUDIO_SYNC_RETAIN_EMBEDDED_AUDIO",
                )
            ] = True
        if retain_video_metadata:
            settings[
                getattr(
                    resolve,
                    "AUDIO_SYNC_RETAIN_VIDEO_METADATA",
                    "AUDIO_SYNC_RETAIN_VIDEO_METADATA",
                )
            ] = True

        result = mp.AutoSyncAudio(clips, settings)
        output = {"success": bool(result), "mode": mode, "synced_clips": len(clips)}
        if not_found:
            output["not_found"] = not_found
        return json.dumps(output, indent=2)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def create_stereo_clip(left_clip_name: str, right_clip_name: str) -> str:
    """Create a stereoscopic 3D clip from a left- and right-eye clip, as JSON.

    Both clips are resolved against the Media Pool's current folder and passed
    to ``MediaPool.CreateStereoClip(left, right)``. The resulting stereo clip
    replaces the input clips in the Media Pool.

    Parameters:
    - left_clip_name: name of the left-eye clip (in the current folder).
    - right_clip_name: name of the right-eye clip (in the current folder).
    """
    try:
        if not left_clip_name or not left_clip_name.strip():
            return "Error: left_clip_name must be a non-empty string."
        if not right_clip_name or not right_clip_name.strip():
            return "Error: right_clip_name must be a non-empty string."

        mp = _media_pool()
        folder = _current_folder()
        clips, not_found = _resolve_clips_by_name(
            folder, [left_clip_name, right_clip_name]
        )
        if not_found:
            return json.dumps(
                {
                    "success": False,
                    "not_found": not_found,
                    "message": "Could not resolve both clips in the current Media Pool folder.",
                },
                indent=2,
            )

        stereo = mp.CreateStereoClip(clips[0], clips[1])
        if stereo is None:
            return "Error: Failed to create stereo clip."
        try:
            name = stereo.GetName()
        except Exception:  # noqa: BLE001
            name = "(unnamed)"
        return json.dumps({"success": True, "stereo_clip": name}, indent=2)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def convert_timeline_to_stereo(num_frames: int) -> str:
    """Convert the current timeline to stereoscopic 3D.

    Calls ``Timeline.ConvertTimelineToStereo(num_frames)`` on the current
    timeline.

    Parameters:
    - num_frames: number of frames in the timeline to convert.
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)
        result = timeline.ConvertTimelineToStereo(num_frames)
        return _ok(
            result,
            f"Converted the current timeline to stereoscopic 3D ({num_frames} frames).",
            "Error: Failed to convert the timeline to stereoscopic 3D.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def get_selected_clips() -> str:
    """Get the names of the clips currently selected in the Media Pool UI, as a JSON list.

    Calls ``MediaPool.GetSelectedClips()`` and returns the selected clips'
    names as a JSON array (empty when nothing is selected).
    """
    try:
        mp = _media_pool()
        clips = mp.GetSelectedClips()
        names = []
        for clip in clips or []:
            try:
                names.append(clip.GetName())
            except Exception:  # noqa: BLE001
                continue
        return json.dumps(names, indent=2)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def set_selected_clip(clip_name: str) -> str:
    """Select a single clip in the Media Pool UI, by name.

    Resolves ``clip_name`` against the Media Pool's current folder and calls
    ``MediaPool.SetSelectedClip(clip)``.

    Parameters:
    - clip_name: name of the clip (in the current folder) to select. If it
      doesn't resolve, it's reported back under ``not_found`` rather than
      raising.
    """
    try:
        if not clip_name or not clip_name.strip():
            return "Error: clip_name must be a non-empty string."
        mp = _media_pool()
        folder = _current_folder()
        clips, not_found = _resolve_clips_by_name(folder, [clip_name])
        if not clips:
            return json.dumps(
                {
                    "success": False,
                    "not_found": not_found,
                    "message": f"Clip '{clip_name}' not found in the current Media Pool folder.",
                },
                indent=2,
            )
        result = mp.SetSelectedClip(clips[0])
        return _ok(
            result,
            f"Selected clip '{clip_name}'.",
            f"Error: Failed to select clip '{clip_name}'.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Mattes & bin (.drb) import ─────────────────────────────────────────────


@mcp.tool()
def get_clip_matte_list(clip_name: str) -> str:
    """List the matte (alpha/mask) file paths attached to a Media Pool clip, as JSON.

    Resolves ``clip_name`` against the Media Pool's current folder
    (``MediaPool.GetCurrentFolder()``) and calls
    ``MediaPool.GetClipMatteList(mediaPoolItem)``, returning the matte file
    paths as a JSON array (an empty array when the clip has no mattes).

    Parameters:
    - clip_name: name of the clip (in the current folder) to inspect. If it
      doesn't resolve, it's reported back under ``not_found`` (with the
      available clip names) rather than raising.
    """
    try:
        if not clip_name or not clip_name.strip():
            return "Error: clip_name must be a non-empty string."
        mp = _media_pool()
        folder = _current_folder()
        clips, not_found = _resolve_clips_by_name(folder, [clip_name])
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
                    "message": f"Clip '{clip_name}' not found in the current Media Pool folder.",
                    "available_clips": available,
                },
                indent=2,
            )

        mattes = mp.GetClipMatteList(clips[0])
        paths = list(mattes) if mattes else []
        return json.dumps(paths, indent=2)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def get_timeline_matte_list(folder_name: str) -> str:
    """List the timeline mattes stored in a Media Pool folder, by folder name, as JSON.

    Resolves ``folder_name`` by searching the whole Media Pool folder tree
    (depth-first from the root) and calls
    ``MediaPool.GetTimelineMatteList(folder)``, returning the matte clips'
    *names* as a JSON array (an empty array when the folder has no timeline
    mattes).

    Parameters:
    - folder_name: name of the folder to list mattes from. If it doesn't
      resolve, it's reported back under ``not_found`` (with the available
      folder names) rather than raising.
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
            available = []

            def _collect(f, depth=0):
                if depth > 5:
                    return
                try:
                    available.append(f.GetName())
                except Exception:  # noqa: BLE001
                    pass
                for sub in f.GetSubFolderList() or []:
                    _collect(sub, depth + 1)

            _collect(root)
            return json.dumps(
                {
                    "success": False,
                    "not_found": [folder_name],
                    "message": f"Folder '{folder_name}' not found in the Media Pool.",
                    "available_folders": available[:50],
                },
                indent=2,
            )

        mattes = mp.GetTimelineMatteList(folder)
        names = []
        for matte in mattes or []:
            try:
                names.append(matte.GetName())
            except Exception:  # noqa: BLE001
                continue
        return json.dumps(names, indent=2)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def delete_clip_mattes(clip_name: str, paths: list[str]) -> str:
    """Delete matte files from a Media Pool clip, by clip name, as JSON.

    Resolves ``clip_name`` against the Media Pool's current folder and calls
    ``MediaPool.DeleteClipMattes(mediaPoolItem, paths)``.

    Parameters:
    - clip_name: name of the clip (in the current folder) whose mattes to
      delete. If it doesn't resolve, it's reported back under ``not_found``
      rather than raising.
    - paths: list of matte file paths to delete (as returned by
      ``get_clip_matte_list``).
    """
    try:
        if not clip_name or not clip_name.strip():
            return "Error: clip_name must be a non-empty string."
        if not paths:
            return "Error: paths must be a non-empty list."
        mp = _media_pool()
        folder = _current_folder()
        clips, not_found = _resolve_clips_by_name(folder, [clip_name])
        if not clips:
            return json.dumps(
                {
                    "success": False,
                    "not_found": not_found,
                    "message": f"Clip '{clip_name}' not found in the current Media Pool folder.",
                },
                indent=2,
            )

        result = mp.DeleteClipMattes(clips[0], paths)
        return _ok(
            result,
            f"Deleted {len(paths)} matte(s) from clip '{clip_name}'.",
            f"Error: Failed to delete mattes from clip '{clip_name}'.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def import_folder_from_file(file_path: str, source_clips_path: str = "") -> str:
    """Import a Media Pool bin from a ``.drb`` file into the current folder.

    Calls ``MediaPool.ImportFolderFromFile(filePath, sourceClipsPath)``. The
    imported folder (and its clips) is added under the Media Pool's current
    folder.

    Parameters:
    - file_path: absolute path to the ``.drb`` bin file to import.
    - source_clips_path: optional filesystem path Resolve searches to relink
      the imported clips' source media. Leave empty to skip relinking.
    """
    try:
        if not file_path or not file_path.strip():
            return "Error: file_path must be a non-empty string."
        mp = _media_pool()
        result = (
            mp.ImportFolderFromFile(file_path, source_clips_path)
            if source_clips_path
            else mp.ImportFolderFromFile(file_path)
        )
        if result:
            return f"Imported folder from '{file_path}'."
        return f"Error: Failed to import folder from '{file_path}'."
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"
