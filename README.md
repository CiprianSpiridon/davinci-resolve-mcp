# davinci-resolve-mcp

A full-coverage [Model Context Protocol](https://modelcontextprotocol.io) server for
**DaVinci Resolve Studio** — control editing, media management, color grading, Fusion
compositing, Fairlight audio, AI/Neural Engine features, and rendering from any MCP
client (Claude Desktop, Cursor, or your own agent).

208 tools — **190 live** tools across 18 domain modules that drive a running Resolve
instance via its scripting API, plus **18 offline** tools that read/write Resolve's own
files (`.drp`/`.drt`/`.drx`) and a local SQLite store with **no Resolve connection at
all**. One server, one process, one `mcp = FastMCP(...)` instance — every tool connects
lazily (live tools) or touches only local files (offline tools), on first call.

## Table of contents

- [What & why](#what--why)
- [Architecture](#architecture)
- [Tool catalog](#tool-catalog)
- [Offline (no-Resolve) tools](#offline-no-resolve-tools)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage with Claude Desktop](#usage-with-claude-desktop)
- [Usage with Cursor](#usage-with-cursor)
- [Agent skill (Claude Code, Cowork, and more)](#agent-skill-claude-code-cowork-and-more)
- [Claude Code / Cowork plugin](#claude-code--cowork-plugin)
- [Development & validation](#development--validation)
- [License](#license)

## What & why

DaVinci Resolve's external scripting API (`DaVinciResolveScript` / `fusionscript`) is a
**Python (and Lua) C-extension** — Blackmagic Design ships it as a native module with a
Python-first calling convention (`resolve.GetProjectManager()`,
`project.GetMediaPool()`, `timeline.GetItemListInTrack(...)`, etc.). There is no native
binding for Node.js, Go, Rust, or any other runtime. Any non-Python MCP server would
have to spawn a Python subprocess and shuttle every call across a bridge anyway — an
extra process, an extra serialization boundary, and an extra failure mode for zero
benefit. Python is therefore not just *a* reasonable choice, it is the **strictly
simpler and more direct** one, and it's what every serious project in this space uses
are Python).

This server aims for **full API coverage**: every major Resolve scripting object
(Project, ProjectManager, MediaPool, MediaPoolItem, MediaStorage, Timeline,
TimelineItem, Gallery/Color, Fusion, Fairlight audio, the render/Deliver page, and
AI/Neural Engine features) gets first-class, individually documented tools — not a
single generic "call any method" passthrough. An `execute_resolve_code` escape hatch is
still included for the long tail of API surface that doesn't (yet) have a dedicated
tool.

Design priorities, in order:

1. **Never crash the server.** Every tool wraps its body in `try/except` and returns an
   `"Error: ..."` string on failure. Resolve not running, no project open, no active
   timeline, a Resolve-free-edition/older-version API gap — all of these come back as a
   clear string an LLM can read and react to, never a raised exception or a dead
   process.
2. **Never touch Resolve at import time.** Connecting is fully lazy — the module tree
   imports cleanly (and is tested to do so) on a machine with no DaVinci Resolve
   installed at all. Every tool reconnects on its own first call via a shared,
   lock-guarded connection singleton.
3. **One tool, one job, one clear docstring.** Tool names are globally unique and each
   docstring documents its parameters — that docstring is the only description an LLM
   client sees when deciding whether and how to call the tool.

## Architecture

```
src/davinci_resolve_mcp/
├── app.py            # the single FastMCP instance + startup lifespan + prompt
├── server.py          # entry point: imports every tools/* module (registration
│                      # side effect), then mcp.run() over stdio
├── connection.py      # ResolveConnection: lazy DaVinciResolveScript import,
│                      # per-OS path auto-detection, RLock-guarded accessors,
│                      # stale-handle reconnect, execute_code() escape hatch
├── helpers.py         # _conn(), _require_timeline(), _get_timeline_item(),
│                      # _ok(), _coerce_value() — shared by every tools/* module
├── resolve_utils.py   # serializers: Resolve objects -> plain dict/JSON-safe
│                      # structures (folders, clips, timelines, items, stills)
├── resources.py        # read-only resolve://... MCP resources (project info,
│                      # current timeline, media pool structure)
├── transcription_engine.py  # local Whisper wrapper (mlx-whisper / openai-whisper)
├── formats/            # OFFLINE codecs: drx_xml, drx_codec (zstd FieldsBlob),
│                      # cdl, lut, drt, drp — parse/author Resolve's own files
├── store/               # OFFLINE DB-as-truth: db.py (local SQLite project/run/
│                      # stage store), provenance.py (append-only audit ledger)
├── grading/             # OFFLINE deterministic compute cores: cdl_ops,
│                      # white_balance, skin_match, qc (broadcast-legal/gamut)
└── tools/              # one module per Resolve API domain — see catalog below
    ├── ai.py, audio.py, code.py, color.py, export_still.py, fusion.py,
    ├── media_pool.py, media_pool_item.py, media_storage.py, project.py,
    ├── project_manager.py, render.py, resolve_app.py, screenshot.py,
    ├── timeline.py, timeline_edit.py, timeline_item.py, transcription.py,
    └── off_*.py           # 18 offline tools — see "Offline (no-Resolve) tools"
```

**The rules that keep this architecture sound:**

- `app.py` owns the **single** `mcp = FastMCP(...)` instance. It never imports any
  `tools.*` module (that would risk a circular import), and it never imports
  `DaVinciResolveScript` — its startup "lifespan" tries a best-effort connect and
  swallows failure, since every tool reconnects on its own.
- Every `tools/*.py` module starts with `from ..app import mcp` and registers its tools
  with `@mcp.tool()` against that shared instance as an import-time side effect.
  `server.py` is the **sole** place that imports every `tools.*` module, which is what
  actually wires the whole tool surface together — add or remove one of those imports
  and you add or remove that module's tools from the running server.
- Tool bodies get Resolve state through `helpers._conn()` (→
  `connection.get_resolve_connection()`), never by importing
  `DaVinciResolveScript` themselves. `connection.py` is the only file that imports it,
  and only inside a method body — never at module import time.
- Ownership of tool *names* is pinned per module (e.g. `detect_scene_cuts` and every
  `insert_*` timeline-editing tool live in `tools/timeline_edit.py`; `grab_still` lives
  in `tools/color.py`; `export_timeline` lives in `tools/export_still.py`) so two
  modules can never register a tool with the same name — MCP requires every tool name
  to be globally unique, and `tests/test_tool_exposure.py` asserts this holds.
- **The 18 offline tools follow the same rules, minus the Resolve connection.** Each
  `tools/off_*.py` module still starts with `from ..app import mcp` and registers with
  `@mcp.tool()` against the same shared instance — `server.py` imports them exactly
  like the live modules, so removing one `off_*` import drops exactly that tool. But
  they **never** call `helpers._conn()` and never import `DaVinciResolveScript` —
  instead they read/write local files (`.drp`/`.drt`/`.drx`, `.comp`, media) and a local
  SQLite store (`store/db.py`, `store/provenance.py`) by composing the `formats/`,
  `store/`, and `grading/` layers. See
  [Offline (no-Resolve) tools](#offline-no-resolve-tools) below.

## Tool catalog

**208 tools total** (verified by `tests/test_tool_exposure.py`, which imports the whole
server with no Resolve instance present and asserts on `mcp.list_tools()`): **190 live**
tools across the 18 domain modules below, driving a running Resolve instance, plus **18
offline** tools (covered in the [next section](#offline-no-resolve-tools)) that never
touch Resolve at all. Also 3 read-only MCP resources and 1 prompt.

| Module | Domain | Tools |
|---|---|---:|
| `tools/timeline.py` | Timeline — read/navigate/structure: list & switch timelines, duplicate, settings, tracks, timecode, item listing, markers | 20 |
| `tools/media_pool_item.py` | `MediaPoolItem` — clip properties/metadata, markers, flags, clip color, proxy media, mark in/out | 20 |
| `tools/project_manager.py` | `ProjectManager` — project list/create/load/save/close/delete, folder navigation, database switching, import/export/restore | 17 |
| `tools/render.py` | Render/Deliver — formats/codecs/presets, render settings, render-queue management, job status | 17 |
| `tools/color.py` | Color page — node graph, LUT get/set, CDL, grade-from-DRX, color versions, gallery stills | 15 |
| `tools/media_pool.py` | `MediaPool` — bin/folder tree, media & timeline import, move/delete/relink/unlink, timeline creation | 15 |
| `tools/resolve_app.py` | App-level — page navigation, product/version info, UI layout presets, keyframe mode, render-preset import/export | 15 |
| `tools/timeline_item.py` | `TimelineItem` — properties, markers, clip attributes, takes | 15 |
| `tools/timeline_edit.py` | Timeline mutation — insert/append/delete/move edits, scene-cut detection | 12 |
| `tools/project.py` | Project info & settings — summary, get/set settings, name, supported render resolutions | 10 |
| `tools/fusion.py` | Fusion comp management on timeline items — list/add/import/export/load/delete/rename | 8 |
| `tools/media_storage.py` | `MediaStorage` — mounted-volume browsing, add-to-media-pool | 7 |
| `tools/ai.py` | AI / Neural Engine — Magic Mask, Smart Reframe, Stabilize, AI subtitles | 6 |
| `tools/audio.py` | Audio / Fairlight — voice isolation, audio-specific track tools | 4 |
| `tools/transcription.py` | Local speech-to-text (mlx-whisper / openai-whisper) | 4 |
| `tools/export_still.py` | Timeline export, current-frame still export, clip thumbnail grab | 3 |
| `tools/code.py` | `execute_resolve_code` — arbitrary-snippet escape hatch for uncovered API surface | 1 |
| `tools/screenshot.py` | Screenshot of the running Resolve UI, returned as an in-band MCP `Image` | 1 |
| **Total** | | **190** |

Plus:
- **3 read-only resources** (`resources.py`): `resolve://project/info`,
  `resolve://timeline/current`, `resolve://mediapool/structure`.
- **1 prompt** (`app.py`): `editing_strategy` — a recommended end-to-end workflow for
  driving Resolve through this tool surface.

Run `./.venv/bin/python -c "from davinci_resolve_mcp.server import mcp; import asyncio; print(len(asyncio.run(mcp.list_tools())))"`
yourself at any time to re-verify the live count (190 + 18 = 208) — no Resolve
installation required.

## Offline (no-Resolve) tools

18 tools that never open a Resolve connection — no `_conn()`, no
`DaVinciResolveScript`, no import-time Resolve. Each is a **single
action-dispatch** `@mcp.tool()` (one tool name, an `action` parameter, and
typed per-action arguments) that reads and/or writes local files — Resolve's
own `.drp`/`.drt`/`.drx`/`.comp` formats, plus a local SQLite store — and
returns a JSON string. Failures come back as an `"Error: ..."` string, exactly
like the live tools, never a raised exception.

**Any tool action that writes or mutates state returns `"verified": false`
in its JSON result.** That flag means the write is *structurally* correct —
it round-trips through this project's own parser, matches the on-disk format
byte-for-byte where checked, and passes the automated test fixtures — but it
has **not yet been calibrated by loading the result into a live DaVinci
Resolve** and confirming Resolve reads it identically. Treat `"verified":
false` output as "correct by construction, unconfirmed by Resolve itself"
until you've round-tripped it through a real Resolve session. (Read-only
actions that write nothing return no `"verified"` field at all — there is
nothing to verify.)

Query `capabilities` (action `"report"`) at any time for a live, in-process
inventory of every offline domain, its action vocabulary, and which optional
dependencies (`ffmpeg`, PyYAML, `zstandard`) are available in the current
interpreter — all 18 below degrade to a clear `"Error: ..."` string for the
one action that needs a missing optional dependency, rather than failing to
import.

| Tool name | Domain | What it does |
|---|---|---|
| `capabilities` | Self-inspection | Report offline dependency availability + the domain/action catalog, as JSON. |
| `drx` | `.drx` grade files | Inspect/decode a PowerGrade's zstd-compressed `FieldsBlob`, export/import ASC-CDL, attach a LUT, apply catalog grading ops, verify against a fixture. |
| `drt` | `.drt` timelines | Parse, author, and surgically edit a `.drt`/`.drp` `SeqContainer` timeline (tracks, clips, in/out frames). |
| `drp` | `.drp` projects | Read/author a `.drp` Resolve Project — folders, Media-Pool clips, timelines, embedded Project XML. |
| `project_read` | Read-only inspector | One-call read of any `.drp` or `.drt` file into a flattened `{timelines, clipRecords, ...}` summary. |
| `project_db` | DB-backed grade ops | Batch operations (e.g. node-graph relayout) across a set of local `.drx` grade files. |
| `offline_ref` | Reference frames | Extract a still frame from local media via `ffmpeg`, and tag shot intent. |
| `conform` | Conform/relink QC | Diff a project's recorded media links against an on-disk manifest by frame math, not filename guessing. |
| `color_trace` | Grade carry-over | Match clips between a graded project and its re-conform by content identity, and carry grades across. |
| `offline_fusion` | `.comp` files | Inspect/edit a Fusion composition file's node graph offline. |
| `audio_plan` | Fairlight planning | Turn a project spec into a Fairlight track/stem plan, plus coverage/loudness analysis. |
| `fairlight_plan` | Bus routing | Compute a Fairlight bus-routing plan (the scripting API can't create buses; this plans what a DB patch would need). |
| `offline_audio` | Loudness/level QC | Measure LUFS/dBTP/LRA for a media file via `ffmpeg`'s `ebur128` filter, with optional pass/fail targets. |
| `pipeline` | DB-as-truth orchestration | Compile a spec into the local SQLite store, run pipeline stages with gates, and track intent-vs-actual drift. |
| `deliverable` | Compliance QC | Run a named compliance profile (broadcast-legal/gamut + custom checks) against caller-supplied numbers. |
| `media_ingest` | Assistant-editor ingest | Scan a folder into a SQLite media manifest (hash + optional `ffprobe` technical metadata). |
| `editorial` | Changelist diffing | Diff two `.drp`/`.drt` projects by clip `DbId` identity and report what changed. |
| `provenance` | Audit ledger | Append and query an immutable provenance/audit trail in the local SQLite store. |

These compose four internal layers that do the real parsing/computation —
tools never reimplement it themselves:

- **`formats/`** — `drx_xml`, `drx_codec` (the length-prefixed zstd
  `FieldsBlob` codec, via the `zstandard` package), `cdl`, `lut`, `drt`, `drp`.
- **`store/`** — `db.py` (the local SQLite project/run/stage store) and
  `provenance.py` (the append-only ledger layered on it).
- **`grading/`** — deterministic compute cores: `cdl_ops`, `white_balance`,
  `skin_match`, `qc` (broadcast-legal/gamut checks).
- Optional executables/packages (`ffmpeg`, PyYAML, `zstandard`) are declared
  as the `offline` extra in `pyproject.toml` — install with
  `./.venv/bin/pip install -e ".[offline]"`; every tool still imports and
  degrades gracefully without them.

studied and reimplemented from scratch in Python.

## Installation

> **Want an AI agent (e.g. Claude Code) to set this up for you?** Hand it
> [`INSTALL.md`](./INSTALL.md) — an imperative, gated setup runbook written for an agent
> to execute end-to-end (detect OS → install → verify tool registration offline → enable
> Resolve scripting → register with your MCP client → live smoke test). The steps below
> are the same process for a human. (`INSTALL.md`'s own gate prints its own tool count —
> see [`INSTALL.md`](./INSTALL.md) for the exact figure it currently checks.)

Requirements: Python **3.10+**, and DaVinci Resolve (Studio recommended — some tools
are Studio-only and degrade to an explanatory error string on the free edition)
installed locally for actual use. The server itself installs and *starts* fine without
Resolve present; tools simply return connection errors until Resolve is running.

### One command (recommended)

From a clone, `install.py` creates the venv, installs the server, and **registers it
with your MCP clients** automatically (with a backup of any existing config):

```bash
git clone https://github.com/CiprianSpiridon/davinci-resolve-mcp.git
cd davinci-resolve-mcp
python3 install.py                       # venv + install + register all detected clients
# or target specific clients / preview:
python3 install.py --clients cursor,claude-code
python3 install.py --dry-run --clients all
```

Prefer not to clone? Install straight from Git, then run `setup`:

```bash
pipx install "git+https://github.com/CiprianSpiridon/davinci-resolve-mcp.git"
# or: pip install "git+https://github.com/CiprianSpiridon/davinci-resolve-mcp.git"
davinci-resolve-mcp setup --clients cursor,claude-code
```

Or via **npx** (the package is published to npm as a discovery + no-clone installer; the
server itself is still Python, so you need Python 3.10+ installed):

```bash
npx @ciprianspiridon/davinci-resolve-mcp setup       # install + register
npx @ciprianspiridon/davinci-resolve-mcp doctor      # health check
# from the repo without npm publish: npx github:CiprianSpiridon/davinci-resolve-mcp setup
```

After installing, check health any time with **`davinci-resolve-mcp doctor`** (verifies
all 208 tools — 190 live + 18 offline — register and, if Resolve is running, that a live
connection succeeds). The `setup`/`doctor` subcommands are also available on the console script directly
(`davinci-resolve-mcp setup --clients cursor`).

### Manual

```bash
git clone https://github.com/CiprianSpiridon/davinci-resolve-mcp.git
cd davinci-resolve-mcp
python3 -m venv .venv
./.venv/bin/pip install -e .
```

This installs the `davinci-resolve-mcp` console script (defined in `pyproject.toml` as
`davinci-resolve-mcp = "davinci_resolve_mcp.server:main"`) into `.venv/bin/`, plus the
`mcp[cli]` dependency. `requirements.txt` mirrors the same runtime dependencies if you
prefer `pip install -r requirements.txt`.

Optional local transcription support (Apple Silicon):

```bash
./.venv/bin/pip install -e ".[transcription]"
```

## Configuration

In DaVinci Resolve: **Preferences → General → External scripting using** must be set to
`Local` (or `Network`) for the scripting API to be reachable at all.

All configuration is via environment variables, and **every one is optional** — the
server auto-detects the standard per-OS Resolve install paths. See
[`.env.example`](./.env.example) for the full reference:

| Variable | Purpose | Default (auto-detected) |
|---|---|---|
| `RESOLVE_SCRIPT_LIB` | Path to the `fusionscript` shared library | macOS: `/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so` · Windows: `C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll` · Linux: `/opt/resolve/libs/Fusion/fusionscript.so` |
| `RESOLVE_SCRIPT_API` | Path to the Resolve `Developer/Scripting` directory (its `Modules` subfolder is added to `sys.path`) | macOS: `/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting` · Windows: `%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting` · Linux: `/opt/resolve/Developer/Scripting` |
| `RESOLVE_MCP_LOG_LEVEL` | Server log verbosity (`DEBUG`/`INFO`/`WARNING`/`ERROR`) — logs go to stderr, never stdout, since stdout carries the MCP protocol stream | `INFO` |

Only set these if your Resolve install lives somewhere non-standard. Set them as
real environment variables — the `env` block of your MCP client config (see below)
is the recommended place, or export them in your shell. `.env.example` lists the
variables and per-platform defaults for reference; the server reads the process
environment and does **not** auto-load a `.env` file.

## Usage with Claude Desktop

Add an entry to your `claude_desktop_config.json`
(`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS,
`%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "davinci-resolve": {
      "command": "/absolute/path/to/davinci-resolve-mcp/.venv/bin/davinci-resolve-mcp",
      "env": {
        "RESOLVE_MCP_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

Use the absolute path to the `davinci-resolve-mcp` script inside your virtualenv's
`bin/` (or `Scripts\davinci-resolve-mcp.exe` on Windows) — Claude Desktop does not
inherit your shell's `PATH` or activated virtualenv. Restart Claude Desktop after
editing the config. Only add `RESOLVE_SCRIPT_LIB` / `RESOLVE_SCRIPT_API` to `env` if
your Resolve install is in a non-standard location (see [Configuration](#configuration)).

## Usage with Cursor

Cursor reads MCP server configuration from `~/.cursor/mcp.json` (global) or
`.cursor/mcp.json` in a specific project. The shape is the same as Claude Desktop's:

```json
{
  "mcpServers": {
    "davinci-resolve": {
      "command": "/absolute/path/to/davinci-resolve-mcp/.venv/bin/davinci-resolve-mcp",
      "env": {
        "RESOLVE_MCP_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

Reload the MCP servers list in Cursor's settings (or restart Cursor) after adding this,
then enable the `davinci-resolve` server for the chat/agent you're using.

## Agent skill (Claude Code, Cowork, and more)

This repo ships a dedicated **agent skill** — the canonical source is the root-level
[`davinci-resolve/`](./davinci-resolve/) folder (`davinci-resolve/SKILL.md`), mirrored
into [`.claude/skills/davinci-resolve`](./.claude/skills/) via a symlink so it
**auto-loads whenever you run Claude Code inside this repository**. It works with any
agent that loads skills — **Claude Code and Claude Cowork** both read from the same
`~/.claude/skills/` directory, so a global install lights it up in both.

The skill covers the MCP end to end: **onboarding** (it walks the agent through installing
and registering the MCP if it isn't configured yet — see
[`davinci-resolve/reference/setup.md`](./davinci-resolve/reference/setup.md)) and
**operation** (the orient→act→verify workflow, a tool-map by task, safety around
destructive/render ops, screenshot discipline, and quick recipes). Its
[`reference/tool-catalog.md`](./davinci-resolve/reference/tool-catalog.md) is an exact,
auto-generated list of every tool grouped by module.

**Install it globally** (available in Claude Code, Cowork, and every other agent) with the
[skills.sh](https://skills.sh) CLI — the repo is discoverable (the skill lives at the repo
root):

```bash
npx skills add CiprianSpiridon/davinci-resolve-mcp --global --agent '*' -y
```

Or copy it into the global skills directory manually (Claude Code + Cowork both read this):

```bash
mkdir -p ~/.claude/skills && cp -r davinci-resolve ~/.claude/skills/davinci-resolve
```

## Claude Code / Cowork plugin

The repo is also installable as a **Claude Code plugin** — this bundles both the MCP
server registration (via `.mcp.json`, using `uvx` so there's no manual `pip install`)
and the [agent skill](#agent-skill-claude-code-cowork-and-more) in one step, using the
repo itself as a plugin marketplace
([`.claude-plugin/marketplace.json`](./.claude-plugin/marketplace.json) +
[`.claude-plugin/plugin.json`](./.claude-plugin/plugin.json)):

```bash
claude plugin marketplace add CiprianSpiridon/davinci-resolve-mcp
claude plugin install davinci-resolve
```

This works the same way in **Claude Code** and **Claude Cowork** (both read the same
plugin/marketplace configuration). After install, restart the client if prompted; the
`davinci-resolve` MCP server and the `davinci-resolve` skill are both active with no
further setup. Requires Python 3.10+ and `uvx` (from [`uv`](https://docs.astral.sh/uv/))
on `PATH` for the bundled MCP server to launch — see
[Installation](#installation) for alternatives if you'd rather manage the venv
yourself and point a client at the console script directly.

## Development & validation

```bash
./.venv/bin/pip install -e . pytest
./.venv/bin/pytest
```

`tests/test_tool_exposure.py` imports the full server with **no DaVinci Resolve
instance present and no network access**, and asserts:

- at least 100 tools are registered (the real count is 208: 190 live + 18 offline —
  see the [Tool catalog](#tool-catalog) and [Offline tools](#offline-no-resolve-tools)
  sections above),
- every tool name is globally unique,
- every tool has a non-empty docstring/description,
- the module-ownership contract holds (e.g. exactly one `detect_scene_cuts`, exactly
  one `grab_still`, no `insert_*` tools registered outside `timeline_edit.py`).

The full implementation plan — every task, its acceptance criteria, and `file:line`
[`.ulpi/plans/davinci-resolve-mcp-full-coverage.json`](./.ulpi/plans/davinci-resolve-mcp-full-coverage.json)
(and its human-readable companion,
[`.ulpi/plans/davinci-resolve-mcp-full-coverage.md`](./.ulpi/plans/davinci-resolve-mcp-full-coverage.md)).


detail, including what specifically was studied from each and `file:line` references,
inside their folders).

|---|---:|---|---|

research.

## License

