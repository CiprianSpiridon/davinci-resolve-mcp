"""Application-level navigation and app-management tools.

These tools operate on the top-level ``Resolve`` object rather than a
project or timeline: switching pages, reading product/version info,
managing UI layout presets on disk, toggling the color-page keyframe mode,
and importing/exporting render presets.

Nothing here imports ``DaVinciResolveScript`` or touches Resolve at import
time — every tool reaches Resolve lazily via ``_conn()`` inside its body.
"""

from __future__ import annotations

import json
import os
import sys

from ..app import mcp
from ..helpers import _conn, _ok

# ── Page navigation ──────────────────────────────────────────────────────

_VALID_PAGES = ("media", "cut", "edit", "fusion", "color", "fairlight", "deliver")


@mcp.tool()
def open_page(page: str) -> str:
    """Switch DaVinci Resolve to a specific page.

    Parameters:
    - page: one of "media", "cut", "edit", "fusion", "color", "fairlight", "deliver".
    """
    page_lower = page.lower() if isinstance(page, str) else page
    if page_lower not in _VALID_PAGES:
        return f"Invalid page '{page}'. Must be one of: {', '.join(_VALID_PAGES)}"
    try:
        resolve = _conn().get_resolve()
        success = resolve.OpenPage(page_lower)
        return _ok(success, f"Switched to {page_lower} page", f"Failed to switch to {page_lower} page")
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def get_current_page() -> str:
    """Get the currently active page in DaVinci Resolve (Media/Cut/Edit/Fusion/Color/Fairlight/Deliver)."""
    try:
        resolve = _conn().get_resolve()
        page = resolve.GetCurrentPage()
        return page or "unknown"
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Product / version info ──────────────────────────────────────────────

@mcp.tool()
def get_product_info() -> str:
    """Get the DaVinci Resolve product name and version string as JSON."""
    try:
        resolve = _conn().get_resolve()
        info = {
            "product": resolve.GetProductName() if hasattr(resolve, "GetProductName") else None,
            "version_string": resolve.GetVersionString() if hasattr(resolve, "GetVersionString") else None,
        }
        return json.dumps(info, indent=2)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def get_version() -> str:
    """Get the DaVinci Resolve version as structured fields (major/minor/patch/build/suffix) JSON."""
    try:
        resolve = _conn().get_resolve()
        if not hasattr(resolve, "GetVersion"):
            return "Error: GetVersion is not available on this Resolve object."
        version = resolve.GetVersion()
        if not version:
            return "Error: Could not retrieve version information."
        fields = ["major", "minor", "patch", "build", "suffix"]
        info = {fields[i]: version[i] for i in range(min(len(fields), len(version)))}
        info["raw"] = list(version)
        return json.dumps(info, indent=2)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Layout presets ───────────────────────────────────────────────────────
# DaVinci Resolve's scripting API can save/load/export/import/delete/update
# a *named* UI layout preset, but it has no "list presets" call — presets
# are stored as ``.setting`` (or similarly-suffixed) files under a per-user,
# per-platform directory, so listing is done by scanning that directory.

_LAYOUT_PRESET_DIRS: dict[str, str] = {
    "darwin": "~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Layouts",
    "win32": os.path.join(
        "%APPDATA%", "Blackmagic Design", "DaVinci Resolve", "Support", "Layouts"
    ),
    "linux": "~/.local/share/DaVinciResolve/Layouts",
}


def _layout_preset_dir() -> str:
    """Return the best-guess per-platform directory Resolve stores UI layout presets in."""
    if sys.platform.startswith("darwin"):
        key = "darwin"
    elif sys.platform.startswith("win") or sys.platform.startswith("cygwin"):
        key = "win32"
    else:
        key = "linux"
    return os.path.expandvars(os.path.expanduser(_LAYOUT_PRESET_DIRS[key]))


@mcp.tool()
def list_layout_presets() -> str:
    """List saved DaVinci Resolve UI layout presets found on disk, as JSON.

    Resolve's scripting API has no direct "list presets" call, so this scans
    the platform-standard layout preset directory. If that directory doesn't
    exist yet (e.g. no preset has ever been saved), returns an empty list.
    """
    try:
        preset_dir = _layout_preset_dir()
        if not os.path.isdir(preset_dir):
            return json.dumps({"directory": preset_dir, "presets": []}, indent=2)
        presets = []
        for entry in sorted(os.listdir(preset_dir)):
            full_path = os.path.join(preset_dir, entry)
            if os.path.isfile(full_path):
                presets.append(os.path.splitext(entry)[0])
        return json.dumps({"directory": preset_dir, "presets": presets}, indent=2)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def load_layout_preset(preset_name: str) -> str:
    """Load a saved UI layout preset by name.

    Parameters:
    - preset_name: name of a previously saved layout preset.
    """
    try:
        resolve = _conn().get_resolve()
        if not hasattr(resolve, "LoadLayoutPreset"):
            return "Error: LoadLayoutPreset is not available on this Resolve object."
        success = resolve.LoadLayoutPreset(preset_name)
        return _ok(success, f"Loaded layout preset '{preset_name}'", f"Failed to load layout preset '{preset_name}'")
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def save_layout_preset(preset_name: str) -> str:
    """Save the current UI layout as a new named preset.

    Parameters:
    - preset_name: name to save the current layout under.
    """
    try:
        resolve = _conn().get_resolve()
        if not hasattr(resolve, "SaveLayoutPreset"):
            return "Error: SaveLayoutPreset is not available on this Resolve object."
        success = resolve.SaveLayoutPreset(preset_name)
        return _ok(success, f"Saved layout preset '{preset_name}'", f"Failed to save layout preset '{preset_name}'")
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def update_layout_preset(preset_name: str) -> str:
    """Overwrite an existing UI layout preset with the current layout.

    Parameters:
    - preset_name: name of the existing preset to overwrite.
    """
    try:
        resolve = _conn().get_resolve()
        if not hasattr(resolve, "UpdateLayoutPreset"):
            return "Error: UpdateLayoutPreset is not available on this Resolve object."
        success = resolve.UpdateLayoutPreset(preset_name)
        return _ok(
            success,
            f"Updated layout preset '{preset_name}'",
            f"Failed to update layout preset '{preset_name}'",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def delete_layout_preset(preset_name: str) -> str:
    """Delete a saved UI layout preset by name.

    Parameters:
    - preset_name: name of the preset to delete.
    """
    try:
        resolve = _conn().get_resolve()
        if not hasattr(resolve, "DeleteLayoutPreset"):
            return "Error: DeleteLayoutPreset is not available on this Resolve object."
        success = resolve.DeleteLayoutPreset(preset_name)
        return _ok(
            success,
            f"Deleted layout preset '{preset_name}'",
            f"Failed to delete layout preset '{preset_name}'",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def export_layout_preset(preset_name: str, export_path: str) -> str:
    """Export a saved UI layout preset to a file on disk.

    Parameters:
    - preset_name: name of the preset to export.
    - export_path: absolute file path to write the exported preset to.
    """
    try:
        resolve = _conn().get_resolve()
        if not hasattr(resolve, "ExportLayoutPreset"):
            return "Error: ExportLayoutPreset is not available on this Resolve object."
        success = resolve.ExportLayoutPreset(preset_name, export_path)
        return _ok(
            success,
            f"Exported layout preset '{preset_name}' to {export_path}",
            f"Failed to export layout preset '{preset_name}'",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def import_layout_preset(import_path: str, preset_name: str = "") -> str:
    """Import a UI layout preset from a file on disk.

    Parameters:
    - import_path: absolute path to the preset file to import.
    - preset_name: name to register the imported preset under. If omitted,
      Resolve derives a name from the imported file.
    """
    try:
        resolve = _conn().get_resolve()
        if not hasattr(resolve, "ImportLayoutPreset"):
            return "Error: ImportLayoutPreset is not available on this Resolve object."
        if preset_name:
            success = resolve.ImportLayoutPreset(import_path, preset_name)
        else:
            success = resolve.ImportLayoutPreset(import_path)
        return _ok(
            success,
            f"Imported layout preset from {import_path}",
            f"Failed to import layout preset from {import_path}",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Keyframe mode (Color page) ──────────────────────────────────────────

@mcp.tool()
def get_keyframe_mode() -> str:
    """Get the current Color page keyframe mode (0=All, 1=Color, 2=Sizing)."""
    try:
        resolve = _conn().get_resolve()
        if not hasattr(resolve, "GetKeyframeMode"):
            return "Error: GetKeyframeMode is not available on this Resolve object."
        mode = resolve.GetKeyframeMode()
        return json.dumps({"keyframe_mode": mode})
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def set_keyframe_mode(mode: int) -> str:
    """Set the Color page keyframe mode.

    Parameters:
    - mode: integer keyframe mode (0=All, 1=Color, 2=Sizing).
    """
    try:
        resolve = _conn().get_resolve()
        if not hasattr(resolve, "SetKeyframeMode"):
            return "Error: SetKeyframeMode is not available on this Resolve object."
        success = resolve.SetKeyframeMode(mode)
        return _ok(success, f"Set keyframe mode to {mode}", f"Failed to set keyframe mode to {mode}")
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Render presets ───────────────────────────────────────────────────────

@mcp.tool()
def import_render_preset(preset_path: str) -> str:
    """Import a render preset from a file into the current project.

    Parameters:
    - preset_path: absolute path to the render preset file to import.
    """
    try:
        project = _conn().get_project()
        if not hasattr(project, "ImportRenderPreset"):
            return "Error: ImportRenderPreset is not available on this project object."
        success = project.ImportRenderPreset(preset_path)
        return _ok(
            success,
            f"Imported render preset from {preset_path}",
            f"Failed to import render preset from {preset_path}",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def export_render_preset(preset_name: str, export_path: str) -> str:
    """Export a render preset from the current project to a file.

    Parameters:
    - preset_name: name of an existing render preset in the current project.
    - export_path: absolute file path to write the exported preset to.
    """
    try:
        project = _conn().get_project()
        if not hasattr(project, "ExportRenderPreset"):
            return "Error: ExportRenderPreset is not available on this project object."
        success = project.ExportRenderPreset(preset_name, export_path)
        return _ok(
            success,
            f"Exported render preset '{preset_name}' to {export_path}",
            f"Failed to export render preset '{preset_name}'",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"
