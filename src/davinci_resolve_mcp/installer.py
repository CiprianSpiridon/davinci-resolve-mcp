"""
Setup & doctor helpers for davinci-resolve-mcp.

Exposed through the console script as ``davinci-resolve-mcp setup`` and
``davinci-resolve-mcp doctor`` (see :func:`davinci_resolve_mcp.server.main`),
and driven for a true first-run by the repo-root ``install.py`` bootstrapper.

``setup`` registers this server with one or more MCP clients (writes/merges
their JSON config, with a timestamped backup) or uses the ``claude`` CLI for
Claude Code. ``doctor`` verifies the install (tool count) and, if Resolve is
running, that a live connection can be made.

Everything here is stdlib-only so it works in any interpreter that can import
the package.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

SERVER_KEY = "davinci-resolve"


# ── client registry ──────────────────────────────────────────────────────

def _home() -> Path:
    return Path.home()


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA", _home() / "AppData" / "Roaming"))


def _xdg_config() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", _home() / ".config"))


# File-based clients: name -> per-OS config path. All use the "mcpServers" key.
_FILE_CLIENTS: Dict[str, Dict[str, Callable[[], Path]]] = {
    "claude-desktop": {
        "Darwin": lambda: _home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
        "Windows": lambda: _appdata() / "Claude" / "claude_desktop_config.json",
        "Linux": lambda: _xdg_config() / "Claude" / "claude_desktop_config.json",
    },
    "cursor": {
        "Darwin": lambda: _home() / ".cursor" / "mcp.json",
        "Windows": lambda: _home() / ".cursor" / "mcp.json",
        "Linux": lambda: _home() / ".cursor" / "mcp.json",
    },
    "windsurf": {
        "Darwin": lambda: _home() / ".codeium" / "windsurf" / "mcp_config.json",
        "Windows": lambda: _home() / ".codeium" / "windsurf" / "mcp_config.json",
        "Linux": lambda: _home() / ".codeium" / "windsurf" / "mcp_config.json",
    },
}

ALL_CLIENTS = ["claude-code"] + list(_FILE_CLIENTS) + ["manual"]


def _server_command() -> str:
    """Absolute path to the installed console script (next to this interpreter)."""
    override = os.environ.get("RESOLVE_MCP_SERVER_PATH")
    if override:
        return override
    bindir = Path(sys.executable).parent
    exe = "davinci-resolve-mcp.exe" if platform.system() == "Windows" else "davinci-resolve-mcp"
    candidate = bindir / exe
    return str(candidate if candidate.exists() else exe)


def _server_entry(env: Optional[Dict[str, str]]) -> dict:
    entry: dict = {"command": _server_command()}
    if env:
        entry["env"] = dict(env)
    return entry


def _client_path(name: str) -> Optional[Path]:
    spec = _FILE_CLIENTS.get(name)
    if not spec:
        return None
    fn = spec.get(platform.system())
    return fn() if fn else None


# ── config merge (pure, unit-testable) ───────────────────────────────────

def merge_config(existing: Optional[dict], entry: dict, key: str = "mcpServers") -> dict:
    """Return a new config dict with our server merged under *key*.

    Preserves any other servers and top-level keys the user already has.
    """
    cfg = dict(existing) if isinstance(existing, dict) else {}
    servers = dict(cfg.get(key) or {})
    servers[SERVER_KEY] = entry
    cfg[key] = servers
    return cfg


def _write_file_client(name: str, entry: dict, dry_run: bool) -> str:
    path = _client_path(name)
    if path is None:
        return f"  - {name}: unsupported on {platform.system()} — skipped"
    existing = None
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            existing = None  # corrupt/unreadable -> start fresh but back it up
    merged = merge_config(existing, entry)
    if dry_run:
        return f"  - {name}: would write {path}\n{json.dumps({'mcpServers': {SERVER_KEY: entry}}, indent=2)}"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_suffix(path.suffix + ".davinci-mcp.bak")
        try:
            shutil.copy2(path, backup)
        except OSError:
            pass
    path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    return f"  - {name}: configured {path}"


def _register_claude_code(entry: dict, dry_run: bool) -> str:
    claude = shutil.which("claude")
    if not claude:
        return (
            "  - claude-code: `claude` CLI not found. Run manually:\n"
            f"      claude mcp add {SERVER_KEY} --scope user -- {entry['command']}"
        )
    cmd = [claude, "mcp", "add", SERVER_KEY, "--scope", "user"]
    for k, v in (entry.get("env") or {}).items():
        cmd += ["-e", f"{k}={v}"]
    cmd += ["--", entry["command"]]
    if dry_run:
        return "  - claude-code: would run " + " ".join(cmd)
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return f"  - claude-code: registered via `claude mcp add` ({entry['command']})"
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()[-200:]
        if "already exists" in detail.lower():
            return "  - claude-code: already registered (unchanged)"
        return f"  - claude-code: `claude mcp add` failed: {detail}"


def _manual(entry: dict) -> str:
    block = json.dumps({"mcpServers": {SERVER_KEY: entry}}, indent=2)
    return "  - manual: add this to your MCP client config:\n" + block


# ── public entry points ──────────────────────────────────────────────────

def run_setup(clients: List[str], env: Optional[Dict[str, str]] = None, dry_run: bool = False) -> int:
    """Register the server with the requested clients. Returns an exit code."""
    if not clients or clients == ["all"]:
        clients = [c for c in ALL_CLIENTS if c != "manual"]
    entry = _server_entry(env)
    print(f"davinci-resolve-mcp setup — command: {entry['command']}")
    if dry_run:
        print("(dry run — nothing will be written)")
    lines: List[str] = []
    for name in clients:
        if name == "claude-code":
            lines.append(_register_claude_code(entry, dry_run))
        elif name == "manual":
            lines.append(_manual(entry))
        elif name in _FILE_CLIENTS:
            lines.append(_write_file_client(name, entry, dry_run))
        else:
            lines.append(f"  - {name}: unknown client (valid: {', '.join(ALL_CLIENTS)})")
    print("\n".join(lines))
    print(
        "\nNext: restart your MCP client, ensure DaVinci Resolve is running with a "
        "project open, and that Preferences > General > 'External scripting using' = "
        "Local. Then run:  davinci-resolve-mcp doctor"
    )
    return 0


def run_doctor() -> int:
    """Verify the install and (if Resolve is up) a live connection."""
    import asyncio

    ok = True
    print("davinci-resolve-mcp doctor")
    print(f"  python:   {sys.version.split()[0]} ({sys.executable})")
    print(f"  platform: {platform.system()}")
    try:
        from .server import mcp  # noqa: WPS433

        names = [t.name for t in asyncio.run(mcp.list_tools())]
        unique = len(names) == len(set(names))
        print(f"  server:   OK — {len(names)} tools registered, unique={unique}")
        if not (len(names) >= 100 and unique):
            ok = False
    except Exception as exc:  # noqa: BLE001
        print(f"  server:   FAILED to import — {exc}")
        return 1

    try:
        from .connection import get_resolve_connection

        conn = get_resolve_connection()
        project = conn.get_project()
        print(f"  resolve:  CONNECTED — project '{project.GetName()}'")
    except Exception as exc:  # noqa: BLE001
        print(
            f"  resolve:  not connected ({exc}).\n"
            "            This is expected if Resolve isn't running / no project is "
            "open / External scripting isn't set to Local. The server itself is fine."
        )
    print("  result:   " + ("healthy" if ok else "PROBLEM — see above"))
    return 0 if ok else 1


def main(argv: Optional[List[str]] = None) -> int:
    """Dispatch ``setup`` / ``doctor`` (used by the console script)."""
    import argparse

    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else ""
    rest = argv[1:]

    if cmd == "doctor":
        return run_doctor()

    if cmd == "setup":
        p = argparse.ArgumentParser(prog="davinci-resolve-mcp setup")
        p.add_argument(
            "--clients",
            default="all",
            help=f"comma-separated: {', '.join(ALL_CLIENTS)}, or 'all' (default)",
        )
        p.add_argument("--dry-run", action="store_true", help="preview without writing")
        p.add_argument(
            "--log-level",
            default=None,
            help="set RESOLVE_MCP_LOG_LEVEL in the written config (DEBUG/INFO/WARNING/ERROR)",
        )
        ns = p.parse_args(rest)
        clients = [c.strip() for c in ns.clients.split(",") if c.strip()]
        env = {"RESOLVE_MCP_LOG_LEVEL": ns.log_level} if ns.log_level else None
        return run_setup(clients, env=env, dry_run=ns.dry_run)

    print(
        "usage: davinci-resolve-mcp <setup|doctor> [options]\n"
        "  setup   register this server with your MCP client(s)\n"
        "  doctor  verify the install and Resolve connection\n"
        "  (no subcommand) run the MCP server over stdio"
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
