#!/usr/bin/env python3
"""rlm — MCP (Model Context Protocol) server for the RLM system.

Speaks newline-delimited JSON-RPC 2.0 on stdin/stdout (the MCP stdio
transport) and exposes the RLM tool surface:

  core (default for the Claude Code / Codex adapters, ``--surface core``):
    ipython, rlm_store, rlm_get, rlm_search, rlm_find, rlm_stats,
    rlm_forget, rlm_snapshot, rlm_restore
  full (default; also for adapters that configure subagents):
    + rlm, rlm_result, rlm_list   (require RLM_SUBAGENT_CMD)

The kernel runs as a DETACHED daemon process (``kernel/kernel.py --socket``)
so its state survives this server being recycled or killed by the editor.
On every start the server re-attaches to the daemon for its project and
editor session (identified by the parent process id); a new editor session
starts clean, and ``rlm_restore`` reloads the last snapshot explicitly.

State layout (under $RLM_STATE_DIR, default ~/.rlm):
  kernels/<project>-<owner>.sock        kernel daemon socket (per project+session)
  kernels/<project>-<owner>.log         kernel daemon log
  kernels/<project>.current.json        session marker for editor hooks
  lake/<project>.jsonl                  context lake (per project)
  snapshots/<project>/kernel-state.pkl  default snapshot location
  children/<project>.json               subagent registry (full surface)

The session owner is the server's parent process id by default. Set
RLM_OWNER_PID to pin a different session id (useful when the editor spawns
the server through a wrapper, and to simulate sessions in tests).

Only JSON-RPC messages are written to stdout; diagnostics go to stderr.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
import uuid

SERVER_INFO = {"name": "rlm", "version": "0.2.0"}
LAKE_GET_MAX_CHARS = 50_000
DEFAULT_TIMEOUT = 120

# ─── State layout ────────────────────────────────────────────────────────────


def state_dir() -> str:
    return os.environ.get("RLM_STATE_DIR") or os.path.join(os.path.expanduser("~"), ".rlm")


def project_key(cwd: str) -> str:
    """Same convention as the editor adapters: sha256(project dir)[:16]."""
    return hashlib.sha256(cwd.encode("utf-8")).hexdigest()[:16]


def kernel_path() -> str:
    env = os.environ.get("RLM_KERNEL")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "..", "kernel", "kernel.py"),  # repo / codex layout
        os.path.join(here, "kernel", "kernel.py"),        # claude plugin layout
        os.path.join(os.getcwd(), "kernel", "kernel.py"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return os.path.abspath(c)
    return candidates[0]


def python_bin() -> str:
    return os.environ.get("RLM_KERNEL_PYTHON") or "python3"


def log(*args) -> None:
    print("[rlm-mcp]", *args, file=sys.stderr, flush=True)


class State:
    """Per-session layout derived from the server's cwd and session owner.

    The session owner is the parent process id (the editor session process)
    unless RLM_OWNER_PID pins an explicit session id.
    """

    def __init__(self) -> None:
        self.cwd = os.getcwd()
        self.root = state_dir()
        self.key = project_key(self.cwd)
        env_owner = os.environ.get("RLM_OWNER_PID", "").strip()
        self.owner_pid = int(env_owner) if env_owner.isdigit() else os.getppid()
        self.lake_file = os.path.join(self.root, "lake", f"{self.key}.jsonl")
        self.socket_path = os.path.join(self.root, "kernels", f"{self.key}-{self.owner_pid}.sock")
        self.kernel_log = os.path.join(self.root, "kernels", f"{self.key}-{self.owner_pid}.log")
        self.current_marker = os.path.join(self.root, "kernels", f"{self.key}.current.json")
        self.snapshot_path = os.path.join(self.root, "snapshots", self.key, "kernel-state.pkl")
        self.children_file = os.path.join(self.root, "children", f"{self.key}.json")


# ─── Kernel client (detached daemon over a Unix socket) ──────────────────────


class KernelError(RuntimeError):
    pass


class KernelClient:
    """Talks the kernel JSON-lines protocol to a detached daemon process.

    The daemon is launched on first use (detached, so it outlives this
    server) and re-attached to on every later start of the server for the
    same project+session.
    """

    def __init__(self, st: State) -> None:
        self.st = st
        self.conn: socket.socket | None = None

    def _connect(self) -> bool:
        try:
            if not os.path.exists(self.st.socket_path):
                return False
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.settimeout(10)
            conn.connect(self.st.socket_path)
            # Drain the ready frame.
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    conn.close()
                    return False
                buf += chunk
            self.conn = conn
            return True
        except OSError:
            return False

    def _start_daemon(self) -> None:
        os.makedirs(os.path.dirname(self.st.socket_path), exist_ok=True)
        if os.path.exists(self.st.socket_path):
            try:
                os.unlink(self.st.socket_path)  # stale socket, daemon is gone
            except OSError:
                pass
        logf = open(self.st.kernel_log, "ab")
        subprocess.Popen(
            [python_bin(), kernel_path(), "--socket", self.st.socket_path,
             "--owner", str(self.st.owner_pid)],
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=logf,
            start_new_session=True,  # detached: survives this server's death
        )

    def _write_marker(self) -> None:
        """Session marker so editor hooks (Claude Code PreCompact) can find
        the kernel socket without re-deriving it."""
        payload = {
            "socket": self.st.socket_path,
            "owner_pid": self.st.owner_pid,
            "project": self.st.key,
            "updated": time.time(),
        }
        os.makedirs(os.path.dirname(self.st.current_marker), exist_ok=True)
        tmp = self.st.current_marker + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, self.st.current_marker)

    def ensure(self) -> None:
        if self.conn is not None:
            return
        if not self._connect():
            self._start_daemon()
            deadline = time.time() + 15
            while time.time() < deadline:
                if self._connect():
                    break
                time.sleep(0.2)
            else:
                raise KernelError(
                    f"kernel daemon did not come up (log: {self.st.kernel_log})"
                )
        self._write_marker()

    def _reset(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except OSError:
                pass
        self.conn = None

    def request(self, rtype: str, payload: dict | None = None, timeout_s: float = 300) -> dict:
        """Send one request, return the terminal event (result/error/names),
        with stdout/stderr collected alongside. Retries once on a dead
        connection (the daemon may have been restarted by the editor)."""
        for attempt in (1, 2):
            try:
                return self._request_once(rtype, payload or {}, timeout_s)
            except (OSError, EOFError, ConnectionError):
                if attempt == 2:
                    raise
                log(f"kernel connection lost ({rtype}); re-attaching")
                self._reset()
        raise RuntimeError("unreachable")

    def _request_once(self, rtype: str, payload: dict, timeout_s: float) -> dict:
        self.ensure()
        assert self.conn is not None
        rid = f"m{uuid.uuid4().hex[:12]}"
        req = {"id": rid, "type": rtype}
        req.update(payload)
        self.conn.sendall((json.dumps(req, separators=(",", ":")) + "\n").encode())

        buf = b""
        out: dict[str, list[str]] = {"stdout": [], "stderr": []}
        deadline = time.time() + timeout_s
        self.conn.settimeout(5)
        while time.time() < deadline:
            try:
                chunk = self.conn.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                raise EOFError("kernel connection closed")
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("id") != rid:
                    continue  # defensive: not our request
                et = ev.get("event")
                if et in ("stdout", "stderr"):
                    out[et].append(ev.get("text", ""))
                elif et in ("result", "error", "names"):
                    ev["stdout"] = "".join(out["stdout"])
                    ev["stderr"] = "".join(out["stderr"])
                    return ev
                elif et == "done" and not out["stdout"] and not out["stderr"]:
                    # interrupt-only requests end with done and no payload
                    ev["stdout"] = ""
                    ev["stderr"] = ""
                    return ev
        raise TimeoutError(f"kernel request {rtype} timed out after {timeout_s}s")


# ─── Context lake (JSONL, same format as the editor adapters) ────────────────


class Lake:
    def __init__(self, file: str) -> None:
        self.file = file
        self.entries: dict[str, dict] = {}
        self._mtime = 0.0

    def _ensure(self) -> None:
        try:
            mtime = os.path.getmtime(self.file) if os.path.exists(self.file) else 0.0
        except OSError:
            mtime = 0.0
        if self._mtime > 0 and mtime <= self._mtime:
            return
        self._mtime = mtime
        self.entries.clear()
        if not os.path.exists(self.file):
            return
        with open(self.file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    if e.get("key"):
                        self.entries[e["key"]] = e
                except json.JSONDecodeError:
                    continue

    def store(self, key: str, content: str, tags: list[str] | None = None,
              source: str = "model") -> dict:
        self._ensure()
        now = int(time.time() * 1000)
        entry = {
            "key": key,
            "content": content,
            "tags": tags or [],
            "source": source,
            "created": self.entries.get(key, {}).get("created", now),
            "updated": now,
        }
        os.makedirs(os.path.dirname(self.file), exist_ok=True)
        with open(self.file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.entries[key] = entry
        return entry

    def get(self, key: str) -> dict | None:
        self._ensure()
        return self.entries.get(key)

    def search(self, pattern: str, max_results: int = 10) -> list[dict]:
        self._ensure()
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error:
            rx = re.compile(re.escape(pattern), re.IGNORECASE)
        out = []
        for e in self.entries.values():
            if (rx.search(e.get("content", "")) or rx.search(e.get("key", ""))
                    or any(rx.search(t) for t in e.get("tags", []))):
                out.append(e)
                if len(out) >= max_results:
                    break
        return out

    def find(self, text: str, max_results: int = 10) -> list[dict]:
        self._ensure()
        needle = text.lower()
        out = []
        for e in self.entries.values():
            if needle in e.get("content", "").lower() or needle in e.get("key", "").lower():
                out.append(e)
                if len(out) >= max_results:
                    break
        return out

    def stats(self) -> dict:
        self._ensure()
        chars = sum(len(e.get("content", "")) for e in self.entries.values())
        return {"entries": len(self.entries), "chars": chars, "keys": sorted(self.entries)}

    def forget(self, pattern: str) -> int:
        self._ensure()
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error:
            rx = re.compile(re.escape(pattern), re.IGNORECASE)
        removed = [k for k, e in self.entries.items()
                   if rx.search(k) or rx.search(e.get("content", ""))]
        for k in removed:
            del self.entries[k]
        if removed:
            with open(self.file, "w", encoding="utf-8") as f:
                for e in self.entries.values():
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
        return len(removed)


def _snippet(entry: dict, needle: str, width: int = 160) -> str:
    idx = entry.get("content", "").lower().find(needle.lower())
    if idx < 0:
        return entry.get("content", "")[:width]
    start = max(0, idx - width // 2)
    return ("…" if start > 0 else "") + entry["content"][start:start + width] + "…"


# ─── Tool implementations (text output matches the editor adapters) ──────────


def tool_ipython(args: dict, st: State, kc: KernelClient) -> str:
    code = args.get("code", "")
    timeout = args.get("timeout") or DEFAULT_TIMEOUT
    ev = kc.request("execute", {"code": code, "timeout": timeout}, timeout_s=float(timeout) + 60)
    if ev.get("event") == "error":
        tb = "".join(ev.get("traceback", [])).strip()
        return f"Error ({ev.get('ename')}): {ev.get('evalue')}" + (f"\n{tb}" if tb else "")
    parts = []
    if ev.get("stdout"):
        parts.append(ev["stdout"].rstrip())
    if ev.get("repr"):
        parts.append(f"→ {ev['repr']}")
    return "\n".join(parts) or "(no output)"


def tool_store(args: dict, st: State, lake: Lake) -> str:
    key = args.get("key", "")
    content = args.get("content", "")
    if not key:
        return "rlm_store requires a non-empty 'key'."
    entry = lake.store(key, content, args.get("tags") or [], "model")
    return f'Stored "{key}" ({len(entry["content"])} chars). Not in the LLM context.'


def tool_get(args: dict, st: State, lake: Lake) -> str:
    key = args.get("key", "")
    entry = lake.get(key)
    if not entry:
        return f'No entry "{key}" in the context lake.'
    content = entry["content"]
    if len(content) > LAKE_GET_MAX_CHARS:
        content = content[:LAKE_GET_MAX_CHARS] + "\n...[truncated]..."
    return f'[{key}] ({len(entry["content"])} chars)\n{content}'


def tool_search(args: dict, st: State, lake: Lake) -> str:
    pattern = args.get("pattern", "")
    results = lake.search(pattern, int(args.get("max_results") or 10))
    if not results:
        return f"No entries match /{pattern}/."
    return "\n".join(
        f"{i + 1}. {e['key']} ({len(e.get('content', ''))} chars)\n   {_snippet(e, pattern)}"
        for i, e in enumerate(results)
    )


def tool_find(args: dict, st: State, lake: Lake) -> str:
    text = args.get("text", "")
    results = lake.find(text, int(args.get("max_results") or 10))
    if not results:
        return f'No entries contain "{text}".'
    return "\n".join(
        f"{i + 1}. {e['key']} ({len(e.get('content', ''))} chars)\n   {_snippet(e, text)}"
        for i, e in enumerate(results)
    )


def tool_stats(_args: dict, st: State, lake: Lake) -> str:
    s = lake.stats()
    if s["entries"] == 0:
        return "Context lake is empty. Store data with rlm_store."
    keys = "\n".join(f"- {k}" for k in s["keys"])
    return f"Context lake: {s['entries']} entries, {s['chars']:,} chars total.\nKeys:\n{keys}"


def tool_forget(args: dict, st: State, lake: Lake) -> str:
    pattern = args.get("pattern", "")
    removed = lake.forget(pattern)
    if removed > 0:
        return f"Removed {removed} entries matching /{pattern}/."
    return f"No entries match /{pattern}/."


def tool_snapshot(args: dict, st: State, kc: KernelClient) -> str:
    path = args.get("path") or st.snapshot_path
    ev = kc.request("snapshot", {"path": path}, timeout_s=60)
    if ev.get("event") == "error":
        return f"Snapshot failed: {ev.get('evalue')}"
    names = ev.get("names") or []
    return f"Snapshot saved to {path} — {len(names)} variables ({ev.get('bytes', 0)} bytes)."


def tool_restore(args: dict, st: State, kc: KernelClient) -> str:
    path = args.get("path") or st.snapshot_path
    if not os.path.exists(path):
        return f"No snapshot found at {path}. Use rlm_snapshot first."
    ev = kc.request("restore", {"path": path}, timeout_s=60)
    if ev.get("event") == "error":
        return f"Restore failed: {ev.get('evalue')}"
    names = ev.get("names") or []
    return f"Restored {len(names)} variables from {path}."


# ─── Subagents (full surface only; require RLM_SUBAGENT_CMD) ─────────────────


def _children_load(st: State) -> list[dict]:
    if not os.path.exists(st.children_file):
        return []
    try:
        with open(st.children_file, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _children_save(st: State, entries: list[dict]) -> None:
    os.makedirs(os.path.dirname(st.children_file), exist_ok=True)
    with open(st.children_file, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, TypeError):
        return False
    return True


def tool_rlm(args: dict, st: State) -> str:
    cmd = os.environ.get("RLM_SUBAGENT_CMD", "")
    if not cmd:
        return ('Subagents are not configured for this adapter. '
                'Set RLM_SUBAGENT_CMD (e.g. "pi --mode json {prompt}").')
    prompt = args.get("prompt", "")
    name = (args.get("name") or prompt[:40]).strip("'\"")[:60]
    child_dir = os.path.join(st.root, "children")
    os.makedirs(child_dir, exist_ok=True)
    out_file = os.path.join(child_dir, f"{int(time.time())}-{uuid.uuid4().hex[:6]}.jsonl")
    argv = [p.replace("{prompt}", prompt) for p in shlex.split(cmd)]
    if "{prompt}" not in cmd:
        argv.append(prompt)
    with open(out_file, "wb") as f:
        proc = subprocess.Popen(argv, cwd=st.cwd, stdout=f, stderr=subprocess.DEVNULL)
    entries = _children_load(st)
    entries.append({
        "rlm_child_id": out_file, "name": name, "status": "running",
        "pid": proc.pid, "created": time.time(),
    })
    _children_save(st, entries)
    return json.dumps({"rlm_child_id": out_file, "name": name, "status": "running"})


def tool_rlm_result(args: dict, st: State) -> str:
    child = args.get("child", "")
    entries = _children_load(st)
    entry = next((c for c in entries if c.get("rlm_child_id") == child or c.get("name") == child), None)
    if not entry:
        return f'No RLM child found for "{child}".'
    if entry.get("status") == "running" and _pid_alive(entry.get("pid", -1)):
        return json.dumps({"status": "running"})
    text = ""
    if os.path.exists(entry["rlm_child_id"]):
        with open(entry["rlm_child_id"], encoding="utf-8", errors="replace") as f:
            text = f.read()[-4000:]
    return json.dumps({"status": "completed", "output_tail": text})


def tool_rlm_list(_args: dict, st: State) -> str:
    entries = _children_load(st)
    if not entries:
        return "No RLM children."
    lines = []
    for i, c in enumerate(entries):
        status = c.get("status", "running")
        if status == "running" and not _pid_alive(c.get("pid", -1)):
            status = "completed"
        lines.append(f"{i + 1}. {c.get('name', '?')} — {status}")
    return "\n".join(lines)


# ─── Tool registry (JSON Schema for MCP tools/list) ──────────────────────────


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


def _str(desc: str) -> dict:
    return {"type": "string", "description": desc}


def _num(desc: str) -> dict:
    return {"type": "number", "description": desc}


def _arr(desc: str) -> dict:
    return {"type": "array", "items": {"type": "string"}, "description": desc}


TOOLS: list[dict] = [
    {
        "name": "ipython",
        "description": ("Execute Python code in a persistent kernel. Variables, imports, functions and "
                        "results survive across calls (and across server restarts within the same "
                        "editor session). Use %%bash for shell commands and %cd to change the kernel's "
                        "working directory. Output is capped and concise."),
        "inputSchema": _schema({"code": _str("Python code to execute in the persistent kernel"),
                                "timeout": _num("Max seconds before the cell is interrupted (default 120)")},
                               ["code"]),
    },
    {
        "name": "rlm_store",
        "description": ("Store a context entry in the persistent context lake. Data stored here does NOT "
                        "enter the LLM prompt — retrieve it later with rlm_get / rlm_search / rlm_find."),
        "inputSchema": _schema({"key": _str("Unique key for the entry"),
                                "content": _str("Content to store (can be large)"),
                                "tags": _arr("Optional tags")}, ["key", "content"]),
    },
    {
        "name": "rlm_get",
        "description": "Retrieve a context entry from the context lake by exact key (truncated to 50KB).",
        "inputSchema": _schema({"key": _str("Entry key")}, ["key"]),
    },
    {
        "name": "rlm_search",
        "description": "Regex-search the context lake. Returns matching keys with a snippet around the first match.",
        "inputSchema": _schema({"pattern": _str("Regex pattern (case-insensitive)"),
                                "max_results": _num("Max matches (default 10)")}, ["pattern"]),
    },
    {
        "name": "rlm_find",
        "description": "Find context lake entries containing an exact text (case-insensitive substring).",
        "inputSchema": _schema({"text": _str("Text to find"),
                                "max_results": _num("Max matches (default 10)")}, ["text"]),
    },
    {
        "name": "rlm_stats",
        "description": "Show context lake statistics: entry count, total chars, and all keys.",
        "inputSchema": _schema({}, []),
    },
    {
        "name": "rlm_forget",
        "description": "Delete context lake entries whose key or content matches a regex pattern.",
        "inputSchema": _schema({"pattern": _str("Regex pattern (case-insensitive)")}, ["pattern"]),
    },
    {
        "name": "rlm_snapshot",
        "description": ("Snapshot the persistent kernel state (variables, imports, functions) to disk. "
                        "Use before compaction or when you want to persist working state."),
        "inputSchema": _schema({"path": _str("Optional absolute path (default: project snapshot)")}, []),
    },
    {
        "name": "rlm_restore",
        "description": ("Restore the persistent kernel state from the last snapshot. "
                        "Call after a compaction or a fresh session to reload working variables."),
        "inputSchema": _schema({"path": _str("Optional absolute path (default: project snapshot)")}, []),
    },
    {
        "name": "rlm",
        "subagent": True,
        "description": ("Spawn a background subagent and return an admission handle immediately. "
                        "The child runs independently; results arrive later via rlm_result."),
        "inputSchema": _schema({"prompt": _str("Task for the subagent"),
                                "name": _str("Readable child name")}, ["prompt"]),
    },
    {
        "name": "rlm_result",
        "subagent": True,
        "description": "Get the result of a previously spawned RLM child (by id or name).",
        "inputSchema": _schema({"child": _str("RLM child id or name")}, ["child"]),
    },
    {
        "name": "rlm_list",
        "subagent": True,
        "description": "List the RLM children (subagents) spawned by this project.",
        "inputSchema": _schema({}, []),
    },
]

HANDLERS = {
    "ipython": lambda a, st, lake, kc: tool_ipython(a, st, kc),
    "rlm_store": lambda a, st, lake, kc: tool_store(a, st, lake),
    "rlm_get": lambda a, st, lake, kc: tool_get(a, st, lake),
    "rlm_search": lambda a, st, lake, kc: tool_search(a, st, lake),
    "rlm_find": lambda a, st, lake, kc: tool_find(a, st, lake),
    "rlm_stats": lambda a, st, lake, kc: tool_stats(a, st, lake),
    "rlm_forget": lambda a, st, lake, kc: tool_forget(a, st, lake),
    "rlm_snapshot": lambda a, st, lake, kc: tool_snapshot(a, st, kc),
    "rlm_restore": lambda a, st, lake, kc: tool_restore(a, st, kc),
    "rlm": lambda a, st, lake, kc: tool_rlm(a, st),
    "rlm_result": lambda a, st, lake, kc: tool_rlm_result(a, st),
    "rlm_list": lambda a, st, lake, kc: tool_rlm_list(a, st),
}


def call_tool(name: str, args: dict, st: State, lake: Lake, kc: KernelClient) -> dict:
    handler = HANDLERS.get(name)
    if handler is None:
        return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}
    try:
        text = handler(args, st, lake, kc)
        return {"content": [{"type": "text", "text": text}]}
    except Exception as exc:  # noqa: BLE001 — tool errors are reported to the model
        log(f"tool {name} failed: {exc}")
        return {"content": [{"type": "text", "text": f"{name} error: {exc}"}], "isError": True}


# ─── JSON-RPC loop (MCP stdio transport) ─────────────────────────────────────


def _respond(rid, result=None, error=None) -> None:
    msg: dict = {"jsonrpc": "2.0", "id": rid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    surface = "full"
    if "--surface" in sys.argv:
        surface = sys.argv[sys.argv.index("--surface") + 1]
    if surface not in ("full", "core"):
        print(f"invalid --surface {surface!r} (expected full|core)", file=sys.stderr)
        return 2

    st = State()
    kc = KernelClient(st)
    lake = Lake(st.lake_file)
    tools = [t for t in TOOLS if surface == "full" or not t.get("subagent")]
    public_tools = [{k: v for k, v in t.items() if k != "subagent"} for t in tools]
    log(f"starting (surface={surface}, project={st.key}, owner_pid={st.owner_pid})")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _respond(None, error={"code": -32700, "message": "parse error"})
            continue

        method = msg.get("method")
        rid = msg.get("id")
        params = msg.get("params") or {}

        if rid is None:
            continue  # notification (e.g. notifications/initialized): no response

        try:
            if method == "initialize":
                version = params.get("protocolVersion") or "2025-06-18"
                _respond(rid, {
                    "protocolVersion": version,
                    "capabilities": {"tools": {}},
                    "serverInfo": SERVER_INFO,
                })
            elif method == "ping":
                _respond(rid, {})
            elif method == "tools/list":
                _respond(rid, {"tools": public_tools})
            elif method == "tools/call":
                _respond(rid, call_tool(params.get("name", ""), params.get("arguments") or {}, st, lake, kc))
            else:
                _respond(rid, error={"code": -32601, "message": f"method not found: {method}"})
        except Exception as exc:  # noqa: BLE001
            log(f"internal error on {method}: {exc}")
            _respond(rid, error={"code": -32603, "message": str(exc)})

    return 0


if __name__ == "__main__":
    sys.exit(main())
