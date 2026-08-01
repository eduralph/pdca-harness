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

from pdca_harness import gates, progress
from pdca_harness.config import Config, LeafConfig


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


class CodexStream(unittest.TestCase):
    """The `codex exec --json` event stream (codex-stream-json), dispatched by format."""

    FMT = "codex-stream-json"

    def _line(self, obj: dict) -> str:
        return json.dumps(obj)

    def test_codex_format_is_registered(self) -> None:
        self.assertIn("codex-stream-json", progress.STREAM_FORMATS)

    def test_command_execution_label(self) -> None:
        ev = {"type": "item.started", "item": {
            "type": "command_execution", "command": "/bin/bash -lc 'pytest -q && ls'"}}
        self.assertEqual(progress._stream_tool_label(self._line(ev), self.FMT),
                         "Running pytest -q && ls")   # shell wrapper unwrapped

    def test_file_change_label(self) -> None:
        ev = {"type": "item.completed", "item": {"type": "file_change",
              "changes": [{"path": "/w/patch.diff", "kind": "add"}]}}
        self.assertEqual(progress._stream_tool_label(self._line(ev), self.FMT),
                         "Adding patch.diff")

    def test_agent_message_has_no_tool_label(self) -> None:
        ev = {"type": "item.completed", "item": {"type": "agent_message", "text": "Done."}}
        self.assertEqual(progress._stream_tool_label(self._line(ev), self.FMT), "")

    def test_session_event_distinguishes_work_from_startup(self) -> None:
        for t in ("item.started", "item.completed", "turn.completed"):
            self.assertTrue(progress._is_session_event(self._line({"type": t}), self.FMT), t)
        for t in ("thread.started", "turn.started"):  # startup only, like claude's `system`
            self.assertFalse(progress._is_session_event(self._line({"type": t}), self.FMT), t)

    def test_claude_parser_is_the_default_format(self) -> None:
        # Back-compat: the claude parser is used when no format is passed.
        self.assertTrue(progress._is_session_event(json.dumps({"type": "assistant"})))
        self.assertFalse(progress._is_session_event(json.dumps({"type": "item.started"})))


class HeartbeatTimeout(unittest.TestCase):
    """The wall-clock bound on ``run_with_heartbeat`` (issue #368).

    A gate command previously had no bound anywhere in the chain, so a hung gate
    held the Check beat indefinitely while the heartbeat printed "… still working".
    On expiry the child's whole process GROUP must die (gates run shell=True — killing
    only the shell orphans the real work) and the distinguishable ``TIMEOUT_RC`` comes
    back, never a verdict the child produced. ``timeout=None`` stays unbounded (every
    other test in this file exercises that default path unchanged).
    """

    @staticmethod
    def _dies(pid: int, within: float = 5.0) -> bool:
        """True once ``pid`` no longer exists (polled — signal delivery is async)."""
        end = time.monotonic() + within
        while time.monotonic() < end:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.05)
        return False

    def test_timeout_kills_a_plain_child_within_the_bound(self) -> None:
        start = time.monotonic()
        rc, _out, produced = progress.run_with_heartbeat(["sleep", "60"], timeout=1)
        self.assertEqual(rc, progress.TIMEOUT_RC)  # distinguishable, not an exit code
        self.assertLess(time.monotonic() - start, 10.0)  # ~1s + kill grace, never 60s
        self.assertFalse(produced)

    def test_shell_true_kills_the_whole_group_no_survivors(self) -> None:
        # Gates run shell=True: the real work is a GRANDCHILD of the shell. The shell
        # prints its own pid ($$ — the group id under start_new_session) and its
        # background child's ($!) before blocking, so the test can verify EVERY group
        # member is gone after expiry — killing only the shell would orphan the sleep.
        cmd = "echo $$; sleep 60 & echo $!; wait"
        start = time.monotonic()
        rc, out, _ = progress.run_with_heartbeat(cmd, shell=True, capture=True, timeout=1)
        self.assertEqual(rc, progress.TIMEOUT_RC)
        self.assertLess(time.monotonic() - start, 10.0)
        pids = [int(tok) for tok in out.split() if tok.isdigit()]
        self.assertEqual(len(pids), 2, f"expected shell + child pids in output: {out!r}")
        for pid in pids:
            self.assertTrue(self._dies(pid), f"pid {pid} survived the group kill")

    def test_unexpired_timeout_returns_the_real_exit_code(self) -> None:
        rc, out, _ = progress.run_with_heartbeat(
            [sys.executable, "-c", "print('ok')"], capture=True, timeout=30)
        self.assertEqual(rc, 0)  # a bound that never expires changes nothing
        self.assertIn("ok", out)


# A real bundle-scoped gating gate row; only cmd/timeout keys vary per test (#368).
_GATE = {"id": "C4", "tier": "C4", "label": "verify", "scope": "bundle", "gating": True}


def _stub_config(root: Path) -> Config:
    return Config(
        root=root,
        bundle_root=root / "results",
        process_dir=root / "process",
        templates_dir=root / "templates",
        default_branch="main",
        tracker_system="github",
        tracker_url="",
        issue_id_example="#1",
        builder=LeafConfig(mode="stub", family="claude"),
        reviewer=LeafConfig(mode="stub", family="codex"),
    )


class GateTimeoutRow(unittest.TestCase):
    """``timeout_secs`` on a ``[[gates.checks]]`` row + the ``[gates]
    default_timeout_secs`` fallback (issue #368): a row that times out is recorded
    ``unverifiable`` — the oracle did not answer (#46) — with the bound named in the
    evidence line, and it never fails ``overall`` (it routes to §6 instead)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _gated_bundle(self, iid: str, gate: dict) -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** to\n", encoding="utf-8")
        (d / "patch.diff").write_text("--- a\n+++ b\n", encoding="utf-8")
        self.cfg.gates_checks = [gate]
        return d

    def _c4_row(self, result: dict) -> dict:
        return next(r for r in result["rows"] if r["element"] == "C4")

    def test_timeout_secs_row_records_unverifiable_with_the_bound_named(self) -> None:
        gate = {**_GATE, "cmd": "sleep 5", "timeout_secs": 1}
        result = gates.run_gates(self._gated_bundle("TO", gate), self.cfg)
        row = self._c4_row(result)
        self.assertEqual(row["result"], "unverifiable")  # not `fail` — no verdict reached
        self.assertIn("exceeded its 1s timeout", row["path_line"])  # the bound, named
        self.assertEqual(result["overall"], "pass")  # kept out of the gating verdict

    def test_default_timeout_secs_is_the_fallback(self) -> None:
        self.cfg.gates_default_timeout_secs = 1
        gate = {**_GATE, "cmd": "sleep 5"}  # no per-row bound → the [gates] fallback
        row = self._c4_row(gates.run_gates(self._gated_bundle("DEF", gate), self.cfg))
        self.assertEqual(row["result"], "unverifiable")
        self.assertIn("exceeded its 1s timeout", row["path_line"])

    def test_row_timeout_wins_over_the_default(self) -> None:
        # timeout_secs = 0 opts a long row OUT of a configured default (unbounded).
        self.cfg.gates_default_timeout_secs = 1
        gate = {**_GATE, "cmd": "sleep 2 && echo done", "timeout_secs": 0}
        row = self._c4_row(gates.run_gates(self._gated_bundle("OPT", gate), self.cfg))
        self.assertEqual(row["result"], "pass")  # ran past the default, unbounded

    def test_no_timeout_configured_leaves_the_gate_unchanged(self) -> None:
        result = gates.run_gates(
            self._gated_bundle("NONE", {**_GATE, "cmd": "echo done"}), self.cfg)
        self.assertEqual(self._c4_row(result)["result"], "pass")
        self.assertEqual(result["overall"], "pass")


if __name__ == "__main__":
    unittest.main()
