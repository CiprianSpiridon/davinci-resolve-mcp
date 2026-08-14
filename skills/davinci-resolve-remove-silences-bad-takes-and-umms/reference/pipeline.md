# Pipeline runbook — exact steps

Orchestration is model-driven (this doc); the deterministic math and the
Resolve build live in `scripts/`. `$SKILL` = this skill's folder, `$WORK` = a
scratch dir for intermediate files (use the session scratchpad). All frame
numbers are timeline frames == source frames (Phase 0 guarantees the 1:1 map).

Read [`gotchas.md`](./gotchas.md) once before running.

## Phase 0 — Preflight & fps match
1. `get_project_info` → confirm a project is open and note `current_page`; confirm the
   version is **Studio 19+** (subtitles-from-audio needs it).
2. Ensure the source clip is in the media pool (`get_media_pool_structure`); if not,
   `import_media([source])`. Record the clip **name prefix** and absolute **source path**.
3. Read the clip's true fps + frame count:
   `execute_resolve_code` → `GetClipProperty("FPS")`, `GetClipProperty("Frames")`.
   Let `FPS`, `CLIP_END = Frames`.
4. **Match the timeline fps to the source** (only sticks while `timeline_count == 0`):
   `set_project_setting("timelineFrameRate", str(FPS))`. Verify it took.

## Phase 1 — Untouched master
`create_timeline_from_clips("<proj> — FULL master (do not edit)", [clip_name])`.
Never edit this; it's the reference. Confirm with `get_current_timeline_info`
(fps == FPS, end_frame == CLIP_END).

## Phase 2 — Transcript (native)
1. `set_current_timeline(master)` → `duplicate_timeline("<proj> — work (subs)")` →
   `set_current_timeline(work)`.
2. `create_subtitles_from_audio(language="english")` (Studio Neural Engine).
3. Write the transcript to disk (avoids context overflow) via `execute_resolve_code`:
   iterate `GetItemListInTrack("subtitle", 1)`, write
   `idx \t GetStart() \t GetEnd() \t mm:ss \t GetName()` to `$WORK/transcript.tsv`.
   Report the caption count.

## Phase 3 — Silence (ffmpeg)
```
python3 $SKILL/scripts/detect_silence.py \
  --source "<abs source path>" --fps FPS \
  --noise-floor -30 --min-silence 0.7 \
  --out $WORK/silences.json
```
Review the printed total. `--min-silence 0.7` is the talking-head default; lower
it only if the speaker's natural pauses run long.

## Phase 4 — Classify fillers / bad takes (Haiku subagent)
Fill the placeholders in [`../scripts/classify_prompt.md`](../scripts/classify_prompt.md)
(`{{TRANSCRIPT_TSV_PATH}}` = `$WORK/transcript.tsv`, `{{REMOVALS_JSON_PATH}}` =
`$WORK/removals.json`, `{{VIDEO_DESCRIPTION}}` = one line about the video) and dispatch:
**Agent** tool, `model: haiku`, `subagent_type: general-purpose`. It writes
`$WORK/removals.json` and returns a compact summary.
**Cross-check** a few returned indices against `transcript.tsv` (the summary's idx must
match the actual caption text) before trusting them.

## Phase 5 — Plan + build
1. Plan (pure, testable):
```
python3 $SKILL/scripts/plan_cuts.py \
  --silences $WORK/silences.json --removals $WORK/removals.json \
  --transcript $WORK/transcript.tsv --clip-end CLIP_END --fps FPS \
  --source-clip-name "<clip name prefix>" \
  --output-name "<proj> — rough cut v1 (silences+fillers+badtakes)" \
  --handle 8 --min-cut 6 --long-threshold 12 --long-mode cut \
  --out $WORK/keep-segments.json --report $WORK/report.json
```
   Show the user `report.json` (removed total + long-silence list + caption cuts).
   **This is the review/veto gate.** For a `--dry-run` feel, stop here.
2. Build + verify: take `$SKILL/scripts/build_and_verify.py`, replace
   `___KEEP_SEGMENTS_JSON___` with `$WORK/keep-segments.json`, and pass the whole
   text as `code` to `execute_resolve_code`.

## Phase 6 — Verify & report
The build payload returns the verification block. Require:
`av_source_mismatch_pairs == 0`, `video_items_linked == N/N`,
`noncontiguous_joints == 0`. It already saved the project.
Report to the user: final length, removed-by-category (from `report.json`), and the
timecoded veto lists. Master + any prior versions remain untouched.

## Re-runs & tuning
Deterministic given the same inputs → to retune, re-run Phase 5 with different
`--handle` / `--min-silence` (needs a fresh Phase 3) / `--long-mode` and a new
`--output-name`. Each run makes a new timeline; nothing is overwritten.

## Out of scope (user handles)
Speed-ramping / timelapsing the long "process waits" (`--long-mode protect` leaves
them intact for you), cross-dissolves at joins, J/L cuts, B-roll/punch-ins.
