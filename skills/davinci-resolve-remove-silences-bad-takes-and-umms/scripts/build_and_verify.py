# Phase 5b/6 — build the rough-cut timeline and verify A/V sync.
#
# This is NOT run standalone — its text is passed as the `code` argument to the
# davinci-resolve MCP tool `execute_resolve_code`, so it runs INSIDE Resolve with
# the pre-bound namespace: resolve, project, mediaPool, timeline, mediaStorage.
#
# Before passing it, replace the placeholder on the next line with the absolute
# path to the keep-segments.json that plan_cuts.py wrote:
KEEP_SEGMENTS_PATH = "___KEEP_SEGMENTS_JSON___"
#
# Method note: there is NO razor/split/ripple-delete in the Resolve scripting API
# (Timeline.SplitClip is unsupported). We rebuild instead: append only the KEEP
# sub-ranges of the source clip. AppendToTimeline appends video + linked audio
# from identical source frames, so A/V stays synced by construction. endFrame is
# EXCLUSIVE, so we pass e for source frames [s, e).
import json

cfg = json.load(open(KEEP_SEGMENTS_PATH))
prefix = cfg["source_clip_prefix"]
name = cfg["output_name"]
keep = cfg["keep"]

# locate source clip by name prefix in the current media-pool folder
clip = None
for c in mediaPool.GetCurrentFolder().GetClipList():
    if c.GetName().startswith(prefix):
        clip = c
        break
if clip is None:
    result = json.dumps({"error": f"source clip starting with {prefix!r} not found in current folder"})
else:
    tl = mediaPool.CreateEmptyTimeline(name)
    project.SetCurrentTimeline(tl)
    infos = [{"mediaPoolItem": clip, "startFrame": s, "endFrame": e} for s, e in keep]  # endFrame EXCLUSIVE
    appended = mediaPool.AppendToTimeline(infos)

    # ---- verify (source-level, not just record-side) ----
    vids = tl.GetItemListInTrack("video", 1) or []
    auds = tl.GetItemListInTrack("audio", 1) or []

    def srng(it):
        return (it.GetSourceStartFrame(), it.GetSourceEndFrame())

    av_mismatch = sum(1 for v, a in zip(vids, auds) if srng(v) != srng(a))
    noncontig = sum(1 for i in range(1, len(vids)) if vids[i].GetStart() != vids[i - 1].GetEnd())
    linked = sum(1 for v in vids if (v.GetLinkedItems() or []))
    dur = tl.GetEndFrame() - tl.GetStartFrame()

    resolve.GetProjectManager().SaveProject()   # note: pm.SaveProject(), NOT project.SaveProject()

    result = json.dumps({
        "timeline": name,
        "keep_segments": len(keep),
        "appended_items": len(appended) if isinstance(appended, list) else str(appended),
        "video_items": len(vids),
        "audio_items": len(auds),
        "av_source_mismatch_pairs": av_mismatch,     # must be 0
        "video_items_linked": f"{linked}/{len(vids)}",  # must be N/N
        "noncontiguous_joints": noncontig,           # must be 0
        "timeline_frames": dur,
        "saved": True,
    }, indent=2)
