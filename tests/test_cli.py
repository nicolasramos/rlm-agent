#!/usr/bin/env python3
"""End-to-end tests for the rlm CLI (adapters/cli/rlm.py).

Every case runs the real command as a subprocess, so a pass here means an agent
with nothing but a shell can drive the kernel and the lake.

    python3 tests/test_cli.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "adapters" / "cli" / "rlm.py"


class CLITest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "home"
        self.project = base / "project"
        self.project.mkdir(parents=True)
        self.env = {**os.environ, "RLM_HOME": str(self.home), "RLM_AUTOSNAPSHOT_SECONDS": "0"}
        self.env.pop("RLM_LAKE_FILE", None)

    def tearDown(self) -> None:
        self.run_cli("stop")
        self.tmp.cleanup()

    def run_cli(self, *args, stdin: str | None = None, cwd: Path | None = None):
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=str(cwd or self.project),
            input=stdin,
            capture_output=True,
            text=True,
            env=self.env,
            timeout=120,
        )

    # ── kernel ──────────────────────────────────────────────────────────────
    def test_state_survives_between_processes(self) -> None:
        first = self.run_cli("exec", "-c", "total = 40 + 2")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_cli("exec", "-c", "total + 1")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("→ 43", second.stdout)

    def test_exec_from_stdin_and_from_file(self) -> None:
        piped = self.run_cli("exec", stdin="print('desde stdin')")
        self.assertIn("desde stdin", piped.stdout)
        script = self.project / "cell.py"
        script.write_text("value = [n * n for n in range(3)]\nvalue\n")
        from_file = self.run_cli("exec", str(script))
        self.assertIn("[0, 1, 4]", from_file.stdout)

    def test_sh_runs_a_shell_command(self) -> None:
        out = self.run_cli("sh", "echo hola-shell")
        self.assertIn("hola-shell", out.stdout)
        self.assertIn("exit code 0", out.stdout)

    def test_failing_cell_exits_one_and_shows_the_error(self) -> None:
        res = self.run_cli("exec", "-c", "1/0")
        self.assertEqual(res.returncode, 1)
        self.assertIn("ZeroDivisionError", res.stdout)

    def test_unknown_command_is_a_usage_error(self) -> None:
        res = self.run_cli("nope")
        self.assertEqual(res.returncode, 2)
        self.assertIn("unknown command", res.stderr)

    # ── lake ────────────────────────────────────────────────────────────────
    def test_lake_roundtrip_from_the_shell(self) -> None:
        stored = self.run_cli("store", "logs/a", "--text", "uno\nNEEDLE-7A3F\ntres", "--tags", "log,ci")
        self.assertIn("Stored", stored.stdout)

        found = self.run_cli("find", "needle-7a3f")
        self.assertIn("logs/a", found.stdout)
        self.assertIn("NEEDLE-7A3F", found.stdout)

        searched = self.run_cli("search", r"NEEDLE-\w+")
        self.assertIn("logs/a", searched.stdout)

        got = self.run_cli("get", "logs/a")
        self.assertEqual(got.stdout.strip(), "uno\nNEEDLE-7A3F\ntres")

        stats = self.run_cli("stats")
        self.assertIn("1 entries", stats.stdout)
        self.assertTrue(list((self.home / "lake").glob("*.jsonl")))

        dropped = self.run_cli("forget", "logs/a")
        self.assertIn("Removed 1", dropped.stdout)
        self.assertEqual(self.run_cli("get", "logs/a").returncode, 1)

    def test_store_reads_a_file_and_pipes(self) -> None:
        big = self.project / "build.log"
        big.write_text("x" * 5000)
        self.assertIn("5000 chars", self.run_cli("store", "build", "--file", str(big)).stdout)
        self.assertIn("Stored", self.run_cli("store", "piped", stdin="desde tuberia").stdout)
        self.assertEqual(self.run_cli("get", "piped").stdout.strip(), "desde tuberia")

    def test_lake_is_per_project(self) -> None:
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        self.run_cli("store", "solo-aqui", "--text", "x")
        res = self.run_cli("find", "solo-aqui", cwd=other)
        self.assertIn("No entries", res.stdout)

    # ── snapshot / restore / inspect / stop ─────────────────────────────────
    def test_snapshot_survives_a_stop(self) -> None:
        self.run_cli("exec", "-c", "marca = {'n': 7}")
        self.assertIn("Snapshot saved", self.run_cli("snapshot").stdout)
        self.run_cli("stop")

        restored = self.run_cli("restore")
        self.assertIn("Restored", restored.stdout)
        out = self.run_cli("exec", "-c", "marca['n'] * 2")
        self.assertIn("→ 14", out.stdout)

    def test_inspect_reports_state_and_stop_closes_the_kernel(self) -> None:
        self.run_cli("exec", "-c", "alpha = 1")
        report = self.run_cli("inspect")
        self.assertIn("kernel: running", report.stdout)
        self.assertIn("alpha (int)", report.stdout)
        self.assertIn("lake:", report.stdout)

        self.assertIn("stopped", self.run_cli("stop").stdout)
        after = self.run_cli("inspect")
        self.assertIn("kernel: not running", after.stdout)
        self.assertEqual(self.run_cli("list").returncode, 1)

    def test_help_and_version(self) -> None:
        self.assertIn("rlm exec", self.run_cli("--help").stdout)
        self.assertIn("rlm-agent", self.run_cli("--version").stdout)

    def test_mcp_subcommand_forwards_to_the_server(self) -> None:
        """`rlm mcp` must exec the MCP server; a bare initialize round-trip proves it."""
        env = {**self.env}
        proc = subprocess.run(
            [sys.executable, str(CLI), "mcp"],
            cwd=str(self.project),
            input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n",
            capture_output=True, text=True, env=env, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        first = json.loads(proc.stdout.splitlines()[0])
        names = {t["name"] for t in first["result"]["tools"]}
        self.assertIn("ipython", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
