"""A failed Do builder leaves its error tail in the bundle (#279, secondary).

The reviewer / advisory leaves capture a failed leaf's stderr to `check-*.error.log`
(`_invoke_leaf_resilient`, #138), but the Do builder called `_invoke` directly — so a failed
Do left NO on-disk trace at all. Its stderr was only tee'd to the terminal, and a post-mortem
of a failed batch depended on terminal scrollback. `do_build` now captures the tail to
`build.error.log`, symmetric with the review leaves, and still re-raises so the flow's
`_isolate` drops just that bundle.

Offline: no model, no network. Run from the project root:
    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from pdca_harness import leaves
from pdca_harness.config import Config, LeafConfig


def _cfg(root: Path) -> Config:
    return Config(
        root=root,
        bundle_root=root / "results",
        process_dir=root / "process",
        templates_dir=root / "templates",
        default_branch="main",
        tracker_system="github",
        tracker_url="",
        issue_id_example="#1",
        builder=LeafConfig(mode="command", family="claude", argv=["claude"]),
        reviewer=LeafConfig(mode="stub", family="codex"),
        worktree=False,          # edit in place — keep the slice free of git
    )


class BuildErrorLog(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp)
        self.d = self.cfg.bundle("ERR")
        self.d.mkdir(parents=True)
        (self.d / "brief.md").write_text("- **Slug:** e\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_failing_build(self, exc: Exception) -> None:
        with mock.patch.object(leaves, "_invoke", side_effect=exc), \
                redirect_stderr(io.StringIO()):
            with self.assertRaises(type(exc)):      # still propagates — _isolate handles it
                leaves.do_build(self.d, self.cfg)

    def test_a_failed_builder_persists_its_error_tail(self) -> None:
        self._run_failing_build(
            leaves.LeafError(1, ["claude"], output="panic: could not open the worktree"))
        log = self.d / leaves.BUILD_ERROR_LOG
        self.assertTrue(log.exists(), "a failed Do must leave a recoverable trace on disk")
        text = log.read_text(encoding="utf-8")
        self.assertIn("panic: could not open the worktree", text)
        self.assertIn("exit 1", text)

    def test_a_missing_binary_is_captured_too(self) -> None:
        # The [Errno 2] 'claude' shape — no output to capture, so the exception text is.
        self._run_failing_build(FileNotFoundError(2, "No such file or directory", "claude"))
        text = (self.d / leaves.BUILD_ERROR_LOG).read_text(encoding="utf-8")
        self.assertIn("no output captured", text)
        self.assertIn("FileNotFoundError", text)

    def test_a_successful_build_leaves_no_error_log(self) -> None:
        with mock.patch.object(leaves, "_invoke", return_value=None):
            leaves.do_build(self.d, self.cfg)
        self.assertFalse((self.d / leaves.BUILD_ERROR_LOG).exists())

    def test_a_stale_log_is_cleared_on_the_next_attempt(self) -> None:
        # A prior cycle's failure must not masquerade as this one's.
        (self.d / leaves.BUILD_ERROR_LOG).write_text("stale failure\n", encoding="utf-8")
        with mock.patch.object(leaves, "_invoke", return_value=None):
            leaves.do_build(self.d, self.cfg)
        self.assertFalse((self.d / leaves.BUILD_ERROR_LOG).exists())

    def test_capture_never_masks_the_real_failure(self) -> None:
        # If the log itself can't be written, the builder's exception still reaches the flow.
        with mock.patch.object(leaves, "_invoke",
                               side_effect=leaves.LeafError(1, ["claude"], output="boom")), \
                mock.patch.object(Path, "write_text", side_effect=OSError("read-only fs")), \
                redirect_stderr(io.StringIO()):
            with self.assertRaises(leaves.LeafError):
                leaves.do_build(self.d, self.cfg)


if __name__ == "__main__":
    unittest.main()
