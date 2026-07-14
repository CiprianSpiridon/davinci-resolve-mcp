# Invariants & gotchas (hard-won — do not skip)

Every one of these cost a debugging cycle the first time. The scripts encode
them; this file explains *why* so you don't "fix" them back into bugs.

## 1. Match the timeline fps to the SOURCE fps — first, before any timeline exists
The source here was 30 fps but the project defaulted to 24 fps. On a mismatched
timeline Resolve conforms/retimes the clip, which breaks frame-accurate cutting
**and** A/V sync. Set `timelineFrameRate` to the source fps while
`timeline_count == 0` (it locks once a timeline is created), so **timeline frame N
== source frame N** and one transcript-second == fps frames exactly.
`set_project_setting("timelineFrameRate", "<fps>")`.

## 2. There is NO razor / split / ripple-delete tool — rebuild instead
The Resolve **scripting API has no** `SplitClip`/`RazorClip`/`RippleDelete`
"cut and delete" on a timeline via script. The pipeline **rebuilds**: it appends
only the KEEP sub-ranges of the source to a fresh timeline via
`mediaPool.AppendToTimeline([{mediaPoolItem, startFrame, endFrame}, ...])`. This
is non-destructive and keeps A/V linked + source-synced by construction. The
tradeoff is hard butt-joins (no transitions) — add dissolves/ramps afterward.

## 3. `AppendToTimeline` `endFrame` is EXCLUSIVE
For source frames `[s, e)` (last kept frame `e-1`) pass `endFrame = e`, **not**
`e-1`. Passing `e-1` makes every segment 1 frame short (N frames total across N
segments). Verified by comparing built duration to the plan.

## 4. Verify SOURCE-level sync, not record-side
Matching timeline positions (`GetStart`/`GetDuration`) does not prove the audio
*content* matches the video. Compare `GetSourceStartFrame()`/`GetSourceEndFrame()`
of each video item vs its audio counterpart — that must be identical. Expect
`av_source_mismatch_pairs == 0`.

## 5. Linkage check: don't use `item in list`
Resolve returns a **new proxy object** each call, and these don't compare by
value — `video_item in audio_list` is a false negative. Check linkage via
`len(v.GetLinkedItems()) >= 1` (and, if needed, compare by `GetStart()`), not `in`.

## 6. Save via the ProjectManager, not the Project
`Project` has no `SaveProject` — `project.SaveProject()` raises
`'NoneType' object is not callable`. Use `resolve.GetProjectManager().SaveProject()`.

## 7. Silence = waveform, not caption gaps
Caption gaps mislabel audible non-speech (humming, music) as removable and miss
silence *inside* a caption. ffmpeg `silencedetect` on the waveform is accurate.
Conversely, an audible `[humming]` stretch is NOT silent — ffmpeg won't catch it;
that's why the transcript pass also removes non-speech *marker* captions.

## 8. Phrase-level granularity — the honest limit
Resolve subtitles are phrase-level, so a mid-sentence "um" isn't individually
timestamped. This pipeline removes **whole** filler/interjection captions and
**whole** bad-take captions, plus silence — it cannot excise an "um" embedded in
an otherwise-good sentence. Say so; don't imply word-surgical filler removal.

## 9. Transcription is native (Studio Neural Engine), no Whisper
Use `create_subtitles_from_audio` (Resolve Studio 19+). Do not install
mlx-whisper / openai-whisper — the native path is the requirement here.
Free edition / no Studio: this pipeline's transcript step won't run.

## 10. Big transcripts blow context — write to a file
1000+ captions returned inline overflow the model. Write `transcript.tsv` to
disk from inside Resolve, then let scripts + a subagent read it. Same for the
removal list (`removals.json`).
