"""
Shared helpers for DaVinci Resolve MCP tool modules.

These small utilities are used across ``tools/*.py`` modules to keep each
tool implementation short and consistent: getting a live connection,
requiring an active timeline, locating a specific timeline item with
actionable error messages, formatting boolean-ish results, and coercing
free-form string input (as arrives over MCP) into the Python types the
Resolve scripting API expects.

Nothing in this module imports ``DaVinciResolveScript`` or touches Resolve
at import time — the connection is only reached lazily, inside function
bodies, via :func:`_conn`.
"""

from __future__ import annotations

import math
from typing import Any

from .connection import ResolveConnection, get_resolve_connection

_VALID_TRACK_TYPES = ("video", "audio", "subtitle")


def _conn() -> ResolveConnection:
    """Shorthand for :func:`get_resolve_connection`.

    Raises ``ConnectionError`` with an actionable message when Resolve isn't
    reachable (see :func:`davinci_resolve_mcp.connection.get_resolve_connection`).
    """
    return get_resolve_connection()


def _require_timeline(conn: ResolveConnection):
    """Return the current timeline or raise with a helpful message.

    Parameters:
    - conn: an active :class:`ResolveConnection`.
    """
    timeline = conn.get_current_timeline()
    if timeline is None:
        raise RuntimeError(
            "No active timeline. Create or open a timeline first."
        )
    return timeline


def _get_timeline_item(track_type: str, track_index: int, item_index: int):
    """Look up a single ``TimelineItem`` on the current timeline.

    Parameters:
    - track_type: one of "video", "audio", "subtitle".
    - track_index: 1-based track index (as used by the Resolve API).
    - item_index: 0-based index of the item within that track's item list.

    Raises ``RuntimeError`` with an actionable message when the track type
    is invalid, the track has no items, or ``item_index`` is out of range.
    """
    if track_type not in _VALID_TRACK_TYPES:
        raise RuntimeError(
            f"Invalid track_type '{track_type}'. Must be one of: "
            f"{', '.join(_VALID_TRACK_TYPES)}."
        )

    conn = _conn()
    timeline = _require_timeline(conn)

    items = timeline.GetItemListInTrack(track_type, track_index)
    if not items:
        raise RuntimeError(
            f"No items found on {track_type} track {track_index}. "
            f"Check track_type ('video'/'audio'/'subtitle') and "
            f"track_index (1-based)."
        )
    if item_index < 0 or item_index >= len(items):
        raise RuntimeError(
            f"item_index {item_index} out of range — track has "
            f"{len(items)} item(s) (0-{len(items) - 1})."
        )
    return items[item_index]


def _ok(result: Any, success_msg: str, fail_msg: str) -> str:
    """Return ``success_msg`` if ``result`` is truthy, else ``fail_msg``."""
    return success_msg if result else fail_msg


def _coerce_value(value_str: str) -> Any:
    """Coerce a string value (as received over MCP) to int/float/bool/str.

    Rules:
    - Numeric strings that are whole numbers become ``int`` (e.g. "1.0" -> 1).
    - Other numeric strings become ``float`` (e.g. "2.5" -> 2.5).
    - "true"/"false" (any case) become ``bool``.
    - Anything else is returned unchanged as a string.

    Non-string input is returned unchanged.
    """
    if not isinstance(value_str, str):
        return value_str

    try:
        as_float = float(value_str)
        if not math.isfinite(as_float):
            # float() happily parses "inf"/"-inf"/"Infinity"/huge literals
            # like "1e400"; int() on those raises OverflowError. Treat them
            # as non-numeric and fall through to string handling below.
            raise ValueError("non-finite float")
        if as_float == int(as_float):
            return int(as_float)
        return as_float
    except (ValueError, TypeError, OverflowError):
        pass

    if value_str.lower() in ("true", "false"):
        return value_str.lower() == "true"

    return value_str
