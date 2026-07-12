"""Tool modules for the DaVinci Resolve MCP server.

Each sibling module in this subpackage registers one or more ``@mcp.tool()``
functions against the shared FastMCP instance defined in
``davinci_resolve_mcp.app``. This ``__init__.py`` intentionally contains no
tool definitions and imports none of its sibling modules; it exists solely
as a package marker so setuptools/importlib can discover
``davinci_resolve_mcp.tools`` as a subpackage. Tool modules are imported
explicitly elsewhere (e.g. in the server entry point) to trigger their
registration side effects.
"""
