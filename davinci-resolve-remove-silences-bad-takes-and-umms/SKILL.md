---
name: davinci-resolve-remove-silences-bad-takes-and-umms
version: 1.0.0
description: >-
  First-pass editorial hygiene for a DaVinci Resolve timeline: remove dead-air silences,
  filler-only cues (um/uh/okay/so), non-speech markers, and verbatim retakes / false starts —
  always non-destructively (the master and every prior version stay intact), then verify A/V
  sync. Uses ffmpeg waveform silence detection + Resolve Studio's native subtitle transcription
  + a cheap subagent to classify fillers/bad-takes, and rebuilds the cut via AppendToTimeline
  (there is no razor/ripple tool in the scripting API). Use when the user asks to "rough cut",
  "tighten", "clean up", "remove dead air / pauses / silences / umms / filler words / retakes /
  false starts / bad takes" on a video or timeline in DaVinci Resolve.
when_to_use: >-
  Trigger when the user wants a first editorial-hygiene pass on a Resolve clip/timeline — remove
  silences, filler words (um/uh), interjections, and bad takes / false starts. Requires DaVinci
  Resolve Studio 19+ (native transcription) and ffmpeg. Builds a new timeline; never edits the
  original. For general Resolve operation see the base `davinci-resolve` skill.
argument-hint: "[source .mp4/.mov or open timeline] [optional: aggressive | keep long demos]"
---

# Remove silences, bad takes & umms (DaVinci Resolve)

A non-destructive first-pass editorial cleaner. It **rebuilds** a tightened timeline from the
keep-ranges of the source (the Resolve scripting API has **no razor/ripple** tool — see
[`reference/gotchas.md`](./reference/gotchas.md) #2), so the original is never touched and
video+audio stay linked and source-synced by construction.

This is a focused composition on top of the base **`davinci-resolve`** MCP skill — read that
for general tool usage; read this only for the cleanup pipeline.

## What it removes
- **Silences** — from the audio **waveform** (ffmpeg `silencedetect`), not caption gaps, so it
  ignores audible humming/music and catches silence inside a caption.
- **Filler-only cues & interjections** — whole captions that are just "Um."/"Okay."/"So."/etc.
- **Non-speech markers** — `[silence]`, `[no audio]`, `[humming]`, `[ Pause ]`.
- **Bad takes / false starts** — an abandoned phrase that the speaker immediately restates.

## Honest limit (state it to the user)
Resolve subtitles are **phrase-level**, so a mid-sentence "um" embedded in an otherwise-good
sentence **cannot** be excised — only *whole* filler/bad-take captions can. Standalone fillers
are caught; embedded ones remain. (Word-level excision would need a word-timed transcript;
out of scope here since the native path is phrase-level.)

## Requirements
- DaVinci Resolve **Studio 19+**, running, project open, External scripting = Local.
- **ffmpeg** on PATH.
- The base `davinci-resolve` MCP server configured (tools `mcp__davinci-resolve__*`).

## Workflow (full runbook: [`reference/pipeline.md`](./reference/pipeline.md))
1. **Preflight & fps match** — read the source fps/frames; set the project timeline fps to the
   source fps *before any timeline exists* so timeline-frame == source-frame 1:1.
2. **Master** — build an untouched `FULL master` reference timeline.
3. **Transcript** — duplicate → `create_subtitles_from_audio` (native) → write `transcript.tsv`.
4. **Silence** — `scripts/detect_silence.py` → `silences.json`.
5. **Classify** — dispatch a **Haiku** subagent with `scripts/classify_prompt.md` → `removals.json`.
6. **Plan (review/veto gate)** — `scripts/plan_cuts.py` → `keep-segments.json` + `report.json`;
   show the report. **Build** — `scripts/build_and_verify.py` via `execute_resolve_code`.
7. **Verify & report** — require `av_source_mismatch_pairs == 0`, all items linked, contiguous;
   report removed-by-category with timecodes.

## Parameters (defaults follow talking-head best practice)
| Flag (on `plan_cuts.py` / `detect_silence.py`) | Default | Meaning |
|---|---|---|
| `--min-silence` | `0.7`s | shorter pauses are natural rhythm — kept |
| `--noise-floor` | `-30` dB | silence threshold |
| `--handle` | `8` frames (~0.25s) | pause kept at each cut edge so speech isn't clipped |
| `--long-threshold` | `12`s | silences this long are "process/demo waits" |
| `--long-mode` | `cut` | `cut` removes them; **`protect`** leaves them for you to speed-ramp |

## Guardrails
- **Non-destructive, always.** New versioned timeline; master + prior versions untouched.
- **Review/veto gate.** Present `report.json` (long-silence list + caption cuts with timecodes)
  before/after building so the user can veto — long silences on a screencast may be live demos.
- **Verify source-level sync** (not record-side); relay `Error:` strings, don't fake success.
- **No Whisper/mlx** — native `create_subtitles_from_audio` only.
- See [`reference/gotchas.md`](./reference/gotchas.md) for the frame-rate, `endFrame`-exclusive,
  linkage-proxy, and `pm.SaveProject()` traps.

## Out of scope (the user handles these)
Speed-ramp / timelapse of long process waits (`--long-mode protect` hands them over intact),
cross-dissolves at joins, J/L cuts, B-roll / punch-ins to hide jump cuts.

## Files
- [`reference/pipeline.md`](./reference/pipeline.md) — phase-by-phase with exact tool calls.
- [`reference/gotchas.md`](./reference/gotchas.md) — the invariants; read once before running.
- `scripts/detect_silence.py` · `scripts/plan_cuts.py` (pure/testable) ·
  `scripts/build_and_verify.py` (runs inside Resolve) · `scripts/classify_prompt.md`.
