#!/usr/bin/env python3
"""Claude Code hooks for RLM.

  snapshot      (PreCompact)  Snapshot the kernel namespace to the project
                              snapshot path before compaction, so variables
                              survive. Talks to the kernel daemon directly
                              over its Unix socket (found via the session
                              marker written by the MCP server).
  restore-hint  (SessionStart, source=compact)  If a snapshot from a previous
                              session exists, inject a hint telling the model
                              to call rlm_restore.

Hooks must never break the editor: every failure path exits 0 silently.
"""

import hashlib
import json
import os
import socket
import sys


def state_dir() -> str:
    return os.environ.get("RLM_STATE_DIR") or os.path.join(os.path.expanduser("~"), ".rlm")


def project_key(cwd: str) -> str:
    return hashlib.sha256(cwd.encode("utf-8")).hexdigest()[:16]


def _read_ready(conn: socket.socket) -> None:
    buf = b""
    while b"\n" not in buf:
        chunk = conn.recv(4096)
        if not chunk:
            raise OSError("kernel connection closed before ready")
        buf += chunk


def cmd_snapshot() -> int:
    cwd = os.getcwd()
    marker = os.path.join(state_dir(), "kernels", f"{project_key(cwd)}.current.json")
    if not os.path.exists(marker):
        return 0  # no kernel in this session: nothing to snapshot
    with open(marker, encoding="utf-8") as f:
        info = json.load(f)
    sock_path = info.get("socket", "")
    if not sock_path or not os.path.exists(sock_path):
        return 0

    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(10)
    try:
        conn.connect(sock_path)
        _read_ready(conn)
        default_path = os.path.join(state_dir(), "snapshots", project_key(cwd), "kernel-state.pkl")
        conn.sendall((json.dumps({"id": "hook-snap", "type": "snapshot", "path": default_path}) + "\n").encode())
        buf = b""
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("id") != "hook-snap":
                    continue
                if ev.get("event") == "result":
                    names = ev.get("names") or []
                    print(f"[rlm-hook] snapshotted {len(names)} variables to {default_path}",
                          file=sys.stderr, flush=True)
                    return 0
                if ev.get("event") == "error":
                    print(f"[rlm-hook] snapshot failed: {ev.get('evalue')}", file=sys.stderr, flush=True)
                    return 0
                if ev.get("event") == "done":
                    return 0
    except (OSError, json.JSONDecodeError):
        return 0  # kernel gone or mid-recycle: compaction proceeds without a snapshot


def cmd_restore_hint() -> int:
    cwd = os.getcwd()
    snap = os.path.join(state_dir(), "snapshots", project_key(cwd), "kernel-state.pkl")
    if not os.path.exists(snap):
        return 0
    hint = (
        "RLM: a kernel snapshot from the previous session exists at "
        f"{snap}. Call mcp__rlm__rlm_restore to reload working variables, "
        "and mcp__rlm__rlm_stats to see what is in the context lake."
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": hint,
        }
    }))
    return 0


def main() -> int:
    try:
        sub = sys.argv[1] if len(sys.argv) > 1 else ""
        if sub == "snapshot":
            return cmd_snapshot()
        if sub == "restore-hint":
            return cmd_restore_hint()
    except Exception:  # noqa: BLE001 — hooks must never break the editor
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
