#!/usr/bin/env node
/**
 * Tests for the rlm-agent installer/updater editor registry.
 *
 * Spec T3: temporary HOME with fake editor directories, route checks,
 * idempotency, invalid --editor, and the single editors list consumed by
 * both install.js and update.js. Runs in CI without any editor installed.
 */
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, "..");
const INSTALL = join(REPO, "install.js");

let tmpRoot = "";

function freshHome() {
  const home = join(tmpRoot, `home-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`);
  mkdirSync(home, { recursive: true });
  return home;
}

function runInstall(args, home) {
  const env = { ...process.env, HOME: home };
  return spawnSync("node", [INSTALL, ...args], { encoding: "utf-8", env, cwd: REPO });
}

function countMatches(text, re) {
  const m = text.match(re);
  return m ? m.length : 0;
}

// ─── Tests ──────────────────────────────────────────────────────────────────

function test_claude_install_files() {
  const home = freshHome();
  mkdirSync(join(home, ".claude"), { recursive: true }); // fake detection dir
  const base = join(home, "claude-plugins");
  const r = runInstall(["--editor", "claude", "--dir", base], home);
  if (r.status !== 0) throw new Error(`install exited ${r.status}: ${r.stderr}`);

  const plugin = join(base, "rlm-agent");
  const required = [
    ".claude-plugin/plugin.json",
    ".mcp.json",
    "hooks/hooks.json",
    "hooks/rlm_hook.py",
    "hooks/rlm_guard_hook.py",
    "skills/rlm-usage/SKILL.md",
    "rlm_mcp_server.py",
    "kernel/kernel.py",
  ];
  for (const rel of required) {
    const p = join(plugin, rel);
    if (!existsSync(p)) throw new Error(`missing installed file: ${rel}`);
  }

  // .mcp.json must reference the installed server by absolute path.
  const mcpJson = readFileSync(join(plugin, ".mcp.json"), "utf-8");
  if (!mcpJson.includes(join(plugin, "rlm_mcp_server.py"))) {
    throw new Error(".mcp.json does not reference the installed server path:\n" + mcpJson);
  }
  if (mcpJson.includes("__PLUGIN_DIR__")) {
    throw new Error(".mcp.json still contains the __PLUGIN_DIR__ placeholder");
  }
  const parsed = JSON.parse(mcpJson);
  if (parsed.mcpServers?.rlm?.timeout < 120000) {
    throw new Error(".mcp.json timeout must be generous (>= 120000 ms): " + JSON.stringify(parsed.mcpServers?.rlm));
  }
  console.log("PASS: test_claude_install_files");
}

function test_codex_install_config() {
  const home = freshHome();
  mkdirSync(join(home, ".codex"), { recursive: true }); // fake detection dir
  const base = join(home, "codex");
  mkdirSync(base, { recursive: true });
  // Pre-existing config with another server, to prove the merge is surgical.
  writeFileSync(join(base, "config.toml"), '[mcp_servers.other]\ncommand = "other"\n');
  const r = runInstall(["--editor", "codex", "--dir", base], home);
  if (r.status !== 0) throw new Error(`install exited ${r.status}: ${r.stderr}`);

  // Server + kernel copied next to the config dir.
  for (const rel of ["rlm-agent/mcp/rlm_mcp_server.py", "rlm-agent/kernel/kernel.py"]) {
    const p = join(base, rel);
    if (!existsSync(p)) throw new Error(`missing installed file: ${rel}`);
  }

  const cfg = readFileSync(join(base, "config.toml"), "utf-8");
  if (!cfg.includes("[mcp_servers.rlm]")) throw new Error("config.toml missing [mcp_servers.rlm]:\n" + cfg);
  if (!cfg.includes("[mcp_servers.other]")) throw new Error("pre-existing section was destroyed:\n" + cfg);
  if (!cfg.includes(join(base, "rlm-agent", "mcp", "rlm_mcp_server.py"))) {
    throw new Error("config.toml must reference the installed server by absolute path:\n" + cfg);
  }
  if (!/tool_timeout_sec\s*=\s*[0-9]{3,}/.test(cfg)) {
    throw new Error("config.toml must set a generous tool_timeout_sec (>= 100):\n" + cfg);
  }

  // Usage rules appended (or created) in AGENTS.md.
  const agents = readFileSync(join(base, "AGENTS.md"), "utf-8");
  if (!agents.includes("rlm-agent:begin")) throw new Error("AGENTS.md missing the rlm rules block begin marker");
  if (!agents.includes("rlm-agent:end")) throw new Error("AGENTS.md missing the rlm rules block end marker");
  console.log("PASS: test_codex_install_config");
}

function test_idempotency() {
  const home = freshHome();
  mkdirSync(join(home, ".claude"), { recursive: true });

  const claudeBase = join(home, "claude-plugins");
  for (let i = 0; i < 2; i++) {
    const r = runInstall(["--editor", "claude", "--dir", claudeBase], home);
    if (r.status !== 0) throw new Error(`claude install #${i + 1} exited ${r.status}: ${r.stderr}`);
  }
  const mcpJson = readFileSync(join(claudeBase, "rlm-agent", ".mcp.json"), "utf-8");
  if (countMatches(mcpJson, /"rlm"\s*:/g) !== 1) {
    throw new Error(".mcp.json duplicated the rlm server entry after re-install");
  }

  const codexDir = join(home, "codex-home");
  mkdirSync(codexDir, { recursive: true });
  for (let i = 0; i < 2; i++) {
    const r = runInstall(["--editor", "codex", "--dir", codexDir], home);
    if (r.status !== 0) throw new Error(`codex install #${i + 1} exited ${r.status}: ${r.stderr}`);
  }
  const cfg = readFileSync(join(codexDir, "config.toml"), "utf-8");
  if (countMatches(cfg, /^\[mcp_servers\.rlm\]\s*$/m) !== 1) {
    throw new Error("config.toml has a duplicated [mcp_servers.rlm] section:\n" + cfg);
  }
  const agents = readFileSync(join(codexDir, "AGENTS.md"), "utf-8");
  if (countMatches(agents, /rlm-agent:begin/g) !== 1) {
    throw new Error("AGENTS.md rules block duplicated after re-install");
  }
  console.log("PASS: test_idempotency");
}

function test_invalid_editor_lists_valid() {
  const home = freshHome();
  const r = runInstall(["--editor", "cursor"], home);
  if (r.status === 0) throw new Error("--editor cursor must exit non-zero");
  const out = (r.stdout + r.stderr).toLowerCase();
  if (!out.includes("claude") || !out.includes("codex")) {
    throw new Error(`invalid-editor output must list valid editors incl. claude and codex: ${out}`);
  }
  console.log("PASS: test_invalid_editor_lists_valid");
}

async function test_single_editors_registry() {
  // The registry must exist with all five editors...
  const mod = await import(join(REPO, "editors.js"));
  const keys = Object.keys(mod.EDITORS);
  for (const k of ["opencode", "pi", "hermes", "claude", "codex"]) {
    if (!keys.includes(k)) throw new Error(`editors.js missing editor "${k}": ${keys.join(", ")}`);
  }
  if (typeof mod.detectPresent !== "function") {
    throw new Error("editors.js must export detectPresent(home)");
  }

  // ...and update.js must consume it (no duplicated detection logic).
  const updateSrc = readFileSync(join(REPO, "update.js"), "utf-8");
  if (!updateSrc.includes("./editors.js")) {
    throw new Error("update.js does not import from ./editors.js (duplicated detection)");
  }
  const installSrc = readFileSync(join(REPO, "install.js"), "utf-8");
  if (!installSrc.includes("./editors.js")) {
    throw new Error("install.js does not import from ./editors.js (duplicated detection)");
  }

  // Detection: a fake home with only the two new editors present.
  const home = freshHome();
  mkdirSync(join(home, ".claude"), { recursive: true });
  mkdirSync(join(home, ".codex"), { recursive: true });
  const present = mod.detectPresent(home);
  if (!present.includes("claude") || !present.includes("codex")) {
    throw new Error(`detectPresent must find claude+codex in fake home, got: ${present.join(", ")}`);
  }
  if (present.includes("opencode") || present.includes("pi") || present.includes("hermes")) {
    throw new Error(`detectPresent false positive: ${present.join(", ")}`);
  }
  console.log("PASS: test_single_editors_registry");
}

// ─── Runner ─────────────────────────────────────────────────────────────────

tmpRoot = mkdtempSync(join(tmpdir(), "rlm-installer-test-"));
let passed = 0;
let failed = 0;
const tests = [
  test_claude_install_files,
  test_codex_install_config,
  test_idempotency,
  test_invalid_editor_lists_valid,
  test_single_editors_registry,
];
for (const t of tests) {
  try {
    await t();
    passed++;
  } catch (err) {
    console.log(`FAIL: ${t.name}: ${err.message}`);
    failed++;
  }
}
rmSync(tmpRoot, { recursive: true, force: true });
console.log(`\nResults: ${passed} passed, ${failed} failed out of ${tests.length}`);
process.exit(failed ? 1 : 0);
