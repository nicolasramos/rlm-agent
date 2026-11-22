# rlm-agent

**RLM (Recursive Language Model) for any editor** — a persistent Python kernel, a context lake, and background subagents that keep large data **out of the LLM prompt**.

One repo, one installer, per-editor adapters. RLM is the paradigm from [arXiv:2512.24601](https://arxiv.org/abs/2512.24601): the model doesn't read the data — it writes Python. A persistent kernel executes the code, variables survive between calls, and large data lives in a context lake the model queries with tools.

## Supported editors

| Editor | Adapter | Mechanism | Subagents | Output guard |
|---|---|---|---|---|
| **OpenCode** | `adapters/opencode/rlm.ts` | Official plugin API (`@opencode-ai/plugin`) | ✅ native | — (compaction snapshot hook) |
| **PI (pi-mono)** | `adapters/pi/rlm.ts` | Extension API (`pi.registerTool`) | ✅ `pi --mode json` | ✅ (watch guard) |
| **Hermes Agent** | `adapters/hermes/__init__.py` | Native Python plugin (`register(ctx)`) | ✅ native lifecycle | ✅ (watch guard, 2K chars) |
| **Claude Code** | `adapters/claude/` (plugin) | MCP server (`mcp/rlm_mcp_server.py`) + hooks | — (see limitations) | ✅ (PostToolUse, > 200 KB) |
| **Codex CLI** | `adapters/codex/` (config) | MCP server (`[mcp_servers.rlm]` in `config.toml`) | — (see limitations) | — (pending, see limitations) |

The three in-process adapters and the two MCP adapters share the same `kernel/kernel.py` and the same context-lake JSONL format, so state is portable between editors.

## Install

```bash
npm i -g rlm-agent
rlm-agent-install
```

The installer is **interactive**: it detects which editors are present, lets you pick which one(s) to install to, and lets you override the install location for each.

Non-interactive (for scripts/CI):

```bash
rlm-agent-install --editor opencode            # default location
rlm-agent-install --editor hermes --dir ~/.hermes
rlm-agent-install --editor claude              # Claude Code plugin
rlm-agent-install --editor codex               # Codex CLI
```

### What gets installed

- **OpenCode** → `~/.config/opencode/plugins/rlm.ts` + `rlm-kernel/kernel.py` (the plugin injects the RLM usage rule into the system prompt)
- **PI** → `~/.pi/agent/extensions/rlm.ts` + `rlm-kernel/kernel.py` + `AGENTS.md` (the usage rule, loaded as global context)
- **Hermes** → `~/.hermes/plugins/rlm/` (plugin + kernel + manifest), auto-enabled in `config.yaml`, plus the **`rlm-usage` skill** → `~/.hermes/skills/agent-workflow/rlm-usage/` (the decision rule that tells the agent *when* to use RLM — without it the plugin only describes the tools)
- **Claude Code** → `~/.claude/plugins/rlm-agent/` — a self-contained plugin:
  - `.claude-plugin/plugin.json` (manifest)
  - `.mcp.json` — declares the `rlm` MCP server (`python3 …/rlm_mcp_server.py --surface core`, 300 s timeout) with absolute paths
  - `hooks/hooks.json` — PreCompact (kernel snapshot), SessionStart/compact (restore hint), PostToolUse (oversized-output guard)
  - `hooks/rlm_hook.py`, `hooks/rlm_guard_hook.py` — the hook scripts
  - `skills/rlm-usage/SKILL.md` — the usage rule with the real tool names of the session (`mcp__rlm__*`)
  - `rlm_mcp_server.py` + `kernel/kernel.py` — self-contained server and kernel
- **Codex** → `~/.codex/rlm-agent/` (MCP server + kernel, repo layout) plus:
  - `[mcp_servers.rlm]` merged into `~/.codex/config.toml` (idempotent; `tool_timeout_sec = 300`)
  - the usage rules block in `~/.codex/AGENTS.md` (between `rlm-agent:begin/end` markers, idempotent)

Restart the editor to activate. In Claude Code the tools appear as `mcp__rlm__ipython`, `mcp__rlm__rlm_store`, … (server-prefixed); in Codex they appear prefixed with the server name as well.

### How the MCP adapters stay persistent

Claude Code recycles its stdio MCP server processes between calls (upstream issue #51507, closed `not_planned`), so a kernel owned by the MCP server process would die with it — and RLM's whole point is state that survives. Therefore the kernel runs as a **detached daemon** (`kernel.py --socket`):

- On start, the MCP server re-attaches to the daemon for its project + editor session (session = the parent process id); variables survive server recycle/crash.
- A new editor session gets a clean kernel; `rlm_restore` reloads the last snapshot explicitly.
- The daemon exits when the editor session process dies, so it never outlives its session.

## Update

```bash
rlm-agent-update
```

Checks the npm registry for a newer version, reinstalls the package, and re-copies the adapter files to every detected editor (default install locations). The editor list is defined once in `editors.js` and shared by the installer and the updater. If you used a custom `--dir` at install time, re-run `rlm-agent-install` manually after updating.

## Tools

| Tool | What it does | RLM aspect |
|---|---|---|
| `ipython` | Execute Python in a **persistent kernel**. Variables, imports, functions and results survive across calls. `%%bash` for shell, `%cd` for persistent cwd, top-level `await`, timeouts. | Programmatic execution |
| `rlm` | Spawn a **background subagent** and get an admission handle immediately. Results arrive later via `rlm_result`. | Recursive subagents |
| `rlm_list` / `rlm_result` | Manage children: list, read results. | Recursive subagents |
| `rlm_snapshot` / `rlm_restore` | Persist / reload the kernel namespace to disk. Survives compaction and restarts. | Durable state |
| `rlm_store` | Store large data in the **context lake** (per-project, persistent). Stored data is NOT in the prompt. | Context folding |
| `rlm_get` / `rlm_search` / `rlm_find` | Retrieve from the lake: by key, by regex, by text — snippets only, on demand. | Context folding |
| `rlm_stats` / `rlm_forget` | Lake statistics and cleanup. | Context folding |

The MCP server exposes the same names: 9 core tools by default for the Claude Code / Codex adapters (`--surface core`), 12 with the subagent trio (`--surface full`, requires `RLM_SUBAGENT_CMD`).

Plus hooks: automatic kernel snapshot + variable summary injected into the **compaction prompt** (Hermes via `register_context_engine`; OpenCode via the compaction hook; Claude Code via the PreCompact/SessionStart hooks), RLM usage instructions in the **system prompt / AGENTS.md / skill**, and kernel cleanup on session end.

## Limitations (honest)

- **No programmatic subagents in Claude Code or Codex (v1).** Neither editor exposes a way for an MCP server to spawn a tracked child session with a handle, so the `rlm` / `rlm_result` / `rlm_list` trio is **not exposed** by the Claude Code and Codex adapters (`--surface core`). The OpenCode, PI and Hermes adapters keep theirs. If you want them via headless CLI (`claude -p` / `codex exec`), that is a separate task.
- **No oversized-output guard in Codex (pending).** The Codex hooks contract is stable, but replacing a tool's output from a hook has not been verified there; the guard ships for Claude Code only (PostToolUse `updatedToolOutput`, requires Claude Code ≥ 2.1.121).
- **Windows is not covered.** The kernel daemon uses Unix sockets; the `fix/windows-portability-and-install-route` branch is not merged into `main`.
- **Session = parent process id.** The kernel daemon is keyed by project + the editor session's pid. If an editor spawns the MCP server through a wrapper process that dies with the server, persistence degrades to per-server lifetime. `RLM_OWNER_PID` pins an explicit session id when needed.
- **Lake paths are per editor.** Each adapter family keeps its own state root (e.g. `~/.pi/agent/rlm-state/lake/`, `~/.rlm/lake/` for the MCP adapters); lakes are not shared across editors, by design.

## Architecture

```
rlm-agent/
├── kernel/kernel.py        # shared persistent Python kernel (stdio + socket transport)
├── mcp/rlm_mcp_server.py   # MCP stdio server wrapping kernel + lake (Claude Code, Codex)
├── adapters/
│   ├── opencode/rlm.ts     # OpenCode plugin
│   ├── pi/rlm.ts           # PI extension
│   ├── hermes/             # Hermes plugin (__init__.py + plugin.yaml)
│   ├── claude/             # Claude Code plugin (manifest, .mcp.json, hooks, skill)
│   └── codex/              # Codex rules block (config.toml is generated by the installer)
├── editors.js              # single editor registry (installer + updater)
├── install.js              # interactive installer
└── update.js               # updater
```

```
Editor (OpenCode / PI / Hermes)            Editor (Claude Code / Codex)
  └─ adapter (rlm.ts / __init__.py)          └─ MCP server (rlm_mcp_server.py, stdio)
       ├─ ipython ──► kernel.py subprocess      │    JSON-RPC 2.0
       │               (JSON-lines over stdio)  ├─ ipython ──► kernel.py DETACHED daemon
       ├─ rlm ─────► background subagent        │               (JSON-lines over Unix socket,
       │               (editor-native)          │                survives server recycle)
       ├─ rlm_store/... ──► context lake        └─ rlm_store/... ──► context lake (JSONL, per project)
       └─ compaction hook → snapshot + summary        hooks: PreCompact snapshot, PostToolUse guard
```

## Why RLM?

LLM agents degrade as context grows: cost rises linearly, performance drops ("context rot"), and every turn re-sends the same data. RLM treats context as **variables** instead of stuffing everything into the prompt:

```
Traditional:  Context (huge) → prompt → LLM → 💀 context rot, cost explosion
RLM:          Context → kernel / lake (external) → LLM calls tools to get only what it needs → ✅
```

Measured (2026-09-06): finding `NEEDLE-7A3F` in a 5,000-line log (~130K tokens) — small local models **cannot** process it in the prompt (prompt too long), but with RLM they succeed with **72–75 tokens in under 5 seconds**. Frontier models can, but pay 125K tokens/request; RLM does the same task in 387–1,374 tokens (a 90–1,700× reduction).

## Development

```bash
# Test the shared kernel (stdio protocol)
python3 tests/test_kernel.py
# Guard regression tests (Hermes + PI adapters)
python3 tests/test_guard.py
python3 tests/test_pi_guard.py
# MCP server + kernel socket mode (no editor needed)
python3 tests/test_mcp_server.py
# Installer/updater registry (node, no editor needed)
node tests/test_installer.mjs
```

## License

MIT
