"""
davinci_resolve_mcp.tools.off_audio_plan — offline Fairlight track/stem
planning + coverage/loudness analysis (OFFLINE/advanced tool set).

Registers exactly one action-dispatch tool, ``audio_plan``, with a single
action:

  * ``"plan"`` — turn a small JSON "project spec" into a Fairlight
    track/stem plan: which audio tracks to create, what channel layout
    (mono/stereo/5.1/7.1) and bus each one belongs to, and the
    ``resolveCommands``-style instructions ("AddTrack" + "SetTrackName")
    an editing tool could later replay against a live Resolve timeline.
    The same spec may optionally carry timeline clips (for a gap/overlap/
    coverage-percent report) and/or loudness measurements + a target (for
    a pass/fail loudness check) — when present, those sections are folded
    into the same "plan" response so a caller gets one coherent picture of
    a project's audio state from one call.

``audio_plan.mjs`` / ``vendor/audio-fairlight`` (content-type templates for
documentary / narrative / podcast / social content, plus coverage and
EBU/ATSC-style loudness checks) — read here, not copied: this module is
original Python.

This module never connects to DaVinci Resolve and never touches the
filesystem: it only transforms an in-memory (JSON-decoded) project spec
into a plan. Every code path returns a JSON string; nothing here raises —
unexpected failures are caught in the tool entry point and reported as
``"Error: {e}"``. This is a read/plan-only domain (no `.drx`/grade bytes
are written), so no action here carries a ``"verified"`` field.
"""

from __future__ import annotations

import json
from typing import Any

from ..app import mcp

_VALID_ACTIONS = ("plan",)

# ── Channel types recognized by Resolve's Fairlight track model ─────────
CHANNEL_TYPES = {
    "mono": "mono",
    "stereo": "stereo",
    "surround_51": "5.1",
    "surround_71": "7.1",
    "adaptive": "adaptive1",
}

# ── Built-in content-type templates ──────────────────────────────────────
# Each template defines a set of tracks (name/label/channel_type/bus/role)
# plus the buses they route to and a recommended output format. These are
# starting points, not requirements — a caller's project spec may supply
# its own explicit `tracks` list instead of (or as well as) a
# `content_type`.
AUDIO_TEMPLATES: dict[str, dict[str, Any]] = {
    "DOCUMENTARY": {
        "name": "Documentary",
        "description": "Interview-driven documentary with natural sound and music",
        "tracks": [
            {"name": "DX L", "label": "Dialogue Left", "channel_type": "mono", "bus": "DX", "role": "dialogue"},
            {"name": "DX R", "label": "Dialogue Right", "channel_type": "mono", "bus": "DX", "role": "dialogue"},
            {"name": "NAT", "label": "Natural Sound", "channel_type": "stereo", "bus": "NAT", "role": "nat_sound"},
            {"name": "MX", "label": "Music", "channel_type": "stereo", "bus": "MX", "role": "music"},
            {"name": "VO", "label": "Voice Over", "channel_type": "mono", "bus": "DX", "role": "voiceover"},
            {"name": "SFX", "label": "Sound Effects", "channel_type": "stereo", "bus": "FX", "role": "sfx"},
        ],
        "buses": ["DX", "NAT", "MX", "FX", "MAIN"],
        "output_format": "stereo",
    },
    "NARRATIVE": {
        "name": "Narrative Feature",
        "description": "Scripted narrative with full surround sound design",
        "tracks": [
            {"name": "DX", "label": "Production Dialogue", "channel_type": "5.1", "bus": "DX", "role": "dialogue"},
            {"name": "ADR", "label": "ADR / Loop Group", "channel_type": "5.1", "bus": "DX", "role": "dialogue"},
            {"name": "FX", "label": "Sound Effects", "channel_type": "5.1", "bus": "FX", "role": "sfx"},
            {"name": "BG", "label": "Backgrounds / Ambience", "channel_type": "5.1", "bus": "FX", "role": "ambience"},
            {"name": "FLY", "label": "Foley", "channel_type": "5.1", "bus": "FX", "role": "foley"},
            {"name": "MX", "label": "Music Score", "channel_type": "5.1", "bus": "MX", "role": "music"},
            {"name": "MX2", "label": "Music Source / Diegetic", "channel_type": "stereo", "bus": "MX", "role": "music"},
            {"name": "FG", "label": "Full Mix Guide", "channel_type": "stereo", "bus": "REF", "role": "guide"},
        ],
        "buses": ["DX", "FX", "MX", "REF", "MAIN"],
        "output_format": "5.1",
    },
    "PODCAST": {
        "name": "Podcast",
        "description": "Multi-host podcast with music beds",
        "tracks": [
            {"name": "HOST", "label": "Host Microphone", "channel_type": "mono", "bus": "DX", "role": "dialogue"},
            {"name": "GUEST", "label": "Guest Microphone", "channel_type": "mono", "bus": "DX", "role": "dialogue"},
            {"name": "GUEST2", "label": "Guest 2 Microphone", "channel_type": "mono", "bus": "DX", "role": "dialogue"},
            {"name": "MX", "label": "Music / Beds", "channel_type": "stereo", "bus": "MX", "role": "music"},
            {"name": "SFX", "label": "Stingers / SFX", "channel_type": "stereo", "bus": "FX", "role": "sfx"},
            {"name": "PHONE", "label": "Phone / Remote", "channel_type": "mono", "bus": "DX", "role": "dialogue"},
        ],
        "buses": ["DX", "MX", "FX", "MAIN"],
        "output_format": "stereo",
    },
    "SOCIAL": {
        "name": "Social Media",
        "description": "Quick-turnaround social content",
        "tracks": [
            {"name": "MIX", "label": "Main Mix", "channel_type": "stereo", "bus": "MAIN", "role": "mix"},
            {"name": "VO", "label": "Voice Over", "channel_type": "mono", "bus": "DX", "role": "voiceover"},
            {"name": "MX", "label": "Music", "channel_type": "stereo", "bus": "MX", "role": "music"},
        ],
        "buses": ["DX", "MX", "MAIN"],
        "output_format": "stereo",
    },
}

# ── Loudness targets for common delivery specs ───────────────────────────
LOUDNESS_TARGETS: dict[str, dict[str, Any]] = {
    "broadcast_us": {"standard": "ATSC A/85", "target_lufs": -24, "tolerance": 2, "true_peak_dbtp": -2},
    "broadcast_eu": {"standard": "EBU R128", "target_lufs": -23, "tolerance": 1, "true_peak_dbtp": -1},
    "streaming": {"standard": "Streaming", "target_lufs": -16, "tolerance": 2, "true_peak_dbtp": -1},
    "cinema": {"standard": "SMPTE RP 200", "target_lufs": -20, "tolerance": 2, "true_peak_dbtp": -3},
    "podcast": {"standard": "Podcast", "target_lufs": -16, "tolerance": 2, "true_peak_dbtp": -1},
}


def _normalize_key(value: str) -> str:
    return value.strip().upper().replace(" ", "_").replace("-", "_")


def _select_template(content_type: str) -> dict[str, Any]:
    """Look up a built-in template by content type; raises ValueError if unknown."""
    key = _normalize_key(content_type)
    template = AUDIO_TEMPLATES.get(key)
    if template is None:
        available = ", ".join(sorted(k.lower() for k in AUDIO_TEMPLATES))
        raise ValueError(f"Unknown content type '{content_type}'. Available: {available}")
    return json.loads(json.dumps(template))  # deep copy


def _resolve_template(project: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve the effective template for a project spec, or None if the
    spec supplies no `tracks`, `template`, or `content_type` at all (an
    "empty project" — this is not an error, it just yields an empty plan).
    """
    explicit_tracks = project.get("tracks")
    if isinstance(explicit_tracks, list) and explicit_tracks:
        buses = project.get("buses")
        if not buses:
            seen: list[str] = []
            for t in explicit_tracks:
                bus = t.get("bus", "MAIN") if isinstance(t, dict) else "MAIN"
                if bus not in seen:
                    seen.append(bus)
            buses = seen
        return {
            "name": project.get("template_name") or project.get("templateName") or "Custom",
            "description": project.get("description", ""),
            "tracks": explicit_tracks,
            "buses": buses,
            "output_format": project.get("output_format") or project.get("outputFormat") or "stereo",
        }

    explicit_template = project.get("template")
    if isinstance(explicit_template, dict) and explicit_template.get("tracks"):
        return explicit_template

    content_type = project.get("content_type") or project.get("contentType")
    if content_type:
        return _select_template(content_type)

    return None


def _generate_track_plan(template: dict[str, Any], start_index: int = 1) -> dict[str, Any]:
    """Turn a resolved template into a concrete track/bus plan with
    Resolve-replayable AddTrack/SetTrackName commands.
    """
    raw_tracks = template.get("tracks") or []
    tracks: list[dict[str, Any]] = []
    for i, t in enumerate(raw_tracks):
        t = t if isinstance(t, dict) else {}
        channel_type = t.get("channel_type") or t.get("channelType") or "mono"
        resolve_subtype = channel_type if channel_type in ("5.1", "7.1", "stereo") else "mono"
        index = start_index + i
        tracks.append(
            {
                "index": index,
                "name": t.get("name", f"A{index}"),
                "label": t.get("label", t.get("name", f"A{index}")),
                "channel_type": channel_type,
                "bus": t.get("bus", "MAIN"),
                "role": t.get("role", ""),
                "resolve_subtype": resolve_subtype,
            }
        )

    resolve_commands = [
        {
            "action": "AddTrack",
            "args": ["audio", t["resolve_subtype"]],
            "then": {"action": "SetTrackName", "args": ["audio", t["index"], t["name"]]},
        }
        for t in tracks
    ]

    return {
        "template_name": template.get("name") or template.get("templateName") or "Custom",
        "output_format": template.get("output_format") or template.get("outputFormat") or "stereo",
        "track_count": len(tracks),
        "tracks": tracks,
        "buses": template.get("buses") or [],
        "resolve_commands": resolve_commands,
    }


def _empty_track_plan() -> dict[str, Any]:
    return {
        "template_name": None,
        "output_format": None,
        "track_count": 0,
        "tracks": [],
        "buses": [],
        "resolve_commands": [],
    }


def _clip_field(clip: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in clip:
            return clip[name]
    return default


def _merge_intervals(intervals: list[tuple[float, float]]) -> list[list[float]]:
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda iv: iv[0])
    merged: list[list[float]] = [list(ordered[0])]
    for start, end in ordered[1:]:
        last = merged[-1]
        if start <= last[1]:
            last[1] = max(last[1], end)
        else:
            merged.append([start, end])
    return merged


def _analyze_coverage(clips: list[Any], opts: dict[str, Any]) -> dict[str, Any]:
    """Detect gaps/overlaps/silence per track and report coverage percent.

    ``clips`` are dicts of the shape
    ``{trackIndex|track_index, trackName|track_name, startTime|start_time,
    endTime|end_time}``.
    """
    min_gap = opts.get("min_gap", opts.get("minGap", 0.5))
    timeline_start = opts.get("timeline_start", opts.get("timelineStart", 0))

    groups: dict[str, list[dict[str, Any]]] = {}
    for clip in clips:
        if not isinstance(clip, dict):
            continue
        track_index = _clip_field(clip, "track_index", "trackIndex")
        key = _clip_field(clip, "track_name", "trackName") or f"Track {track_index}"
        groups.setdefault(key, []).append(clip)

    timeline_end = opts.get("timeline_end", opts.get("timelineEnd"))
    if timeline_end is None:
        timeline_end = 0.0
        for clip in clips:
            if not isinstance(clip, dict):
                continue
            end = _clip_field(clip, "end_time", "endTime", default=0) or 0
            if end > timeline_end:
                timeline_end = end

    total_duration = timeline_end - timeline_start
    tracks: dict[str, Any] = {}
    total_gaps = 0
    total_overlaps = 0

    for track_name, track_clips in groups.items():
        sorted_clips = sorted(track_clips, key=lambda c: _clip_field(c, "start_time", "startTime", default=0) or 0)
        gaps: list[dict[str, Any]] = []
        overlaps: list[dict[str, Any]] = []

        def st(c: dict[str, Any]) -> float:
            return _clip_field(c, "start_time", "startTime", default=0) or 0

        def en(c: dict[str, Any]) -> float:
            return _clip_field(c, "end_time", "endTime", default=0) or 0

        if sorted_clips and st(sorted_clips[0]) - timeline_start > min_gap:
            gaps.append(
                {
                    "start_time": timeline_start,
                    "end_time": st(sorted_clips[0]),
                    "duration": st(sorted_clips[0]) - timeline_start,
                }
            )

        for i in range(1, len(sorted_clips)):
            prev, curr = sorted_clips[i - 1], sorted_clips[i]
            if st(curr) < en(prev):
                overlaps.append(
                    {
                        "clip_a": i - 1,
                        "clip_b": i,
                        "start_time": st(curr),
                        "end_time": min(en(prev), en(curr)),
                        "duration": min(en(prev), en(curr)) - st(curr),
                    }
                )
            elif st(curr) - en(prev) > min_gap:
                gaps.append({"start_time": en(prev), "end_time": st(curr), "duration": st(curr) - en(prev)})

        if sorted_clips:
            last = sorted_clips[-1]
            if timeline_end - en(last) > min_gap:
                gaps.append({"start_time": en(last), "end_time": timeline_end, "duration": timeline_end - en(last)})

        covered_time = 0.0
        merged = _merge_intervals([(st(c), en(c)) for c in sorted_clips])
        for s, e in merged:
            covered_time += min(e, timeline_end) - max(s, timeline_start)

        coverage_percent = round((covered_time / total_duration) * 10000) / 100 if total_duration > 0 else 0

        total_gaps += len(gaps)
        total_overlaps += len(overlaps)
        tracks[track_name] = {
            "clip_count": len(sorted_clips),
            "gaps": gaps,
            "overlaps": overlaps,
            "coverage_percent": coverage_percent,
        }

    return {
        "tracks": tracks,
        "summary": {
            "track_count": len(tracks),
            "total_gaps": total_gaps,
            "total_overlaps": total_overlaps,
            "timeline_duration": total_duration,
        },
    }


def _check_loudness(measurements: dict[str, Any], target_lufs: Any) -> dict[str, Any]:
    """Validate loudness measurements against a target LUFS or preset key."""
    if isinstance(target_lufs, str):
        preset = LOUDNESS_TARGETS.get(_normalize_key(target_lufs).lower())
        if preset is None:
            raise ValueError(f"Unknown loudness target '{target_lufs}'")
        target = preset
    else:
        target = {"target_lufs": float(target_lufs), "tolerance": 2, "true_peak_dbtp": -1}

    results: list[dict[str, Any]] = []
    all_pass = True

    integrated = _clip_field(measurements, "integrated_lufs", "integratedLUFS")
    if integrated is None:
        raise ValueError("measurements.integrated_lufs (or integratedLUFS) is required")

    int_diff = abs(integrated - target["target_lufs"])
    int_pass = int_diff <= target["tolerance"]
    if not int_pass:
        all_pass = False
    results.append(
        {
            "check": "integrated_loudness",
            "value": integrated,
            "target": target["target_lufs"],
            "tolerance": target["tolerance"],
            "pass": int_pass,
            "message": (
                f"Integrated loudness {integrated} LUFS within tolerance"
                if int_pass
                else f"Integrated loudness {integrated} LUFS outside tolerance "
                f"(target: {target['target_lufs']} +/- {target['tolerance']})"
            ),
        }
    )

    true_peak = _clip_field(measurements, "true_peak_dbtp", "truePeakdBTP")
    target_tp = target.get("true_peak_dbtp")
    if true_peak is not None and target_tp is not None:
        tp_pass = true_peak <= target_tp
        if not tp_pass:
            all_pass = False
        results.append(
            {
                "check": "true_peak",
                "value": true_peak,
                "target": target_tp,
                "pass": tp_pass,
                "message": (
                    f"True peak {true_peak} dBTP within limit"
                    if tp_pass
                    else f"True peak {true_peak} dBTP exceeds limit of {target_tp} dBTP"
                ),
            }
        )

    lra = _clip_field(measurements, "lra", "LRA")
    if lra is not None:
        lra_warn = lra > 20
        results.append(
            {
                "check": "loudness_range",
                "value": lra,
                "pass": not lra_warn,
                "message": (
                    f"Loudness range {lra} LU is unusually wide (>20 LU)"
                    if lra_warn
                    else f"Loudness range {lra} LU is within normal bounds"
                ),
            }
        )
        if lra_warn:
            all_pass = False

    return {"pass": all_pass, "standard": target.get("standard", "Custom"), "results": results}


def _plan(project: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"action": "plan"}

    try:
        template = _resolve_template(project)
    except ValueError as e:
        result["error"] = str(e)
        result["track_plan"] = _empty_track_plan()
        result["available_content_types"] = sorted(k.lower() for k in AUDIO_TEMPLATES)
        return result

    if template is not None:
        start_index = project.get("start_index", project.get("startIndex", 1))
        try:
            start_index = int(start_index)
        except (TypeError, ValueError):
            start_index = 1
        result["track_plan"] = _generate_track_plan(template, start_index)
    else:
        # Empty project spec (no tracks/template/content_type) -> empty
        # plan, not an error.
        result["track_plan"] = _empty_track_plan()

    clips = project.get("clips")
    if isinstance(clips, list) and clips:
        coverage_opts = project.get("coverage_opts") or project.get("coverageOpts") or {}
        if not isinstance(coverage_opts, dict):
            coverage_opts = {}
        result["coverage"] = _analyze_coverage(clips, coverage_opts)

    measurements = project.get("measurements")
    target_lufs = project.get("target_lufs", project.get("targetLUFS"))
    if isinstance(measurements, dict) and target_lufs is not None:
        try:
            result["loudness"] = _check_loudness(measurements, target_lufs)
        except ValueError as e:
            result["loudness"] = {"error": str(e)}

    result["available_content_types"] = sorted(k.lower() for k in AUDIO_TEMPLATES)
    return result


@mcp.tool()
def audio_plan(action: str, project_json: str = "") -> str:
    """Offline Fairlight track/stem planning + coverage/loudness analysis. Never touches Resolve.

    Parameters:
    - action: one of
        - "plan": turn `project_json` (a JSON object string; "" / "{}"
          counts as an empty project spec) into a track/stem plan. The
          spec may supply, in priority order:
            1. `tracks` — an explicit list of
               `{name, label, channel_type, bus, role}` track defs, used
               as-is;
            2. `template` — an explicit `{name, tracks, buses,
               output_format}` object;
            3. `content_type` — one of "documentary", "narrative",
               "podcast", "social" (case/space/dash-insensitive),
               resolved against a built-in track template.
          `start_index` (default 1) sets the first Resolve audio-track
          index. A spec with none of the above (e.g. "" or "{}") is a
          valid EMPTY project and returns an empty plan (`track_count: 0`),
          not an error; an unknown `content_type` returns an `"error"`
          field (not a raised exception) plus the list of
          `available_content_types`.

          The same spec may also carry, independently of the track plan:
            - `clips` — a list of timeline audio clips
              (`{track_name|track_index, start_time, end_time}`) to run a
              gap/overlap/coverage-percent report over (optional
              `coverage_opts: {min_gap, timeline_start, timeline_end}`);
              adds a `"coverage"` section to the result when present.
            - `measurements` (`{integrated_lufs, true_peak_dbtp?, lra?}`)
              plus `target_lufs` (a number, or a preset key: "broadcast_us"
              / "broadcast_eu" / "streaming" / "cinema" / "podcast") to run
              a pass/fail loudness check; adds a `"loudness"` section when
              both are present.
          The result always includes `available_content_types` for
          reference. This is a read/plan-only tool: it never writes a
          `.drx`/grade file, so no `"verified"` field is returned.
    - project_json: JSON-encoded project spec, see above.

    Any other action returns the list of valid actions instead of erroring.
    """
    action_lower = action.strip().lower() if isinstance(action, str) else ""

    try:
        if action_lower == "plan":
            raw = (project_json or "").strip()
            if not raw:
                project: Any = {}
            else:
                try:
                    project = json.loads(raw)
                except json.JSONDecodeError as e:
                    return json.dumps(
                        {"action": "plan", "error": f"project_json is not valid JSON: {e}"}, indent=2
                    )
            if not isinstance(project, dict):
                return json.dumps(
                    {"action": "plan", "error": "project_json must decode to a JSON object"}, indent=2
                )
            return json.dumps(_plan(project), indent=2)

        return json.dumps(
            {
                "error": f"Unknown action '{action}'.",
                "valid_actions": list(_VALID_ACTIONS),
            },
            indent=2,
        )
    except Exception as e:  # noqa: BLE001 - a tool entry point must never raise
        return f"Error: {e}"
