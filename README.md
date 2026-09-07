# rlm-agent

**RLM (Recursive Language Model) for any editor** — a persistent Python kernel, a context lake, and background subagents that keep large data **out of the LLM prompt**.

One repo, one installer, per-editor adapters. RLM is the paradigm from [arXiv:2512.24601](https://arxiv.org/abs/2512.24601): the model doesn't read the data — it writes Python. A persistent kernel executes the code, variables survive between calls, and large data lives in a context lake the model queries with tools.

## Supported editors

| Editor | Adapter | Mechanism |
|---|---|---|
| **OpenCode** | `adapters/opencode/rlm.ts` | Official plugin API (`@opencode-ai/plugin`) |
| **PI (pi-mono)** | `adapters/pi/rlm.ts` | Extension API (`pi.registerTool`) |
| **Hermes Agent** | `adapters/hermes/__init__.py` | Native Python plugin (`register(ctx)`) |

All three share the same `kernel/kernel.py` and the same context-lake JSONL format, so state is portable between editors.

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
```

### What gets installed

- **OpenCode** → `~/.config/opencode/plugins/rlm.ts` + `rlm-kernel/kernel.py`
- **PI** → `~/.pi/agent/extensions/rlm.ts` + `rlm-kernel/kernel.py`
- **Hermes** → `~/.hermes/plugins/rlm/` (plugin + kernel + manifest) and auto-enabled in `config.yaml`

Restart the editor (or `/reset` in Hermes) to activate.

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

Plus hooks: automatic kernel snapshot + variable summary injected into the **compaction prompt** (Hermes via `register_context_engine`; OpenCode via the compaction hook), RLM usage instructions in the **system prompt**, and kernel cleanup on session end.

## Architecture

```
rlm-agent/
├── kernel/kernel.py        # shared persistent Python kernel (JSON-lines over stdio)
├── adapters/
│   ├── opencode/rlm.ts     # OpenCode plugin
│   ├── pi/rlm.ts           # PI extension
│   └── hermes/             # Hermes plugin (__init__.py + plugin.yaml)
├── install.js              # interactive installer
└── package.json
```

```
Editor (OpenCode / PI / Hermes)
  └─ adapter (rlm.ts / __init__.py)
       ├─ ipython ──► kernel.py subprocess (JSON-lines over stdio)
       │               persistent namespace · %%bash · %cd · snapshot/restore
       ├─ rlm ─────► background subagent (editor-native)
       │               → handle immediately; child processes in background
       ├─ rlm_store/get/search/find/stats/forget ──► context lake (JSONL, per project)
       └─ compaction hook → snapshot + variable summary injected
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
# Test the shared kernel
python3 tests/test_kernel.py
```

## License

MIT
