---
name: davinci-resolve-generate-plugin-list
description: >-
  Index every usable DaVinci Resolve plugin/effect/template installed on this machine —
  ResolveFX/OFX effects, third-party OFX bundles, and Fusion template packs (titles, lower-thirds,
  chapter cards, transitions, incl. purchased MotionVFX .drfx packs) — into a persistent TOC:
  a full manifest file (.json + .md), a short auto-managed summary in root CLAUDE.md, and a
  project-memory pointer, so Claude knows what's available without re-scanning. Use when the user
  wants to catalog / index / "generate a list of" their Resolve plugins, or after installing new
  packs. The davinci-resolve-use-plugins skill consumes this TOC.
---

# Generate the DaVinci Resolve plugin TOC

Builds a durable index of what this machine can actually do in Resolve, so any Claude session
knows the available effects/templates and the `davinci-resolve-use-plugins` skill can look up
exact insert names / regids instead of re-scanning.

**Runs filesystem-only** (no running Resolve, no MCP-server restart): the generator imports the
`davinci-resolve-mcp` scanners directly from the server venv, so it reflects the CURRENT code —
including the `.drfx`-expansion fix (a `.drfx` is a ZIP pack of dozens of templates).

## What it writes (the important design choice)
Do **not** dump the full list into `CLAUDE.md` — it loads every session and would bloat context.
Split it:
- **`davinci-plugins.json`** + **`davinci-plugins.md`** (project root) — the FULL TOC, source of
  truth, read on demand.
- **`CLAUDE.md`** — a short, bounded, auto-managed **summary** block (counts + pack names +
  pointer to the manifest). Regenerating replaces just that block (delimited by
  `<!-- BEGIN/END davinci-plugins -->`).
- **Project memory** — a one-line reference pointer (you add this; see step 3).

## Steps
1. **Locate the server venv.** The MCP server lives at a repo with a `.venv`; find its python
   (e.g. `<repo>/.venv/bin/python`). If unknown, check the `davinci-resolve` MCP registration
   (`claude mcp get davinci-resolve`) or the setup memory.
2. **Run the generator** (stamp the date yourself — scripts can't read the clock):
   ```
   <repo>/.venv/bin/python scripts/generate_plugin_toc.py \
     --out ./davinci-plugins \
     --claude-md ./CLAUDE.md \
     --stamp "<today's date>"
   ```
   It prints the counts and where it wrote. Relay them.
3. **Add a project-memory pointer** (Claude writes this, per the memory system): a `reference`
   memory noting the manifest path + headline counts + that `use-plugins` consumes it. Link it to
   `[[davinci-resolve-mcp-setup]]`.
4. **Report**: counts by category, pack names, and the manifest path. Tell the user to re-run
   after installing new packs.

## Notes
- **Machine-specific & perishable.** The TOC reflects THIS machine at generation time; regenerate
  after installing/removing packs. Say so in the summary (the generator stamps the date).
- **Titles/generators are the actionable ones** (`insert_template_by_name`). Transitions/effects
  templates are listed for awareness but are **GUI-only** (no scripting API to insert a named
  transition). The manifest separates these so `use-plugins` won't try to script them.
- **ResolveFX** entries carry the `regid` (no `ofx.` prefix) ready for `apply_ofx_to_clip`.

## Files
- `scripts/generate_plugin_toc.py` — the generator (imports the MCP scanners; writes json/md +
  upserts the CLAUDE.md block).
