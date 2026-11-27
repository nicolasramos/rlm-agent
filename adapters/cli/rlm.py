#!/usr/bin/env python3
"""rlm — command-line RLM for agents that have a shell but no plugin API.

The MCP server (adapters/mcp/server.py) is the best integration when the client
speaks MCP. This CLI is the compatible one: any agent that can run a command can
use the same kernel and the same context lake.

    rlm exec -c "import pandas as pd; df = pd.read_csv('big.csv'); len(df)"
    rlm exec script.py
    rlm sh "grep -c ERROR app.log"          # the same as a %%bash cell
    echo "print(41 + 1)" | rlm exec
    rlm store logs/build --file build.log --tags build,ci
    rlm search "ERROR|FATAL"
    rlm get logs/build | head -50
    rlm inspect
    rlm mcp                                 # run the MCP server (stdio)

State is shared with every other adapter: the lake is
~/.rlm/lake/<project-hash>.jsonl and the kernel snapshot is
~/.rlm/state/<project-hash>/kernel-state.pkl.

The persistent namespace needs a process that outlives one command, so `exec`
talks to a small broker daemon per project over a Unix socket in ~/.rlm/run/.
It is started on first use and stops itself after RLM_IDLE_TIMEOUT seconds
(default 3600) without traffic, or with `rlm stop`.

Exit codes: 0 ok, 1 the cell raised, 2 usage or transport error.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from rlm_client import (  # installed layout: rlm_client.py next to this file
        ContextLake, Kernel, LAKE_GET_MAX_CHARS, state_dir_for, snippet, rlm_home,
        project_key,
    )
except ImportError:  # repo layout: adapters/cli/rlm.py + kernel/rlm_client.py
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "kernel"))
    from rlm_client import (  # type: ignore[no-redef]
        ContextLake, Kernel, LAKE_GET_MAX_CHARS, state_dir_for, snippet, rlm_home,
        project_key,
    )

VERSION = "0.2.0"
IDLE_TIMEOUT = int(os.environ.get("RLM_IDLE_TIMEOUT", "3600"))
START_TIMEOUT = 8.0


# ─── Paths ────────────────────────────────────────────────────────────────────

def run_dir() -> Path:
    d = rlm_home() / "run"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def socket_path(project: str) -> Path:
    return run_dir() / f"{project_key(project)}.sock"


def log_path(project: str) -> Path:
    return run_dir() / f"{project_key(project)}.log"


def state_file(project: str, name: str) -> Path:
    return state_dir_for(project) / name


# ─── Broker (server side) ─────────────────────────────────────────────────────

class Broker:
    """Owns one kernel process and answers one request per socket connection."""

    def __init__(self, project: str, path: str):
        self.project = project
        self.path = path
        self.kernel = Kernel(project)
        self.last_used = time.time()
        self.cells = 0

    def serve(self) -> int:
        # Never become a second broker for the same project: a short probe tells
        # a live server from a stale socket file.
        if _probe(self.project, 0.2):
            print("another rlm broker is already serving this project", file=sys.stderr)
            return 0
        if os.path.exists(self.path):
            try:
                os.unlink(self.path)
            except OSError:
                pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        srv.listen(16)
        srv.settimeout(1.0)
        try:
            while True:
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    if IDLE_TIMEOUT > 0 and time.time() - self.last_used > IDLE_TIMEOUT:
                        break
                    continue
                with conn:
                    self._handle(conn)
                if self._stop:
                    break
        finally:
            try:
                srv.close()
            except OSError:
                pass
            _unlink_if_ours(self.path, srv)
            self.kernel.shutdown()
        return 0

    _stop = False

    def _handle(self, conn: socket.socket) -> None:
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                return
            buf += chunk
        line, _ = buf.split(b"\n", 1)
        try:
            req = json.loads(line.decode("utf-8"))
        except Exception:
            self._reply(conn, {"ok": False, "error": "malformed request"})
            return
        self.last_used = time.time()
        op = req.get("op")
        try:
            if op == "exec":
                res = self.kernel.execute(req.get("code", ""), int(req.get("timeout") or 120))
                self.cells += 1
                self._write_summary()
                self._maybe_snapshot(req.get("snapshot") or True)
                self._reply(conn, {
                    "ok": res["ok"],
                    "stdout": res["stdout"],
                    "stderr": res["stderr"],
                    "repr": (res["result"] or {}).get("repr"),
                    "error": None if res["ok"] else {
                        "name": (res["error"] or {}).get("ename"),
                        "value": (res["error"] or {}).get("evalue"),
                        "traceback": "".join((res["error"] or {}).get("traceback", [])).strip(),
                    },
                })
            elif op == "names":
                self._reply(conn, {"ok": True, "names": self.kernel.list_names(), "cells": self.cells})
            elif op == "snapshot":
                path = state_file(self.project, "kernel-state.pkl")
                path.parent.mkdir(parents=True, exist_ok=True)
                ev = self.kernel.snapshot(str(path))
                self._reply(conn, {"ok": ev.get("event") != "error", "path": str(path),
                                   "names": len(ev.get("names", [])), "error": ev.get("evalue")})
            elif op == "restore":
                path = state_file(self.project, "kernel-state.pkl")
                if not path.exists():
                    self._reply(conn, {"ok": False, "error": f"no snapshot at {path}"})
                    return
                ev = self.kernel.restore(str(path))
                self._write_summary()
                self._reply(conn, {"ok": ev.get("event") != "error", "names": len(ev.get("names", [])),
                                   "error": ev.get("evalue")})
            elif op == "stop":
                self._stop = True
                self._reply(conn, {"ok": True})
            else:
                self._reply(conn, {"ok": False, "error": f"unknown op {op!r}"})
        except Exception as exc:
            self._reply(conn, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def _reply(self, conn: socket.socket, payload: dict) -> None:
        try:
            conn.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode())
        except OSError:
            pass

    def _write_summary(self) -> None:
        try:
            names = self.kernel.list_names()
            target = state_file(self.project, "summary.json")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({
                "project": self.project,
                "cells": self.cells,
                "names": names[:40],
                "snapshot": str(state_file(self.project, "kernel-state.pkl")),
                "updated": int(time.time() * 1000),
                "adapter": "cli",
            }, ensure_ascii=False, indent=2))
        except Exception:
            pass

    def _maybe_snapshot(self, enabled: bool) -> None:
        if not enabled:
            return
        every = int(os.environ.get("RLM_AUTOSNAPSHOT_SECONDS", "60"))
        if every <= 0:
            return
        marker = run_dir() / f"{project_key(self.project)}.snap"
        try:
            last = float(marker.read_text().strip() or "0")
        except Exception:
            last = 0.0
        if time.time() - last < every:
            return
        try:
            names = self.kernel.list_names()
            if not names:
                return
            path = state_file(self.project, "kernel-state.pkl")
            path.parent.mkdir(parents=True, exist_ok=True)
            self.kernel.snapshot(str(path))
            marker.write_text(str(time.time()))
        except Exception:
            pass


# ─── Client side ──────────────────────────────────────────────────────────────

def _connect(project: str, timeout: float = START_TIMEOUT) -> socket.socket:
    path = socket_path(project)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(str(path))
    return s


def _probe(project: str, wait: float) -> bool:
    """True when a broker answers within `wait` seconds (stale files do not)."""
    deadline = time.time() + wait
    while True:
        try:
            s = _connect(project, timeout=0.5)
            s.close()
            return True
        except OSError:
            if time.time() >= deadline:
                return False
            time.sleep(0.05)


def _unlink_if_ours(path: str, srv: socket.socket) -> None:
    """Remove the socket only when it is still the one this process bound."""
    try:
        if os.stat(path).st_ino == os.fstat(srv.fileno()).st_ino:
            os.unlink(path)
    except OSError:
        pass


def _request(project: str, payload: dict, autostart: bool = True,
             read_timeout: float = 180.0) -> dict:
    try:
        s = _connect(project)
    except (FileNotFoundError, ConnectionRefusedError, OSError):
        if not autostart:
            raise RuntimeError("RLM kernel is not running for this project")
        _start_broker(project)
        s = _connect(project)
    with s:
        s.settimeout(read_timeout)
        s.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    line = buf.split(b"\n", 1)[0]
    if not line:
        raise RuntimeError("kernel broker closed the connection")
    return json.loads(line.decode("utf-8"))


def _start_broker(project: str) -> None:
    """Start the broker, serialized with a lock so two calls cannot race.

    The probe inside the lock retries for a moment on purpose: a connection can
    be refused for a few milliseconds while a broker that another process just
    spawned finishes binding, and spawning on that first refusal is exactly how
    two kernels end up writing the same snapshot.
    """
    import fcntl

    lock_file = socket_path(project).with_suffix(".lock")
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_file, "w") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            if _probe(project, 1.5):
                return  # another process started it while we waited
            log = open(log_path(project), "ab")
            subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "serve", "--project", project],
                cwd=project,
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True,
                env=dict(os.environ),
            )
            if not _probe(project, START_TIMEOUT):
                raise RuntimeError(f"kernel broker did not start; see {log_path(project)}")
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _lake(project: str) -> ContextLake:
    return ContextLake(project)


# ─── Commands ─────────────────────────────────────────────────────────────────

def cmd_exec(args: list) -> int:
    project = os.getcwd()
    code = None
    timeout = 120
    no_snapshot = False
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-c", "--code"):
            i += 1
            code = args[i]
        elif a in ("-t", "--timeout"):
            i += 1
            timeout = int(args[i])
        elif a == "--no-snapshot":
            no_snapshot = True
        elif a in ("-h", "--help"):
            return _usage("exec")
        elif a.startswith("-"):
            print(f"rlm exec: unknown option {a}", file=sys.stderr)
            return _usage("exec")
        else:
            try:
                code = Path(a).read_text()
            except OSError as exc:
                print(f"rlm exec: cannot read {a}: {exc.strerror or exc}", file=sys.stderr)
                return 2
        i += 1
    if code is None:
        if sys.stdin.isatty():
            return _usage("exec")
        code = sys.stdin.read()
    try:
        res = _request(project, {"op": "exec", "code": code, "timeout": timeout,
                                 "snapshot": not no_snapshot}, read_timeout=timeout + 60)
    except Exception as exc:
        print(f"rlm: {exc}", file=sys.stderr)
        return 2
    _print_exec(res)
    return 0 if res.get("ok") else 1


def _print_exec(res: dict) -> None:
    if res.get("stdout"):
        sys.stdout.write(res["stdout"] if res["stdout"].endswith("\n") else res["stdout"] + "\n")
    if res.get("stderr"):
        sys.stdout.write("[stderr] " + res["stderr"].rstrip() + "\n")
    err = res.get("error")
    if err:
        name = err.get("name") or "Error"
        value = err.get("value") or ""
        print(f"Error ({name}): {value}")
        tb = (err.get("traceback") or "").strip()
        if tb:
            print(tb)
        return
    if res.get("repr") is not None:
        print(f"→ {res['repr']}")


def cmd_sh(args: list) -> int:
    command = " ".join(args) if args else ""
    if not command:
        return _usage("sh")
    return cmd_exec(["-c", "%%bash\n" + command])


def cmd_store(args: list) -> int:
    project = os.getcwd()
    key = None
    content = None
    tags: list = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-f", "--file"):
            i += 1
            content = Path(args[i]).read_text(errors="replace")
        elif a in ("-t", "--text"):
            i += 1
            content = args[i]
        elif a == "--tags":
            i += 1
            tags = [t.strip() for t in args[i].split(",") if t.strip()]
        elif a in ("-h", "--help"):
            return _usage("store")
        else:
            key = a
        i += 1
    if not key:
        return _usage("store")
    if content is None:
        if sys.stdin.isatty():
            print("rlm: nothing to store (use --file, --text or pipe on stdin)", file=sys.stderr)
            return 2
        content = sys.stdin.read()
    entry = _lake(project).store(key, content, tags, "cli")
    print(f'Stored "{key}" ({len(entry["content"])} chars). Not in the LLM context.')
    return 0


def cmd_get(args: list) -> int:
    if not args:
        return _usage("get")
    entry = _lake(os.getcwd()).get(args[0])
    if not entry:
        print(f'No entry "{args[0]}" in the context lake.')
        return 1
    content = entry.get("content", "")
    if len(content) > LAKE_GET_MAX_CHARS:
        content = content[:LAKE_GET_MAX_CHARS] + "\n...[truncated]..."
    sys.stdout.write(content if content.endswith("\n") else content + "\n")
    return 0


def cmd_search(args: list) -> int:
    if not args:
        return _usage("search")
    results = _lake(os.getcwd()).search(args[0], 10)
    if not results:
        print(f"No entries match /{args[0]}/ in the context lake.")
        return 1
    for n, e in enumerate(results, 1):
        print(f"{n}. {e['key']} ({len(e.get('content', ''))} chars)")
        print(f"   {snippet(e, args[0])}")
    return 0


def cmd_find(args: list) -> int:
    if not args:
        return _usage("find")
    results = _lake(os.getcwd()).find(args[0], 10)
    if not results:
        print(f'No entries contain "{args[0]}" in the context lake.')
        return 1
    for n, e in enumerate(results, 1):
        print(f"{n}. {e['key']} ({len(e.get('content', ''))} chars)")
        print(f"   {snippet(e, args[0])}")
    return 0


def cmd_stats(args: list) -> int:
    lake = _lake(os.getcwd())
    s = lake.stats()
    if not s["entries"]:
        print("Context lake is empty. Store data with `rlm store`.")
        return 0
    print(f"Context lake: {s['entries']} entries, {s['chars']:,} chars total.")
    print(f"File: {lake.file}")
    print("Keys:")
    for k in s["keys"]:
        print(f"- {k}")
    return 0


def cmd_forget(args: list) -> int:
    if not args:
        return _usage("forget")
    removed = _lake(os.getcwd()).forget(args[0])
    if removed:
        print(f"Removed {removed} entries matching /{args[0]}/.")
        return 0
    print(f"No entries match /{args[0]}/.")
    return 1


def cmd_snapshot(args: list) -> int:
    project = os.getcwd()
    try:
        res = _request(project, {"op": "snapshot"})
    except Exception as exc:
        print(f"rlm: {exc}", file=sys.stderr)
        return 2
    if not res.get("ok"):
        print(f"Snapshot failed: {res.get('error')}")
        return 1
    print(f"Snapshot saved to {res['path']} — {res.get('names', 0)} variables.")
    return 0


def cmd_restore(args: list) -> int:
    project = os.getcwd()
    path = state_file(project, "kernel-state.pkl")
    if not path.exists():
        print(f"No snapshot found at {path}. Use `rlm snapshot` first.")
        return 1
    try:
        res = _request(project, {"op": "restore"})
    except Exception as exc:
        print(f"rlm: {exc}", file=sys.stderr)
        return 2
    if not res.get("ok"):
        print(f"Restore failed: {res.get('error')}")
        return 1
    print(f"Restored {res.get('names', 0)} variables from {path}.")
    return 0


def cmd_inspect(args: list) -> int:
    project = os.getcwd()
    print(f"project: {project}")
    print(f"state dir: {state_dir_for(project)}")
    try:
        res = _request(project, {"op": "names"}, autostart=False)
        names = res.get("names") or []
        print(f"kernel: running, {len(names)} variables, {res.get('cells', 0)} cells")
        for n in names[:40]:
            print(f"- {n.get('name')} ({n.get('type')}): {n.get('repr')}")
    except Exception:
        print("kernel: not running (start it with `rlm exec`)")
    path = state_file(project, "kernel-state.pkl")
    print(f"snapshot: {path} ({'present' if path.exists() else 'absent'})")
    s = _lake(project).stats()
    print(f"lake: {s['entries']} entries, {s['chars']:,} chars ({_lake(project).file})")
    return 0


def cmd_list(args: list) -> int:
    try:
        res = _request(os.getcwd(), {"op": "names"}, autostart=False)
    except Exception:
        print("RLM kernel is not running for this project.")
        return 1
    names = res.get("names") or []
    if not names:
        print("Kernel is running but holds no named variables.")
        return 0
    for n in names:
        print(f"{n.get('name')} ({n.get('type')}): {n.get('repr')}")
    return 0


def cmd_stop(args: list) -> int:
    project = os.getcwd()
    path = socket_path(project)
    if not path.exists():
        print("RLM kernel is not running for this project.")
        return 0
    try:
        _request(project, {"op": "stop"}, autostart=False)
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
    print("RLM kernel stopped.")
    return 0


def cmd_serve(args: list) -> int:
    project = os.getcwd()
    i = 0
    while i < len(args):
        if args[i] == "--project":
            i += 1
            project = os.path.abspath(args[i])
        i += 1
    return Broker(project, str(socket_path(project))).serve()


def cmd_mcp(args: list) -> int:
    here = Path(__file__).resolve().parent
    for candidate in (here.parent / "mcp" / "server.py", here / "server.py"):
        if candidate.exists():
            os.execv(sys.executable, [sys.executable, str(candidate), *args])
    print("rlm: MCP server not found next to this command", file=sys.stderr)
    return 2


COMMANDS = {
    "exec": cmd_exec,
    "sh": cmd_sh,
    "store": cmd_store,
    "get": cmd_get,
    "search": cmd_search,
    "find": cmd_find,
    "stats": cmd_stats,
    "forget": cmd_forget,
    "snapshot": cmd_snapshot,
    "restore": cmd_restore,
    "inspect": cmd_inspect,
    "list": cmd_list,
    "stop": cmd_stop,
    "serve": cmd_serve,
    "mcp": cmd_mcp,
}

USAGE = """rlm — persistent Python kernel + context lake for any agent with a shell

  rlm exec -c "code"        run a cell in the persistent kernel (prints stdout, then → value)
  rlm exec file.py          run a file as a cell
  echo "code" | rlm exec    run stdin as a cell
  rlm sh "shell command"    run a shell command inside the kernel (%%bash)
  rlm store KEY [--file F] [--text S] [--tags a,b]
  rlm get KEY               print a lake entry
  rlm search REGEX          list lake keys matching a regex, with snippets
  rlm find TEXT             list lake keys containing an exact text
  rlm stats                 lake size, file and keys
  rlm forget REGEX          drop lake entries
  rlm snapshot | restore    persist / reload the kernel namespace
  rlm inspect               kernel variables, snapshot and lake in one report
  rlm list                  kernel variables only
  rlm stop                  stop this project's kernel process
  rlm mcp                   run the MCP server (stdio) for MCP clients

State: {home}
Exit codes: 0 ok, 1 the cell raised, 2 usage or transport error.
"""


def _usage(command: str = "") -> int:
    text = USAGE.format(home=rlm_home())
    if command:
        for block in text.split("\n\n"):
            if f"rlm {command}" in block:
                print(block.strip())
                return 0
    print(text.strip())
    return 0 if not command else 2


def main(argv: list) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        _usage()
        return 0
    if argv[0] in ("-v", "--version", "version"):
        print(f"rlm-agent {VERSION}")
        return 0
    cmd = COMMANDS.get(argv[0])
    if cmd is None:
        print(f"rlm: unknown command {argv[0]!r}", file=sys.stderr)
        _usage()
        return 2
    return cmd(argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
