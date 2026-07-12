#!/usr/bin/env node
/*
 * npm distribution bootstrapper for davinci-resolve-mcp.
 *
 * PURPOSE: this npm package exists as a DISTRIBUTION / DISCOVERY channel (npm is
 * where people find and try MCP servers) and as a no-clone installer. It is NOT a
 * runtime component — MCP clients launch the Python console script directly. This
 * launcher just finds Python and delegates:
 *
 *   npx @ciprianspiridon/davinci-resolve-mcp setup    # install + register clients
 *   npx @ciprianspiridon/davinci-resolve-mcp doctor   # health check
 *   npx @ciprianspiridon/davinci-resolve-mcp          # run the server (stdio)
 *
 * (Also works from the repo: `npx github:CiprianSpiridon/davinci-resolve-mcp setup`.)
 */
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const PKG = dirname(dirname(fileURLToPath(import.meta.url)));
const isWin = process.platform === "win32";
const venvBin = join(PKG, ".venv", isWin ? "Scripts" : "bin");

function findPython() {
  const venvPy = join(venvBin, isWin ? "python.exe" : "python");
  if (existsSync(venvPy)) return venvPy;
  for (const name of ["python3", "python"]) {
    if (spawnSync(name, ["--version"], { stdio: "ignore" }).status === 0) return name;
  }
  console.error(
    "error: Python 3.10+ not found (the server is Python). Install it from python.org and re-run.\n" +
      "  macOS/Linux: python3   Windows: python"
  );
  process.exit(1);
}

function run(cmd, args) {
  const r = spawnSync(cmd, args, { stdio: "inherit", cwd: PKG });
  process.exit(r.status === null ? 1 : r.status);
}

const argv = process.argv.slice(2);
const sub = argv[0];
const python = findPython();

if (sub === "setup" || sub === "doctor") {
  run(python, [join(PKG, "install.py"), ...argv]);
} else if (sub === "--help" || sub === "-h") {
  console.log(
    "davinci-resolve-mcp (npm installer/launcher — the server is Python)\n\n" +
      "  setup [--clients ...] [--dry-run]   install + register MCP client(s)\n" +
      "  doctor                              verify install and Resolve connection\n" +
      "  (no args)                           run the MCP server over stdio\n"
  );
  process.exit(0);
} else {
  const server = join(venvBin, isWin ? "davinci-resolve-mcp.exe" : "davinci-resolve-mcp");
  if (existsSync(server)) run(server, argv);
  else run(python, ["-m", "davinci_resolve_mcp.server", ...argv]);
}
