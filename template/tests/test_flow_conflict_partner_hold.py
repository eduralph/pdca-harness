"""Stack mode holds a `Conflicts with` partner of a bundle that pushed no branch (#629).

`waves.compute_waves` orders a `Conflicts with` pair (the name-lower id builds first), so the
second is built on a line meant to hold the first. When the first is accepted but its publish
pushes no branch this run, the fold leaves it out (`held_unpushed`, #593). Its partner must
then be held — named on its own stderr line, and told to wait until the first's PR merges
(a re-run sooner builds it on a base without that work) — exactly as a `Depends on`
dependent is held, while the rest of the run goes on.

Offline: bundles on disk, the build leaf replaced by a stub that accepts each bundle it is
given (a non-empty patch.diff, `accept` in §9), publishing stubbed, `gh` stubbed. The
publisher is NOT a stub (a stub publisher is a dry-run, which holds nothing). Driven through
``flow.flow_ids`` in stack mode, publishing on. The fold is the REAL one wherever no bundle
it is given pushed a branch: it then has nothing to merge and returns before any git step,
so no repository is needed. Only the control case, where a branch IS pushed, stubs it.

Only modules are imported (never a symbol the fix adds), so with the production change
reverted these cases still load and run — and fail on their assertions.
    cd template && PYTHONPATH=src python3 -m unittest tests.test_flow_conflict_partner_hold
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from pdca_harness import flow, leaves, publish, signoff, split, state
from pdca_harness.config import Config, LeafConfig

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
TODAY = "2026-10-14"
TARGET = "org/repo @ main"
#: What the held line tells the operator to do: publish the bundle, then WAIT for its PR to
#: merge into the partner's base before re-running. A re-run straight after publishing
#: would build the partner without that work: the carry starts only from a plain
#: `Depends on`, and a finished id is outside the next run's conflict map.
WAIT = "and wait until their PR merges into main (org/repo), then re-run."


def _patch(name: str) -> str:
    """A non-empty patch adding a file of ``name``'s own, so no two bundles share a path."""
    return (f"diff --git a/{name}.txt b/{name}.txt\nnew file mode 100644\n--- /dev/null\n"
            f"+++ b/{name}.txt\n@@ -0,0 +1 @@\n+p\n")


def _cfg(root: Path) -> Config:
    return Config(
        root=root, bundle_root=root / "results", process_dir=root / "process",
        templates_dir=TEMPLATES, default_branch="main", tracker_system="github",
        tracker_url="", issue_id_example="#1",
        builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
        planner=LeafConfig(mode="stub", interactive=True),
        signoff=LeafConfig(mode="stub", interactive=True),
        publisher=LeafConfig(mode="command", interactive=True),
        act=LeafConfig(mode="stub", interactive=True), gates_checks=[],
        # Inside the temp dir, and never git checkouts: nothing outside it is looked at.
        repo_checkouts={"org/repo": str(root / "repo")})


def _proposal(*bodies: str) -> str:
    """A `split-proposal.md` the production parser accepts (`split.parse`)."""
    out = "<!-- pdca:split-proposal v1 -->\n# Split proposal\n\n"
    for i, body in enumerate(bodies, 1):
        out += f"<!-- pdca:child child-{i} -->\n{body}<!-- pdca:end child-{i} -->\n\n"
    return out


class ConflictPartnerHold(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = _cfg(self.tmp)
        self.built: list[str] = []
        self.unpushed: set[str] = set()   # bundle dir names whose publish pushes nothing
        self.splits: dict[str, tuple[list[str], str]] = {}  # parent name → (ids, proposal)
        self._real_run = subprocess.run

    # -- fixture ---------------------------------------------------------------------------

    def _brief(self, iid: str, extra: str = "", target: str | None = TARGET) -> Path:
        """A PLANNED bundle; ``target`` None: its brief has no ``Repo + branch target``."""
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True, exist_ok=True)
        tgt = f"- **Repo + branch target:** {target}\n" if target is not None else ""
        (d / "brief.md").write_text(f"- **Slug:** s{iid}\n{tgt}{extra}", encoding="utf-8")
        return d

    def _fixture(self, *, declared_by_partner: bool = True) -> None:
        """The brief's batch. Waves: {701, 704}, then {702, 703}, then {705}. The conflict
        edge is declared on CJ's side, or (``declared_by_partner`` False) on CA's."""
        self._brief("701", "" if declared_by_partner else "- **Conflicts with:** 702\n")  # CA
        self._brief("702", "- **Conflicts with:** 701\n" if declared_by_partner else "")  # CJ
        self._brief("704", target=None)
        self._brief("703", "- **Depends on:** 704\n", target=None)  # U
        self._brief("705", "- **Depends on:** 702\n", target=None)

    # -- stubs -----------------------------------------------------------------------------

    def _gh_stub(self, args, *a, **kw):
        if not (isinstance(args, list) and args[:1] == ["gh"]):
            return self._real_run(args, *a, **kw)
        return subprocess.CompletedProcess(
            args, 0, json.dumps({"state": "OPEN", "headRefOid": "0" * 40}), "")

    def _drive_wave(self, cfg, wave: list[Path], **_kw) -> int:
        """The build leaf ran on each bundle and the human accepted it — or, for a parent in
        ``self.splits``, the slice was split instead, exactly as `pdca split --accept` does
        it (the PRODUCTION `split.accept`), and the human confirmed the split at sign-off;
        the run then adopts the children itself."""
        for d in wave:
            self.built.append(d.name)
            if d.name in self.splits:
                ids, proposal = self.splits[d.name]
                (d / split.PROPOSAL).write_text(proposal, encoding="utf-8")
                split.accept(d, ids, cfg)
            else:
                (d / "patch.diff").write_text(_patch(d.name), encoding="utf-8")
            (d / "check-gates.json").write_text("{}", encoding="utf-8")
            shutil.copyfile(TEMPLATES / "SUMMARY.md.tpl", d / "SUMMARY.md")
            signoff.record(d / "SUMMARY.md", action="accept", by="T", date=TODAY)
        return 0

    def _publish(self, cfg, d: Path, *a, **k) -> bool:
        """Push ``d``'s branch (a ``publish.json`` naming it, True) — unless it is one of
        ``self.unpushed``: its publish failed before the push (False, nothing recorded). A
        bundle with no target is skipped as the real publish skips it (no record, False)."""
        if d.name in self.unpushed or not all(publish._resolve_target(d)[:2]):
            return False
        (d / "publish.json").write_text(json.dumps({
            "mode": "new-pr", "branch": f"fix/{d.name}", "base": "main", "repo": "org/repo",
            "pr_url": f"https://github.com/org/repo/pull/{d.name}", "by": "T",
            "date": TODAY}), encoding="utf-8")
        return True

    def _run(self, ids: list[str], *, stub_fold: bool = False) -> None:
        err, out = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(subprocess, "run", self._gh_stub))
            stack.enter_context(mock.patch.object(flow, "_drive_wave", self._drive_wave))
            stack.enter_context(mock.patch.object(flow, "_publish_bundle", self._publish))
            if stub_fold:
                # A pushed branch would make the REAL fold look for a git checkout, and there
                # is none in the temp dir: the fold folds nothing here, and stops nothing.
                stack.enter_context(mock.patch.object(flow, "_fold_lines",
                                                      lambda *a, **k: ({}, {}, None)))
            stack.enter_context(mock.patch.object(publish, "draft_texts",
                                                  lambda *a, **k: True))
            stack.enter_context(mock.patch.object(leaves, "do_plan_batch",
                                                  lambda *a, **k: None))
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            self.results = flow.flow_ids(self.cfg, ids, do_publish=True, do_act=False,
                                         by="T", today=TODAY)
        self.err = err.getvalue()
        self.assertNotIn("Traceback", self.err)

    def _state(self, iid: str) -> str:
        return state.state(self.cfg.bundle(iid))

    def _held(self, held: str, by: str) -> list[str]:
        """The stderr lines naming ``held``, ``by``, the conflict edge and why."""
        return [ln for ln in self.err.splitlines()
                if held in ln and by in ln and "Conflicts with" in ln
                and "pushed no branch" in ln]

    def _assert_held(self, held: str, by: str) -> None:
        """ONE line holds ``held`` for ``by``: not as a prerequisite, and with advice that
        waits for ``by``'s merge rather than leading straight back to a re-run."""
        lines = self._held(held, by)
        self.assertEqual(len(lines), 1, self.err)
        self.assertIn(f"flow: {held} held", lines[0])
        self.assertNotIn("prerequisite", lines[0])
        self.assertNotRegex(self.err, rf"{held} skipped — prerequisite")
        self.assertIn(f"`pdca publish <id>` for {by.removeprefix('issue_')}", lines[0])
        self.assertTrue(lines[0].endswith(WAIT), lines[0])

    # -- cases -----------------------------------------------------------------------------

    def test_partner_of_an_unpushed_bundle_is_held_and_the_run_goes_on(self) -> None:
        self._fixture()
        self.unpushed = {"issue_701"}
        self._run(["701", "702", "703", "704", "705"])

        # CA was built and accepted in wave 0, but pushed nothing.
        self.assertIn("issue_701", self.built)
        self.assertEqual(self._state("701"), state.COMPLETE)
        # (a) CJ is never built and stays PLANNED.
        self.assertNotIn("issue_702", self.built, self.err)
        self.assertEqual(self._state("702"), state.PLANNED)
        # (b) ONE line names CJ, CA, the conflict edge and why — and says to wait.
        self._assert_held("issue_702", "issue_701")
        # (c) U, in the SAME later wave as CJ, is built and COMPLETE.
        self.assertIn("issue_703", self.built)
        self.assertEqual(self._state("703"), state.COMPLETE)
        # (d) 705 (`Depends on: 702`) cascades: not built.
        self.assertNotIn("issue_705", self.built)
        self.assertEqual(self._state("705"), state.PLANNED)
        # (e) The announcement names the conflict partners too.
        ann = [ln for ln in self.err.splitlines()
               if "issue_701 pushed no branch this run" in ln]
        self.assertEqual(len(ann), 1, self.err)
        self.assertIn("are its `Conflicts with` partners are held", ann[0])
        # The run did not stop: the real fold had nothing to merge after either wave.
        self.assertNotIn("STOPPING", self.err)

    def test_the_edge_declared_on_the_unpushed_side_holds_the_partner_too(self) -> None:
        """Either direction: CA's brief declares `Conflicts with: 702`, CJ's declares none."""
        self._fixture(declared_by_partner=False)
        self.unpushed = {"issue_701"}
        self._run(["701", "702", "703", "704", "705"])
        self.assertNotIn("issue_702", self.built, self.err)
        self.assertEqual(self._state("702"), state.PLANNED)
        self._assert_held("issue_702", "issue_701")
        self.assertIn("issue_703", self.built)
        self.assertNotIn("issue_705", self.built)

    def test_a_split_childs_partner_adopted_mid_run_is_held(self) -> None:
        """The conflict relation is read off the run's LIVE bundles: 706 splits in wave 0 into
        707 and 708 (`Conflicts with` its sibling 707), which the run adopts into waves 1
        and 2. 707 is accepted but pushes nothing, so 708 is held. A map built before wave 0
        (706 alone) has no edge between them, and 708 would build without 707's work."""
        self._brief("706", target=None)
        child = "# Brief — {slug}\n\n- **Slug:** {slug}\n- **Repo + branch target:** {tgt}\n"
        self.splits = {"issue_706": (["707", "708"], _proposal(
            child.format(slug="child-one", tgt=TARGET),
            child.format(slug="child-two", tgt=TARGET) + "- **Conflicts with:** child-1\n"))}
        self.unpushed = {"issue_707"}
        self._run(["706"])

        self.assertIn("issue_707", self.built, self.err)       # adopted, built, accepted
        self.assertEqual(self._state("707"), state.COMPLETE)
        self.assertIn("issue_707 pushed no branch this run", self.err)
        self.assertNotIn("issue_708", self.built, self.err)
        self.assertEqual(self._state("708"), state.PLANNED)
        self._assert_held("issue_708", "issue_707")

    def test_partner_builds_when_the_first_pushed_its_branch(self) -> None:
        """Unchanged: CA's publish pushes its branch, so CJ is built in wave 1 as today."""
        self._fixture()
        self.unpushed = set()
        self._run(["701", "702", "703", "704", "705"], stub_fold=True)
        for iid in ("701", "702", "703", "704", "705"):
            self.assertIn(f"issue_{iid}", self.built, self.err)
            self.assertEqual(self._state(iid), state.COMPLETE, self.err)
        self.assertLess(self.built.index("issue_701"), self.built.index("issue_702"))
        self.assertNotIn("Conflicts with", self.err)
        self.assertNotIn("pushed no branch", self.err)


if __name__ == "__main__":
    unittest.main()
