# Cookbook — task recipes

The primary how-to. Each recipe is orient → act → verify with the **exact** live tool names.
Every timeline-item tool locates the clip by `track_type` (video/audio/subtitle) +
1-based `track_index` + 0-based `item_index`. Every tool returns a plain string; on failure
it returns `"Error: ..."` — read it, don't assume success.

---

## Reframe / transform a clip (pan, zoom, rotate, flip)
Inspector Transform. No page switch needed.
1. Orient: `get_current_timeline_info`, `get_timeline_items`, `screenshot`.
2. Act: `set_transform(pan=, tilt=, zoom_x=, zoom_y=, zoom_gang=, rotation_angle=,
   anchor_point_x=, anchor_point_y=, flip_x=, flip_y=, track_type, track_index, item_index)`.
   Reset with `reset_transform`. Read current values with `get_inspector_properties`.
3. Verify: `screenshot` + re-read `get_timeline_item_properties`.

**Vertical (9:16) reframe:** `smart_reframe` (Studio, auto) — or `set_transform`
(`zoom_x`/`zoom_y` up, `pan` to recompose) for manual, non-Studio.

## Crop / dynamic zoom
- Crop: `set_cropping(crop_left=, crop_right=, crop_top=, crop_bottom=, crop_softness=,
  crop_retain=, ...)`.
- Ken-Burns push: `set_dynamic_zoom(...)`.
- Speed/retime: `set_retime_and_scaling(...)`. Composite/blend: `set_composite(...)`.

## Keyframe a move
1. `add_transform_keyframe(frame=, pan=/zoom_x=/…, track_type, track_index, item_index)`
   at each hold point. 2. Inspect with `get_transform_keyframes`. 3. Remove with
   `delete_transform_keyframe`. (Fusion-input keyframes: `fusion_add_keyframe`.)

## Apply an effect (ResolveFX / OFX) to a clip
1. Discover the plugin's `regid`: `enumerate_ofx` or `get_resolvefx_registry`
   (`discover_regid` to resolve a friendly name).
2. Act: `apply_ofx_to_clip(regid, params='{"Param":value}', track_type, track_index,
   item_index)`. The node is created in the clip's Fusion comp and spliced before
   `MediaOut1`; the `ofx.` prefix is kept.
3. Verify: `screenshot`.

## Apply a template (Fusion title / generator / transition template)
1. `enumerate_templates` to list installed templates by category.
2. `insert_template_by_name(...)` or `append_template_with_placement(...)`.
   Attach a saved comp with `attach_fusion_comp`.

## Build or edit a title
- Simple insert: `insert_title` / `insert_fusion_title` (timeline_edit) at a position.
- Edit text on an existing Text+/title comp: `set_title_text(text=, size=, center_x=,
  center_y=, comp_index=1, track_type, track_index, item_index)`.
- Author from scratch in the comp: `fusion_add_tool("TextPlus")` →
  `fusion_set_input` → `fusion_connect_input`. See **fusion-tools.md** for the node table.

## Transition at a cut
- **Live timeline:** `add_default_transition_at_cut(...)` (video),
  `add_default_audio_transition_at_cut(...)` (audio). The live API has **no transition
  object** to address directly — this drives the default-transition action.
- **Offline file:** `place_transition(file_path=<.drt/.drp>, track=, at_frame=,
  duration_frames=, transition_type='cross-dissolve', track_type='video',
  output_path=)`. Offline writes return `"verified": false` — warn the user.

## Grade a clip
1. `open_page('color')`. 2. Orient: `get_node_graph`, `get_num_nodes`
   (node graphs are **read-only** to enumerate; you set values, not topology).
3. Act: `set_cdl(...)`, `set_lut(...)`, or `apply_grade_from_drx(...)` to load a `.drx`.
   Color versions: `add_color_version` / `load_color_version`.
4. Verify: `grab_still` (Gallery panel must be visible), `screenshot`.

## Render / deliver
1. `open_page('deliver')`. 2. `set_render_format_and_codec(...)` then
   `set_render_settings(TargetDir=, CustomName=, ...)` — or `load_render_preset`.
   H.264: format `mp4`, codec `H.264`.
3. `add_render_job` → `start_rendering` → poll `get_render_job_status` /
   `is_rendering`; `stop_rendering` cancels. Fast path: `quick_export`.
Confirm before overwriting an existing `TargetDir`/`CustomName`.

## Subtitles / transcription
- Studio, on the Neural Engine: `create_subtitles_from_audio` on the active timeline;
  clear with `clear_subtitles`.
- No Studio (local Whisper + ffmpeg): `transcribe_and_add_subtitles`, or
  `transcribe_audio` then `export_srt` and import as a subtitle track.
  Models: `list_whisper_models`.

## Markers
- Timeline: `add_marker` / `delete_marker_at_frame`.
- Timeline item: `add_item_marker` / `delete_item_marker_at_frame`.
- Media-pool clip: `add_clip_marker`. Colors are a fixed vocabulary — a rejected color
  returns the valid list (marker colors differ from clip colors).

## "What's on the timeline?"
`get_current_timeline_info` + `get_timeline_items` + `screenshot`. Read tracks with
`get_track_count` / `get_track_name`; item detail with `get_timeline_item_properties`.
