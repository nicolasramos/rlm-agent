#!/usr/bin/env python3
"""Claude Code PostToolUse guard for RLM (requires Claude Code >= 2.1.121).

When a tool returns more than RLM_GUARD_THRESHOLD_CHARS of text (default
200 KB), the full output is stored in the project context lake and the tool
output is replaced with a short digest (head + tail + key, under 4000 chars)
via ``hookSpecificOutput.updatedToolOutput``. Folding is CHARACTER-based:
line-based folding silently no-ops on single-line (minified) output.

The model retrieves the full content with mcp__rlm__rlm_get / rlm_search /
rlm_find. Hooks must never break the editor: every failure path exits 0
without output, leaving the original tool result untouched.
"""

import hashlib
import json
import os
import sys
import time

THRESHOLD = int(os.environ.get("RLM_GUARD_THRESHOLD_CHARS", "200000"))
HEAD = int(os.environ.get("RLM_GUARD_HEAD_CHARS", "2500"))
TAIL = int(os.environ.get("RLM_GUARD_TAIL_CHARS", "1000"))
GUARD_MARKER = "[WATCH GUARD]"


def state_dir() -> str:
    return os.environ.get("RLM_STATE_DIR") or os.path.join(os.path.expanduser("~"), ".rlm")


def lake_file_for(cwd: str) -> str:
    key = hashlib.sha256(cwd.encode("utf-8")).hexdigest()[:16]
    return os.path.join(state_dir(), "lake", f"{key}.jsonl")


def clip_head(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    cut = s[:n]
    nl = cut.rfind("\n")
    if nl > n * 0.1:
        return cut[:nl] + "\n..."
    return cut


def clip_tail(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    cut = s[-n:]
    nl = cut.find("\n")
    if nl > n * 0.1:
        return "...\n" + cut[nl + 1:]
    return cut


def digest(key: str, content: str) -> str:
    head = clip_head(content, HEAD)
    tail = clip_tail(content, TAIL)
    body = head + "\n\n...\n\n" + tail
    marker = (
        f"\n\n{GUARD_MARKER} Output completo ({len(content):,} chars) guardado en el context lake. "
        f"Recupera con mcp__rlm__rlm_get(key: \"{key}\") o busca con mcp__rlm__rlm_search / mcp__rlm__rlm_find."
    )
    if len(body) + len(marker) < len(content):
        return body + marker
    return content  # folding did not help: leave the output untouched


def store(key: str, content: str, tags) -> None:
    path = lake_file_for(os.getcwd())
    now = int(time.time() * 1000)
    entry = {
        "key": key,
        "content": content,
        "tags": tags,
        "source": "watch_guard",
        "created": now,
        "updated": now,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def extract_text(response):
    """Return the text the model will see, or None when the response is not
    a plain-text payload we are willing to fold."""
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        # Fold only a single dominant text field (e.g. Bash "stdout");
        # structured/compound responses are left untouched.
        for field in ("stdout", "output", "content"):
            if isinstance(response.get(field), str):
                return response[field]
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        return 0
    tool_name = payload.get("tool_name") or "tool"
    text = extract_text(payload.get("tool_response"))
    if text is None or len(text) <= THRESHOLD:
        return 0
    key = f"auto/{int(time.time())}/{tool_name}"
    try:
        store(key, text, ["auto", "watch-guard", tool_name])
    except OSError:
        return 0
    folded = digest(key, text)
    if len(folded) >= len(text):
        return 0
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": folded,
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
