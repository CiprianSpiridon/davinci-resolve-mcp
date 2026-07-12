"""Project info & settings tools for the DaVinci Resolve MCP server.

Covers reading a summary of the current project (name, version, current
page, timeline count, frame rate/resolution), getting/setting individual
project settings (and dumping the full settings dict), getting/setting the
project name, listing supported render resolutions for a format/codec pair,
refreshing the LUT list, and listing/applying project presets.

All tools reach Resolve lazily via ``_conn()`` inside their bodies — nothing
here imports ``DaVinciResolveScript`` or touches a live Resolve instance at
import time. Object: ``Project`` (``ResolveConnection.get_project()``), plus
the top-level ``Resolve`` object for version/page info.
"""

from __future__ import annotations

import json

from ..app import mcp
from ..helpers import _coerce_value, _conn, _ok


def _project():
    """Shorthand: the currently open Project, or raise."""
    return _conn().get_project()


# ── Project info summary ─────────────────────────────────────────────────


@mcp.tool()
def get_project_info() -> str:
    """Get a summary of the current DaVinci Resolve project, as JSON.

    Includes project name, Resolve version string, current page, timeline
    count, and (when available) frame rate / resolution settings.
    """
    try:
        conn = _conn()
        resolve = conn.get_resolve()
        project = conn.get_project()

        info: dict = {
            "name": project.GetName(),
            "version": resolve.GetVersionString() if hasattr(resolve, "GetVersionString") else None,
            "current_page": resolve.GetCurrentPage() if hasattr(resolve, "GetCurrentPage") else None,
            "timeline_count": project.GetTimelineCount() if hasattr(project, "GetTimelineCount") else None,
        }

        for key in (
            "timelineFrameRate",
            "timelinePlaybackFrameRate",
            "timelineResolutionWidth",
            "timelineResolutionHeight",
        ):
            value = project.GetSetting(key) if hasattr(project, "GetSetting") else None
            if value:
                info[key] = value

        return json.dumps(info, indent=2)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Project settings ──────────────────────────────────────────────────────


@mcp.tool()
def get_all_project_settings() -> str:
    """Get every setting on the current project, as JSON.

    Uses ``Project.GetSetting('')`` which returns the full settings dict per
    the Resolve scripting API. Returns an explanatory string if that call is
    unavailable or returns nothing usable.
    """
    try:
        project = _project()
        if not hasattr(project, "GetSetting"):
            return "Error: GetSetting is not available on this project object."
        settings = project.GetSetting("")
        if not isinstance(settings, dict):
            return "Error: Could not retrieve the full settings dict from the current project."
        return json.dumps(settings, indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def get_project_setting(setting_name: str) -> str:
    """Get a single named setting from the current project.

    Parameters:
    - setting_name: setting key, e.g. "timelineFrameRate",
      "timelineResolutionWidth", "colorScienceMode". See
      get_all_project_settings() for the full list of valid keys.
    """
    try:
        if not setting_name or not setting_name.strip():
            return "Error: setting_name must be a non-empty string."
        project = _project()
        if not hasattr(project, "GetSetting"):
            return "Error: GetSetting is not available on this project object."
        value = project.GetSetting(setting_name)
        if value is None or value == "":
            return f"Error: Setting '{setting_name}' not found or has no value."
        return json.dumps({setting_name: value}, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def set_project_setting(setting_name: str, setting_value: str) -> str:
    """Set a single named setting on the current project.

    Parameters:
    - setting_name: setting key, e.g. "timelineFrameRate",
      "timelineResolutionWidth", "colorScienceMode". See
      get_all_project_settings() for valid keys.
    - setting_value: new value as a string. Numeric-looking strings are
      coerced to int/float and "true"/"false" to bool before being passed
      to the Resolve API; anything else is passed through as a string.

    Never raises on an unknown key or a value Resolve rejects — Resolve's
    SetSetting commonly returns False rather than raising, and that is
    reported here as a clear failure string.
    """
    try:
        if not setting_name or not setting_name.strip():
            return "Error: setting_name must be a non-empty string."
        project = _project()
        if not hasattr(project, "SetSetting"):
            return "Error: SetSetting is not available on this project object."
        coerced = _coerce_value(setting_value)
        result = project.SetSetting(setting_name, coerced)
        return _ok(
            result,
            f"Set project setting '{setting_name}' = {coerced!r}",
            f"Error: Failed to set project setting '{setting_name}' = {coerced!r}. "
            "The key may be invalid, read-only, or the value out of range.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Project name ──────────────────────────────────────────────────────────


@mcp.tool()
def get_project_name() -> str:
    """Get the name of the currently open project."""
    try:
        project = _project()
        return project.GetName()
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def set_project_name(name: str) -> str:
    """Rename the currently open project.

    Parameters:
    - name: new project name. Must be non-empty; Resolve's SetName returns
      False (reported here as a failure string) on collision with an
      existing project name in the same folder or an invalid name.
    """
    try:
        if not name or not name.strip():
            return "Error: name must be a non-empty string."
        project = _project()
        if not hasattr(project, "SetName"):
            return "Error: SetName is not available on this project object."
        result = project.SetName(name)
        return _ok(
            result,
            f"Project renamed to '{name}'.",
            f"Error: Failed to rename project to '{name}'. A project with "
            "that name may already exist, or the name is invalid.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Render resolutions ───────────────────────────────────────────────────


@mcp.tool()
def get_render_resolutions(format_name: str = "", codec: str = "") -> str:
    """List render resolutions supported by the current project, as JSON.

    Parameters:
    - format_name: render format (e.g. "mp4", "mov"). Leave empty to list
      resolutions across all formats and codecs.
    - codec: render codec (e.g. "H.264"). Leave empty along with
      format_name to list resolutions across all formats and codecs.

    Returns a list of {"Width": ..., "Height": ...} dicts.
    """
    try:
        project = _project()
        if not hasattr(project, "GetRenderResolutions"):
            return "Error: GetRenderResolutions is not available on this project object."
        if format_name or codec:
            resolutions = project.GetRenderResolutions(format_name, codec)
        else:
            resolutions = project.GetRenderResolutions()
        if not resolutions:
            return json.dumps([])
        return json.dumps(resolutions, indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── LUTs ──────────────────────────────────────────────────────────────────


@mcp.tool()
def refresh_luts() -> str:
    """Refresh Resolve's LUT list from disk, picking up newly added LUT files."""
    try:
        project = _project()
        if not hasattr(project, "RefreshLUTList"):
            return "Error: RefreshLUTList is not available on this project object."
        result = project.RefreshLUTList()
        return _ok(result, "LUT list refreshed successfully.", "Error: Failed to refresh the LUT list.")
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Project presets ───────────────────────────────────────────────────────


@mcp.tool()
def get_preset_list() -> str:
    """List project presets (format/timeline presets) available on the current project, as JSON."""
    try:
        project = _project()
        if not hasattr(project, "GetPresetList"):
            return "Error: GetPresetList is not available on this project object."
        presets = project.GetPresetList()
        if not presets:
            return json.dumps([])
        return json.dumps(presets, indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def set_preset(preset_name: str) -> str:
    """Apply a named project preset to the current project.

    Parameters:
    - preset_name: exact preset name, as returned by get_preset_list().
    """
    try:
        if not preset_name or not preset_name.strip():
            return "Error: preset_name must be a non-empty string."
        project = _project()
        if not hasattr(project, "SetPreset"):
            return "Error: SetPreset is not available on this project object."
        result = project.SetPreset(preset_name)
        return _ok(
            result,
            f"Applied preset '{preset_name}' to the current project.",
            f"Error: Failed to apply preset '{preset_name}'. Use "
            "get_preset_list() to see available preset names.",
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"
