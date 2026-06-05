"""Unit tests for the heartbeat status probe (stdlib unittest — no deps).

``progress.bundle_activity`` turns a watched directory into the one-line snapshot
the heartbeat appends each tick: which expected artifacts exist yet, and how long
since the newest write (so a stalled leaf is visible). It is project-agnostic —
Tier 1 only; a project whose leaves run a long containerized job can extend it
with its own runner probe. Run from the project root:
    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import shutil
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


if __name__ == "__main__":
    unittest.main()
