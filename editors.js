/**
 * editors.js — SINGLE source of truth for the editor list, consumed by both
 * install.js and update.js (spec: one list, not two duplicated detections).
 *
 * Each editor definition:
 *   label       display name
 *   detect(home)      true when the editor looks present in this home dir
 *   defaultDir(home)  default install base dir
 *   files(base)       [{src, dest}] relative to the package root / base dir
 *   postInstall?(base)  extra work (config merge, placeholder rewrite)
 *   restart       message printed after install
 */
import { copyFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));

// ─── postInstall helpers ─────────────────────────────────────────────────────

// Claude Code: the plugin's .mcp.json and hooks.json are templates with a
// __PLUGIN_DIR__ placeholder; rewrite it to the absolute install location.
function rewriteClaudePlaceholders(pluginDir) {
  for (const rel of [".mcp.json", join("hooks", "hooks.json")]) {
    const p = join(pluginDir, rel);
    if (!existsSync(p)) continue;
    const text = readFileSync(p, "utf-8").replaceAll("__PLUGIN_DIR__", pluginDir);
    writeFileSync(p, text);
  }
}

// Codex: merge [mcp_servers.rlm] into config.toml (idempotent: replaces an
// existing section, keeps everything else).
function mergeCodexConfig(base) {
  const cfgPath = join(base, "config.toml");
  const serverDir = join(base, "rlm-agent");
  const section = [
    "[mcp_servers.rlm]",
    'command = "python3"',
    `args = ["${join(serverDir, "mcp", "rlm_mcp_server.py")}", "--surface", "core"]`,
    "tool_timeout_sec = 300",
    "startup_timeout_sec = 60",
    "",
  ].join("\n");

  let cfg = existsSync(cfgPath) ? readFileSync(cfgPath, "utf-8") : "";
  const lines = cfg.split("\n");
  let start = -1;
  for (let i = 0; i < lines.length; i++) {
    if (/^\s*\[mcp_servers\.rlm\]\s*$/.test(lines[i])) { start = i; break; }
  }
  if (start >= 0) {
    let end = lines.length;
    for (let i = start + 1; i < lines.length; i++) {
      if (/^\s*\[/.test(lines[i])) { end = i; break; }
    }
    lines.splice(start, end - start);
  }
  let out = lines.join("\n").trimEnd();
  if (out) out += "\n\n";
  out += section;
  writeFileSync(cfgPath, out);
}

// Codex: usage rules in AGENTS.md between markers (idempotent replace).
function appendCodexRules(base) {
  const rulesPath = join(base, "AGENTS.md");
  const src = readFileSync(join(here, "adapters", "codex", "RULES.md"), "utf-8");
  const block = `<!-- rlm-agent:begin -->\n${src.trim()}\n<!-- rlm-agent:end -->`;
  let content = existsSync(rulesPath) ? readFileSync(rulesPath, "utf-8") : "";
  const re = /<!-- rlm-agent:begin -->[\s\S]*?<!-- rlm-agent:end -->/;
  if (re.test(content)) {
    content = content.replace(re, block);
  } else {
    content = (content ? content.trimEnd() + "\n\n" : "") + block + "\n";
  }
  writeFileSync(rulesPath, content);
}

// ─── Editor registry ─────────────────────────────────────────────────────────

export const EDITORS = {
  opencode: {
    label: "OpenCode",
    detect: (home) => existsSync(join(home, ".config", "opencode")),
    defaultDir: (home) => join(home, ".config", "opencode"),
    files: (base) => [
      { src: join(here, "adapters", "opencode", "rlm.ts"), dest: join(base, "plugins", "rlm.ts") },
      { src: join(here, "kernel", "kernel.py"), dest: join(base, "rlm-kernel", "kernel.py") },
    ],
    restart: "Restart opencode to activate.",
  },
  pi: {
    label: "PI (pi-mono)",
    detect: (home) => existsSync(join(home, ".pi", "agent", "extensions")) || existsSync(join(home, ".pi")),
    defaultDir: (home) => join(home, ".pi", "agent"),
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
    detect: (home) => existsSync(join(home, ".hermes", "config.yaml")) || existsSync(join(home, ".hermes")),
    defaultDir: (home) => join(home, ".hermes"),
    files: (base) => [
      { src: join(here, "adapters", "hermes", "__init__.py"), dest: join(base, "plugins", "rlm", "__init__.py") },
      { src: join(here, "adapters", "hermes", "plugin.yaml"), dest: join(base, "plugins", "rlm", "plugin.yaml") },
      { src: join(here, "kernel", "kernel.py"), dest: join(base, "plugins", "rlm", "kernel.py") },
      // Usage skill: the decision rule that tells the agent WHEN to use RLM.
      { src: join(here, "adapters", "hermes", "skills", "rlm-usage", "SKILL.md"), dest: join(base, "skills", "agent-workflow", "rlm-usage", "SKILL.md") },
    ],
    restart: "Restart Hermes (or /reset in a session) to load the plugin.",
    postInstall: (base) => enableHermesPlugin(base),
  },
  claude: {
    label: "Claude Code",
    detect: (home) => existsSync(join(home, ".claude")) || existsSync(join(home, ".claude.json")),
    defaultDir: (home) => join(home, ".claude", "plugins"),
    files: (base) => [
      { src: join(here, "adapters", "claude", ".claude-plugin", "plugin.json"), dest: join(base, "rlm-agent", ".claude-plugin", "plugin.json") },
      { src: join(here, "adapters", "claude", ".mcp.json"), dest: join(base, "rlm-agent", ".mcp.json") },
      { src: join(here, "adapters", "claude", "hooks", "hooks.json"), dest: join(base, "rlm-agent", "hooks", "hooks.json") },
      { src: join(here, "adapters", "claude", "hooks", "rlm_hook.py"), dest: join(base, "rlm-agent", "hooks", "rlm_hook.py") },
      { src: join(here, "adapters", "claude", "hooks", "rlm_guard_hook.py"), dest: join(base, "rlm-agent", "hooks", "rlm_guard_hook.py") },
      { src: join(here, "adapters", "claude", "skills", "rlm-usage", "SKILL.md"), dest: join(base, "rlm-agent", "skills", "rlm-usage", "SKILL.md") },
      // The MCP server and kernel are copied into the plugin dir so the
      // installed plugin is self-contained (server finds ../kernel or kernel/).
      { src: join(here, "mcp", "rlm_mcp_server.py"), dest: join(base, "rlm-agent", "rlm_mcp_server.py") },
      { src: join(here, "kernel", "kernel.py"), dest: join(base, "rlm-agent", "kernel", "kernel.py") },
    ],
    postInstall: (base) => rewriteClaudePlaceholders(join(base, "rlm-agent")),
    restart: "Restart Claude Code (or start a new session) to activate.",
  },
  codex: {
    label: "Codex CLI",
    detect: (home) => existsSync(join(home, ".codex")) || existsSync(join(home, ".codex", "config.toml")),
    defaultDir: (home) => join(home, ".codex"),
    files: (base) => [
      { src: join(here, "mcp", "rlm_mcp_server.py"), dest: join(base, "rlm-agent", "mcp", "rlm_mcp_server.py") },
      { src: join(here, "kernel", "kernel.py"), dest: join(base, "rlm-agent", "kernel", "kernel.py") },
    ],
    postInstall: (base) => { mergeCodexConfig(base); appendCodexRules(base); },
    restart: "Restart Codex to activate.",
  },
};

export function detectPresent(home = homedir()) {
  return Object.entries(EDITORS)
    .filter(([, def]) => def.detect(home))
    .map(([key]) => key);
}

export function installEditor(key, baseDir) {
  const def = EDITORS[key];
  if (!def) {
    console.error(`Unknown editor "${key}". Valid: ${Object.keys(EDITORS).join(", ")}`);
    process.exit(1);
  }
  const files = def.files(baseDir);
  const created = [];
  for (const f of files) {
    mkdirSync(dirname(f.dest), { recursive: true });
    copyFileSync(f.src, f.dest);
    created.push(f.dest);
  }
  console.log(`\n✓ ${def.label} installed:`);
  for (const c of created) console.log(`  ${c}`);
  if (def.postInstall) {
    try {
      def.postInstall(baseDir);
    } catch (e) {
      console.log(`  ⚠ ${e.message}`);
    }
  }
  console.log(`  ${def.restart}`);
}

// ─── Hermes config.yaml enablement (unchanged from install.js) ──────────────

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
