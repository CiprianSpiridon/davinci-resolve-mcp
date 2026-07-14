---
name: davinci-resolve-use-plugins
version: 1.1.0
description: >-
  Discover and USE every plugin/effect/template on this machine in DaVinci Resolve via the
  davinci-resolve MCP: built-in ResolveFX/OFX effects applied to a clip, installed Fusion template
  packs (titles, lower-thirds, chapter cards, intros, generators — e.g. purchased MotionVFX .drfx
  packs) inserted onto the timeline, and MotionVFX "Effect" templates applied onto a clip as a
  Fusion .setting macro. Use when the user wants to add an effect/plugin, drop a title/lower-third/
  intro/chapter card, apply a ResolveFX, or "use my plugins / MotionVFX / templates" in Resolve.
when_to_use: >-
  Trigger when the user wants to apply a ResolveFX/OFX effect, insert a title / lower-third / intro
  / chapter card / generator, or apply a MotionVFX effect template — including purchased .drfx
  packs. Requires DaVinci Resolve Studio running with a project + timeline. Read the pre-built TOC
  (davinci-plugins.md/.json) first. For general Resolve operation see the base `davinci-resolve` skill.
argument-hint: "[e.g. 'add a vignette to clip 3' | 'drop an intro title' | 'apply mAntique Cracked to clip 5']"
---

# Use plugins, effects & template packs (DaVinci Resolve)

> **Claude: you CAN use these.** Every plugin/effect/template in the TOC
> (`davinci-plugins.md`) is usable right now — titles, generators, effects, and ResolveFX via
> scripting; transitions best-effort/GUI. Don't tell the user they're "GUI-only" or that you can't
> use their MotionVFX packs. When asked to use one: (1) find it in the TOC, (2) read its **lane**
> below, (3) run that lane's tool. Do this before saying anything is impossible.

**Short answer: nearly all of them are usable via scripting.** The five categories in the TOC map
to four scripting lanes (plus one honest asterisk). Pick the right lane — mixing them up is the #1
cause of "nothing happened":

| TOC category | Lane | How (scriptable?) |
|---|---|---|
| **Titles** (incl. MotionVFX) | insert as its own clip | ✅ `insert_template_by_name(kind="fusion_title", name)` at the playhead |
| **Generators** | insert as its own clip | ✅ `insert_template_by_name(kind="fusion_generator", name)` |
| **Effects** (MotionVFX Fusion effects) | apply onto an existing clip | ✅ extract the `.setting` from the `.drfx`, then `apply_macro_to_clip(setting_path=…)` |
| **ResolveFX / OFX** (built-in) | apply onto an existing clip | ✅ `apply_ofx_to_clip(regid)` |
| **Transitions** (MotionVFX) | span a cut | ⚠️ **no transition-at-cut API** — best-effort (apply as a comp over the cut) or GUI |

This is a focused composition over the base **`davinci-resolve`** MCP skill (see its
`cookbook.md` / `fusion-tools.md`).

## 0 — Ensure the TOC + CLAUDE.md are current, then read the TOC
1. **If `davinci-plugins.json` / `davinci-plugins.md` is missing, OR the root `CLAUDE.md` has no
   `<!-- BEGIN davinci-plugins -->` block, OR the user just installed/removed packs → run the
   `davinci-resolve-generate-plugin-list` skill first.** It (re)writes the TOC **and updates
   root `CLAUDE.md`** with the capability summary, so every future session knows these exist. Do
   this before using anything — it's how the awareness stays in sync.
2. **Read the TOC** — it has every exact `name`, `description` (e.g. `Titles › Infographics`),
   ResolveFX `regid`, and for `.drfx` templates the `drfx` archive path + internal `member` path
   you need for the macro lane.
3. Only fall back to live `enumerate_templates` / `get_resolvefx_registry` if you can't generate
   the TOC (e.g. the MCP repo venv isn't reachable).

## Preconditions
DaVinci Resolve **Studio** running, project + timeline open, External scripting = Local. Work on a
duplicate/versioned timeline (insert/apply mutate the live timeline with no scripted undo). Verify
with `screenshot`.

## Lane 1 — Titles / lower-thirds / chapter cards / intros (incl. MotionVFX)
1. Playhead where it starts: `set_current_timecode("HH:MM:SS:FF")`.
2. Insert by the registered name from the TOC:
   `insert_template_by_name(kind="fusion_title", name="mTuber 4 Chapter")`
   (it retries the internal template-id on a `None` result; on "template not resolved", confirm the
   exact name in the GUI Effects Library).
3. Fill text: `set_title_text(text="Chapter 1 — Setup", track_type="video", track_index=<title's
   track>, item_index=<its index>)`. Multi-field MotionVFX layouts: drive inputs with
   `fusion_set_input`, or edit in the GUI.
4. Verify: `get_timeline_items` + `screenshot`.

## Lane 2 — Generators
Same as Lane 1 with `kind="fusion_generator"` (e.g. backgrounds, animated shapes).

## Lane 3 — MotionVFX Effect templates (apply onto a clip)
A MotionVFX "Effect" is a Fusion `.setting` macro inside the `.drfx`. Apply it to a clip's comp:
1. From the TOC row get `drfx` (archive path) + `member` (internal path). Extract the `.setting`:
   ```
   python3 -c "import zipfile,tempfile,sys; z=zipfile.ZipFile(sys.argv[1]); print(z.extract(sys.argv[2], tempfile.mkdtemp()))" \
     "<drfx path>" "<member path>"
   ```
   → prints an absolute `.setting` path.
2. Apply it to the target clip:
   `apply_macro_to_clip(setting_path="<extracted .setting>", track_type="video", track_index=1, item_index=<clip>)`
   (tries `ImportFusionComp`, falls back to `LoadSettings`). If the clip has no comp yet it still
   works; complex macros that expect specific inputs may need GUI tweaks — relay the result string.
3. Verify: `screenshot`. (`attach_fusion_comp` / `import_fusion_comp` are equivalent entry points.)

## Lane 4 — ResolveFX / OFX (apply onto a clip)
`apply_ofx_to_clip(regid="com.blackmagicdesign.resolvefx.vignette", params='{"Opacity":0.8}',
track_type="video", track_index=1, item_index=0)`
- `regid` = the TOC's `regid` (wireId) **WITHOUT** the `ofx.` prefix (the tool adds it).
- Node is spliced before `MediaOut1`, so it renders. Enum inputs take the **integer** index.
- Upbeat-look picks (with TOC descriptions): `vignette`, `glow`, `filmlook`, `lightray`, `sharpen`,
  `gaussianblur` (blur-in), `zoomblur` (punch energy), `dehaze`, `beauty`.

## Lane 5 — Transitions (the one genuine GUI-only case)
A MotionVFX transition `.setting` is a Fusion **`MacroOperator` with TWO inputs**
(`MainInput1`/`MainInput2`) — an edit-point transition that Resolve auto-wires to the two adjacent
clips at a cut. So:
- The Effects macro-lane does **not** apply (that's single-input; a transition needs both clips).
- There is **no scripting call** to place a Fusion transition at a cut or wire its two inputs
  (verified: official API has none; repo `operating-notes.md #4`; `hasattr` on live objects is a
  false positive — it returns True for any name). Do custom transitions in the **GUI**.
- **Scriptable transitions = built-in dissolves only.** Live: `add_default_transition_at_cut(...)`
  (Cmd+T keystroke). Offline: `place_transition` / `author_transition_interchange` byte-author a
  `.drt`/`.drp` — but they support only a fixed SMPTE-dissolve list (`cross-dissolve`, `dissolve`,
  `additive-dissolve`, `film-dissolve`, `dip-to-color`) and come back **`"verified": false`**
  (needs a live import to confirm). They have **no** primitive for a custom **Fusion/MotionVFX**
  transition (that's a Fusion-transition element referencing the macro — undocumented, version-
  specific serialization). Hand-writing it into the `.drt` bytes is theoretically possible but
  unsupported, fragile, and unverified — treat as GUI-only.
- **Practical upbeat alternatives that ARE reliably scriptable:** a built-in dissolve at section
  breaks; or hard cut + a title/generator "whoosh" overlay on an upper track; or a
  `zoomblur`/`gaussianblur` blur-in on the incoming clip via `apply_ofx_to_clip`.

## Guardrails
- **Match the lane** (table). Trying to `insert_template_by_name` an Effect, or `apply_ofx_to_clip`
  a `.drfx` name, fails — Effects use the macro lane, ResolveFX use the regid lane.
- **Non-destructive**: insert/apply on a versioned timeline, not the master.
- Relay `Error:` strings; verify visually. See the base skill's `operating-notes.md` for the
  `ofx.`-prefix and Fusion gotchas.

## Honest limits
- Titles/generators/effects/ResolveFX: scriptable. **Named transitions at a cut: not scriptable**
  (default transition or GUI only).
- `set_title_text` sets the primary Text+ field; multi-field layouts may need `fusion_set_input`/GUI.
- A rare pack may register a different display name than its `.setting` basename — if an insert
  says "template not resolved", confirm the name in the GUI Effects Library.
