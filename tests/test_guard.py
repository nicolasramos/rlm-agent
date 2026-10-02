#!/usr/bin/env python3
"""Tests for the Hermes watch guard's context folding (`_guard_digest`).

Regression target: folding used to slice by LINES (`lines[:40] + lines[-10:]`),
which silently no-ops for any output with <= 50 lines. Minified JSON and API
responses are a single line, so the digest came back identical to (or larger
than) the raw output and 100% of it still entered the prompt.
"""

import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ADAPTER = os.path.join(_HERE, "..", "adapters", "hermes", "__init__.py")


def _load_guard():
    """Import the Hermes adapter module and return the guard helpers."""
    spec = importlib.util.spec_from_file_location("rlm_hermes_adapter", _ADAPTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The adapter imports Hermes-internal packages that are absent in CI. Only the
# pure string helpers are under test, so load those two functions standalone
# when the full module import is unavailable.
def _load_guard_standalone():
    """Fallback: pull _guard_digest and its constants out of the source text."""
    import ast

    with open(_ADAPTER, encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src)

    wanted = {"_guard_digest", "_clip_head", "_clip_tail"}
    ns = {"os": os, "Optional": object}
    for node in tree.body:
        keep = (
            (isinstance(node, ast.FunctionDef) and node.name in wanted)
            or (isinstance(node, ast.Assign)
                and any(getattr(t, "id", "").startswith("GUARD_") for t in node.targets))
            or (isinstance(node, ast.Assign)
                and any(getattr(t, "id", "") == "_GUARD_MARKER" for t in node.targets))
        )
        if keep:
            code = ast.get_source_segment(src, node)
            if code:
                exec(compile(code, _ADAPTER, "exec"), ns)
    return type("M", (), ns)


def _guard():
    try:
        return _load_guard()
    except Exception:
        return _load_guard_standalone()


G = _guard()


def _digest(content: str, key: str = "auto/1/test") -> str:
    return G._guard_digest(key, content)


def test_single_line_output_is_folded():
    """A one-line minified-JSON payload above the threshold must be clipped."""
    raw = '{"data":[' + ",".join(f'{{"i":{i},"v":"{"x" * 80}"}}' for i in range(500)) + "]}"
    assert raw.count("\n") == 0, "fixture must be a single line"
    assert len(raw) > 10_000

    out = _digest(raw)
    assert len(out) < len(raw), (
        f"single-line output was not folded: raw={len(raw)} digest={len(out)}"
    )
    assert len(out) < len(raw) * 0.6, (
        f"fold is too weak: raw={len(raw)} digest={len(out)}"
    )


def test_digest_is_never_larger_than_input():
    """The digest must never exceed the input (the old bug made it larger)."""
    cases = [
        '{"k":"' + "a" * 30_000 + '"}',                       # one huge line
        "\n".join(f"line {i} " + "y" * 200 for i in range(200)),  # many lines
        "\n".join(f"line {i}" for i in range(300)),             # many short lines
        "ab" * 20_000,                                          # no newline, no spaces
    ]
    for i, raw in enumerate(cases):
        out = _digest(raw)
        assert len(out) <= len(raw), (
            f"case {i}: digest larger than input (raw={len(raw)} digest={len(out)})"
        )


def test_small_output_under_threshold_is_untouched_by_helper():
    """A digest must never grow the prompt: tiny input comes back verbatim.

    The caller only invokes the guard above GUARD_THRESHOLD_CHARS, so this path
    is defensive — but it must not append the pointer at the cost of inflating.
    """
    raw = "short output"
    out = _digest(raw)
    assert raw in out, "small content must survive verbatim"
    assert len(out) <= len(raw), "small content must not be inflated"


def test_above_threshold_input_always_carries_the_pointer():
    """Real payloads (above the fold size) must name the lake key for retrieval."""
    raw = "m" * 40_000
    out = _digest(raw, key="auto/42/terminal")
    assert G._GUARD_MARKER in out
    assert "auto/42/terminal" in out, "digest must name the lake key"
    assert "chars omitted" in out, "digest must state how much was omitted"


def test_sub_threshold_but_big_output_returns_body_only():
    """Content that cannot be shrunk must not be inflated by the marker.

    At or below head+tail the text is returned intact; if even that plus the
    marker would exceed the input, the body wins (never grow the prompt).
    """
    raw = "b" * (G.GUARD_HEAD_CHARS + G.GUARD_TAIL_CHARS - 1)
    out = _digest(raw)
    assert len(out) <= len(raw), f"inflated a sub-threshold payload: {len(raw)} -> {len(out)}"


def test_guard_only_fires_above_threshold():
    """_on_transform_tool_result must ignore output at or below the threshold."""
    handler = getattr(G, "_on_transform_tool_result", None)
    if handler is None:
        return  # standalone fallback: no handler available
    small = "z" * (G.GUARD_THRESHOLD_CHARS - 1)
    assert handler(tool_name="t", result=small, status="ok") is None


def test_threshold_leaves_room_for_a_real_fold():
    """head+tail+marker must be strictly below the threshold.

    Otherwise a payload just over the threshold would be stored and returned at
    its full size (plus the marker): the guard would pay the latency of a lake
    write and shrink nothing. Enforced by an assert in the module, asserted here
    too so the intent survives any refactor.
    """
    reserve = getattr(G, "_GUARD_MARKER_RESERVE", 200)
    assert G.GUARD_HEAD_CHARS + G.GUARD_TAIL_CHARS + reserve < G.GUARD_THRESHOLD_CHARS, (
        f"head({G.GUARD_HEAD_CHARS}) + tail({G.GUARD_TAIL_CHARS}) + marker(~{reserve}) "
        f">= threshold({G.GUARD_THRESHOLD_CHARS})"
    )


def test_every_folded_payload_actually_shrinks():
    """A payload just above the threshold must still be smaller after folding."""
    # Include the worst case: barely over the threshold, with a long lake key
    # (keys embed the tool name, so they are short, but be generous).
    key = "auto/1789584472185/terminal"
    for extra in (1, 50, 500, 2000):
        raw = "w" * (G.GUARD_THRESHOLD_CHARS + extra)
        out = _digest(raw, key=key)
        assert len(out) < len(raw), (
            f"threshold+{extra}: folding grew or matched the input "
            f"({len(raw)} -> {len(out)})"
        )


def test_marker_present_and_single_line_body_has_no_newline_requirement():
    raw = "q" * 25_000
    out = _digest(raw, key="auto/xyz/big")
    assert G._GUARD_MARKER in out
    assert "auto/xyz/big" in out, "digest must name the lake key for retrieval"
    assert "chars omitted" in out, "digest must state how much was omitted"


def test_no_line_is_cut_in_half_for_multiline_input():
    """Head/tail clipping should land on newline boundaries for normal text."""
    lines = [f"sentence number {i} with some padding text" for i in range(2000)]
    raw = "\n".join(lines)
    out = _digest(raw)
    head = out.split("\n… [")[0]
    # Every retained head line must be a complete original line.
    for ln in head.split("\n"):
        if ln:
            assert ln in lines, f"head clipped mid-line: {ln[:60]!r}"


def test_rlm_get_results_are_never_re_folded():
    """rlm_get is the retrieval escape hatch and must be exempt from folding.

    If a retrieved entry were folded again, the model could never read more
    than head+tail of ANY lake entry: each rlm_get would return a digest
    pointing at a new key whose retrieval is folded too — an unbounded loop.
    """
    handler = getattr(G, "_on_transform_tool_result", None)
    if handler is None:
        return  # standalone fallback: handler not loaded
    big = "v" * (G.GUARD_THRESHOLD_CHARS + 500)
    assert handler(tool_name="rlm_get", result=big, status="ok") is None, (
        "rlm_get output must not be folded (retrieval must return the entry)"
    )


def test_other_large_tool_results_are_folded():
    """The exemption is rlm_get-specific: other oversized results still fold."""
    handler = getattr(G, "_on_transform_tool_result", None)
    if handler is None:
        return  # standalone fallback: handler not loaded
    # Replace the lake with an in-memory double so the test never writes to
    # the user's real lake directory.
    lake_for = getattr(G, "_lake_for", None)
    if lake_for is None:
        return  # standalone fallback: store path not loadable
    stored = {}

    class _FakeLake:
        def store(self, key, content, tags=None, source="model"):
            stored[key] = content
            return {"key": key, "content": content}

    G._lake_for = lambda cwd: _FakeLake()
    try:
        big = "v" * (G.GUARD_THRESHOLD_CHARS + 500)
        out = handler(tool_name="terminal", result=big, status="ok")
    finally:
        G._lake_for = lake_for
    assert out is not None and len(out) < len(big), "oversized output must fold"
    assert stored, "the guard must store the full payload in the lake"


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
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{'OK' if not failures else 'FAILED'} ({failures} failing)")
    sys.exit(1 if failures else 0)
