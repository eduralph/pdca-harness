"""Slice for the reference 'test exercises the production path' gate (issue #154).

A bundle-scoped ADVISORY check: each *added* test file in the patch must import the
production package (PDCA_PROD_PACKAGE); a test that imports nothing from production may be
exercising a hand-ported copy, so the check declares itself UNVERIFIABLE → §6 (it always
exits 0 — never blocks). Driven as a subprocess, exactly as a gate command runs it. Run
from the project root:
    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPT = (Path(__file__).resolve().parents[1]
           / "scripts" / "checks" / "test_exercises_production.py")


class ProdPathGate(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, patch: str | None, *, pkg: str | None = "mypkg") -> tuple[int, str]:
        bundle = self.tmp / "issue_X"
        bundle.mkdir(parents=True, exist_ok=True)
        if patch is not None:
            (bundle / "patch.diff").write_text(patch, encoding="utf-8")
        env = {**os.environ, "PDCA_BUNDLE": str(bundle)}
        env.pop("PDCA_PROD_PACKAGE", None)
        if pkg is not None:
            env["PDCA_PROD_PACKAGE"] = pkg
        r = subprocess.run([sys.executable, str(_SCRIPT)], env=env,
                           capture_output=True, text=True)
        return r.returncode, r.stdout

    def test_test_importing_production_passes(self) -> None:
        patch = ("diff --git a/tests/test_foo.py b/tests/test_foo.py\n"
                 "--- /dev/null\n+++ b/tests/test_foo.py\n@@ -0,0 +1,3 @@\n"
                 "+from mypkg.foo import bar\n+def test_x():\n+    assert bar() == 1\n")
        rc, out = self._run(patch)
        self.assertEqual(rc, 0)
        self.assertNotIn("PDCA-UNVERIFIABLE", out)

    def test_test_without_production_import_is_unverifiable(self) -> None:
        # A test that re-implements `bar()` locally and never imports production.
        patch = ("diff --git a/tests/test_copy.py b/tests/test_copy.py\n"
                 "--- /dev/null\n+++ b/tests/test_copy.py\n@@ -0,0 +1,3 @@\n"
                 "+def bar():\n+    return 1\n+def test_x():\n+    assert bar() == 1\n")
        rc, out = self._run(patch)
        self.assertEqual(rc, 0)                    # advisory — never blocks
        self.assertIn("PDCA-UNVERIFIABLE", out)
        self.assertIn("test_copy.py", out)

    def test_non_test_file_change_passes(self) -> None:
        patch = ("diff --git a/mypkg/foo.py b/mypkg/foo.py\n"
                 "--- a/mypkg/foo.py\n+++ b/mypkg/foo.py\n@@ -1 +1 @@\n-x\n+y\n")
        rc, out = self._run(patch)
        self.assertEqual(rc, 0)
        self.assertNotIn("PDCA-UNVERIFIABLE", out)

    def test_empty_patch_passes(self) -> None:
        rc, out = self._run("")
        self.assertEqual(rc, 0)
        self.assertNotIn("PDCA-UNVERIFIABLE", out)

    def test_edit_to_existing_test_not_flagged(self) -> None:
        # An EDIT to an existing test (no new-file marker) must NOT be flagged even though
        # the added lines don't add a production import — the import is unchanged context.
        patch = ("diff --git a/tests/test_foo.py b/tests/test_foo.py\n"
                 "--- a/tests/test_foo.py\n+++ b/tests/test_foo.py\n"
                 "@@ -3,1 +3,2 @@\n existing\n+    assert bar() == 2\n")
        rc, out = self._run(patch)
        self.assertEqual(rc, 0)
        self.assertNotIn("PDCA-UNVERIFIABLE", out)

    def test_unset_package_is_unverifiable(self) -> None:
        patch = "diff --git a/tests/test_foo.py b/tests/test_foo.py\n+from mypkg import x\n"
        rc, out = self._run(patch, pkg=None)
        self.assertEqual(rc, 0)
        self.assertIn("PDCA-UNVERIFIABLE", out)
        self.assertIn("PDCA_PROD_PACKAGE", out)


if __name__ == "__main__":
    unittest.main()
