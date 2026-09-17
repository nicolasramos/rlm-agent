# Fix report — Hermes adapter on Windows: install route + runtime bugs

Date: 2026-09-17 · Branch: `fix/windows-portability-and-install-route` · Host: Windows 11, Python 3.13.5, Hermes desktop

This document records the failure modes found on a real Windows install, the root cause of each, and the verification performed for the fixes in this branch. It doubles as the PR body.

## Symptom summary

RLM was "installed" on a Windows host (plugin loaded, 12 tools visible) yet never used in practice:

1. `ipython` returned only the last-expression repr — every `print()` disappeared.
2. `rlm` (subagent tool) failed with `'str' object has no attribute 'get'`.
3. `rlm-agent-update` reported success while nothing live changed.
4. The published npm kernel **cannot start on Windows at all** (only the pre-`fcntl` local copies ran).

## Root causes

### 1. Installer/updater wrote to a home the runtime never reads

`install.js` / `update.js` hardcoded `~/.hermes`. On Windows the Hermes runtime home is `%LOCALAPPDATA%\hermes` (`HERMES_HOME`, resolved by `hermes_constants.get_hermes_home()`), so:

- `rlm-agent-install` placed the plugin in a legacy directory → it never loads.
- `rlm-agent-update` re-copied files to the same dead path → "update succeeded", runtime unchanged.
- The `rlm-usage` skill went to `~/.hermes/skills/agent-workflow/` → never discovered.

A secondary defect: `update.js` passed `--dir <homedir>` for every editor, so a *default* update installed OpenCode's plugin into `~/plugins/rlm.ts` instead of `~/.config/opencode/plugins/`.

### 2. Block-list YAML duplication

`enableHermesPlugin()` appended `- rlm` to a block-style `plugins.enabled` list without checking membership, producing duplicate entries on every re-run. (The inline-list branch already guarded this; the block branch did not.)

### 3. Kernel: ungated `import fcntl` breaks Windows at startup

The locked lake rewrite introduced `import fcntl` at module top level. Windows has no `fcntl` → `ModuleNotFoundError` before the `ready` frame → the kernel never starts. The published npm build carries this bug; local copies that predate it ran because they lack the locked rewrite entirely.

### 4. Adapter: stdout attribution mismatch (published builds)

The kernel emitted stream events with `"id": null` while the adapter accumulated output keyed by request id, so all `print()` text was dropped silently. Fixed upstream by the `_current_cell` contextvar; this report records it because published builds still carry it and the symptom (silent loss, not an error) makes it hard to spot.

### 5. Adapter: `rlm` subagent called `delegate_task` without a parent

`delegate_task` is an agent-loop tool (`_AGENT_LOOP_TOOLS`) that plugins cannot invoke; called without `parent_agent` it returns a `tool_error` **string**, and `resp.get(...)` then raised `'str' object has no attribute 'get'`. Correct route: `ctx.subagent_lifecycle` + `SubagentLaunchRequest` (already used by current upstream).

### 6. Adapter: registering a no-op context engine

`register_context_engine()` replaces the built-in `ContextCompressor`. The plugin's engine returned `should_compress() -> False`, i.e. selecting it would **disable automatic compaction**. The default `context.engine: compressor` never selects it, so the registration was inert in the default setup — dead weight that turns destructive the moment a user opts in. Removed.

### 7. Watch guard: folding `rlm_get` results defeats retrieval

The guard folds any tool result above the threshold. Folding an `rlm_get` result returns a digest pointing at a *new* key whose retrieval is folded too — an unbounded loop in which no lake entry can ever be read beyond head+tail. `rlm_get` is now exempt (it is the explicit retrieval path, already capped at 50 KB).

### 8. State paths hardcoded to the legacy home

Lake files and snapshots were written under `~/.hermes/rlm-state/`, outside `HERMES_HOME` — invisible to profiles and lost on machines where the runtime home differs.

## Changes in this branch

| Area | Change |
|---|---|
| `kernel/kernel.py` | `fcntl` degrades to `None`; lock skipped when unavailable. Verified by a test that imports the kernel with `fcntl` blocked. |
| `adapters/hermes/__init__.py` | State root under the Hermes home (`HERMES_HOME` → `hermes_constants` default → legacy), one-shot non-destructive legacy-state merge; interpreter probe requires a working `--version` with `sys.executable` fallback; context-engine registration removed (documented); `rlm_get` exempt from folding; digest marker in English. |
| `install.js` | Hermes home resolution (`HERMES_HOME` → `%LOCALAPPDATA%\hermes` → `~/.hermes`); block-list dedupe. |
| `update.js` | Same home resolution; `--dir` forwarded only when explicitly passed. |
| `plugin.yaml` | Version 0.1.2; `provides_hooks` matches what is actually registered. |
| `README.md` | Documents `<HERMES_HOME>` resolution and the real hooks (no context engine). |
| `skills/rlm-usage` | Drops stale gotchas (stdout IS captured now), documents the automatic watch guard and the `rlm_get` exemption. |
| `tests/` | Kernel import without `fcntl`; `rlm_get` folding exemption; oversized other results still fold (in-memory lake double). |

## Verification performed

- `tests/test_kernel.py` → 7/7 PASS on Windows (py3.13), including the new no-`fcntl` import test.
- `tests/test_guard.py` → 12/12 PASS, including the two new folding tests.
- Raw protocol probe (JSON-lines over stdio): `print()` arrives as `{"event":"stdout","id":"<rid>"}` **before** the result frame, on the patched kernel.
- End-to-end adapter↔kernel on Windows: prints, stderr, tracebacks, variable persistence, `%%bash` stdout, `%cd` — all verified.
- Installer dry-runs in three homes (block list with `rlm` present → no duplicate; fresh home via `HERMES_HOME` → created + enabled; inline list → appended correctly).
- `hermes plugins doctor` + `validate` on the installed plugin: 12 tools, 2 hooks, no collisions.
- Legacy-state merge: pre-existing `somit/repo-inventory` entry (473 chars) and snapshot recovered under the new root; nothing deleted.

## Deployment notes

- **Publish required**: the npm package (0.1.1) still ships the broken kernel + old adapter. Bump and `npm publish` to deliver these fixes to `rlm-agent-install` users.
- Users who installed before this fix should re-run `rlm-agent-install --editor hermes` (it now resolves the right home) or sync the two adapter files manually.
- No context engine is registered by design; kernel durability rides on `on_session_end` + `rlm_snapshot`/`rlm_restore`, and context folding on the `transform_tool_result` guard.
