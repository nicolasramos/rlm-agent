#!/usr/bin/env python3
"""Claude Code hook shim for rlm-agent.

Hooks run in their own process, so they cannot talk to the MCP server's kernel.
The MCP server therefore writes a state summary (~/.rlm/state/<project>/summary.json)
after every cell; this shim reads it and:

  SessionStart → adds the kernel variable list as context (additionalContext)
  PreCompact   → prints instructions for the compaction summary (stdout is used verbatim)
  SessionEnd   → nothing

Wire it in ~/.claude/settings.json:

  {
    "hooks": {
      "SessionStart": [{"hooks": [{"type": "command",
        "command": "python3 ~/.claude/rlm/claude_hook.py"}]}],
      "PreCompact":    [{"hooks": [{"type": "command",
        "command": "python3 ~/.claude/rlm/claude_hook.py"}]}]
    }
  }
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

MAX_CONTEXT = 9_000  # Claude Code caps hook context at 10,000 characters


def _state_dir(project: str) -> Path:
    home = os.environ.get("RLM_HOME")
    root = Path(home).expanduser() if home else Path.home() / ".rlm"
    import hashlib
    key = hashlib.sha256(str(Path(project).expanduser().resolve()).rstrip("/").encode()).hexdigest()[:16]
    return root / "state" / key


def _summary(project: str) -> dict:
    try:
        return json.loads((_state_dir(project) / "summary.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _kernel_lines(summary: dict, limit: int = 25) -> list:
    names = summary.get("names") or []
    return [
        f"- {n.get('name')} ({n.get('type') or '?'}): {(n.get('repr') or '')[:120]}"
        for n in names[:limit]
    ]


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0

    event = payload.get("hook_event_name") or ""
    project = payload.get("cwd") or os.getcwd()
    summary = _summary(project)
    lines = _kernel_lines(summary)

    if event == "SessionStart":
        if not summary.get("names"):
            return 0
        body = (
            "RLM kernel state for this project (persistent Python kernel reachable through the "
            "`rlm` MCP server, tool `ipython`).\n"
            + "\n".join(lines)
            + f"\n{len(summary.get('names') or [])} variables, {summary.get('cells', 0)} cells so far. "
            f"If the kernel was restarted, call `rlm_restore`. Inspect with `rlm_inspect`."
        )
        out = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": body[:MAX_CONTEXT],
            }
        }
        sys.stdout.write(json.dumps(out) + "\n")
        return 0

    if event == "PreCompact":
        if not summary.get("names"):
            return 0
        text = [
            "Preserve this RLM kernel state in the summary. The kernel is a persistent Python "
            "namespace behind the `rlm` MCP server; its variables are NOT part of this conversation "
            "but they still exist on disk.",
            *lines,
            f"Snapshot: {summary.get('snapshot')}",
            "After compaction, continue by calling `rlm_inspect` (and `rlm_restore` if the kernel was "
            "restarted) instead of re-reading files.",
        ]
        sys.stdout.write("\n".join(text)[:MAX_CONTEXT] + "\n")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
