---
name: davinci-resolve-use-plugins
description: >-
  Discover and USE every plugin/effect/template on this machine in DaVinci Resolve via the
  davinci-resolve MCP: built-in ResolveFX/OFX effects applied to a clip, and installed Fusion
  template packs (titles, lower-thirds, chapter cards, intros, generators, effects, transitions —
  e.g. purchased MotionVFX .drfx packs). The organizing principle is the MediaIn-count classifier:
  every MotionVFX element on a timeline is a clip Fusion comp; count its MediaIn tools (0 = title/
  generator, 1 = effect, 2 = transition) to pick the placement lane. Use when the user wants to add
  an effect/plugin, drop a title/lower-third/intro/chapter card, apply a ResolveFX, place a
  MotionVFX transition, or "use my plugins / MotionVFX / templates" in Resolve.
---

# Use plugins, effects & template packs (DaVinci Resolve)

> **Claude: you CAN use all of these — including transitions.** Every plugin/effect/template in the
> TOC (`davinci-plugins.md`) is usable right now via scripting: titles, generators, effects,
> ResolveFX, **and MotionVFX transitions**. The MotionVFX pipeline is **proven live** (chapter cards,
> lower-third, subscribe overlay, and effects placed and frame-confirmed). Don't tell the user
> anything is "GUI-only" or that you can't use their MotionVFX packs. When asked to use one:
> (1) find it in the TOC, (2) **classify it** (count its MediaIn tools — below), (3) run that lane's
> tool. Do this before saying anything is impossible.

**The one organizing principle: count the `MediaIn` tools.** Once a MotionVFX element is on a
timeline it is a clip Fusion comp. That single number picks the lane — mixing lanes up is the #1
cause of "nothing happened":

| Element (classify FIRST) | MediaIn | Placement lane | Tools |
|---|---|---|---|
| **Title / generator** — lower-third, callout, chart, meter, avatar, logo, quote | **0** | carrier on an **upper** track (V2+) — generates an alpha overlay | `cache_template_comp` → `place_overlay_title` |
| **Effect** — camera move, magnify, stylize, button, switch; also list/quote/cover cards that composite over footage | **1** | apply on the clip's **OWN** track (it processes the clip below) | `cache_effect_comp` → `apply_clip_effect` |
| **Transition** — A/B blend across a cut | **2** | offline `.drt` injection (a 2-input comp can't export standalone) | `place_motionvfx_transition` |
| **ResolveFX / OFX** — built-in, not MotionVFX | n/a | apply on a clip (node spliced before `MediaOut1`) | `apply_ofx_to_clip(regid)` |

This is a focused composition over the base **`davinci-resolve`** MCP skill (see its
`cookbook.md` / `fusion-tools.md` / `reference/operating-notes.md`).

## 0 — Ensure the TOC + CLAUDE.md are current, then read the TOC
1. **If `davinci-plugins.json` / `davinci-plugins.md` is missing, OR the root `CLAUDE.md` has no
   `<!-- BEGIN davinci-plugins -->` block, OR the user just installed/removed packs → run the
   `davinci-resolve-generate-plugin-list` skill first.** It (re)writes the TOC **and updates
   root `CLAUDE.md`** with the capability summary, so every future session knows these exist. Do
   this before using anything — it's how the awareness stays in sync.
2. **Read the TOC** — it has every exact `name`, `description` (e.g. `Titles › Infographics`),
   ResolveFX `regid`, and for `.drfx` templates the `drfx` archive path + internal `member` path.
3. Only fall back to live `enumerate_templates` / `get_resolvefx_registry` if you can't generate
   the TOC (e.g. the MCP repo venv isn't reachable).

## Preconditions
DaVinci Resolve **Studio** running, project + timeline open, External scripting = Local. Work on a
duplicate/versioned timeline (insert/apply mutate the live timeline with no scripted undo).
**Verify every result with `export_current_frame` on a sampled hold frame — NOT
`get_current_thumbnail`** (see the verify rule at the bottom; the thumbnail shows one clip's
Color-page output and reads **black** for an alpha overlay, misleading you).

## Classify first — the MediaIn-count classifier
`classify_timeline_element(track_type, track_index, item_index)` returns `{macro, media_in, lane}`.
Under the hood it reads the clip's comp and counts MediaIn tools — the same number that drives the
table above:

```python
c = item.GetFusionCompByIndex(1)                  # 1-based
tools = c.GetToolList(False)                       # {1: tool, ...}
macro = next(t.Name for k,t in tools.items() if t.GetAttrs("TOOLS_RegID")=="MacroOperator")
n_in  = sum(1 for k,t in tools.items() if t.GetAttrs("TOOLS_RegID")=="MediaIn")
# n_in == 0 → title/generator | 1 → effect | 2 → transition
```

`ExportFusionComp` **succeeds for 0- and 1-MediaIn** comps (titles + effects are file-cacheable) and
**returns `false` for 2-MediaIn transitions** (a standalone `.comp` can't carry both neighbour
feeds) — which is exactly why transitions go the `.drt` route. **Never walk the full graph live**
(`GetInputList()`+connections across many clips hung the connection once — see operating-notes §10):
read `GetToolList` per clip in small batches, classify, then export only the few you need.

## Lane A — Titles / generators (0 MediaIn) — PROVEN pipeline
A native insert lands on **V1** and a **raw `.drfx` `.setting` renders BLACK** (it's a bare
`MacroOperator` with no output node). The proven fix is to round-trip a real comp, then place it on
a **carrier** clip on an upper track:
1. **`cache_template_comp(name)`** — on a scratch timeline `InsertFusionTitle(name)` →
   `ExportFusionComp(path)`. The export gains the real output node: **`MediaOut1 = Saver`** (a
   `Saver` named `MediaOut1`, wired `Input = { SourceOp=<last tool>, Source="Output" }`). Cache once
   per template.
2. **`place_overlay_title(name, track, record_frame, duration, fields)`** — appends a video-only
   **carrier** on the overlay track via `AppendToTimeline([{…, trackIndex>=2, mediaType:1}])`
   (**never V1**), attaches the cached comp with `attach_fusion_comp`, then fills the layout with
   `set_template_fields(fields, name=<template>)`. `duration` = on-screen time.
3. Verify with **`export_current_frame`** — titles are **kinetic**, so sample a **hold frame** (a
   beat after the in-animation settles), not the very first frame.

Do **not** use the old "`insert_template_by_name` then `move_clips` up" flow — the raw insert renders
black and lands on the footage track.

## Lane B — Effects (1 MediaIn) — apply on the clip's own track
A 1-MediaIn effect processes the clip below it (its MediaIn node is named `MediaSource`; on import
Resolve re-binds it to the target clip's media). This covers camera moves, magnify, stylize, buttons,
switches, and the list/quote/cover cards that embed and composite over footage.
1. **`cache_effect_comp(name)`** — from a GUI-placed reference clip of that effect,
   `ExportFusionComp(path, 1)` (succeeds for 1-MediaIn) and cache it. The exported comp is already
   wired `MediaSource (MediaIn) → <Macro> → MediaOut1 (Saver, Source="Output")`.
2. **`apply_clip_effect(name, track_type, track_index, item_index)`** — imports the cached comp onto
   the **target clip's own track** (not an upper carrier); optionally `set_template_fields` for
   exposed controls.
3. Verify with **`export_current_frame`**.

**Why the old `apply_macro_to_clip(raw .setting)` blacked out:** it imported the macro **alone**, with
no `MediaSource`/`MediaOut1` scaffold. Never hand-build that scaffold — export an already-wired
reference clip and re-import it (the same round-trip that makes Lane A work).

## Lane C — ResolveFX / OFX (built-in, apply onto a clip)
`apply_ofx_to_clip(regid="com.blackmagicdesign.resolvefx.vignette", params='{"Opacity":0.8}',
track_type="video", track_index=1, item_index=0)`
- `regid` = the TOC's `regid` (wireId) **WITHOUT** the `ofx.` prefix (the tool adds it).
- Node is spliced before `MediaOut1`, so it renders. Enum inputs take the **integer** index.
- Upbeat-look picks (with TOC descriptions): `vignette`, `glow`, `filmlook`, `lightray`, `sharpen`,
  `gaussianblur` (blur-in), `zoomblur` (punch energy), `dehaze`, `beauty`.

## Lane D — Transitions (2 MediaIn) — offline `.drt` injection
A transition comp is `MediaIn1 + MediaIn2 → <Macro> → MediaOut1` and **does not export standalone**
(`ExportFusionComp` returns `false` — confirmed on all 44 MotionVFX types), so it is placed by editing
the timeline's `.drt`, not by comp import.
- **`place_motionvfx_transition(name, cut_frame, duration_frames)`** — resolves the template name to a
  library `<Sm2TiTransition>` element (harvested from the reference `.drt`), sets `<Start>=cut_frame`
  and `<Duration>=duration_frames`, gives it a fresh `DbId`, injects it into the target `.drt`
  `<Items>`, and re-imports via `import_timeline_from_file`. Verify with `export_current_frame`.
- The 44 names span mTuber 3/4, mKeynote, mKeynote 2, mPodcast, mAntique (e.g. `mTuber 4 Zoom`,
  `mTuber 4 Swoosh`, `mKeynote 2 Color Wipe`, `mAntique Double Exposure`). This is the whole library —
  don't default to a plain dissolve when the user owns these.
- **Built-in dissolves** remain the fallback: `add_default_transition_at_cut(...)` (live, Cmd+T) or
  the offline `place_transition` / `author_transition_interchange` (built-in SMPTE list,
  `"verified": false`). Reach for `place_motionvfx_transition` first when the user wants a named
  MotionVFX transition.

## Resolve AI / native features (validated live)
- **Voice Isolation** — `set_voice_isolation_state(track_index, enabled, amount 0-100)`. Works live.
- **Stabilize** — `stabilize(track_type, track_index, item_index)`. Neural Engine, live.
- **Smart Reframe** — `smart_reframe(track_type, track_index, item_index)`. **Apply-only**: it bakes
  into internal reframe state and is **not** reflected in the readable transform block, so you can't
  read/tune the computed path back.
- **Super Scale** — `set_super_scale(clip, mode)` where **`mode` is an INT 1-4** (1 = Auto/none,
  2 = 2×, 3 = 3×, 4 = 4×). It's a **MediaPoolItem** property, so it propagates to **every** timeline
  instance of that source. A string value fails — pass an int (the underlying `set_clip_property`
  coerces numeric strings, so `"2"`/`"3"`/`"4"` also work).
- **Dynamic Zoom** — only the **easing** is scriptable: `set_dynamic_zoom(ease, …)` →
  `DynamicZoomEase` (Linear/In/Out/InAndOut). The **enable flag and pre/post-crop rects are NOT
  exposed**. For a scripted move: an **animated** push-in / Ken-Burns is done with
  **`animate_clip_transform`** (a keyframed Fusion Transform on a video-only carrier+comp), a
  **static** punch-in with **`set_transform`** on the clip. **Do not rely on
  `add_transform_keyframe`** — it is a **no-op** on current Resolve builds (Edit-page transform
  keyframes are unscriptable; `TimelineItem.AddKeyframe` doesn't exist). Do **not** rely on the
  Dynamic Zoom panel from script.

## Guardrails
- **Overlays NEVER on V1.** Titles/generators go on a carrier on an **upper** video track via
  `AppendToTimeline(trackIndex>=2, mediaType:1)` — never the footage track (an insert on V1
  splits/overwrites the clip; above it, the graphic composites over the video via its alpha). A
  1-MediaIn **effect** is the exception — it belongs on the clip's own track (it processes that clip).
- **Classify before you place.** `classify_timeline_element` (MediaIn count) picks the lane; running
  the wrong lane's tool is why "nothing happened". ResolveFX use the `regid` lane, MotionVFX effects
  the effect-comp lane, transitions the `.drt` lane.
- **Non-destructive**: insert/apply on a versioned timeline, not the master. Save with
  `pm.SaveProject()`.
- Relay `Error:` strings; verify visually with `export_current_frame`. See the base skill's
  `operating-notes.md` for the `ofx.`-prefix, verify rule, and Fusion gotchas.

## Honest limits
- Titles, generators, effects, ResolveFX, **and MotionVFX transitions are all scriptable** via the
  lanes above — nothing here is "GUI-only".
- Transition placement is via offline `.drt` injection (`place_motionvfx_transition`) — structurally
  proven; a live re-import + `export_current_frame` is the calibration.
- `set_template_fields` drives the published macro controls; `set_title_text` covers only the primary
  Text+ field. Multi-field MotionVFX layouts: pass the full `fields` dict (or `fusion_set_input`).
- **Smart Reframe** and **Dynamic Zoom framing** are apply-only / not introspectable (for a scripted
  zoom use `animate_clip_transform` for an animated move or `set_transform` for a static one —
  `add_transform_keyframe` is a no-op, see the motion note above). **Super Scale** needs an **int**.
- A rare pack may register a different display name than its `.setting` basename — if resolution
  fails, confirm the name in the GUI Effects Library.
