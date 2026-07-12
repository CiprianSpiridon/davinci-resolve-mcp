# DaVinci Resolve MCP — full tool reference

Auto-generated from `mcp.list_tools()` — **208 tools** across 18 live domain modules (190 tools) and 18 offline (no-Resolve) modules (18 tools). First sentence of each tool's docstring shown.

## Live tools (drive a running DaVinci Resolve instance)

### `tools/media_pool_item.py` (20)
- **add_clip_flag** — Add a flag of the given color to a media pool clip
- **add_clip_marker** — Add a marker to a media pool clip
- **clear_clip_color** — Clear the clip color of a media pool clip
- **clear_clip_flags** — Clear flags of a given color (or all flags) from a media pool clip
- **clear_clip_mark_in_out** — Clear the mark in/out points of a media pool clip
- **delete_clip_markers_by_color** — Delete all markers of a given color from a media pool clip
- **get_clip_color** — Get the clip color (as shown in the media pool UI) of a media pool clip
- **get_clip_flags** — Get the list of flag colors set on a media pool clip, as JSON
- **get_clip_mark_in_out** — Get the mark in/out points of a media pool clip, as JSON
- **get_clip_markers** — Get all markers on a media pool clip, as JSON keyed by frame number
- **get_clip_metadata** — Get one or all metadata fields of a media pool clip, as JSON
- **get_clip_property** — Get one or all properties of a media pool clip, as JSON
- **link_proxy_media** — Link a proxy media file to a media pool clip
- **replace_clip** — Replace a media pool clip's underlying source media with another file
- **set_clip_color** — Set the clip color (as shown in the media pool UI) of a media pool clip
- **set_clip_mark_in_out** — Set the mark in/out points of a media pool clip
- **set_clip_metadata** — Set a metadata field on a media pool clip
- **set_clip_property** — Set a property on a media pool clip
- **unlink_proxy_media** — Unlink the proxy media file from a media pool clip
- **update_clip_marker_custom_data** — Update the custom data string of an existing marker on a media pool clip

### `tools/timeline.py` (20)
- **add_track** — Add a new track of the given type to the current timeline
- **delete_track** — Delete a specific track from the current timeline
- **duplicate_timeline** — Duplicate the current timeline, returning info about the new one
- **get_current_timecode** — Get the current playhead timecode on the current timeline
- **get_current_timeline_info** — Get detailed information about the current timeline, as JSON
- **get_end_timecode** — Get the timecode of the last frame of the current timeline
- **get_start_timecode** — Get the start timecode of the current timeline (its frame-0 timecode)
- **get_timeline_items** — List all clips/items on a specific track of the current timeline, as JSON
- **get_timeline_list** — List every timeline in the current project, as JSON
- **get_timeline_setting** — Get one or all settings on the current timeline, as JSON
- **get_track_count** — Get the number of tracks of a given type on the current timeline
- **get_track_enable** — Check whether a specific track on the current timeline is enabled
- **get_track_lock** — Check whether a specific track on the current timeline is locked
- **get_track_name** — Get the name of a specific track on the current timeline
- **set_current_timecode** — Move the playhead on the current timeline to a specific timecode
- **set_current_timeline** — Make the named timeline the current (active) timeline
- **set_timeline_setting** — Set a single named setting on the current timeline
- **set_track_enable** — Enable or disable a specific track on the current timeline
- **set_track_lock** — Lock or unlock a specific track on the current timeline
- **set_track_name** — Rename a specific track on the current timeline

### `tools/project_manager.py` (17)
- **close_project** — Close the currently open project
- **create_project** — Create a new project in the ProjectManager's current folder
- **create_project_folder** — Create a new folder in the ProjectManager's current folder
- **delete_project** — Permanently delete a project by name from the current folder
- **export_project** — Export a project by name to a .drp file
- **get_current_database** — Get the database connection Resolve's Project Manager is currently using
- **get_database_list** — List database connections configured in Resolve's Project Manager
- **get_folder_list** — List subfolder names in the ProjectManager's current folder
- **get_project_list** — List project names in the ProjectManager's current folder
- **goto_parent_folder** — Navigate the ProjectManager up to the parent of the current folder
- **goto_project_folder** — Navigate into a named subfolder of the ProjectManager's current folder
- **goto_root_folder** — Navigate the ProjectManager back to the root folder of the Project Library
- **import_project** — Import a project (.drp) file into the current database/folder
- **load_project** — Load (open) an existing project by name from the current folder
- **restore_project** — Restore a project from a project backup/archive file
- **save_project** — Save the currently open project
- **set_current_database** — Switch Resolve's Project Manager to a different database connection

### `tools/render.py` (17)
- **add_render_job** — Add a render job to the queue based on the current render settings
- **delete_all_render_jobs** — Delete every render job currently in the render queue
- **delete_render_job** — Delete a single render job from the queue
- **get_render_codecs** — Get the codecs supported by a given render format
- **get_render_formats** — Get available render formats, or the codecs supported by one format
- **get_render_job_list** — List all render jobs currently in the render queue, with their settings/status
- **get_render_job_status** — Get the status (progress, completion, error) of a specific render job
- **get_render_mode** — Get the current render mode (0 = individual clips, 1 = single clip)
- **get_render_preset_list** — List the names of available render presets (built-in and saved)
- **get_render_settings** — Get current render format/codec, render mode, queued jobs, and presets
- **is_rendering** — Check whether a render is currently in progress
- **load_render_preset** — Load a render preset by name, applying its settings to the render queue
- **set_render_format_and_codec** — Set the current render format and codec together
- **set_render_mode** — Set the render mode
- **set_render_settings** — Configure render settings for the current project
- **start_rendering** — Start rendering queued jobs
- **stop_rendering** — Stop any currently running render process

### `tools/color.py` (15)
- **add_color_version** — Add a new color version to a clip
- **apply_grade_from_drx** — Apply a grade from a .drx PowerGrade file to a clip's node graph
- **delete_color_version** — Delete a color version from a clip by name
- **get_current_color_version** — Get the current color version (name + type) of a clip, as JSON
- **get_lut** — Get the LUT path applied to a node in a clip's color node graph
- **get_node_graph** — Get the color-grading node graph (node count + per-node label/LUT) for a clip
- **get_node_label** — Get the label of a node in a clip's color node graph
- **get_num_nodes** — Get the number of nodes in a clip's color node graph
- **grab_all_stills** — Grab stills from every clip in the current timeline into the gallery
- **grab_still** — Grab a still of the current frame under the playhead into the gallery
- **load_color_version** — Load a named color version as the active version on a clip
- **reset_all_grades** — Reset all grades on a clip's color node graph
- **set_cdl** — Apply CDL (Color Decision List) values to a node on a clip
- **set_lut** — Apply a LUT to a node in a clip's color node graph
- **set_node_enabled** — Enable or disable a node in a clip's color node graph

### `tools/media_pool.py` (15)
- **append_to_timeline** — Append Media Pool clips to the current timeline, by name
- **create_bin** — Create a new bin (subfolder) under the Media Pool's current folder
- **create_empty_timeline** — Create a new empty timeline with the given name in the current project
- **create_timeline_from_clips** — Create a new timeline with the given name, populated from Media Pool clips by name
- **delete_clips** — Delete one or more clips from the Media Pool's current folder, by name
- **delete_folders** — Delete one or more bins (subfolders) from the Media Pool, by name
- **get_current_folder** — Get the name (and clip/subfolder listing) of the Media Pool's current folder, as JSON
- **get_media_pool_structure** — Get the folder/clip structure of the Media Pool, as JSON
- **import_media** — Import media files into the Media Pool's current folder, as JSON
- **import_timeline_from_file** — Import a timeline from an AAF/EDL/XML/FCPXML/DRT/ADL/OTIO file, as JSON
- **move_clips** — Move clips from the Media Pool's current folder into a target folder, by name
- **refresh_folders** — Refresh the Media Pool's folders from other stations in collaboration mode
- **relink_clips** — Relink Media Pool clips to a new source folder location, by name
- **set_current_folder** — Set the Media Pool's current folder by name
- **unlink_clips** — Unlink Media Pool clips (mark them offline), by name

### `tools/resolve_app.py` (15)
- **delete_layout_preset** — Delete a saved UI layout preset by name
- **export_layout_preset** — Export a saved UI layout preset to a file on disk
- **export_render_preset** — Export a render preset from the current project to a file
- **get_current_page** — Get the currently active page in DaVinci Resolve (Media/Cut/Edit/Fusion/Color/Fairlight/Deliver)
- **get_keyframe_mode** — Get the current Color page keyframe mode (0=All, 1=Color, 2=Sizing)
- **get_product_info** — Get the DaVinci Resolve product name and version string as JSON
- **get_version** — Get the DaVinci Resolve version as structured fields (major/minor/patch/build/suffix) JSON
- **import_layout_preset** — Import a UI layout preset from a file on disk
- **import_render_preset** — Import a render preset from a file into the current project
- **list_layout_presets** — List saved DaVinci Resolve UI layout presets found on disk, as JSON
- **load_layout_preset** — Load a saved UI layout preset by name
- **open_page** — Switch DaVinci Resolve to a specific page
- **save_layout_preset** — Save the current UI layout as a new named preset
- **set_keyframe_mode** — Set the Color page keyframe mode
- **update_layout_preset** — Overwrite an existing UI layout preset with the current layout

### `tools/timeline_item.py` (15)
- **add_item_marker** — Add a marker to a timeline item
- **add_take** — Add a take to a timeline item, sourced from a media pool clip
- **delete_item_marker_at_frame** — Delete the marker at a specific (source) frame on a timeline item
- **delete_take** — Delete a take from a timeline item by index
- **finalize_take** — Finalize the currently selected take on a timeline item, replacing the item with that take
- **get_item_clip_color** — Get the clip color of a timeline item
- **get_item_enabled** — Get whether a timeline item is enabled (not disabled/muted), as JSON
- **get_item_markers** — Get all markers on a timeline item, as JSON keyed by (source) frame number
- **get_item_source_range** — Get the source-media trim range of a timeline item, as JSON
- **get_take_count** — Get the number of takes on a timeline item and the currently selected take index, as JSON
- **get_timeline_item_properties** — Get all known properties, markers, flags, and Fusion/clip info for a timeline item, as JSON
- **select_take** — Select a take on a timeline item by index
- **set_item_clip_color** — Set the clip color of a timeline item
- **set_item_enabled** — Enable or disable (mute) a timeline item
- **set_timeline_item_property** — Set one property on a timeline item

### `tools/timeline_edit.py` (12)
- **add_marker** — Add a marker to the current timeline
- **create_compound_clip** — Create a compound clip from one or more timeline items on a single track
- **delete_marker_at_frame** — Delete the marker at a specific frame on the current timeline
- **delete_markers_by_color** — Delete all markers of a given color from the current timeline
- **detect_scene_cuts** — Detect scene cuts in the current timeline and add cut markers/edits
- **get_markers** — Get all markers on the current timeline, as JSON keyed by frame number
- **insert_fusion_generator** — Insert a Fusion generator into the current timeline at the playhead
- **insert_fusion_title** — Insert a Fusion title into the current timeline at the playhead
- **insert_generator** — Insert a generator (e.g. "Solid Color", "Bars and Tone") into the current timeline at the playhead
- **insert_ofx_generator** — Insert an OFX (OpenFX) generator into the current timeline at the playhead
- **insert_title** — Insert a title into the current timeline at the playhead
- **update_marker_custom_data** — Update the custom data string of an existing marker on the current timeline

### `tools/project.py` (10)
- **get_all_project_settings** — Get every setting on the current project, as JSON
- **get_preset_list** — List project presets (format/timeline presets) available on the current project, as JSON
- **get_project_info** — Get a summary of the current DaVinci Resolve project, as JSON
- **get_project_name** — Get the name of the currently open project
- **get_project_setting** — Get a single named setting from the current project
- **get_render_resolutions** — List render resolutions supported by the current project, as JSON
- **refresh_luts** — Refresh Resolve's LUT list from disk, picking up newly added LUT files
- **set_preset** — Apply a named project preset to the current project
- **set_project_name** — Rename the currently open project
- **set_project_setting** — Set a single named setting on the current project

### `tools/fusion.py` (8)
- **add_fusion_comp** — Add a new Fusion composition to a timeline item
- **create_fusion_clip** — Create a Fusion clip from one or more timeline items
- **delete_fusion_comp** — Delete a named Fusion composition from a timeline item
- **export_fusion_comp** — Export a Fusion composition from a timeline item to a file
- **get_fusion_comp_list** — Get all Fusion compositions associated with a timeline item
- **import_fusion_comp** — Import a Fusion composition from a file into a timeline item
- **load_fusion_comp** — Load a named Fusion composition as the active composition on a timeline item
- **rename_fusion_comp** — Rename a Fusion composition on a timeline item

### `tools/media_storage.py` (7)
- **add_clip_mattes_to_media_pool** — Add clip mattes (from Media Storage) to a MediaPoolItem in the current project
- **add_items_to_media_pool** — Add file/folder paths from Media Storage into the current Media Pool bin, as JSON
- **add_timeline_mattes_to_media_pool** — Add timeline mattes (from Media Storage) to a timeline item
- **get_file_list** — List files/clips in an absolute folder path, as seen by Media Storage, as JSON
- **get_mounted_volumes** — List mounted volumes/drives visible in DaVinci Resolve's Media Storage, as JSON
- **get_subfolder_list** — List subfolders of an absolute folder path, as seen by Media Storage, as JSON
- **reveal_in_storage** — Reveal an absolute file or folder path in Resolve's Media Storage browser

### `tools/ai.py` (6)
- **clear_subtitles** — Remove all AI-generated subtitles from the current timeline
- **create_magic_mask** — Create an AI-powered Magic Mask on a timeline item for subject isolation
- **create_subtitles_from_audio** — Generate subtitles from audio on the current timeline using AI speech
- **regenerate_magic_mask** — Regenerate an existing Magic Mask on a timeline item
- **smart_reframe** — Apply Smart Reframe to a timeline item (AI-based reframing)
- **stabilize** — Apply stabilization to a timeline item using the DaVinci Neural Engine

### `tools/audio.py` (4)
- **get_audio_track_count** — Get the number of audio tracks on the current timeline
- **get_voice_isolation_state** — Get the Voice Isolation state for an audio track
- **set_track_mute** — Mute or unmute an audio track on the current timeline
- **set_voice_isolation_state** — Enable/disable Voice Isolation on an audio track (isolate speech from noise)

### `tools/transcription.py` (4)
- **export_srt** — Transcribe an audio/video file locally and save the result as an SRT
- **list_whisper_models** — List available local Whisper model names, as JSON, along with the
- **transcribe_and_add_subtitles** — Transcribe an audio/video file locally and add a marker per segment to
- **transcribe_audio** — Transcribe an audio/video file locally (mlx-whisper on Apple Silicon,

### `tools/export_still.py` (3)
- **export_current_frame** — Export the current frame (playhead position) as a still image
- **export_timeline** — Export the current timeline to a file (AAF/EDL/XML/OTIO/ALE/etc)
- **get_current_thumbnail** — Get a PNG thumbnail of the current frame of the current timeline

### `tools/code.py` (1)
- **execute_resolve_code** — Execute arbitrary Python code in the DaVinci Resolve scripting environment

### `tools/screenshot.py` (1)
- **screenshot** — Take a screenshot so you can SEE the current state of the DaVinci Resolve

## Offline tools (no Resolve connection — read/write local files & a local SQLite store)

Each offline module registers exactly **one** action-dispatch tool (name = domain, params: `action` + typed args). Any action that writes/mutates state returns a `"verified": false` field in its JSON result — structurally correct, not yet calibrated against a live Resolve session. See the README's [Offline (no-Resolve) tools](../../README.md#offline-no-resolve-tools) section for the full breakdown.

### `tools/off_audio.py` — **offline_audio**
Offline audio loudness/level reads via the optional ffmpeg executable. Never touches Resolve.

### `tools/off_audio_plan.py` — **audio_plan**
Offline Fairlight track/stem planning + coverage/loudness analysis. Never touches Resolve.

### `tools/off_capabilities.py` — **capabilities**
Report OFFLINE/advanced tool-set capabilities as JSON. Never touches Resolve.

### `tools/off_color_trace.py` — **color_trace**
A better ColorTrace, offline: carry grades across a re-conform by clip identity. Never touches Resolve.

### `tools/off_conform.py` — **conform**
Offline conform/relink QC + lineage engine (OFFLINE/advanced tool set). Never touches Resolve.

### `tools/off_deliverable.py` — **deliverable**
Deliverable QC / compliance: run a named compliance profile and report per-check pass/fail. Never touches Resolve, never reads a file.

### `tools/off_drp.py` — **drp**
Offline ``.drp`` (Resolve Project) reader/author/Media-Pool surgeon. Never touches Resolve.

### `tools/off_drt.py` — **drt**
Offline ``.drt``/``.drp`` timeline (SeqContainer) inspector/author/surgeon. Never touches Resolve.

### `tools/off_drx.py` — **drx**
Offline ``.drx`` (per-clip grade) inspector / codec / grading-catalog front door. Never touches Resolve.

### `tools/off_editorial.py` — **editorial**
Offline editorial integrity / changelist engine (OFFLINE/advanced tool set). Never touches Resolve.

### `tools/off_fairlight.py` — **fairlight_plan**
Offline Fairlight bus-routing planner. Never touches Resolve.

### `tools/off_fusion.py` — **offline_fusion**
Read/inspect/edit a Fusion composition (.comp) file offline. Never touches Resolve.

### `tools/off_media.py` — **media_ingest**
Offline media-ingest / assistant-editor front end. Never touches Resolve.

### `tools/off_offline_ref.py` — **offline_ref**
Display-referred reference-frame extraction and shot-intent tagging. Never touches Resolve.

### `tools/off_pipeline.py` — **pipeline**
DB-as-truth pipeline orchestration for the OFFLINE/advanced tool set. Never touches Resolve.

### `tools/off_project_db.py` — **project_db**
DB-backed project operations for the OFFLINE/advanced tool set. Never touches Resolve.

### `tools/off_project_read.py` — **project_read**
Offline, read-only ``.drp``/``.drt`` project/timeline/clip inspector. Never touches Resolve.

### `tools/off_provenance.py` — **provenance**
Provenance / audit ledger for the OFFLINE/advanced tool set. Never touches Resolve.

