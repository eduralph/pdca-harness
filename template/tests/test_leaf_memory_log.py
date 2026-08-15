"""Leaf memory telemetry — the #420 bound's observability arm. Stdlib unittest only.

The bound made an OOM kill attributable but not EXPLAINABLE: the measured incident
(a builder's test run forked ~1200 python3 processes and filled its 16G scope in
under a minute) ended as `died with <Signals.SIGKILL: 9>` and nothing else — the
kernel's task table names bare comms, the stderr tail of a SIGKILLed leaf is empty,
and `--collect` erases the cgroup before a human can read it. What this pins:

  1. sampling — a tick reads the scope cgroup and aggregates its population by
     COMMAND LINE (the datum the kernel's own OOM table lacks), appending JSONL;
  2. attribution discipline — until the child is seen in a cgroup of its own,
     a tick samples NOTHING (the shared cgroup would attribute the whole session);
  3. post-mortem — a failed leaf's LeafError carries the last sample + the scope's
     journal excerpt, riding the existing #138/#279 capture into `*.error.log`;
  4. wiring — the hook exists exactly when the spawn is capped AND a log was named;
     `_invoke_leaf_resilient` derives the `*.memory.jsonl` twin of its error log;
     `do_build` wires the builder's; `run_with_heartbeat` calls the hook per tick.

The scope itself is faked (`proc_root` / `cgroup_root` point at a temp tree) — there
is no hermetic way to enter a real one. Run from the project root:
    PYTHONPATH=src python -m unittest tests.test_leaf_memory_log
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from pdca_harness import leaves, progress
from pdca_harness.config import Config, LeafConfig

_BOUND = "16G"
_LEAF_ARGV = ["fake-vendor-cli", "-p"]
_PAGE = os.sysconf("SC_PAGE_SIZE")
_UNIT = "run-p1-i1.scope"


def _cfg(root: Path, **kw) -> Config:
    return Config(
        root=root,
        bundle_root=root / "results",
        process_dir=root / "process",
        templates_dir=root / "templates",
        default_branch="main",
        tracker_system="github",
        tracker_url="",
        issue_id_example="1",
        builder=LeafConfig(mode="stub"),
        reviewer=LeafConfig(mode="stub"),
        **kw,
    )


class _FakeScope(unittest.TestCase):
    """Base: a fake /proc + /sys/fs/cgroup tree with one scope holding 3 processes —
    two of the same command line (the storm shape) and one other (the leaf itself)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.proc = self.tmp / "proc"
        self.sys = self.tmp / "sys"
        self.log = self.tmp / "build.memory.jsonl"
        (self.proc / "self").mkdir(parents=True)
        (self.proc / "self" / "cgroup").write_text("0::/u/terminal\n", encoding="utf-8")
        # pid 777: the spawned child, inside its own scope.
        self._proc_entry(777, cmd=b"claude\0-p\0", rss_pages=100)
        (self.proc / "777" / "cgroup").write_text(f"0::/u/{_UNIT}\n", encoding="utf-8")
        # pids 300/301: the storm — one command line, twice. pid 777 rides along.
        storm = b"python3\0-m\0unittest\0tests.test_x\0"
        self._proc_entry(300, cmd=storm, rss_pages=2000)
        self._proc_entry(301, cmd=storm, rss_pages=2000)
        self.scope = self.sys / "u" / _UNIT
        self.scope.mkdir(parents=True)
        (self.scope / "memory.current").write_text(str(2 * 1024 ** 3), encoding="utf-8")
        (self.scope / "memory.peak").write_text(str(3 * 1024 ** 3), encoding="utf-8")
        (self.scope / "cgroup.procs").write_text("777\n300\n301\n", encoding="utf-8")
        self.tel = leaves._MemoryTelemetry(
            self.log, _BOUND, proc_root=self.proc, cgroup_root=self.sys)

    def _proc_entry(self, pid: int, *, cmd: bytes, rss_pages: int) -> None:
        d = self.proc / str(pid)
        d.mkdir(parents=True)
        (d / "cmdline").write_bytes(cmd)
        (d / "comm").write_text(cmd.split(b"\0")[0].decode() + "\n", encoding="utf-8")
        (d / "statm").write_text(f"{rss_pages * 2} {rss_pages} 0 0 0 0 0\n",
                                 encoding="utf-8")

    def _records(self) -> list[dict]:
        return [json.loads(ln) for ln in
                self.log.read_text(encoding="utf-8").splitlines()]


class TelemetrySampling(_FakeScope):
    def test_a_tick_samples_the_scope_and_aggregates_by_cmdline(self) -> None:
        suffix = self.tel.tick(777)
        # The heartbeat suffix: usage against the bound, and the population count —
        # the live early-warning a human can act on before the kernel does.
        self.assertEqual(suffix, f"mem 2.0GB/{_BOUND} · 3 procs")
        rec = self._records()[-1]
        self.assertEqual(rec["memory"], 2 * 1024 ** 3)
        self.assertEqual(rec["peak"], 3 * 1024 ** 3)
        self.assertEqual(rec["procs"], 3)
        self.assertEqual(rec["unit"], _UNIT)
        # The aggregation IS the diagnosis: the kernel's OOM table showed 1187
        # indistinguishable "python3" rows; per-cmdline grouping names the argv once.
        top = rec["top"][0]
        self.assertEqual(top["cmd"], "python3 -m unittest tests.test_x")
        self.assertEqual(top["n"], 2)
        self.assertEqual(top["rss"], 2 * 2000 * _PAGE)

    def test_the_spawn_record_marks_the_attempt_boundary(self) -> None:
        self.assertEqual(self._records(), [{"event": "spawn", "bound": _BOUND}])

    def test_no_sample_until_the_child_enters_a_scope_of_its_own(self) -> None:
        # pid 888 still shares the harness's cgroup — sampling it would attribute the
        # whole terminal session to the leaf, the unattributable state #420 removed.
        d = self.proc / "888"
        d.mkdir()
        (d / "cgroup").write_text("0::/u/terminal\n", encoding="utf-8")
        self.assertEqual(self.tel.tick(888), "")
        self.assertEqual(len(self._records()), 1)  # the spawn record only

    def test_a_vanished_scope_yields_nothing_not_a_crash(self) -> None:
        self.tel.tick(777)
        shutil.rmtree(self.scope)  # the leaf exited; --collect reaped the cgroup
        self.assertEqual(self.tel.tick(777), "")

    def test_memory_peak_is_optional(self) -> None:
        (self.scope / "memory.peak").unlink()  # pre-5.19 kernels have no such file
        self.assertTrue(self.tel.tick(777))
        self.assertIsNone(self._records()[-1]["peak"])

    def test_a_process_exiting_mid_sample_is_skipped(self) -> None:
        (self.scope / "cgroup.procs").write_text("777\n300\n999\n", encoding="utf-8")
        self.assertIn("3 procs", self.tel.tick(777))  # 999 raced away: still a sample
        self.assertEqual(sum(t["n"] for t in self._records()[-1]["top"]), 2)

    def test_an_argv_with_newlines_stays_on_one_line(self) -> None:
        # `python3 -c "…\n…"` — the measured live-check shape: raw, it breaks the
        # post-mortem's one-command-one-line layout.
        (self.proc / "777" / "cmdline").write_bytes(b"python3\0-c\0import x\ny()\0")
        self.tel.tick(777)
        cmds = [t["cmd"] for t in self._records()[-1]["top"]]
        self.assertIn("python3 -c import x y()", cmds)
        self.assertFalse([c for c in cmds if "\n" in c])


class PostMortem(_FakeScope):
    def test_the_post_mortem_names_the_storm_and_the_journal_verdict(self) -> None:
        self.tel.tick(777)
        with mock.patch.object(leaves, "_scope_journal",
                               return_value=["systemd: Failed with result 'oom-kill'."]):
            text = self.tel.post_mortem(-9)
        self.assertIn(f"bound {_BOUND}", text)
        self.assertIn("2.0GB used", text)
        self.assertIn("peak 3.0GB", text)
        self.assertIn("2× python3 -m unittest tests.test_x", text)
        self.assertIn("Failed with result 'oom-kill'", text)
        self.assertIn(str(self.log), text)  # points at the full sample series
        exit_rec = self._records()[-1]
        self.assertEqual(exit_rec["event"], "exit")
        self.assertEqual(exit_rec["rc"], -9)

    def test_a_leaf_dead_before_the_first_sample_still_gets_a_post_mortem(self) -> None:
        with mock.patch.object(leaves, "_scope_journal", return_value=[]) as journal:
            text = self.tel.post_mortem(-9)
        self.assertIn("no sample captured", text)
        # Unit unknown (the harvest is best-effort), but the SINCE bound still holds:
        # an unbounded harvest hands a previous run's OOM kill to this leaf.
        journal.assert_called_once_with("", self.tel.wall_start)


class SpawnWiring(unittest.TestCase):
    """The hook exists exactly when the spawn is capped AND a log was named."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = _cfg(self.tmp)
        self.log = self.tmp / "build.memory.jsonl"
        self.spawned: dict = {}
        self.heartbeat_rc = 0

        def fake_heartbeat(argv, **kw):
            self.spawned["argv"] = list(argv)
            self.spawned["kw"] = dict(kw)
            return self.heartbeat_rc, "stderr-tail", True

        self._patch(progress, "run_with_heartbeat", fake_heartbeat)
        self._patch(leaves, "_memory_cap_supported", lambda argv: True)
        leaves._MEMORY_CAP_DECISION.clear()
        self.addCleanup(leaves._MEMORY_CAP_DECISION.clear)

    def _patch(self, obj, name: str, value) -> None:
        original = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(setattr, obj, name, original)

    def _leaf(self) -> LeafConfig:
        return LeafConfig(mode="command", family="generic", argv=list(_LEAF_ARGV))

    def test_a_capped_spawn_with_a_log_gets_the_hook(self) -> None:
        setattr(self.cfg, "leaf_memory_max", _BOUND)
        leaves._invoke(self._leaf(), self.tmp, "P", cfg=self.cfg, memory_log=self.log)
        self.assertTrue(callable(self.spawned["kw"].get("telemetry")))
        self.assertTrue(self.log.exists(), "the spawn record opens the log")

    def test_an_unbounded_spawn_gets_no_hook_and_writes_no_log(self) -> None:
        # Without a scope there is nothing attributable to sample: the telemetry
        # against the shared cgroup would be the whole session's numbers.
        leaves._invoke(self._leaf(), self.tmp, "P", cfg=self.cfg, memory_log=self.log)
        self.assertIsNone(self.spawned["kw"].get("telemetry"))
        self.assertFalse(self.log.exists())

    def test_a_capped_spawn_without_a_log_gets_no_hook(self) -> None:
        setattr(self.cfg, "leaf_memory_max", _BOUND)
        leaves._invoke(self._leaf(), self.tmp, "P", cfg=self.cfg)
        self.assertIsNone(self.spawned["kw"].get("telemetry"))

    def test_a_failed_capped_leaf_carries_the_post_mortem_in_its_error(self) -> None:
        setattr(self.cfg, "leaf_memory_max", _BOUND)
        self.heartbeat_rc = -9
        with mock.patch.object(leaves, "_scope_journal", return_value=["kernel: oom"]):
            with self.assertRaises(leaves.LeafError) as ctx:
                leaves._invoke(self._leaf(), self.tmp, "P", cfg=self.cfg,
                               memory_log=self.log)
        # Both halves in one place: what the leaf said, then what it consumed —
        # `_format_leaf_attempt` writes this output verbatim into `*.error.log`.
        self.assertIn("stderr-tail", ctx.exception.output)
        self.assertIn("memory telemetry", ctx.exception.output)
        self.assertIn("kernel: oom", ctx.exception.output)

    def test_resilient_derives_and_clears_the_error_logs_twin(self) -> None:
        error_log = self.tmp / "check-review.error.log"
        stale = self.tmp / "check-review.memory.jsonl"
        stale.write_text('{"event":"spawn"}\n', encoding="utf-8")
        seen: dict = {}

        def fake_invoke(leaf, workdir, prompt, **kw):
            seen.update(kw)

        with mock.patch.object(leaves, "_invoke", side_effect=fake_invoke):
            leaves._invoke_leaf_resilient(self._leaf(), self.tmp, "P",
                                          error_log=error_log)
        self.assertEqual(seen.get("memory_log"), stale)
        self.assertFalse(stale.exists(),
                         "a stale telemetry file describes a run this cycle never had")

    def test_do_build_wires_the_builders_memory_log(self) -> None:
        cfg = _cfg(self.tmp, worktree=False)
        cfg.builder = LeafConfig(mode="command", family="generic", argv=list(_LEAF_ARGV))
        d = cfg.bundle("M1")
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** m\n", encoding="utf-8")
        seen: dict = {}

        def fake_invoke(leaf, workdir, prompt, **kw):
            seen.update(kw)

        with mock.patch.object(leaves, "_invoke", side_effect=fake_invoke), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            leaves.do_build(d, cfg)
        self.assertEqual(seen.get("memory_log"), d / leaves.BUILD_MEMORY_LOG)


class HeartbeatHook(unittest.TestCase):
    """`run_with_heartbeat` drives the hook: the child's pid, per tick, best-effort."""

    def test_telemetry_is_called_with_the_childs_pid_and_decorates_the_tick(self) -> None:
        calls: list[int] = []

        def probe(pid: int) -> str:
            calls.append(pid)
            return "mem 1.0GB/16G · 3 procs"

        err = io.StringIO()
        with redirect_stderr(err):
            rc, _, _ = progress.run_with_heartbeat(
                [sys.executable, "-c", "import time; time.sleep(1.3)"],
                interval=1, telemetry=probe)
        self.assertEqual(rc, 0)
        # Once at t≈0 (a leaf dying before the first tick was still observed once),
        # then on each tick.
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(len(set(calls)), 1, "every call carries the same child pid")
        self.assertIn("mem 1.0GB/16G · 3 procs", err.getvalue())

    def test_a_raising_probe_never_breaks_the_run(self) -> None:
        with redirect_stderr(io.StringIO()):
            rc, _, _ = progress.run_with_heartbeat(
                [sys.executable, "-c", "import time; time.sleep(1.1)"],
                interval=1, telemetry=lambda pid: 1 / 0)
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
