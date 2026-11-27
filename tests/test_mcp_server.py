#!/usr/bin/env python3
"""Protocol tests for the rlm MCP server and the Claude Code hook shim.

Runs the real server as a subprocess and speaks JSON-RPC over its stdio, so a
pass here means the adapter works end to end without any editor installed.

    python3 tests/test_mcp_server.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "adapters" / "mcp" / "server.py"
HOOK = ROOT / "adapters" / "mcp" / "claude_hook.py"


class MCPServer:
    """Minimal JSON-RPC client over the server's stdio."""

    def __init__(self, cwd: str, env: dict):
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER)],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,
        )
        self.next_id = 0

    def request(self, method: str, params: dict | None = None):
        self.next_id += 1
        rid = self.next_id
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        while True:
            assert self.proc.stdout is not None
            line = self.proc.stdout.readline()
            if not line:
                err = self.proc.stderr.read() if self.proc.stderr else ""
                raise RuntimeError(f"server closed the pipe; stderr:\n{err}")
            line = line.strip()
            if not line:
                continue
            ev = json.loads(line)
            if ev.get("id") == rid:
                return ev

    def notify(self, method: str, params: dict | None = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def call(self, tool: str, **arguments):
        resp = self.request("tools/call", {"name": tool, "arguments": arguments})
        assert "result" in resp, resp
        return resp["result"]

    def close(self) -> None:
        try:
            self.request("shutdown")
        except Exception:
            pass
        try:
            self.proc.kill()
        except Exception:
            pass


class MCPServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "rlm-home"
        self.work = Path(self.tmp.name) / "project"
        self.work.mkdir(parents=True)
        env = {
            **os.environ,
            "RLM_HOME": str(self.home),
            "RLM_AUTOSNAPSHOT_SECONDS": "0",
        }
        env.pop("RLM_LAKE_FILE", None)
        self.env = env
        self.server = MCPServer(str(self.work), env)

    def tearDown(self) -> None:
        self.server.close()
        self.tmp.cleanup()

    # ── protocol ────────────────────────────────────────────────────────────
    def test_initialize_and_tools_list(self) -> None:
        init = self.server.request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        })
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(init["result"]["serverInfo"]["name"], "rlm")
        self.assertIn("tools", init["result"]["capabilities"])
        self.server.notify("notifications/initialized")

        listed = self.server.request("tools/list")["result"]["tools"]
        names = {t["name"] for t in listed}
        for expected in ("ipython", "rlm_store", "rlm_get", "rlm_search", "rlm_find",
                         "rlm_stats", "rlm_forget", "rlm_snapshot", "rlm_restore", "rlm_inspect"):
            self.assertIn(expected, names)
        for tool in listed:
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_ping_and_unknown_method(self) -> None:
        self.assertEqual(self.server.request("ping")["result"], {})
        resp = self.server.request("does/not/exist")
        self.assertEqual(resp["error"]["code"], -32601)

    def test_unknown_tool_is_a_tool_error(self) -> None:
        res = self.server.call("nope")
        self.assertTrue(res["isError"])
        self.assertIn("Unknown tool", res["content"][0]["text"])

    # ── kernel ──────────────────────────────────────────────────────────────
    def test_kernel_persists_between_calls(self) -> None:
        first = self.server.call("ipython", code="x = 40 + 2\nx")
        self.assertEqual(first["content"][0]["text"].strip(), "→ 42")
        second = self.server.call("ipython", code="x * 2")
        self.assertEqual(second["content"][0]["text"].strip(), "→ 84")

    def test_kernel_captures_stdout_and_bash(self) -> None:
        out = self.server.call("ipython", code="print('hola')")["content"][0]["text"]
        self.assertIn("hola", out)
        bash = self.server.call("ipython", code="%%bash\necho desde-bash")["content"][0]["text"]
        self.assertIn("desde-bash", bash)

    def test_kernel_error_is_reported(self) -> None:
        res = self.server.call("ipython", code="1/0")
        self.assertFalse(res["isError"], "a Python exception is a normal tool result")
        self.assertIn("ZeroDivisionError", res["content"][0]["text"])

    def test_cd_magic_changes_cwd(self) -> None:
        target = str(self.work)
        out = self.server.call("ipython", code=f"%cd {target}")["content"][0]["text"]
        self.assertIn(target, out)

    # ── context lake ────────────────────────────────────────────────────────
    def test_lake_store_search_find_get_forget(self) -> None:
        self.assertIn("Stored", self.server.call(
            "rlm_store", key="logs/needle", content="linea uno\nNEEDLE-7A3F aqui\nlinea tres",
            tags=["log"])["content"][0]["text"])

        found = self.server.call("rlm_find", text="needle-7a3f")["content"][0]["text"]
        self.assertIn("logs/needle", found)
        self.assertIn("NEEDLE-7A3F", found)

        searched = self.server.call("rlm_search", pattern=r"NEEDLE-\w+")["content"][0]["text"]
        self.assertIn("logs/needle", searched)

        got = self.server.call("rlm_get", key="logs/needle")["content"][0]["text"]
        self.assertIn("linea tres", got)

        stats = self.server.call("rlm_stats")["content"][0]["text"]
        self.assertIn("1 entries", stats)

        self.assertTrue(list((self.home / "lake").glob("*.jsonl")), "lake file should exist")

        removed = self.server.call("rlm_forget", pattern="logs/needle")["content"][0]["text"]
        self.assertIn("Removed 1", removed)
        self.assertIn("No entry", self.server.call("rlm_get", key="logs/needle")["content"][0]["text"])

    def test_lake_survives_a_new_server_process(self) -> None:
        self.server.call("rlm_store", key="shared", content="dato compartido")
        other = MCPServer(str(self.work), self.env)  # same RLM_HOME, same project
        try:
            found = other.call("rlm_find", text="compartido")["content"][0]["text"]
            self.assertIn("shared", found)
        finally:
            other.close()

    def test_lake_key_is_per_project(self) -> None:
        other_project = Path(self.tmp.name) / "other"
        other_project.mkdir()
        self.server.call("rlm_store", key="solo-aqui", content="x")
        other = MCPServer(str(other_project), self.env)
        try:
            res = other.call("rlm_find", text="solo-aqui")["content"][0]["text"]
            self.assertIn("No entries", res)
        finally:
            other.close()

    # ── snapshot / restore / inspect ────────────────────────────────────────
    def test_snapshot_restore_roundtrip(self) -> None:
        self.server.call("ipython", code="import json\npayload = {'a': 1, 'b': [2, 3]}")
        snap = self.server.call("rlm_snapshot")["content"][0]["text"]
        self.assertIn("variables", snap)

        restored = self.server.call("rlm_restore")["content"][0]["text"]
        self.assertIn("Restored", restored)
        out = self.server.call("ipython", code="payload['b'][1]")["content"][0]["text"]
        self.assertEqual(out.strip(), "→ 3")

    def test_restore_without_snapshot_is_explained(self) -> None:
        res = self.server.call("rlm_restore")["content"][0]["text"]
        self.assertIn("No snapshot found", res)

    def test_inspect_reports_kernel_and_lake(self) -> None:
        self.server.call("ipython", code="alpha = 1")
        self.server.call("rlm_store", key="k", content="v")
        report = self.server.call("rlm_inspect")["content"][0]["text"]
        self.assertIn("alpha", report)
        self.assertIn("lake: 1 entries", report)
        self.assertIn("snapshot:", report)


class ClaudeHookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "rlm-home"
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        self.state = self._state_dir()
        self.state.mkdir(parents=True)
        (self.state / "summary.json").write_text(json.dumps({
            "project": str(self.project),
            "cells": 3,
            "names": [{"name": "df", "type": "DataFrame", "repr": "<DataFrame 5000x8>"}],
            "snapshot": str(self.state / "kernel-state.pkl"),
        }), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _state_dir(self) -> Path:
        import hashlib
        key = hashlib.sha256(str(self.project.resolve()).rstrip("/").encode()).hexdigest()[:16]
        return self.home / "state" / key

    def _run(self, payload: dict) -> str:
        env = {**os.environ, "RLM_HOME": str(self.home)}
        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_session_start_injects_kernel_state(self) -> None:
        out = json.loads(self._run({"hook_event_name": "SessionStart", "cwd": str(self.project)}))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("df (DataFrame)", ctx)
        self.assertIn("rlm_restore", ctx)

    def test_session_start_is_silent_without_state(self) -> None:
        (self.state / "summary.json").unlink()
        self.assertIsNone(
            json.loads(self._run({"hook_event_name": "SessionStart", "cwd": str(self.project)}) or "null")
        )

    def test_precompact_prints_instructions(self) -> None:
        out = self._run({"hook_event_name": "PreCompact", "cwd": str(self.project), "trigger": "auto"})
        self.assertIn("RLM kernel state", out)
        self.assertIn("df (DataFrame)", out)
        self.assertNotIn('{"hookSpecificOutput"', out, "PreCompact must print plain text")

    def test_other_events_and_garbage_produce_nothing(self) -> None:
        self.assertEqual(self._run({"hook_event_name": "SessionEnd", "cwd": str(self.project)}), "")
        proc = subprocess.run([sys.executable, str(HOOK)], input="not json",
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
