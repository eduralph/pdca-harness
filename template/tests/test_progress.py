"""Unit tests for the heartbeat status probe (stdlib unittest — no deps).

``progress.bundle_activity`` turns a watched directory into the one-line snapshot
the heartbeat appends each tick: which expected artifacts exist yet, and how long
since the newest write (so a stalled leaf is visible). It is project-agnostic —
Tier 1 only; a project whose leaves run a long containerized job can extend it
with its own runner probe. Run from the project root:
    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

from pdca_harness import progress


class BundleActivity(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_artifacts_present_and_absent(self) -> None:
        (self.tmp / "patch.diff").write_text("x" * 2048, encoding="utf-8")
        s = progress.bundle_activity(self.tmp, ("patch.diff", "build-notes.md"))
        self.assertIn("patch.diff ✓ 2.0KB", s)
        self.assertIn("build-notes.md —", s)  # not written yet

    def test_fresh_write_shows_seconds(self) -> None:
        (self.tmp / "patch.diff").write_text("x", encoding="utf-8")
        self.assertRegex(progress.bundle_activity(self.tmp), r"last write \d+s ago")

    def test_quiet_dir_warns(self) -> None:
        f = self.tmp / "old.txt"
        f.write_text("x", encoding="utf-8")
        old = time.time() - 400  # >5 min since the last write
        os.utime(f, (old, old))
        self.assertIn("⚠ no writes", progress.bundle_activity(self.tmp))

    def test_probe_never_raises_on_missing_dir(self) -> None:
        self.assertEqual(progress.bundle_activity(self.tmp / "does-not-exist"), "")

    def test_fmt_size_boundaries(self) -> None:
        self.assertEqual(progress._fmt_size(512), "512B")
        self.assertEqual(progress._fmt_size(2048), "2.0KB")
        self.assertEqual(progress._fmt_size(2 * 1024 * 1024), "2.0MB")


class StreamToolUse(unittest.TestCase):
    """Tier 3 — parse Claude's --output-format stream-json for the live tool-use."""

    @staticmethod
    def _line(name: str, inp: dict) -> str:
        return json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]},
        })

    def test_tool_label_per_tool(self) -> None:
        self.assertEqual(progress._tool_label("Edit", {"file_path": "/a/patch.diff"}),
                         "Editing patch.diff")
        self.assertEqual(progress._tool_label("Write", {"file_path": "b/build-notes.md"}),
                         "Editing build-notes.md")
        self.assertEqual(progress._tool_label("Read", {"file_path": "/x/glade.py"}),
                         "Reading glade.py")
        self.assertEqual(progress._tool_label("Bash", {"command": "./run-tests foo\nbar"}),
                         "Running ./run-tests foo")
        self.assertEqual(progress._tool_label("Grep", {"pattern": "navigation_type"}),
                         "Searching navigation_type")
        self.assertEqual(progress._tool_label("Task", {"description": "find flaky tests"}),
                         "Subagent: find flaky tests")
        self.assertEqual(progress._tool_label("WeirdTool", {}), "WeirdTool")

    def test_stream_line_extracts_tool_use(self) -> None:
        self.assertEqual(
            progress._stream_tool_label(self._line("Edit", {"file_path": "p/patch.diff"})),
            "Editing patch.diff")

    def test_stream_line_last_tool_use_wins(self) -> None:
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "b.py"}},
        ]}})
        self.assertEqual(progress._stream_tool_label(line), "Editing b.py")

    def test_non_tool_lines_yield_empty(self) -> None:
        self.assertEqual(progress._stream_tool_label("not json at all"), "")
        self.assertEqual(progress._stream_tool_label(
            json.dumps({"type": "user", "message": {"content": []}})), "")
        self.assertEqual(progress._stream_tool_label(json.dumps({"type": "assistant",
                         "message": {"content": [{"type": "text", "text": "hi"}]}})), "")
        self.assertEqual(progress._stream_tool_label(json.dumps({"type": "result"})), "")

    def test_is_session_event_distinguishes_work_from_system(self) -> None:
        self.assertTrue(progress._is_session_event(json.dumps({"type": "assistant"})))
        self.assertTrue(progress._is_session_event(json.dumps({"type": "result"})))
        self.assertFalse(progress._is_session_event(
            json.dumps({"type": "system", "subtype": "init"})))
        self.assertFalse(progress._is_session_event(
            json.dumps({"type": "system", "subtype": "api_retry"})))
        self.assertFalse(progress._is_session_event("not json"))

    def test_run_with_heartbeat_consumes_stream_json(self) -> None:
        # Wiring smoke: a json-emitting child runs cleanly under stream_json (stdout is
        # consumed for parsing, not captured/echoed) and returns its exit code. The
        # child emitted stdout, so a session started → produced is True.
        prog = "print('{\"type\": \"result\"}')"
        rc, out, produced = progress.run_with_heartbeat(
            [sys.executable, "-c", prog], stream_json=True)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")  # stdout is parsed, not captured; this child wrote no stderr
        self.assertTrue(produced)

    def test_stream_json_tees_stderr_tail_and_flags_no_session(self) -> None:
        # A child that dies at invocation — only stderr, no stdout stream event — is the
        # transient signal (#138): the stderr survives as the returned tail and
        # produced is False (no session started).
        prog = "import sys; sys.stderr.write('overloaded_error 529\\n'); sys.exit(1)"
        rc, out, produced = progress.run_with_heartbeat(
            [sys.executable, "-c", prog], stream_json=True)
        self.assertEqual(rc, 1)
        self.assertIn("overloaded_error 529", out)  # stderr teed into the tail
        self.assertFalse(produced)  # no stdout → no session started

    def test_system_init_event_is_not_substantive(self) -> None:
        # Claude emits system/init (and api_retry) BEFORE doing work. A session that
        # printed only those then died on a rate limit must still read as transient
        # (produced False) so the leaf is retried (#138 — Codex review on PR #140).
        prog = ("import sys; print('{\"type\": \"system\", \"subtype\": \"init\"}'); "
                "sys.exit(1)")
        rc, _out, produced = progress.run_with_heartbeat(
            [sys.executable, "-c", prog], stream_json=True)
        self.assertEqual(rc, 1)
        self.assertFalse(produced)  # only a system event → no real work → transient


if __name__ == "__main__":
    unittest.main()
