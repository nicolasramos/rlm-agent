#!/usr/bin/env node
/**
 * rlm-agent-update — check the npm registry for a newer rlm-agent, reinstall
 * the package, and re-copy the adapter files to every detected editor.
 *
 * Usage:
 *   rlm-agent-update            # check + update + re-install files
 *   rlm-agent-update --dir X    # use X as the base dir for every editor
 *
 * The update uses the DEFAULT install locations for each editor. If you
 * installed with a custom --dir, re-run rlm-agent-install manually after
 * updating.
 */
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const pkg = JSON.parse(readFileSync(join(here, "package.json"), "utf-8"));
const current = pkg.version;
const PACKAGE = pkg.name;

function semverCmp(a, b) {
  const pa = String(a).split(".").map(Number);
  const pb = String(b).split(".").map(Number);
  for (let i = 0; i < 3; i++) {
    const x = pa[i] || 0;
    const y = pb[i] || 0;
    if (x > y) return 1;
    if (x < y) return -1;
  }
  return 0;
}

function hermesHome() {
  // Same resolution as install.js: HERMES_HOME wins, then the Windows
  // %LOCALAPPDATA%\hermes default, then the legacy ~/.hermes.
  if (process.env.HERMES_HOME) return resolve(process.env.HERMES_HOME);
  if (process.platform === "win32" && process.env.LOCALAPPDATA) {
    const p = join(process.env.LOCALAPPDATA, "hermes");
    if (existsSync(p)) return p;
  }
  return join(homedir(), ".hermes");
}

function detectEditors(base) {
  const present = [];
  if (existsSync(join(base, ".config", "opencode"))) present.push("opencode");
  if (existsSync(join(base, ".pi", "agent", "extensions")) || existsSync(join(base, ".pi"))) present.push("pi");
  if (existsSync(hermesHome())) present.push("hermes");
  return present;
}

const args = process.argv.slice(2);
const dirIdx = args.indexOf("--dir");
const base = dirIdx >= 0 ? resolve(args[dirIdx + 1]) : homedir();

// 1. Check the registry for a newer version.
let latest = current;
try {
  const res = spawnSync("npm", ["view", PACKAGE, "version"], { encoding: "utf-8", timeout: 15000 });
  if (res.status === 0 && res.stdout.trim()) {
    latest = res.stdout.trim();
  } else {
    console.warn("⚠ could not reach the npm registry — using the installed version.");
  }
} catch {
  console.warn("⚠ could not reach the npm registry — using the installed version.");
}

if (semverCmp(latest, current) <= 0) {
  console.log(`rlm-agent is up to date (${current}).`);
  process.exit(0);
}

console.log(`rlm-agent ${current} → ${latest}. Updating…`);
const inst = spawnSync("npm", ["i", "-g", `${PACKAGE}@latest`], { stdio: "inherit" });
if (inst.status !== 0) {
  console.error("✗ npm install failed — update aborted.");
  process.exit(1);
}

// 2. Re-copy adapter files to every detected editor (default locations).
const present = detectEditors(base);
if (present.length === 0) {
  console.log("No supported editor detected — package updated, files not copied.");
  process.exit(0);
}
for (const key of present) {
  // --dir is only forwarded when the user actually passed one; otherwise each
  // editor resolves its own default (homing every editor at $HOME would
  // install opencode to ~/plugins/rlm.ts and miss the real HERMES_HOME).
  const installArgs = ["--editor", key];
  if (dirIdx >= 0) installArgs.push("--dir", base);
  const r = spawnSync("rlm-agent-install", installArgs, { stdio: "inherit" });
  if (r.status !== 0) console.warn(`⚠ re-install for ${key} failed.`);
}
console.log("rlm-agent updated. Restart your editor(s) to activate.");
