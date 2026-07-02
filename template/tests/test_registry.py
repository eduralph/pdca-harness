"""Reverse registry-consistency (issue #205) — the pure diff predicate + the gate CLI.

Locks in that a patch adding a manifest line for a path it doesn't touch FAILS, while a
patch that adds a file and registers *that* file passes; plus comment/blank handling, a
non-bare-path `pattern`, and the not-configured / no-patch default-open paths. Run from the
project root:
    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pdca_harness import cli, registry
from pdca_harness.config import Config, LeafConfig

# A patch that adds a POTFILES.skip line for a file it never touches (the issue_10554 bug).
_CONTAMINATED = """\
diff --git a/po/POTFILES.skip b/po/POTFILES.skip
index 1111..2222 100644
--- a/po/POTFILES.skip
+++ b/po/POTFILES.skip
@@ -1,1 +1,2 @@
 gramps/already/registered.py
+gramps/plugins/importer/test/importxml_daterange_test.py
"""

# A patch that adds a new source file AND registers exactly that file — consistent.
_CONSISTENT = """\
diff --git a/gramps/plugins/foo.py b/gramps/plugins/foo.py
new file mode 100644
--- /dev/null
+++ b/gramps/plugins/foo.py
@@ -0,0 +1 @@
+print("hi")
diff --git a/po/POTFILES.in b/po/POTFILES.in
index 1111..2222 100644
--- a/po/POTFILES.in
+++ b/po/POTFILES.in
@@ -1,1 +1,2 @@
 gramps/existing.py
+gramps/plugins/foo.py
"""


class FindViolations(unittest.TestCase):
    REG = ["po/POTFILES.in", "po/POTFILES.skip"]

    def test_contaminated_line_is_a_violation(self) -> None:
        v = registry.find_violations(_CONTAMINATED, self.REG)
        self.assertEqual(len(v), 1)
        self.assertIn("importxml_daterange_test.py", v[0])

    def test_registering_a_touched_file_is_clean(self) -> None:
        self.assertEqual(registry.find_violations(_CONSISTENT, self.REG), [])

    def test_not_configured_is_open(self) -> None:
        # No registry files declared ⇒ nothing to enforce (default-open).
        self.assertEqual(registry.find_violations(_CONTAMINATED, []), [])

    def test_comment_and_blank_added_lines_are_ignored(self) -> None:
        diff = (
            "diff --git a/po/POTFILES.in b/po/POTFILES.in\n"
            "--- a/po/POTFILES.in\n+++ b/po/POTFILES.in\n"
            "@@ -1 +1,3 @@\n existing.py\n+# a comment, not a path\n+\n")
        self.assertEqual(registry.find_violations(diff, self.REG), [])

    def test_pattern_extracts_path_from_non_bare_manifest(self) -> None:
        diff = (
            "diff --git a/MANIFEST.in b/MANIFEST.in\n"
            "--- a/MANIFEST.in\n+++ b/MANIFEST.in\n"
            "@@ -1 +1,2 @@\n include kept.txt\n+include untouched/x.txt\n")
        v = registry.find_violations(diff, ["MANIFEST.in"], pattern=r"^include (.+)$")
        self.assertEqual(len(v), 1)
        self.assertIn("untouched/x.txt", v[0])

    def test_touched_paths_includes_both_rename_sides(self) -> None:
        diff = "diff --git a/old/name.py b/new/name.py\n"
        self.assertEqual(registry.touched_paths(diff), {"old/name.py", "new/name.py"})


class RegistryCheckCLI(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cfg(self, files: list[str]) -> Config:
        return Config(
            root=self.tmp, bundle_root=self.tmp / "results", process_dir=self.tmp / "process",
            templates_dir=self.tmp / "templates", default_branch="main",
            tracker_system="github", tracker_url="", issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
            registry_consistency={"files": files})

    def _bundle(self, cfg: Config, patch: str) -> Path:
        d = cfg.bundle("X")
        d.mkdir(parents=True)
        (d / "patch.diff").write_text(patch, encoding="utf-8")
        return d

    def test_exit_1_on_violation(self) -> None:
        cfg = self._cfg(["po/POTFILES.skip"])
        self._bundle(cfg, _CONTAMINATED)
        self.assertEqual(cli._registry_check(cfg, SimpleNamespace(issue_id="X")), 1)

    def test_exit_0_when_consistent(self) -> None:
        cfg = self._cfg(["po/POTFILES.in"])
        self._bundle(cfg, _CONSISTENT)
        self.assertEqual(cli._registry_check(cfg, SimpleNamespace(issue_id="X")), 0)

    def test_exit_0_when_not_configured(self) -> None:
        cfg = self._cfg([])
        self._bundle(cfg, _CONTAMINATED)
        self.assertEqual(cli._registry_check(cfg, SimpleNamespace(issue_id="X")), 0)

    def test_exit_0_when_no_patch(self) -> None:
        cfg = self._cfg(["po/POTFILES.skip"])
        d = cfg.bundle("X")
        d.mkdir(parents=True)  # close/no-fix bundle: no patch.diff
        self.assertEqual(cli._registry_check(cfg, SimpleNamespace(issue_id="X")), 0)


if __name__ == "__main__":
    unittest.main()
