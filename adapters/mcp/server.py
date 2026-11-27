#!/usr/bin/env python3
"""rlm MCP server — RLM (kernel + context lake) over the Model Context Protocol.

One stdio MCP server shared by every editor that speaks MCP:

  Claude Code   ~/.claude/rlm/server.py      (claude mcp add rlm -- python3 ...)
  Codex         ~/.codex/rlm/server.py       ([mcp_servers.rlm] in config.toml)
  Copilot       ~/.copilot/rlm/server.py     (mcp.json / mcp-config.json)

Tools: ipython, rlm_store, rlm_get, rlm_search, rlm_find, rlm_stats,
rlm_forget, rlm_snapshot, rlm_restore, rlm_inspect.

Design notes
-------------
* Zero dependencies: JSON-RPC 2.0 over newline-delimited stdio, stdlib only.
* One kernel process per server process; state lives in ~/.rlm so it is shared
  between editors and survives restarts.
* The server writes a state summary next to the snapshot after every cell so
  editor hooks that run in a SEPARATE process (Claude Code SessionStart /
  PreCompact) can report kernel variables without talking to the kernel.
* Background subagents are editor-native and cannot be driven from an MCP
  server; use the editor's own subagent feature instead. See README.

Run with --selftest for a dependency-free protocol check.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from rlm_client import (  # installed layout: kernel.py + rlm_client.py next to this file
        ContextLake, Kernel, LAKE_GET_MAX_CHARS, state_dir_for, snippet,
        rlm_home, project_key,
    )
except ImportError:  # repo layout: adapters/mcp/server.py + kernel/rlm_client.py
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "kernel"))
    from rlm_client import (  # type: ignore[no-redef]
        ContextLake, Kernel, LAKE_GET_MAX_CHARS, state_dir_for, snippet,
        rlm_home, project_key,
    )

SERVER_NAME = "rlm"
SERVER_VERSION = "0.2.0"
LATEST_PROTOCOL = "2025-06-18"
DEFAULT_TIMEOUT = 120
AUTOSNAPSHOT_SECONDS = int(os.environ.get("RLM_AUTOSNAPSHOT_SECONDS", "60"))

# ─── State (one server process = one editor session) ─────────────────────────

_kernel: Optional[Kernel] = None
_kernel_cwd: Optional[str] = None
_lakes: Dict[str, ContextLake] = {}


def _cwd() -> str:
    return _kernel_cwd or os.getcwd()


def _get_kernel() -> Kernel:
    global _kernel, _kernel_cwd
    if _kernel is None or _kernel.closed:
        _kernel_cwd = os.getcwd()
        _kernel = Kernel(_kernel_cwd)
    return _kernel


def _lake() -> ContextLake:
    cwd = _cwd()
    if cwd not in _lakes:
        _lakes[cwd] = ContextLake(cwd)
    return _lakes[cwd]


# ─── Sidecar state (read by editor hooks) ─────────────────────────────────────

def _state_file(name: str) -> Path:
    return state_dir_for(_cwd()) / name


def _write_summary(names: List[dict], cells: int) -> None:
    try:
        target = _state_file("summary.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({
            "project": _cwd(),
            "cells": cells,
            "names": names[:40],
            "updated": int(time.time() * 1000),
            "snapshot": str(_state_file("kernel-state.pkl")),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


_cells = 0
_last_snapshot = 0.0


def _after_cell() -> None:
    """Refresh the hook sidecar and (throttled) the kernel snapshot."""
    global _cells, _last_snapshot
    _cells += 1
    try:
        names = _get_kernel().list_names()
    except Exception:
        return
    _write_summary(names, _cells)
    if AUTOSNAPSHOT_SECONDS <= 0 or not names:
        return
    now = time.time()
    if now - _last_snapshot < AUTOSNAPSHOT_SECONDS:
        return
    try:
        _get_kernel().snapshot(str(_state_file("kernel-state.pkl")))
    except Exception:
        return
    _last_snapshot = now


# ─── Tool implementations ─────────────────────────────────────────────────────

def _format_execute(res: dict) -> str:
    if res.get("error"):
        err = res["error"]
        tb = "".join(err.get("traceback", [])).strip()
        return f"Error ({err.get('ename')}): {err.get('evalue')}" + (f"\n{tb}" if tb else "")
    parts = []
    if res.get("stdout"):
        parts.append(res["stdout"].rstrip())
    if res.get("stderr"):
        parts.append("[stderr]\n" + res["stderr"].rstrip())
    if res.get("result") and res["result"].get("repr"):
        parts.append(f"→ {res['result']['repr']}")
    return "\n".join(parts) or "(no output)"


def _ipython(args: dict) -> str:
    code = args.get("code", "")
    timeout = args.get("timeout") or DEFAULT_TIMEOUT
    try:
        out = _format_execute(_get_kernel().execute(code, int(timeout)))
    except Exception as exc:
        return f"ipython error: {exc}"
    _after_cell()
    return out


def _rlm_store(args: dict) -> str:
    key = args.get("key")
    if not key:
        return 'rlm_store requires a non-empty "key".'
    content = args.get("content", "")
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, default=str)
    entry = _lake().store(key, content, args.get("tags") or [], "model")
    return f'Stored "{key}" ({len(entry["content"])} chars). Not in the LLM context.'


def _rlm_get(args: dict) -> str:
    key = args.get("key")
    entry = _lake().get(key)
    if not entry:
        return f'No entry "{key}" in the context lake.'
    content = entry.get("content", "")
    if len(content) > LAKE_GET_MAX_CHARS:
        content = content[:LAKE_GET_MAX_CHARS] + "\n...[truncated]..."
    return f"[{key}] ({len(entry.get('content', ''))} chars)\n{content}"


def _rlm_search(args: dict) -> str:
    pattern = args.get("pattern", "")
    results = _lake().search(pattern, int(args.get("max_results") or 10))
    if not results:
        return f"No entries match /{pattern}/ in the context lake."
    return "\n".join(
        f"{i + 1}. {e['key']} ({len(e.get('content', ''))} chars)\n   {snippet(e, pattern)}"
        for i, e in enumerate(results)
    )


def _rlm_find(args: dict) -> str:
    text = args.get("text", "")
    results = _lake().find(text, int(args.get("max_results") or 10))
    if not results:
        return f'No entries contain "{text}" in the context lake.'
    return "\n".join(
        f"{i + 1}. {e['key']} ({len(e.get('content', ''))} chars)\n   {snippet(e, text)}"
        for i, e in enumerate(results)
    )


def _rlm_stats(args: dict) -> str:
    lake = _lake()
    s = lake.stats()
    if s["entries"] == 0:
        return "Context lake is empty. Store data with rlm_store."
    keys = "\n".join(f"- {k}" for k in s["keys"])
    return f"Context lake: {s['entries']} entries, {s['chars']:,} chars total.\nFile: {lake.file}\nKeys:\n{keys}"


def _rlm_forget(args: dict) -> str:
    pattern = args.get("pattern", "")
    removed = _lake().forget(pattern)
    if removed:
        return f"Removed {removed} entries matching /{pattern}/."
    return f"No entries match /{pattern}/."


def _rlm_snapshot(args: dict) -> str:
    if _kernel is None:
        return "No kernel state to snapshot (ipython not used yet)."
    file = _state_file("kernel-state.pkl")
    file.parent.mkdir(parents=True, exist_ok=True)
    try:
        ev = _kernel.snapshot(str(file))
    except Exception as exc:
        return f"Snapshot failed: {exc}"
    if ev.get("event") == "error":
        return f"Snapshot failed: {ev.get('evalue')}"
    return f"Snapshot saved to {file} — {len(ev.get('names', []))} variables."


def _rlm_restore(args: dict) -> str:
    file = _state_file("kernel-state.pkl")
    if not file.exists():
        return f"No snapshot found at {file}. Use rlm_snapshot first."
    try:
        ev = _get_kernel().restore(str(file))
    except Exception as exc:
        return f"Restore failed: {exc}"
    if ev.get("event") == "error":
        return f"Restore failed: {ev.get('evalue')}"
    names = ev.get("names", [])
    _write_summary([{"name": n, "type": "", "repr": ""} for n in names[:40]], _cells)
    return f"Restored {len(names)} variables from {file}."


def _rlm_inspect(args: dict) -> str:
    """Kernel variables + snapshot + lake summary in one call (no code executed)."""
    lines = [f"project: {_cwd()}", f"state dir: {state_dir_for(_cwd())}"]
    if _kernel is None:
        lines.append("kernel: not started (ipython not used yet)")
    else:
        try:
            names = _kernel.list_names()
        except Exception as exc:
            names = []
            lines.append(f"kernel: list failed ({exc})")
        lines.append(f"kernel: {len(names)} variables, {_cells} cells executed")
        for n in names[:40]:
            lines.append(f"- {n.get('name')} ({n.get('type')}): {n.get('repr')}")
    snapshot = _state_file("kernel-state.pkl")
    lines.append(f"snapshot: {snapshot} ({'present' if snapshot.exists() else 'absent'})")
    s = _lake().stats()
    lines.append(f"lake: {s['entries']} entries, {s['chars']:,} chars ({_lake().file})")
    return "\n".join(lines)


def _str(desc: str) -> dict:
    return {"type": "string", "description": desc}


def _num(desc: str) -> dict:
    return {"type": "number", "description": desc}


def _arr(desc: str) -> dict:
    return {"type": "array", "items": {"type": "string"}, "description": desc}


TOOLS: List[dict] = [
    {
        "name": "ipython",
        "description": (
            "Execute Python in a persistent kernel. Variables, imports and functions survive "
            "across calls. Use %%bash for shell, %cd for a persistent cwd, top-level await is "
            "allowed. Output is capped; return a value as the trailing expression to see it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": _str("Python code to execute in the persistent kernel"),
                "timeout": _num("Max seconds before the cell is interrupted (default 120)"),
            },
            "required": ["code"],
        },
        "handler": _ipython,
    },
    {
        "name": "rlm_store",
        "description": (
            "Store data in the persistent context lake. Stored data does NOT enter the prompt; "
            "retrieve it later with rlm_get / rlm_search / rlm_find."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": _str("Unique key for the entry"),
                "content": _str("Content to store (can be large)"),
                "tags": _arr("Optional tags"),
            },
            "required": ["key", "content"],
        },
        "handler": _rlm_store,
    },
    {
        "name": "rlm_get",
        "description": "Retrieve a context-lake entry by exact key (truncated to 50KB).",
        "inputSchema": {"type": "object", "properties": {"key": _str("Entry key")}, "required": ["key"]},
        "handler": _rlm_get,
    },
    {
        "name": "rlm_search",
        "description": "Regex-search the context lake; returns matching keys with a snippet each.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": _str("Regex pattern (case-insensitive)"),
                "max_results": _num("Max matches (default 10)"),
            },
            "required": ["pattern"],
        },
        "handler": _rlm_search,
    },
    {
        "name": "rlm_find",
        "description": "Find context-lake entries containing an exact text (case-insensitive).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": _str("Text to find"),
                "max_results": _num("Max matches (default 10)"),
            },
            "required": ["text"],
        },
        "handler": _rlm_find,
    },
    {
        "name": "rlm_stats",
        "description": "Context-lake statistics: entry count, total chars, keys and file path.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": _rlm_stats,
    },
    {
        "name": "rlm_forget",
        "description": "Delete context-lake entries whose key or content matches a regex.",
        "inputSchema": {"type": "object", "properties": {"pattern": _str("Regex pattern")}, "required": ["pattern"]},
        "handler": _rlm_forget,
    },
    {
        "name": "rlm_snapshot",
        "description": "Persist the kernel namespace to disk. Use before compaction or a restart.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": _rlm_snapshot,
    },
    {
        "name": "rlm_restore",
        "description": "Reload the kernel namespace from the last snapshot (after compaction or restart).",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": _rlm_restore,
    },
    {
        "name": "rlm_inspect",
        "description": "Report kernel variables, snapshot path and lake size without executing code.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": _rlm_inspect,
    },
]

_TOOL_INDEX = {t["name"]: t for t in TOOLS}


# ─── JSON-RPC plumbing ────────────────────────────────────────────────────────

def _write(msg: dict) -> None:
    try:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def _ok(rid: Any, result: dict) -> None:
    _write({"jsonrpc": "2.0", "id": rid, "result": result})


def _err(rid: Any, code: int, message: str, data: Any = None) -> None:
    error: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    _write({"jsonrpc": "2.0", "id": rid, "error": error})


def _text_result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def handle(req: dict) -> None:
    method = req.get("method")
    rid = req.get("id")
    params = req.get("params") or {}
    is_notification = "id" not in req

    if method == "initialize":
        _ok(rid, {
            "protocolVersion": params.get("protocolVersion") or LATEST_PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "RLM kernel + context lake. Keep large data out of the prompt: generate or load "
                "it inside ipython, store what you will re-read with rlm_store, and query the "
                "lake with rlm_search / rlm_find / rlm_get."
            ),
        })
        return

    if is_notification:  # notifications/initialized, notifications/cancelled, ...
        return

    if method == "ping":
        _ok(rid, {})
        return

    if method == "tools/list":
        _ok(rid, {"tools": [{k: t[k] for k in ("name", "description", "inputSchema")} for t in TOOLS]})
        return

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = _TOOL_INDEX.get(name or "")
        if tool is None:
            _ok(rid, _text_result(f"Unknown tool: {name}", is_error=True))
            return
        try:
            text = tool["handler"](args)
            _ok(rid, _text_result(text))
        except Exception as exc:
            tb = traceback.format_exc(limit=3)
            _ok(rid, _text_result(f"{name} failed: {exc}\n{tb}", is_error=True))
        return

    if method == "shutdown":
        _shutdown_kernel()
        _ok(rid, {})
        return

    _err(rid, -32601, f"Method not found: {method}")


def _shutdown_kernel() -> None:
    global _kernel
    if _kernel is not None:
        try:
            _kernel.shutdown()
        except Exception:
            pass
        _kernel = None


def main() -> int:
    # Nothing but protocol frames may reach stdout.
    signal_out = sys.stdout
    sys.stdout = sys.stderr  # type: ignore[assignment]
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(req, dict):
                continue
            saved, sys.stdout = sys.stdout, signal_out
            try:
                handle(req)
            finally:
                sys.stdout = saved
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown_kernel()
    return 0


if __name__ == "__main__":
    sys.exit(main())
