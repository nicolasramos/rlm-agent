## RLM (recursive language model) — usage rules

You have an `rlm` MCP server. Its tools appear in your tool list prefixed
with the server name — look for the `rlm` / `ipython` entries and use the
exact names your session shows.

- **Store, don't paste**: any tool output, log, dataset or research digest
  that is large (> ~2K chars) or that you will touch more than once goes
  into the context lake with `rlm_store`. Read back only the slice you need
  with `rlm_search` / `rlm_find` / `rlm_get`. Never let bulk text sit in the
  prompt turn after turn.
- **Keep state in the kernel**: parsed values, counters, dataframes and
  intermediate results live in `ipython` variables that survive between
  calls (and across server restarts within the same session). Never re-read
  a file or re-compute a value you already hold. `%%bash` runs shell
  commands; `%cd` changes the kernel's working directory.
- **Persist across sessions**: before ending a session with durable state,
  call `rlm_snapshot`; in a new session call `rlm_restore` to reload it.
- **Do not**: paste a large blob into the prompt when the lake would hold
  it; re-run a command to recover data you already stored; store data you
  will never re-query (the lake is not a dumpster — `rlm_forget` cleans up).
