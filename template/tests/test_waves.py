"""Offline unit tests for wave partitioning (`waves.compute_waves`) and the overlap
parser (`waves.diff_files`).

Proves the deterministic leveling: a batch is ordered into waves from declared
`Depends on` / `Conflicts with` (docs 09), where a dependent lands in a later wave,
conflicting bundles never share a wave, `Depends on (merged)` / `Stacks on` fold into
plain dependency edges, and an unschedulable graph (cycle / unresolved dep) is rejected
up front. No model, no git — pure computation. Run from the project root:
    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from pdca_harness import waves
from pdca_harness.config import Config, LeafConfig

_BRIEF = "- **Slug:** {slug}\n- **Defect:** off by one.\n- **Repo + branch target:** o/r @ main\n"


def _cfg(root: Path) -> Config:
    return Config(
        root=root, bundle_root=root / "results", process_dir=root / "process",
        templates_dir=root / "templates", default_branch="main", tracker_system="github",
        tracker_url="", issue_id_example="#1",
        builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"))


class ComputeWaves(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _brief(self, iid: str, *, depends_on: str = "", conflicts_with: str = "",
               depends_on_merged: str = "", stacks_on: str = "") -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        body = _BRIEF.format(slug=iid.lower())
        if depends_on:
            body += f"- **Depends on:** {depends_on}\n"
        if depends_on_merged:
            body += f"- **Depends on (merged):** {depends_on_merged}\n"
        if stacks_on:
            body += f"- **Stacks on:** {stacks_on}\n"
        if conflicts_with:
            body += f"- **Conflicts with:** {conflicts_with}\n"
        (d / "brief.md").write_text(body, encoding="utf-8")
        return d

    def _waves(self, *ids: str) -> list[list[str]]:
        bundles = [self.cfg.bundle(i) for i in ids]
        return [[p.name for p in w] for w in waves.compute_waves(self.cfg, bundles)]

    def test_no_fields_single_wave_sorted_by_name(self) -> None:
        # No ordering fields → one wave, sort-by-name — byte-for-byte the prior dispatch.
        for i in ("N3", "N1", "N2"):
            self._brief(i)
        self.assertEqual(self._waves("N3", "N1", "N2"),
                         [["issue_N1", "issue_N2", "issue_N3"]])

    def test_depends_on_orders_into_two_waves(self) -> None:
        # AA depends on ZZ → ZZ in wave 0, AA in wave 1 (despite sort-by-name putting AA first).
        self._brief("AA", depends_on="ZZ")
        self._brief("ZZ")
        self.assertEqual(self._waves("AA", "ZZ"), [["issue_ZZ"], ["issue_AA"]])

    def test_conflict_pair_split_across_waves(self) -> None:
        # No dependency, just a conflict — oriented by name order (CA before CB), so the
        # two never share a wave (each wave has one of them).
        self._brief("CA", conflicts_with="CB")
        self._brief("CB")
        w = self._waves("CA", "CB")
        self.assertEqual(w, [["issue_CA"], ["issue_CB"]])

    def test_conflict_already_dep_ordered_adds_no_level(self) -> None:
        # CB depends on CA AND conflicts with it — the dependency already separates them,
        # so the conflict introduces NO extra wave (2 waves, not 3).
        self._brief("CA")
        self._brief("CB", depends_on="CA", conflicts_with="CA")
        self.assertEqual(self._waves("CA", "CB"), [["issue_CA"], ["issue_CB"]])

    def test_diamond_multi_parent(self) -> None:
        # D depends on B and C; B and C depend on A → [[A],[B,C],[D]]. Exercises a
        # multi-parent dependent (D), which the integration branch handles (and the old
        # _stack_base_branch parents[0] could not).
        self._brief("A")
        self._brief("B", depends_on="A")
        self._brief("C", depends_on="A")
        self._brief("D", depends_on="B, C")
        self.assertEqual(self._waves("A", "B", "C", "D"),
                         [["issue_A"], ["issue_B", "issue_C"], ["issue_D"]])

    def test_merged_and_stacks_fold_into_depends_on(self) -> None:
        # Depends on (merged) (#107) and Stacks on (#123) are subsumed — both produce a
        # plain dependency edge, so the dependents land in the next wave.
        self._brief("MA")
        self._brief("MB", depends_on_merged="MA")
        self._brief("SC", stacks_on="MA")
        self.assertEqual(self._waves("MA", "MB", "SC"),
                         [["issue_MA"], ["issue_MB", "issue_SC"]])

    def test_conflict_clique_serialises(self) -> None:
        # Three mutually-conflicting bundles must each land in their own wave (built on
        # the prior wave's folded result), so the clique serialises into 3 waves.
        self._brief("K1", conflicts_with="K2, K3")
        self._brief("K2", conflicts_with="K3")
        self._brief("K3")
        self.assertEqual(self._waves("K1", "K2", "K3"),
                         [["issue_K1"], ["issue_K2"], ["issue_K3"]])

    def test_cycle_rejected(self) -> None:
        self._brief("X", depends_on="Y")
        self._brief("Y", depends_on="X")
        with self.assertRaises(ValueError):
            self._waves("X", "Y")

    def test_unresolved_dependency_rejected(self) -> None:
        # A dependency neither in the batch nor an existing COMPLETE bundle on disk.
        self._brief("Q", depends_on="GHOST")
        with self.assertRaises(ValueError):
            self._waves("Q")

    def test_empty_batch_is_no_waves(self) -> None:
        self.assertEqual(waves.compute_waves(self.cfg, []), [])


class DiffFiles(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _patch(self, text: str) -> Path:
        p = self.tmp / "patch.diff"
        p.write_text(text, encoding="utf-8")
        return p

    def test_parses_touched_files(self) -> None:
        patch = self._patch(
            "diff --git a/src/foo.py b/src/foo.py\n--- a/src/foo.py\n+++ b/src/foo.py\n"
            "@@ -1 +1 @@\n-x\n+y\n"
            "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
            "@@ -1 +1 @@\n-a\n+b\n")
        self.assertEqual(waves.diff_files(patch), {"src/foo.py", "README.md"})

    def test_added_file_ignores_dev_null(self) -> None:
        patch = self._patch(
            "diff --git a/new.py b/new.py\nnew file mode 100644\n"
            "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+hi\n")
        self.assertEqual(waves.diff_files(patch), {"new.py"})

    def test_missing_patch_is_empty(self) -> None:
        self.assertEqual(waves.diff_files(self.tmp / "nope.diff"), set())

    def test_overlap_detection(self) -> None:
        a = self._patch_named("a.diff", "diff --git a/shared.py b/shared.py\n")
        b = self._patch_named("b.diff", "diff --git a/shared.py b/shared.py\n")
        c = self._patch_named("c.diff", "diff --git a/other.py b/other.py\n")
        self.assertTrue(waves.diff_files(a) & waves.diff_files(b))     # overlap
        self.assertFalse(waves.diff_files(a) & waves.diff_files(c))    # disjoint

    def _patch_named(self, name: str, text: str) -> Path:
        p = self.tmp / name
        p.write_text(text, encoding="utf-8")
        return p


if __name__ == "__main__":
    unittest.main()
