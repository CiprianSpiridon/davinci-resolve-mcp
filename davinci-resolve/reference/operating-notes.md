# DaVinci Resolve MCP — operating notes (real-world gotchas)

Load this on demand when you drive the newer / less-obvious surface (Neural-Engine
extras, gallery stills, color management, transitions, OFX, Fusion keyframing, node
graphs, cloud projects, app lifecycle). These are **behaviours that look like bugs but
are not** — the tool is doing the correct thing given a Resolve API constraint. Read the
returned string and react per the note; don't retry blindly.

Everything here is guidance only — it does **not** change the tool counts in
[`../SKILL.md`](../SKILL.md) or [`tool-catalog.md`](./tool-catalog.md).

---

## 1. Studio + Neural-Engine-Extras gating returns **False**, not an error
`AnalyzeForIntellisearch`, `AnalyzeForSlate`, `GenerateSpeech`, and `TranscribeAudio`
(the Neural-Engine "extras") are Studio features. When the running edition can't perform
them, the underlying Resolve call **returns `False` / a falsy result rather than raising**,
and some are absent on the object entirely (the tools `hasattr`-guard first and return an
explanatory string). Treat a `False`/"failed" result as *"unavailable in this edition or
this clip state"*, not as something to loop on. Tools: `analyze_for_intellisearch`,
`analyze_for_slate`, `generate_speech`, plus `transcribe_audio` (Studio path).
For a no-Studio alternative to transcription, use the local Whisper tools
(`transcribe_audio` local path / `transcribe_and_add_subtitles` / `export_srt`).

## 2. Gallery `ExportStills` / stills ops need the Gallery panel **visible** — can return False silently
`grab_still`, `grab_all_stills`, and album `ExportStills` require the **Color page** with
the **Gallery panel shown**. If it isn't visible, the API can return `False` **silently**
(no exception). Before stills work: `open_page('color')`, make sure the Gallery panel is
up, then retry. Relay the tool's "requires the Gallery panel to be visible" string to the
user instead of assuming the export happened — verify by checking the target path.

## 3. `set_input_color_space` needs a **color-managed** project (no silent science-mode toggle)
Setting a media-pool clip's Input Color Space only works when the project is in a
color-managed mode (**DaVinci YRGB Color Managed** or **ACES**). In a plain **DaVinci
YRGB** project the set **silently fails** (value unchanged). The tool does **not** flip
`colorScienceMode` for you — changing color science is a project-wide decision with grade
consequences, so it stays the user's call. If the clip's color space won't stick, check
`colorScienceMode` first (via project settings) and tell the user to switch the project to
a color-managed mode before retrying.

## 4. The scripting API has **no transition object** — transitions are a hybrid
Resolve's scripting API exposes **no** object to create/read/edit a timeline transition
(no `AddTransition`). So `place_transition` and friends cannot verify through the API. The
server offers three honest paths, and offline writes are marked **`"verified": false`**:
- **Offline byte-patch** into a `.drt`/`.drp` (`place_transition`, `drt` authoring) —
  structurally valid, `"verified": false`; a live Resolve import is the only real
  confirmation.
- **Interchange authoring** (`author_transition_interchange`,
  `author_audio_crossfade_interchange`) → a `.drt` you `import_timeline_from_file`.
- **Keystroke hybrid** — fire the default-transition shortcut (**Cmd+T** video /
  **Shift+T** audio on macOS) at the playhead; the tool reports `"verified": true` only
  when it can re-read the item list and see the change, otherwise `"verified": false` with
  a "keystroke may not have registered" note.

Always relay the `verified` flag: a `false` means "written but not confirmed against a live
Resolve" — tell the user before they rely on the file.

## 5. `apply_ofx_to_clip` keeps the `ofx.` prefix and splices **before `MediaOut1`**
Pass `regid` **without** the `ofx.` prefix (e.g.
`com.blackmagicdesign.resolvefx.gaussianblur`). Fusion's registry needs the prefix, so the
tool adds it (`comp.AddTool('ofx.' + regid, …)`, and it keeps an existing `ofx.` if you
included one). The new node is spliced into the clip's Fusion render chain **between
`MediaOut1`'s current upstream tool and `MediaOut1`** (new node's `Source` takes the old
upstream, `MediaOut1`'s `Input` takes the new node) so the effect actually renders. If the
clip's comp has **no `MediaOut1`**, the splice can't happen and the tool returns an error —
create/attach a Fusion comp on the clip first.

## 6. Keyframing a **virgin** Fusion input needs a `BezierSpline` modifier first
A plain `SetInput` on an un-animated Fusion input sets a **static** value — it will not
animate. `fusion_add_keyframe` therefore attaches a `BezierSpline` modifier
(`tool.AddModifier(input, "BezierSpline")`) the **first** time an input is keyed, then
writes the keyframe on the spline; later keys reuse the existing spline
(`GetConnectedOutput().GetTool()` recovers it). If `AddModifier` fails, the input may not
accept a spline — the tool returns an explanatory error rather than silently setting a
static value.

## 7. Color **node graphs are read / serve only** (no per-parameter API)
The color-page node graph tools (`get_node_graph`, `get_num_nodes`, `get_node_label`,
`set_node_enabled`, LUT/CDL) **read and serve** graph structure and a few coarse controls;
Resolve exposes **no per-parameter scripting API** for individual grade wheels/curves/qualifiers.
Don't promise fine parameter edits through scripting. For grade values, use `set_cdl` /
`set_lut`, color versions, or the **offline** `.drx` path (`drx` tool) — and remember
offline grade writes come back `"verified": false`.

## 8. Cloud-project APIs are **Studio / 18.5+**, `hasattr`-gated
Blackmagic Cloud project lifecycle and cloud settings live on newer Studio builds
(**Resolve 18.5+**). The tools guard every lookup with `hasattr` and, when the method is
missing, return `Error: … Requires DaVinci Resolve Studio 18.5 or newer.` (database
tools similarly guard `GetDatabaseList` / `GetCurrentDatabase` / `SetCurrentDatabase` /
`RestoreProject`). A "not available in this version" string here is the edition/version
gate, not a fixable failure.

## 9. `quit_resolve` **terminates the application**
`quit_resolve` shuts the whole Resolve app down — after it, every live tool will return
`Error: Could not connect to DaVinci Resolve` until the user relaunches. It is a
destructive, hard-to-undo action: **confirm with the user first**, and never call it as
part of a cleanup/retry loop.

## 10. Never live-introspect a Fusion comp's FULL graph — it hangs Resolve
Enumerating every tool in a comp and walking each tool's inputs **hung Resolve** on heavy
MotionVFX / template comps (verified). Don't do it. Read a template's controls **offline** from
its `.setting` inside the `.drfx` — `get_template_controls(name)` returns the `macro_tool`, the
published input **keys**, and resolved defaults with **no** Resolve running. Then set **specific
inputs by key on the named macro tool**: `set_template_fields` (or `fusion_set_input`) resolves
the ONE macro via `FindTool(macro_tool)` and calls `SetInput(key, value)`. Reading that single
macro's inputs is fine; walking **every** tool is what hangs. The offline `.setting` is the source
of truth for a template's controls + defaults and is stable regardless of the running server
build — so there's no reason to pay the (Resolve-hanging) cost of full live graph introspection.

## 11. MotionVFX + AI automation — durable invariants (element type = MediaIn count)
The MotionVFX pipeline is proven live. Once an element is on a timeline it's a clip Fusion comp;
count its `MediaIn` tools (`GetFusionCompByIndex(1).GetToolList(False)`) and that number **is** the
placement lane — read it, then dispatch. These are the invariants; don't regress them:
- **Classifier: 0 = title/generator** (carrier on an upper track), **1 = effect** (apply on the
  clip's own track), **2 = transition** (offline `.drt` injection).
- **`ExportFusionComp` succeeds for 0- and 1-MediaIn comps, returns `False` for 2-MediaIn
  transitions** (a standalone `.comp` can't carry both neighbour feeds) → titles + effects are
  file-cacheable; transitions must go the `.drt` route.
- **A raw `.drfx` `.setting` renders BLACK** (bare `MacroOperator`, no output node). Never
  hand-build the scaffold — **round-trip a native or exported comp**, which gains the real output
  node **`MediaOut1 = Saver`** wired `Source="Output"`. (For a 1-MediaIn effect the exported comp
  also carries a `MediaSource` MediaIn that Resolve re-binds to the target clip on import.)
- **`export_current_frame` is the ONLY reliable composite check.** `get_current_thumbnail` shows one
  clip's Color-page output — **black** for an alpha overlay — and lies. Titles are kinetic; sample a
  hold frame, not frame 0.
- **Super Scale set needs an INT** (a string returns `False`). It's a MediaPoolItem property, so it
  propagates to every timeline instance of that source.
- **Dynamic Zoom enable + framing rects are NOT scriptable** — only the easing (`DynamicZoomEase`).
  For a scripted push-in / Ken-Burns use `add_transform_keyframe` (ZoomX/ZoomY/Pan/Tilt, after
  `set_keyframe_mode(1)`).
- **`Insert*` targets V1 and can't be redirected** — overlays go on an upper track via
  `AppendToTimeline(trackIndex>=2, mediaType:1)`, and its `endFrame` is **EXCLUSIVE**. Locking V1
  makes an insert **fail**, it does not redirect.
- Save with **`pm.SaveProject()`** (not `project.SaveProject()`).
- **Resolve proxy objects make `hasattr` ALWAYS true** — never probe for a method that way; call it
  and handle the falsy/`None` result.
- **Don't walk the full Fusion graph live** (see §10) — `GetToolList` per clip in small batches,
  classify, export only the few you need.

---

### General reminders that reinforce the above
- Every tool returns a **plain string**; failures are `"Error: …"` strings, never
  exceptions. Read and react.
- **`"verified": false`** on any offline `.drt` / `.drp` / `.drx` write means
  *structurally valid but not calibrated against a live Resolve panel* — say so before the
  user relies on the file.
- Studio-only surfaces (Magic Mask, Smart Reframe, Stabilize, AI subtitles, Voice
  Isolation, the Neural-Engine extras above) return a "requires Resolve Studio" string on
  the free edition — expected, not a bug.
