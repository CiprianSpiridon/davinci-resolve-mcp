"""
Entry point for the DaVinci Resolve MCP server.

This module is the SOLE place in the package that imports every
``tools.*`` submodule. Each of those modules does ``from .app import mcp``
and registers one or more tools against that shared FastMCP instance via
``@mcp.tool()`` as a side effect of being imported — so simply importing
them here (even though none of the names are referenced afterwards) is
what wires the whole tool surface together. Adding or removing one of the
imports below directly changes the set of tools the server exposes; there
is no other registration point.

Nothing in this module — nor anything transitively imported by it — may
import ``DaVinciResolveScript`` or otherwise contact a running Resolve
instance at import time. Connections are established lazily, inside tool
bodies, via ``davinci_resolve_mcp.connection.get_resolve_connection()``.
This lets the server start (and this module be imported) even when
DaVinci Resolve isn't running.
"""

from __future__ import annotations

from .app import mcp

# Importing this module registers the read-only `resolve://...` resources
# against `mcp` via `@mcp.resource(uri)`, the same side-effect-on-import
# pattern used by the `tools.*` modules below.
from . import resources  # noqa: F401

# ── Tool module registration ────────────────────────────────────────────
# Importing each module below runs its top-level `@mcp.tool()` decorators,
# registering its tools against `mcp`. The imports are intentionally
# unused (F401) — the import itself is the side effect we want.
from .tools import ai  # noqa: F401
from .tools import audio  # noqa: F401
from .tools import code  # noqa: F401
from .tools import color  # noqa: F401
from .tools import export_still  # noqa: F401
from .tools import fusion  # noqa: F401
from .tools import media_pool  # noqa: F401
from .tools import media_pool_item  # noqa: F401
from .tools import media_storage  # noqa: F401
from .tools import project  # noqa: F401
from .tools import project_manager  # noqa: F401
from .tools import render  # noqa: F401
from .tools import resolve_app  # noqa: F401
from .tools import screenshot  # noqa: F401
from .tools import timeline  # noqa: F401
from .tools import timeline_edit  # noqa: F401
from .tools import timeline_item  # noqa: F401
from .tools import transcription  # noqa: F401

# ── Offline (no-Resolve) tool module registration ───────────────────────
# These 18 modules never contact a running Resolve instance — they operate
# purely on files (.drx/.drt/.drp, CDL, LUT, project JSON, a local SQLite
# store) and always return JSON strings (or an "Error: ..." string on
# failure). Same side-effect-on-import wiring as the live tools above:
# removing one of these imports drops exactly that tool.
from .tools import off_audio  # noqa: F401
from .tools import off_audio_plan  # noqa: F401
from .tools import off_capabilities  # noqa: F401
from .tools import off_color_trace  # noqa: F401
from .tools import off_conform  # noqa: F401
from .tools import off_deliverable  # noqa: F401
from .tools import off_drp  # noqa: F401
from .tools import off_drt  # noqa: F401
from .tools import off_drx  # noqa: F401
from .tools import off_editorial  # noqa: F401
from .tools import off_fairlight  # noqa: F401
from .tools import off_fusion  # noqa: F401
from .tools import off_media  # noqa: F401
from .tools import off_offline_ref  # noqa: F401
from .tools import off_pipeline  # noqa: F401
from .tools import off_project_db  # noqa: F401
from .tools import off_project_read  # noqa: F401
from .tools import off_provenance  # noqa: F401

__all__ = ["mcp", "main"]


def main() -> None:
    """Console-script entry point (``davinci-resolve-mcp``).

    With no arguments, run the MCP server over stdio (blocks for the server's
    lifetime). The ``setup`` and ``doctor`` subcommands are dispatched to
    :mod:`davinci_resolve_mcp.installer` instead:

    - ``davinci-resolve-mcp setup [--clients ...] [--dry-run]`` — register this
      server with your MCP client(s).
    - ``davinci-resolve-mcp doctor`` — verify the install and Resolve connection.
    """
    import sys

    if len(sys.argv) > 1 and sys.argv[1] in ("setup", "doctor"):
        from . import installer

        raise SystemExit(installer.main(sys.argv[1:]))

    mcp.run()


if __name__ == "__main__":
    main()
