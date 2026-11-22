#!/usr/bin/env node
/**
 * rlm-agent installer — interactive.
 *
 * Detects which editors are present, lets the user pick which one(s) to
 * install to, and lets the user override the install location for each.
 * The editor list lives in editors.js (single source of truth, shared with
 * update.js).
 *
 * Usage:
 *   node install.js                 # interactive
 *   node install.js --editor opencode   # non-interactive, default location
 *   node install.js --editor hermes --dir ~/.hermes
 *   node install.js --editor claude     # Claude Code plugin
 *   node install.js --editor codex      # Codex CLI (config.toml + AGENTS.md)
 *
 * Editors supported:
 *   opencode  → ~/.config/opencode/plugins/rlm.ts + rlm-kernel/kernel.py
 *   pi        → ~/.pi/agent/extensions/rlm.ts + rlm-kernel/kernel.py
 *   hermes    → ~/.hermes/plugins/rlm/__init__.py + kernel.py
 *   claude    → ~/.claude/plugins/rlm-agent/ (plugin: manifest, .mcp.json,
 *               hooks, rlm-usage skill, MCP server + kernel)
 *   codex     → ~/.codex/rlm-agent/ (MCP server + kernel) + [mcp_servers.rlm]
 *               in config.toml + usage rules in AGENTS.md
 */
import { homedir } from "node:os";
import { resolve } from "node:path";
import readline from "node:readline/promises";
import { EDITORS, detectPresent, installEditor } from "./editors.js";

const HOME = homedir();

// ─── Prompt helper (TTY + pipe-safe) ─────────────────────────────────────────
// readline/promises only serves ONE question from a piped stdin. For pipes we
// buffer all lines up front and serve them sequentially; for a TTY we use the
// normal interactive prompt.
let _pipedLines = null;
let _pipedIdx = 0;

async function _loadPipedLines() {
  if (_pipedLines !== null) return;
  _pipedLines = [];
  if (process.stdin.isTTY) return;
  const rl = readline.createInterface({ input: process.stdin });
  for await (const line of rl) {
    _pipedLines.push(line);
  }
}

async function ask(rl, question, defaultVal = "") {
  const prompt = defaultVal ? `${question} [${defaultVal}]: ` : `${question}: `;
  if (process.stdin.isTTY) {
    const ans = await rl.question(prompt);
    return ans.trim() || defaultVal;
  }
  // Piped stdin: serve buffered lines.
  await _loadPipedLines();
  process.stdout.write(prompt);
  const ans = _pipedIdx < _pipedLines.length ? _pipedLines[_pipedIdx++] : "";
  return ans.trim() || defaultVal;
}

async function main() {
  const args = process.argv.slice(2);
  const editorArg = args.indexOf("--editor");
  const dirArg = args.indexOf("--dir");

  const present = detectPresent(HOME);
  const allKeys = Object.keys(EDITORS);

  // Non-interactive mode
  if (editorArg >= 0) {
    const key = args[editorArg + 1];
    if (!EDITORS[key]) {
      console.error(`Unknown editor "${key}". Valid: ${allKeys.join(", ")}`);
      process.exit(1);
    }
    const base = dirArg >= 0 ? resolve(args[dirArg + 1]) : EDITORS[key].defaultDir(HOME);
    installEditor(key, base);
    return;
  }

  // Interactive mode
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  console.log("rlm-agent installer — pick where to install RLM.\n");

  if (present.length === 0) {
    console.log("No supported editor detected. You can still install manually:");
    console.log(`  node install.js --editor <${allKeys.join("|")}> --dir <path>`);
    rl.close();
    return;
  }

  console.log("Detected editors:");
  present.forEach((k, i) => console.log(`  ${i + 1}. ${EDITORS[k].label}`));
  console.log("  a. All detected editors");
  console.log("  q. Quit\n");

  const choice = (await ask(rl, "Which editor(s)? (number, comma-separated, 'a', or 'q')")).trim().toLowerCase();
  if (choice === "q") { rl.close(); return; }

  let selected = [];
  if (choice === "a") {
    selected = present;
  } else {
    for (const part of choice.split(",")) {
      const n = parseInt(part.trim(), 10);
      if (n >= 1 && n <= present.length) selected.push(present[n - 1]);
    }
  }
  if (selected.length === 0) {
    console.log("No valid selection. Nothing installed.");
    rl.close();
    return;
  }

  for (const key of selected) {
    const def = EDITORS[key];
    const defaultDir = def.defaultDir(HOME);
    const custom = (await ask(rl, `Install dir for ${def.label}`, defaultDir)).trim() || defaultDir;
    installEditor(key, custom);
  }

  console.log("\nDone. RLM is installed. See README.md for usage.");
  rl.close();
}

main().catch((err) => {
  console.error("Install failed:", err);
  process.exit(1);
});
