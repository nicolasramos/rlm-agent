"""Shared RLM client — persistent kernel process + context lake.

Both pieces are editor-agnostic: the kernel speaks JSON-lines over stdio
(kernel/kernel.py) and the lake is a per-project JSONL file. This module is
imported by the MCP adapter (Claude Code, Codex, Copilot) and can be reused by
any other Python-based adapter so lake semantics never drift between editors.

Layout (override the root with RLM_HOME):

  ~/.rlm/lake/<project-hash>.jsonl     one lake per project, shared by every editor
  ~/.rlm/state/<project-hash>/         kernel snapshot + state summary per project
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_TIMEOUT = 120
HARD_TIMEOUT_MS = 300_000
LAKE_GET_MAX_CHARS = 50_000


# ─── Locations ────────────────────────────────────────────────────────────────

def rlm_home() -> Path:
    env = os.environ.get("RLM_HOME")
    return Path(env).expanduser() if env else Path.home() / ".rlm"


def project_key(project_dir: str) -> str:
    """Stable 16-char hash of a project directory (symlinks resolved)."""
    resolved = str(Path(project_dir).expanduser().resolve()).rstrip("/")
    return hashlib.sha256(resolved.encode()).hexdigest()[:16]


def lake_file_for(project_dir: str) -> Path:
    env = os.environ.get("RLM_LAKE_FILE")
    if env:
        return Path(env).expanduser()
    return rlm_home() / "lake" / f"{project_key(project_dir)}.jsonl"


def state_dir_for(project_dir: str) -> Path:
    return rlm_home() / "state" / project_key(project_dir)


def python_bin() -> str:
    env = os.environ.get("RLM_KERNEL_PYTHON")
    if env:
        return env
    for c in ("python3", "/usr/bin/python3", "/opt/homebrew/bin/python3"):
        try:
            if subprocess.run([c, "--version"], capture_output=True).returncode == 0:
                return c
        except Exception:
            continue
    return "python3"


def kernel_path() -> Path:
    """Find kernel.py: env override, next to this file, then known installs."""
    env = os.environ.get("RLM_KERNEL")
    if env:
        return Path(env).expanduser()
    here = Path(__file__).resolve().parent
    candidates = [
        here / "kernel.py",                                        # copied next to the client
        here.parent / "kernel" / "kernel.py",                       # repo layout
        Path.home() / ".hermes" / "plugins" / "rlm" / "kernel.py",
        Path.home() / ".config" / "opencode" / "rlm-kernel" / "kernel.py",
        Path.home() / ".pi" / "agent" / "rlm-kernel" / "kernel.py",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


# ─── Kernel process ────────────────────────────────────────────────────────────

class Kernel:
    """Client for one kernel.py subprocess (JSON-lines protocol)."""

    def __init__(self, cwd: str):
        self.cwd = cwd
        self.proc = subprocess.Popen(
            [python_bin(), str(kernel_path())],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "RLM_LAKE_FILE": str(lake_file_for(cwd))},
        )
        self.stdout_accum: Dict[str, List[str]] = {}
        self.stderr_accum: Dict[str, List[str]] = {}
        self.pending: Dict[str, Dict[str, Any]] = {}
        self.next_id = 0
        self.closed = False
        self.stderr_tail: List[str] = []
        self._lock = threading.Lock()
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._handle_line(line.decode("utf-8", "replace"))

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr_tail.append(line.decode("utf-8", "replace"))
            if len(self.stderr_tail) > 20:
                self.stderr_tail.pop(0)

    def _handle_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            ev = json.loads(line)
        except Exception:
            return
        event_type = ev.get("event")
        rid = ev.get("id")
        if event_type in ("stdout", "stderr") and rid is not None:
            with self._lock:
                accum = self.stdout_accum if event_type == "stdout" else self.stderr_accum
                accum.setdefault(rid, []).append(ev.get("text", ""))
            return
        if event_type in ("result", "error", "names"):
            with self._lock:
                fut = self.pending.pop(rid, None)
            if fut:
                fut["value"] = ev
                fut["event"].set()

    def _request(self, req_type: str, payload: Optional[dict] = None,
                 timeout_ms: int = HARD_TIMEOUT_MS) -> dict:
        if self.closed:
            raise RuntimeError("kernel is not running")
        with self._lock:
            self.next_id += 1
            rid = f"k{self.next_id}"
            fut: Dict[str, Any] = {"event": threading.Event(), "value": None}
            self.pending[rid] = fut
        body = {"id": rid, "type": req_type, **(payload or {})}
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write((json.dumps(body) + "\n").encode())
            self.proc.stdin.flush()
        except Exception as exc:
            with self._lock:
                self.pending.pop(rid, None)
            raise RuntimeError(f"kernel write failed: {exc}")
        if not fut["event"].wait(timeout_ms / 1000):
            with self._lock:
                self.pending.pop(rid, None)
            raise RuntimeError(f"kernel request timed out after {timeout_ms}ms")
        return fut["value"]

    def execute(self, code: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
        ev = self._request("execute", {"code": code, "timeout": timeout})
        rid = str(ev.get("id") or "")
        with self._lock:
            stdout_text = "".join(self.stdout_accum.pop(rid, [])) if rid else ""
            stderr_text = "".join(self.stderr_accum.pop(rid, [])) if rid else ""
        return {
            "ok": ev.get("event") == "result" and ev.get("ok") is not False,
            "result": ev if ev.get("event") == "result" else None,
            "error": ev if ev.get("event") == "error" else None,
            "stdout": stdout_text,
            "stderr": stderr_text,
        }

    def list_names(self) -> List[dict]:
        return self._request("list_names").get("names", [])

    def snapshot(self, file: str) -> dict:
        return self._request("snapshot", {"path": file})

    def restore(self, file: str) -> dict:
        return self._request("restore", {"path": file})

    def shutdown(self) -> None:
        if self.closed:
            return
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write(
                (json.dumps({"id": f"k{self.next_id + 1}", "type": "shutdown"}) + "\n").encode()
            )
            self.proc.stdin.flush()
        except Exception:
            pass
        try:
            self.proc.kill()
        except Exception:
            pass
        self.closed = True

    def stderr_tail_text(self) -> str:
        return "".join(self.stderr_tail)[-2000:]


# ─── Context lake ─────────────────────────────────────────────────────────────

class ContextLake:
    """Per-project JSONL context lake (append-only writes, atomic forgets)."""

    def __init__(self, project_dir: str):
        self.file = lake_file_for(project_dir)
        self.entries: Dict[str, dict] = {}
        self.last_mtime = 0.0

    def _ensure_loaded(self) -> None:
        try:
            mtime = self.file.stat().st_mtime if self.file.exists() else 0.0
        except Exception:
            mtime = 0.0
        if self.last_mtime and mtime <= self.last_mtime:
            return
        self.last_mtime = mtime
        self.entries.clear()
        if not self.file.exists():
            return
        for line in self.file.read_text(errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
                if e.get("key"):
                    self.entries[e["key"]] = e
            except Exception:
                continue

    def _append(self, entry: dict) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _rewrite(self, removed: Optional[List[str]] = None) -> None:
        """Merge with what is on disk, then drop the keys removed locally."""
        self.file.parent.mkdir(parents=True, exist_ok=True)
        merged: Dict[str, dict] = {}
        if self.file.exists():
            for line in self.file.read_text(errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                    if e.get("key"):
                        merged[e["key"]] = e
                except Exception:
                    continue
        for k, e in self.entries.items():
            merged[k] = e
        for k in removed or []:
            merged.pop(k, None)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in merged.values()),
            encoding="utf-8",
        )
        tmp.replace(self.file)
        self.last_mtime = 0.0

    def store(self, key: str, content: str, tags: Optional[List[str]] = None,
              source: str = "model") -> dict:
        self._ensure_loaded()
        now = int(time.time() * 1000)
        entry = {
            "key": key,
            "content": content,
            "tags": tags or [],
            "source": source,
            "created": self.entries.get(key, {}).get("created", now),
            "updated": now,
        }
        self.entries[key] = entry
        self._append(entry)
        return entry

    def get(self, key: str) -> Optional[dict]:
        self._ensure_loaded()
        return self.entries.get(key)

    def search(self, pattern: str, max_results: int = 10) -> List[dict]:
        self._ensure_loaded()
        try:
            re_ = re.compile(pattern, re.IGNORECASE)
        except Exception:
            re_ = re.compile(re.escape(pattern), re.IGNORECASE)
        out: List[dict] = []
        for e in self.entries.values():
            if (re_.search(e.get("content", "")) or re_.search(e.get("key", ""))
                    or any(re_.search(t) for t in e.get("tags", []))):
                out.append(e)
                if len(out) >= max_results:
                    break
        return out

    def find(self, text: str, max_results: int = 10) -> List[dict]:
        self._ensure_loaded()
        needle = text.lower()
        out: List[dict] = []
        for e in self.entries.values():
            if needle in e.get("content", "").lower() or needle in e.get("key", "").lower():
                out.append(e)
                if len(out) >= max_results:
                    break
        return out

    def forget(self, pattern: str) -> int:
        self._ensure_loaded()
        try:
            re_ = re.compile(pattern, re.IGNORECASE)
        except Exception:
            re_ = re.compile(re.escape(pattern), re.IGNORECASE)
        removed = 0
        removed_keys: List[str] = []
        for k in list(self.entries.keys()):
            e = self.entries[k]
            if re_.search(e.get("key", "")) or re_.search(e.get("content", "")):
                del self.entries[k]
                removed_keys.append(k)
                removed += 1
        if removed:
            self._rewrite(removed_keys)
        return removed

    def stats(self) -> dict:
        self._ensure_loaded()
        chars = sum(len(e.get("content", "")) for e in self.entries.values())
        return {"entries": len(self.entries), "chars": chars, "keys": list(self.entries.keys())}


def snippet(entry: dict, needle: str, width: int = 160) -> str:
    content = entry.get("content", "")
    idx = content.lower().find(needle.lower())
    if idx < 0:
        return content[:width]
    start = max(0, idx - width // 2)
    return ("…" if start > 0 else "") + content[start:start + width] + "…"
