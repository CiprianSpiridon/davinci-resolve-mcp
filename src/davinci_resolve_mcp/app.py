"""
Core FastMCP application instance for the DaVinci Resolve MCP server.

This module owns the single ``mcp`` object that every ``tools.*`` module
imports and registers tools against via ``@mcp.tool()``. It is intentionally
minimal: it configures logging, defines a best-effort startup lifespan that
tries to connect to a running DaVinci Resolve instance (without ever failing
server startup if Resolve isn't reachable yet), and registers the
``editing_strategy`` prompt that gives an LLM client a recommended workflow
for driving Resolve through this server's tools.

IMPORTANT: this module must NOT import any ``tools.*`` module — doing so
would create either a circular import (tool modules import ``mcp`` from
here) or force tool registration order to live here instead of in the
top-level server entry point. It also must never import
``DaVinciResolveScript`` or otherwise touch a live Resolve instance at
import time; the only Resolve contact happens lazily, inside the lifespan
coroutine, via :func:`davinci_resolve_mcp.connection.get_resolve_connection`.
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict

from mcp.server.fastmcp import FastMCP

from .connection import get_resolve_connection

# ── Logging ──────────────────────────────────────────────────────────────
# MCP servers speak protocol messages over stdout, so ALL logging must go to
# stderr — anything written to stdout would corrupt the protocol stream.
# Level is configurable via RESOLVE_MCP_LOG_LEVEL (DEBUG/INFO/WARNING/ERROR),
# defaulting to INFO; an unrecognized value falls back to INFO.
_LOG_LEVEL = getattr(
    logging, os.getenv("RESOLVE_MCP_LOG_LEVEL", "INFO").upper(), logging.INFO
)
logging.basicConfig(
    level=_LOG_LEVEL,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("davinci_resolve_mcp.app")


# ── Lifespan ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Best-effort connect to DaVinci Resolve on startup.

    Connection failures are logged and swallowed — the server must always
    finish starting up even if Resolve isn't running yet, since every tool
    reconnects lazily via ``get_resolve_connection()`` on its own first call.
    """
    logger.info("DaVinciResolveMCP server starting up")
    try:
        conn = get_resolve_connection()
        project = conn.get_project()
        logger.info("Connected to Resolve — project: %s", project.GetName())
    except Exception as exc:  # noqa: BLE001 - startup connect is best-effort
        logger.warning("Could not connect to Resolve on startup: %s", exc)
        logger.warning("Tools will attempt to connect when called.")

    try:
        yield {}
    finally:
        logger.info("DaVinciResolveMCP server shutting down")


# ── The single shared FastMCP instance ──────────────────────────────────
# Every tools.* module does `from ..app import mcp` and registers tools with
# `@mcp.tool()` against this exact instance. Nothing else should construct a
# FastMCP instance anywhere else in this package.
mcp = FastMCP("DaVinciResolveMCP", lifespan=server_lifespan)


# ── Prompt: recommended editing workflow ────────────────────────────────

@mcp.prompt()
def editing_strategy() -> str:
    """Recommended workflow guidance for driving DaVinci Resolve through this MCP server."""
    return """When working with DaVinci Resolve through MCP, follow this workflow:

    1. ALWAYS start by checking the current state:
       - Use get_project_info() to understand the open project (name,
         frame rate, resolution, current page).
       - Use get_current_timeline_info() to see the active timeline, if any.
       - Use get_current_page() to know which Resolve page is active
         (Media / Cut / Edit / Fusion / Color / Fairlight / Deliver) —
         many tools only make sense on a specific page.
       - If a screenshot tool is available, use it before and after
         changes to visually confirm the state of the UI.

    2. For media management:
       - Use get_media_pool_structure() to see available bins and clips.
       - Use import_media() to bring new footage into the media pool.
       - Use create_empty_timeline() or create_timeline_from_clips() to
         start a new edit.
       - Use append_to_timeline() to add clips to the current timeline.

    3. For editing operations:
       - Use get_timeline_items() to see what's on each track.
       - Use set_timeline_item_property() for transforms (Pan, Tilt,
         Zoom, Opacity, Crop) on a specific clip instance.
       - Use add_marker() to mark important points on the timeline.
       - Use set_current_timecode() to navigate the playhead.

    4. For color grading (switch to the Color page first with
       open_page("color")):
       - Use get_node_graph() to inspect the current grade's node tree.
       - Use set_lut() to apply LUTs to a node.
       - Use set_cdl() for primary CDL (slope/offset/power/saturation)
         adjustments.
       - Use grab_still() to capture a still of the current grade.

    5. For rendering and delivery:
       - Use get_render_formats() / get_render_presets() to see available
         output options.
       - Use set_render_settings() to configure the output before queuing.
       - Use add_render_job() then start_rendering() to render, and
         get_render_job_status() to monitor progress.

    6. For anything not covered by a specific tool:
       - Prefer the most specific tool available; only fall back to a
         generic "execute arbitrary code" escape hatch (if the server
         exposes one) for cases no dedicated tool covers.

    IMPORTANT:
    - DaVinci Resolve must be running, with a project open, for most tools
      to return useful data — tools that need a project or timeline report
      a clear error string (never a crash) when one isn't available.
    - Some capabilities require DaVinci Resolve Studio (the paid edition)
      rather than the free version, and some AI-powered features require a
      recent Studio release; those tools describe the requirement in their
      own error message when unavailable rather than raising.
    """
