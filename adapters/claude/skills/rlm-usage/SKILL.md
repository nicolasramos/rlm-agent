---
name: rlm-usage
description: Use when tasks involve large data or multi-step state.
---

# RLM Usage Rules (when to use the RLM system in Claude Code)

RLM = persistent Python kernel (`mcp__rlm__ipython`), context lake
(`mcp__rlm__rlm_store` / `mcp__rlm__rlm_get` / `mcp__rlm__rlm_search` /
`mcp__rlm__rlm_find` / `mcp__rlm__rlm_stats` / `mcp__rlm__rlm_forget`),
snapshot/restore (`mcp__rlm__rlm_snapshot` / `mcp__rlm__rlm_restore`).

All RLM tools in this session are provided by the `rlm` MCP server and appear
in your tool list with the `mcp__rlm__` prefix. The names above are the real
names for this session — use them exactly.

## Decision rule: RLM or normal flow?

Use RLM when **two or more** of these hold:

- **Big data**: the data exceeds ~10K chars or ~2.5K tokens (long logs,
  datasets, tool outputs, transcripts).
- **Re-use**: you will query the SAME data more than once in the task
  (parse → filter → transform → summarize).
- **Multi-step state**: the task has 3+ steps sharing intermediate state
  (variables, partial results).
- **Long context**: the task will produce a lot of intermediate text you do
  not need to see in full.

## Which tool to use

| Situation | Tool | Example |
|---|---|---|
| State between steps of ONE task | `mcp__rlm__ipython` (kernel) | Load a log, filter it, count, summarize — all in kernel variables |
| Big data that is re-queried or must survive the session | `mcp__rlm__rlm_store` + `mcp__rlm__rlm_search`/`mcp__rlm__rlm_find` | 5,000-line log: store in the lake, find the needle with a regex |
| Before compaction / long session | `mcp__rlm__rlm_snapshot` | Persist kernel state to disk (the PreCompact hook also does this) |
| After compaction / fresh session | `mcp__rlm__rlm_restore` | Reload variables from the snapshot |

## Anti-patterns (do NOT use RLM)

- 1–2 call tasks (a grep, a file read, a simple question) → normal flow.
- Data used ONCE that fits in context → normal flow.
- The lake is NOT a dumpster: only store data you will re-query.
  `mcp__rlm__rlm_forget` cleans up.

## Verified gotchas

- `mcp__rlm__ipython` captures `print()` output AND returns the trailing
  expression's repr; variables persist across calls and survive server
  restarts within the same editor session.
- `%%bash` runs a shell command (exit code as result); `%cd` changes the
  kernel's persistent working directory.
- `mcp__rlm__rlm_get` truncates entries to 50 KB; use `mcp__rlm__rlm_search`
  / `mcp__rlm__rlm_find` to locate the slice you need first.
- Oversized tool output (> 200 KB) is folded automatically into the lake by
  the PostToolUse guard; the digest carries a key — retrieve it with
  `mcp__rlm__rlm_get`.
