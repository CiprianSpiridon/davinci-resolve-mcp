# Onboarding runbook — install & register the davinci-resolve MCP

**Audience: an AI agent (Claude Code).** Follow phases in order; each ends with a
**verification gate** — don't advance until it passes. Report what you did and the gate
result after each phase. This runbook is self-contained (it clones the repo itself), so it
works even when this skill was installed standalone via `skills add`.

## Operating rules
- One command at a time; check output; don't batch past a failure.
- **Two steps need the human — you can't do them:** (1) enable External scripting in
  Resolve's GUI Preferences, (2) restart the MCP client app after editing its config. Flag
  and wait.
- **Absolute paths only** in client config — MCP clients don't inherit shell `PATH`/venv.
- Never install outside `.venv`; never disable TLS.
- Phases 0–3 & 5 work with no Resolve present; only Phase 6 (live check) needs Resolve.

**Fast path:** Phases 1–3 & 5 are automated — from a clone run `python3 install.py
--clients <clients>` (venv + install + client registration), then `davinci-resolve-mcp
doctor`. You still do Phase 4 (Resolve GUI) and the client restart by hand. The phases
below are the manual/controlled version.

## Phase 0 — Environment
```bash
uname -s 2>/dev/null || echo Windows      # Darwin=macOS, Linux, else Windows
python3 --version || python --version      # need >= 3.10
git --version
```
Per-OS variables: macOS/Linux → `PY=python3`, venv dir `.venv/bin`, binary
`.venv/bin/davinci-resolve-mcp`. Windows → `PY=python`, venv dir `.venv\Scripts`, binary
`.venv\Scripts\davinci-resolve-mcp.exe`.
**Gate 0:** Python ≥ 3.10 and git present.

## Phase 1 — Repository
If already inside the repo (a `pyproject.toml` with `name = "davinci-resolve-mcp"`), just
`cd` to its root. Otherwise:
```bash
git clone https://github.com/CiprianSpiridon/davinci-resolve-mcp.git
cd davinci-resolve-mcp
```
**Gate 1:** `test -f pyproject.toml && echo OK` prints `OK`. Record `REPO="$(pwd)"`.

## Phase 2 — Virtualenv + install
```bash
# macOS/Linux
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip -q
./.venv/bin/pip install -e . -q
```
```powershell
# Windows
python -m venv .venv
.\.venv\Scripts\pip install --upgrade pip -q
.\.venv\Scripts\pip install -e . -q
```
Optional extras (ask first — large downloads): `-e ".[transcription]"` (local Whisper),
`-e ".[screenshot]"` (Windows/Linux capture).
**Gate 2:** install succeeds and the console script exists (`test -x ./.venv/bin/davinci-resolve-mcp`).

## Phase 3 — Offline verification (no Resolve)
```bash
./.venv/bin/python - <<'PY'
import asyncio
from davinci_resolve_mcp.server import mcp
names=[t.name for t in asyncio.run(mcp.list_tools())]
res=asyncio.run(mcp.list_resources())
assert len(names)>=100 and len(names)==len(set(names))
print(f"OK: {len(names)} tools, {len(res)} resources, unique")
PY
```
**Gate 3:** prints `OK: 208 tools, 3 resources, unique` (≥100 required; 208 = 190 live + 18 offline). If it errors on
import, the install is broken — do not register a broken server.

## Phase 4 — Enable Resolve scripting  *(HUMAN STEP)*
Tell the user verbatim:
> In DaVinci Resolve: **Preferences → General → "External scripting using" → `Local`**,
> **Save**, then **restart DaVinci Resolve**. (Scripting is a Resolve **Studio** feature;
> on the free edition many tools return a "requires Resolve Studio" message — expected.)

**Gate 4:** user confirms. (You may proceed to register now and defer this, noting live
calls fail until it's done.)

## Phase 5 — Register with the MCP client
`SERVER="$REPO/.venv/bin/davinci-resolve-mcp"` (Windows: `"$REPO\.venv\Scripts\davinci-resolve-mcp.exe"`).

**Claude Code (preferred):**
```bash
claude mcp add davinci-resolve --scope user -- "$REPO/.venv/bin/davinci-resolve-mcp"
# with an env var:
claude mcp add davinci-resolve --scope user -e RESOLVE_MCP_LOG_LEVEL=INFO -- "$REPO/.venv/bin/davinci-resolve-mcp"
claude mcp list && claude mcp get davinci-resolve
```

**Claude Desktop** — merge into `"mcpServers"` in
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or
`%APPDATA%\Claude\claude_desktop_config.json` (Windows):
```json
{ "mcpServers": { "davinci-resolve": {
  "command": "/ABSOLUTE/PATH/.venv/bin/davinci-resolve-mcp",
  "env": { "RESOLVE_MCP_LOG_LEVEL": "INFO" } } } }
```
**Cursor** — same object in `~/.cursor/mcp.json` or `.cursor/mcp.json`.

Then tell the user to **restart the client app** (HUMAN STEP). Only add
`RESOLVE_SCRIPT_LIB` / `RESOLVE_SCRIPT_API` to `env` if Resolve is installed in a
non-standard location.
**Gate 5:** config has `davinci-resolve` with an **absolute** binary path, JSON is valid,
and (Claude Code) `claude mcp list` shows it; human restarted the client.

## Phase 6 — Live smoke test *(needs Resolve running)*
With Resolve open + a project loaded + Phase 4 done, have the user ask the agent to call
`get_project_info`, or:
```bash
./.venv/bin/python - <<'PY'
from davinci_resolve_mcp.helpers import _conn
try: print("Connected:", _conn().get_project().GetName())
except Exception as e: print("Not connected yet:", e)
PY
```
**Gate 6:** returns real project data / `Connected: <name>`.

## Optional — make this skill available in every project
This skill lives at `davinci-resolve/` in the repo. To install it for all your projects
via skills.sh:
```bash
npx skills add CiprianSpiridon/davinci-resolve-mcp
```
Or copy it into your user skills dir: `mkdir -p ~/.claude/skills && cp -r davinci-resolve ~/.claude/skills/`.

## Troubleshooting
| Symptom | Fix |
|---|---|
| Python < 3.10 / not found | Install Python 3.10+ (`python` on Windows, `python3` elsewhere). |
| `ModuleNotFoundError: mcp` | Editable install didn't finish — re-run Phase 2 in the venv. |
| Tools listed but never respond | `command` path not absolute / outside venv → fix + restart client. |
| `Error: Could not connect to DaVinci Resolve` | Resolve not running / no project open / External scripting≠Local / not restarted. |
| `... requires Resolve Studio 19+` | Expected on free/old edition; other tools still work. |
| `import DaVinciResolveScript` fails | Non-standard install → set `RESOLVE_SCRIPT_API`/`RESOLVE_SCRIPT_LIB` in the client `env`. |
| Transcription returns install hint | Install the `[transcription]` extra (only if the user wants local STT). |
