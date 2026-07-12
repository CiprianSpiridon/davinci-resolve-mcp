"""
Screenshot tool — gives the LLM "eyes" onto the running DaVinci Resolve UI.

This module implements a single ``screenshot`` tool that captures the current
screen (macOS: the Resolve window specifically, when it can be located via
Quartz; otherwise a full-screen grab) and returns it as an in-band MCP
``Image``. Capture is entirely OS-native (``screencapture`` on macOS; a
best-effort attempt via ``mss``/PIL on Windows and Linux) — nothing here
touches the Resolve scripting API, so this module never needs a live
connection.

Platform notes:
- macOS: uses the built-in ``screencapture`` CLI. If the optional ``Quartz``
  module (part of PyObjC, commonly bundled with DaVinci Resolve's own Python
  or available via ``pyobjc-framework-Quartz``) is importable, we try to find
  the main DaVinci Resolve window and capture just that window; otherwise we
  fall back to a full-screen capture.
- Windows/Linux: no equivalent to ``screencapture`` ships with the OS, so we
  make a best-effort attempt using the optional ``mss`` package (if
  installed) to grab the full screen. If ``mss`` isn't available, or the
  platform has no supported backend (e.g. headless Linux with no display),
  the tool raises a clear, actionable ``RuntimeError`` instead of crashing.

Nothing in this module imports ``DaVinciResolveScript`` or connects to
Resolve at import time, and the optional ``Quartz`` import is guarded so the
module imports cleanly on any platform, including Linux with no Quartz and
no display.
"""

from __future__ import annotations

import io
import os
import platform
import subprocess
import tempfile

from mcp.server.fastmcp import Image

from ..app import mcp


def _find_resolve_window_id() -> int | None:
    """Find the CGWindowID of the main DaVinci Resolve window via Quartz.

    macOS-only and entirely optional: if the ``Quartz`` package (PyObjC)
    isn't installed, this quietly returns ``None`` so the caller falls back
    to a full-screen capture.
    """
    try:
        import Quartz  # type: ignore[import-not-found]  # macOS-only, optional

        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
        )
        for w in windows:
            owner = w.get("kCGWindowOwnerName", "")
            if "DaVinci Resolve" in str(owner):
                layer = w.get("kCGWindowLayer", 999)
                # The main window sits on layer 0; skip menus/tooltips/etc.
                if layer == 0:
                    return w.get("kCGWindowNumber")
    except ImportError:
        pass
    except Exception:
        # Any other Quartz failure (e.g. no display, permission issues) just
        # falls back to a full-screen capture rather than failing the tool.
        pass
    return None


def _capture_macos(allow_fullscreen_fallback: bool = True) -> bytes:
    """Capture the Resolve window (or full screen as fallback) on macOS.

    Returns PNG bytes. Requires the ``screencapture`` CLI (built into
    macOS) and, for window targeting, Screen Recording permission granted
    to the host application. When the Resolve window can't be located and
    ``allow_fullscreen_fallback`` is False, raises instead of grabbing the
    whole desktop.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    try:
        wid = _find_resolve_window_id()
        if wid is not None:
            cmd = ["screencapture", "-x", "-l", str(wid), tmp.name]
        elif allow_fullscreen_fallback:
            cmd = ["screencapture", "-x", tmp.name]
        else:
            raise RuntimeError(
                "Could not locate the DaVinci Resolve window and "
                "allow_fullscreen_fallback is False, so a full-desktop capture "
                "was declined. Ensure Resolve is running and visible, or pass "
                "allow_fullscreen_fallback=True to capture the whole screen."
            )

        r = subprocess.run(cmd, capture_output=True)
        if r.returncode != 0:
            raise RuntimeError(
                "screencapture failed. Grant Screen Recording permission to "
                "the host app in System Settings > Privacy & Security > "
                "Screen Recording, then try again."
            )
        with open(tmp.name, "rb") as f:
            data = f.read()
        if not data:
            raise RuntimeError("screencapture produced an empty file.")
        return data
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _capture_windows_or_linux(allow_fullscreen_fallback: bool = True) -> bytes:
    """Best-effort full-screen capture on Windows/Linux via the optional
    ``mss`` package, re-encoded to PNG via Pillow.

    Neither Windows nor Linux ship a built-in CLI equivalent to macOS's
    ``screencapture``, so this relies on the optional third-party ``mss``
    package (fast, cross-platform screen grabbing) plus Pillow for PNG
    encoding. Raises ``RuntimeError`` with actionable install instructions
    if either dependency is missing, or if there's no display to capture
    (e.g. a headless Linux session with no X11/Wayland).

    There is no window-scoped capture on these platforms, so the grab always
    covers the full desktop; when ``allow_fullscreen_fallback`` is False the
    capture is declined rather than exposing unrelated windows.
    """
    if not allow_fullscreen_fallback:
        raise RuntimeError(
            "Window-scoped capture is not supported on Windows/Linux; only a "
            "full-desktop grab is possible here. Pass allow_fullscreen_fallback="
            "True to allow it."
        )
    try:
        import mss  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            "Screenshot capture on this platform requires the optional "
            "'mss' package. Install it with: pip install mss pillow"
        ) from e

    try:
        from PIL import Image as PILImage  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            "Screenshot capture on this platform requires the optional "
            "'pillow' package. Install it with: pip install mss pillow"
        ) from e

    try:
        with mss.mss() as sct:
            # Monitor 0 is the union of all monitors; capture the primary
            # screen (or the full virtual desktop) as a reasonable default.
            monitor = sct.monitors[0]
            raw = sct.grab(monitor)
    except Exception as e:
        raise RuntimeError(
            f"Failed to capture the screen: {e}. This usually means there's "
            f"no accessible display (e.g. a headless session)."
        ) from e

    img = PILImage.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()
    if not data:
        raise RuntimeError("Screen capture produced an empty image.")
    return data


def _capture_screenshot(allow_fullscreen_fallback: bool = True) -> bytes:
    """Capture the current screen and return PNG bytes, dispatching by OS.

    Raises ``RuntimeError`` with a clear, actionable message if the current
    platform has no supported capture backend available.
    """
    system = platform.system()
    if system == "Darwin":
        return _capture_macos(allow_fullscreen_fallback)
    if system in ("Windows", "Linux"):
        return _capture_windows_or_linux(allow_fullscreen_fallback)
    raise RuntimeError(
        f"Screenshot capture is not supported on this platform ({system!r})."
    )


@mcp.tool()
def screenshot(allow_fullscreen_fallback: bool = True) -> Image:
    """
    Take a screenshot so you can SEE the current state of the DaVinci Resolve
    UI. Useful before/after a visual change, when the user describes something
    visual, or to verify what's on screen.

    Privacy note: on macOS this targets the Resolve window specifically when it
    can be located. If it can't (or on Windows/Linux, which have no
    window-scoped capture), the grab covers the FULL desktop, which may include
    unrelated or sensitive windows. Set allow_fullscreen_fallback=False to
    decline a full-desktop capture and get a clear error instead.

    Parameters:
    - allow_fullscreen_fallback: when True (default) fall back to a full-screen
      grab if the Resolve window can't be captured on its own; when False,
      raise rather than capture the whole desktop.

    Platform support:
    - macOS: captures the DaVinci Resolve window directly when it can be
      located. Requires Screen Recording permission for the host app.
    - Windows/Linux: best-effort full-desktop capture via the optional
      'mss'/'pillow' packages; raises a clear error if they aren't
      installed or no display is available.

    Raises RuntimeError with an actionable message if capture fails or the
    platform/environment has no supported capture backend.
    """
    try:
        png_data = _capture_screenshot(allow_fullscreen_fallback)
        if not png_data:
            raise RuntimeError("Screenshot captured but the image was empty.")
        return Image(data=png_data, format="png")
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Error taking screenshot: {e}")
