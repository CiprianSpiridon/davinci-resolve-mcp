# Quickstart

Get `davinci-resolve-mcp` talking to DaVinci Resolve and your MCP client
(e.g. Claude Desktop) in a few minutes.

## 1. Enable external scripting in DaVinci Resolve

DaVinci Resolve only accepts scripting connections from other applications
if you turn that on explicitly:

1. Open DaVinci Resolve.
2. Go to **DaVinci Resolve → Preferences** (macOS) or **File → Preferences**
   (Windows/Linux).
3. Under the **General** tab, find **External scripting using** and set it
   to **Local** (or **Network**, if the MCP server will run on a different
   machine than Resolve).
4. Click **Save**, then restart DaVinci Resolve if it was already running.

Resolve must be running (with a project open) whenever you want the MCP
server to be able to control it. The server itself starts fine even if
Resolve isn't running yet — tool calls will just report a connection error
until you launch Resolve.

## 2. Install the server

Clone this repository and install it in editable mode into a virtual
environment:

```bash
git clone https://github.com/CiprianSpiridon/davinci-resolve-mcp.git
cd davinci-resolve-mcp
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .
```

This installs the `davinci-resolve-mcp` command (defined in
`pyproject.toml` as the `davinci_resolve_mcp.server:main` entry point) into
the virtual environment.

Optional: install the `transcription` extra if you want the AI transcription
tools to run local speech-to-text:

```bash
pip install -e ".[transcription]"
```

## 3. Configure environment variables (optional)

The server auto-detects the standard DaVinci Resolve install locations for
macOS, Windows, and Linux, so most users don't need to set anything. If your
install is in a non-standard location, or scripting still isn't connecting,
set the variables that apply to your platform as real environment variables —
in the `env` block of your MCP client config (recommended) or your shell. Use
`.env.example` as a reference for the values; the server reads the process
environment and does not auto-load a `.env` file:

| Variable | Purpose |
| --- | --- |
| `RESOLVE_SCRIPT_LIB` | Path to the Resolve `fusionscript` shared library (`fusionscript.so` / `fusionscript.dll`). |
| `RESOLVE_SCRIPT_API` | Path to the Resolve `Developer/Scripting` folder; its `Modules` subfolder is added to `sys.path` so `import DaVinciResolveScript` works. |
| `RESOLVE_MCP_LOG_LEVEL` | Logging verbosity for the MCP server (`DEBUG`, `INFO`, `WARNING`, `ERROR`). Defaults to `INFO`. |

See `.env.example` in the repo root for the default path for each platform.

## 4. Configure your MCP client

For Claude Desktop, open (or create) your `claude_desktop_config.json` and
add an entry pointing at the `davinci-resolve-mcp` command installed in
step 2. A ready-to-copy template is provided at
`claude_desktop_config.example.json` in this repo — copy its `mcpServers`
entry into your own config, adjusting the `command` path to point at the
`davinci-resolve-mcp` executable inside your virtual environment (e.g.
`/absolute/path/to/davinci-resolve-mcp/.venv/bin/davinci-resolve-mcp`).

Claude Desktop config file locations:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Restart Claude Desktop after editing the config so it picks up the new
server.

## 5. Try your first prompt

With DaVinci Resolve running (project open) and Claude Desktop restarted,
try asking:

> "What DaVinci Resolve project is currently open, and what's on the
> timeline?"

If everything is wired up correctly, Claude will call the server's tools
(e.g. project/timeline inspection tools) and describe the current project
back to you. If it reports a connection error instead, double-check step 1
(external scripting must be enabled and Resolve must be running) and step 3
(`RESOLVE_SCRIPT_LIB` / `RESOLVE_SCRIPT_API`, if your install is
non-standard).
