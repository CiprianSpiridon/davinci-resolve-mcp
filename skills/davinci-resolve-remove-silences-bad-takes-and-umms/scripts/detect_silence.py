#!/usr/bin/env python3
"""Phase 3 — detect true silence from the audio waveform with ffmpeg.

Waveform silence (not caption gaps): this ignores humming/music/room-tone that
IS audible, and catches silence that falls *inside* a caption. Output is a list
of [start_frame, end_frame] intervals at the timeline fps, written as JSON for
plan_cuts.py to consume.

Usage:
    python3 detect_silence.py \
        --source "/path/to/source.mp4" \
        --fps 30 \
        --noise-floor -30 \
        --min-silence 0.7 \
        --out /path/to/silences.json

Requires: ffmpeg on PATH. Does not touch DaVinci Resolve.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys


def detect(source: str, noise_db: float, min_silence: float) -> list[tuple[float, float]]:
    """Run ffmpeg silencedetect and return (start_s, end_s) pairs."""
    cmd = [
        "ffmpeg", "-nostats", "-hide_banner", "-vn", "-i", source,
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
        "-f", "null", "-",
    ]
    # silencedetect writes to stderr; -f null discards the media.
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 and "silence_end" not in proc.stderr:
        sys.exit(f"ffmpeg failed:\n{proc.stderr[-2000:]}")
    log = proc.stderr
    starts = [float(x) for x in re.findall(r"silence_start:\s*([0-9.]+)", log)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([0-9.]+)", log)]
    # Pair sequentially; a trailing unmatched start (file ends in silence) is dropped.
    return list(zip(starts, ends))


def main() -> None:
    ap = argparse.ArgumentParser(description="Detect silence intervals with ffmpeg.")
    ap.add_argument("--source", required=True, help="absolute path to the source A/V file")
    ap.add_argument("--fps", type=float, required=True, help="timeline fps (match the source fps)")
    ap.add_argument("--noise-floor", type=float, default=-30.0,
                    help="dB below which is 'silent' (default -30; lower = stricter)")
    ap.add_argument("--min-silence", type=float, default=0.7,
                    help="minimum silence seconds to report (default 0.7 = talking-head standard)")
    ap.add_argument("--out", required=True, help="output JSON path for [start_frame,end_frame] list")
    args = ap.parse_args()

    pairs = detect(args.source, args.noise_floor, args.min_silence)
    frames = [[round(s * args.fps), round(e * args.fps)] for s, e in pairs]
    with open(args.out, "w") as f:
        json.dump(frames, f)

    total = sum(e - s for s, e in pairs)
    print(json.dumps({
        "events": len(frames),
        "total_silence_seconds": round(total, 1),
        "total_silence_mmss": f"{int(total // 60):02d}:{int(total % 60):02d}",
        "out": args.out,
    }, indent=2))


if __name__ == "__main__":
    main()
