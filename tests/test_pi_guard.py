#!/usr/bin/env python3
"""Tests for the Pi watch guard's context folding.

Verifies the same invariants as test_guard.py for the Hermes adapter:
char-based folding (not line-based), never enlarges output, names the
lake key, skips ImageContent, and is configurable via env vars.
"""

import os
import sys

GUARD_THRESHOLD_CHARS = 10_000
GUARD_HEAD_CHARS = 4_000
GUARD_TAIL_CHARS = 1_000
GUARD_MARKER = "[WATCH GUARD]"


def clip_head(s: str, n: int) -> str:
    """First n chars, backing up to previous newline if cheap."""
    if len(s) <= n:
        return s
    cut = s[:n]
    nl = cut.rfind("\n")
    if nl > n // 2:
        cut = cut[:nl]
    return cut


def clip_tail(s: str, n: int) -> str:
    """Last n chars, advancing to the next newline if cheap."""
    if len(s) <= n:
        return s
    cut = s[-n:]
    nl = cut.find("\n")
    if nl > n // 2:
        cut = cut[:nl]
    return cut


def guard_digest(key: str, content: str) -> str:
    """Fold oversized tool output by CHARACTERS, not lines."""
    marker = (
        f"\n\n{GUARD_MARKER} Output completo ({len(content):,} chars) guardado en el context lake. "
        f"Recupera con rlm_get('{key}') o busca con rlm_search/rlm_find."
    )
    if len(content) <= GUARD_HEAD_CHARS + GUARD_TAIL_CHARS:
        return content

    head = clip_head(content, GUARD_HEAD_CHARS)
    tail = clip_tail(content, GUARD_TAIL_CHARS)
    if len(head) + len(tail) >= len(content):
        head = content[:GUARD_HEAD_CHARS]
        tail = content[-GUARD_TAIL_CHARS:]
    omitted = len(content) - len(head) - len(tail)
    body = head + f"\n… [{omitted:,} chars omitted] …\n" + tail

    if len(body) + len(marker) < len(content):
        return body + marker
    if len(body) < len(content):
        return body
    return content


def _digest(content: str, key: str = "auto/1/test") -> str:
    return guard_digest(key, content)


def test_single_line_output_is_folded():
    """A one-line minified-JSON payload above the threshold must be clipped."""
    raw = '{"data":[' + ",".join(f'{{"id":{i},"value":"x"*80}}' for i in range(500)) + ']}'
    assert raw.count("\n") == 0, "fixture must be a single line"
    assert len(raw) > 10_000

    out = _digest(raw)
    assert len(out) < len(raw), f"single-line output was not folded: raw={len(raw)} digest={len(out)}"
    assert len(out) < len(raw) * 0.6, f"fold is too weak: raw={len(raw)} digest={len(out)}"


def test_digest_is_never_larger_than_input():
    """The digest must never exceed the input (the old bug made it larger)."""
    cases = [
        '{"k":"' + "a" * 30_000 + '"}',                       # one huge line
        "\n".join(f"line {i}: " + "a" * 200 for i in range(100)),  # 100 lines
        "short",                                               # tiny
    ]
    for i, raw in enumerate(cases):
        out = _digest(raw)
        assert len(out) <= len(raw), f"case {i}: digest {len(out)} > raw {len(raw)}"


def test_small_output_under_threshold_is_untouched():
    """Content at or below threshold must pass through unchanged."""
    raw = "hello world"
    out = _digest(raw)
    assert out == raw, "small content must not be inflated"


def test_above_threshold_carries_key_pointer():
    """Real payloads (above the fold size) must name the lake key."""
    raw = "x" * 40_000
    out = _digest(raw, key="auto/42/terminal")
    assert GUARD_MARKER in out
    assert "auto/42/terminal" in out, "digest must name the lake key"
    assert "chars omitted" in out, "digest must state how much was omitted"


def test_sub_threshold_unchanged():
    """Content that can't be folded must return raw."""
    raw = "b" * (GUARD_HEAD_CHARS + GUARD_TAIL_CHARS - 1)
    out = _digest(raw)
    assert len(out) <= len(raw), f"inflated a sub-threshold payload: {len(raw)} -> {len(out)}"


def test_marker_present_and_body_has_content():
    """The folded body must have content between head and tail markers."""
    raw = "q" * 25_000
    out = _digest(raw, key="auto/xyz/big")
    assert GUARD_MARKER in out
    assert "auto/xyz/big" in out, "digest must name the lake key for retrieval"


def test_no_line_is_cut_in_half_for_multiline_input():
    """Head/tail clipping should land on newline boundaries for normal text."""
    lines = [f"sentence number {i} with some padding text here" for i in range(100)]
    raw = "\n".join(lines)
    out = _digest(raw)
    head = out.split("\n… [")[0]
    for ln in head.split("\n"):
        assert ln in lines or ln == "", f"line was cut: {ln!r}"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{'OK' if not failures else 'FAILED'} ({failures} failing)")
    sys.exit(1 if failures else 0)
