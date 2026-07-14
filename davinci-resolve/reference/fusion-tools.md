# DaVinci Resolve MCP — Fusion node engine + Inspector cheat-sheet

The cheat-sheet for **which inputs to set to actually build something** with our live
Fusion node engine, the OFX splice, keyframing, and the Edit-page Inspector panels.

Load this when you're wiring a Fusion comp on a timeline item (`fusion_add_tool` /
`fusion_connect_input` / `fusion_set_input` / `fusion_get_input` / `fusion_list_inputs`),
titling (`set_title_text`), applying a ResolveFX plugin (`apply_ofx_to_clip`), animating a
Fusion input (`fusion_add_keyframe`), or setting Transform/Crop/Composite/Retime on a clip
(`set_transform` etc.).

Every tool takes a **timeline-item locator** — `track_type` ("video" default / "audio" /
"subtitle"), `track_index` (1-based, default 1), `item_index` (0-based, default 0) — and a
`comp_index` (1-based Fusion comp on the item, default 1). Every tool returns a **plain
string** (JSON on success, `Error: …` on failure); read it and react. See
[`operating-notes.md`](./operating-notes.md) for the behaviours that look like bugs but aren't.

## Table of Contents
- [Fusion comp lifecycle](#fusion-comp-lifecycle)
- [The two RegID conventions (read this first)](#the-two-regid-conventions-read-this-first)
- [Fusion tool-type catalog (for `fusion_add_tool`)](#fusion-tool-type-catalog-for-fusion_add_tool)
- [TextPlus inputs](#textplus-inputs)
- [Background inputs](#background-inputs)
- [Merge inputs](#merge-inputs)
- [Transform inputs](#transform-inputs)
- [Connection / wiring patterns](#connection--wiring-patterns)
- [Keyframing a Fusion input (`fusion_add_keyframe`)](#keyframing-a-fusion-input-fusion_add_keyframe)
- [Applying an OFX / ResolveFX plugin (`apply_ofx_to_clip`)](#applying-an-ofx--resolvefx-plugin-apply_ofx_to_clip)
- [Discovering RegIDs and inputs](#discovering-regids-and-inputs)
- [Edit-page Inspector property reference](#edit-page-inspector-property-reference)

---

## Fusion comp lifecycle

A timeline item can carry more than one Fusion comp; `comp_index` (1-based) picks which. The
node-engine tools auto-create comp 1 if none exists.

| Task | Tool |
|------|------|
| List comps on a clip | `get_fusion_comp_list` |
| Add / delete a comp | `add_fusion_comp` / `delete_fusion_comp` |
| Import / export / load by name | `import_fusion_comp` / `export_fusion_comp` / `load_fusion_comp` |
| Rename a comp | `rename_fusion_comp` |
| New Fusion clip from timeline item(s) | `create_fusion_clip` |
| Apply a saved `.setting` macro to a clip | `apply_macro_to_clip` |
| Attach a saved `.comp`/`.setting` onto an item | `attach_fusion_comp` |
| Arbitrary Fusion Lua (escape hatch) | `execute_fusion_lua` |

---

## The two RegID conventions (read this first)

There are **two different tools that add a node, and they take the `ofx.` prefix
oppositely.** This is the single most common footgun — get it right:

| Tool | Argument | Native tool | ResolveFX / OFX tool |
|------|----------|-------------|----------------------|
| `fusion_add_tool` | `reg_id` | bare name, e.g. `"Merge"`, `"TextPlus"`, `"Transform"` | **keep** the full prefix: `"ofx.com.blackmagicdesign.resolvefx.gaussianblur"` (passed to `AddTool` verbatim) |
| `apply_ofx_to_clip` | `regid` | (not for native tools) | **omit** the prefix: `"com.blackmagicdesign.resolvefx.gaussianblur"` (the tool prepends `ofx.` and splices it into the render chain) |

So: `fusion_add_tool` just drops a node on the flow (you wire it yourself); it needs the
`ofx.` prefix for OFX. `apply_ofx_to_clip` drops **and splices** an OFX node in front of
`MediaOut1` for you; it adds the `ofx.` prefix itself — see
[operating-notes #5](./operating-notes.md#5-apply_ofx_to_clip-keeps-the-ofx-prefix-and-splices-before-mediaout1).

Native RegIDs are the internal ids, **not** the Effects-panel display names: the text tool
is `TextPlus` (not "Text+"), the merge tool is `Merge`. `set_title_text` finds the text
node by matching `TOOLS_RegID == "TextPlus"`.

---

## Fusion tool-type catalog (for `fusion_add_tool`)

Pass the `reg_id` (native = bare name) to
`fusion_add_tool(reg_id, tool_name=…, comp_index=1, pos_x=-32768, pos_y=-32768, …)`.
`tool_name` sets `TOOLS_Name` (the handle you pass to every other tool); leave `pos_x`/`pos_y`
at `-32768` to auto-place. In a Resolve timeline-item comp, `MediaIn1` and `MediaOut1`
already exist — you build **between** them.

**Generators / sources (make picture):**
- `Background` — solid color / gradient fill
- `TextPlus` — styled text with full typography (RegID is `TextPlus`)
- `FastNoise` — procedural noise
- `Plasma` — procedural plasma
- `Rectangle`, `Ellipse` — shape sources
- `MediaIn` — the clip's picture (auto-present as `MediaIn1`)
- `Loader` — read media from disk (rare inside a Resolve comp)

**Compositing:**
- `Merge` — composite Foreground over Background (the core compositing node)
- `ChannelBooleans` — per-channel math / channel remap
- `MatteControl` — combine / refine mattes
- `Dissolve` — cross-dissolve two inputs

**Filters / color:**
- `Blur` — Gaussian blur
- `Glow` — glow
- `Sharpen` — sharpen
- `BrightnessContrast` — brightness / contrast / gain / gamma
- `ColorCorrector` — full color corrector
- `ColorCurves` — curve-based color
- `HueCurves` — hue-qualified color

**Transform / geometry:**
- `Transform` — 2D pan / rotate / scale
- `Resize` — change resolution
- `Crop` — crop to a region
- `DVE` — 3D perspective transform
- `CornerPositioner` — corner-pin

**Masks (wire into an `EffectMask` input):**
- `RectangleMask` — rectangular mask
- `EllipseMask` — elliptical mask
- `PolylineMask` — freeform bezier mask
- `BitmapMask` — image-driven mask

**Output:**
- `MediaOut` — to timeline (auto-present as `MediaOut1`; must remain the tail of the chain
  for the comp to render)
- `Saver` — write to disk

> ResolveFX effects (Gaussian Blur, Lens Flare, Face Refinement, …) are OFX nodes, not the
> native filters above. Add them with the `ofx.<wireId>` RegID via `fusion_add_tool`, or —
> to splice one into the render chain automatically — with `apply_ofx_to_clip`. See
> [Discovering RegIDs](#discovering-regids-and-inputs).

---

## TextPlus inputs

Set with `fusion_set_input(tool_name, input_name, value)` (`value` is a **string** —
numeric/boolean strings are auto-coerced; `StyledText` and other `Text…`-typed inputs are
passed verbatim so `"007"` stays `"007"`). For text, `set_title_text` is the shortcut — it
locates the `TextPlus` node for you and sets `StyledText` (+ optional `Size`/`Center`).

| Input | Type | Notes |
|-------|------|-------|
| `StyledText` | string | The text content (verbatim, never coerced) |
| `Font` | string | Font family name |
| `Style` | string | "Regular" / "Bold" / "Italic" / "Bold Italic" |
| `Size` | float | Relative font size, ~0.0–1.0 of frame height (e.g. `0.08`) |
| `Center` | point | Normalized `{x, y}`, `0.5/0.5` = center — **see the point-input note below** |
| `Red1` / `Green1` / `Blue1` | float | Text fill color channels, 0.0–1.0 |
| `Alpha1` | float | Text fill opacity, 0.0–1.0 |
| `Tracking` | float | Letter spacing |
| `LineSpacing` | float | Line-spacing multiplier |
| `HorizontalJustification` | int | 0=Left, 1=Center, 2=Right |
| `VerticalJustification` | int | 0=Top, 1=Center, 2=Bottom |
| `Enabled1` | bool | Enable shading element 1 (fill) |
| `Enabled2` / `Type2` / `Thickness2` | bool/int/float | Element 2 (typically border) |
| `Red2` / `Green2` / `Blue2` | float | Border color |
| `Enabled4` / `Offset4` / `Softness4` | bool/point/float | Element 4 (typically drop shadow) |

**Point-input note:** `Center` and `Offset*` are 2-component point inputs (Fusion wants
`{1: x, 2: y}`). `fusion_set_input` takes a **single string** value, so it can't populate
both components cleanly. For Text+ position use `set_title_text(text, center_x=…,
center_y=…)` (it sets `Center` as `{1: x, 2: y}` internally). For arbitrary point inputs on
other tools, keyframe each component via `fusion_add_keyframe`, or drop to
`execute_fusion_lua`.

---

## Background inputs

`Background` (RegID `Background`) fills the frame with a solid color or gradient.

| Input | Type | Notes |
|-------|------|-------|
| `TopLeftRed` | float | Red, 0.0–1.0 (solid color = TopLeft*) |
| `TopLeftGreen` | float | Green, 0.0–1.0 |
| `TopLeftBlue` | float | Blue, 0.0–1.0 |
| `TopLeftAlpha` | float | Alpha, 0.0–1.0 |
| `Type` | int/string | 0=Solid, 1=Horizontal gradient, 2=Vertical, 3=Corners, 4=Radial |
| `Width` | int | Pixel width (defaults to comp resolution) |
| `Height` | int | Pixel height |
| `Depth` | int | Bit depth: 1=8-bit int, 2=16-bit int, 3=16-bit float, 4=32-bit float |

Example — an opaque dark-blue fill:
```
fusion_add_tool(reg_id="Background", tool_name="BG1")
fusion_set_input("BG1", "TopLeftRed",   "0.05")
fusion_set_input("BG1", "TopLeftGreen", "0.08")
fusion_set_input("BG1", "TopLeftBlue",  "0.18")
fusion_set_input("BG1", "TopLeftAlpha", "1.0")
```

---

## Merge inputs

`Merge` (RegID `Merge`) composites a **Foreground over a Background**. Wire the two image
inputs with `fusion_connect_input`; set the scalar/blend inputs with `fusion_set_input`.

| Input | Type | Set how | Notes |
|-------|------|---------|-------|
| `Background` | Image | `fusion_connect_input` | The under layer |
| `Foreground` | Image | `fusion_connect_input` | The over layer |
| `EffectMask` | Image | `fusion_connect_input` | Optional mask (limits the merge) |
| `Blend` | float | `fusion_set_input` | Foreground opacity, 0.0–1.0 |
| `ApplyMode` | string | `fusion_set_input` | Blend mode: "Normal", "Screen", "Multiply", "Overlay", "SoftLight", … |
| `Center` | point | (point input) | Foreground position `{x, y}` |
| `Size` | float | `fusion_set_input` | Foreground scale, 1.0 = 100% |
| `Angle` | float | `fusion_set_input` | Foreground rotation, degrees |

---

## Transform inputs

`Transform` (RegID `Transform`) pans/rotates/scales its single `Input` image.

| Input | Type | Set how | Notes |
|-------|------|---------|-------|
| `Input` | Image | `fusion_connect_input` | Image to transform |
| `Center` | point | (point input) | Position `{x, y}`, 0.5/0.5 = center |
| `Size` | float | `fusion_set_input` | Uniform scale, 1.0 = 100% |
| `Angle` | float | `fusion_set_input` | Rotation, degrees |
| `Pivot` | point | (point input) | Rotation/scale pivot `{x, y}` |
| `XSize` / `YSize` | float | `fusion_set_input` | Independent axis scale (when not ganged) |
| `Aspect` | float | `fusion_set_input` | Aspect-ratio adjust |
| `FlipHoriz` / `FlipVert` | bool | `fusion_set_input` | Mirror |

---

## Connection / wiring patterns

Wire with `fusion_connect_input(tool_name, input_name, source_tool, source_output="Output")`.
`tool_name` is the **receiving** node; leave `source_tool` **empty** to **disconnect** an
input. The common input IDs are `Background`, `Foreground`, `Input`, `EffectMask`, `Source`.

**Basic composite — text over a background:**
```
Background --> Merge1.Background
TextPlus1  --> Merge1.Foreground
```
```
fusion_connect_input("Merge1", "Background", "Background1")
fusion_connect_input("Merge1", "Foreground", "TextPlus1")
```

**A chain of filters into the output:**
```
MediaIn1 --> Blur1.Input
Blur1    --> BrightnessContrast1.Input
BC1      --> MediaOut1.Input
```

**Text with a mask (mask limits the text layer):**
```
Background1  --> Merge1.Background
TextPlus1    --> Merge1.Foreground
EllipseMask1 --> Merge1.EffectMask
```

**Corrected lower-third (two text layers over the picture):**

Build the graphic (name + title over a bar), then composite the whole graphic **over the
clip's `MediaIn1`** and feed `MediaOut1`. The graphic is the Foreground, the picture is the
Background:
```
Background1 --> Merge1.Background          (the bar / plate)
TextPlus1   --> Merge1.Foreground          (name line)
Merge1      --> Merge2.Background
TextPlus2   --> Merge2.Foreground          (title line)   ← Merge2 = the finished graphic
MediaIn1    --> MergeFinal.Background       (the clip)
Merge2      --> MergeFinal.Foreground       (graphic over clip)
MergeFinal  --> MediaOut1.Input
```
```
fusion_connect_input("Merge1", "Background", "Background1")
fusion_connect_input("Merge1", "Foreground", "TextPlus1")
fusion_connect_input("Merge2", "Background", "Merge1")
fusion_connect_input("Merge2", "Foreground", "TextPlus2")
fusion_connect_input("MergeFinal", "Background", "MediaIn1")
fusion_connect_input("MergeFinal", "Foreground", "Merge2")
fusion_connect_input("MediaOut1", "Input", "MergeFinal")
```

> The failure mode to avoid: leaving `MediaIn1` on a Foreground input, or forgetting to
> reconnect `MediaOut1.Input` to the tail of your chain — then the comp renders the raw clip
> (or nothing) instead of your graphic.

---

## Keyframing a Fusion input (`fusion_add_keyframe`)

`fusion_add_keyframe(tool_name, input_name, frame, value, comp_index=1, replace=False, …)`

A plain `fusion_set_input` on a **virgin (un-animated) input only changes its constant
value — it does NOT create animation.** So the first time an input is keyed,
`fusion_add_keyframe` attaches a **`BezierSpline` modifier**
(`tool.AddModifier(input, "BezierSpline")`) and writes the key on the spline; the next call
on the **same** input detects the existing spline and only adds the new key — the modifier
is never re-attached. This is
[operating-notes #6](./operating-notes.md#6-keyframing-a-virgin-fusion-input-needs-a-bezierspline-modifier-first).

- `value` is a **string**, auto-coerced to int/float/bool where it looks numeric/boolean.
- `frame` is the time of the key.
- `replace=True` replaces ALL keys on the spline; default `False` merges this one key.
- The response reports `readback`, `keyframe_count`, and `added_modifier` (True on the first
  key of an input). If `AddModifier` returns falsy, the input doesn't accept a spline — the
  tool returns an explanatory error rather than silently setting a static value.

Example — animate a Merge's `Size` from 0 → 1 over 30 frames:
```
fusion_add_keyframe("Merge1", "Size", frame=0,  value="0.0")   # attaches BezierSpline
fusion_add_keyframe("Merge1", "Size", frame=30, value="1.0")   # reuses the spline
```

Common animatable scalar inputs: `Blend`, `Size`, `Angle`, `Opacity`, filter strengths. For
2-component point inputs (`Center`), key each sub-component or use `execute_fusion_lua`.
There is no separate "add modifier" tool — BezierSpline is applied for you by
`fusion_add_keyframe`; other modifier types (XYPath, Expression, Perturbation, …) require
`execute_fusion_lua`.

**Edit-page transform keyframes are different:** to keyframe an Inspector Transform/Crop/
Composite property **directly on the timeline item** (not a Fusion input), use
`add_transform_keyframe(property_name, frame, value, interpolation="")` /
`get_transform_keyframes` / `delete_transform_keyframe`. Those keys need keyframe mode on
first (`set_keyframe_mode` with mode 1 or 2) or they land as a static value. Valid
properties: `Pan`, `Tilt`, `ZoomX`, `ZoomY`, `RotationAngle`, `AnchorPointX`,
`AnchorPointY`, `CropLeft`, `CropRight`, `CropTop`, `CropBottom`, `Opacity`. Interpolation:
`Linear`, `Bezier`, `EaseIn`, `EaseOut`, `EaseInOut`.

---

## Applying an OFX / ResolveFX plugin (`apply_ofx_to_clip`)

`apply_ofx_to_clip(regid, params="", track_type="video", track_index=1, item_index=0, comp_index=1)`

Drops a ResolveFX/OFX node into the clip's Fusion comp **and splices it into the render
chain** between `MediaOut1`'s current upstream tool and `MediaOut1` (new node's `Source`
takes the old upstream; `MediaOut1.Input` takes the new node), wrapped in one undoable step.
So the effect actually touches the picture — no orphan node.

- `regid` — the registry id **WITHOUT** the `ofx.` prefix
  (e.g. `com.blackmagicdesign.resolvefx.gaussianblur`). The tool prepends `ofx.`; a value
  that already has it is used as-is. See
  [operating-notes #5](./operating-notes.md#5-apply_ofx_to_clip-keeps-the-ofx-prefix-and-splices-before-mediaout1).
- `params` — optional JSON object of input-id → value applied **after** wiring, e.g.
  `'{"Blend": 0.5, "FilterType": 1}'`. Values coerce from strings. A single bad input id
  doesn't abort the (already-wired) apply.
- If the clip's comp has **no `MediaOut1`**, the splice can't happen and the tool errors —
  create/attach a Fusion comp on the clip first (`add_fusion_comp`).
- If `AddTool` returns nil, the RegID is unregistered or the plugin isn't installed — check
  the id and installation.

```
apply_ofx_to_clip(
    regid="com.blackmagicdesign.resolvefx.gaussianblur",
    params='{"Blend": 0.5}',
)
```

**OFX enum inputs are addressed by integer index, not label.** `fusion_set_input` will try
to map an unmatched label string to its choice index automatically (and reports
`resolved_index`), but the reliable move is to pass the integer (e.g. `"FilterType": 1`).

---

## Discovering RegIDs and inputs

Our server has **no `GetRegList`** surface. Use these instead:

| To discover… | Use | Needs Resolve? |
|--------------|-----|----------------|
| The **inputs** (`INPS_ID` / name / data type) a tool accepts | `fusion_list_inputs(tool_name, comp_index=1, …)` | yes (tool must exist in a comp) |
| A **live tool's ground-truth RegID** (`TOOLS_RegID`) | `discover_regid(tool_name="")` — opens the Fusion page, reads `GetAttrs()['TOOLS_RegID']`; empty name = active tool. For OFX it also returns the `ofx.`-stripped `wireId` | yes (Fusion page open) |
| **Installed OFX bundles** on disk | `enumerate_ofx()` — scans the OFX/Plugins dirs for `*.ofx.bundle` | no |
| **Insertable Fusion titles/generators** on disk | `enumerate_templates(scope="all"\|"shipped"\|"user")` | no |

Workflow: `get_resolvefx_registry()` → copy the entry's `regid` (already `ofx.…`) for
`fusion_add_tool`, or its `wireId` (no prefix) for `apply_ofx_to_clip`. Then
`fusion_list_inputs` on the added node to read the exact input IDs before `fusion_set_input`.
The current value of any input reads back via `fusion_get_input(tool_name, input_name)`.

---

## Edit-page Inspector property reference

These are **not** Fusion — they're the Video-tab Inspector panels on the `TimelineItem`
itself, via `TimelineItem.SetProperty`. Each panel is an all-optional setter: **only the
fields you pass are changed.** `inspector_property_reference()` returns this whole table
(schema + enum name↔int maps) live and needs no Resolve connection;
`get_inspector_properties()` reads a specific item's current values (enum keys come back as
the raw integer — map them with the tables below).

Enum fields accept a **name (case-insensitive) or the integer** interchangeably
(`set_composite(mode="Multiply")` == `set_composite(mode=4)`). Values beyond a numeric range
are clipped by Resolve; `width`/`height` are the timeline's UI max limits.

### `set_transform(...)` — Transform panel

| Property | Param | Type | Range |
|----------|-------|------|-------|
| `Pan` | `pan` | float | −4.0×width to 4.0×width |
| `Tilt` | `tilt` | float | −4.0×height to 4.0×height |
| `ZoomX` | `zoom_x` | float | 0.0 to 100.0 (1.0 = 100%) |
| `ZoomY` | `zoom_y` | float | 0.0 to 100.0 |
| `ZoomGang` | `zoom_gang` | bool | link X/Y zoom |
| `RotationAngle` | `rotation_angle` | float | −360.0 to 360.0 |
| `AnchorPointX` | `anchor_point_x` | float | −4.0×width to 4.0×width |
| `AnchorPointY` | `anchor_point_y` | float | −4.0×height to 4.0×height |
| `Pitch` | `pitch` | float | −1.5 to 1.5 |
| `Yaw` | `yaw` | float | −1.5 to 1.5 |
| `FlipX` | `flip_x` | bool | mirror horizontally |
| `FlipY` | `flip_y` | bool | mirror vertically |

`reset_transform()` restores the neutral identity (Pan/Tilt/Rotation/Anchor/Pitch/Yaw = 0,
Zoom = 1.0, ZoomGang on, flips off).

### `set_cropping(...)` — Cropping panel

| Property | Param | Type | Range |
|----------|-------|------|-------|
| `CropLeft` | `crop_left` | float | 0.0 to width |
| `CropRight` | `crop_right` | float | 0.0 to width |
| `CropTop` | `crop_top` | float | 0.0 to height |
| `CropBottom` | `crop_bottom` | float | 0.0 to height |
| `CropSoftness` | `crop_softness` | float | −100.0 to 100.0 |
| `CropRetain` | `crop_retain` | bool | "Retain Image Position" |

### `set_composite(...)` — Composite panel

| Property | Param | Type | Range |
|----------|-------|------|-------|
| `CompositeMode` | `mode` | enum | CompositeMode (0–31), name or int |
| `Opacity` | `opacity` | float | 0.0 to 100.0 |
| `Distortion` | `distortion` | float | −1.0 to 1.0 |

**CompositeMode** names → int: Normal 0, Add 1, Subtract 2, Difference 3, Multiply 4,
Screen 5, Overlay 6, HardLight 7, SoftLight 8, Darken 9, Lighten 10, ColorDodge 11,
ColorBurn 12, Exclusion 13, Hue 14, Saturate 15, Colorize 16, LumaMask 17, Divide 18,
LinearDodge 19, LinearBurn 20, LinearLight 21, VividLight 22, PinLight 23, HardMix 24,
LighterColor 25, DarkerColor 26, Foreground 27, Alpha 28, InvertedAlpha 29, Lum 30,
InvertedLum 31.

### `set_dynamic_zoom(...)` — Dynamic Zoom panel

| Property | Param | Type | Range |
|----------|-------|------|-------|
| `DynamicZoomEase` | `ease` | enum | DynamicZoomEase (0–3) |

**DynamicZoomEase**: Linear 0, In 1, Out 2, InAndOut 3.

### `set_retime_and_scaling(...)` — Retime & Scaling panel

| Property | Param | Type | Range |
|----------|-------|------|-------|
| `RetimeProcess` | `retime_process` | enum | RetimeProcess (0–3) |
| `MotionEstimation` | `motion_estimation` | enum | MotionEstimation (0–6) |
| `Scaling` | `scaling` | enum | Scaling (0–4) |
| `ResizeFilter` | `resize_filter` | enum | ResizeFilter (0–15) |

**RetimeProcess**: UseProject 0, Nearest 1, FrameBlend 2, OpticalFlow 3.
**MotionEstimation**: UseProject 0, StandardFaster 1, StandardBetter 2, EnhancedFaster 3,
EnhancedBetter 4, SpeedWarpBetter 5, SpeedWarpFaster 6.
**Scaling**: UseProject 0, Crop 1, Fit 2, Fill 3, Stretch 4.
**ResizeFilter**: UseProject 0, Sharper 1, Smoother 2, Bicubic 3, Bilinear 4, Bessel 5,
Box 6, CatmullRom 7, Cubic 8, Gaussian 9, Lanczos 10, Mitchell 11, NearestNeighbor 12,
Quadratic 13, Sinc 14, Linear 15.

> A partial-set response ("Resolve rejected …") means the key isn't valid for this item's
> type or the value is out of range — enum setters validate names/ints up front and return
> `Error: …` listing the valid choices before touching the item.
