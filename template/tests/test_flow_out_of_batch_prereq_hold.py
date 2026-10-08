"""Stack mode holds a dependent of a prerequisite no line of the run carries (#647).

A plain ``Depends on: P`` where ``P`` is NOT one of the ids the run was asked to drive is
carried into the dependent's base by nothing in the run: ``P`` is COMPLETE ("a draft PR was
opened"), not merged. Such a dependent must wait, loudly, until ``P``'s PR merges into the
dependent's own target base — that repo and branch, not an integration line, and not via an
``Onto branch`` commit. ``Depends on (merged)`` uses the same "merged" rule. A dependent
with no usable target of its own is not held, and its ``Depends on (merged)`` keeps the old
answer (any merge counts); a ``Depends on (merged)`` prerequisite that is not COMPLETE is
"not ready", as before — there is no PR to wait for.

Offline: bundles on disk with ``publish.json`` records, the build / sign-off leaves and
publishing stubbed, and ``gh pr view`` answered by a stub in front of ``subprocess.run``.
The publisher is NOT a stub (a stub publisher is a dry-run, which holds nothing), except in
the dry-run case. Both entry points are driven: ``flow.flow_ids`` (named ids without ``P``)
and ``flow.flow_batch`` (the sweep, which never includes a COMPLETE ``P``).

Only modules are imported (never a symbol the fix adds), so with the production change
reverted these cases still load and run — and fail on their assertions.
    cd template && PYTHONPATH=src python3 -m unittest tests.test_flow_out_of_batch_prereq_hold
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

from pdca_harness import flow, leaves, merged, publish, signoff, state
from pdca_harness.config import Config, LeafConfig

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
TODAY = "2026-10-08"
TARGET = "org/repo @ main"
PATCH = "diff --git a/p.txt b/p.txt\nnew file mode 100644\n--- /dev/null\n+++ b/p.txt\n" \
        "@@ -0,0 +1 @@\n+p\n"


def _cfg(root: Path, *, publisher: str = "command") -> Config:
    return Config(
        root=root, bundle_root=root / "results", process_dir=root / "process",
        templates_dir=TEMPLATES, default_branch="main", tracker_system="github",
        tracker_url="", issue_id_example="#1",
        builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
        planner=LeafConfig(mode="stub", interactive=True),
        signoff=LeafConfig(mode="stub", interactive=True),
        publisher=LeafConfig(mode=publisher, interactive=True),
        act=LeafConfig(mode="stub", interactive=True), gates_checks=[],
        # Inside the temp dir, and never git checkouts: nothing outside it is looked at.
        repo_checkouts={"org/repo": str(root / "repo"), "org/other": str(root / "other")})


class OutOfBatchPrereqHold(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = _cfg(self.tmp)
        # PR state by bundle id: "OPEN" / "MERGED" / …; "FAIL": `gh` exits 1;
        # self.gh_missing: `gh` is not installed (OSError).
        self.gh: dict[str, str] = {}
        self.gh_missing = False
        self.gh_calls: list[list[str]] = []
        self.built: list[str] = []
        self._real_run = subprocess.run

    # -- fixture ---------------------------------------------------------------------------

    def _brief(self, iid: str, extra: str = "", target: str | None = TARGET) -> Path:
        """A PLANNED bundle; ``target`` None: its brief has no ``Repo + branch target``."""
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True, exist_ok=True)
        tgt = f"- **Repo + branch target:** {target}\n" if target is not None else ""
        (d / "brief.md").write_text(f"- **Slug:** {iid.lower()}\n{tgt}{extra}",
                                    encoding="utf-8")
        return d

    def _prereq(self, iid: str = "P", *, pr: str = "OPEN", patch: str | None = PATCH,
                mode: str = "new-pr", base: str = "main", repo: str = "org/repo",
                target: str | None = TARGET, published: bool = True,
                action: str = "accept") -> Path:
        """``iid`` signed off (``action``) in an EARLIER run and, when ``published``,
        published: a PR in state ``pr``, recorded with ``mode`` / ``base`` / ``repo``.
        ``patch`` None: no patch.diff — a close disposition, whose marker stands in for it.
        ``target``: its own brief's ``Repo + branch target`` (None: none)."""
        d = self._brief(iid, target=target)
        if patch is not None:
            (d / "patch.diff").write_text(patch, encoding="utf-8")
        else:
            (d / state.CLOSE_MARKER).write_text("likely-close\n", encoding="utf-8")
        (d / "check-gates.json").write_text("{}", encoding="utf-8")
        shutil.copyfile(TEMPLATES / "SUMMARY.md.tpl", d / "SUMMARY.md")
        signoff.record(d / "SUMMARY.md", action=action, by="T", date="2026-10-01")
        if published:
            (d / "publish.json").write_text(json.dumps({
                "mode": mode, "branch": f"fix/{iid}", "base": base, "repo": repo,
                "pr_url": f"https://github.com/{repo}/pull/{iid}", "by": "T",
                "date": "2026-10-01"}), encoding="utf-8")
        self.assertEqual(state.state(d), state.COMPLETE if action == "accept"
                         else state.DISCONTINUED)
        self.gh[iid] = pr
        return d

    # -- stubs -----------------------------------------------------------------------------

    def _gh_stub(self, args, *a, **kw):
        if not (isinstance(args, list) and args[:1] == ["gh"]):
            return self._real_run(args, *a, **kw)
        self.gh_calls.append(list(args))
        if self.gh_missing:
            raise FileNotFoundError(2, "No such file or directory", "gh")
        iid = str(args[3]).rsplit("/", 1)[-1]
        st = self.gh.get(iid, "OPEN")
        if st == "FAIL":
            return subprocess.CompletedProcess(args, 1, "", "gh: HTTP 502")
        return subprocess.CompletedProcess(
            args, 0, json.dumps({"state": st, "headRefOid": "0" * 40}), "")

    def _drive_wave(self, cfg, wave: list[Path], **_kw) -> int:
        self.built.extend(d.name for d in wave)   # the build leaf ran on it
        return 1

    def _run(self, entry: str, ids: list[str] | None = None, *,
             do_publish: bool = True) -> None:
        err, out = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(subprocess, "run", self._gh_stub))
            stack.enter_context(mock.patch.object(flow, "_drive_wave", self._drive_wave))
            stack.enter_context(mock.patch.object(flow, "_publish_bundle",
                                                  lambda *a, **k: True))
            stack.enter_context(mock.patch.object(publish, "draft_texts",
                                                  lambda *a, **k: True))
            stack.enter_context(mock.patch.object(leaves, "do_plan_batch",
                                                  lambda *a, **k: None))
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            if entry == "ids":
                self.results = flow.flow_ids(self.cfg, ids or [], do_publish=do_publish,
                                             do_act=False, by="T", today=TODAY)
            else:
                self.results = flow.flow_batch(self.cfg, do_publish=do_publish,
                                               do_act=False, by="T", today=TODAY)
        self.err = err.getvalue()
        self.assertNotIn("Traceback", self.err)

    def _runnable(self, d: Path) -> list[Path]:
        """``flow._runnable`` on ``d`` alone, as a run that drives only ``d`` calls it."""
        err = io.StringIO()
        with mock.patch.object(subprocess, "run", self._gh_stub), redirect_stderr(err):
            runnable = flow._runnable(self.cfg, [d], {d.name})
        self.err = err.getvalue()
        return runnable

    # -- assertions ------------------------------------------------------------------------

    def _held(self, dependent: str = "D", prereq: str = "P", *,
              carry_advice: bool = True) -> str:
        """``dependent`` was not built and stays PLANNED; ONE stderr line names it, the
        prerequisite and the reason (not merged into ``main``) with what to do — naming
        the prerequisite in the run only where that puts it on the dependent's line (a
        plain ``Depends on`` of the same target). ``U`` built."""
        self.assertNotIn(f"issue_{dependent}", self.built, self.err)
        self.assertEqual(self.results[dependent], state.PLANNED)
        lines = [x for x in self.err.splitlines()
                 if f"issue_{dependent}" in x and f"issue_{prereq}" in x]
        self.assertEqual(len(lines), 1, self.err)
        self.assertIn("not merged into main", lines[0])
        self.assertIn("until their PR merges into main", lines[0])
        if carry_advice:
            self.assertIn(f"name issue_{prereq} in the same `pdca flow <ids>` command",
                          lines[0])
        else:
            self.assertNotIn("pdca flow", lines[0])
        self.assertIn("issue_U", self.built, self.err)
        return lines[0]

    def _not_ready(self, dependent: str = "D", prereq: str = "P") -> None:
        """The pre-#647 reason, unchanged: "prerequisite(s) not ready" — never "not merged
        into …" / "wait until …", which would send the operator after a merge."""
        self.assertIn(f"flow: issue_{dependent} skipped — prerequisite(s) not ready "
                      f"({prereq});", self.err)
        self.assertNotIn("not merged into", self.err)
        self.assertNotIn("wait until", self.err)

    def _built(self, *iids: str) -> None:
        for iid in iids:
            self.assertIn(f"issue_{iid}", self.built, self.err)

    def _du(self, field: str = "Depends on", target: str | None = TARGET) -> None:
        self._brief("D", f"- **{field}:** P\n", target=target)
        self._brief("U")

    # -- (1) open PR, prerequisite outside the requested batch ⇒ held -----------------------

    def test_flow_ids_holds_a_dependent_of_an_unrequested_open_prereq(self):
        self._prereq(pr="OPEN")
        self._du()
        self._run("ids", ["D", "U"])
        self._held()

    def test_flow_batch_sweep_holds_a_dependent_of_a_finished_open_prereq(self):
        self._prereq(pr="OPEN")
        self._du()
        self._run("batch")
        self._held()

    def test_a_prereq_for_another_target_is_held_without_the_carry_advice(self):
        # Named in the run, P would go onto ITS target's line, not D's: no such advice.
        self._prereq(pr="OPEN", target="org/other @ main", repo="org/other")
        self._du()
        self._run("ids", ["D", "U"])
        self._held(carry_advice=False)

    # -- (2) merged into the dependent's base ⇒ built --------------------------------------

    def test_prereq_merged_into_the_target_base_lets_the_dependent_build(self):
        self._prereq(pr="MERGED", mode="new-pr", base="main")
        self._du()
        self._run("ids", ["D", "U"])
        self._built("D", "U")

    def test_stacked_pr_record_merged_into_main_counts(self):
        self._prereq(pr="MERGED", mode="stacked-pr", base="main")
        self._du()
        self._run("batch")
        self._built("D", "U")

    # -- (3) merged somewhere else ⇒ held, for `Depends on` and `Depends on (merged)` ------

    def test_merged_into_an_integration_line_does_not_count(self):
        # #591's real record shape: a stacked-pr against the integration line.
        self._prereq(pr="MERGED", mode="stacked-pr", base="pdca-integration/main")
        self._du()
        self._run("ids", ["D", "U"])
        self._held()

    def test_merged_into_an_integration_line_does_not_count_in_the_sweep(self):
        self._prereq(pr="MERGED", mode="stacked-pr", base="pdca-integration/main")
        self._du()
        self._run("batch")
        self._held()

    def test_an_onto_branch_record_does_not_count_even_on_main(self):
        self._prereq(pr="MERGED", mode="stacked", base="main")
        self._du()
        self._run("ids", ["D", "U"])
        self._held()

    def test_a_record_for_another_repo_does_not_count(self):
        self._prereq(pr="MERGED", mode="new-pr", base="main", repo="org/other")
        self._du()
        self._run("ids", ["D", "U"])
        self._held()

    def test_depends_on_merged_waits_for_a_merge_into_its_own_base(self):
        # Naming P in the run does not carry a `Depends on (merged)` edge: wait only.
        self._prereq(pr="MERGED", mode="stacked-pr", base="pdca-integration/main")
        self._du("Depends on (merged)")
        self._run("ids", ["D", "U"])
        self._held(carry_advice=False)

    def test_depends_on_merged_on_an_open_pr_says_what_it_waits_for(self):
        self._prereq(pr="OPEN")
        self._du("Depends on (merged)")
        self._run("ids", ["D", "U"])
        self._held(carry_advice=False)

    def test_depends_on_merged_builds_once_merged_into_its_base(self):
        self._prereq(pr="MERGED", mode="new-pr", base="main")
        self._du("Depends on (merged)")
        self._run("ids", ["D", "U"])
        self._built("D", "U")

    # -- (4) nothing to wait for -----------------------------------------------------------

    def test_an_empty_patch_prereq_holds_nothing(self):
        self._prereq(pr="OPEN", patch="")
        self._du()
        self._run("ids", ["D", "U"])
        self._built("D", "U")

    def test_a_missing_patch_prereq_holds_nothing(self):
        self._prereq(pr="OPEN", patch=None)
        self._du()
        self._run("batch")
        self._built("D", "U")

    def test_a_prereq_with_no_target_holds_nothing(self):
        # No line ever carries it (the fold and the #646 carry skip it), and it opens no PR.
        self._prereq(target=None, published=False)
        self._du()
        self._run("ids", ["D", "U"])
        self._built("D", "U")
        self.assertEqual(self.gh_calls, [])

    # -- (5) gh missing or failing ⇒ held, no traceback ------------------------------------

    def test_gh_missing_holds_the_dependent_without_a_traceback(self):
        self._prereq(pr="MERGED")
        self._du()
        self.gh_missing = True
        self._run("ids", ["D", "U"])
        self._held()

    def test_gh_missing_holds_a_depends_on_merged_dependent_without_a_traceback(self):
        # `merged.is_merged` has no OSError guard: this used to raise out of the run.
        self._prereq(pr="MERGED")
        self._du("Depends on (merged)")
        self.gh_missing = True
        self._run("ids", ["D", "U"])
        self._held(carry_advice=False)

    def test_gh_failing_holds_the_dependent(self):
        self._prereq(pr="FAIL")
        self._du()
        self._run("batch")
        self._held()

    # -- (6) unchanged ---------------------------------------------------------------------

    def test_a_requested_prereq_is_left_to_the_carry(self):
        # P named in the run: child-1's pre-wave carry owns it (here: carried cleanly, so
        # it puts nothing in `held`). No merge check on P.
        self._prereq(pr="OPEN")
        self._du()
        with mock.patch.object(flow, "_carry_finished", lambda *a, **k: None):
            self._run("ids", ["P", "D", "U"])
        self._built("D", "U")
        self.assertEqual(self.gh_calls, [])

    def test_a_stacks_on_edge_is_not_held(self):
        self._prereq(pr="OPEN")
        self._du("Stacks on")
        self._run("ids", ["D", "U"])
        self._built("D", "U")

    def test_no_publish_holds_nothing(self):
        self._prereq(pr="OPEN")
        self._du()
        self._run("ids", ["D", "U"], do_publish=False)
        self._built("D", "U")
        self.assertEqual(self.gh_calls, [])

    def test_merge_mode_holds_nothing(self):
        self.cfg.wave_mode = "merge"
        self._prereq(pr="OPEN")
        self._du()
        self._run("ids", ["D", "U"])
        self._built("D", "U")
        self.assertEqual(self.gh_calls, [])

    def test_a_dry_run_asks_no_host_and_holds_nothing(self):
        self.cfg = _cfg(self.tmp, publisher="stub")
        self._prereq(pr="OPEN")
        self._du()
        self._run("batch")
        self._built("D", "U")
        self.assertEqual(self.gh_calls, [])

    def test_is_merged_still_answers_true_for_a_merged_onto_branch_record(self):
        # Merge mode's resume check and `status` keep today's answer.
        self._prereq(pr="MERGED", mode="stacked", base="pdca-integration/main")
        with mock.patch.object(subprocess, "run", self._gh_stub):
            self.assertTrue(merged.is_merged(self.cfg, "P"))

    # -- a dependent with no usable target of its own ⇒ no #647 hold ------------------------
    # Publish skips it (`skip_if_no_target`), so it has no base to wait for: no #647 hold,
    # and its `Depends on (merged)` keeps `merged.is_merged`'s answer and its "not ready"
    # line — never held forever, never "not merged into  ()".

    def test_a_dependent_with_no_target_field_is_not_held(self):
        self._prereq(pr="OPEN")
        self._du(target=None)
        self._run("ids", ["D", "U"])
        self._built("D", "U")
        self.assertNotIn("not merged into", self.err)
        self.assertEqual(self.gh_calls, [])

    def test_a_dependent_with_no_target_branch_is_not_held(self):
        self._prereq(pr="OPEN")
        self._du(target="org/repo")
        self._run("batch")
        self._built("D", "U")
        self.assertNotIn("not merged into", self.err)
        self.assertEqual(self.gh_calls, [])

    def test_a_targetless_depends_on_merged_dependent_keeps_the_is_merged_answer(self):
        # Merged into the integration line: `is_merged` says merged, so D builds as before.
        self._prereq(pr="MERGED", mode="stacked-pr", base="pdca-integration/main")
        self._du("Depends on (merged)", target=None)
        self._run("ids", ["D", "U"])
        self._built("D", "U")
        with mock.patch.object(subprocess, "run", self._gh_stub):
            self.assertTrue(merged.is_merged(self.cfg, "P"))

    def test_a_targetless_depends_on_merged_dependent_waits_with_the_old_line(self):
        self._prereq(pr="OPEN")
        self._du("Depends on (merged)", target="org/repo")
        self._run("ids", ["D", "U"])
        self.assertNotIn("issue_D", self.built, self.err)
        self.assertEqual(self.results["D"], state.PLANNED)
        self._not_ready()
        self._built("U")

    def test_gh_missing_for_a_targetless_depends_on_merged_dependent(self):
        self._prereq(pr="MERGED")
        self._du("Depends on (merged)", target=None)
        self.gh_missing = True
        self._run("ids", ["D", "U"])
        self.assertNotIn("issue_D", self.built, self.err)
        self._not_ready()
        self._built("U")

    # -- a `Depends on (merged)` prerequisite that is not COMPLETE ⇒ "not ready" -----------
    # `waves.check_dep_graph` refuses such a run up front, so `_runnable` meets one only
    # when its state changes after the waves were computed (an iterate or a discontinue
    # while earlier waves run). It is "not ready": there is no PR to wait for.

    def test_a_planned_depends_on_merged_prereq_is_not_ready(self):
        self._brief("P")
        d = self._brief("D", "- **Depends on (merged):** P\n")
        self.assertEqual(self._runnable(d), [])
        self._not_ready()
        self.assertEqual(self.gh_calls, [])

    def test_a_discontinued_depends_on_merged_prereq_is_not_ready(self):
        self._prereq(pr="CLOSED", action="discontinue")
        d = self._brief("D", "- **Depends on (merged):** P\n")
        self.assertEqual(self._runnable(d), [])
        self._not_ready()
        self.assertEqual(self.gh_calls, [])


if __name__ == "__main__":
    unittest.main()
