---
name: davinci-resolve
version: 1.0.0
description: >-
  Set up and drive DaVinci Resolve through the davinci-resolve MCP server: editing, media
  pool, color grading, Fusion, Fairlight audio, AI/Neural Engine, rendering, and local
  transcription (190 tools). Handles first-run ONBOARDING (installs and registers the MCP
  server if it isn't configured yet) and day-to-day operation. Use when the user wants to
  set up the DaVinci Resolve MCP, or to inspect/change a Resolve project, timeline, clips,
  grade, titles, subtitles, or renders — e.g. "install the resolve mcp", "add markers",
  "cut this timeline", "grade this clip", "vertical reframe", "render an H.264",
  "transcribe and add subtitles", "what's on video track 1". Do NOT use for non-Resolve
  video tools.
when_to_use: >-
  Trigger on two situations: (1) SETUP — the user asks to install/configure/onboard the
  DaVinci Resolve MCP, or you try a resolve tool and it isn't available; run the Onboarding
  flow. (2) OPERATION — the user asks to inspect or change anything in a DaVinci Resolve
  project (timeline, clips, media, color, Fusion, audio, subtitles, render); run the
  operating workflow. Do not use for other NLEs or generic ffmpeg tasks.
argument-hint: "[install the resolve mcp | a DaVinci Resolve task, e.g. 'render an H.264']"
---

# DaVinci Resolve MCP — setup & operation

This skill covers the **`davinci-resolve` MCP server** end to end: onboarding a machine
that doesn't have it yet, then operating its **190 tools** across 18 domains
(project/timeline/media/color/Fusion/Fairlight/AI/render/transcription) plus 3
`resolve://` resources and an `editing_strategy` prompt.

**Two references, loaded on demand (don't inline them):**
- [`reference/setup.md`](./reference/setup.md) — the full, gated MCP install/registration
  runbook. Open it when onboarding.
- [`reference/tool-catalog.md`](./reference/tool-catalog.md) — the exact 190-tool list with
  one-line descriptions, grouped by module. Open it when you need a precise tool name/param.

---

## Step 1 — Is the MCP available? (decide setup vs operation)

Check whether the `davinci-resolve` MCP tools are exposed in this session (tool names look
like `mcp__davinci-resolve__get_project_info`, or your client lists a `davinci-resolve`
server).

- **Tools present** → go to [Operating workflow](#operating-workflow).
- **Tools absent, or the user asked to install/set up** → run
  **[Onboarding](#step-2--onboarding-install--register-the-mcp)** first.

## Step 2 — Onboarding (install & register the MCP)

**Open [`reference/setup.md`](./reference/setup.md) and follow it phase by phase.** It is a
gated runbook that: detects OS/Python (≥3.10), clones the repo, creates a `.venv` and
editable-installs the server, verifies **190 tools register offline** (no Resolve needed),
flags the two **human-only** steps (enable Resolve *External scripting = Local*; restart
the MCP client), registers the server with Claude Code (`claude mcp add`), Claude Desktop,
or Cursor using an **absolute** venv binary path, and runs a live smoke test.

Non-negotiables during setup (from that runbook):
- **Absolute paths only** in client config — MCP clients don't inherit `PATH`/venvs.
- **You can't do the two GUI/restart steps** — instruct the user and wait.
- Never install outside `.venv`; never disable TLS.

When onboarding is done, tell the user it's ready and offer a first prompt (e.g. *"What
project is open? List the clips on video track 1."*), then continue with operation.

## Operating workflow

**Every tool returns a plain string; failures come back as `"Error: ..."` strings, never
exceptions.** Read the returned string and react — never assume success.

### Preconditions
1. `davinci-resolve` MCP configured (else run onboarding).
2. **Resolve running with a project open**, and **Preferences → General → "External
   scripting using" = Local**. `Error: Could not connect to DaVinci Resolve` means one of
   these is missing — fix the precondition, don't retry blindly.
3. Scripting is a Resolve **Studio** feature. On the free edition, Studio-only tools (Magic
   Mask, Smart Reframe, Stabilize, AI subtitles, Voice Isolation) return a "requires
   Resolve Studio" string — expected, not a bug to work around.

### Loop: orient → act → verify
1. **Orient first.** `get_project_info`, `get_current_page`, `get_current_timeline_info`,
   `get_timeline_items` — or read `resolve://project/info`, `resolve://timeline/current`,
   `resolve://mediapool/structure`. **Use `screenshot` as your eyes**: before a visual
   change, after it, when the user describes something visual, and when debugging.
2. **Switch page** with `open_page` when needed (`color` for grading, `fusion` for comps,
   `deliver` for rendering).
3. **Act with the most specific tool** (not the generic escape hatch — see Safety). Locate
   timeline items by `track_type` (video/audio/subtitle) + 1-based `track_index` + 0-based
   `item_index`.
4. **Verify** by re-reading state or a `screenshot`, and relay the tool's own result string.

### Tool map by task (exact names/params in `reference/tool-catalog.md`)
- **Navigate/inspect**: `open_page`, `get_current_page`, `get_project_info`, layout presets,
  keyframe mode → `tools/resolve_app.py`, `tools/project.py`.
- **Projects (lifecycle)**: create/load/save/close/delete, databases, import/export →
  `tools/project_manager.py`.
- **Media**: volumes + import (`tools/media_storage.py`); bins, import, move/delete/relink,
  create timelines (`tools/media_pool.py`); per-clip props/metadata/markers/flags/color/
  proxies (`tools/media_pool_item.py`).
- **Timeline (read)**: list/switch/duplicate, settings, tracks, timecode, item listing →
  `tools/timeline.py`. **Timeline (edit)**: markers, inserts (`insert_*`), compound clips,
  `detect_scene_cuts` → `tools/timeline_edit.py`. **Per-item** props/transforms/takes →
  `tools/timeline_item.py`.
- **Color**: node graph, `set_lut`, `set_cdl`, color versions, gallery `grab_still` →
  `tools/color.py` (color page). **Fusion**: comp management + `create_fusion_clip` →
  `tools/fusion.py` (deep node work: `execute_resolve_code`).
- **Audio/Fairlight**: voice isolation, audio tracks → `tools/audio.py`.
- **AI/Neural Engine** (Studio): Magic Mask, Smart Reframe, Stabilize,
  `create_subtitles_from_audio` → `tools/ai.py`.
- **Render/Deliver**: formats/codecs/presets, settings, `add_render_job`/`start_rendering`/
  `get_render_job_status`/`stop_rendering` → `tools/render.py`.
- **Export/stills**: `export_timeline`, `export_current_frame`, `get_current_thumbnail` →
  `tools/export_still.py`.
- **Transcription (local, no Studio)**: `transcribe_audio`, `transcribe_and_add_subtitles`,
  `export_srt`, `list_whisper_models` → `tools/transcription.py` (needs Whisper + ffmpeg).
- **Uncovered API**: `execute_resolve_code` → `tools/code.py`.

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

## Quick recipes
- **Subtitles from audio**: active timeline → `create_subtitles_from_audio` (Studio) or
  local `transcribe_and_add_subtitles` / `export_srt` then import as a subtitle track.
- **Vertical (9:16) reframe**: `smart_reframe` (Studio) or `set_timeline_item_property`
  (Pan/Tilt/ZoomX/ZoomY) on the item.
- **Grade a clip**: `open_page('color')` → `get_node_graph` → `set_cdl`/`set_lut` →
  `grab_still`.
- **Render H.264**: `set_render_settings` (format `mp4`, codec `H.264`, `TargetDir`,
  `CustomName`) → `add_render_job` → `start_rendering` → poll `get_render_job_status`.
- **"What's on the timeline?"**: `get_current_timeline_info` + `get_timeline_items` +
  `screenshot`.
