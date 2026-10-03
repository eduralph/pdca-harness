"""The flow never drives a bundle that has no Plan artifact (#597).

Before #481, `split --accept` on a parent whose brief an iterate-to-Plan had archived to
`iteration-vN/brief.md` wrote the close marker and NO replacement brief. `state.state`
reads such a parent as past Do (BUILT), so every intake path put it in the drive set and
the first brief read — `_point_at_integration` → `publish._resolve_target` →
`brief.parse_fields` — raised `FileNotFoundError` out of the whole `pdca flow` run.

The pre-#481 disk is built with PRODUCTION code, never by hand: `split.accept` runs on a
parent whose brief `driver._archive_iteration` archived, the brief it writes is read (the
expected bytes) and then deleted — exactly what a pre-#481 accept left. Everything is the
ordinary offline driver suite: six stub leaves, empty gates, no tracker / network / `gh`.
The fixture shape mirrors `tests/test_flow_adopt_recovery.py:47-66` and `:135-146`, copied,
never imported.

Modules are imported, never new symbols, so with the fix reverted this file still imports
and each test fails on its own assertion (or on the crash) — red, not unverifiable.

    cd template && PYTHONPATH=src python3 -m unittest tests.test_flow_briefless_split_parent
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from pdca_harness import cli, drive_claim, driver, flow, split, state
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


def _brief(slug: str) -> str:
    """An authored brief: Slug, Success criterion and Repo + branch target all filled, so
    `handoff.check_planner` (what `split._parent_plan` holds an archive to) accepts it."""
    return (f"# Brief — {slug}\n\n"
            f"- **Slug:** {slug}\n"
            f"- **Defect:** stub defect for {slug}.\n"
            "- **Success criterion:** the stub test passes.\n"
            "- **Repo + branch target:** example-repo @ main\n"
            "- **Test file:** test_stub.py\n"
            "- **External dependencies:** none\n")


def _proposal(n: int) -> str:
    """A `split-proposal.md` the production parser accepts (`split.parse`)."""
    out = "<!-- pdca:split-proposal v1 -->\n# Split proposal\n\n"
    for i in range(1, n + 1):
        out += (f"<!-- pdca:child child-{i} -->\n{_brief(f'child-{i}')}\n"
                f"<!-- pdca:end child-{i} -->\n\n")
    return out


class BrieflessSplitParent(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = self._instance()
        self.err = io.StringIO()
        self.out = io.StringIO()
        self.maps: list[dict[str, str]] = []
        self._orig = (flow.flow_ids, flow.flow_batch, flow._drive_wave)
        real_ids, real_batch = self._orig[0], self._orig[1]

        # Pass-through spies: the PRODUCTION function runs and its exact return value is
        # handed back; the copy kept is the results map `cli._flow` reports from.
        def ids_spy(*a, **kw):
            got = real_ids(*a, **kw)
            self.maps.append(dict(got))
            return got

        def batch_spy(*a, **kw):
            got = real_batch(*a, **kw)
            self.maps.append(dict(got))
            return got

        flow.flow_ids = ids_spy
        flow.flow_batch = batch_spy

    def tearDown(self) -> None:
        flow.flow_ids, flow.flow_batch, flow._drive_wave = self._orig

    # -- instance + capture -------------------------------------------------------------

    def _instance(self) -> Config:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return _stub_config(tmp)

    def _cli(self, ids: list[str], *, csv: str | None = None) -> int:
        """`pdca flow <ids…>` (or `pdca flow --from-csv`) through `cli._flow` —
        `tests/test_flow_adopt_recovery.py:135-146`, `--no-publish` throughout."""
        args = SimpleNamespace(issue_ids=ids, from_csv=csv, from_briefs=None,
                               no_publish=True, no_act=True, by="", lanes=None,
                               max_passes=None)
        with redirect_stderr(self.err), redirect_stdout(self.out):
            return cli._flow(self.cfg, args)

    def _lines(self, name: str) -> list[str]:
        return [ln for ln in self.err.getvalue().splitlines() if name in ln]

    # -- disks, all written by production code -------------------------------------------

    def _post_481(self, iid: str = "654", kids: tuple[str, ...] = ("701", "702"),
                  *, archived: bool = True) -> Path:
        """A split parent exactly as `split --accept` leaves it today. With ``archived`` its
        brief was first archived by the driver's OWN iterate-to-Plan (the realistic parent),
        so accept rebuilt it (#481); without, the parent kept its own brief."""
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "brief.md").write_text(_brief(f"slice-{iid}"), encoding="utf-8")
        if archived:
            driver._archive_iteration(d, 1, include_brief=True)
            self.assertFalse((d / "brief.md").exists())
            self.assertTrue((d / "iteration-v1" / "brief.md").is_file())
        (d / split.PROPOSAL).write_text(_proposal(len(kids)), encoding="utf-8")
        split.accept(d, list(kids), self.cfg)
        self.assertTrue((d / "brief.md").is_file())
        return d

    def _pre_481(self, iid: str = "654", kids: tuple[str, ...] = ("701", "702"),
                 *, archived: bool = True) -> tuple[Path, bytes]:
        """The disk a pre-#481 accept left: today's accept, its brief deleted. Returns the
        parent and the bytes today's accept wrote — what the repair must reproduce."""
        d = self._post_481(iid, kids, archived=archived)
        expected = (d / "brief.md").read_bytes()
        (d / "brief.md").unlink()
        self.assertEqual(state.state(d), state.BUILT)   # past Do, non-terminal: drivable
        self.assertEqual((d / state.CLOSE_MARKER).read_text(encoding="utf-8").strip(),
                         "split")
        return d, expected

    def _briefed(self, iid: str) -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "brief.md").write_text(_brief(f"slice-{iid}"), encoding="utf-8")
        return d

    def _complete(self, d: Path) -> None:
        """Carry one bundle to COMPLETE with the production per-wave driver."""
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self._orig[2](self.cfg, [d], by="t", today="2026-10-02", max_passes=2)
        self.assertEqual(state.state(d), state.COMPLETE)

    def _assert_skipped(self, d: Path, why: str) -> None:
        self.assertFalse((d / "brief.md").exists(), "a brief was written from no source")
        self.assertFalse((d / "check-gates.json").exists(), "the briefless bundle was driven")
        named = [ln for ln in self._lines(d.name) if "NOT driven" in ln]
        self.assertEqual(len(named), 1, self.err.getvalue())
        self.assertIn(why, named[0])
        # Worded for a flow run: `split --accept`'s own refusal and retry are not pasted
        # in, and the line carries ONE remedy — the flow's.
        self.assertNotIn("refusing to split", named[0])
        self.assertNotIn("re-run", named[0])
        self.assertEqual(named[0].count(f"`pdca flow {d.name.removeprefix('issue_')}`"), 1,
                         named[0])

    def _assert_let_go(self, skipped: Path, *, driven: Path) -> None:
        """Probed from a SECOND claim scope while the run is still live: the bundle it skipped
        can be claimed (let go, #565), the one it drives cannot — so the first answer is a
        release, not a run whose claims are already gone."""
        other = drive_claim.Run(self.cfg)
        try:
            refusal = other.take(driven)
            self.assertIsNotNone(refusal, "the probe took a bundle the live run drives")
            self.assertTrue(refusal.held)
            self.assertIsNone(other.take(skipped),
                              "this run still holds a bundle it will not drive")
        finally:
            other.close()

    # -- (i) repaired at intake, then driven like the post-#481 control -----------------

    def test_a_pre_481_split_parent_is_repaired_then_driven_like_the_control(self) -> None:
        parent, expected = self._pre_481()
        rc = self._cli(["654"])
        self.assertEqual((parent / "brief.md").read_bytes(), expected)
        rebuilt = [ln for ln in self._lines("issue_654") if "rebuilt it from" in ln]
        self.assertEqual(len(rebuilt), 1, self.err.getvalue())
        self.assertIn("iteration-v1/brief.md", rebuilt[0])
        got = self.maps[-1]

        # The control: the same parent, written by today's accept, brief left in place.
        self.cfg, self.err, self.out = self._instance(), io.StringIO(), io.StringIO()
        control = self._post_481()
        control_bytes = (control / "brief.md").read_bytes()
        control_rc = self._cli(["654"])
        want = self.maps[-1]

        self.assertEqual(got["654"], want["654"])
        self.assertEqual(got, want)
        self.assertEqual(rc, control_rc)
        self.assertEqual((control / "brief.md").read_bytes(), control_bytes,
                         "a bundle with its own brief.md was rewritten")
        self.assertNotEqual(got["654"], state.BUILT, "the repaired parent was not driven")

    # -- (ii) no usable source: named, not driven, claim released, siblings driven -------

    def _skip_named(self, parent: Path, why: str) -> None:
        sibling = self._briefed("800")
        rc = self._cli(["654", "800"])
        self._assert_skipped(parent, why)
        self.assertIn("654", self.maps[-1])
        self.assertEqual(self.maps[-1]["654"], state.BUILT)
        self.assertNotEqual(state.state(sibling), state.PLANNED, "the sibling was not driven")
        self.assertNotEqual(rc, 0)

    def test_no_archive_is_skipped(self) -> None:
        parent, _ = self._pre_481(archived=False)
        self._skip_named(parent, "no iterate-to-Plan archive")

    def test_an_archive_split_refuses_is_skipped(self) -> None:
        parent, _ = self._pre_481()
        src = parent / "iteration-v1" / "brief.md"
        src.write_text(src.read_text(encoding="utf-8").replace(
            "example-repo @ main", "<owner/repo> @ <branch>"), encoding="utf-8")
        self._skip_named(parent, "is incomplete")

    def test_a_missing_lineage_record_is_skipped(self) -> None:
        parent, _ = self._pre_481()
        (parent / split.LINEAGE).unlink()
        self._skip_named(parent, split.LINEAGE)

    def test_a_lineage_record_naming_no_children_is_skipped(self) -> None:
        parent, _ = self._pre_481()
        path = parent / split.LINEAGE
        record = json.loads(path.read_text(encoding="utf-8"))
        record["children"] = []
        path.write_text(json.dumps(record), encoding="utf-8")
        self._skip_named(parent, split.LINEAGE)

    # -- (ii) the claim on a skipped bundle is let go, on every intake path ------------

    def test_a_named_id_it_skips_is_let_go(self) -> None:
        parent, _ = self._pre_481(archived=False)
        sibling = self._briefed("800")
        with drive_claim.run(self.cfg) as claims:
            for d in (parent, sibling):              # as `cli._flow` claims named ids
                self.assertIsNone(claims.take(d))
            with redirect_stderr(self.err), redirect_stdout(self.out):
                got = flow.flow_ids(self.cfg, ["654", "800"], do_publish=False,
                                    claims=claims)
            self.assertEqual(got["654"], state.BUILT)
            self._assert_let_go(parent, driven=sibling)

    def test_the_sweep_lets_go_of_a_bundle_it_skips(self) -> None:
        parent, _ = self._pre_481(archived=False)
        with drive_claim.run(self.cfg) as claims:
            with redirect_stderr(self.err), redirect_stdout(self.out):
                got = flow.flow_batch(self.cfg, csv="tracker.csv", do_publish=False,
                                      claims=claims)
            self.assertNotIn("654", got)
            self.assertIn("BATCH1", got)
            self._assert_let_go(parent, driven=self.cfg.bundle("BATCH1"))

    def test_adoption_lets_go_of_a_child_it_skips(self) -> None:
        self._grandparent()
        shutil.rmtree(self.cfg.bundle("654"))
        child, _ = self._pre_481(archived=False)
        with drive_claim.run(self.cfg) as claims:
            # As `cli._flow` claims the named seed; adoption claims 654 and 656 itself.
            self.assertIsNone(claims.take(self.cfg.bundle("500")))
            with redirect_stderr(self.err), redirect_stdout(self.out):
                got = flow.flow_ids(self.cfg, ["500"], do_publish=False, claims=claims)
            self.assertNotIn("654", got)
            self.assertIn("656", got)
            self._assert_let_go(child, driven=self.cfg.bundle("656"))

    def test_a_briefless_bundle_past_do_that_is_not_a_split_parent_is_skipped(self) -> None:
        d = self.cfg.bundle("654")
        d.mkdir(parents=True)
        (d / "patch.diff").write_text("--- a/x\n+++ b/x\n", encoding="utf-8")
        self.assertEqual(state.state(d), state.BUILT)
        self._skip_named(d, "past Do")

    # -- (iii) the --from-csv sweep and split adoption ---------------------------------

    def test_the_sweep_repairs_then_drives_like_the_control(self) -> None:
        parent, expected = self._pre_481()
        rc = self._cli([], csv="tracker.csv")
        self.assertEqual((parent / "brief.md").read_bytes(), expected)
        got = self.maps[-1]

        # The control: the same sweep over the post-#481 disk, brief left in place.
        self.cfg, self.err, self.out = self._instance(), io.StringIO(), io.StringIO()
        self._post_481()
        control_rc = self._cli([], csv="tracker.csv")
        want = self.maps[-1]

        self.assertIn("654", got)
        self.assertEqual(got, want)
        self.assertEqual(rc, control_rc)
        self.assertNotEqual(got["654"], state.BUILT, "the repaired parent was not driven")

    def test_the_sweep_skips_a_parent_with_no_source_and_drives_the_rest(self) -> None:
        parent, _ = self._pre_481(archived=False)
        self._cli([], csv="tracker.csv")
        self._assert_skipped(parent, "no iterate-to-Plan archive")
        self.assertNotIn("654", self.maps[-1])
        # The stub Plan session briefs BATCH1/BATCH2; they are still driven.
        self.assertIn("BATCH1", self.maps[-1])

    def test_the_sweep_never_writes_a_bundle_another_run_holds(self) -> None:
        parent, _ = self._pre_481()
        other = drive_claim.Run(self.cfg)
        try:
            self.assertIsNone(other.take(parent))
            self._cli([], csv="tracker.csv")
        finally:
            other.close()
        self.assertFalse((parent / "brief.md").exists(),
                         "a bundle held by another live run was written")
        self.assertNotIn("654", self.maps[-1])

    def _grandparent(self) -> Path:
        """500, terminal on a split into 654 and 656 — the seed `pdca flow 500` adopts from."""
        g = self._briefed("500")
        (g / split.PROPOSAL).write_text(_proposal(2), encoding="utf-8")
        split.accept(g, ["654", "656"], self.cfg)
        self._complete(g)
        return g

    def test_an_adopted_child_is_repaired_then_driven(self) -> None:
        self._grandparent()
        child = self.cfg.bundle("654")
        shutil.rmtree(child)                       # re-made as a pre-#481 split parent
        child, expected = self._pre_481()
        self._cli(["500"])
        self.assertEqual((child / "brief.md").read_bytes(), expected)
        self.assertIn("654", self.maps[-1])
        self.assertNotEqual(self.maps[-1]["654"], state.BUILT)

    def test_an_adopted_child_with_no_source_is_skipped(self) -> None:
        self._grandparent()
        shutil.rmtree(self.cfg.bundle("654"))
        child, _ = self._pre_481(archived=False)
        self._cli(["500"])
        self._assert_skipped(child, "no iterate-to-Plan archive")
        self.assertNotIn("654", self.maps[-1])
        self.assertIn("656", self.maps[-1])   # its sibling is still adopted, driven
        self.assertNotEqual(state.state(self.cfg.bundle("656")), state.PLANNED)

    # -- (iv) nothing else changes -----------------------------------------------------

    def test_a_terminal_briefless_split_parent_is_never_written(self) -> None:
        parent = self._post_481()
        self._complete(parent)
        (parent / "brief.md").unlink()
        self.assertEqual(state.state(parent), state.COMPLETE)
        self._cli(["654"])
        self.assertFalse((parent / "brief.md").exists(),
                         "a brief was written into a terminal bundle")
        self.assertIn("701", self.maps[-1])    # still a seed: its children adopted

    def test_a_bundle_with_its_own_brief_is_left_byte_identical(self) -> None:
        d = self._briefed("800")
        before = (d / "brief.md").read_bytes()
        self._cli(["800"])
        self.assertEqual((d / "brief.md").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
