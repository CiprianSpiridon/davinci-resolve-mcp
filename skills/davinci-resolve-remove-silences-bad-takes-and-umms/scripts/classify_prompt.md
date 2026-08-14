# Phase 4 — transcript classification subagent prompt

Dispatch this with the **Agent** tool, `model: haiku`, `subagent_type: general-purpose`.
It reads the full transcript and returns a removal list. Substitute the two
`{{...}}` placeholders. Bulk semantic classification on a cheap model keeps the
orchestrator's context clean and the file-based output avoids ingesting 1000+ lines.

---

You are doing first-pass editorial hygiene on a video transcript for a DaVinci Resolve rough cut. Your job: read the FULL transcript and identify which captions should be REMOVED because they are dead / disfluent / redundant, not substantive content.

INPUT FILE (tab-separated, read ALL rows in chunks):
{{TRANSCRIPT_TSV_PATH}}
Columns: idx <TAB> start_frame <TAB> end_frame <TAB> mm:ss <TAB> text

The video is: {{VIDEO_DESCRIPTION}}

Classify captions into these REMOVE categories:
1. "nonspeech" — bracketed non-speech markers the transcriber inserted: [silence], [no audio], [humming], [ Pause ], [music], etc. Remove the whole caption.
2. "filler" — a caption whose ENTIRE content is filler/interjection with no information: standalone "Okay.", "So.", "Um.", "Uh.", "Right.", "You know?", "Well,", "Yeah.", "I mean." etc. Only if the WHOLE caption is filler — do NOT remove a caption that merely STARTS with "So"/"Okay" then says something substantive.
3. "badtake" — a false start / restart / self-correction the speaker abandons then redoes. Signals: an aborted phrase immediately followed by the speaker restating the same idea; "sorry", "let me redo/start over/rephrase", "actually, no", "wait,", "scratch that", "hold on" followed by a redo. Remove the ABANDONED take, keep the good one. Judge by reading the SURROUNDING captions.
4. "stutter" — an immediate word/phrase repetition that's a verbal stumble ("the the", "I I want"). Only clear stumbles, NOT rhetorical emphasis like "very, very specific" (keep that).

RULES:
- Be CONSERVATIVE. When in doubt, KEEP. Removing real content is worse than leaving a minor filler.
- Whole captions only — you cannot cut individual words mid-caption.
- Judge bad takes from context (captions before/after), not a single line.

OUTPUT — do BOTH:
(A) Write JSON to this exact path:
{{REMOVALS_JSON_PATH}}
Shape (sort "remove" by idx ascending):
{"remove":[{"idx":<int>,"cat":"nonspeech|filler|badtake|stutter","reason":"<short>"}, ...],
 "uncertain":[{"idx":<int>,"note":"<short>"}, ...]}
(B) Return ONLY a compact summary: total captions read, counts per category, count uncertain, and up to 5 example removals per category (idx + text). Confirm you read every row. Do NOT paste the whole list — it's in the file.

If you had to stop before the last row, say so explicitly.
