"""AI / Neural Engine tools for the DaVinci Resolve MCP server.

Covers Magic Mask (create/regenerate), Smart Reframe, Stabilize — all
per-clip Neural Engine features reached through a located ``TimelineItem``
— plus timeline-level AI subtitle generation/clearing
(``CreateSubtitlesFromAudio`` / ``ClearSubtitles``).

Per the ownership contract, this module owns ``create_magic_mask``,
``regenerate_magic_mask``, ``smart_reframe``, ``stabilize``,
``create_subtitles_from_audio``, and ``clear_subtitles``. Scene-cut
detection (``detect_scene_cuts``) lives in ``timeline_edit.py``, not here.

All of these APIs are gated behind DaVinci Resolve Studio (the free
edition does not ship the Neural Engine) and, in some cases, a minimum
Resolve version. Every tool checks ``hasattr(obj, 'Method')`` before
calling it and returns an explanatory string — never raises — when the
method is unavailable, so the server behaves sanely against the free
edition or older Resolve installs.

Nothing in this module imports ``DaVinciResolveScript`` or touches a live
Resolve instance at import time — the connection is only reached lazily,
inside each tool body, via ``_conn()``/``_get_timeline_item()``.
"""

from __future__ import annotations

from ..app import mcp
from ..helpers import _conn, _get_timeline_item, _ok, _require_timeline

_STUDIO_MSG = "{method} is not available. Requires Resolve Studio 19+."


@mcp.tool()
def create_magic_mask(
    mode: str = "F",
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """
    Create an AI-powered Magic Mask on a timeline item for subject isolation.
    Requires DaVinci Resolve Studio with Neural Engine.

    Parameters:
    - mode: "F" (forward), "B" (backward), or "BI" (bidirectional)
    - track_type/track_index/item_index: Clip locator
    """
    valid_modes = ("F", "B", "BI")
    if mode not in valid_modes:
        return f"Invalid mode '{mode}'. Must be one of: {', '.join(valid_modes)}"
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        if not hasattr(item, "CreateMagicMask"):
            return _STUDIO_MSG.format(method="CreateMagicMask")
        success = item.CreateMagicMask(mode)
        return _ok(
            success,
            f"Magic Mask created (mode: {mode})",
            "Failed to create Magic Mask",
        )
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def regenerate_magic_mask(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """
    Regenerate an existing Magic Mask on a timeline item.
    Requires DaVinci Resolve Studio with Neural Engine.

    Parameters:
    - track_type/track_index/item_index: Clip locator
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        if not hasattr(item, "RegenerateMagicMask"):
            return _STUDIO_MSG.format(method="RegenerateMagicMask")
        success = item.RegenerateMagicMask()
        return _ok(success, "Magic Mask regenerated", "Failed to regenerate Magic Mask")
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def smart_reframe(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """
    Apply Smart Reframe to a timeline item (AI-based reframing).
    Requires DaVinci Resolve Studio with Neural Engine.

    Parameters:
    - track_type/track_index/item_index: Clip locator
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        if not hasattr(item, "SmartReframe"):
            return _STUDIO_MSG.format(method="SmartReframe")
        success = item.SmartReframe()
        return _ok(success, "Smart Reframe applied", "Failed to apply Smart Reframe")
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def stabilize(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """
    Apply stabilization to a timeline item using the DaVinci Neural Engine.
    Requires DaVinci Resolve Studio.

    Parameters:
    - track_type/track_index/item_index: Clip locator
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        if not hasattr(item, "Stabilize"):
            return _STUDIO_MSG.format(method="Stabilize")
        success = item.Stabilize()
        return _ok(success, "Stabilization applied", "Failed to stabilize")
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def create_subtitles_from_audio(
    language: str = "auto",
    preset: str = "default",
    chars_per_line: int = 42,
    line_break: str = "single",
    gap: int = 0,
) -> str:
    """
    Generate subtitles from audio on the current timeline using AI speech
    recognition. Requires DaVinci Resolve Studio 19+.

    Parameters:
    - language: "auto", "english", "french", "german", "italian", "japanese",
                "korean", "mandarin_simplified", "mandarin_traditional",
                "portuguese", "russian", "spanish", "danish", "dutch",
                "norwegian", "swedish"
    - preset: "default", "teletext", or "netflix"
    - chars_per_line: Characters per line (1-60, default: 42)
    - line_break: "single" or "double"
    - gap: Gap between subtitles in frames (0-10, default: 0)
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)
        resolve = conn.get_resolve()

        if not hasattr(timeline, "CreateSubtitlesFromAudio"):
            return _STUDIO_MSG.format(method="CreateSubtitlesFromAudio")

        language_map = {
            "auto": "AUTO_CAPTION_AUTO",
            "english": "AUTO_CAPTION_ENGLISH",
            "french": "AUTO_CAPTION_FRENCH",
            "german": "AUTO_CAPTION_GERMAN",
            "italian": "AUTO_CAPTION_ITALIAN",
            "japanese": "AUTO_CAPTION_JAPANESE",
            "korean": "AUTO_CAPTION_KOREAN",
            "mandarin_simplified": "AUTO_CAPTION_MANDARIN_SIMPLIFIED",
            "mandarin_traditional": "AUTO_CAPTION_MANDARIN_TRADITIONAL",
            "portuguese": "AUTO_CAPTION_PORTUGUESE",
            "russian": "AUTO_CAPTION_RUSSIAN",
            "spanish": "AUTO_CAPTION_SPANISH",
            "danish": "AUTO_CAPTION_DANISH",
            "dutch": "AUTO_CAPTION_DUTCH",
            "norwegian": "AUTO_CAPTION_NORWEGIAN",
            "swedish": "AUTO_CAPTION_SWEDISH",
        }
        if language not in language_map:
            return (
                f"Invalid language '{language}'. Must be one of: "
                f"{', '.join(language_map)}"
            )

        preset_map = {
            "default": "AUTO_CAPTION_SUBTITLE_DEFAULT",
            "teletext": "AUTO_CAPTION_TELETEXT",
            "netflix": "AUTO_CAPTION_NETFLIX",
        }
        if preset not in preset_map:
            return f"Invalid preset '{preset}'. Must be one of: {', '.join(preset_map)}"

        line_break_map = {
            "single": "AUTO_CAPTION_LINE_SINGLE",
            "double": "AUTO_CAPTION_LINE_DOUBLE",
        }
        if line_break not in line_break_map:
            return (
                f"Invalid line_break '{line_break}'. Must be one of: "
                f"{', '.join(line_break_map)}"
            )

        def _resolve_const(name: str):
            return getattr(resolve, name, None)

        lang_const = _resolve_const(language_map[language])
        preset_const = _resolve_const(preset_map[preset])
        lb_const = _resolve_const(line_break_map[line_break])

        subtitle_lang_key = _resolve_const("SUBTITLE_LANGUAGE")
        subtitle_preset_key = _resolve_const("SUBTITLE_CAPTION_PRESET")
        subtitle_cpl_key = _resolve_const("SUBTITLE_CHARS_PER_LINE")
        subtitle_lb_key = _resolve_const("SUBTITLE_LINE_BREAK")
        subtitle_gap_key = _resolve_const("SUBTITLE_GAP")

        if subtitle_lang_key is None:
            return (
                "Subtitle constants not available on this Resolve version. "
                "Requires Resolve Studio 19+."
            )

        settings = {
            subtitle_lang_key: lang_const,
            subtitle_preset_key: preset_const,
            subtitle_cpl_key: chars_per_line,
            subtitle_lb_key: lb_const,
            subtitle_gap_key: gap,
        }

        result = timeline.CreateSubtitlesFromAudio(settings)
        return _ok(
            result,
            "Subtitles generated from audio successfully",
            "Failed to generate subtitles",
        )
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def clear_subtitles() -> str:
    """
    Remove all AI-generated subtitles from the current timeline.
    Requires DaVinci Resolve Studio 19+.
    """
    try:
        conn = _conn()
        timeline = _require_timeline(conn)
        if not hasattr(timeline, "ClearSubtitles"):
            return _STUDIO_MSG.format(method="ClearSubtitles")
        success = timeline.ClearSubtitles()
        return _ok(success, "Subtitles cleared", "Failed to clear subtitles")
    except Exception as e:
        return f"Error: {e}"
