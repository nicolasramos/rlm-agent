# rlm-agent

**RLM (Recursive Language Model) for any editor** — a persistent Python kernel, a context lake, and background subagents that keep large data **out of the LLM prompt**.

One repo, one installer, per-editor adapters. RLM is the paradigm from [arXiv:2512.24601](https://arxiv.org/abs/2512.24601): the model doesn't read the data — it writes Python. A persistent kernel executes the code, variables survive between calls, and large data lives in a context lake the model queries with tools.

## Supported editors

| Editor | Adapter | Mechanism |
|---|---|---|
| **OpenCode** | `adapters/opencode/rlm.ts` | Official plugin API (`@opencode-ai/plugin`) |
| **PI (pi-mono)** | `adapters/pi/rlm.ts` | Extension API (`pi.registerTool`) |
| **Hermes Agent** | `adapters/hermes/__init__.py` | Native Python plugin (`register(ctx)`) |
| **Claude Code** | `adapters/mcp/server.py` | MCP stdio server + SessionStart/PreCompact hooks |
| **Codex** | `adapters/mcp/server.py` | MCP stdio server (`[mcp_servers.rlm]`) |
| **Copilot** (VS Code + Copilot CLI) | `adapters/mcp/server.py` | MCP stdio server (`mcpServers.rlm`) |

All of them share the same `kernel/kernel.py`, the same context lake and the same state directory, so work started in one editor can be finished in another.

## Install

```bash
npm i -g rlm-agent
rlm-agent-install
```

The installer is **interactive**: it detects which editors are present, lets you pick which one(s) to install to, and lets you override the install location for each.

Non-interactive (for scripts/CI):

```bash
rlm-agent-install --editor opencode                  # default location
rlm-agent-install --editor hermes --dir ~/.hermes
rlm-agent-install --editor claude                    # MCP server + hooks + CLAUDE.md rule
rlm-agent-install --editor codex                     # MCP server + config.toml entry + AGENTS.md rule
rlm-agent-install --editor copilot --project ~/code/my-repo   # MCP registration + repo instructions
```

`--editor` accepts several editors in one run only interactively; for scripts run it once per editor. Re-running is safe: every config edit is idempotent and everything it writes is wrapped in a marked block.

### What gets installed

- **OpenCode** → `~/.config/opencode/plugins/rlm.ts` + `rlm-kernel/kernel.py` (the plugin injects the RLM usage rule into the system prompt)
- **PI** → `~/.pi/agent/extensions/rlm.ts` + `rlm-kernel/kernel.py` + `AGENTS.md` (the usage rule, loaded as global context)
- **Hermes** → `~/.hermes/plugins/rlm/` (plugin + kernel + manifest), auto-enabled in `config.yaml`, plus the **`rlm-usage` skill** → `~/.hermes/skills/agent-workflow/rlm-usage/` (the decision rule that tells the agent *when* to use RLM — without it the plugin only describes the tools)
- **Claude Code** → `~/.claude/rlm/` (MCP server + kernel + hook shim), `mcpServers.rlm` registered with `claude mcp add --scope user`, the usage rule appended to `~/.claude/CLAUDE.md`, and `SessionStart` + `PreCompact` hooks merged into `~/.claude/settings.json`
- **Codex** → `~/.codex/rlm/` (MCP server + kernel), `[mcp_servers.rlm]` added to `~/.codex/config.toml` (via `codex mcp add` when the CLI is present), the usage rule in `~/.codex/AGENTS.md`
- **Copilot** → `~/.copilot/rlm/` (MCP server + kernel), `mcpServers.rlm` in `~/.copilot/mcp-config.json` (portable format, read by Copilot CLI and the Agent Host), the usage rule in `<project>/.github/copilot-instructions.md`

Restart the editor (or `/reset` in Hermes) to activate.

## Update

```bash
rlm-agent-update
```

Checks the npm registry for a newer version, reinstalls the package, and re-copies the adapter files to every detected editor (default install locations). If you used a custom `--dir` at install time, re-run `rlm-agent-install` manually after updating.

## Tools

| Tool | What it does | RLM aspect |
|---|---|---|
| `ipython` | Execute Python in a **persistent kernel**. Variables, imports, functions and results survive across calls. `%%bash` for shell, `%cd` for persistent cwd, top-level `await`, timeouts. | Programmatic execution |
| `rlm` | Spawn a **background subagent** and get an admission handle immediately. Results arrive later via `rlm_result`. Editor-native adapters only. | Recursive subagents |
| `rlm_list` / `rlm_result` | Manage children: list, read results. Editor-native adapters only. | Recursive subagents |
| `rlm_snapshot` / `rlm_restore` | Persist / reload the kernel namespace to disk. Survives compaction and restarts. | Durable state |
| `rlm_store` | Store large data in the **context lake** (per-project, persistent). Stored data is NOT in the prompt. | Context folding |
| `rlm_get` / `rlm_search` / `rlm_find` | Retrieve from the lake: by key, by regex, by text — snippets only, on demand. | Context folding |
| `rlm_stats` / `rlm_forget` | Lake statistics and cleanup. | Context folding |
| `rlm_inspect` | Report kernel variables, snapshot path and lake size without executing code. | Durable state |

Plus hooks: automatic kernel snapshot + variable summary injected into the **compaction prompt** (Hermes via `register_context_engine`; OpenCode via the compaction hook; Claude Code via the `PreCompact` hook shim), RLM usage instructions in the **system prompt** (or `CLAUDE.md` / `AGENTS.md` / `copilot-instructions.md`), and kernel cleanup on session end.

### Subagents in MCP clients

Claude Code, Codex and Copilot reach RLM through one MCP server, and an MCP server **cannot spawn editor-native subagents**. Those three therefore get the kernel, the lake and snapshot/restore, but not `rlm` / `rlm_list` / `rlm_result`. For recursive work, use the editor's own subagent feature and hand it lake keys ("the data is in key `logs/2026-11-27`, read it with `rlm_get`").

## State and the context lake

```
~/.rlm/
├── lake/<project-hash>.jsonl       # one lake per project, shared by every editor
└── state/<project-hash>/
    ├── summary.json                # kernel variable summary (read by the Claude Code hooks)
    └── kernel-state.pkl            # kernel snapshot (rlm_snapshot, autosnapshotted every 60 s)
```

Override the root with `RLM_HOME`; point one lake elsewhere with `RLM_LAKE_FILE`; pin the interpreter with `RLM_KERNEL_PYTHON`; disable autosnapshot with `RLM_AUTOSNAPSHOT_SECONDS=0`.

## Architecture

```
rlm-agent/
├── kernel/kernel.py        # shared persistent Python kernel (JSON-lines over stdio)
├── kernel/rlm_client.py    # shared kernel client + context lake (used by the MCP adapter)
├── adapters/
│   ├── opencode/rlm.ts     # OpenCode plugin
│   ├── pi/rlm.ts           # PI extension
│   ├── hermes/             # Hermes plugin (__init__.py + plugin.yaml)
│   └── mcp/                # MCP stdio server for Claude Code / Codex / Copilot
│       ├── server.py       # JSON-RPC over stdio, stdlib only
│       ├── claude_hook.py  # SessionStart + PreCompact hook shim
│       └── instructions.md # the usage rule installed as global instructions
├── install.js              # interactive installer
└── package.json
```

```
Editor (OpenCode / PI / Hermes)          Editor (Claude Code / Codex / Copilot)
  └─ adapter (rlm.ts / __init__.py)        └─ MCP client ──stdio JSON-RPC──► adapters/mcp/server.py
       ├─ ipython ──► kernel.py subprocess       ├─ ipython ──► kernel.py subprocess
       ├─ rlm ─────► background subagent          ├─ rlm_store/get/search/find/stats/forget ──► lake
       ├─ rlm_store/... ──► context lake          ├─ rlm_snapshot/restore ──► ~/.rlm/state
       └─ compaction hook → snapshot + summary    └─ hooks read ~/.rlm/state summary.json
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
python3 tests/test_kernel.py        # shared kernel
python3 tests/test_mcp_server.py    # MCP server + Claude Code hook shim (real stdio round-trips)
python3 adapters/mcp/server.py      # the server itself; speaks JSON-RPC on stdin/stdout
```

## License

MIT
