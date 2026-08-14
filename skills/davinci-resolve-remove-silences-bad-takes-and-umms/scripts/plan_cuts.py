#!/usr/bin/env python3
"""Phase 5a — the deterministic cut planner (pure, testable, no Resolve).

Turns the silence intervals + the subagent's caption removals into the final
list of KEEP segments (source-frame ranges) the timeline will be rebuilt from,
plus a human-readable report with timecodes so the user can veto specific cuts.

This is intentionally a plain, reviewed function rather than something the model
re-derives each run — the interval math and the handle logic are where an
improvised version drifts by a frame.

Inputs (all frame numbers are timeline frames == source frames, 1:1, because
the pipeline sets the timeline fps to the source fps first):
    --silences   silences.json     : [[start_frame,end_frame], ...]  (from detect_silence.py)
    --removals   removals.json      : {"remove":[{"idx","cat","reason"}], "uncertain":[...]}  (subagent)
    --transcript transcript.tsv     : idx <TAB> start_f <TAB> end_f <TAB> mm:ss <TAB> text
    --clip-end   int                : total source frame count
    --source-clip-name STR          : media-pool clip name prefix (for the build step)
    --output-name STR               : name for the new timeline

Tunables (best-practice defaults):
    --handle 8            frames kept inside each silence edge (~0.25s @30fps) so speech isn't clipped
    --min-cut 6           only trim a silence if >= this many frames are removable after handles
    --long-threshold 12   seconds; silences this long are "demo/process waits"
    --long-mode cut|protect   what to do with long silences (default cut). 'protect' leaves them
                              intact so you can speed-ramp/timelapse them yourself.

Outputs:
    --out       keep-segments.json  : {"source_clip_prefix","output_name","clip_end",
                                       "endFrame_exclusive":true,"keep":[[s,e],...]}
    --report    report.json (optional; also printed): removed totals + per-category veto lists
"""
from __future__ import annotations

import argparse
import json


def mmss(frames: float, fps: float) -> str:
    s = frames / fps
    return f"{int(s // 60):02d}:{int(s % 60):02d}"


def load_captions(path: str) -> dict[int, dict]:
    rows: dict[int, dict] = {}
    with open(path) as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) >= 5:
                rows[int(p[0])] = {"s": int(p[1]), "e": int(p[2]), "tc": p[3], "t": p[4]}
    return rows


def merge(intervals: list[list[int]]) -> list[list[int]]:
    intervals = sorted([iv for iv in intervals if iv[1] > iv[0]])
    out: list[list[int]] = []
    for a, b in intervals:
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def plan(silences, removals, caps, clip_end, fps, handle, min_cut, long_thresh_f, long_mode):
    remove: list[list[int]] = []
    long_silences = []          # for the veto report
    silence_removed_raw = 0

    for s, e in silences:
        dur = e - s
        is_long = dur >= long_thresh_f
        if is_long:
            long_silences.append({"tc": mmss(s, fps), "dur_s": round(dur / fps, 1),
                                   "action": "protected" if long_mode == "protect" else "cut"})
            if long_mode == "protect":
                continue
        if dur > 2 * handle + min_cut:
            a, b = s + handle, e - handle
            remove.append([a, b])
            silence_removed_raw += b - a

    caption_cuts = []           # for the veto report
    for r in removals.get("remove", []):
        i = r["idx"]
        if i not in caps:
            continue
        c = caps[i]
        remove.append([c["s"], c["e"]])
        caption_cuts.append({"idx": i, "cat": r.get("cat", ""), "tc": c["tc"],
                             "text": c["t"][:80], "reason": r.get("reason", "")})

    remove = [[max(0, a), min(clip_end, b)] for a, b in remove]
    merged = merge(remove)

    # keep = complement; drop sub-3-frame slivers
    keep: list[list[int]] = []
    cur = 0
    for a, b in merged:
        if a > cur:
            keep.append([cur, a])
        cur = max(cur, b)
    if cur < clip_end:
        keep.append([cur, clip_end])
    keep = [seg for seg in keep if seg[1] - seg[0] >= 3]

    kept = sum(e - s for s, e in keep)
    report = {
        "orig_mmss": mmss(clip_end, fps),
        "kept_mmss": mmss(kept, fps),
        "removed_mmss": mmss(clip_end - kept, fps),
        "keep_segments": len(keep),
        "silence_removed_raw_mmss": mmss(silence_removed_raw, fps),
        "dead_captions_removed": len(caption_cuts),
        "caption_cuts": sorted(caption_cuts, key=lambda x: x["idx"]),
        "long_silences": sorted(long_silences, key=lambda x: -x["dur_s"]),
        "uncertain_kept": removals.get("uncertain", []),
    }
    return keep, report


def main() -> None:
    ap = argparse.ArgumentParser(description="Plan keep-segments from silences + caption removals.")
    ap.add_argument("--silences", required=True)
    ap.add_argument("--removals", required=True)
    ap.add_argument("--transcript", required=True)
    ap.add_argument("--clip-end", type=int, required=True)
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--source-clip-name", required=True)
    ap.add_argument("--output-name", required=True)
    ap.add_argument("--handle", type=int, default=8)
    ap.add_argument("--min-cut", type=int, default=6)
    ap.add_argument("--long-threshold", type=float, default=12.0)
    ap.add_argument("--long-mode", choices=["cut", "protect"], default="cut")
    ap.add_argument("--out", required=True)
    ap.add_argument("--report", default="")
    args = ap.parse_args()

    silences = json.load(open(args.silences))
    removals = json.load(open(args.removals))
    caps = load_captions(args.transcript)
    long_thresh_f = int(args.long_threshold * args.fps)

    keep, report = plan(silences, removals, caps, args.clip_end, args.fps,
                        args.handle, args.min_cut, long_thresh_f, args.long_mode)

    with open(args.out, "w") as f:
        json.dump({
            "source_clip_prefix": args.source_clip_name,
            "output_name": args.output_name,
            "clip_end": args.clip_end,
            "endFrame_exclusive": True,   # AppendToTimeline endFrame is EXCLUSIVE — build passes e, not e-1
            "keep": keep,
        }, f)
    if args.report:
        json.dump(report, open(args.report, "w"), indent=2)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
