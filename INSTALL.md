# INSTALL.md — Agent setup runbook for `davinci-resolve-mcp`

**Audience: an AI coding agent (e.g. Claude Code).** You are setting up the
`davinci-resolve-mcp` MCP server on the user's machine and registering it with their MCP
client so they can drive DaVinci Resolve from natural language. Follow the phases **in
order**. Each phase ends with a **verification gate** — do not advance until it passes.
Report what you did and the gate result after each phase.

## Operating rules (read first)

- **Run one command at a time and check its output.** Do not batch past a failing step.
- **Two steps require the human — you cannot do them yourself.** Flag them clearly and
  wait: (1) enabling external scripting inside Resolve's GUI Preferences, and (2)
  restarting the MCP client app after you edit its config. Everything else you do.
- **Never disable TLS verification, never weaken security**, and never `pip install`
  outside the project's virtualenv (`.venv`).
- **Absolute paths only** in any MCP client config — MCP clients do NOT inherit the
  user's shell `PATH` or an activated virtualenv.
- If a gate fails, stop and consult [Troubleshooting](#troubleshooting) before improvising.
- The server is designed to install and start **with no DaVinci Resolve present** — so
  Phases 1–3 and 5 work on any machine; only Phase 6 (live check) needs Resolve running.

---

## Phase 0 — Detect the environment

Run these and record the answers; later commands branch on them.

```bash
# OS
uname -s 2>/dev/null || echo "Windows"        # Darwin=macOS, Linux=Linux, else Windows
# Python (need >= 3.10)
python3 --version || python --version
# git present?
git --version
```

Set two variables for the rest of this runbook based on the OS:

| OS | `PY` (system python) | `VENV_BIN` (venv scripts dir) | server binary |
|---|---|---|---|
| macOS / Linux | `python3` | `.venv/bin` | `.venv/bin/davinci-resolve-mcp` |
| Windows | `python` | `.venv\Scripts` | `.venv\Scripts\davinci-resolve-mcp.exe` |

**Gate 0:** Python reports **3.10 or newer** and `git` exists. If Python is older or
missing, tell the user to install Python 3.10+ and stop.

---

## Phase 1 — Get the repository

If you are **already inside the `davinci-resolve-mcp` repo** (a `pyproject.toml` with
`name = "davinci-resolve-mcp"` is present), skip the clone and just `cd` to the repo root.

Otherwise clone it into a stable location the user will keep (not a temp dir):

```bash
git clone https://github.com/CiprianSpiridon/davinci-resolve-mcp.git
cd davinci-resolve-mcp
```

**Gate 1:** `pwd` is the repo root and `test -f pyproject.toml && echo OK` prints `OK`.
Record the **absolute repo path** — you need it for the client config later
(`REPO="$(pwd)"`).

---

## Phase 2 — Create the virtualenv and install

```bash
# macOS / Linux
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip -q
./.venv/bin/pip install -e . -q
```

```powershell
# Windows (PowerShell)
python -m venv .venv
.\.venv\Scripts\pip install --upgrade pip -q
.\.venv\Scripts\pip install -e . -q
```

Optional extras — only if the user asked for local transcription or a Windows/Linux
screenshot backend:

```bash
# Local speech-to-text (Whisper). On Apple Silicon this pulls mlx-whisper;
# on Windows/Linux it pulls openai-whisper. Large download — ask first.
./.venv/bin/pip install -e ".[transcription]" -q
# Windows/Linux screenshot capture backend (mss + pillow):
./.venv/bin/pip install -e ".[screenshot]" -q
```

**Gate 2:** the editable install completes with no error, and the console script exists:

```bash
test -x ./.venv/bin/davinci-resolve-mcp && echo "binary OK"   # macOS/Linux
# Windows: dir .\.venv\Scripts\davinci-resolve-mcp.exe
```

---

## Phase 3 — Offline verification (no Resolve needed)

Prove the server imports and registers its full tool surface **without** a running
Resolve. This confirms the install is sound independent of Resolve.

```bash
./.venv/bin/python - <<'PY'
import asyncio
from davinci_resolve_mcp.server import mcp
tools = asyncio.run(mcp.list_tools())
names = [t.name for t in tools]
res = asyncio.run(mcp.list_resources())
assert len(names) >= 100, f"too few tools: {len(names)}"
assert len(names) == len(set(names)), "duplicate tool names!"
print(f"OK: {len(names)} tools, {len(res)} resources, all names unique")
PY
```

Optionally run the test suite (installs `pytest` into the venv if missing):

```bash
./.venv/bin/pip install pytest -q && ./.venv/bin/pytest -q
```

**Gate 3:** the snippet prints `OK: 190 tools, 3 resources, all names unique` (the count
must be ≥ 100; 190 is expected). If it errors on import, the install is broken — see
Troubleshooting; do not proceed to register a broken server.

---

## Phase 4 — Enable scripting in DaVinci Resolve  *(HUMAN STEP)*

**You cannot click Resolve's GUI — instruct the user and wait for confirmation.** Tell
them, verbatim:

> In DaVinci Resolve, open **Preferences → General**, find **"External scripting using"**,
> set it to **`Local`**, click **Save**, and **restart DaVinci Resolve**. (Scripting is a
> DaVinci Resolve **Studio** feature; on the free edition many tools will return a
> "requires Resolve Studio" message — that's expected, not a bug.)

**Gate 4:** the user confirms they set External scripting to `Local` and restarted
Resolve. (If they only want to configure the MCP now and use it later, you may proceed —
just note that live calls will fail until this is done.)

---

## Phase 5 — Register the server with the MCP client

Pick the branch matching the user's client. Use the **absolute** server-binary path from
Phase 1/2: `SERVER="$REPO/.venv/bin/davinci-resolve-mcp"` (macOS/Linux) or
`"$REPO\.venv\Scripts\davinci-resolve-mcp.exe"` (Windows).

### 5a — Claude Code (recommended: the CLI does it for you)

```bash
# --scope user makes it available in every project; use --scope project to
# write a shared .mcp.json in the current repo instead.
claude mcp add davinci-resolve --scope user -- "$REPO/.venv/bin/davinci-resolve-mcp"
```

To pass an env var (e.g. verbose logs, or a non-standard Resolve path), add `-e`:

```bash
claude mcp add davinci-resolve --scope user \
  -e RESOLVE_MCP_LOG_LEVEL=INFO \
  -- "$REPO/.venv/bin/davinci-resolve-mcp"
```

Verify registration:

```bash
claude mcp list          # should show: davinci-resolve
claude mcp get davinci-resolve
```

If `claude mcp add` isn't available, fall back to writing `.mcp.json` at the repo root
(project scope) with the JSON shape shown in 5c.

### 5b — Claude Desktop

Edit the config file (create it if missing):
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Merge this into the top-level `"mcpServers"` object (don't clobber existing servers):

```json
{
  "mcpServers": {
    "davinci-resolve": {
      "command": "/ABSOLUTE/PATH/TO/davinci-resolve-mcp/.venv/bin/davinci-resolve-mcp",
      "env": { "RESOLVE_MCP_LOG_LEVEL": "INFO" }
    }
  }
}
```

Then tell the user: **fully quit and reopen Claude Desktop** (HUMAN STEP).

### 5c — Cursor (or any client using the same schema)

Edit `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (per-project) with the exact
same object shape as 5b, then tell the user to reload MCP servers / restart Cursor.

A ready-to-copy template lives at
[`claude_desktop_config.example.json`](./claude_desktop_config.example.json) — replace
its placeholder path with the real absolute `SERVER` path.

**Gate 5:** the config contains the `davinci-resolve` server with the **absolute** binary
path, the JSON is valid (`python3 -m json.tool <file>` succeeds), and — for Claude
Code — `claude mcp list` shows it. Confirm the human restarted the client app.

### 5d — (Claude Code only, optional) install the bundled skill

This repo ships a Claude Code skill at `.claude/skills/davinci-resolve/` that teaches the
agent how to operate the MCP (workflow, tool map, safety, recipes). It auto-loads when
Claude Code runs **inside this repo**. To make it available in every project, offer to
copy it to the user skills dir:

```bash
mkdir -p ~/.claude/skills && cp -r .claude/skills/davinci-resolve ~/.claude/skills/
```

---

## Phase 6 — Live smoke test *(needs Resolve running)*

Only meaningful once Resolve is running with a project open and Phase 4 is done. Ask the
user to open DaVinci Resolve and any project, then in the MCP client (fresh
session/chat) have them ask the agent to call **`get_project_info`**, or run this direct
check yourself:

```bash
./.venv/bin/python - <<'PY'
from davinci_resolve_mcp.tools import project  # noqa: F401  (ensures import path works)
from davinci_resolve_mcp.helpers import _conn
try:
    conn = _conn()                      # connects lazily
    print("Connected:", conn.get_project().GetName())
except Exception as e:
    print("Not connected yet:", e)
PY
```

**Gate 6:** with Resolve running, `get_project_info` (via the client) returns real
project JSON, or the direct check prints `Connected: <project name>`. If it prints a
connection error, work through Troubleshooting item "connection".

**Setup is complete when Gates 0–5 pass and (with Resolve running) Gate 6 returns real
project data.**

---

## Troubleshooting

| Symptom | Likely cause → fix |
|---|---|
| `python: command not found` or version < 3.10 | Install Python 3.10+; on Windows use `python`, on macOS/Linux `python3`. |
| `pip install -e .` fails building | Ensure you're in the repo root (Gate 1) and using the venv's pip (`./.venv/bin/pip`), not system pip. |
| Import snippet raises `ModuleNotFoundError: mcp` | The editable install didn't complete — re-run Phase 2 inside the venv. |
| Client shows the server but tools never respond | The `command` path isn't absolute or points outside the venv. Use the exact `$REPO/.venv/bin/davinci-resolve-mcp`. Restart the client after fixing. |
| **connection**: tools return `Error: Could not connect to DaVinci Resolve` | Resolve isn't running, no project is open, or Phase 4 (External scripting = `Local`) wasn't done / Resolve wasn't restarted. Fix all three. |
| Tools return `... requires Resolve Studio 19+` | Expected on the free edition or older Resolve — those specific AI/Studio tools are unavailable; the rest still work. |
| `import DaVinciResolveScript` fails at connect time | Resolve installed somewhere non-standard — set `RESOLVE_SCRIPT_API` and `RESOLVE_SCRIPT_LIB` (see the table in [README → Configuration](./README.md#configuration)) in the client config's `env` block. |
| Transcription tools return an install hint | Optional Whisper backend not installed — run the `[transcription]` extra from Phase 2 (only if the user wants local STT). |
| Screenshot tool errors on Windows/Linux | Install the `[screenshot]` extra (mss + pillow); on headless Linux there's no display to capture. |

## What "done" looks like (report this back to the user)

- Repo at `<absolute path>`, virtualenv installed, `davinci-resolve-mcp` binary present.
- Offline gate: **190 tools, 3 resources, unique names** (Phase 3).
- Server registered with `<client>` using the absolute binary path.
- Reminder to the user: External scripting must be `Local` and Resolve must be running
  for tools to act; the server is **Studio-oriented** (free edition degrades gracefully).
- Suggest a first prompt: *"What DaVinci Resolve project is open? List the clips on video
  track 1."*
