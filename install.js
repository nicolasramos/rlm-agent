#!/usr/bin/env node
/**
 * rlm-agent installer — interactive.
 *
 * Detects which editors are present, lets the user pick which one(s) to
 * install to, and lets the user override the install location for each.
 *
 * Usage:
 *   node install.js                          # interactive
 *   node install.js --editor opencode        # non-interactive, default location
 *   node install.js --editor hermes --dir ~/.hermes
 *   node install.js --editor copilot --project ~/code/my-repo
 *
 * Editors supported:
 *   opencode  → ~/.config/opencode/plugins/rlm.ts + rlm-kernel/kernel.py
 *   pi        → ~/.pi/agent/extensions/rlm.ts + rlm-kernel/kernel.py
 *   hermes    → ~/.hermes/plugins/rlm/__init__.py + kernel.py
 *   claude    → ~/.claude/rlm/ (MCP server) + CLAUDE.md section + hooks
 *   codex     → ~/.codex/rlm/ (MCP server) + AGENTS.md section + config.toml entry
 *   copilot   → ~/.copilot/rlm/ (MCP server) + ~/.copilot/mcp-config.json
 *
 * The MCP-based editors (claude, codex, copilot) share one stdio MCP server:
 * adapters/mcp/server.py plus kernel/rlm_client.py and kernel/kernel.py.
 */
import { mkdirSync, copyFileSync, existsSync, readFileSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { homedir } from "node:os";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import readline from "node:readline/promises";

const here = dirname(fileURLToPath(import.meta.url));
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

// ─── Small helpers ───────────────────────────────────────────────────────────

function have(bin) {
  const r = spawnSync("sh", ["-c", `command -v ${bin}`], { encoding: "utf-8" });
  return r.status === 0 && Boolean(r.stdout.trim());
}

function pythonBin() {
  if (process.env.RLM_KERNEL_PYTHON) return process.env.RLM_KERNEL_PYTHON;
  for (const c of ["python3", "/usr/bin/python3", "/opt/homebrew/bin/python3"]) {
    if (have(c)) return c;
  }
  return "python3";
}

const START = "<!-- rlm-agent:start — managed by rlm-agent; this block is replaced on reinstall -->";
const END = "<!-- rlm-agent:end -->";

/** Append (or replace) a marked section in a markdown file without touching the rest. */
function upsertBlock(file, body) {
  const block = `${START}\n${body.trim()}\n${END}`;
  let current = "";
  if (existsSync(file)) current = readFileSync(file, "utf-8");
  const s = current.indexOf(START);
  const e = current.indexOf(END);
  let next;
  if (s >= 0 && e > s) {
    next = current.slice(0, s) + block + current.slice(e + END.length);
  } else {
    next = current.trimEnd() + (current.trim() ? "\n\n" : "") + block + "\n";
  }
  mkdirSync(dirname(file), { recursive: true });
  writeFileSync(file, next);
  return file;
}

/** Merge one entry into a JSON config file, creating it if needed. */
function upsertJson(file, mutate) {
  let data = {};
  if (existsSync(file)) {
    try {
      data = JSON.parse(readFileSync(file, "utf-8"));
    } catch (e) {
      throw new Error(`${file} is not valid JSON (${e.message}) — left untouched.`);
    }
  }
  mutate(data);
  mkdirSync(dirname(file), { recursive: true });
  writeFileSync(file, JSON.stringify(data, null, 2) + "\n");
  return file;
}

const instructions = () => readFileSync(join(here, "adapters", "mcp", "instructions.md"), "utf-8");

/** Files that make up the shared MCP server, installed under <base>/rlm/. */
function mcpFiles(base) {
  return [
    { src: join(here, "adapters", "mcp", "server.py"), dest: join(base, "rlm", "server.py") },
    { src: join(here, "adapters", "cli", "rlm.py"), dest: join(base, "rlm", "rlm.py") },
    { src: join(here, "kernel", "rlm_client.py"), dest: join(base, "rlm", "rlm_client.py") },
    { src: join(here, "kernel", "kernel.py"), dest: join(base, "rlm", "kernel.py") },
  ];
}

function serverPath(base) {
  return join(base, "rlm", "server.py");
}

/**
 * Register the MCP server with an editor CLI when it is installed. Returns true
 * when the registration ran, false when the user has to run the printed command
 * (or edit the printed config) themselves.
 */
function registerWithCli(bin, args, manualHint, listArgs) {
  if (!have(bin)) {
    console.log(`  ! ${bin} not found on PATH — register manually:`);
    console.log(`      ${manualHint}`);
    return false;
  }
  // Reinstalling must not fail with "already exists" and print a scary warning.
  if (listArgs) {
    const listed = spawnSync(bin, listArgs, { encoding: "utf-8" });
    if (listed.status === 0 && /\brlm\b/.test(listed.stdout)) {
      console.log(`  ✓ already registered with ${bin}`);
      return true;
    }
  }
  const r = spawnSync(bin, args, { stdio: "inherit" });
  if (r.status !== 0) {
    console.log(`  ! \`${bin} ${args.join(" ")}\` failed — register manually:`);
    console.log(`      ${manualHint}`);
    return false;
  }
  console.log(`  ✓ registered with ${bin}`);
  return true;
}

// ─── Editor definitions ──────────────────────────────────────────────────────
// Each editor: how to detect it, and the files to copy (source → dest relative
// to the editor's base dir). `dest` may be a function of the base dir.
const EDITORS = {
  opencode: {
    label: "OpenCode",
    detect: () => existsSync(join(HOME, ".config", "opencode")),
    defaultDir: () => join(HOME, ".config", "opencode"),
    files: (base) => [
      { src: join(here, "adapters", "opencode", "rlm.ts"), dest: join(base, "plugins", "rlm.ts") },
      { src: join(here, "kernel", "kernel.py"), dest: join(base, "rlm-kernel", "kernel.py") },
    ],
    restart: "Restart opencode to activate.",
  },
  pi: {
    label: "PI (pi-mono)",
    detect: () => existsSync(join(HOME, ".pi", "agent", "extensions")) || existsSync(join(HOME, ".pi")),
    defaultDir: () => join(HOME, ".pi", "agent"),
    files: (base) => [
      { src: join(here, "adapters", "pi", "rlm.ts"), dest: join(base, "extensions", "rlm.ts") },
      { src: join(here, "kernel", "kernel.py"), dest: join(base, "rlm-kernel", "kernel.py") },
      // Usage rules: the decision rule that tells the agent WHEN to use RLM.
      // PI loads ~/.pi/agent/AGENTS.md as global context on every session.
      { src: join(here, "adapters", "pi", "AGENTS.md"), dest: join(base, "AGENTS.md") },
    ],
    restart: "Run /reload in PI to activate.",
  },
  hermes: {
    label: "Hermes Agent",
    detect: () => existsSync(join(HOME, ".hermes", "config.yaml")) || existsSync(join(HOME, ".hermes")),
    defaultDir: () => join(HOME, ".hermes"),
    files: (base) => [
      { src: join(here, "adapters", "hermes", "__init__.py"), dest: join(base, "plugins", "rlm", "__init__.py") },
      { src: join(here, "adapters", "hermes", "plugin.yaml"), dest: join(base, "plugins", "rlm", "plugin.yaml") },
      { src: join(here, "kernel", "kernel.py"), dest: join(base, "plugins", "rlm", "kernel.py") },
      // Usage skill: the decision rule that tells the agent WHEN to use RLM.
      // Without it the plugin only describes the tools; the skill activates them.
      { src: join(here, "adapters", "hermes", "skills", "rlm-usage", "SKILL.md"), dest: join(base, "skills", "agent-workflow", "rlm-usage", "SKILL.md") },
    ],
    restart: "Restart Hermes (or /reset in a session) to load the plugin.",
    postInstall: (base) => enableHermesPlugin(base),
  },

  // ── MCP-based editors ──────────────────────────────────────────────────────
  claude: {
    label: "Claude Code",
    detect: () =>
      existsSync(join(HOME, ".claude")) || existsSync(join(HOME, ".claude.json")) || have("claude"),
    defaultDir: () => join(HOME, ".claude"),
    files: (base) => [
      ...mcpFiles(base),
      { src: join(here, "adapters", "mcp", "claude_hook.py"), dest: join(base, "rlm", "claude_hook.py") },
    ],
    instructions: (base) => upsertBlock(join(base, "CLAUDE.md"), instructions()),
    postInstall: (base) => {
      registerWithCli(
        "claude",
        ["mcp", "add", "--scope", "user", "rlm", "--", pythonBin(), serverPath(base)],
        `claude mcp add --scope user rlm -- ${pythonBin()} ${serverPath(base)}`,
        ["mcp", "list"],
      );
      installClaudeHooks(base);
    },
    restart: "Restart Claude Code (or /mcp to check the server) to activate.",
  },

  codex: {
    label: "Codex",
    detect: () =>
      existsSync(join(HOME, ".codex")) || existsSync("/Applications/Codex.app") || have("codex"),
    defaultDir: () => join(HOME, ".codex"),
    files: (base) => mcpFiles(base),
    instructions: (base) => upsertBlock(join(base, "AGENTS.md"), instructions()),
    postInstall: (base) => {
      const registered = registerWithCli(
        "codex",
        ["mcp", "add", "rlm", "--", pythonBin(), serverPath(base)],
        `codex mcp add rlm -- ${pythonBin()} ${serverPath(base)}`,
        ["mcp", "list"],
      );
      if (!registered) upsertCodexToml(base);
    },
    restart: "Restart Codex to activate.",
  },

  copilot: {
    label: "Copilot (VS Code + Copilot CLI)",
    detect: () => existsSync(join(HOME, ".copilot")) || have("copilot"),
    defaultDir: () => join(HOME, ".copilot"),
    files: (base) => mcpFiles(base),
    // Copilot has no user-level markdown instructions file: the rules go into the
    // repository's .github/copilot-instructions.md (override with --project).
    instructions: (base, opts) =>
      upsertBlock(
        join(opts.project || process.cwd(), ".github", "copilot-instructions.md"),
        instructions(),
      ),
    postInstall: (base) => {
      // Portable MCP format (mcpServers) — read by Copilot CLI and the Agent Host.
      const file = upsertJson(join(base, "mcp-config.json"), (data) => {
        data.mcpServers = data.mcpServers || {};
        data.mcpServers.rlm = {
          type: "stdio",
          command: pythonBin(),
          args: [serverPath(base)],
        };
      });
      console.log(`  ✓ MCP server registered in ${file}`);
    },
    restart: "Reload VS Code (MCP: Show Installed Servers) to activate.",
  },
};

function detectPresent() {
  return Object.entries(EDITORS)
    .filter(([, def]) => def.detect())
    .map(([key]) => key);
}

function installEditor(key, baseDir, opts = {}) {
  const def = EDITORS[key];
  const files = def.files(baseDir);
  const created = [];
  for (const f of files) {
    mkdirSync(dirname(f.dest), { recursive: true });
    copyFileSync(f.src, f.dest);
    created.push(f.dest);
  }
  console.log(`\n✓ ${def.label} installed:`);
  for (const c of created) console.log(`  ${c}`);
  if (def.instructions) {
    try {
      console.log(`  ✓ usage rules: ${def.instructions(baseDir, opts)}`);
    } catch (e) {
      console.log(`  ⚠ usage rules not installed: ${e.message}`);
    }
  }
  if (def.postInstall) {
    try {
      def.postInstall(baseDir);
    } catch (e) {
      console.log(`  ⚠ ${e.message}`);
    }
  }
  console.log(`  ${def.restart}`);
}

// Claude Code hooks: SessionStart injects the kernel variable list, PreCompact
// feeds the compaction summary. Hooks run in their own process, so they read the
// state summary the MCP server writes after every cell.
function installClaudeHooks(base) {
  const hook = join(base, "rlm", "claude_hook.py");
  const command = `${pythonBin()} ${hook}`;
  const file = join(base, "settings.json");
  try {
    upsertJson(file, (data) => {
      data.hooks = data.hooks || {};
      for (const event of ["SessionStart", "PreCompact"]) {
        const entries = Array.isArray(data.hooks[event]) ? data.hooks[event] : [];
        const already = JSON.stringify(entries).includes("claude_hook.py");
        if (!already) entries.push({ hooks: [{ type: "command", command }] });
        data.hooks[event] = entries;
      }
    });
    console.log(`  ✓ hooks registered in ${file}`);
  } catch (e) {
    console.log(`  ⚠ hooks not registered: ${e.message}`);
    console.log(`     Add to ${file} by hand: "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "${command}"}]}], "PreCompact": [{"hooks": [{"type": "command", "command": "${command}"}]}]}`);
  }
}

const TOML_START = "# rlm-agent:start — managed by rlm-agent; this block is replaced on reinstall";
const TOML_END = "# rlm-agent:end";

function upsertCodexToml(base) {
  const file = join(base, "config.toml");
  const block = [
    TOML_START,
    "[mcp_servers.rlm]",
    `command = "${pythonBin()}"`,
    `args = ["${serverPath(base)}"]`,
    TOML_END,
  ].join("\n");
  let current = existsSync(file) ? readFileSync(file, "utf-8") : "";
  const s = current.indexOf(TOML_START);
  const e = current.indexOf(TOML_END);
  let next;
  if (s >= 0 && e > s) {
    next = current.slice(0, s) + block + current.slice(e + TOML_END.length);
  } else {
    next = current.trimEnd() + (current.trim() ? "\n\n" : "") + block + "\n";
  }
  mkdirSync(dirname(file), { recursive: true });
  writeFileSync(file, next);
  console.log(`  ✓ [mcp_servers.rlm] written to ${file}`);
}

// Hermes user plugins are opt-in via `plugins.enabled` in config.yaml. Add the
// `rlm` plugin to the allow-list so it actually loads. Handles both inline
// (`enabled: [a, b]`) and block (`enabled:\n  - a\n  - b`) YAML list styles.
function enableHermesPlugin(base) {
  const configPath = join(base, "config.yaml");
  if (!existsSync(configPath)) {
    throw new Error("Hermes config.yaml not found — plugin installed but not enabled. Run `hermes plugins enable rlm`.");
  }
  let cfg = readFileSync(configPath, "utf-8");
  const lines = cfg.split("\n");
  let inPlugins = false;
  let enabledIdx = -1;
  let enabledIndent = "";
  let enabledStyle = ""; // "inline" | "block"
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const m = line.match(/^(\s*)(\S.*)$/);
    if (!m) continue;
    const indent = m[1];
    const content = m[2];
    if (!inPlugins) {
      if (content === "plugins:") { inPlugins = true; continue; }
      continue;
    }
    // Inside plugins: section. A key at indent 0 ends it.
    if (indent.length === 0) break;
    if (content.startsWith("enabled:")) {
      enabledIdx = i;
      enabledIndent = indent;
      const rest = content.slice("enabled:".length).trim();
      enabledStyle = rest.startsWith("[") ? "inline" : "block";
      break;
    }
  }
  if (enabledIdx < 0) {
    // No plugins.enabled — add one under the plugins: section (or create it).
    const pluginsIdx = lines.findIndex((l) => l.trim() === "plugins:");
    if (pluginsIdx >= 0) {
      lines.splice(pluginsIdx + 1, 0, "  enabled: [rlm]");
    } else {
      lines.push("", "plugins:", "  enabled: [rlm]");
    }
  } else if (enabledStyle === "inline") {
    const line = lines[enabledIdx];
    const m = line.match(/^(\s*)enabled:\s*\[(.*?)\]\s*$/);
    if (m) {
      const items = m[2].split(",").map((s) => s.trim()).filter(Boolean);
      if (!items.includes("rlm")) items.push("rlm");
      lines[enabledIdx] = `${m[1]}enabled: [${items.join(", ")}]`;
    }
  } else {
    // Block style: enabled:\n  - a\n  - b. Add `  - rlm` after the last item.
    let insertAt = enabledIdx;
    for (let i = enabledIdx + 1; i < lines.length; i++) {
      const lm = lines[i].match(/^(\s*)- (.+)$/);
      if (lm && lm[1].length > enabledIndent.length) { insertAt = i; continue; }
      break;
    }
    const itemIndent = enabledIndent + "  ";
    lines.splice(insertAt + 1, 0, `${itemIndent}- rlm`);
  }
  writeFileSync(configPath, lines.join("\n"));
  console.log("  ✓ enabled in config.yaml (plugins.enabled includes rlm)");
}

async function main() {
  const args = process.argv.slice(2);
  const editorArg = args.indexOf("--editor");
  const dirArg = args.indexOf("--dir");
  const projectArg = args.indexOf("--project");
  const opts = { project: projectArg >= 0 ? resolve(args[projectArg + 1]) : null };

  const present = detectPresent();
  const allKeys = Object.keys(EDITORS);

  // Non-interactive mode
  if (editorArg >= 0) {
    const key = args[editorArg + 1];
    if (!EDITORS[key]) {
      console.error(`Unknown editor "${key}". Valid: ${allKeys.join(", ")}`);
      process.exit(1);
    }
    const base = dirArg >= 0 ? resolve(args[dirArg + 1]) : EDITORS[key].defaultDir();
    installEditor(key, base, opts);
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
    const defaultDir = def.defaultDir();
    const custom = (await ask(rl, `Install dir for ${def.label}`, defaultDir)).trim() || defaultDir;
    installEditor(key, resolve(custom), opts);
  }

  console.log("\nDone. RLM is installed. See README.md for usage.");
  rl.close();
}

main().catch((err) => {
  console.error("Install failed:", err);
  process.exit(1);
});
