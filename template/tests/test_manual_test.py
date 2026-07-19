"""Offline slice for `pdca try <id>` — the manual-test launch (stdlib unittest, no deps).

Proves the handler MATERIALIZES the patched tree on demand (worktree.stage, reconstructed
from patch.diff) and launches [manual_test].cmd from it with the PDCA_* env, and fails closed
(never spawning anything) on the operator-error paths: missing config, missing/unbuilt bundle,
worktree isolation off, no stageable tree. subprocess.run is mocked so no app is ever started.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pdca_harness import cli, lane, manual_test, worktree
from pdca_harness.config import Config, LeafConfig


def _cfg(root: Path, *, cmd: str = "python -m gramps", worktree_on: bool = True) -> Config:
    return Config(
        root=root,
        bundle_root=root / "results",
        process_dir=root / "process",
        templates_dir=root / "templates",
        default_branch="main",
        tracker_system="github",
        tracker_url="",
        issue_id_example="#1",
        builder=LeafConfig(mode="stub"),
        reviewer=LeafConfig(mode="stub"),
        manual_test_cmd=cmd,
        worktree=worktree_on,
    )


class ManualTestLaunch(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _built_bundle(self, iid: str = "X") -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "patch.diff").write_text("--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y\n", encoding="utf-8")
        return d

    def _fake_worktree(self, owner: str | None = "issue_X") -> Path:
        wt = self.tmp / "target.pdca-wt"
        (wt / ".git").mkdir(parents=True)
        if owner is not None:  # the marker ensure() stamps; None ⇒ unconfirmed
            wt.with_name(wt.name + ".owner").write_text(owner, encoding="utf-8")
        return wt

    def test_happy_path_launches_from_worktree_with_env(self) -> None:
        self._built_bundle()
        wt = self._fake_worktree()
        with mock.patch.object(worktree, "stage", return_value=wt), \
                mock.patch.object(manual_test.subprocess, "run") as run:
            run.return_value = SimpleNamespace(returncode=0)
            rc = manual_test.launch(self.cfg, "X")
        self.assertEqual(rc, 0)
        run.assert_called_once()
        args, kwargs = run.call_args
        self.assertEqual(args[0], "python -m gramps")
        self.assertTrue(kwargs["shell"])
        self.assertEqual(kwargs["cwd"], str(wt))
        env = kwargs["env"]
        self.assertEqual(env["PDCA_WORKTREE"], str(wt))
        self.assertEqual(env["PDCA_TARGET"], str(wt))
        self.assertEqual(env["PDCA_BUNDLE"], str(self.cfg.bundle("X")))

    def test_passes_through_app_exit_code(self) -> None:
        self._built_bundle()
        with mock.patch.object(worktree, "stage", return_value=self._fake_worktree()), \
                mock.patch.object(manual_test.subprocess, "run") as run:
            run.return_value = SimpleNamespace(returncode=3)
            self.assertEqual(manual_test.launch(self.cfg, "X"), 3)

    def test_lane_exported_when_set(self) -> None:
        self._built_bundle()
        wt = self._fake_worktree()
        with mock.patch.object(worktree, "stage", return_value=wt), \
                mock.patch.object(lane, "current", return_value=2), \
                mock.patch.object(manual_test.subprocess, "run") as run:
            run.return_value = SimpleNamespace(returncode=0)
            manual_test.launch(self.cfg, "X")
        self.assertEqual(run.call_args.kwargs["env"]["PDCA_LANE"], "2")

    def test_lane_absent_when_serial(self) -> None:
        self._built_bundle()
        with mock.patch.object(worktree, "stage", return_value=self._fake_worktree()), \
                mock.patch.object(lane, "current", return_value=None), \
                mock.patch.object(manual_test.subprocess, "run") as run:
            run.return_value = SimpleNamespace(returncode=0)
            manual_test.launch(self.cfg, "X")
        self.assertNotIn("PDCA_LANE", run.call_args.kwargs["env"])

    def test_missing_config_exits_2_without_launching(self) -> None:
        self.cfg.manual_test_cmd = ""
        self._built_bundle()
        with mock.patch.object(worktree, "stage", return_value=self._fake_worktree()), \
                mock.patch.object(manual_test.subprocess, "run") as run:
            self.assertEqual(manual_test.launch(self.cfg, "X"), 2)
            run.assert_not_called()

    def test_missing_bundle_exits_1(self) -> None:
        with mock.patch.object(manual_test.subprocess, "run") as run:
            self.assertEqual(manual_test.launch(self.cfg, "NOPE"), 1)
            run.assert_not_called()

    def test_unbuilt_bundle_exits_1(self) -> None:
        d = self.cfg.bundle("X")
        d.mkdir(parents=True)  # PLANNED / close-no-fix: no patch.diff
        with mock.patch.object(manual_test.subprocess, "run") as run:
            self.assertEqual(manual_test.launch(self.cfg, "X"), 1)
            run.assert_not_called()

    def test_empty_patch_exits_1(self) -> None:
        d = self.cfg.bundle("X")
        d.mkdir(parents=True)
        (d / "patch.diff").write_text("   \n", encoding="utf-8")
        with mock.patch.object(manual_test.subprocess, "run") as run:
            self.assertEqual(manual_test.launch(self.cfg, "X"), 1)
            run.assert_not_called()

    def test_no_worktree_exits_1_without_launching(self) -> None:
        self._built_bundle()
        with mock.patch.object(worktree, "stage", return_value=None), \
                mock.patch.object(manual_test.subprocess, "run") as run:
            self.assertEqual(manual_test.launch(self.cfg, "X"), 1)
            run.assert_not_called()

    def test_launch_holds_the_lane_lock_for_the_whole_session(self) -> None:
        # #297 review round 8: while the human drives the app, the footprint sweeper
        # (and any Do/gate run) must find the lane BUSY — without the lock a sweep
        # could clean/reset/remove the patched tree beneath the active validation.
        import contextlib
        self._built_bundle()
        events: list = []

        @contextlib.contextmanager
        def recording_lock(d, cfg, *, wait):
            events.append(("lock", wait))
            yield
            events.append(("unlock",))

        def fake_run(*a, **kw):
            events.append(("run",))
            return SimpleNamespace(returncode=0)

        with mock.patch.object(worktree, "lane_lock", recording_lock), \
                mock.patch.object(worktree, "stage", return_value=self._fake_worktree()), \
                mock.patch.object(manual_test.subprocess, "run", side_effect=fake_run):
            self.assertEqual(manual_test.launch(self.cfg, "X"), 0)
        # Non-blocking acquire, and the interactive command ran INSIDE the lock.
        self.assertEqual(events, [("lock", False), ("run",), ("unlock",)])

    def test_busy_lane_refuses_the_manual_test(self) -> None:
        # A Do/gate run owns the lane right now — refuse with a reason rather than
        # stage over (and later be swept under) its critical section.
        self._built_bundle()
        with mock.patch.object(worktree, "lane_lock",
                               side_effect=worktree.WorktreeError("lane busy")), \
                mock.patch.object(manual_test.subprocess, "run") as run:
            self.assertEqual(manual_test.launch(self.cfg, "X"), 1)
            run.assert_not_called()

    def test_dispatch_via_cli(self) -> None:
        # Exercise the full parser → dispatch → handler path; stub Config.load so it uses
        # our built bundle instead of hunting for a pdca.toml in cwd.
        self._built_bundle()
        wt = self._fake_worktree()
        with mock.patch.object(cli.Config, "load", return_value=self.cfg), \
                mock.patch.object(worktree, "stage", return_value=wt), \
                mock.patch.object(manual_test.subprocess, "run") as run:
            run.return_value = SimpleNamespace(returncode=0)
            rc = cli.main(["try", "X"])
        self.assertEqual(rc, 0)
        run.assert_called_once()


class ManualTestConfig(unittest.TestCase):
    def test_config_reads_manual_test_cmd(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "pdca.toml").write_text(
                '[project]\nname = "x"\n[leaves.builder]\nfamily = "claude"\n'
                '[leaves.reviewer]\nfamily = "codex"\n'
                '[manual_test]\ncmd = "python -m gramps"\n',
                encoding="utf-8")
            cfg = Config.load(tmp)
            self.assertEqual(cfg.manual_test_cmd, "python -m gramps")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_config_defaults_empty_when_absent(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "pdca.toml").write_text(
                '[project]\nname = "x"\n[leaves.builder]\nfamily = "claude"\n'
                '[leaves.reviewer]\nfamily = "codex"\n',
                encoding="utf-8")
            self.assertEqual(Config.load(tmp).manual_test_cmd, "")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
