#!/usr/bin/env python3
"""
davinci-resolve-mcp — one-command installer / bootstrapper.

Run this from a clone of the repo for a full first-run setup:

    python3 install.py                          # venv + install + register all clients
    python3 install.py --clients cursor,claude-code
    python3 install.py --no-venv                # install into the current interpreter
    python3 install.py --dry-run --clients all  # preview, write nothing
    python3 install.py doctor                   # just run the health check

It: (1) creates a local ``.venv`` (unless ``--no-venv``), (2) editable-installs the
package into it, then (3) delegates to ``davinci-resolve-mcp setup`` /
``doctor`` (see :mod:`davinci_resolve_mcp.installer`) to register the server with your
MCP clients and verify the install. Stdlib-only so it runs on a bare Python 3.10+.
"""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
from pathlib import Path

MIN_PY = (3, 10)
REPO = Path(__file__).resolve().parent


def _fail(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts" if platform.system() == "Windows" else "bin") / (
        "python.exe" if platform.system() == "Windows" else "python"
    )


def _venv_server(venv: Path) -> Path:
    return venv / ("Scripts" if platform.system() == "Windows" else "bin") / (
        "davinci-resolve-mcp.exe" if platform.system() == "Windows" else "davinci-resolve-mcp"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="install.py",
        description="One-command installer for davinci-resolve-mcp.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", nargs="?", default="setup", choices=["setup", "doctor"],
                        help="setup (default) or doctor")
    parser.add_argument("--clients", default="all",
                        help="comma-separated clients or 'all' (claude-code,claude-desktop,cursor,windsurf,manual)")
    parser.add_argument("--no-venv", action="store_true", help="use the current interpreter, don't create .venv")
    parser.add_argument("--dry-run", action="store_true", help="preview client registration without writing")
    parser.add_argument("--log-level", default=None, help="set RESOLVE_MCP_LOG_LEVEL in written configs")
    args = parser.parse_args()

    if sys.version_info < MIN_PY:
        _fail(f"Python {MIN_PY[0]}.{MIN_PY[1]}+ required, found {platform.python_version()}")

    if not (REPO / "pyproject.toml").exists():
        _fail(f"run this from the repo root (no pyproject.toml at {REPO})")

    # 1) choose interpreter (venv or current)
    if args.no_venv:
        py = Path(sys.executable)
        server = Path(sys.executable).parent / _venv_server(Path()).name
    else:
        venv = REPO / ".venv"
        if not venv.exists():
            print(f"Creating virtualenv at {venv} ...")
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        py = _venv_python(venv)
        server = _venv_server(venv)

    # 2) editable install
    print("Installing davinci-resolve-mcp (editable) ...")
    subprocess.run([str(py), "-m", "pip", "install", "--upgrade", "pip", "-q"], check=True)
    subprocess.run([str(py), "-m", "pip", "install", "-e", ".", "-q"], cwd=str(REPO), check=True)

    # 3) doctor-only?
    if args.command == "doctor":
        return subprocess.run([str(server), "doctor"]).returncode

    # 3) register with clients (tell the installed server exactly where it lives)
    env = dict(os.environ)
    env["RESOLVE_MCP_SERVER_PATH"] = str(server)
    setup_cmd = [str(server), "setup", "--clients", args.clients]
    if args.dry_run:
        setup_cmd.append("--dry-run")
    if args.log_level:
        setup_cmd += ["--log-level", args.log_level]
    rc = subprocess.run(setup_cmd, env=env).returncode

    print(f"\nInstalled. Server binary: {server}")
    print("Run a health check any time with:  " + str(server) + " doctor")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
