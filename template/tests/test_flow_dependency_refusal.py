"""`pdca flow <ids>` refuses an unschedulable dependency graph with rc 2, not a traceback (#589).

`waves.check_dep_graph` rejects, before any build, a brief whose `Depends on` names a bundle
that is neither in the batch nor COMPLETE, and a dependency cycle. That refusal is right; it
used to reach the operator as a raw `ValueError` traceback out of `cli._flow`, because only
`flow.PreflightError` was caught around `flow.flow_ids`. These tests drive `cli._flow` — the
function `pdca flow` dispatches to — with named ids and assert on behaviour only: the return
code, the stderr text, and that no bundle was built.

Modules are imported, never the new exception class (`from pdca_harness import cli, …`): on
the C4 red leg the production hunks are reverted, and a module-level import of a symbol the
fix adds would fail to load (PDCA-UNVERIFIABLE) instead of going red.

All leaves stubbed, gates empty, no tracker / network / `gh` / container — the fixture shape
of `tests/test_flow_adopt_recovery.py:47-66` and `:135-146`.

    cd template && PYTHONPATH=src python3 -m unittest tests.test_flow_dependency_refusal
"""

from __future__ import annotations

import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from pdca_harness import cli, driver, flow, state
from pdca_harness.config import Config, LeafConfig


def _stub_config(root: Path) -> Config:
    """All six leaves stubbed, gates empty — `tests/test_flow_adopt_recovery.py:47-66`."""
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
        planner=LeafConfig(mode="stub", family="claude", interactive=True),
        signoff=LeafConfig(mode="stub", family="claude", interactive=True),
        publisher=LeafConfig(mode="stub", family="claude", interactive=True),
        act=LeafConfig(mode="stub", family="claude", interactive=True),
        act_cadence=1,
        repo_checkouts={"example-org/example-repo": str(root / "example-repo")},
    )


def _brief(slug: str, *extra: str) -> str:
    """An authored brief (a filled Slug, so `state` reads PLANNED)."""
    return (f"# Brief — {slug}\n\n"
            f"- **Slug:** {slug}\n"
            f"- **Defect:** stub defect for {slug}.\n"
            "- **Success criterion:** the stub test passes.\n"
            "- **Repo + branch target:** example-repo @ main\n"
            "- **Test file:** test_stub.py\n"
            + "".join(line + "\n" for line in extra))


class FlowDependencyRefusal(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.cfg = _stub_config(tmp)
        self.err = io.StringIO()
        self.out = io.StringIO()
        # Count every build: Do must never run on any bundle of a refused batch
        # (the same probe as `tests/test_flow_slice.py:1068-1082`).
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
        (d / "brief.md").write_text(_brief(f"slug-{iid.lower()}", *extra), encoding="utf-8")
        return d

    def _cli(self, ids: list[str]) -> int:
        """`pdca flow <ids…>` through `cli._flow`, `--no-publish --no-act`."""
        args = SimpleNamespace(issue_ids=ids, from_csv=None, from_briefs=None,
                               no_publish=True, no_act=True, by="", lanes=None,
                               max_passes=None)
        with redirect_stderr(self.err), redirect_stdout(self.out):
            return cli._flow(self.cfg, args)

    def _assert_nothing_built(self, *ids: str) -> None:
        self.assertEqual(self.built, [], "Do ran on a bundle of a refused batch")
        for iid in ids:
            d = self.cfg.bundle(iid)
            self.assertFalse((d / "patch.diff").exists(), f"{d.name} was built")
            self.assertEqual(state.state(d), state.PLANNED)

    def _assert_refusal(self, rc: int) -> str:
        err = self.err.getvalue()
        self.assertEqual(rc, 2, f"expected rc 2, got {rc}; stderr:\n{err}")
        self.assertNotIn("Traceback", err)
        return err

    def _call(self, ids: list[str]) -> int:
        """`_cli`, but a raised exception is a FAILURE of this test (the pre-fix defect),
        reported with its message rather than as an error."""
        try:
            return self._cli(ids)
        except ValueError as exc:  # the pre-fix behaviour: a traceback to the operator
            self.fail(f"`pdca flow {' '.join(ids)}` raised instead of refusing: "
                      f"{type(exc).__name__}: {exc}")

    # -- unresolved dependency ----------------------------------------------------------

    def test_unresolved_dependency_single_id_refuses_rc2(self) -> None:
        self._bundle("A", "- **Depends on:** 773")
        rc = self._call(["A"])
        err = self._assert_refusal(rc)
        self.assertIn("issue_A: declared dependency '773' is neither in this batch nor an "
                      "existing COMPLETE bundle", err)
        # …plus the ways out: add it to the batch, drop the edge, or finish it first.
        self.assertIn("add 773", err)
        self.assertIn("drop the edge", err)
        self.assertIn("finish 773 first", err)
        self._assert_nothing_built("A")

    def test_unresolved_dependency_two_ids_refuses_rc2(self) -> None:
        self._bundle("A", "- **Depends on:** 773")
        self._bundle("B")
        rc = self._call(["A", "B"])
        err = self._assert_refusal(rc)
        self.assertIn("issue_A: declared dependency '773' is neither in this batch nor an "
                      "existing COMPLETE bundle", err)
        self._assert_nothing_built("A", "B")

    # -- dependency cycle ---------------------------------------------------------------

    def test_dependency_cycle_refuses_rc2(self) -> None:
        self._bundle("A", "- **Depends on:** B")
        self._bundle("B", "- **Depends on:** A")
        rc = self._call(["A", "B"])
        err = self._assert_refusal(rc)
        self.assertIn("dependency cycle: issue_A → issue_B → issue_A", err)
        self._assert_nothing_built("A", "B")

    # -- (iii): no over-broad catch -----------------------------------------------------

    def test_other_value_error_still_propagates(self) -> None:
        # Green on both legs by design: the new handling must catch ONLY the
        # unschedulable-graph refusal, never an arbitrary ValueError out of `flow_ids`.
        self._bundle("A")
        real = flow.flow_ids
        self.addCleanup(setattr, flow, "flow_ids", real)

        def boom(*_a: object, **_k: object) -> dict[str, str]:
            raise ValueError("something else")
        flow.flow_ids = boom
        with self.assertRaises(ValueError) as cm:
            self._cli(["A"])
        self.assertEqual(str(cm.exception), "something else")


if __name__ == "__main__":
    unittest.main()
