"""Arbitrary code-execution power tool for the DaVinci Resolve MCP server.

Exposes a single escape-hatch tool, ``execute_resolve_code``, that runs a
snippet of Python inside the DaVinci Resolve scripting environment via
``ResolveConnection.execute_code``. This is meant for operations not (yet)
covered by a dedicated tool.

Nothing in this module imports ``DaVinciResolveScript`` or touches a live
Resolve instance at import time — the connection is only reached lazily,
inside the tool body, via ``_conn()``.
"""

from __future__ import annotations

from ..app import mcp
from ..helpers import _conn


@mcp.tool()
def execute_resolve_code(code: str) -> str:
    """Execute arbitrary Python code in the DaVinci Resolve scripting environment.

    Use this for operations not covered by a specific tool.

    Pre-loaded namespace variables available to the executed code:
    - resolve: the top-level Resolve object
    - project: the current project (or None)
    - mediaPool: the current project's media pool (or None)
    - timeline: the current timeline (or None)
    - mediaStorage: the Resolve media storage object

    Use print() to emit output, or assign a variable named 'result' — its
    repr/str is included in the returned text. If the snippet produces
    neither, a "no output" message is returned. Exceptions raised by the
    snippet itself are caught inside Resolve's execution and returned as an
    "Error executing code: ..." string with a traceback (never propagated);
    if establishing the Resolve connection itself fails, that is caught
    here and returned as an "Error: ..." string.

    Parameters:
    - code: Python source code to execute.
    """
    try:
        conn = _conn()
        return conn.execute_code(code)
    except Exception as e:
        return f"Error: {e}"
