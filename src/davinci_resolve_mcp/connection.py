"""
Connection layer for the DaVinci Resolve MCP server.

DaVinci Resolve exposes a scripting API to external Python processes through
a native module (``DaVinciResolveScript``) that is *not* installed in a normal
site-packages location. Reaching it requires two things to be set up before
the module is imported:

  1. ``RESOLVE_SCRIPT_LIB`` — path to the Resolve fusionscript shared library.
  2. ``RESOLVE_SCRIPT_API`` — path to the Resolve scripting API folder, whose
     ``Modules`` subdirectory must be added to ``sys.path`` so that
     ``import DaVinciResolveScript`` succeeds.

Everything in this module is LAZY: importing this file never imports
``DaVinciResolveScript`` and never touches a running Resolve instance. The
connection is only established the first time a caller invokes
:func:`get_resolve_connection` (or ``ResolveConnection.connect`` directly).

The :class:`ResolveConnection` class also provides fresh, lock-guarded
accessors for the commonly used Resolve objects (project manager, current
project, media pool, current timeline, media storage, gallery) since these
objects can become stale as the user interacts with the Resolve UI, and a
generic :meth:`ResolveConnection.execute_code` escape hatch for running
arbitrary snippets against the live API.
"""

from __future__ import annotations

import io
import logging
import os
import sys
import threading
import traceback
from contextlib import redirect_stdout
from typing import Any, Optional

logger = logging.getLogger("davinci_resolve_mcp.connection")

# ── Default install paths per platform ──────────────────────────────────────
# These are only used as a fallback when the corresponding environment
# variable is not already set. Users with non-standard installs can always
# override via RESOLVE_SCRIPT_LIB / RESOLVE_SCRIPT_API.
_PLATFORM_DEFAULT_PATHS: dict[str, dict[str, str]] = {
    "darwin": {
        "script_api": (
            "/Library/Application Support/Blackmagic Design/"
            "DaVinci Resolve/Developer/Scripting"
        ),
        "script_lib": (
            "/Applications/DaVinci Resolve/DaVinci Resolve.app/"
            "Contents/Libraries/Fusion/fusionscript.so"
        ),
    },
    "win32": {
        "script_api": os.path.join(
            os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
            "Blackmagic Design",
            "DaVinci Resolve",
            "Support",
            "Developer",
            "Scripting",
        ),
        "script_lib": os.path.join(
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            "Blackmagic Design",
            "DaVinci Resolve",
            "fusionscript.dll",
        ),
    },
    "linux": {
        "script_api": "/opt/resolve/Developer/Scripting",
        "script_lib": "/opt/resolve/libs/Fusion/fusionscript.so",
    },
}


def _platform_key() -> str:
    """Map ``sys.platform`` to one of 'darwin' / 'win32' / 'linux'.

    Any platform we don't specifically recognize (BSD variants, etc.) falls
    back to the 'linux' defaults rather than raising, since the Resolve
    Linux layout is the most POSIX-generic of the three.
    """
    if sys.platform.startswith("darwin"):
        return "darwin"
    if sys.platform.startswith("win") or sys.platform.startswith("cygwin"):
        return "win32"
    return "linux"


class ResolveConnection:
    """Owns a single connection to a running DaVinci Resolve instance.

    All access to the underlying Resolve API objects goes through an
    ``RLock`` so this class is safe to share across threads (the MCP server
    may service tool calls concurrently). The lock is reentrant so nested
    accessor calls made from within a locked section (e.g. ``get_media_pool``
    calling ``get_project``) do not deadlock.
    """

    def __init__(self) -> None:
        self.resolve: Any = None
        self._lock = threading.RLock()

    # ── Connection lifecycle ────────────────────────────────────────────

    def connect(self) -> bool:
        """Attempt to establish a connection to Resolve.

        Idempotent: if already connected, returns True immediately without
        reaching out to Resolve again. ``DaVinciResolveScript`` is imported
        here, inside the method body, and nowhere else in this module.
        """
        with self._lock:
            if self.resolve is not None:
                return True

            try:
                self._configure_environment()
                import DaVinciResolveScript as dvr_script  # type: ignore

                resolve = dvr_script.scriptapp("Resolve")
                if resolve is None:
                    logger.error(
                        "scriptapp('Resolve') returned None. Is DaVinci "
                        "Resolve running with external scripting enabled?"
                    )
                    return False
                self.resolve = resolve
                version = self._safe_call(resolve.GetVersionString)
                logger.info(
                    "Connected to DaVinci Resolve (version=%s)",
                    version or "unknown",
                )
                return True
            except ImportError as exc:
                logger.error(
                    "Could not import DaVinciResolveScript (%s). Set "
                    "RESOLVE_SCRIPT_API / RESOLVE_SCRIPT_LIB or install "
                    "DaVinci Resolve at the default location for this OS.",
                    exc,
                )
                return False
            except Exception as exc:  # noqa: BLE001 - connection is best-effort
                logger.error("Failed to connect to DaVinci Resolve: %s", exc)
                return False

    def disconnect(self) -> None:
        """Drop the current handle. A later call to ``connect`` reconnects."""
        with self._lock:
            self.resolve = None
            logger.info("Disconnected from DaVinci Resolve")

    def _configure_environment(self) -> None:
        """Populate env vars / sys.path needed to import DaVinciResolveScript.

        Respects any values the user (or their shell profile / .env) already
        set; only fills in gaps using the per-platform defaults.
        """
        defaults = _PLATFORM_DEFAULT_PATHS[_platform_key()]

        script_lib = os.environ.get("RESOLVE_SCRIPT_LIB")
        if not script_lib:
            candidate = defaults["script_lib"]
            if os.path.exists(candidate):
                os.environ["RESOLVE_SCRIPT_LIB"] = candidate
                script_lib = candidate
                logger.info("Auto-configured RESOLVE_SCRIPT_LIB=%s", candidate)

        script_api = os.environ.get("RESOLVE_SCRIPT_API")
        if not script_api:
            script_api = defaults["script_api"]
            os.environ["RESOLVE_SCRIPT_API"] = script_api
            logger.info("Auto-configured RESOLVE_SCRIPT_API=%s", script_api)

        modules_path = os.path.join(script_api, "Modules")
        if modules_path not in sys.path:
            sys.path.insert(0, modules_path)
            logger.info("Added Resolve Modules directory to sys.path: %s", modules_path)

        # On Windows, fusionscript.dll pulls in sibling DLLs from its own
        # install directory; Python 3.8+ no longer implicitly searches PATH
        # for DLL dependencies, so it must be added explicitly.
        if sys.platform == "win32" and script_lib:
            lib_dir = os.path.dirname(script_lib)
            add_dll_directory = getattr(os, "add_dll_directory", None)
            if add_dll_directory and os.path.isdir(lib_dir):
                try:
                    add_dll_directory(lib_dir)
                except OSError:
                    pass

    @staticmethod
    def _safe_call(fn, *args, **kwargs):
        """Invoke a Resolve API callable, swallowing errors as ``None``."""
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Resolve API call %s failed: %s", getattr(fn, "__name__", fn), exc
            )
            return None

    def _ensure_connected(self) -> None:
        if self.resolve is None:
            raise ConnectionError(
                "Not connected to DaVinci Resolve. Make sure Resolve is "
                "running and external scripting is enabled in "
                "Preferences > General > External scripting using."
            )

    def is_alive(self) -> bool:
        """Best-effort liveness check for the current handle.

        Returns False (rather than raising) whenever the handle is missing
        or any Resolve call along the way fails, so callers can use this to
        decide whether to reconnect.
        """
        with self._lock:
            if self.resolve is None:
                return False
            try:
                pm = self.resolve.GetProjectManager()
                return pm is not None
            except Exception:  # noqa: BLE001
                return False

    # ── Accessors ────────────────────────────────────────────────────────
    # Each accessor re-fetches the object from Resolve rather than caching
    # it, since Resolve invalidates handles (e.g. current project/timeline)
    # as the user interacts with the app.

    def get_resolve(self) -> Any:
        with self._lock:
            self._ensure_connected()
            return self.resolve

    def get_project_manager(self) -> Any:
        with self._lock:
            self._ensure_connected()
            pm = self.resolve.GetProjectManager()
            if pm is None:
                raise RuntimeError(
                    "Could not get ProjectManager — is Resolve still running?"
                )
            return pm

    def get_project(self) -> Any:
        with self._lock:
            pm = self.get_project_manager()
            project = pm.GetCurrentProject()
            if project is None:
                raise RuntimeError("No project is currently open in DaVinci Resolve")
            return project

    def get_media_pool(self) -> Any:
        with self._lock:
            project = self.get_project()
            media_pool = project.GetMediaPool()
            if media_pool is None:
                raise RuntimeError("Could not get MediaPool from the current project")
            return media_pool

    def get_current_timeline(self) -> Any:
        with self._lock:
            project = self.get_project()
            return project.GetCurrentTimeline()

    def get_media_storage(self) -> Any:
        with self._lock:
            self._ensure_connected()
            media_storage = self.resolve.GetMediaStorage()
            if media_storage is None:
                raise RuntimeError("Could not get MediaStorage from Resolve")
            return media_storage

    def get_gallery(self) -> Any:
        with self._lock:
            project = self.get_project()
            gallery = project.GetGallery()
            if gallery is None:
                raise RuntimeError("Could not get Gallery from the current project")
            return gallery

    # ── Arbitrary code execution ────────────────────────────────────────

    def execute_code(self, code: str) -> str:
        """Execute a snippet of Python with Resolve API objects in scope.

        The executed code sees the following names pre-populated (any that
        could not be resolved, e.g. no project open, are ``None``):
          - ``resolve``      the top-level Resolve app object
          - ``project``      the current project, if any
          - ``mediaPool``    the current project's media pool, if any
          - ``timeline``     the current timeline, if any
          - ``mediaStorage`` the Resolve media storage object

        Anything printed to stdout during execution is captured and
        returned. If the snippet assigns a ``result`` variable, its
        ``repr``/``str`` is appended to (or used as) the returned text.
        Exceptions raised by the snippet are caught and reported as text
        rather than propagated, so a bad snippet cannot crash the tool call.
        """
        with self._lock:
            self._ensure_connected()

            project = None
            media_pool = None
            timeline = None
            media_storage = None

            try:
                pm = self.resolve.GetProjectManager()
                if pm is not None:
                    project = pm.GetCurrentProject()
            except Exception:  # noqa: BLE001
                pass

            if project is not None:
                try:
                    media_pool = project.GetMediaPool()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    timeline = project.GetCurrentTimeline()
                except Exception:  # noqa: BLE001
                    pass

            try:
                media_storage = self.resolve.GetMediaStorage()
            except Exception:  # noqa: BLE001
                pass

            namespace: dict[str, Any] = {
                "resolve": self.resolve,
                "project": project,
                "mediaPool": media_pool,
                "timeline": timeline,
                "mediaStorage": media_storage,
            }

            captured = io.StringIO()
            try:
                with redirect_stdout(captured):
                    exec(code, namespace)  # noqa: S102 - explicit escape-hatch tool
                output = captured.getvalue()
                result = namespace.get("result")
                if result is not None:
                    output = f"{output}\nresult = {result}" if output else str(result)
                return output if output else "Code executed successfully (no output)"
            except Exception as exc:  # noqa: BLE001
                return f"Error executing code: {exc}\n{traceback.format_exc()}"


# ── Module-level singleton ───────────────────────────────────────────────

_connection: Optional[ResolveConnection] = None


def get_resolve_connection() -> ResolveConnection:
    """Return the shared :class:`ResolveConnection`, (re)connecting as needed.

    If a connection already exists and still responds, it is reused as-is.
    Otherwise (no connection yet, or the previous handle went stale — e.g.
    the user quit and relaunched Resolve) a fresh :class:`ResolveConnection`
    is created and connected.

    Raises:
        ConnectionError: if a connection could not be established.
    """
    global _connection

    if _connection is not None and _connection.is_alive():
        return _connection

    if _connection is not None:
        logger.warning("Resolve connection is stale; reconnecting...")

    candidate = ResolveConnection()
    if not candidate.connect():
        _connection = None
        raise ConnectionError(
            "Could not connect to DaVinci Resolve. Make sure Resolve is "
            "running and that external scripting is enabled in "
            "Preferences > General > External scripting using."
        )

    _connection = candidate
    return _connection
