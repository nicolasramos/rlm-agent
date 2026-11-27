# RLM usage rules (when to use the RLM system)

RLM = persistent kernel (`ipython`), context lake (`rlm_store` / `rlm_get` / `rlm_search` /
`rlm_find` / `rlm_stats` / `rlm_forget`), snapshot / restore (`rlm_snapshot` / `rlm_restore`),
state report (`rlm_inspect`). They are exposed by the `rlm` MCP server; in clients that
namespace MCP tools they appear as `mcp__rlm__ipython` and so on.

## Decision rule: RLM or normal flow?

Use RLM when **two or more** of these hold:

- **Large data**: the data exceeds ~10K chars or ~2.5K tokens (long logs, datasets, tool output,
  transcripts).
- **Re-use**: you will query the SAME data more than once (parse → filter → transform → summarize).
- **Multi-step state**: the task has 3+ steps sharing intermediate state (variables, partial
  results).
- **Long context**: the task will produce a lot of intermediate text you do not need to read in
  full.

## Which tool to use

| Situation | Tool | Example |
|---|---|---|
| State across steps of ONE task | `ipython` (kernel) | Load a log, filter it, count, summarize, all in kernel variables |
| Large data you will re-query or that must outlive the session | `rlm_store` + `rlm_search` / `rlm_find` | 5,000-line log: store it, find the needle with a regex |
| Before compaction / long session | `rlm_snapshot` | Persist the kernel namespace to disk |
| After compaction / restart | `rlm_restore` | Reload kernel variables from the snapshot |
| "What is in the kernel right now?" | `rlm_inspect` | Variable list, snapshot path, lake size |

## Anti-patterns (do NOT use RLM)

- One or two calls (a grep, a short file read, a simple question) → normal flow.
- Data used ONCE that fits in context → normal flow.
- The lake is not a bin: only store data you will re-read. Use `rlm_forget` to clean up.

## Verified gotchas

- `ipython` returns the value of the trailing expression. `print()` output is captured too, but
  prefer returning values so the output stays small.
- `%%bash` runs a shell command in a subprocess; its stdout is captured.
- `%cd <dir>` changes the kernel's working directory for later cells.
- The kernel is per editor session; variables survive between calls of the SAME session. Across
  restarts, use `rlm_snapshot` / `rlm_restore`.
- The lake is per project and shared by every editor: `~/.rlm/lake/<project-hash>.jsonl`.

## Subagents

An MCP server cannot spawn editor-native subagents. For recursive work use the editor's own
subagent/task feature and point it at the lake keys it needs, for example:
"Run this analysis; the data is in context-lake key `logs/2026-11-27`, read it with `rlm_get`."

## Shell fallback (any client)

When MCP is not available (or the client cannot load servers), the same kernel and the same lake are
reachable from a shell: `rlm exec -c "..."`, `rlm sh "..."`, `rlm store` / `get` / `search` / `find` /
`stats` / `forget`, `rlm inspect`. If `rlm` is not on the PATH, the installer leaves the same command
next to the MCP server (`python3 <install-dir>/rlm/rlm.py`). State is shared with MCP, so a variable
created through one path is visible from the other.
