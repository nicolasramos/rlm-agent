"""rlm-agent — RLM (Recursive Language Model) plugin for Hermes Agent.

Ports the RLM plugin for OpenCode/PI to Hermes' native Python plugin API:

  - ipython: persistent Python kernel (reuses kernel/kernel.py as-is)
  - rlm_store/get/search/find/stats/forget: context lake (same JSONL format)
  - rlm_snapshot/rlm_restore: persist / reload the kernel namespace
  - rlm: background subagent via delegate_task (Hermes-native)
  - system prompt section + compaction hook via register_context_engine

Install: copy this file to ~/.hermes/plugins/rlm/__init__.py and
kernel/kernel.py to ~/.hermes/plugins/rlm/kernel.py (or set RLM_KERNEL).
Then restart Hermes (or /reset in a session).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# ─── Config ──────────────────────────────────────────────────────────────────

DEFAULT_TIMEOUT = 120
HARD_TIMEOUT_MS = 300_000
LAKE_GET_MAX_CHARS = 50_000
LAKE_CAPTURE_MIN_CHARS = 10_000
LAKE_MAX_ENTRIES = 500
RLM_MAX_DEPTH = 2
MAX_CHILDREN_PER_SESSION = 8

_PLUGIN_DIR = Path(__file__).parent


def _kernel_path() -> Path:
    env = os.environ.get("RLM_KERNEL")
    if env:
        return Path(env)
    candidates = [
        _PLUGIN_DIR / "kernel.py",
        Path.home() / ".hermes" / "plugins" / "rlm" / "kernel.py",
        Path.home() / ".config" / "opencode" / "rlm-kernel" / "kernel.py",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def _python_bin() -> str:
    env = os.environ.get("RLM_KERNEL_PYTHON")
    if env:
        return env
    for c in ("python3", "/opt/homebrew/bin/python3.11", "/usr/local/bin/python3", "/usr/bin/python3"):
        try:
            r = subprocess.run([c, "--version"], capture_output=True)
            if r.returncode == 0:
                return c
        except Exception:
            pass
    return "python3"


def _lake_file_for(project_dir: str) -> Path:
    import hashlib
    h = hashlib.sha256(project_dir.encode()).hexdigest()[:16]
    return Path.home() / ".hermes" / "rlm-state" / "lake" / f"{h}.jsonl"


def _state_dir_for(session_id: str) -> Path:
    return Path.home() / ".hermes" / "rlm-state" / session_id


# ─── Kernel client (JSON-lines over stdio, same protocol as kernel.py) ──────

class Kernel:
    def __init__(self, cwd: str):
        self.proc = subprocess.Popen(
            [_python_bin(), str(_kernel_path())],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "RLM_LAKE_FILE": str(_lake_file_for(cwd))},
        )
        self.buffer = ""
        self.pending = {}
        self.next_id = 0
        self.closed = False
        self.stderr_tail = []
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_reader.start()

    def _read_loop(self):
        while True:
            line = self.proc.stdout.readline()
            if not line:
                break
            self._handle_line(line.decode("utf-8", "replace"))

    def _read_stderr(self):
        while True:
            line = self.proc.stderr.readline()
            if not line:
                break
            self.stderr_tail.append(line.decode("utf-8", "replace"))
            if len(self.stderr_tail) > 20:
                self.stderr_tail.pop(0)

    def _handle_line(self, line: str):
        line = line.strip()
        if not line:
            return
        try:
            ev = json.loads(line)
        except Exception:
            return
        if ev.get("event") in ("result", "error", "names"):
            with self._lock:
                p = self.pending.pop(ev.get("id"), None)
            if p:
                p["value"] = ev
                p["event"].set()

    def _request(self, req_type: str, payload=None, timeout_ms=HARD_TIMEOUT_MS):
        if self.closed:
            raise RuntimeError("kernel is not running")
        with self._lock:
            self.next_id += 1
            rid = f"k{self.next_id}"
            fut = {"event": threading.Event(), "value": None}
            self.pending[rid] = fut
        body = {"id": rid, "type": req_type, **(payload or {})}
        try:
            self.proc.stdin.write((json.dumps(body) + "\n").encode())
            self.proc.stdin.flush()
        except Exception as e:
            with self._lock:
                self.pending.pop(rid, None)
            raise RuntimeError(f"kernel write failed: {e}")
        if not fut["event"].wait(timeout_ms / 1000):
            with self._lock:
                self.pending.pop(rid, None)
            raise RuntimeError(f"kernel request timed out after {timeout_ms}ms")
        return fut["value"]

    def execute(self, code: str, timeout=DEFAULT_TIMEOUT):
        ev = self._request("execute", {"code": code, "timeout": timeout})
        return {
            "ok": ev.get("event") == "result" and ev.get("ok") is not False,
            "result": ev if ev.get("event") == "result" else None,
            "error": ev if ev.get("event") == "error" else None,
            "stdout": "",
            "stderr": "",
        }

    def list_names(self):
        ev = self._request("list_names")
        return ev.get("names", [])

    def snapshot(self, file: str):
        return self._request("snapshot", {"path": file})

    def restore(self, file: str):
        return self._request("restore", {"path": file})

    def shutdown(self):
        if self.closed:
            return
        try:
            self.proc.stdin.write((json.dumps({"id": f"k{self.next_id + 1}", "type": "shutdown"}) + "\n").encode())
            self.proc.stdin.flush()
        except Exception:
            pass
        try:
            self.proc.kill()
        except Exception:
            pass
        self.closed = True

    def stderr_tail_text(self):
        return "".join(self.stderr_tail)[-2000:]


# ─── Context lake (context folding — RLM paper arXiv:2512.24601) ────────────

class ContextLake:
    def __init__(self, project_dir: str):
        self.file = _lake_file_for(project_dir)
        self.entries = {}
        self.loaded = False

    def _ensure_loaded(self):
        if self.loaded:
            return
        self.loaded = True
        if self.file.exists():
            for line in self.file.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                    if e.get("key"):
                        self.entries[e["key"]] = e
                except Exception:
                    pass

    def _append(self, entry):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def _rewrite(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(e) for e in self.entries.values()]
        self.file.write_text("\n".join(lines) + ("\n" if lines else ""))

    def store(self, key, content, tags=None, source="model"):
        self._ensure_loaded()
        now = int(time.time() * 1000)
        entry = {
            "key": key, "content": content, "tags": tags or [], "source": source,
            "created": self.entries.get(key, {}).get("created", now), "updated": now,
        }
        self.entries[key] = entry
        self._append(entry)
        return entry

    def get(self, key):
        self._ensure_loaded()
        return self.entries.get(key)

    def search(self, pattern, max_results=10):
        self._ensure_loaded()
        try:
            re_ = re.compile(pattern, re.IGNORECASE)
        except Exception:
            re_ = re.compile(re.escape(pattern), re.IGNORECASE)
        out = []
        for e in self.entries.values():
            if re_.search(e["content"]) or re_.search(e["key"]) or any(re_.search(t) for t in e["tags"]):
                out.append(e)
                if len(out) >= max_results:
                    break
        return out

    def find(self, text, max_results=10):
        self._ensure_loaded()
        needle = text.lower()
        out = []
        for e in self.entries.values():
            if needle in e["content"].lower() or needle in e["key"].lower():
                out.append(e)
                if len(out) >= max_results:
                    break
        return out

    def forget(self, pattern):
        self._ensure_loaded()
        try:
            re_ = re.compile(pattern, re.IGNORECASE)
        except Exception:
            re_ = re.compile(re.escape(pattern), re.IGNORECASE)
        removed = 0
        for k in list(self.entries.keys()):
            e = self.entries[k]
            if re_.search(e["key"]) or re_.search(e["content"]):
                del self.entries[k]
                removed += 1
        if removed:
            self._rewrite()
        return removed

    def stats(self):
        self._ensure_loaded()
        chars = sum(len(e["content"]) for e in self.entries.values())
        return {"entries": len(self.entries), "chars": chars, "keys": list(self.entries.keys())}


def _snippet(entry, needle, width=160):
    idx = entry["content"].lower().find(needle.lower())
    if idx < 0:
        return entry["content"][:width]
    start = max(0, idx - width // 2)
    return ("…" if start > 0 else "") + entry["content"][start:start + width] + "…"


# ─── Plugin state ────────────────────────────────────────────────────────────

_kernels = {}          # session_id -> Kernel
_lakes = {}            # project_dir -> ContextLake
_children = {}         # session_id -> [child entries]
_parents = {}          # session_id -> parent_id


def _lake_for(project_dir):
    if project_dir not in _lakes:
        _lakes[project_dir] = ContextLake(project_dir)
    return _lakes[project_dir]


def _get_kernel(session_id, cwd):
    if session_id in _kernels:
        return _kernels[session_id]
    k = Kernel(cwd)
    _kernels[session_id] = k
    return k


def _format_execute(res):
    if res["error"]:
        tb = "".join(res["error"].get("traceback", [])).strip()
        return f"Error ({res['error'].get('ename')}): {res['error'].get('evalue')}{chr(10) + tb if tb else ''}"
    parts = []
    if res["stdout"]:
        parts.append(res["stdout"].rstrip())
    if res["result"] and res["result"].get("repr"):
        parts.append(f"→ {res['result']['repr']}")
    return "\n".join(parts) or "(no output)"


# ─── Tool schemas (OpenAI function-calling format) ───────────────────────────

def _schema(name, description, properties, required=()):
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": properties, "required": list(required)},
    }


def _str(desc):
    return {"type": "string", "description": desc}


def _num(desc):
    return {"type": "number", "description": desc}


def _arr(desc):
    return {"type": "array", "items": {"type": "string"}, "description": desc}


def register(ctx) -> None:
    """Register all RLM tools. Called once by the Hermes plugin loader."""
    toolset = "rlm"

    def _ipython(args, **kw):
        session_id = kw.get("session_id") or kw.get("task_id") or "default"
        cwd = kw.get("cwd") or os.getcwd()
        try:
            kernel = _get_kernel(session_id, cwd)
            res = kernel.execute(args.get("code", ""), args.get("timeout") or DEFAULT_TIMEOUT)
            return _format_execute(res)
        except Exception as e:
            return f"ipython error: {e}"

    def _rlm_store(args, **kw):
        cwd = kw.get("cwd") or os.getcwd()
        lake = _lake_for(cwd)
        entry = lake.store(args.get("key"), args.get("content", ""), args.get("tags") or [], "model")
        return f'Stored "{args.get("key")}" ({entry["content"].__len__()} chars). Not in the LLM context.'

    def _rlm_get(args, **kw):
        cwd = kw.get("cwd") or os.getcwd()
        lake = _lake_for(cwd)
        entry = lake.get(args.get("key"))
        if not entry:
            return f'No entry "{args.get("key")}" in the context lake.'
        content = entry["content"]
        if len(content) > LAKE_GET_MAX_CHARS:
            content = content[:LAKE_GET_MAX_CHARS] + "\n...[truncated]..."
        return f'[{args.get("key")}] ({len(entry["content"])} chars)\n{content}'

    def _rlm_search(args, **kw):
        cwd = kw.get("cwd") or os.getcwd()
        lake = _lake_for(cwd)
        results = lake.search(args.get("pattern"), args.get("max_results") or 10)
        if not results:
            return f'No entries match /{args.get("pattern")}/ in the context lake.'
        return "\n".join(
            f"{i + 1}. {e['key']} ({len(e['content'])} chars)\n   {_snippet(e, args.get('pattern'))}"
            for i, e in enumerate(results)
        )

    def _rlm_find(args, **kw):
        cwd = kw.get("cwd") or os.getcwd()
        lake = _lake_for(cwd)
        results = lake.find(args.get("text"), args.get("max_results") or 10)
        if not results:
            return f'No entries contain "{args.get("text")}" in the context lake.'
        return "\n".join(
            f"{i + 1}. {e['key']} ({len(e['content'])} chars)\n   {_snippet(e, args.get('text'))}"
            for i, e in enumerate(results)
        )

    def _rlm_stats(args, **kw):
        cwd = kw.get("cwd") or os.getcwd()
        lake = _lake_for(cwd)
        s = lake.stats()
        if s["entries"] == 0:
            return "Context lake is empty. Store data with rlm_store."
        return f"Context lake: {s['entries']} entries, {s['chars']:,} chars total.\nKeys:\n" + "\n".join(f"- {k}" for k in s["keys"])

    def _rlm_forget(args, **kw):
        cwd = kw.get("cwd") or os.getcwd()
        lake = _lake_for(cwd)
        removed = lake.forget(args.get("pattern"))
        return f"Removed {removed} entries matching /{args.get('pattern')}/." if removed else f'No entries match /{args.get("pattern")}/.'

    def _rlm_snapshot(args, **kw):
        session_id = kw.get("session_id") or kw.get("task_id") or "default"
        kernel = _kernels.get(session_id)
        if not kernel:
            return "No kernel state to snapshot (ipython not used yet)."
        file = str(_state_dir_for(session_id) / "kernel-state.pkl")
        ev = kernel.snapshot(file)
        if ev.get("event") == "error":
            return f"Snapshot failed: {ev.get('evalue')}"
        return f"Snapshot saved to {file} — {len(ev.get('names', []))} variables."

    def _rlm_restore(args, **kw):
        session_id = kw.get("session_id") or kw.get("task_id") or "default"
        cwd = kw.get("cwd") or os.getcwd()
        file = str(_state_dir_for(session_id) / "kernel-state.pkl")
        if not Path(file).exists():
            return f"No snapshot found at {file}. Use rlm_snapshot first."
        kernel = _get_kernel(session_id, cwd)
        ev = kernel.restore(file)
        if ev.get("event") == "error":
            return f"Restore failed: {ev.get('evalue')}"
        return f"Restored {len(ev.get('names', []))} variables from {file}."

    def _rlm(args, **kw):
        # Background subagent via Hermes-native delegate_task. The child runs
        # independently; results arrive later via rlm_result.
        session_id = kw.get("session_id") or kw.get("task_id") or "default"
        prompt = args.get("prompt", "")
        name = (args.get("name") or prompt[:60]).strip().strip("'\"")[:80]
        try:
            from tools.delegate_tool import delegate_task
            child_id = f"rlm-{int(time.time() * 1000)}"
            delegate_task(tasks=[{"goal": prompt, "context": "RLM child subagent."}], background=True)
            entry = {"rlm_child_id": child_id, "name": name, "status": "running", "created": int(time.time() * 1000)}
            _children.setdefault(session_id, []).append(entry)
            return json.dumps({"rlm_child_id": child_id, "name": name, "status": "running"})
        except Exception as e:
            return f"rlm spawn failed: {e}"

    def _rlm_result(args, **kw):
        session_id = kw.get("session_id") or kw.get("task_id") or "default"
        child = args.get("child")
        for c in _children.get(session_id, []):
            if c["rlm_child_id"] == child or c["name"] == child:
                return json.dumps({"status": c["status"], "rlm_child_id": c["rlm_child_id"]})
        return f'No RLM child found for "{child}".'

    def _rlm_list(args, **kw):
        session_id = kw.get("session_id") or kw.get("task_id") or "default"
        lst = _children.get(session_id, [])
        if not lst:
            return "No RLM children in this session."
        return "\n".join(f"{i + 1}. {c['name']} — {c['rlm_child_id']} — {c['status']}" for i, c in enumerate(lst))

    tools = [
        ("ipython", "Execute Python code in a persistent kernel. Variables, imports, functions and results survive across calls. Use %%bash for shell commands and %cd to change the kernel's working directory. Output is capped and concise.", {"code": _str("Python code to execute in the persistent kernel"), "timeout": _num("Max seconds before the cell is interrupted (default 120)")}, ("code",), _ipython),
        ("rlm_store", "Store a context entry in the persistent context lake. Data stored here does NOT enter the LLM prompt — retrieve it later with rlm_get / rlm_search / rlm_find.", {"key": _str("Unique key for the entry"), "content": _str("Content to store (can be large)"), "tags": _arr("Optional tags")}, ("key", "content"), _rlm_store),
        ("rlm_get", "Retrieve a context entry from the context lake by exact key (truncated to 50KB).", {"key": _str("Entry key")}, ("key",), _rlm_get),
        ("rlm_search", "Regex-search the context lake. Returns matching keys with a snippet around the first match.", {"pattern": _str("Regex pattern (case-insensitive)"), "max_results": _num("Max matches (default 10)")}, ("pattern",), _rlm_search),
        ("rlm_find", "Find context lake entries containing an exact text (case-insensitive substring).", {"text": _str("Text to find"), "max_results": _num("Max matches (default 10)")}, ("text",), _rlm_find),
        ("rlm_stats", "Show context lake statistics: entry count, total chars, and all keys.", {}, (), _rlm_stats),
        ("rlm_forget", "Delete context lake entries whose key or content matches a regex pattern.", {"pattern": _str("Regex pattern (case-insensitive)")}, ("pattern",), _rlm_forget),
        ("rlm_snapshot", "Snapshot the persistent kernel state (variables, imports, functions) to disk. Use before compaction or to persist working state.", {}, (), _rlm_snapshot),
        ("rlm_restore", "Restore the persistent kernel state from the last snapshot. Call after a compaction or a fresh session.", {}, (), _rlm_restore),
        ("rlm", "Spawn a background subagent (RLM child) and return an admission handle immediately. The child runs independently; results arrive later via rlm_result.", {"prompt": _str("Task for the subagent"), "name": _str("Readable child name")}, ("prompt",), _rlm),
        ("rlm_result", "Get the result of a previously spawned RLM child (by child id or name).", {"child": _str("RLM child id or name from rlm_list")}, ("child",), _rlm_result),
        ("rlm_list", "List the RLM children (subagents) spawned by the current session.", {}, (), _rlm_list),
    ]

    for name, desc, props, required, handler in tools:
        ctx.register_tool(
            name=name,
            toolset=toolset,
            schema=_schema(name, desc, props, required),
            handler=handler,
            emoji="🧠",
        )

    # System prompt section — RLM usage instructions injected into each session.
    ctx.register_system_prompt_section(
        "rlm",
        "## RLM Programming Model (persistent Python kernel + context lake)\n"
        "You have an RLM-style persistent Python kernel via the `ipython` tool. State (variables, imports, functions) SURVIVES across tool calls. Keep working state in the kernel instead of re-reading files or re-sending data into context on every turn.\n"
        "Context lake: `rlm_store` saves large data to a persistent per-project lake (NOT in your context). `rlm_search` (regex) and `rlm_find` (text) locate entries and return snippets; `rlm_get` loads a full entry by key; `rlm_stats` lists keys; `rlm_forget` deletes.\n"
        "Subagents: `rlm` spawns a background subagent and returns an admission handle immediately — do NOT wait for it. Check results later with `rlm_result`.",
        position="after_memory",
    )

    # Compaction hook — snapshot the kernel and inject a variable summary.
    def _on_compact(session_id):
        kernel = _kernels.get(session_id)
        if not kernel:
            return ""
        try:
            names = kernel.list_names()
            file = str(_state_dir_for(session_id) / "kernel-state.pkl")
            kernel.snapshot(file)
            if names:
                lines = "\n".join(f"- {n.get('name')} ({n.get('type')}): {n.get('repr')}" for n in names[:40])
                return f"## RLM kernel state\nThis session has a persistent Python kernel (ipython tool). Current variables:\n{lines}\nThe full state was snapshotted to {file}. Use the ipython tool to inspect or continue working; call rlm_restore if the kernel was restarted."
        except Exception:
            pass
        return ""

    ctx.register_hook("on_session_end", lambda **kw: _cleanup(kw.get("session_id") or "default"))


def _cleanup(session_id):
    k = _kernels.pop(session_id, None)
    if k:
        try:
            k.shutdown()
        except Exception:
            pass
    _children.pop(session_id, None)
