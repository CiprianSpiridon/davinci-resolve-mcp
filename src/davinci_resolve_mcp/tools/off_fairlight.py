"""
davinci_resolve_mcp.tools.off_fairlight — offline Fairlight bus-routing
planner (OFFLINE/advanced tool set).

Registers exactly one action-dispatch tool, ``fairlight_plan``, with a
single action:

  * ``"route"`` — turn a small JSON "routing spec" (a list of bus
    definitions plus a list of track-to-bus assignments) into a validated
    Fairlight bus-routing plan: the resolved channel format/count for each
    bus, a per-track routing table (which bus each track feeds, and
    whether the track's channel count matches the bus it is routed to),
    a main-bus summary, and any routing warnings.

DaVinci Resolve's scripting API has no bus-routing surface at all — see
the reference ``fairlight.mjs`` / ``vendor/fairlight``, which patches the
reverse-engineered ``FLStudioModelBA`` binary blob inside a live project's
SQLite database (Section 6 bus-definition table, 0xBC-anchored bus-name
records, per-bus property records) to configure buses. This tool produces
the *plan* a human, or a separate DB-patching tool, would need — it never
opens a database, never touches a project file, and never connects to a
running Resolve instance. The bus-format -> channel-count table and the
Fairlight "audio bus" type code below mirror that reference (read, not
copied); this module is original Python.

Every code path returns a JSON string; nothing here raises — invalid
routing specs are reported as a clean JSON ``"error"`` field, and any
unexpected failure is caught in the tool entry point and reported as
``"Error: {e}"``. This is a read/plan-only domain (no bytes are written to
any project, database, or file), so no action here carries a ``"verified"``
field.
"""

from __future__ import annotations

import json
from typing import Any

from ..app import mcp

_VALID_ACTIONS = ("route",)

# ── Fairlight bus format -> channel-count table (mirrors the reference
# fairlight.mjs FORMAT_CHANNELS table, read from the reverse-engineered
# FLStudioModelBA bus-definition table) ──────────────────────────────────
FORMAT_CHANNELS: dict[str, int] = {
    "mono": 1,
    "stereo": 2,
    "5.1": 6,
    "5.1film": 6,
    "7.1": 8,
    "7.1.4": 16,
}

# Reverse lookup used to label a bus/track that was given an explicit
# channel count instead of a named format. Only one canonical name per
# channel count (matches the reference CHANNELS_FORMAT table).
CHANNELS_FORMAT: dict[int, str] = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1", 16: "7.1.4"}

# Fairlight "audio" bus type code from the reverse-engineered bus table.
BUS_TYPE_AUDIO = 7

# The bus-definition table in FLStudioModelBA holds at most 32 records.
MAX_BUSES = 32


def _err(action: str, message: str, **extra: Any) -> str:
    payload: dict[str, Any] = {"action": action, "error": message}
    payload.update(extra)
    return json.dumps(payload, indent=2)


def _channels_for(fmt: Any, channels: Any, where: str) -> tuple[int, str]:
    """Resolve a (channels, format_label) pair from a bus/track spec entry.

    ``fmt`` (a format key like "stereo"/"5.1") takes precedence when
    present; otherwise an explicit ``channels`` integer is used, labeled
    via the reverse lookup (or "Nch" for an unrecognized channel count).
    Raises ``ValueError`` with a message identifying ``where`` (e.g.
    "bus 'DX'") on anything invalid.
    """
    if fmt is not None:
        key = str(fmt).strip().lower()
        if key not in FORMAT_CHANNELS:
            available = ", ".join(sorted(set(FORMAT_CHANNELS)))
            raise ValueError(f"{where}: unknown format '{fmt}'. Available: {available}")
        return FORMAT_CHANNELS[key], key

    if channels is not None:
        try:
            n = int(channels)
        except (TypeError, ValueError):
            raise ValueError(f"{where}: channels must be an integer, got {channels!r}") from None
        if n <= 0:
            raise ValueError(f"{where}: channels must be a positive integer, got {n}")
        return n, CHANNELS_FORMAT.get(n, f"{n}ch")

    raise ValueError(f"{where}: must specify either 'format' or 'channels'")


def _resolve_buses(raw_buses: Any) -> list[dict[str, Any]]:
    """Validate + resolve a routing spec's ``buses`` list into concrete
    bus records: ``{id, type, channels, format, name}``. Raises
    ``ValueError`` (with a clear message) on any structural problem.
    """
    if not isinstance(raw_buses, list) or not raw_buses:
        raise ValueError("'buses' must be a non-empty list of {name, format|channels} objects")
    if len(raw_buses) > MAX_BUSES:
        raise ValueError(f"too many buses: {len(raw_buses)} (Fairlight supports at most {MAX_BUSES})")

    buses: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for i, b in enumerate(raw_buses):
        if not isinstance(b, dict):
            raise ValueError(f"bus #{i}: must be an object with at least a 'name'")
        name = b.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"bus #{i}: missing or empty 'name'")
        name = name.strip()
        if name in seen_names:
            raise ValueError(f"duplicate bus name: '{name}'")
        seen_names.add(name)

        channels, fmt_label = _channels_for(b.get("format"), b.get("channels"), f"bus '{name}'")
        buses.append(
            {
                "id": i,
                "type": BUS_TYPE_AUDIO,
                "channels": channels,
                "format": fmt_label,
                "name": name,
            }
        )
    return buses


def _route_tracks(raw_tracks: Any, buses_by_name: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate + resolve a routing spec's ``tracks`` list against the
    already-resolved ``buses_by_name`` map. Returns ``(routing, warnings)``.
    Raises ``ValueError`` on any structural problem (unknown bus, bad
    track channel spec); channel-count mismatches between a track and the
    bus it feeds are non-fatal and reported as ``warnings`` instead.
    """
    if raw_tracks is None:
        raw_tracks = []
    if not isinstance(raw_tracks, list):
        raise ValueError("'tracks' must be a list of {name, bus, format|channels} objects")

    routing: list[dict[str, Any]] = []
    warnings: list[str] = []
    for i, t in enumerate(raw_tracks):
        if not isinstance(t, dict):
            raise ValueError(f"track #{i}: must be an object with at least 'name' and 'bus'")
        name = t.get("name") or f"A{i + 1}"
        bus_name = t.get("bus")
        if not isinstance(bus_name, str) or not bus_name.strip():
            raise ValueError(f"track '{name}': missing or empty 'bus'")
        bus_name = bus_name.strip()
        bus = buses_by_name.get(bus_name)
        if bus is None:
            available = ", ".join(sorted(buses_by_name)) or "(no buses defined)"
            raise ValueError(f"track '{name}': routes to unknown bus '{bus_name}'. Available buses: {available}")

        fmt = t.get("channel_type", t.get("channelType", t.get("format")))
        channels = t.get("channels")
        track_channels, track_fmt_label = _channels_for(fmt, channels, f"track '{name}'") if (fmt is not None or channels is not None) else (1, "mono")

        entry = {
            "track": name,
            "channel_type": track_fmt_label,
            "channels": track_channels,
            "bus": bus_name,
            "bus_channels": bus["channels"],
            "bus_format": bus["format"],
        }
        routing.append(entry)
        if track_channels > bus["channels"]:
            msg = (
                f"track '{name}' ({track_fmt_label}, {track_channels}ch) exceeds the channel "
                f"count of bus '{bus_name}' ({bus['format']}, {bus['channels']}ch) — it will be "
                "downmixed"
            )
            warnings.append(msg)

    return routing, warnings


def _route(spec: dict[str, Any]) -> dict[str, Any]:
    buses = _resolve_buses(spec.get("buses"))
    buses_by_name = {b["name"]: b for b in buses}

    routing, warnings = _route_tracks(spec.get("tracks"), buses_by_name)

    main_fmt = spec.get("main_bus_format", spec.get("mainBusFormat"))
    if main_fmt is not None:
        main_channels, main_label = _channels_for(main_fmt, None, "main_bus_format")
    else:
        # Default the main bus to the first defined bus's format, matching
        # the reference's FirstMainBusFormat = the bus[0] channel count.
        main_channels, main_label = buses[0]["channels"], buses[0]["format"]

    unrouted = [b["name"] for b in buses if not any(r["bus"] == b["name"] for r in routing)]
    if unrouted:
        warnings.append("bus(es) with no tracks routed to them: " + ", ".join(unrouted))

    return {
        "action": "route",
        "main_bus": {"format": main_label, "channels": main_channels, "type": BUS_TYPE_AUDIO},
        "buses": buses,
        "bus_count": len(buses),
        "routing": routing,
        "track_count": len(routing),
        "warnings": warnings,
        "available_formats": sorted(set(FORMAT_CHANNELS)),
    }


@mcp.tool()
def fairlight_plan(action: str, routing_json: str = "") -> str:
    """Offline Fairlight bus-routing planner. Never touches Resolve.

    DaVinci Resolve's scripting API cannot create or route Fairlight
    buses; the reference implementation patches a project's SQLite
    database directly. This tool instead computes the *routing plan* a
    caller (or a separate DB-patching step) would need, purely from an
    in-memory spec.

    Parameters:
    - action: one of
        - "route": turn `routing_json` (a JSON object string) into a
          validated bus-routing plan. The spec shape is:
            {
              "buses": [{"name": str, "format": str} | {"name": str, "channels": int}, ...],
              "tracks": [{"name": str, "bus": str, "channel_type": str} | {..., "channels": int}, ...],
              "main_bus_format": str  # optional, defaults to buses[0]'s format
            }
          `format` is one of "mono", "stereo", "5.1", "5.1film", "7.1",
          "7.1.4" (case-insensitive); a bus/track may instead give an
          explicit `channels` integer. `buses` is required and must be a
          non-empty list of at most 32 uniquely-named buses (Fairlight's
          bus-definition table limit). `tracks` is optional; each track
          must route to a bus named in `buses`. The result includes the
          resolved `buses` (with `id`/`type`/`channels`/`format`), the
          per-track `routing` table, a `main_bus` summary, and
          `warnings` for non-fatal issues (a track's channel count
          exceeding its bus's channel count -> downmix warning; a bus
          with no tracks routed to it). A structurally invalid spec
          (missing/empty `buses`, unknown format, duplicate bus name,
          track routed to an unknown bus, too many buses, etc.) returns
          a JSON object with a clear `"error"` field instead of raising.
    - routing_json: JSON-encoded routing spec, see above.

    Any other action returns the list of valid actions instead of
    erroring. This is a read/plan-only tool: it never writes to a
    project, database, or file, so no `"verified"` field is returned.
    """
    action_lower = action.strip().lower() if isinstance(action, str) else ""

    try:
        if action_lower == "route":
            raw = (routing_json or "").strip()
            if not raw:
                return _err("route", "routing_json is required and must be a JSON object with a 'buses' list")
            try:
                spec = json.loads(raw)
            except json.JSONDecodeError as e:
                return _err("route", f"routing_json is not valid JSON: {e}")
            if not isinstance(spec, dict):
                return _err("route", "routing_json must decode to a JSON object")
            try:
                return json.dumps(_route(spec), indent=2)
            except ValueError as e:
                return _err("route", str(e))

        return json.dumps(
            {
                "error": f"Unknown action '{action}'.",
                "valid_actions": list(_VALID_ACTIONS),
            },
            indent=2,
        )
    except Exception as e:  # noqa: BLE001 - a tool entry point must never raise
        return f"Error: {e}"
