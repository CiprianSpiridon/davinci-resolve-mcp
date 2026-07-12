---
name: davinci-resolve
description: >-
  Drive DaVinci Resolve (editing, media pool, color grading, Fusion, Fairlight audio,
  AI/Neural Engine, rendering, transcription) through the davinci-resolve MCP server.
  Use when the user wants to inspect or change a Resolve project, timeline, clips, grade,
  titles, subtitles, or renders — e.g. "add markers", "cut this timeline", "grade this
  clip", "make a vertical reframe", "render an H.264", "transcribe and add subtitles",
  "what's on video track 1". Do NOT use for non-Resolve video tools, or when the
  davinci-resolve MCP server is not configured.
---

# Operating DaVinci Resolve via MCP

This skill tells you how to use the **`davinci-resolve`** MCP server well. It exposes
**190 tools** across 18 domains (project/timeline/media/color/fusion/audio/AI/render/
transcription) plus 3 `resolve://` resources and an `editing_strategy` prompt. The full,
exact tool list with one-line descriptions is in
[`reference/tool-catalog.md`](./reference/tool-catalog.md) — open it when you need the
precise tool name/params for a domain instead of guessing.

## Preconditions — check these before acting

1. The `davinci-resolve` MCP server must be configured in this client. If its tools
   aren't available, stop and point the user at the repo's `INSTALL.md`.
2. **DaVinci Resolve must be running with a project open**, and **Preferences → General →
   "External scripting using" = Local**. If a tool returns
   `Error: Could not connect to DaVinci Resolve`, that's the cause — ask the user to
   launch Resolve, open a project, and confirm the scripting preference.
3. Scripting is a Resolve **Studio** feature. On the free edition, Studio-only tools
   (Magic Mask, Smart Reframe, Stabilize, AI subtitles, Voice Isolation) return a
   "requires Resolve Studio" string — that's expected, not a failure to retry.

## Golden workflow: orient → act → verify

**Every tool returns a plain string, and failures come back as `"Error: ..."` strings,
never exceptions.** Always read the returned string and react to it — do not assume
success.

1. **Orient first.** Before changing anything, understand the current state:
   - `get_project_info`, `get_current_page`, `get_current_timeline_info`,
     `get_timeline_items` — or read the resources `resolve://project/info`,
     `resolve://timeline/current`, `resolve://mediapool/structure`.
   - **Use `screenshot` as your eyes.** Call it to see the actual Resolve UI before a
     visual change, after it, when the user describes something visual ("this looks too
     dark", "the title is off-centre"), and when debugging why something didn't work.
2. **Switch to the right page** with `open_page` when a task needs it — color work needs
   the `color` page; Fusion needs `fusion`; delivery needs `deliver`.
3. **Act with the most specific tool.** Prefer a dedicated tool over the generic
   `execute_resolve_code` escape hatch (see Safety). Locate timeline items by
   `track_type` (video/audio/subtitle), 1-based `track_index`, and 0-based `item_index`.
4. **Verify.** Re-read state (or `screenshot`) to confirm the change landed. Report the
   tool's own result string back to the user.

## Tool map by task (open the catalog for exact names/params)

- **Navigate / inspect app**: `open_page`, `get_current_page`, `get_project_info`,
  layout presets, keyframe mode → `tools/resolve_app.py`, `tools/project.py`.
- **Projects (lifecycle)**: create/load/save/close/delete, databases, import/export →
  `tools/project_manager.py`.
- **Media**: browse volumes and import (`tools/media_storage.py`); bins, import,
  move/delete/relink, create timelines (`tools/media_pool.py`); per-clip properties,
  metadata, markers, flags, clip colour, proxies (`tools/media_pool_item.py`).
- **Timeline (read/structure)**: list/switch/duplicate timelines, settings, tracks,
  timecode, item listing → `tools/timeline.py`.
- **Timeline (edit)**: markers (`add_marker` etc.), inserts
  (`insert_generator`/`insert_fusion_title`/…), compound clips, `detect_scene_cuts` →
  `tools/timeline_edit.py`. Per-item properties/transforms/takes →
  `tools/timeline_item.py`.
- **Color**: node graph, `set_lut`, `set_cdl`, color versions, gallery `grab_still` →
  `tools/color.py`. Requires the color page.
- **Fusion**: comp management on a clip (list/add/import/export/load/delete/rename,
  `create_fusion_clip`) → `tools/fusion.py`. For deep node graphs, use
  `execute_resolve_code`.
- **Audio / Fairlight**: voice isolation, audio track tools → `tools/audio.py`.
- **AI / Neural Engine** (Studio): Magic Mask, Smart Reframe, Stabilize,
  `create_subtitles_from_audio` → `tools/ai.py`.
- **Render / Deliver**: formats/codecs/presets, render settings,
  `add_render_job`/`start_rendering`/`get_render_job_status`/`stop_rendering` →
  `tools/render.py`.
- **Export & stills**: `export_timeline` (AAF/EDL/FCPXML/OTIO/…), `export_current_frame`,
  `get_current_thumbnail` → `tools/export_still.py`.
- **Transcription (local, no Studio needed)**: `transcribe_audio`,
  `transcribe_and_add_subtitles`, `export_srt`, `list_whisper_models` →
  `tools/transcription.py` (needs a Whisper backend + ffmpeg installed).
- **Anything uncovered**: `execute_resolve_code` → `tools/code.py`.

## Safety and judgement

- **`execute_resolve_code` runs arbitrary Python** in Resolve's scripting environment
  (namespace: `resolve`, `project`, `mediaPool`, `timeline`, `mediaStorage`; use
  `print()` or set `result`). Use it only for API surface no dedicated tool covers, keep
  snippets small and read-only-ish where possible, and show the user what you're about to
  run for anything non-trivial.
- **Destructive / hard-to-undo actions — confirm with the user first**: `delete_project`,
  `delete_clips`, `delete_folders`, `delete_track`, `close_project` (unsaved work),
  `replace_clip`, and anything that overwrites files (`export_*`, render `TargetDir`).
  Resolve scripting has no universal undo.
- **Rendering is real work**: after `add_render_job` + `start_rendering`, poll
  `get_render_job_status` rather than assuming instant completion; `stop_rendering`
  cancels.
- **Marker/clip colours are fixed vocabularies** — if a colour is rejected, the tool
  returns the valid list; use one of those (marker/flag colours differ from clip
  colours).
- Don't fabricate results. If a tool returns `Error:` or an empty/"no active timeline"
  message, relay that and fix the precondition rather than pretending it worked.

## Quick recipes

- **"Add subtitles from this audio"**: ensure a timeline is active → for Resolve's built-in
  AI, `create_subtitles_from_audio` (Studio); for a local file, `transcribe_and_add_subtitles`
  (adds markers) or `export_srt` then import as a subtitle track.
- **"Make a vertical (9:16) reframe of this clip"**: `open_page('color')` or `edit`,
  select the item, `smart_reframe` (Studio) — or set transform props via
  `set_timeline_item_property` (Pan/Tilt/ZoomX/ZoomY).
- **"Grade this clip"**: `open_page('color')`, `get_node_graph` to see nodes, then
  `set_cdl` / `set_lut`; `grab_still` to save a reference.
- **"Render an H.264 of the timeline"**: `set_render_settings` (format `mp4`, codec
  `H.264`, `TargetDir`, `CustomName`) → `add_render_job` → `start_rendering` → poll
  `get_render_job_status`.
- **"What's on the timeline?"**: `get_current_timeline_info` + `get_timeline_items`, and
  `screenshot` to show the user.
