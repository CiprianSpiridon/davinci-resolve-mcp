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

__all__ = ["mcp", "main"]


def main() -> None:
    """Run the DaVinci Resolve MCP server over stdio.

    This is the console-script entry point (``davinci-resolve-mcp``,
    configured in ``pyproject.toml``) as well as the target of
    ``python -m davinci_resolve_mcp`` / ``python -m
    davinci_resolve_mcp.server``. It blocks for the lifetime of the
    server, serving the MCP protocol over stdio.
    """
    mcp.run()


if __name__ == "__main__":
    main()
