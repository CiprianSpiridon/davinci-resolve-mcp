"""Render / delivery tools for the DaVinci Resolve MCP server.

Covers the DaVinci Resolve "Deliver" page workflow driven through the
``Project`` scripting object: discovering available render formats/codecs
and presets, reading/writing render settings (target directory, in/out
points, format+codec, custom render mode), managing the render queue
(add/delete/delete-all render jobs), starting/stopping rendering, and
polling render job status / progress.

All tools reach Resolve lazily via ``_conn()`` inside their bodies — nothing
here imports ``DaVinciResolveScript`` or touches a live Resolve instance at
import time. Object: ``Project`` (``ResolveConnection.get_project()``).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..app import mcp
from ..helpers import _conn, _ok
from ..resolve_utils import safe_serialize


def _project():
    """Shorthand: the currently open Project, or raise."""
    return _conn().get_project()


# ── Formats / codecs / presets ───────────────────────────────────────────


@mcp.tool()
def get_render_formats(render_format: Optional[str] = None) -> str:
    """Get available render formats, or the codecs supported by one format.

    Parameters:
    - render_format: If provided, returns the codecs supported by this
      format (e.g. "mp4", "mov"). Otherwise returns the full format list.
    """
    try:
        project = _project()

        if render_format:
            codecs = project.GetRenderCodecs(render_format)
            return json.dumps(
                {"format": render_format, "codecs": safe_serialize(codecs)}, indent=2
            )

        formats = project.GetRenderFormats()
        return json.dumps({"formats": safe_serialize(formats)}, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_render_codecs(render_format: str) -> str:
    """Get the codecs supported by a given render format.

    Parameters:
    - render_format: Format string as returned by ``get_render_formats``
      (e.g. "mp4", "mov", "mxf").
    """
    try:
        project = _project()
        codecs = project.GetRenderCodecs(render_format)
        if not codecs:
            return f"No codecs found for format '{render_format}'. Check get_render_formats() for valid format keys."
        return json.dumps({"format": render_format, "codecs": safe_serialize(codecs)}, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_render_preset_list() -> str:
    """List the names of available render presets (built-in and saved)."""
    try:
        project = _project()
        presets = project.GetRenderPresetList()
        return json.dumps({"presets": safe_serialize(presets) if presets else []}, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def load_render_preset(preset_name: str) -> str:
    """Load a render preset by name, applying its settings to the render queue.

    Parameters:
    - preset_name: Name of an existing render preset (see ``get_render_preset_list``).
    """
    try:
        project = _project()
        success = project.LoadRenderPreset(preset_name)
        return _ok(
            success,
            f"Render preset '{preset_name}' loaded",
            f"Failed to load render preset '{preset_name}'. Check get_render_preset_list() for valid names.",
        )
    except Exception as e:
        return f"Error: {e}"


# ── Render settings ───────────────────────────────────────────────────────


@mcp.tool()
def get_render_settings() -> str:
    """Get current render format/codec, render mode, queued jobs, and presets."""
    try:
        project = _project()

        result: Dict[str, Any] = {}

        try:
            result["current_format_codec"] = safe_serialize(project.GetCurrentRenderFormatAndCodec())
        except Exception:
            pass
        try:
            result["render_mode"] = project.GetCurrentRenderMode()
        except Exception:
            pass
        try:
            jobs = project.GetRenderJobList()
            result["render_jobs"] = safe_serialize(jobs) if jobs else []
        except Exception:
            pass
        try:
            presets = project.GetRenderPresetList()
            result["render_presets"] = safe_serialize(presets) if presets else []
        except Exception:
            pass
        try:
            result["is_rendering"] = project.IsRenderingInProgress()
        except Exception:
            pass

        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def set_render_settings(
    settings: Optional[Dict[str, Any]] = None,
    render_format: Optional[str] = None,
    codec: Optional[str] = None,
) -> str:
    """Configure render settings for the current project.

    Parameters:
    - settings: Dict of render settings. Common keys:
        "TargetDir" (str), "CustomName" (str), "SelectAllFrames" (bool),
        "MarkIn" (int), "MarkOut" (int), "ExportVideo" (bool), "ExportAudio" (bool),
        "FormatWidth" (int), "FormatHeight" (int), "FrameRate" (float)
    - render_format: Format string (e.g. "mp4", "mov"). Must be set together
      with codec.
    - codec: Codec string (e.g. "H.264", "H.265", "ProRes 422 HQ"). Must be
      set together with render_format.

    At least one of ``settings`` or the ``render_format``+``codec`` pair must
    be provided.
    """
    try:
        project = _project()
        results: Dict[str, str] = {}

        if render_format and codec:
            success = project.SetCurrentRenderFormatAndCodec(render_format, codec)
            results["format_codec"] = "set" if success else "failed"

        if settings:
            success = project.SetRenderSettings(settings)
            results["settings"] = "set" if success else "failed"

        if not results:
            return "No settings provided. Pass 'settings' dict and/or 'render_format'+'codec'."

        return json.dumps(results, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def set_render_format_and_codec(render_format: str, codec: str) -> str:
    """Set the current render format and codec together.

    Parameters:
    - render_format: Format string (e.g. "mp4", "mov", "mxf").
    - codec: Codec string (e.g. "H.264", "H.265", "ProRes 422 HQ").
    """
    try:
        project = _project()
        success = project.SetCurrentRenderFormatAndCodec(render_format, codec)
        return _ok(
            success,
            f"Render format set to '{render_format}' / codec '{codec}'",
            f"Failed to set render format '{render_format}' / codec '{codec}'. "
            "Check get_render_formats() and get_render_codecs() for valid values.",
        )
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_render_mode() -> str:
    """Get the current render mode (0 = individual clips, 1 = single clip)."""
    try:
        project = _project()
        if not hasattr(project, "GetCurrentRenderMode"):
            return "GetCurrentRenderMode is not available in this Resolve version."
        mode = project.GetCurrentRenderMode()
        return json.dumps({"render_mode": mode}, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def set_render_mode(mode: int) -> str:
    """Set the render mode.

    Parameters:
    - mode: 0 for "Individual clips", 1 for "Single clip" (whole timeline).
    """
    try:
        project = _project()
        if not hasattr(project, "SetCurrentRenderMode"):
            return "SetCurrentRenderMode is not available in this Resolve version."
        success = project.SetCurrentRenderMode(mode)
        return _ok(success, f"Render mode set to {mode}", f"Failed to set render mode to {mode}.")
    except Exception as e:
        return f"Error: {e}"


# ── Render job queue ──────────────────────────────────────────────────────


@mcp.tool()
def add_render_job() -> str:
    """Add a render job to the queue based on the current render settings."""
    try:
        project = _project()
        job_id = project.AddRenderJob()
        if job_id:
            return json.dumps({"job_id": job_id})
        return "Failed to add render job. Configure render settings first (set_render_settings)."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def delete_render_job(job_id: str) -> str:
    """Delete a single render job from the queue.

    Parameters:
    - job_id: The render job ID (as returned by ``add_render_job`` or
      ``get_render_job_list``).
    """
    try:
        project = _project()
        success = project.DeleteRenderJob(job_id)
        return _ok(
            success,
            f"Render job '{job_id}' deleted",
            f"Failed to delete render job '{job_id}'. Check get_render_job_list() for valid job IDs.",
        )
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def delete_all_render_jobs() -> str:
    """Delete every render job currently in the render queue."""
    try:
        project = _project()
        success = project.DeleteAllRenderJobs()
        return _ok(success, "All render jobs deleted", "Failed to delete all render jobs.")
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_render_job_list() -> str:
    """List all render jobs currently in the render queue, with their settings/status."""
    try:
        project = _project()
        jobs = project.GetRenderJobList()
        return json.dumps({"render_jobs": safe_serialize(jobs) if jobs else []}, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_render_job_status(job_id: str) -> str:
    """Get the status (progress, completion, error) of a specific render job.

    Parameters:
    - job_id: The render job ID (as returned by ``add_render_job`` or
      ``get_render_job_list``).
    """
    try:
        project = _project()
        status = project.GetRenderJobStatus(job_id)
        if not status:
            return f"No render job found with ID '{job_id}'"
        return json.dumps(safe_serialize(status), indent=2)
    except Exception as e:
        return f"Error: {e}"


# ── Rendering control ─────────────────────────────────────────────────────


@mcp.tool()
def start_rendering(job_ids: Optional[List[str]] = None) -> str:
    """Start rendering queued jobs.

    Parameters:
    - job_ids: Optional list of render job IDs to render. If omitted,
      renders all queued jobs.
    """
    try:
        project = _project()
        if job_ids:
            success = project.StartRendering(job_ids)
        else:
            success = project.StartRendering()
        return _ok(success, "Rendering started", "Failed to start rendering. Check render job queue.")
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def stop_rendering() -> str:
    """Stop any currently running render process."""
    try:
        project = _project()
        project.StopRendering()
        return "Rendering stopped"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def is_rendering() -> str:
    """Check whether a render is currently in progress."""
    try:
        project = _project()
        in_progress = project.IsRenderingInProgress()
        return json.dumps({"is_rendering": bool(in_progress)})
    except Exception as e:
        return f"Error: {e}"
