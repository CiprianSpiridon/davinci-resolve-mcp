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
