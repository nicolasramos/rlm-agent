#!/usr/bin/env python3
"""Tests for rlm-kernel — verify stdout events carry the correct request id."""

import json
import subprocess
import sys
import os


def _send_and_collect(code: str, rid: str = "test-rid-001") -> list[dict]:
    """Send one execute request and a shutdown, collect all output."""
    _kernel_dir = os.path.join(os.path.dirname(__file__), "..", "kernel")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(_kernel_dir, "kernel.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Send execute request
    execute_req = json.dumps({"id": rid, "type": "execute", "code": code}) + "\n"
    # Send shutdown to terminate the kernel
    shutdown_req = json.dumps({"id": "shutdown-1", "type": "shutdown"}) + "\n"

    if proc.stdin is not None:
        proc.stdin.write(execute_req)
        proc.stdin.write(shutdown_req)
        proc.stdin.flush()

    # Read all output
    output_lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        output_lines.append(line.strip())

    proc.wait(timeout=15)

    # Parse all JSON lines
    events: list[dict] = []
    for line in output_lines:
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    return events


def test_stdout_carries_request_id():
    """stdout events must carry the same id as the request, not null."""
    events = _send_and_collect('print("hello from cell")', rid="cell-abc-123")

    stdout_events = [e for e in events if e.get("event") == "stdout"]
    assert stdout_events, "Expected at least one stdout event"

    for evt in stdout_events:
        assert evt.get("id") == "cell-abc-123", (
            f"stdout event id is {evt.get('id')!r}, expected 'cell-abc-123'"
        )
        assert "hello from cell" in evt.get("text", ""), (
            f"Expected 'hello from cell' in text, got: {evt.get('text')!r}"
        )

    # Verify result also has the correct id
    result_events = [e for e in events if e.get("event") == "result"]
    assert result_events, "Expected a result event"
    assert result_events[0].get("id") == "cell-abc-123", (
        f"result event id is {result_events[0].get('id')!r}"
    )

    print("PASS: test_stdout_carries_request_id")


def test_stdout_before_result():
    """stdout events must arrive BEFORE the result event (same id)."""
    events = _send_and_collect('print("before result")\nx = 42', rid="order-test")

    stdout_events = [e for e in events if e.get("event") == "stdout"]
    result_events = [e for e in events if e.get("event") == "result"]

    assert stdout_events, "Expected stdout events"
    assert result_events, "Expected a result event"

    # The last stdout event must come before the result event in the list
    stdout_last_idx = max(
        i for i, e in enumerate(events) if e.get("event") == "stdout"
    )
    result_first_idx = min(
        i for i, e in enumerate(events) if e.get("event") == "result"
    )

    assert stdout_last_idx < result_first_idx, (
        f"stdout (last at {stdout_last_idx}) must come before result (first at {result_first_idx})"
    )

    print("PASS: test_stdout_before_result")


def test_bash_cell_stdout_has_id():
    """%%bash cells already send with rid; verify they still work."""
    events = _send_and_collect("%%bash\necho 'bash output'", rid="bash-rid")

    stdout_events = [e for e in events if e.get("event") == "stdout"]
    assert stdout_events, "Expected stdout from %%bash"

    for evt in stdout_events:
        assert evt.get("id") == "bash-rid", (
            f"%%bash stdout id is {evt.get('id')!r}, expected 'bash-rid'"
        )

    print("PASS: test_bash_cell_stdout_has_id")


def test_error_events_have_id():
    """Error events must also carry the request id."""
    events = _send_and_collect('1/0', rid="error-rid")

    error_events = [e for e in events if e.get("event") == "error"]
    assert error_events, "Expected an error event"

    assert error_events[0].get("id") == "error-rid", (
        f"error event id is {error_events[0].get('id')!r}"
    )

    print("PASS: test_error_events_have_id")


def test_multiple_prints_same_id():
    """Multiple print() calls in one cell must all carry the same id."""
    events = _send_and_collect(
        'print("first")\nprint("second")\nprint("third")',
        rid="multi-print",
    )

    stdout_events = [e for e in events if e.get("event") == "stdout"]
    assert stdout_events, "Expected at least one stdout event"

    # All stdout events must carry the correct id
    for evt in stdout_events:
        assert evt.get("id") == "multi-print", (
            f"stdout event id is {evt.get('id')!r}, expected 'multi-print'"
        )

    # The combined text should contain all three prints
    combined_text = "".join(e.get("text", "") for e in stdout_events)
    assert "first" in combined_text, f"Expected 'first' in text, got: {combined_text!r}"
    assert "second" in combined_text, f"Expected 'second' in text, got: {combined_text!r}"
    assert "third" in combined_text, f"Expected 'third' in text, got: {combined_text!r}"

    print("PASS: test_multiple_prints_same_id")


if __name__ == "__main__":
    tests = [
        test_stdout_carries_request_id,
        test_stdout_before_result,
        test_bash_cell_stdout_has_id,
        test_error_events_have_id,
        test_multiple_prints_same_id,
    ]

    passed = 0
    failed = 0
    for test_fn in tests:
        try:
            test_fn()
            passed += 1
        except Exception as exc:
            print(f"FAIL: {test_fn.__name__}: {exc}")
            failed += 1

    print(f"\nResults: {passed} passed, {failed} failed out of {len(tests)}")
    sys.exit(1 if failed else 0)
