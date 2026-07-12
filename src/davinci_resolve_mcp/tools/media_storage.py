"""MediaStorage tools for the DaVinci Resolve MCP server.

These tools operate on the top-level ``MediaStorage`` object
(``resolve.GetMediaStorage()``), which browses mounted volumes and folders as
seen by Resolve -- independent of any open project -- and can pull raw
files/folders from disk into the current project's Media Pool.

Nothing here imports ``DaVinciResolveScript`` or touches Resolve at import
time -- every tool reaches Resolve lazily via ``_conn()`` inside its body.
"""

from __future__ import annotations

import json

from ..app import mcp
from ..helpers import _conn, _get_timeline_item, _ok


def _media_storage():
    """Shorthand for ``_conn().get_media_storage()``."""
    return _conn().get_media_storage()


# ── Browsing ──────────────────────────────────────────────────────────────

@mcp.tool()
def get_mounted_volumes() -> str:
    """List mounted volumes/drives visible in DaVinci Resolve's Media Storage, as JSON."""
    try:
        ms = _media_storage()
        volumes = ms.GetMountedVolumeList() or []
        return json.dumps({"volumes": list(volumes)}, indent=2)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def get_subfolder_list(path: str) -> str:
    """List subfolders of an absolute folder path, as seen by Media Storage, as JSON.

    Parameters:
    - path: absolute folder path (as browsable in Media Storage) to list
      subfolders for. If the path doesn't exist or has no subfolders, returns
      an empty list with a clear message rather than raising.
    """
    try:
        if not path:
            return json.dumps(
                {"path": path, "subfolders": [], "message": "No path provided."},
                indent=2,
            )
        ms = _media_storage()
        subfolders = ms.GetSubFolderList(path) or []
        if not subfolders:
            return json.dumps(
                {
                    "path": path,
                    "subfolders": [],
                    "message": f"No subfolders found at '{path}' (path may not exist or be empty).",
                },
                indent=2,
            )
        return json.dumps({"path": path, "subfolders": list(subfolders)}, indent=2)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def get_file_list(path: str) -> str:
    """List files/clips in an absolute folder path, as seen by Media Storage, as JSON.

    Parameters:
    - path: absolute folder path (as browsable in Media Storage) to list
      files for. If the path doesn't exist or has no files, returns an empty
      list with a clear message rather than raising.
    """
    try:
        if not path:
            return json.dumps(
                {"path": path, "files": [], "message": "No path provided."}, indent=2
            )
        ms = _media_storage()
        files = ms.GetFileList(path) or []
        if not files:
            return json.dumps(
                {
                    "path": path,
                    "files": [],
                    "message": f"No files found at '{path}' (path may not exist or be empty).",
                },
                indent=2,
            )
        return json.dumps({"path": path, "files": list(files)}, indent=2)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def reveal_in_storage(path: str) -> str:
    """Reveal an absolute file or folder path in Resolve's Media Storage browser.

    Parameters:
    - path: absolute file or folder path to reveal.
    """
    try:
        if not path:
            return "Error: path is required."
        ms = _media_storage()
        success = ms.RevealInStorage(path)
        return _ok(
            success,
            f"Revealed '{path}' in Media Storage",
            f"Failed to reveal '{path}' in Media Storage",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Importing into the Media Pool ────────────────────────────────────────

@mcp.tool()
def add_items_to_media_pool(paths: list[str]) -> str:
    """Add file/folder paths from Media Storage into the current Media Pool bin, as JSON.

    Parameters:
    - paths: list of absolute file or folder paths (as seen by Media Storage)
      to import. Folders are imported recursively by Resolve.
    """
    try:
        if not paths:
            return "Error: paths must be a non-empty list of absolute file/folder paths."
        ms = _media_storage()
        clips = ms.AddItemListToMediaPool(list(paths))
        if not clips:
            return json.dumps(
                {
                    "success": False,
                    "clips_added": 0,
                    "message": (
                        "No items were added. Check that the paths exist and "
                        "are readable by Resolve."
                    ),
                },
                indent=2,
            )
        names = []
        for clip in clips:
            try:
                names.append(clip.GetName())
            except Exception:  # noqa: BLE001
                names.append("(unnamed)")
        return json.dumps(
            {"success": True, "clips_added": len(clips), "clip_names": names}, indent=2
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


def _find_clip_by_unique_id(folder, target_id: str):
    """Recursively search a Media Pool folder tree for a clip by unique ID."""
    for clip in folder.GetClipList() or []:
        try:
            if clip.GetUniqueId() == target_id:
                return clip
        except Exception:  # noqa: BLE001
            continue
    for sub in folder.GetSubFolderList() or []:
        found = _find_clip_by_unique_id(sub, target_id)
        if found is not None:
            return found
    return None


@mcp.tool()
def add_clip_mattes_to_media_pool(media_pool_item_id: str, matte_paths: list[str]) -> str:
    """Add clip mattes (from Media Storage) to a MediaPoolItem in the current project.

    Parameters:
    - media_pool_item_id: unique ID of the target MediaPoolItem, as returned
      by ``MediaPoolItem.GetUniqueId()`` (e.g. surfaced by media-pool listing
      tools).
    - matte_paths: list of absolute file paths of matte images to attach.
    """
    try:
        if not media_pool_item_id:
            return "Error: media_pool_item_id is required."
        if not matte_paths:
            return "Error: matte_paths must be a non-empty list of absolute file paths."
        conn = _conn()
        ms = conn.get_media_storage()
        media_pool = conn.get_media_pool()
        root = media_pool.GetRootFolder()
        if root is None:
            return "Error: Could not get the Media Pool root folder."
        clip = _find_clip_by_unique_id(root, media_pool_item_id)
        if clip is None:
            return f"Error: MediaPoolItem with ID '{media_pool_item_id}' not found in the current project."
        success = ms.AddClipMattesToMediaPool(clip, list(matte_paths), root)
        return _ok(
            success,
            f"Added {len(matte_paths)} matte(s) to clip '{media_pool_item_id}'",
            f"Failed to add mattes to clip '{media_pool_item_id}'",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def add_timeline_mattes_to_media_pool(
    matte_paths: list[str],
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Add timeline mattes (from Media Storage) to a timeline item.

    Parameters:
    - matte_paths: list of absolute file paths of matte images to attach.
    - track_type: one of "video", "audio", "subtitle". Default "video".
    - track_index: 1-based track index. Default 1.
    - item_index: 0-based index of the item within that track's item list.
      Default 0.
    """
    try:
        if not matte_paths:
            return "Error: matte_paths must be a non-empty list of absolute file paths."
        item = _get_timeline_item(track_type, track_index, item_index)
        ms = _conn().get_media_storage()
        success = ms.AddTimelineMattesToMediaPool(item, list(matte_paths))
        return _ok(
            success,
            f"Added {len(matte_paths)} matte(s) to timeline item at index {item_index}",
            "Failed to add mattes to timeline item",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"
