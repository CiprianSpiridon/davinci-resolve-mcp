# DaVinci Resolve MCP — full tool reference

Auto-generated from `mcp.list_tools()` — **326 tools** (308 live + 18 offline) across 40 modules.

## Live tools (drive a running DaVinci Resolve via the scripting API)

### `tools/color.py` (42)
- **add_color_group** — Create a new color group in the current project.
- **add_color_version** — Add a new color version to a clip.
- **analyze_dolby_vision** — Analyze Dolby Vision metadata across the current timeline's clips.
- **apply_arri_cdl_lut** — Apply the ARRI CDL and LUT to a clip's color node graph.
- **apply_grade_from_drx** — Apply a grade from a .drx PowerGrade file to a clip's node graph.
- **assign_clip_to_color_group** — Assign a clip to a color group by group name.
- **copy_grades** — Copy a source clip's current grade onto one or more target clips.
- **create_gallery_album** — Create a new gallery still or PowerGrade album in the current project.
- **delete_color_group** — Delete a color group from the current project by name.
- **delete_color_version** — Delete a color version from a clip by name.
- **delete_stills** — Delete stills from a gallery still album by 0-based index.
- **export_lut** — Export a clip's current grade as a LUT file.
- **export_stills** — Export stills from a gallery still album to image files.
- **get_album_stills** — List the stills in a gallery still album, as a JSON array.
- **get_color_group_node_graph** — Get a color group's pre-clip or post-clip node graph, as JSON.
- **get_current_color_version** — Get the current color version (name + type) of a clip, as JSON.
- **get_current_still_album** — Get the name of the gallery's currently selected still album.
- **get_lut** — Get the LUT path applied to a node in a clip's color node graph.
- **get_node_cache_mode** — Get the render-cache mode of a node in a clip's color node graph, as JSON.
- **get_node_graph** — Get the color-grading node graph (node count + per-node label/LUT) for a clip.
- **get_node_label** — Get the label of a node in a clip's color node graph.
- **get_num_nodes** — Get the number of nodes in a clip's color node graph.
- **get_still_label** — Get the label of one still in a gallery still album.
- **get_timeline_node_graph** — Get the current timeline's node graph (timeline-level grade), as JSON.
- **get_tools_in_node** — Get the list of tool names used in a node of a clip's color node graph.
- **grab_all_stills** — Grab stills from every clip in the current timeline into the gallery.
- **grab_still** — Grab a still of the current frame under the playhead into the gallery.
- **import_stills** — Import stills into a gallery still album from image files.
- **list_color_groups** — List the names of all color groups in the current project, as a JSON array.
- **list_gallery_albums** — List the names of gallery still or PowerGrade albums, as a JSON array.
- **load_color_version** — Load a named color version as the active version on a clip.
- **remove_clip_from_color_group** — Remove a clip from whatever color group it is currently assigned to.
- **rename_color_version** — Rename a clip's color version from ``old_name`` to ``new_name``.
- **rename_gallery_album** — Rename a gallery still or PowerGrade album.
- **reset_all_grades** — Reset all grades on a clip's color node graph.
- **reset_node_colors** — Reset the node color (label color) on every node of the active version.
- **set_cdl** — Apply CDL (Color Decision List) values to a node on a clip.
- **set_current_still_album** — Set the gallery's current still album by name.
- **set_lut** — Apply a LUT to a node in a clip's color node graph.
- **set_node_cache_mode** — Set the render-cache mode of a node in a clip's color node graph.
- **set_node_enabled** — Enable or disable a node in a clip's color node graph.
- **set_still_label** — Set the label of one still in a gallery still album.

### `tools/media_pool_item.py` (29)
- **add_clip_flag** — Add a flag of the given color to a media pool clip.
- **add_clip_marker** — Add a marker to a media pool clip.
- **analyze_for_intellisearch** — Run Neural-Engine IntelliSearch analysis on a media pool clip.
- **analyze_for_slate** — Run Neural-Engine Slate (clapperboard) analysis on a media pool clip.
- **clear_clip_color** — Clear the clip color of a media pool clip.
- **clear_clip_flags** — Clear flags of a given color (or all flags) from a media pool clip.
- **clear_clip_mark_in_out** — Clear the mark in/out points of a media pool clip.
- **delete_clip_markers_by_color** — Delete all markers of a given color from a media pool clip.
- **get_clip_color** — Get the clip color (as shown in the media pool UI) of a media pool clip.
- **get_clip_flags** — Get the list of flag colors set on a media pool clip, as JSON.
- **get_clip_mark_in_out** — Get the mark in/out points of a media pool clip, as JSON.
- **get_clip_markers** — Get all markers on a media pool clip, as JSON keyed by frame number.
- **get_clip_metadata** — Get one or all metadata fields of a media pool clip, as JSON.
- **get_clip_property** — Get one or all properties of a media pool clip, as JSON.
- **get_third_party_metadata** — Get one or all third-party metadata fields of a media pool clip, as JSON.
- **link_proxy_media** — Link a proxy media file to a media pool clip.
- **monitor_growing_file** — Monitor a growing (still-being-written) file for a media pool clip.
- **perform_audio_classification** — Run Neural-Engine audio classification on a media pool clip.
- **remove_motion_blur** — Apply Neural-Engine motion deblur to a media pool clip.
- **replace_clip** — Replace a media pool clip's underlying source media with another file.
- **set_clip_color** — Set the clip color (as shown in the media pool UI) of a media pool clip.
- **set_clip_mark_in_out** — Set the mark in/out points of a media pool clip.
- **set_clip_metadata** — Set a metadata field on a media pool clip.
- **set_clip_property** — Set a property on a media pool clip.
- **set_input_color_space** — Set the Input Color Space of a media pool clip (color-managed projects only).
- **set_third_party_metadata** — Set a third-party metadata field on a media pool clip.
- **unlink_proxy_media** — Unlink the proxy media file from a media pool clip.
- **update_clip_marker_custom_data** — Update the custom data string of an existing marker on a media pool clip.
- **update_sidecar** — Update the sidecar file for a camera-raw media pool clip.

### `tools/timeline.py` (27)
- **add_track** — Add a new track of the given type to the current timeline.
- **clear_timeline_mark_in_out** — Clear the mark in/out range on the current timeline.
- **delete_track** — Delete a specific track from the current timeline.
- **duplicate_timeline** — Duplicate the current timeline, returning info about the new one.
- **get_current_timecode** — Get the current playhead timecode on the current timeline.
- **get_current_timeline_info** — Get detailed information about the current timeline, as JSON.
- **get_end_timecode** — Get the timecode of the last frame of the current timeline.
- **get_start_timecode** — Get the start timecode of the current timeline (its frame-0 timecode).
- **get_timeline_items** — List all clips/items on a specific track of the current timeline, as JSON.
- **get_timeline_list** — List every timeline in the current project, as JSON.
- **get_timeline_mark_in_out** — Get the current timeline's mark in/out range, as JSON.
- **get_timeline_setting** — Get one or all settings on the current timeline, as JSON.
- **get_track_count** — Get the number of tracks of a given type on the current timeline.
- **get_track_enable** — Check whether a specific track on the current timeline is enabled.
- **get_track_lock** — Check whether a specific track on the current timeline is locked.
- **get_track_name** — Get the name of a specific track on the current timeline.
- **get_track_sub_type** — Get the sub-type of a specific track on the current timeline.
- **import_into_timeline** — Import (merge) items from an AAF/EDL/XML/DRT file INTO the current timeline.
- **set_clips_linked** — Link or unlink two or more timeline items (e.g. an A/V clip pair).
- **set_current_timecode** — Move the playhead on the current timeline to a specific timecode.
- **set_current_timeline** — Make the named timeline the current (active) timeline.
- **set_start_timecode** — Set the start timecode of the current timeline (its frame-0 timecode).
- **set_timeline_mark_in_out** — Set the mark in/out range on the current timeline.
- **set_timeline_setting** — Set a single named setting on the current timeline.
- **set_track_enable** — Enable or disable a specific track on the current timeline.
- **set_track_lock** — Lock or unlock a specific track on the current timeline.
- **set_track_name** — Rename a specific track on the current timeline.

### `tools/media_pool.py` (25)
- **append_to_timeline** — Append Media Pool clips to the current timeline, by name.
- **auto_sync_audio** — Auto-sync audio across Media Pool clips (needs a video + audio pair), as JSON.
- **convert_timeline_to_stereo** — Convert the current timeline to stereoscopic 3D.
- **create_bin** — Create a new bin (subfolder) under the Media Pool's current folder.
- **create_empty_timeline** — Create a new empty timeline with the given name in the current project.
- **create_stereo_clip** — Create a stereoscopic 3D clip from a left- and right-eye clip, as JSON.
- **create_timeline_from_clips** — Create a new timeline with the given name, populated from Media Pool clips by name.
- **delete_clip_mattes** — Delete matte files from a Media Pool clip, by clip name, as JSON.
- **delete_clips** — Delete one or more clips from the Media Pool's current folder, by name.
- **delete_folders** — Delete one or more bins (subfolders) from the Media Pool, by name.
- **export_metadata** — Export clip metadata from the Media Pool to a CSV file, as JSON.
- **get_clip_matte_list** — List the matte (alpha/mask) file paths attached to a Media Pool clip, as JSON.
- **get_current_folder** — Get the name (and clip/subfolder listing) of the Media Pool's current folder, as JSON.
- **get_media_pool_structure** — Get the folder/clip structure of the Media Pool, as JSON.
- **get_selected_clips** — Get the names of the clips currently selected in the Media Pool UI, as a JSON list.
- **get_timeline_matte_list** — List the timeline mattes stored in a Media Pool folder, by folder name, as JSON.
- **import_folder_from_file** — Import a Media Pool bin from a ``.drb`` file into the current folder.
- **import_media** — Import media files into the Media Pool's current folder, as JSON.
- **import_timeline_from_file** — Import a timeline from an AAF/EDL/XML/FCPXML/DRT/ADL/OTIO file, as JSON.
- **move_clips** — Move clips from the Media Pool's current folder into a target folder, by name.
- **refresh_folders** — Refresh the Media Pool's folders from other stations in collaboration mode.
- **relink_clips** — Relink Media Pool clips to a new source folder location, by name.
- **set_current_folder** — Set the Media Pool's current folder by name.
- **set_selected_clip** — Select a single clip in the Media Pool UI, by name.
- **unlink_clips** — Unlink Media Pool clips (mark them offline), by name.

### `tools/render.py` (24)
- **add_render_job** — Add a render job to the queue based on the current render settings.
- **delete_all_render_jobs** — Delete every render job currently in the render queue.
- **delete_render_job** — Delete a single render job from the queue.
- **delete_render_preset** — Delete a render preset by name.
- **export_burn_in_preset** — Export a data burn-in preset to a file.
- **get_quick_export_presets** — List the available Quick Export render preset names.
- **get_render_codecs** — Get the codecs supported by a given render format.
- **get_render_formats** — Get available render formats, or the codecs supported by one format.
- **get_render_job_list** — List all render jobs currently in the render queue, with their settings/status.
- **get_render_job_status** — Get the status (progress, completion, error) of a specific render job.
- **get_render_mode** — Get the current render mode (0 = individual clips, 1 = single clip).
- **get_render_preset_list** — List the names of available render presets (built-in and saved).
- **get_render_settings** — Get current render format/codec, render mode, queued jobs, and presets.
- **import_burn_in_preset** — Import a data burn-in preset from a file.
- **is_rendering** — Check whether a render is currently in progress.
- **load_burn_in_preset** — Load a data burn-in preset for the current project.
- **load_render_preset** — Load a render preset by name, applying its settings to the render queue.
- **quick_export** — Render the current timeline using a Quick Export preset.
- **save_render_preset** — Save the current render settings as a new render preset.
- **set_render_format_and_codec** — Set the current render format and codec together.
- **set_render_mode** — Set the render mode.
- **set_render_settings** — Configure render settings for the current project.
- **start_rendering** — Start rendering queued jobs.
- **stop_rendering** — Stop any currently running render process.

### `tools/timeline_item.py` (22)
- **add_item_marker** — Add a marker to a timeline item.
- **add_take** — Add a take to a timeline item, sourced from a media pool clip.
- **delete_item_marker_at_frame** — Delete the marker at a specific (source) frame on a timeline item.
- **delete_take** — Delete a take from a timeline item by index.
- **finalize_take** — Finalize the currently selected take on a timeline item, replacing the item with that take.
- **get_item_clip_color** — Get the clip color of a timeline item.
- **get_item_enabled** — Get whether a timeline item is enabled (not disabled/muted), as JSON.
- **get_item_markers** — Get all markers on a timeline item, as JSON keyed by (source) frame number.
- **get_item_source_range** — Get the source-media trim range of a timeline item, as JSON.
- **get_linked_items** — Get the timeline items linked to a timeline item (e.g. its audio companions), as a JSON list.
- **get_output_cache_state** — Get the color and Fusion output-cache state of a timeline item, as JSON.
- **get_source_audio_channel_mapping** — Get the source audio channel mapping of a timeline item, as JSON.
- **get_stereo_params** — Get the stereoscopic-3D parameters of a timeline item, as JSON.
- **get_take_count** — Get the number of takes on a timeline item and the currently selected take index, as JSON.
- **get_timeline_item_properties** — Get all known properties, markers, flags, and Fusion/clip info for a timeline item, as JSON.
- **get_track_type_and_index** — Get the track type and 1-based track index a timeline item lives on, as JSON.
- **select_take** — Select a take on a timeline item by index.
- **set_color_output_cache** — Enable or disable the color-page output cache for a timeline item.
- **set_fusion_output_cache** — Set the Fusion-page output cache mode for a timeline item.
- **set_item_clip_color** — Set the clip color of a timeline item.
- **set_item_enabled** — Enable or disable (mute) a timeline item.
- **set_timeline_item_property** — Set one property on a timeline item.

### `tools/project_manager.py` (20)
- **archive_project** — Archive a project by name to a ``.dra`` archive on disk.
- **close_project** — Close the currently open project.
- **create_cloud_project** — Create a new Blackmagic Cloud project (DaVinci Resolve Studio 18.5+).
- **create_project** — Create a new project in the ProjectManager's current folder.
- **create_project_folder** — Create a new folder in the ProjectManager's current folder.
- **delete_project** — Permanently delete a project by name from the current folder.
- **export_project** — Export a project by name to a .drp file.
- **get_current_database** — Get the database connection Resolve's Project Manager is currently using.
- **get_database_list** — List database connections configured in Resolve's Project Manager.
- **get_folder_list** — List subfolder names in the ProjectManager's current folder.
- **get_project_list** — List project names in the ProjectManager's current folder.
- **goto_parent_folder** — Navigate the ProjectManager up to the parent of the current folder.
- **goto_project_folder** — Navigate into a named subfolder of the ProjectManager's current folder.
- **goto_root_folder** — Navigate the ProjectManager back to the root folder of the Project Library.
- **import_project** — Import a project (.drp) file into the current database/folder.
- **load_cloud_project** — Load an existing Blackmagic Cloud project (DaVinci Resolve Studio 18.5+).
- **load_project** — Load (open) an existing project by name from the current folder.
- **restore_project** — Restore a project from a project backup/archive file.
- **save_project** — Save the currently open project.
- **set_current_database** — Switch Resolve's Project Manager to a different database connection.

### `tools/resolve_app.py` (18)
- **delete_layout_preset** — Delete a saved UI layout preset by name.
- **export_layout_preset** — Export a saved UI layout preset to a file on disk.
- **export_render_preset** — Export a render preset from the current project to a file.
- **get_current_page** — Get the currently active page in DaVinci Resolve (Media/Cut/Edit/Fusion/Color/Fairlight/Deliver).
- **get_keyframe_mode** — Get the current Color page keyframe mode (0=All, 1=Color, 2=Sizing).
- **get_product_info** — Get the DaVinci Resolve product name and version string as JSON.
- **get_version** — Get the DaVinci Resolve version as structured fields (major/minor/patch/build/suffix) JSON.
- **import_layout_preset** — Import a UI layout preset from a file on disk.
- **import_render_preset** — Import a render preset from a file into the current project.
- **list_layout_presets** — List saved DaVinci Resolve UI layout presets found on disk, as JSON.
- **load_layout_preset** — Load a saved UI layout preset by name.
- **open_page** — Switch DaVinci Resolve to a specific page.
- **play_timeline** — Start playback of the current timeline in DaVinci Resolve.
- **quit_resolve** — Terminate the DaVinci Resolve application.
- **save_layout_preset** — Save the current UI layout as a new named preset.
- **set_keyframe_mode** — Set the Color page keyframe mode.
- **stop_timeline** — Stop playback of the current timeline in DaVinci Resolve.
- **update_layout_preset** — Overwrite an existing UI layout preset with the current layout.

### `tools/fusion.py` (16)
- **add_fusion_comp** — Add a new Fusion composition to a timeline item.
- **apply_macro_to_clip** — Apply a Fusion ``.setting`` macro to a clip's composition (file round-trip).
- **create_fusion_clip** — Create a Fusion clip from one or more timeline items.
- **delete_fusion_comp** — Delete a named Fusion composition from a timeline item.
- **execute_fusion_lua** — Execute a Lua script in DaVinci Resolve's Fusion (escape hatch).
- **export_fusion_comp** — Export a Fusion composition from a timeline item to a file.
- **fusion_add_tool** — Add a Fusion tool (node) to a timeline item's composition.
- **fusion_connect_input** — Wire (or unwire) a tool input to another tool's output.
- **fusion_get_input** — Read the current value of a Fusion tool input.
- **fusion_list_inputs** — List all inputs of a Fusion tool (their INPS_ID / INPS_Name / type).
- **fusion_set_input** — Set a value on a Fusion tool input, verifying it by readback.
- **get_fusion_comp_list** — Get all Fusion compositions associated with a timeline item.
- **import_fusion_comp** — Import a Fusion composition from a file into a timeline item.
- **load_fusion_comp** — Load a named Fusion composition as the active composition on a timeline item.
- **rename_fusion_comp** — Rename a Fusion composition on a timeline item.
- **set_title_text** — Set the text (and optional Size/Center) of a Text+ title on a clip.

### `tools/timeline_edit.py` (12)
- **add_marker** — Add a marker to the current timeline.
- **create_compound_clip** — Create a compound clip from one or more timeline items on a single track.
- **delete_marker_at_frame** — Delete the marker at a specific frame on the current timeline.
- **delete_markers_by_color** — Delete all markers of a given color from the current timeline.
- **detect_scene_cuts** — Detect scene cuts in the current timeline and add cut markers/edits.
- **get_markers** — Get all markers on the current timeline, as JSON keyed by frame number.
- **insert_fusion_generator** — Insert a Fusion generator into the current timeline at the playhead.
- **insert_fusion_title** — Insert a Fusion title into the current timeline at the playhead.
- **insert_generator** — Insert a generator (e.g. "Solid Color", "Bars and Tone") into the current timeline at the playhead.
- **insert_ofx_generator** — Insert an OFX (OpenFX) generator into the current timeline at the playhead.
- **insert_title** — Insert a title into the current timeline at the playhead.
- **update_marker_custom_data** — Update the custom data string of an existing marker on the current timeline.

### `tools/audio.py` (10)
- **apply_fairlight_preset** — Apply a Fairlight preset to the current timeline.
- **generate_speech** — Synthesize speech (text-to-speech) via the DaVinci Neural Engine.
- **get_audio_track_count** — Get the number of audio tracks on the current timeline.
- **get_fairlight_presets** — List the available Fairlight audio presets.
- **get_voice_isolation_state** — Get the Voice Isolation state for an audio track.
- **insert_audio_at_playhead** — Insert an audio file at the playhead on the selected Fairlight track.
- **set_audio_volume** — Set the fader volume (gain) of a single audio clip on the timeline.
- **set_track_mute** — Mute or unmute an audio track on the current timeline.
- **set_track_volume** — Set the fader volume (gain) of a whole audio track on the timeline.
- **set_voice_isolation_state** — Enable/disable Voice Isolation on an audio track (isolate speech from noise).

### `tools/fx_plugins.py` (12)
- **append_template_with_placement** — Place a Media Pool item onto a video track with explicit framing.
- **apply_ofx_to_clip** — Apply an OFX / ResolveFX plugin to a timeline clip via its Fusion comp.
- **attach_fusion_comp** — Import a saved Fusion composition onto an existing timeline item.
- **discover_regid** — Read a LIVE Fusion tool's ``TOOLS_RegID`` (ground-truth RegID).
- **enumerate_ofx** — List installed OFX plugin bundles (``*.ofx.bundle``) on this machine.
- **enumerate_templates** — List Fusion Edit templates (titles/generators/etc.) on this machine.
- **get_template_controls** — Read a template's exposed Inspector controls (macro_tool + published input keys) offline.
- **insert_template_by_name** — Insert a title / generator template at the playhead of the current timeline.
- **install_dctl** — Write a DCTL source file into Resolve's LUT or ACES Transforms tree.
- **install_fuse** — Write a Fusion Fuse (.fuse) source file into Fusion's Fuses directory.
- **set_template_fields** — Set several controls (text/color/scale/…) on a placed template in one call.

### `tools/project.py` (10)
- **get_all_project_settings** — Get every setting on the current project, as JSON.
- **get_preset_list** — List project presets (format/timeline presets) available on the current project, as JSON.
- **get_project_info** — Get a summary of the current DaVinci Resolve project, as JSON.
- **get_project_name** — Get the name of the currently open project.
- **get_project_setting** — Get a single named setting from the current project.
- **get_render_resolutions** — List render resolutions supported by the current project, as JSON.
- **refresh_luts** — Refresh Resolve's LUT list from disk, picking up newly added LUT files.
- **set_preset** — Apply a named project preset to the current project.
- **set_project_name** — Rename the currently open project.
- **set_project_setting** — Set a single named setting on the current project.

### `tools/inspector.py` (8)
- **get_inspector_properties** — Get the Video-tab Inspector property snapshot for a timeline item, as JSON.
- **inspector_property_reference** — List every Video-tab Inspector property with its range and enum tables, as JSON.
- **reset_transform** — Reset a timeline item's Transform panel to the neutral identity transform.
- **set_composite** — Set Inspector > Composite panel fields on a timeline item (all optional).
- **set_cropping** — Set Inspector > Cropping panel fields on a timeline item (all optional).
- **set_dynamic_zoom** — Set Inspector > Dynamic Zoom easing on a timeline item.
- **set_retime_and_scaling** — Set Inspector > Retime and Scaling fields on a timeline item (all optional).
- **set_transform** — Set Inspector > Transform panel fields on a timeline item (all optional).

### `tools/media_storage.py` (7)
- **add_clip_mattes_to_media_pool** — Add clip mattes (from Media Storage) to a MediaPoolItem in the current project.
- **add_items_to_media_pool** — Add file/folder paths from Media Storage into the current Media Pool bin, as JSON.
- **add_timeline_mattes_to_media_pool** — Add timeline mattes (from Media Storage) to a timeline item.
- **get_file_list** — List files/clips in an absolute folder path, as seen by Media Storage, as JSON.
- **get_mounted_volumes** — List mounted volumes/drives visible in DaVinci Resolve's Media Storage, as JSON.
- **get_subfolder_list** — List subfolders of an absolute folder path, as seen by Media Storage, as JSON.
- **reveal_in_storage** — Reveal an absolute file or folder path in Resolve's Media Storage browser.

### `tools/ai.py` (6)
- **clear_subtitles** — Remove all AI-generated subtitles from the current timeline.
- **create_magic_mask** — Create an AI-powered Magic Mask on a timeline item for subject isolation.
- **create_subtitles_from_audio** — Generate subtitles from audio on the current timeline using AI speech
- **regenerate_magic_mask** — Regenerate an existing Magic Mask on a timeline item.
- **smart_reframe** — Apply Smart Reframe to a timeline item (AI-based reframing).
- **stabilize** — Apply stabilization to a timeline item using the DaVinci Neural Engine.

### `tools/transcription.py` (6)
- **clear_clip_transcription** — Clear the native (Neural-Engine) audio transcription of a media pool clip
- **export_srt** — Transcribe an audio/video file locally and save the result as an SRT
- **list_whisper_models** — List available local Whisper model names, as JSON, along with the
- **transcribe_and_add_subtitles** — Transcribe an audio/video file locally and add a marker per segment to
- **transcribe_audio** — Transcribe an audio/video file locally (mlx-whisper on Apple Silicon,
- **transcribe_clip_audio** — Transcribe a media pool clip's audio using Resolve's own Neural-Engine

### `tools/transitions.py` (5)
- **add_default_audio_transition_at_cut** — Add Resolve's default AUDIO cross-fade at a cut via a live keystroke (Shift+T).
- **add_default_transition_at_cut** — Add Resolve's default VIDEO transition at a cut via a live keystroke (Cmd/Ctrl+T).
- **author_audio_crossfade_interchange** — Author an importable audio-crossfade timeline file; no live Resolve.
- **author_transition_interchange** — Author an importable timeline file carrying dissolves; no live Resolve.
- **place_transition** — Inject a native transition into a ``.drt``/``.drp`` offline; write a byte-patched copy.

### `tools/keyframes.py` (4)
- **add_transform_keyframe** — Add an Edit-page transform keyframe on a timeline item.
- **delete_transform_keyframe** — Delete an Edit-page transform keyframe at a frame on a timeline item.
- **fusion_add_keyframe** — Keyframe a Fusion tool input (animate it with a BezierSpline).
- **get_transform_keyframes** — Read back the Edit-page transform keyframes for a property on an item.

### `tools/export_still.py` (3)
- **export_current_frame** — Export the current frame (playhead position) as a still image.
- **export_timeline** — Export the current timeline to a file (AAF/EDL/XML/OTIO/ALE/etc).
- **get_current_thumbnail** — Get a PNG thumbnail of the current frame of the current timeline.

### `tools/code.py` (1)
- **execute_resolve_code** — Execute arbitrary Python code in the DaVinci Resolve scripting environment.

### `tools/screenshot.py` (1)
- **screenshot** — Take a screenshot so you can SEE the current state of the DaVinci Resolve

## Offline tools (operate on Resolve FILES + a local SQLite store — no Resolve needed)

### `tools/off_audio.py` (1)
- **offline_audio** — Offline audio loudness/level reads via the optional ffmpeg executable. Never touches Resolve.

### `tools/off_audio_plan.py` (1)
- **audio_plan** — Offline Fairlight track/stem planning + coverage/loudness analysis. Never touches Resolve.

### `tools/off_capabilities.py` (1)
- **capabilities** — Report OFFLINE/advanced tool-set capabilities as JSON. Never touches Resolve.

### `tools/off_color_trace.py` (1)
- **color_trace** — A better ColorTrace, offline: carry grades across a re-conform by clip identity. Never touches Resolve.

### `tools/off_conform.py` (1)
- **conform** — Offline conform/relink QC + lineage engine (OFFLINE/advanced tool set). Never touches Resolve.

### `tools/off_deliverable.py` (1)
- **deliverable** — Deliverable QC / compliance: run a named compliance profile and report per-check pass/fail. Never touches Resolve, never reads a file.

### `tools/off_drp.py` (1)
- **drp** — Offline ``.drp`` (Resolve Project) reader/author/Media-Pool surgeon. Never touches Resolve.

### `tools/off_drt.py` (1)
- **drt** — Offline ``.drt``/``.drp`` timeline (SeqContainer) inspector/author/surgeon. Never touches Resolve.

### `tools/off_drx.py` (1)
- **drx** — Offline ``.drx`` (per-clip grade) inspector / codec / grading-catalog front door. Never touches Resolve.

### `tools/off_editorial.py` (1)
- **editorial** — Offline editorial integrity / changelist engine (OFFLINE/advanced tool set). Never touches Resolve.

### `tools/off_fairlight.py` (1)
- **fairlight_plan** — Offline Fairlight bus-routing planner. Never touches Resolve.

### `tools/off_fusion.py` (1)
- **offline_fusion** — Read/inspect/edit a Fusion composition (.comp) file offline. Never touches Resolve.

### `tools/off_media.py` (1)
- **media_ingest** — Offline media-ingest / assistant-editor front end. Never touches Resolve.

### `tools/off_offline_ref.py` (1)
- **offline_ref** — Display-referred reference-frame extraction and shot-intent tagging. Never touches Resolve.

### `tools/off_pipeline.py` (1)
- **pipeline** — DB-as-truth pipeline orchestration for the OFFLINE/advanced tool set. Never touches Resolve.

### `tools/off_project_db.py` (1)
- **project_db** — DB-backed project operations for the OFFLINE/advanced tool set. Never touches Resolve.

### `tools/off_project_read.py` (1)
- **project_read** — Offline, read-only ``.drp``/``.drt`` project/timeline/clip inspector. Never touches Resolve.

### `tools/off_provenance.py` (1)
- **provenance** — Provenance / audit ledger for the OFFLINE/advanced tool set. Never touches Resolve.
