"""`Merges with` names an atomic merge group, and merge mode builds no member it can see (#412).

A group is bundles that must not land on the base separately. In `wave_mode = "merge"` the
driver `gh pr merge`s each non-final wave's PRs one at a time, so a member could land alone.
Until merge mode supports groups (#778), a run with a member is refused on the strict path
(`waves.compute_waves` → `pdca flow <ids>` rc 2, `pdca waves` rc 1) and the group and its
dependents are held on the tolerant one (`waves.partition_schedulable`, the `--from-csv`
sweep). A bundle is a member when its own brief declares a non-empty `Merges with`, or when
another brief in the run names it there. In stack mode the field changes nothing.

Modules are imported, never a symbol the change adds (`from pdca_harness import waves, …`),
so on the C4 red leg (production hunks reverted) the module still loads and every case
fails on an assertion. Fixtures are bundle dirs with a `brief.md` under a temp `results/`,
as `tests/test_waves.py` builds them; the `pdca flow` case copies the setup of
`tests/test_flow_dependency_refusal.py`. No network, no tracker, no `gh`.

    cd template && PYTHONPATH=src python3 -m unittest tests.test_merge_group
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pdca_harness import brief, cli, driver, state, waves
from pdca_harness.config import Config, LeafConfig

_BRIEF = "- **Slug:** {slug}\n- **Defect:** off by one.\n- **Repo + branch target:** o/r @ main\n"
_MERGES = "- **Merges with:**"

#: The batches the criterion names, as {id: extra brief lines}.
#: (a) A declares the group with B.
BATCH_A = {"A": [f"{_MERGES} B"], "B": []}
#: (b) only B declares it; A's brief is silent.
BATCH_B = {"A": [], "B": [f"{_MERGES} A"]}
#: (b) a run of A alone, naming a partner X that is not in the run.
BATCH_B_OUT = {"A": [f"{_MERGES} X"]}
#: (d) A declares the group with B, C depends on A, D has no edge.
BATCH_D = {"A": [f"{_MERGES} B"], "B": [], "C": ["- **Depends on:** A"], "D": []}


def _cfg(root: Path, wave_mode: str) -> Config:
    """The `tests/test_waves.py` config, with the wave mode under test."""
    return Config(
        root=root, bundle_root=root / "results", process_dir=root / "process",
        templates_dir=root / "templates", default_branch="main", tracker_system="github",
        tracker_url="", issue_id_example="#1",
        builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
        wave_mode=wave_mode)


class _Batches(unittest.TestCase):
    def _root(self, wave_mode: str) -> Config:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return _cfg(tmp, wave_mode)

    @staticmethod
    def _write(cfg: Config, batch: dict[str, list[str]], *,
               drop_merges: bool = False) -> list[Path]:
        """One bundle per id; ``drop_merges`` writes the same briefs minus `Merges with`."""
        out = []
        for iid, extra in batch.items():
            d = cfg.bundle(iid)
            d.mkdir(parents=True)
            lines = [ln for ln in extra if not (drop_merges and ln.startswith(_MERGES))]
            (d / "brief.md").write_text(
                _BRIEF.format(slug=iid.lower()) + "".join(ln + "\n" for ln in lines),
                encoding="utf-8")
            out.append(d)
        return out

    @staticmethod
    def _waves(cfg: Config, bundles: list[Path]) -> list[list[str]]:
        return [[p.name for p in w] for w in waves.compute_waves(cfg, bundles)]

    @staticmethod
    def _partition(cfg: Config, bundles: list[Path]) -> tuple[list[str], dict[str, str]]:
        sched, held = waves.partition_schedulable(cfg, bundles)
        return sorted(p.name for p in sched), held

    def _refusal(self, cfg: Config, bundles: list[Path]) -> waves.DependencyGraphError:
        try:
            got = self._waves(cfg, bundles)
        except waves.DependencyGraphError as exc:
            return exc
        self.fail(f"merge mode levelled a `Merges with` group instead of refusing it: {got}")

    def _assert_ways_out(self, text: str) -> None:
        self.assertIn('wave_mode = "stack"', text)
        self.assertIn("fold the group into one bundle", text)


class MergesWithField(unittest.TestCase):
    """The field parses like `Conflicts with`: ids only on the value line, a trailing note
    ignored, and `#12` / `12` / `issue_12` all accepted."""

    def test_parses_like_conflicts_with(self) -> None:
        parse = getattr(brief, "merges_with", None)
        self.assertIsNotNone(parse, "brief.py has no parser for the `Merges with` field")
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bp = tmp / "brief.md"
        for value, want in (("12", ["12"]), ("#12", ["12"]), ("issue_12", ["12"]),
                            ("#12, issue_13 (the caller has to land first)", ["12", "13"]),
                            ("12 — one unit", ["12"]), ("none", []), ("—", [])):
            with self.subTest(value=value):
                bp.write_text(f"- **Slug:** s\n{_MERGES} {value}\n"
                              f"- **Conflicts with:** {value}\n", encoding="utf-8")
                self.assertEqual(parse(bp), want)
                self.assertEqual(parse(bp), brief.conflicts_with(bp))
        bp.write_text("- **Slug:** s\n", encoding="utf-8")
        self.assertEqual(parse(bp), [])                          # absent ⇒ no group


class MergeModeStrict(_Batches):
    """(a), (b): `compute_waves` refuses a run with a group member in merge mode."""

    def test_a_declares_the_group(self) -> None:
        cfg = self._root("merge")
        exc = self._refusal(cfg, self._write(cfg, BATCH_A))
        self.assertIn("issue_A", str(exc))
        self.assertIn("issue_B", str(exc))
        self._assert_ways_out(exc.remedy)

    def test_only_the_partner_declares_the_group(self) -> None:
        cfg = self._root("merge")
        exc = self._refusal(cfg, self._write(cfg, BATCH_B))
        self.assertIn("issue_A", str(exc))
        self.assertIn("issue_B", str(exc))
        self._assert_ways_out(exc.remedy)

    def test_a_run_of_one_member_whose_partner_is_not_in_the_run(self) -> None:
        cfg = self._root("merge")
        exc = self._refusal(cfg, self._write(cfg, BATCH_B_OUT))
        self.assertIn("issue_A", str(exc))
        self.assertIn("issue_X", str(exc))
        self._assert_ways_out(exc.remedy)

    def test_every_id_spelling_names_the_partner(self) -> None:
        # The run is A, B and an unrelated D, so only a parsed id can put B in the group.
        for value in ("B", "#B", "issue_B", "B (the caller has to land first)"):
            with self.subTest(value=value):
                cfg = self._root("merge")
                bundles = self._write(cfg, {"A": [f"{_MERGES} {value}"], "B": [], "D": []})
                exc = self._refusal(cfg, bundles)
                self.assertIn("issue_B", str(exc))
                self.assertNotIn("issue_D", str(exc))


class MergeModeTolerant(_Batches):
    """(d): `partition_schedulable` holds the group and its dependents, never raises."""

    def test_holds_the_group_and_its_dependent_and_keeps_the_rest(self) -> None:
        cfg = self._root("merge")
        bundles = self._write(cfg, BATCH_D)
        sched, held = self._partition(cfg, bundles)
        self.assertEqual(sched, ["issue_D"])
        self.assertEqual(set(held), {"issue_A", "issue_B", "issue_C"})
        for name in ("issue_A", "issue_B"):
            self.assertIn("Merges with", held[name])
            self._assert_ways_out(held[name])
        self.assertIn("issue_A", held["issue_C"])               # held through its prerequisite
        # The remainder is still a batch the strict path levels without raising.
        self.assertEqual(self._waves(cfg, [cfg.bundle("D")]), [["issue_D"]])


class StackModeUnchanged(_Batches):
    """(e): in stack mode the field changes nothing — same waves, same partition, as the same
    batch without the `Merges with` lines; nothing refused, nothing held."""

    def test_same_result_as_without_the_field(self) -> None:
        for label, batch in (("a", BATCH_A), ("b", BATCH_B), ("b, out of run", BATCH_B_OUT),
                             ("d", BATCH_D)):
            with self.subTest(batch=label):
                with_field = self._root("stack")
                without = self._root("stack")
                got = self._write(with_field, batch)
                ref = self._write(without, batch, drop_merges=True)
                self.assertEqual(self._waves(with_field, got), self._waves(without, ref))
                self.assertEqual(self._partition(with_field, got), self._partition(without, ref))
                self.assertEqual(self._partition(with_field, got)[1], {})


class FlowRefusesAGroup(unittest.TestCase):
    """(c): `pdca flow A B` on batch (a) in merge mode exits 2 before building anything.

    The setup of `tests/test_flow_dependency_refusal.py`: all leaves stubbed, gates empty,
    `driver.run_issue` wrapped to count builds, `cli._flow` with `--no-publish --no-act`."""

    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.cfg = Config(
            root=tmp,
            bundle_root=tmp / "results",
            process_dir=tmp / "process",
            templates_dir=tmp / "templates",
            default_branch="main",
            tracker_system="github",
            tracker_url="",
            issue_id_example="#1",
            builder=LeafConfig(mode="stub", family="claude"),
            reviewer=LeafConfig(mode="stub", family="codex"),
            planner=LeafConfig(mode="stub", family="claude", interactive=True),
            signoff=LeafConfig(mode="stub", family="claude", interactive=True),
            publisher=LeafConfig(mode="stub", family="claude", interactive=True),
            act=LeafConfig(mode="stub", family="claude", interactive=True),
            act_cadence=1,
            repo_checkouts={"example-org/example-repo": str(tmp / "example-repo")},
            wave_mode="merge",
        )
        self.built: list[str] = []
        real = driver.run_issue
        self.addCleanup(setattr, driver, "run_issue", real)

        def counting(d: Path, cfg: Config) -> str:
            self.built.append(d.name)
            return real(d, cfg)
        driver.run_issue = counting

    def _bundle(self, iid: str, *extra: str) -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text(
            f"# Brief — slug-{iid.lower()}\n\n"
            f"- **Slug:** slug-{iid.lower()}\n"
            f"- **Defect:** stub defect for {iid}.\n"
            "- **Success criterion:** the stub test passes.\n"
            "- **Repo + branch target:** example-repo @ main\n"
            "- **Test file:** test_stub.py\n"
            + "".join(line + "\n" for line in extra), encoding="utf-8")
        return d

    def test_flow_refuses_with_rc2_and_builds_nothing(self) -> None:
        self._bundle("A", f"{_MERGES} B")
        self._bundle("B")
        args = SimpleNamespace(issue_ids=["A", "B"], from_csv=None, from_briefs=None,
                               no_publish=True, no_act=True, by="", lanes=None,
                               max_passes=None)
        err, out = io.StringIO(), io.StringIO()
        try:
            with redirect_stderr(err), redirect_stdout(out):
                rc = cli._flow(self.cfg, args)
        except ValueError as exc:
            self.fail(f"`pdca flow A B` raised instead of refusing: {type(exc).__name__}: {exc}")
        text = err.getvalue()
        self.assertEqual(rc, 2, f"expected rc 2, got {rc}; stderr:\n{text}")
        self.assertNotIn("Traceback", text)
        self.assertIn("Ways out:", text)
        self.assertIn("issue_A", text)
        self.assertIn("issue_B", text)
        self.assertEqual(self.built, [], "Do ran on a bundle of a refused batch")
        for iid in ("A", "B"):
            d = self.cfg.bundle(iid)
            self.assertFalse((d / "patch.diff").exists(), f"{d.name} was built")
            self.assertEqual(state.state(d), state.PLANNED)


class WavesPreviewRefusesAGroup(unittest.TestCase):
    """(f): bare `pdca waves` through `cli.main`, merge mode, over a `results/` holding A
    (declares `Merges with: B`), B and an unrelated D: rc 1, `unschedulable:` naming A and B."""

    def test_bare_waves_reports_the_group_unschedulable(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "pdca.toml").write_text(
            '[paths]\nbundle_root = "results"\n[driver]\nwave_mode = "merge"\n',
            encoding="utf-8")
        cfg = _cfg(tmp, "merge")
        _Batches._write(cfg, {"A": [f"{_MERGES} B"], "B": [], "D": []})
        err, out = io.StringIO(), io.StringIO()
        cwd = Path.cwd()
        os.chdir(tmp)
        try:
            with mock.patch.dict(os.environ), redirect_stderr(err), redirect_stdout(out):
                os.environ.pop("PDCA_BUNDLE_ROOT", None)        # results/ under tmp, as written
                rc = cli.main(["waves"])
        finally:
            os.chdir(cwd)
        self.assertEqual(rc, 1, f"expected rc 1, got {rc}; stdout:\n{out.getvalue()}")
        text = err.getvalue()
        self.assertIn("unschedulable:", text)
        self.assertIn("issue_A", text)
        self.assertIn("issue_B", text)


if __name__ == "__main__":
    unittest.main()
