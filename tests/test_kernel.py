#!/usr/bin/env python3
"""End-to-end tests for rlm-kernel over the stdio protocol.

Usage: python3 tests/test_kernel.py [--python /path/to/python3]
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

KERNEL = os.path.join(os.path.dirname(__file__), "..", "kernel", "kernel.py")


class Kernel:
    def __init__(self, python: str = sys.executable, env: dict | None = None):
        self.proc = subprocess.Popen(
            [python, KERNEL],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env={**os.environ, **(env or {})},
        )
        self._next_id = 0
        # Wait for ready
        line = self.proc.stdout.readline()
        assert "ready" in line, f"expected ready, got: {line!r}"

    def request(self, rtype: str, **kw) -> list[dict]:
        self._next_id += 1
        rid = f"t{self._next_id}"
        req = {"id": rid, "type": rtype, **kw}
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        events = []
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("kernel closed unexpectedly")
            ev = json.loads(line)
            events.append(ev)
            if ev.get("event") == "done" and ev.get("id") == rid:
                return events

    def execute(self, code: str, timeout: int | None = None) -> dict:
        kw = {"code": code}
        if timeout:
            kw["timeout"] = timeout
        events = self.request("execute", **kw)
        result = next((e for e in events if e.get("event") == "result"), None)
        error = next((e for e in events if e.get("event") == "error"), None)
        stdout = "".join(e.get("text", "") for e in events if e.get("event") == "stdout")
        stderr = "".join(e.get("text", "") for e in events if e.get("event") == "stderr")
        return {"result": result, "error": error, "stdout": stdout, "stderr": stderr}

    def close(self):
        try:
            self.request("shutdown")
        except Exception:
            pass
        self.proc.wait(timeout=5)


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        raise SystemExit(1)


def main():
    python = sys.executable
    if "--python" in sys.argv:
        python = sys.argv[sys.argv.index("--python") + 1]

    k = Kernel(python)
    try:
        # 1. Basic execution + trailing expression
        r = k.execute("x = 41\nx + 1")
        check("trailing expression", r["result"] and r["result"]["ok"] and r["result"]["repr"] == "42",
              str(r))

        # 2. State persists across calls
        r = k.execute("x * 2")
        check("state persists", r["result"] and r["result"]["repr"] == "82", str(r))

        # 3. Imports persist
        r = k.execute("import json\ndata = {'a': [1, 2, 3]}\njson.dumps(data)")
        check("imports persist", r["result"] and r["result"]["repr"] == "'{\"a\": [1, 2, 3]}'", str(r))

        # 4. stdout capture
        r = k.execute("print('hello from cell')\nprint('second line')")
        check("stdout capture", "hello from cell" in r["stdout"] and "second line" in r["stdout"],
              r["stdout"])

        # 5. Error handling
        r = k.execute("1 / 0")
        check("error event", r["error"] and r["error"]["ename"] == "ZeroDivisionError", str(r))

        # 6. State survives errors
        r = k.execute("x + 1")
        check("state survives errors", r["result"] and r["result"]["repr"] == "42", str(r))

        # 7. Top-level await
        r = k.execute("import asyncio\nasync def f():\n    return 99\nawait f()")
        check("top-level await", r["result"] and r["result"]["repr"] == "99", str(r))

        # 8. %%bash cell
        r = k.execute("%%bash\necho bash-output-$((1+1))")
        check("%%bash", "bash-output-2" in r["stdout"], r["stdout"])

        # 9. %cd persists
        tmp = tempfile.mkdtemp()
        r = k.execute(f"%cd {tmp}")
        check("%cd", r["result"] and r["result"]["repr"] == os.path.realpath(tmp), str(r))
        r = k.execute("import os\nos.getcwd()")
        check("%cd persists", r["result"] and r["result"]["repr"] == repr(os.path.realpath(tmp)), str(r))

        # 10. list_names
        events = k.request("list_names")
        names_ev = next(e for e in events if e.get("event") == "names")
        names = {n["name"] for n in names_ev["names"]}
        check("list_names", "x" in names and "data" in names and "json" not in names, str(names))

        # 11. Snapshot + restore
        snap_path = os.path.join(tmp, "state.pkl")
        events = k.request("snapshot", path=snap_path)
        snap = next(e for e in events if e.get("event") == "result")
        check("snapshot", snap["ok"] and "x" in snap["names"], str(snap))
        r = k.execute("x = 1")
        check("mutate after snapshot", r["result"] and r["result"]["ok"], str(r))
        r = k.execute("x")
        check("mutated value", r["result"] and r["result"]["repr"] == "1", str(r))
        events = k.request("restore", path=snap_path)
        rest = next(e for e in events if e.get("event") == "result")
        check("restore", rest["ok"] and "x" in rest["names"], str(rest))
        r = k.execute("x")
        check("restored value", r["result"] and r["result"]["repr"] == "41", str(r))

        # 12. Timeout interrupts a runaway cell
        t0 = time.time()
        r = k.execute("import time\nwhile True:\n    time.sleep(0.05)", timeout=2)
        elapsed = time.time() - t0
        check("timeout", r["error"] and r["error"]["ename"] == "KeyboardInterrupt" and elapsed < 10,
              f"elapsed={elapsed:.1f}s err={r['error']}")

        # 13. Kernel still alive after interrupt
        r = k.execute("x + 1")
        check("alive after interrupt", r["result"] and r["result"]["repr"] == "42", str(r))

        # 14. Output cap
        r = k.execute("print('A' * 500000)")
        check("stdout capped", len(r["stdout"]) <= 110_000, f"len={len(r['stdout'])}")

        # 15. repr cap
        r = k.execute("list(range(100000))")
        check("repr capped", r["result"] and len(r["result"]["repr"]) <= 5_000,
              f"len={len(r['result']['repr']) if r['result'] else '?'}")

        # 16. rlm_lake bridge: store from kernel, read back, cross-check file
        lake_dir = tempfile.mkdtemp()
        lake_file = os.path.join(lake_dir, "lake.jsonl")
        k2 = Kernel(python, env={"RLM_LAKE_FILE": lake_file})
        try:
            r = k2.execute("rlm_lake.store('testkey', 'hello lake ' * 100, ['tag1'])")
            check("rlm_lake.store", r["result"] and r["result"]["ok"], str(r))
            r = k2.execute("rlm_lake.get('testkey')")
            check("rlm_lake.get", r["result"] and "hello lake" in r["result"]["repr"], str(r))
            r = k2.execute("rlm_lake.search('hello')")
            check("rlm_lake.search", r["result"] and "testkey" in r["result"]["repr"], str(r))
            r = k2.execute("rlm_lake.stats()")
            check("rlm_lake.stats", r["result"] and "testkey" in r["result"]["repr"], str(r))
            # The entry must be on disk (plugin tools read the same file)
            with open(lake_file, encoding="utf-8") as f:
                content = f.read()
            check("rlm_lake persisted to disk", "testkey" in content and "hello lake" in content,
                  f"file={content[:80]!r}")
        finally:
            k2.close()

        # 17. Concurrent forget — two kernels share a lake; neither should lose data
        import threading, time as _time
        lake_dir3 = tempfile.mkdtemp()
        lake_file3 = os.path.join(lake_dir3, "concurrent_lake.jsonl")

        kA = Kernel(python, env={"RLM_LAKE_FILE": lake_file3})
        kB = Kernel(python, env={"RLM_LAKE_FILE": lake_file3})
        try:
            # A stores 10 entries, B stores 10 different entries
            for i in range(10):
                r = kA.execute(f"rlm_lake.store('a_key_{i}', 'from A entry {i}')")
                check(f"concurrent A store {i}", r["result"] and r["result"]["ok"], str(r))
            for i in range(10):
                r = kB.execute(f"rlm_lake.store('b_key_{i}', 'from B entry {i}')")
                check(f"concurrent B store {i}", r["result"] and r["result"]["ok"], str(r))

            # Now A forgets its own keys; B reads back — all 20 should survive
            r = kA.execute("rlm_lake.forget('a_key_')")
            check("concurrent forget returned count",
                  r["result"] and str(r["result"]["repr"]).startswith("10") , str(r))

            # B verifies all its keys are still there
            got_b = []
            for i in range(10):
                r2 = kB.execute(f"rlm_lake.get('b_key_{i}')")
                got_b.append(r2["result"]["repr"] if r2["result"] else None)
            check("concurrent: all B keys survive forget",
                  all(got_b), f"B entries missing: {got_b}")

            # A reads back its keys — should be gone
            a_still = []
            for i in range(3):  # sample check
                r2 = kA.execute(f"rlm_lake.get('a_key_{i}')")
                a_still.append(r2["result"]["repr"] if r2["result"] else "MISSING")
            check("concurrent: A keys removed after forget",
                  all(s == "None" for s in a_still), str(a_still))

            # Final count: exactly 10 entries on disk
            r = kA.execute("rlm_lake.stats()")
            stats_repr = r["result"]["repr"] if r["result"] else ""
            check("concurrent: stats shows 10 entries", "10" in stats_repr,
                  f"stats repr={stats_repr}")

            # Verify file content — only 10 lines
            with open(lake_file3, encoding="utf-8") as f:
                lines = [l for l in f.read().strip().split("\n") if l.strip()]
            check("concurrent: file has exactly 10 lines", len(lines) == 10,
                  f"expected 10 lines, got {len(lines)}: {lines[:3]}...")
        finally:
            kA.close()
            kB.close()

        print("\nAll kernel tests passed.")
    finally:
        k.close()


if __name__ == "__main__":
    main()