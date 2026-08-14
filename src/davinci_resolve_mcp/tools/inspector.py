"""Inspector Video-tab tools — panelled ``TimelineItem`` property wrappers.

This module owns the *Video* tab of the Edit/Cut-page Inspector: the
Transform, Cropping, Composite, Dynamic Zoom, and Retime & Scaling panels.
Each panel is exposed as an all-optional setter that batches *only the
fields the caller actually provided* into ``TimelineItem.SetProperty``,
plus a read-only snapshot tool and a connection-free reference tool.

It is a thin, panelled convenience layer over the generic
``get_timeline_item_properties`` / ``set_timeline_item_property`` mechanism
in ``tools/timeline_item.py`` (``TimelineItem.GetProperty`` /
``SetProperty``): same object, same locator, same property keys — but
grouped the way the Inspector groups them, with enum fields that accept a
human name (``"Multiply"``) *or* the raw integer (``4``) interchangeably.

The single source of truth for every enum is ``_ENUMS`` below
(DynamicZoomEase 0-3, CompositeMode 0-31, RetimeProcess 0-3,
MotionEstimation 0-6, Scaling 0-4, ResizeFilter 0-15) and for every
scalar property the ``_PROPERTY_SCHEMA`` table (key -> type + accepted
range). Both follow the "Looking up Timeline item properties" section of
the Resolve scripting API documentation.

``get_inspector_properties`` reuses ``resolve_utils.timeline_item_to_dict_full``.
``inspector_property_reference`` needs no Resolve connection at all — it
returns the schema and enum tables straight from the in-module constants.
Plugin/template listing (``list_installed_plugins`` etc.) is intentionally
NOT here — that surface is owned by ``tools/fx_plugins.py``.

All tools locate their target via ``helpers._get_timeline_item`` and reach
Resolve lazily inside their bodies; nothing here imports
``DaVinciResolveScript`` or touches a live Resolve instance at import time.
Every tool returns a ``str`` and never raises out of its body. Object:
``TimelineItem``.
"""

from __future__ import annotations

import json
from typing import Any, Optional, Union

from ..app import mcp
from ..helpers import _get_timeline_item
from ..resolve_utils import timeline_item_to_dict_full

# ── Enum source of truth ─────────────────────────────────────────────────
# Each enum maps a canonical human name -> the integer Resolve stores. The
# names are the ones the reference lists (minus the constant prefix), in the
# UI's usual casing. Setters accept either the name (case-insensitive) or the
# int; getters/reference report both directions.

_ENUMS: dict[str, dict[str, int]] = {
    "DynamicZoomEase": {
        "Linear": 0,
        "In": 1,
        "Out": 2,
        "InAndOut": 3,
    },
    "CompositeMode": {
        "Normal": 0,
        "Add": 1,
        "Subtract": 2,
        "Difference": 3,
        "Multiply": 4,
        "Screen": 5,
        "Overlay": 6,
        "HardLight": 7,
        "SoftLight": 8,
        "Darken": 9,
        "Lighten": 10,
        "ColorDodge": 11,
        "ColorBurn": 12,
        "Exclusion": 13,
        "Hue": 14,
        "Saturate": 15,
        "Colorize": 16,
        "LumaMask": 17,
        "Divide": 18,
        "LinearDodge": 19,
        "LinearBurn": 20,
        "LinearLight": 21,
        "VividLight": 22,
        "PinLight": 23,
        "HardMix": 24,
        "LighterColor": 25,
        "DarkerColor": 26,
        "Foreground": 27,
        "Alpha": 28,
        "InvertedAlpha": 29,
        "Lum": 30,
        "InvertedLum": 31,
    },
    "RetimeProcess": {
        "UseProject": 0,
        "Nearest": 1,
        "FrameBlend": 2,
        "OpticalFlow": 3,
    },
    "MotionEstimation": {
        "UseProject": 0,
        "StandardFaster": 1,
        "StandardBetter": 2,
        "EnhancedFaster": 3,
        "EnhancedBetter": 4,
        "SpeedWarpBetter": 5,
        "SpeedWarpFaster": 6,
    },
    "Scaling": {
        "UseProject": 0,
        "Crop": 1,
        "Fit": 2,
        "Fill": 3,
        "Stretch": 4,
    },
    "ResizeFilter": {
        "UseProject": 0,
        "Sharper": 1,
        "Smoother": 2,
        "Bicubic": 3,
        "Bilinear": 4,
        "Bessel": 5,
        "Box": 6,
        "CatmullRom": 7,
        "Cubic": 8,
        "Gaussian": 9,
        "Lanczos": 10,
        "Mitchell": 11,
        "NearestNeighbor": 12,
        "Quadratic": 13,
        "Sinc": 14,
        "Linear": 15,
    },
}

# ── Scalar property schema ───────────────────────────────────────────────
# key -> (kind, range-description). "kind" is one of "float", "bool", "enum".
# Ranges quote the reference; "width"/"height" are the timeline's UI max
# limits (values beyond the range are clipped by Resolve).

_PROPERTY_SCHEMA: dict[str, dict[str, str]] = {
    "Pan": {"type": "float", "range": "-4.0*width to 4.0*width"},
    "Tilt": {"type": "float", "range": "-4.0*height to 4.0*height"},
    "ZoomX": {"type": "float", "range": "0.0 to 100.0"},
    "ZoomY": {"type": "float", "range": "0.0 to 100.0"},
    "ZoomGang": {"type": "bool", "range": "true/false"},
    "RotationAngle": {"type": "float", "range": "-360.0 to 360.0"},
    "AnchorPointX": {"type": "float", "range": "-4.0*width to 4.0*width"},
    "AnchorPointY": {"type": "float", "range": "-4.0*height to 4.0*height"},
    "Pitch": {"type": "float", "range": "-1.5 to 1.5"},
    "Yaw": {"type": "float", "range": "-1.5 to 1.5"},
    "FlipX": {"type": "bool", "range": "true/false (flip horizontally)"},
    "FlipY": {"type": "bool", "range": "true/false (flip vertically)"},
    "CropLeft": {"type": "float", "range": "0.0 to width"},
    "CropRight": {"type": "float", "range": "0.0 to width"},
    "CropTop": {"type": "float", "range": "0.0 to height"},
    "CropBottom": {"type": "float", "range": "0.0 to height"},
    "CropSoftness": {"type": "float", "range": "-100.0 to 100.0"},
    "CropRetain": {"type": "bool", "range": "true/false (Retain Image Position)"},
    "DynamicZoomEase": {"type": "enum", "range": "DynamicZoomEase (0-3)"},
    "CompositeMode": {"type": "enum", "range": "CompositeMode (0-31)"},
    "Opacity": {"type": "float", "range": "0.0 to 100.0"},
    "Distortion": {"type": "float", "range": "-1.0 to 1.0"},
    "RetimeProcess": {"type": "enum", "range": "RetimeProcess (0-3)"},
    "MotionEstimation": {"type": "enum", "range": "MotionEstimation (0-6)"},
    "Scaling": {"type": "enum", "range": "Scaling (0-4)"},
    "ResizeFilter": {"type": "enum", "range": "ResizeFilter (0-15)"},
}

# Default transform values applied by reset_transform (neutral identity
# transform: no pan/tilt/rotation, 1.0x zoom, centred anchor, no flips).
_TRANSFORM_DEFAULTS: dict[str, Any] = {
    "Pan": 0.0,
    "Tilt": 0.0,
    "ZoomX": 1.0,
    "ZoomY": 1.0,
    "ZoomGang": True,
    "RotationAngle": 0.0,
    "AnchorPointX": 0.0,
    "AnchorPointY": 0.0,
    "Pitch": 0.0,
    "Yaw": 0.0,
    "FlipX": False,
    "FlipY": False,
}


class _EnumError(ValueError):
    """Raised internally when an enum name/int cannot be resolved."""


def _resolve_enum(enum_name: str, value: Union[str, int]) -> int:
    """Resolve an enum field ``value`` (name or int) to its integer code.

    Accepts a canonical name (case-insensitive, e.g. ``"Multiply"``), a
    numeric string (``"4"``), or an ``int`` (``4``). Raises ``_EnumError``
    with an actionable message — listing the valid names — when the value is
    neither a known name nor an in-range integer for ``enum_name``.
    """
    name_to_int = _ENUMS[enum_name]
    valid_names = ", ".join(name_to_int.keys())
    valid_ints = sorted(name_to_int.values())

    # Booleans are ints in Python — reject them explicitly so True/False
    # can't masquerade as enum codes 1/0.
    if isinstance(value, bool):
        raise _EnumError(
            f"Invalid {enum_name} {value!r}. Must be a name (one of: "
            f"{valid_names}) or an integer {valid_ints[0]}-{valid_ints[-1]}."
        )

    if isinstance(value, int):
        if value in valid_ints:
            return value
        raise _EnumError(
            f"Invalid {enum_name} {value}. Integer must be one of "
            f"{valid_ints}, or use a name: {valid_names}."
        )

    if isinstance(value, str):
        stripped = value.strip()
        for name, code in name_to_int.items():
            if stripped.lower() == name.lower():
                return code
        # Allow a numeric string that names a valid code.
        try:
            as_int = int(stripped)
        except ValueError:
            as_int = None
        if as_int is not None and as_int in valid_ints:
            return as_int
        raise _EnumError(
            f"Invalid {enum_name} '{value}'. Must be one of: {valid_names} "
            f"(or an integer {valid_ints[0]}-{valid_ints[-1]})."
        )

    raise _EnumError(
        f"Invalid {enum_name} {value!r}. Must be a name (one of: "
        f"{valid_names}) or an integer."
    )


def _apply_properties(
    track_type: str,
    track_index: int,
    item_index: int,
    fields: dict[str, Any],
    panel: str,
) -> str:
    """Batch-set ``fields`` on the located item via ``SetProperty``.

    Locates the timeline item, sets each provided key, and returns a summary
    string of what was set (and any keys Resolve rejected). Returns an
    ``Error:`` string rather than raising when the locator or a call fails.
    ``fields`` is expected to already hold only caller-provided, resolved
    values (``None`` entries are skipped defensively).
    """
    provided = {k: v for k, v in fields.items() if v is not None}
    if not provided:
        return (
            f"Error: No {panel} fields provided — pass at least one field to "
            "set. Nothing was changed."
        )
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"

    applied: list[str] = []
    failed: list[str] = []
    for key, value in provided.items():
        try:
            ok = item.SetProperty(key, value)
        except Exception as e:  # noqa: BLE001
            failed.append(f"{key} ({e})")
            continue
        if ok:
            applied.append(f"{key}={value!r}")
        else:
            failed.append(key)

    location = f"{track_type} track {track_index} item {item_index}"
    if applied and not failed:
        return f"Set {panel} on {location}: {', '.join(applied)}."
    if applied and failed:
        return (
            f"Partially set {panel} on {location}: applied "
            f"{', '.join(applied)}; Resolve rejected {', '.join(failed)}. "
            "Check those keys are valid for this item's type and in range."
        )
    return (
        f"Error: Failed to set {panel} on {location}: Resolve rejected "
        f"{', '.join(failed)}. Check the keys are valid for this item's type "
        "and the values are within the accepted range."
    )


# ── Transform panel ──────────────────────────────────────────────────────


@mcp.tool()
def set_transform(
    pan: Optional[float] = None,
    tilt: Optional[float] = None,
    zoom_x: Optional[float] = None,
    zoom_y: Optional[float] = None,
    zoom_gang: Optional[bool] = None,
    rotation_angle: Optional[float] = None,
    anchor_point_x: Optional[float] = None,
    anchor_point_y: Optional[float] = None,
    pitch: Optional[float] = None,
    yaw: Optional[float] = None,
    flip_x: Optional[bool] = None,
    flip_y: Optional[bool] = None,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Set Inspector > Transform panel fields on a timeline item (all optional).

    Only the fields you pass are changed; the rest are left untouched. Maps
    to TimelineItem.SetProperty for Pan/Tilt/ZoomX/ZoomY/ZoomGang/
    RotationAngle/AnchorPointX/AnchorPointY/Pitch/Yaw/FlipX/FlipY.

    Parameters:
    - pan / tilt: position offset (float; range ±4×width / ±4×height).
    - zoom_x / zoom_y: scale factor (float, 0.0-100.0; 1.0 = 100%).
    - zoom_gang: link X/Y zoom (bool).
    - rotation_angle: degrees (float, -360.0 to 360.0).
    - anchor_point_x / anchor_point_y: transform anchor (float).
    - pitch / yaw: 3D tilt (float, -1.5 to 1.5).
    - flip_x / flip_y: mirror horizontally / vertically (bool).
    - track_type: one of "video", "audio", "subtitle". Defaults to "video".
    - track_index: 1-based track index.
    - item_index: 0-based index within that track's item list.

    See inspector_property_reference for exact ranges.
    """
    fields = {
        "Pan": pan,
        "Tilt": tilt,
        "ZoomX": zoom_x,
        "ZoomY": zoom_y,
        "ZoomGang": zoom_gang,
        "RotationAngle": rotation_angle,
        "AnchorPointX": anchor_point_x,
        "AnchorPointY": anchor_point_y,
        "Pitch": pitch,
        "Yaw": yaw,
        "FlipX": flip_x,
        "FlipY": flip_y,
    }
    return _apply_properties(track_type, track_index, item_index, fields, "Transform")


# ── Cropping panel ───────────────────────────────────────────────────────


@mcp.tool()
def set_cropping(
    crop_left: Optional[float] = None,
    crop_right: Optional[float] = None,
    crop_top: Optional[float] = None,
    crop_bottom: Optional[float] = None,
    crop_softness: Optional[float] = None,
    crop_retain: Optional[bool] = None,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Set Inspector > Cropping panel fields on a timeline item (all optional).

    Only the fields you pass are changed. Maps to TimelineItem.SetProperty
    for CropLeft/CropRight/CropTop/CropBottom/CropSoftness/CropRetain.

    Parameters:
    - crop_left / crop_right: horizontal crop (float, 0.0 to width).
    - crop_top / crop_bottom: vertical crop (float, 0.0 to height).
    - crop_softness: edge softness (float, -100.0 to 100.0).
    - crop_retain: "Retain Image Position" checkbox (bool).
    - track_type: one of "video", "audio", "subtitle". Defaults to "video".
    - track_index: 1-based track index.
    - item_index: 0-based index within that track's item list.

    See inspector_property_reference for exact ranges.
    """
    fields = {
        "CropLeft": crop_left,
        "CropRight": crop_right,
        "CropTop": crop_top,
        "CropBottom": crop_bottom,
        "CropSoftness": crop_softness,
        "CropRetain": crop_retain,
    }
    return _apply_properties(track_type, track_index, item_index, fields, "Cropping")


# ── Composite panel ──────────────────────────────────────────────────────


@mcp.tool()
def set_composite(
    mode: Optional[Union[str, int]] = None,
    opacity: Optional[float] = None,
    distortion: Optional[float] = None,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Set Inspector > Composite panel fields on a timeline item (all optional).

    Only the fields you pass are changed. Maps to TimelineItem.SetProperty
    for CompositeMode/Opacity/Distortion.

    Parameters:
    - mode: blend mode as a name ("Normal", "Multiply", "Screen", ...) or
      the integer code (0-31). e.g. "Multiply" and 4 both set CompositeMode=4.
      See inspector_property_reference for the full name<->int table.
    - opacity: layer opacity (float, 0.0 to 100.0).
    - distortion: lens distortion (float, -1.0 to 1.0).
    - track_type: one of "video", "audio", "subtitle". Defaults to "video".
    - track_index: 1-based track index.
    - item_index: 0-based index within that track's item list.
    """
    fields: dict[str, Any] = {}
    if mode is not None:
        try:
            fields["CompositeMode"] = _resolve_enum("CompositeMode", mode)
        except _EnumError as e:
            return f"Error: {e}"
    if opacity is not None:
        fields["Opacity"] = opacity
    if distortion is not None:
        fields["Distortion"] = distortion
    return _apply_properties(track_type, track_index, item_index, fields, "Composite")


# ── Dynamic Zoom panel ───────────────────────────────────────────────────


@mcp.tool()
def set_dynamic_zoom(
    ease: Union[str, int],
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Set Inspector > Dynamic Zoom easing on a timeline item.

    Maps to TimelineItem.SetProperty for DynamicZoomEase.

    Parameters:
    - ease: easing as a name ("Linear", "In", "Out", "InAndOut") or the
      integer code (0-3).
    - track_type: one of "video", "audio", "subtitle". Defaults to "video".
    - track_index: 1-based track index.
    - item_index: 0-based index within that track's item list.
    """
    try:
        code = _resolve_enum("DynamicZoomEase", ease)
    except _EnumError as e:
        return f"Error: {e}"
    return _apply_properties(
        track_type,
        track_index,
        item_index,
        {"DynamicZoomEase": code},
        "Dynamic Zoom",
    )


# ── Retime & Scaling panel ───────────────────────────────────────────────


@mcp.tool()
def set_retime_and_scaling(
    retime_process: Optional[Union[str, int]] = None,
    motion_estimation: Optional[Union[str, int]] = None,
    scaling: Optional[Union[str, int]] = None,
    resize_filter: Optional[Union[str, int]] = None,
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Set Inspector > Retime and Scaling fields on a timeline item (all optional).

    Only the fields you pass are changed. Maps to TimelineItem.SetProperty
    for RetimeProcess/MotionEstimation/Scaling/ResizeFilter. Each enum field
    accepts a name or an integer code.

    Parameters:
    - retime_process: "UseProject"/"Nearest"/"FrameBlend"/"OpticalFlow" or 0-3.
    - motion_estimation: "UseProject"/"StandardFaster"/"StandardBetter"/
      "EnhancedFaster"/"EnhancedBetter"/"SpeedWarpBetter"/"SpeedWarpFaster" or 0-6.
    - scaling: "UseProject"/"Crop"/"Fit"/"Fill"/"Stretch" or 0-4.
    - resize_filter: "UseProject"/"Sharper"/"Smoother"/"Bicubic"/... or 0-15.
    - track_type: one of "video", "audio", "subtitle". Defaults to "video".
    - track_index: 1-based track index.
    - item_index: 0-based index within that track's item list.

    See inspector_property_reference for the full name<->int tables.
    """
    fields: dict[str, Any] = {}
    for value, enum_name in (
        (retime_process, "RetimeProcess"),
        (motion_estimation, "MotionEstimation"),
        (scaling, "Scaling"),
        (resize_filter, "ResizeFilter"),
    ):
        if value is None:
            continue
        try:
            fields[enum_name] = _resolve_enum(enum_name, value)
        except _EnumError as e:
            return f"Error: {e}"
    return _apply_properties(
        track_type, track_index, item_index, fields, "Retime and Scaling"
    )


# ── Reset ────────────────────────────────────────────────────────────────


@mcp.tool()
def reset_transform(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Reset a timeline item's Transform panel to the neutral identity transform.

    Sets Pan/Tilt/RotationAngle/AnchorPoint*/Pitch/Yaw to 0, ZoomX/ZoomY to
    1.0 (100%), ZoomGang on, and clears FlipX/FlipY. Cropping, Composite,
    Dynamic Zoom, and Retime settings are left untouched.

    Parameters:
    - track_type: one of "video", "audio", "subtitle". Defaults to "video".
    - track_index: 1-based track index.
    - item_index: 0-based index within that track's item list.
    """
    return _apply_properties(
        track_type,
        track_index,
        item_index,
        dict(_TRANSFORM_DEFAULTS),
        "Transform reset",
    )


# ── Read snapshot ────────────────────────────────────────────────────────


@mcp.tool()
def get_inspector_properties(
    track_type: str = "video",
    track_index: int = 1,
    item_index: int = 0,
) -> str:
    """Get the Video-tab Inspector property snapshot for a timeline item, as JSON.

    Returns the item's serialized properties (Transform, Cropping, Composite,
    Dynamic Zoom, Retime & Scaling keys) alongside its markers/flags/Fusion
    info, via resolve_utils.timeline_item_to_dict_full. Enum keys report the
    raw integer Resolve stores — cross-reference inspector_property_reference
    for the matching name.

    Parameters:
    - track_type: one of "video", "audio", "subtitle". Defaults to "video".
    - track_index: 1-based track index.
    - item_index: 0-based index within that track's item list.
    """
    try:
        item = _get_timeline_item(track_type, track_index, item_index)
        return json.dumps(timeline_item_to_dict_full(item), indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Reference (no connection) ────────────────────────────────────────────


@mcp.tool()
def inspector_property_reference() -> str:
    """List every Video-tab Inspector property with its range and enum tables, as JSON.

    Needs NO Resolve connection — returns the in-module schema directly. For
    each property key: its value type and accepted range. For each enum
    (DynamicZoomEase, CompositeMode, RetimeProcess, MotionEstimation,
    Scaling, ResizeFilter): the full name->int and int->name tables so a
    caller can map a stored integer back to its name (and vice versa)
    offline.
    """
    reference = {
        "properties": _PROPERTY_SCHEMA,
        "enums": {
            enum_name: {
                "name_to_int": name_to_int,
                "int_to_name": {
                    str(code): name for name, code in name_to_int.items()
                },
            }
            for enum_name, name_to_int in _ENUMS.items()
        },
        "notes": (
            "Enum fields accept a name (case-insensitive) or its integer "
            "code interchangeably. Values beyond a numeric range are clipped "
            "by Resolve; 'width'/'height' are the timeline's UI max limits."
        ),
    }
    return json.dumps(reference, indent=2, default=str)
