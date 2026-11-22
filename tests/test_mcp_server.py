#!/usr/bin/env python3
"""Tests for the RLM MCP server (mcp/rlm_mcp_server.py) and the kernel's
socket transport mode.

Pattern: same as tests/test_kernel.py — subprocess + newline-delimited JSON.
No editor is needed: the MCP stdio transport is driven directly over the
server's stdin/stdout.

Covers spec T1 (protocol without an editor) and T2 (kernel persistence
across the transport: two consecutive server processes on the same project
and session; the second sees the variables of the first).
"""

import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(HERE, "..")
SERVER = os.path.join(REPO, "mcp", "rlm_mcp_server.py")
KERNEL = os.path.join(REPO, "kernel", "kernel.py")

EXPECTED_12 = {
    "ipython",
    "rlm_store", "rlm_get", "rlm_search", "rlm_find",
    "rlm_stats", "rlm_forget",
    "rlm_snapshot", "rlm_restore",
    "rlm", "rlm_result", "rlm_list",
}
EXPECTED_CORE = EXPECTED_12 - {"rlm", "rlm_result", "rlm_list"}


# ─── MCP stdio client (newline-delimited JSON-RPC 2.0) ───────────────────────

class McpClient:
    """Minimal MCP stdio client for the rlm server."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        self._next_id = 0
        self._lines: "queue.Queue[str]" = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        """Read server stdout line by line into a queue (fast for big payloads)."""
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)  # EOF sentinel

    # -- transport ---------------------------------------------------------
    def _write(self, obj: dict) -> None:
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def _read_line(self, timeout: float = 30.0) -> dict:
        """Wait for one full JSON-RPC message on stdout."""
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"server did not answer; stderr tail: {self.stderr_tail()!r}")
        if line is None:
            raise EOFError(f"server stdout closed; stderr tail: {self.stderr_tail()!r}")
        line = line.strip()
        while not line:
            try:
                line = self._lines.get(timeout=1.0)
            except queue.Empty:
                raise TimeoutError(f"server did not answer; stderr tail: {self.stderr_tail()!r}")
            if line is None:
                raise EOFError(f"server stdout closed; stderr tail: {self.stderr_tail()!r}")
            line = line.strip()
        return json.loads(line)

    def stderr_tail(self) -> str:
        # Non-blocking peek is not available on pipes; return what we can.
        try:
            if self.proc.stderr is None:
                return ""
            os.set_blocking(self.proc.stderr.fileno(), False)
            chunks = []
            while True:
                try:
                    chunk = self.proc.stderr.read(65536)
                except (BlockingIOError, OSError):
                    break
                if not chunk:
                    break
                chunks.append(chunk)
            os.set_blocking(self.proc.stderr.fileno(), True)
            return ("".join(chunks))[-2000:]
        except Exception:
            return ""

    def notify(self, method: str, params: dict | None = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)

    def request(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        self._next_id += 1
        rid = self._next_id
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)
        while True:
            ev = self._read_line(timeout=timeout)
            if ev.get("id") == rid:
                if "error" in ev:
                    raise RuntimeError(f"{method} error: {ev['error']}")
                return ev["result"]

    def initialize(self) -> dict:
        res = self.request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "rlm-test", "version": "0"},
        })
        self.notify("notifications/initialized")
        return res

    def call(self, tool: str, arguments: dict | None = None) -> str:
        """Call a tool and return its combined text content."""
        res = self.request("tools/call", {"name": tool, "arguments": arguments or {}}, timeout=120)
        if res.get("isError"):
            raise RuntimeError(f"tool {tool} returned error: {res.get('content')}")
        parts = [c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"]
        return "\n".join(parts)

    def tool_names(self) -> set:
        res = self.request("tools/list")
        return {t["name"] for t in res.get("tools", [])}

    def kill(self) -> None:
        """Kill the server (simulate editor recycle/crash); the kernel daemon
        must survive because it is detached."""
        try:
            self.proc.kill()
            self.proc.wait(timeout=10)
        except Exception:
            pass


# ─── Harness helpers ─────────────────────────────────────────────────────────

class Env:
    """One isolated project + state dir pair."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="rlm-mcp-test-")
        self.project = os.path.join(self.root, "project")
        self.state = os.path.join(self.root, "state")
        os.makedirs(self.project)
        os.makedirs(self.state)

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


def start_client(env: Env, extra_args=None, owner_pid: int | None = None) -> McpClient:
    """Start the MCP server with an isolated state dir.

    owner_pid simulates a different editor session (the kernel daemon is
    keyed by project + session owner); None means "same session as the test
    process", i.e. the server's real parent.
    """
    if not os.path.exists(SERVER):
        raise FileNotFoundError(f"MCP server does not exist yet: {SERVER}")
    proc_env = dict(os.environ)
    proc_env["RLM_STATE_DIR"] = env.state
    if owner_pid is not None:
        proc_env["RLM_OWNER_PID"] = str(owner_pid)
    else:
        proc_env.pop("RLM_OWNER_PID", None)
    proc = subprocess.Popen(
        [sys.executable, SERVER] + (extra_args or []),
        cwd=env.project,
        env=proc_env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return McpClient(proc)


# ─── T1: protocol without an editor ─────────────────────────────────────────

def test_initialize_and_tools_list():
    """initialize must answer with serverInfo/capabilities; tools/list must
    return the 12 canonical RLM tool names (default surface)."""
    env = Env()
    try:
        c = start_client(env)
        try:
            res = c.initialize()
            assert res.get("serverInfo", {}).get("name") == "rlm", f"serverInfo: {res}"
            assert "tools" in res.get("capabilities", {}), f"capabilities: {res}"
            names = c.tool_names()
            assert names == EXPECTED_12, f"tools/list returned {sorted(names)}"
        finally:
            c.kill()
    finally:
        env.close()
    print("PASS: test_initialize_and_tools_list")


def test_ipython_persistent_state():
    """x = 41 in one call, print(x + 1) in the next must return 42."""
    env = Env()
    try:
        c = start_client(env)
        try:
            c.initialize()
            out1 = c.call("ipython", {"code": "x = 41"})
            assert "error" not in out1.lower(), f"unexpected error: {out1!r}"
            out2 = c.call("ipython", {"code": "print(x + 1)"})
            assert "42" in out2, f"expected 42 in output, got: {out2!r}"
        finally:
            c.kill()
    finally:
        env.close()
    print("PASS: test_ipython_persistent_state")


def test_lake_round_trip_and_truncation():
    """rlm_store of 200 KB + rlm_stats lists it + rlm_get returns it
    truncated to 50 KB; the full content must NOT be in the tool response."""
    env = Env()
    try:
        c = start_client(env)
        try:
            c.initialize()
            big = "A" * 200_000
            stored = c.call("rlm_store", {"key": "informe", "content": big})
            assert "informe" in stored, f"store response: {stored!r}"

            stats = c.call("rlm_stats")
            assert "informe" in stats, f"stats must list the key: {stats!r}"

            got = c.call("rlm_get", {"key": "informe"})
            assert "200,000 chars" in got or "(200000" in got.replace(" ", ""), f"header: {got[:200]!r}"
            assert len(got) < 60_000, f"rlm_get response too large: {len(got)} chars (must be truncated to 50 KB)"
            assert "A" * 200_000 not in got, "full 200 KB content must not be in the response"
        finally:
            c.kill()
    finally:
        env.close()
    print("PASS: test_lake_round_trip_and_truncation")


def test_lake_isolation_between_projects():
    """The lake of one project must not be visible from another project."""
    env_a = Env()
    env_b = Env()
    try:
        ca = start_client(env_a)
        cb = start_client(env_b)
        try:
            ca.initialize()
            cb.initialize()
            ca.call("rlm_store", {"key": "secret", "content": "only-in-a"})
            stats_b = cb.call("rlm_stats")
            assert "secret" not in stats_b, f"project B must not see A's lake: {stats_b!r}"
        finally:
            ca.kill()
            cb.kill()
    finally:
        env_a.close()
        env_b.close()
    print("PASS: test_lake_isolation_between_projects")


def test_snapshot_and_restore_across_recycle():
    """Snapshot before closing, restore in the next session: variables come
    back by value. A new editor session (different session owner) must start
    clean — the variable is absent until rlm_restore."""
    env = Env()
    fake_owner = subprocess.Popen(["sleep", "300"],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
    try:
        c1 = start_client(env)
        try:
            c1.initialize()
            c1.call("ipython", {"code": "magic_number = 41"})
            snap = c1.call("rlm_snapshot")
            assert "Snapshot saved" in snap, f"snapshot response: {snap!r}"
        finally:
            c1.kill()

        # New session: a different session owner id (a real editor process
        # would be a new pid here).
        c2 = start_client(env, owner_pid=fake_owner.pid)
        try:
            c2.initialize()
            out = c2.call("ipython", {"code": "print(magic_number) if 'magic_number' in globals() else print('absent')"})
            assert "absent" in out, f"new session must start clean, got: {out!r}"
            restored = c2.call("rlm_restore")
            assert "Restored" in restored, f"restore response: {restored!r}"
            out2 = c2.call("ipython", {"code": "print(magic_number)"})
            assert "41" in out2, f"restored value missing: {out2!r}"
        finally:
            c2.kill()
    finally:
        fake_owner.kill()
        fake_owner.wait(timeout=5)
        env.close()
    print("PASS: test_snapshot_and_restore_across_recycle")


# ─── T2: kernel persistence across the transport ────────────────────────────

def test_state_survives_server_recycle():
    """Two consecutive server processes on the same project and session:
    the second one (after killing the first) must see its variables. This is
    the criterion that separates this delivery from a toy."""
    env = Env()
    try:
        c1 = start_client(env)
        try:
            c1.initialize()
            out = c1.call("ipython", {"code": "x = 41"})
            assert "error" not in out.lower(), f"setup failed: {out!r}"
        finally:
            c1.kill()  # mid-session kill; kernel daemon must survive

        c2 = start_client(env)
        try:
            c2.initialize()
            out = c2.call("ipython", {"code": "x += 1; print(x)"})
            assert "42" in out, f"state did not survive the recycle: {out!r}"
        finally:
            c2.kill()
    finally:
        env.close()
    print("PASS: test_state_survives_server_recycle")


def test_surface_core_excludes_subagents():
    """--surface core (used by the Claude Code and Codex adapters) must
    expose the 9 non-subagent tools only."""
    env = Env()
    try:
        c = start_client(env, extra_args=["--surface", "core"])
        try:
            c.initialize()
            names = c.tool_names()
            assert names == EXPECTED_CORE, f"--surface core returned {sorted(names)}"
        finally:
            c.kill()
    finally:
        env.close()
    print("PASS: test_surface_core_excludes_subagents")


def test_long_cell_completes():
    """A cell that runs longer than a normal tool call must complete with its
    result, not a transport error (the 90 s E2E case, scaled down)."""
    env = Env()
    try:
        c = start_client(env)
        try:
            c.initialize()
            out = c.call("ipython", {"code": "import time; time.sleep(3); 'ok'"})
            assert "ok" in out, f"long cell failed: {out!r}"
        finally:
            c.kill()
    finally:
        env.close()
    print("PASS: test_long_cell_completes")


# ─── Kernel socket mode (direct, no MCP) ─────────────────────────────────────

def test_kernel_socket_mode_protocol():
    """kernel.py --socket must speak the same JSON-lines protocol: stdout
    events carry the request id and arrive before result."""
    env = Env()
    try:
        sock_path = os.path.join(env.state, "k", "direct.sock")
        os.makedirs(os.path.dirname(sock_path), exist_ok=True)
        proc = subprocess.Popen(
            [sys.executable, KERNEL, "--socket", sock_path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            deadline = time.time() + 10
            conn = None
            while time.time() < deadline:
                if os.path.exists(sock_path):
                    try:
                        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        conn.connect(sock_path)
                        break
                    except OSError:
                        pass
                time.sleep(0.1)
            assert conn is not None, "kernel socket never appeared"

            def send(req: dict) -> None:
                conn.sendall((json.dumps(req) + "\n").encode())

            def read_events_until_done(rid: str, timeout: float = 15.0) -> list[dict]:
                buf = b""
                deadline = time.time() + timeout
                events = []
                while time.time() < deadline:
                    conn.settimeout(max(0.1, deadline - time.time()))
                    try:
                        chunk = conn.recv(65536)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        if not line.strip():
                            continue
                        ev = json.loads(line)
                        events.append(ev)
                        if ev.get("event") == "done" and ev.get("id") == rid:
                            return events
                return events

            # Ready event on connect.
            conn.settimeout(5)
            ready_line = b""
            while b"\n" not in ready_line:
                ready_line += conn.recv(4096)
            ready = json.loads(ready_line.split(b"\n", 1)[0])
            assert ready.get("event") == "ready", f"first frame: {ready}"

            send({"id": "s1", "type": "execute", "code": 'print("socket hello")\ny = 7'})
            events = read_events_until_done("s1")
            stdout_events = [e for e in events if e.get("event") == "stdout"]
            result_events = [e for e in events if e.get("event") == "result"]
            assert stdout_events and stdout_events[0].get("id") == "s1", f"events: {events}"
            assert "socket hello" in stdout_events[0].get("text", "")
            assert result_events, f"missing result: {events}"
            si = next(i for i, e in enumerate(events) if e.get("event") == "stdout")
            ri = next(i for i, e in enumerate(events) if e.get("event") == "result")
            assert si < ri, "stdout must arrive before result"

            send({"id": "s2", "type": "shutdown"})
            read_events_until_done("s2")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
    finally:
        env.close()
    print("PASS: test_kernel_socket_mode_protocol")


def _main():
    tests = [
        test_initialize_and_tools_list,
        test_ipython_persistent_state,
        test_lake_round_trip_and_truncation,
        test_lake_isolation_between_projects,
        test_snapshot_and_restore_across_recycle,
        test_state_survives_server_recycle,
        test_surface_core_excludes_subagents,
        test_long_cell_completes,
        test_kernel_socket_mode_protocol,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL: {t.__name__}: {exc}")
            failed += 1
    print(f"\nResults: {passed} passed, {failed} failed out of {len(tests)}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    _main()
