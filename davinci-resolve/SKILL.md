---
name: davinci-resolve
version: 1.0.0
description: >-
  Set up and drive DaVinci Resolve through the davinci-resolve MCP server: editing, media
  pool, color grading, Fusion, Fairlight audio, AI/Neural Engine, rendering, and local
  transcription — plus 18 OFFLINE tools that read/write Resolve's own .drp/.drt/.drx files
  and a local SQLite store with no Resolve running (324 tools total). Use when the user wants to
  inspect or change a Resolve project, timeline, clips, transform/reframe, grade, effects,
  Fusion titles, transitions, subtitles, or renders, or to work on .drp/.drt/.drx files
  without Resolve — e.g. "add markers", "cut this timeline", "grade this clip", "reframe
  vertical", "apply a blur", "build a lower third", "render an H.264", "transcribe and add
  subtitles", "edit this .drx offline", "what's on video track 1". Do NOT use for non-Resolve
  video tools.
when_to_use: >-
  Trigger when the user asks to inspect or change anything in a DaVinci Resolve project
  (timeline, clips, media, transform, color, Fusion, effects, transitions, audio, subtitles,
  render). If the MCP server isn't installed yet, INSTALL.md at the repo root has the setup
  steps. Do not use for other NLEs or generic ffmpeg tasks.
argument-hint: "[a DaVinci Resolve task, e.g. 'render an H.264' | 'what's on video track 1']"
---

# DaVinci Resolve MCP

Drives the **`davinci-resolve` MCP server**: **324 tools** — **306 live** across 18 domains
(project/timeline/media/color/Fusion/Fairlight/AI/render/transcription) that drive a running
Resolve, plus **18 offline** tools that read/write Resolve's own files (`.drp`/`.drt`/`.drx`)
and a local SQLite store with **no Resolve running** — plus 3 `resolve://` resources and an
`editing_strategy` prompt.

**Not installed?** If the `mcp__davinci-resolve__*` tools aren't exposed, see
**[`INSTALL.md`](../INSTALL.md)** at the repo root. Everything below assumes a configured server.

---

## Using the tools

**Every tool returns a plain string; failures come back as `"Error: ..."`, never
exceptions.** Read the returned string and react — never assume success.

**Preconditions:** Resolve running with a project open, and **Preferences → General →
"External scripting using" = Local**. `Error: Could not connect to DaVinci Resolve` means one
of these is missing — fix it, don't retry blindly. Scripting is a **Studio** feature; on the
free edition, Studio-only tools (Magic Mask, Smart Reframe, Stabilize, AI subtitles, Voice
Isolation) return a "requires Resolve Studio" string — expected, not a bug.

**Loop: orient → act → verify**
1. **Orient first.** `get_project_info`, `get_current_page`, `get_current_timeline_info`,
   `get_timeline_items` — or read `resolve://project/info`, `resolve://timeline/current`,
   `resolve://mediapool/structure`. **Use `screenshot` as your eyes**: before a visual
   change, after it, when the user describes something visual, and when debugging.
2. **Switch page** with `open_page` when needed (`color` grading, `fusion` comps, `deliver`
   rendering).
3. **Act with the most specific tool** (not the `execute_resolve_code` escape hatch — see
   Safety).
4. **Verify** by re-reading state or a `screenshot`, and relay the tool's own result string.

**Item-locator convention.** Every timeline-item tool addresses a clip by three args:
`track_type` (`video`/`audio`/`subtitle`) + **1-based** `track_index` + **0-based**
`item_index`. Fusion tools add a **1-based** `comp_index`.

### Quick index: TASK → tools
Full recipe for each is in **[`reference/cookbook.md`](./reference/cookbook.md)**.

| Task | Tools | Recipe |
|---|---|---|
| Reframe / transform (pan, zoom, rotate, flip) | `set_transform`, `reset_transform`, `smart_reframe` (Studio) | cookbook → Reframe |
| Crop / dynamic zoom / retime | `set_cropping`, `set_dynamic_zoom`, `set_retime_and_scaling`, `set_composite` | cookbook → Crop |
| Keyframe a move | `add_transform_keyframe`, `get_transform_keyframes`, `delete_transform_keyframe` | cookbook → Keyframe |
| Apply an effect (ResolveFX/OFX) | `enumerate_ofx`, `get_resolvefx_registry`, `discover_regid`, `apply_ofx_to_clip` | cookbook → Apply an effect |
| Apply a template | `enumerate_templates`, `insert_template_by_name`, `append_template_with_placement` | cookbook → template |
| Build / edit a title | `fusion_add_tool`, `set_title_text`, `insert_title`, `insert_fusion_title` | cookbook → title |
| Fusion comp node work | `add_fusion_comp`, `fusion_add_tool`, `fusion_set_input`, `fusion_connect_input` | **fusion-tools.md** |
| Transition at a cut | `add_default_transition_at_cut` (live), `place_transition` (offline `.drt`/`.drp`) | cookbook → Transition |
| Grade a clip | `open_page('color')`, `get_node_graph`, `set_cdl`, `set_lut`, `apply_grade_from_drx` | cookbook → Grade |
| Render / deliver | `set_render_format_and_codec`, `set_render_settings`, `add_render_job`, `start_rendering`, `get_render_job_status` | cookbook → Render |
| Subtitles / transcription | `create_subtitles_from_audio` (Studio), `transcribe_and_add_subtitles`, `export_srt` | cookbook → Subtitles |
| Markers | `add_marker`, `add_item_marker`, `add_clip_marker` | cookbook → Markers |
| Inspect the timeline | `get_current_timeline_info`, `get_timeline_items`, `screenshot` | the orient loop above |

**References, loaded on demand (don't inline them):**
- **[`reference/cookbook.md`](./reference/cookbook.md)** — task recipes, the primary how-to.
  Start here for any "how do I…" task.
- [`reference/fusion-tools.md`](./reference/fusion-tools.md) — Fusion node tools and
  Inspector input tables (address nodes/inputs by name).
- [`reference/tool-catalog.md`](./reference/tool-catalog.md) — the exact 324-tool list
  (live + offline) with one-line descriptions, grouped by module. Open it for a precise
  tool name/param.
- [`reference/operating-notes.md`](./reference/operating-notes.md) — gotchas for the newer /
  less-obvious surface: Neural-Engine extras that return `False` (not an error), gallery
  stills needing the panel visible, color-managed-only input color space, the no-transition-
  object hybrid, the `ofx.` prefix + `MediaOut1` splice, BezierSpline-first keyframing,
  read-only node graphs, Studio-18.5-gated cloud projects, `quit_resolve` terminating the
  app. Skim it before driving those tools.

## Safety & judgement
- **`execute_resolve_code` runs arbitrary Python** in Resolve (namespace: `resolve`,
  `project`, `mediaPool`, `timeline`, `mediaStorage`; `print()` or set `result`). Use only
  for API with no dedicated tool; keep snippets small; show the user non-trivial code first.
- **Confirm before destructive / hard-to-undo actions**: `delete_project`, `delete_clips`,
  `delete_folders`, `delete_track`, `close_project` (unsaved work), `replace_clip`, and
  anything that overwrites files (`export_*`, render `TargetDir`). Resolve scripting has no
  universal undo.
- **Rendering is real work**: after `add_render_job` + `start_rendering`, poll
  `get_render_job_status`; `stop_rendering` cancels.
- **Colours are fixed vocabularies** — a rejected colour returns the valid list (marker/flag
  colours differ from clip colours).
- **Don't fabricate results.** Relay `Error:` / "no active timeline" strings and fix the
  precondition instead of pretending success.

## Offline tools (operate on Resolve FILES + a local SQLite store — **no Resolve needed**)
These 18 action-dispatch tools each take an `action` param + typed args and work with **no
running Resolve** (cloud or local). They read/write Resolve's own files and a local DB.
**Grade/file WRITE actions return `"verified": false`** — structurally valid but not yet
calibrated against a live Resolve panel, so tell the user that before they rely on a written
`.drx`/`.drp`.
- **Files**: `drx` (color grades — inspect/decode/export-CDL/attach-LUT/write), `drt`
  (timelines — parse/author/validate/inject/extract), `drp` (projects — read/author/edit),
  `offline_fusion` (comps).
- **Project intelligence**: `project_read` (lint / clip queries), `project_db`
  (index + `relayout_node_graphs` — tidy node layout, grade bytes preserved),
  `conform` (relink QC + lineage), `color_trace` (carry grades across a re-conform),
  `editorial` (changelist / integrity), `media_ingest` (scan → manifest).
- **Grade/QC compute**: via `drx` actions and the grading cores (CDL ops, white-balance,
  skin-match, broadcast-legal/gamut `qc`), `deliverable` (compliance QC), `offline_ref`.
- **Orchestration**: `pipeline` (DB-as-truth: YAML/JSON spec → SQLite → staged runs with
  gates + provenance + intent-vs-actual drift), `provenance` (audit / episode report),
  `capabilities` (what's available + dep status + verified/unverified state).
Full names/actions: [`reference/tool-catalog.md`](./reference/tool-catalog.md).

## Quick recipes
- **Subtitles from audio**: active timeline → `create_subtitles_from_audio` (Studio) or
  local `transcribe_and_add_subtitles` / `export_srt` then import as a subtitle track.
- **Vertical (9:16) reframe**: `smart_reframe` (Studio) or `set_transform`
  (`zoom_x`/`zoom_y`/`pan`) on the item.
- **Grade a clip**: `open_page('color')` → `get_node_graph` → `set_cdl`/`set_lut` →
  `grab_still`.
- **Render H.264**: `set_render_format_and_codec` (format `mp4`, codec `H.264`) →
  `set_render_settings` (`TargetDir`, `CustomName`) → `add_render_job` → `start_rendering`
  → poll `get_render_job_status`.
- **"What's on the timeline?"**: `get_current_timeline_info` + `get_timeline_items` +
  `screenshot`.
