#!/usr/bin/env node
/*
 * npx launcher for the davinci-resolve-mcp (Python) server.
 *
 *   npx github:CiprianSpiridon/davinci-resolve-mcp setup     # install + register clients
 *   npx github:CiprianSpiridon/davinci-resolve-mcp doctor    # health check
 *   npx github:CiprianSpiridon/davinci-resolve-mcp           # run the MCP server (stdio)
 *
 * This is a thin bootstrapper only — the actual server and installer are Python
 * (see install.py / src/davinci_resolve_mcp). It finds a Python 3.10+ interpreter
 * and delegates: `setup`/`doctor` -> `python install.py <cmd>`; no args -> run the
 * installed server (or `python -m davinci_resolve_mcp.server`).
 */
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const PKG = dirname(dirname(fileURLToPath(import.meta.url))); // repo/package root
const isWin = process.platform === "win32";
const venvBin = join(PKG, ".venv", isWin ? "Scripts" : "bin");

function firstExisting(paths) {
  return paths.find((p) => existsSync(p));
}

function findPython() {
  const venvPy = join(venvBin, isWin ? "python.exe" : "python");
  if (existsSync(venvPy)) return venvPy;
  // Probe common interpreter names on PATH.
  for (const name of ["python3", "python"]) {
    const r = spawnSync(name, ["--version"], { stdio: "ignore" });
    if (r.status === 0) return name;
  }
  console.error(
    "error: Python 3.10+ not found. Install it (python.org) and re-run.\n" +
      "  macOS/Linux: python3   Windows: python"
  );
  process.exit(1);
}

function run(cmd, args, opts = {}) {
  const r = spawnSync(cmd, args, { stdio: "inherit", cwd: PKG, ...opts });
  process.exit(r.status === null ? 1 : r.status);
}

const argv = process.argv.slice(2);
const sub = argv[0];
const python = findPython();

if (sub === "setup" || sub === "doctor") {
  // Delegate to the Python one-command installer.
  run(python, [join(PKG, "install.py"), ...argv]);
} else if (sub === "--help" || sub === "-h") {
  console.log(
    "davinci-resolve-mcp (npx launcher)\n\n" +
      "  setup [--clients ...] [--dry-run]   install + register MCP client(s)\n" +
      "  doctor                              verify install and Resolve connection\n" +
      "  (no args)                           run the MCP server over stdio\n"
  );
  process.exit(0);
} else {
  // Run the server. Prefer the installed console script; else the module.
  const server = firstExisting([
    join(venvBin, isWin ? "davinci-resolve-mcp.exe" : "davinci-resolve-mcp"),
  ]);
  if (server) run(server, argv);
  else run(python, ["-m", "davinci_resolve_mcp.server", ...argv]);
}
