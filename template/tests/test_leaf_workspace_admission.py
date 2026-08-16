"""Interactive-leaf workspace admission (issue #494, stdlib unittest, no deps).

Every leaf the driver spawns runs with cwd ``cfg.root`` (a claude-family CLI walks up
from cwd to find ``.claude/agents`` + its hooks — ``leaves._do_build_command``), so
anything it is told to read outside that root has to be ADMITTED to its workspace by the
driver, via the family's grounding flag (``--add-dir`` for claude/codex,
``--include-directories`` for gemini — families.py:93/:112/:123). The headless half
already does that: the builder's worktree and bundle dir (``_do_build_command``), the
reviewer's resolved target (``_run_review_sandboxed``), both advisories. The six
INTERACTIVE spawns — ``do_plan``, ``do_plan_batch``, ``run_signoff``,
``run_signoff_batch``, ``run_act``, ``run_publish`` —
passed no ``extra_argv`` at all, while their prompts point straight at
the target checkout. So the human was asked to approve the same out-of-workspace read
every session, and could not make the approval stick: the grant would live in the
operator's untracked ``.claude/settings.local.json``, which a lane worktree never
materializes and ``--setting-sources project`` (families.py:100-102) drops by design.

This module asserts the whole contract over the argv the driver produces — which is the
whole of what the harness controls:

* (i)   each of the six admits the primary checkout the bundles of THAT session resolve
        to, deduped, existing-directories only;
* (ii)  with nothing to resolve, and ONLY for Plan (the beat that authors the first
        brief), it is the instance's known target set — ``[publisher.checkouts]`` ∪ what
        its existing briefs resolve to. A sign-off / Act / publish session already has a
        bundle to be about and NEVER widens to that set, even when its own bundle names
        a repo this host has no checkout of;
* (iii) never a lane worktree, even with one on disk that ``worktree.path()`` returns;
* (iv)  nothing wider — an existing sibling checkout named by neither the config nor any
        brief is never admitted, nor is the parent directory holding them;
* (v)   a family with no grounding mechanism (``generic``) is spawned byte-identically;
* (vi)  no other spawn property moves — cwd stays ``cfg.root``, the handoff env survives.

Run from template/:  PYTHONPATH=src python3 -m unittest tests.test_leaf_workspace_admission
"""

from __future__ import annotations

import contextlib
import io
import shutil
import subprocess as sp
import tempfile
import unittest
from pathlib import Path

from pdca_harness import handoff, leaves, worktree
from pdca_harness.config import Config, LeafConfig

# An interactive leaf on a family that HAS a grounding mechanism, and one that has none
# (an empty family resolves to the `generic` profile, whose grounding_flag is "" —
# families.py:44/:126). The second is criterion (v)'s subject.
_CLAUDE = LeafConfig(mode="command", family="claude", argv=["claude"], interactive=True)
_GENERIC = LeafConfig(mode="command", family="", argv=["true"], interactive=True)

_ALL_SIX = ("planner", "planner", "signoff", "signoff", "act", "publisher")


class LeafWorkspaceAdmission(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "instance"          # the pdca instance (cwd of every leaf)
        (self.root / "results").mkdir(parents=True)
        # Two real target checkouts, siblings of the instance root exactly as the
        # `<root>/../<repo>` convention resolves them (publish.py:579-587)...
        self.target = self._checkout("repo")
        self.other = self._checkout("other")
        # ...and a third that neither the config nor any brief in these fixtures names.
        # It exists, and it sits in the very directory the sibling convention searches —
        # criterion (iv)'s negative is only worth anything because it is reachable.
        self.unrelated = self._checkout("unrelated")

        self.captured: list[dict] = []
        self._orig_invoke = leaves._invoke

        def fake_invoke(leaf, workdir, prompt, **kw):
            self.captured.append({"workdir": workdir, "extra_argv": kw.get("extra_argv"),
                                  "env": kw.get("env")})

        leaves._invoke = fake_invoke

    def tearDown(self) -> None:
        leaves._invoke = self._orig_invoke
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- fixtures ---------------------------------------------------------------
    def _checkout(self, name: str) -> Path:
        p = self.tmp / name
        p.mkdir()
        sp.run(["git", "init", "-q", str(p)], check=True, capture_output=True)
        return p

    def _cfg(self, *, checkouts: dict | None = None, **leaf_cfg) -> Config:
        cfg = Config(
            root=self.root,
            bundle_root=self.root / "results",
            process_dir=self.root / "process",
            templates_dir=self.root / "templates",
            default_branch="main",
            tracker_system="github",
            tracker_url="",
            issue_id_example="1",
            builder=LeafConfig(mode="stub"),
            reviewer=LeafConfig(mode="stub"),
            base_remote="origin",
            **leaf_cfg,
        )
        # Default: nothing configured, so a brief's target resolves by the sibling
        # convention — the shape of the instance this defect was found in
        # ([publisher.checkouts] empty, `../pdca-harness` next door).
        cfg.repo_checkouts = dict(checkouts or {})
        return cfg

    def _bundle(self, cfg: Config, iid: str, target: str = "org/repo @ main") -> Path:
        """A PLANNED bundle whose brief names ``target``."""
        d = cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text(
            f"- **Slug:** slug-{iid}\n- **Repo + branch target:** {target}\n",
            encoding="utf-8")
        return d

    def _unplanned(self, cfg: Config, iid: str) -> Path:
        d = cfg.bundle(iid)
        d.mkdir(parents=True)
        return d

    def _frozen(self, cfg: Config, iid: str, target: str = "org/repo @ main") -> Path:
        """A COMPLETE (frozen) bundle — what Act reviews (act.py:87-94)."""
        d = self._bundle(cfg, iid, target)
        (d / "patch.diff").write_text("--- a/x\n+++ b/x\n", encoding="utf-8")
        (d / "check-gates.json").write_text("[]\n", encoding="utf-8")
        (d / "SUMMARY.md").write_text(
            "## 9. Check sign-off\n- Outcome: accepted\n", encoding="utf-8")
        return d

    # -- helpers ----------------------------------------------------------------
    @staticmethod
    @contextlib.contextmanager
    def _quiet():
        """Swallow whatever the driven production code prints — the suite's own report
        is the only thing this module's run should say (issue #402)."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            yield buf

    def _admitted(self, index: int = -1) -> list[str]:
        """The directories the ``index``-th spawn admitted (flag/value pairs stripped)."""
        argv = self.captured[index]["extra_argv"]
        self.assertIsNotNone(
            argv, "the spawn passed no extra_argv at all — nothing was admitted")
        self.assertEqual(argv[0::2], ["--add-dir"] * (len(argv) // 2),
                         f"every admitted dir must ride the family's grounding flag: {argv}")
        return argv[1::2]

    def _admits_nothing(self, index: int, why: str) -> None:
        """Assert the ``index``-th spawn admits no directory AND names no flag.

        Empty and absent are the same spawn: ``leaves._invoke`` folds ``extra_argv`` in
        with ``argv += list(extra_argv or [])``, so both produce a byte-identical command
        line. The contract is "no grant", not a representation.
        """
        self.assertFalse(self.captured[index]["extra_argv"], why)

    def _drive_all_six(self, cfg: Config) -> None:
        """Run each of the six interactive spawns once, in ``_ALL_SIX`` order."""
        cfg.act_cadence = 1
        self._frozen(cfg, "15")          # Act reviews frozen bundles, so give it one
        with self._quiet():
            leaves.do_plan(self._unplanned(cfg, "10"), cfg)
            leaves.do_plan_batch(cfg, ids=["11"])
            leaves.run_signoff(self._bundle(cfg, "12"), cfg)
            leaves.run_signoff_batch(cfg, [self._bundle(cfg, "13"),
                                           self._bundle(cfg, "14", "org/other @ main")])
            leaves.run_act(cfg, "2026-08-16")
            leaves.run_publish(self._bundle(cfg, "16"), cfg)
        self.assertEqual(len(self.captured), 6, "one spawn per interactive leaf")

    # -- (i) the bundles the session is about -----------------------------------
    def test_signoff_admits_the_bundles_own_checkout(self) -> None:
        cfg = self._cfg(signoff=_CLAUDE)
        self._bundle(cfg, "1", "org/other @ main")   # a NEIGHBOUR bundle, not this session's
        d = self._bundle(cfg, "2")
        with self._quiet():
            leaves.run_signoff(d, cfg)
        self.assertEqual(len(self.captured), 1)
        self.assertEqual(self._admitted(), [str(self.target)])
        self.assertEqual(self.captured[0]["workdir"], cfg.root)          # (vi) cwd unmoved

    def test_publish_admits_the_bundles_own_checkout(self) -> None:
        cfg = self._cfg(publisher=_CLAUDE)
        d = self._bundle(cfg, "3")
        with self._quiet():
            leaves.run_publish(d, cfg)
        self.assertEqual(self._admitted(), [str(self.target)])
        self.assertEqual(self.captured[-1]["workdir"], cfg.root)
        # (vi) the exit-contract env still rides along, unchanged by the grant.
        self.assertEqual((self.captured[-1]["env"] or {}).get(handoff.ENV_ROLE), "publisher")

    def test_signoff_batch_admits_each_targets_checkout_once(self) -> None:
        cfg = self._cfg(signoff=_CLAUDE)
        bundles = [self._bundle(cfg, "4"),
                   self._bundle(cfg, "5", "org/other @ main"),
                   self._bundle(cfg, "6")]                       # same target as issue_4
        with self._quiet():
            leaves.run_signoff_batch(cfg, bundles)
        self.assertEqual(len(self.captured), 1, "one seeded session over the whole batch")
        self.assertEqual(self._admitted(), [str(self.target), str(self.other)])

    def test_act_admits_the_reviewed_bundles_checkouts_once(self) -> None:
        cfg = self._cfg(act=_CLAUDE)
        cfg.act_cadence = 1
        self._frozen(cfg, "7")
        self._frozen(cfg, "8", "org/other @ main")
        self._frozen(cfg, "9")                                   # same target as issue_7
        with self._quiet():
            leaves.run_act(cfg, "2026-08-16")
        self.assertEqual(len(self.captured), 1)
        self.assertEqual(self._admitted(), [str(self.target), str(self.other)])

    # -- (ii) the pre-brief fallback: PLAN's, and only Plan's --------------------
    def test_plan_admits_the_instances_known_targets(self) -> None:
        # do_plan runs on an UNPLANNED bundle — there is no brief to resolve, which is
        # the whole point of the beat. The set is what the instance already names:
        # [publisher.checkouts] first, then what its existing briefs resolve to.
        cfg = self._cfg(planner=_CLAUDE, checkouts={"org/repo": str(self.target)})
        self._bundle(cfg, "20", "org/other @ main")
        with self._quiet():
            leaves.do_plan(self._unplanned(cfg, "21"), cfg)
        self.assertEqual(len(self.captured), 1)
        self.assertEqual(self._admitted(), [str(self.target), str(self.other)])
        self.assertEqual(self.captured[0]["workdir"], cfg.root)

    def test_plan_batch_admits_the_instances_known_targets(self) -> None:
        # `do_plan_batch`'s CSV/default path picks its ids MID-session, so not even a
        # bundle list exists to resolve from; an id-seeded batch is UNPLANNED for the
        # same reason. Both land on the same known-target set.
        cfg = self._cfg(planner=_CLAUDE)
        self._bundle(cfg, "22", "org/other @ main")
        self._unplanned(cfg, "23")
        with self._quiet():
            leaves.do_plan_batch(cfg, ids=["23"])
            leaves.do_plan_batch(cfg, csv="issues.csv")
        self.assertEqual(len(self.captured), 2)
        self.assertEqual(self._admitted(0), [str(self.other)])
        self.assertEqual(self._admitted(1), [str(self.other)])

    def test_an_archived_brief_still_names_a_known_target(self) -> None:
        # `results/completed/issue_*` is the archive convention (config.py:496-512): a
        # brief there is an existing brief, so its checkout is a known target too.
        cfg = self._cfg(planner=_CLAUDE)
        archived = cfg.bundle_root / "completed" / "issue_24"
        archived.mkdir(parents=True)
        (archived / "brief.md").write_text(
            "- **Slug:** s\n- **Repo + branch target:** org/other @ main\n", encoding="utf-8")
        with self._quiet():
            leaves.do_plan(self._unplanned(cfg, "25"), cfg)
        self.assertEqual(self._admitted(), [str(self.other)])

    def test_nothing_known_admits_nothing(self) -> None:
        # No config, no briefs, nothing on disk to point at: the set is empty rather
        # than guessed — no tracker-URL derivation, no parent, no "probably next door".
        cfg = self._cfg(planner=_CLAUDE)
        with self._quiet():
            leaves.do_plan(self._unplanned(cfg, "26"), cfg)
        self._admits_nothing(0, "an instance that names no target must grant none")

    def test_a_replan_over_an_existing_brief_stays_at_that_bundles_target(self) -> None:
        # The fallback is for an EMPTY resolved set (criterion ii). A re-plan (an
        # iterate-to-Plan) has a brief, so it resolves like any other session and must
        # not pick up the instance's other targets on top.
        cfg = self._cfg(planner=_CLAUDE, checkouts={"org/other": str(self.other)})
        with self._quiet():
            leaves.do_plan(self._bundle(cfg, "27"), cfg)
        self.assertEqual(self._admitted(), [str(self.target)],
                         "a briefed Plan session resolved its own target, so the "
                         "known-target fallback must not fire on top of it")

    def test_a_bundle_scoped_session_never_widens_to_the_known_targets(self) -> None:
        """Sign-off / batch / Act / publish have a bundle to be about — when its brief
        names a repo this host has no checkout of, the honest grant is NOTHING.

        This is the regression the previous attempt shipped: one fallback for all six
        spawns silently handed a sign-off session the instance's OTHER checkouts. The
        fixture is built so that mistake is VISIBLE — the instance's known-target set is
        non-empty here, and the first assertion proves it through the one leaf that may
        use it (Plan). A fixture without that would pass while admitting nothing for the
        wrong reason.
        """
        cfg = self._cfg(planner=_CLAUDE, signoff=_CLAUDE, publisher=_CLAUDE, act=_CLAUDE,
                        checkouts={"org/repo": str(self.target)})
        cfg.act_cadence = 1
        self._bundle(cfg, "40")                                  # resolvable: → self.target
        absent = self._frozen(cfg, "41", "org/absent @ main")     # named, but not on disk
        with self._quiet():
            leaves.do_plan(self._unplanned(cfg, "42"), cfg)
        self.assertEqual(self._admitted(0), [str(self.target)],
                         "fixture check: this instance HAS a known target, so a leaf "
                         "falling back would visibly admit it")
        with self._quiet():
            leaves.run_signoff(absent, cfg)
            leaves.run_signoff_batch(cfg, [absent])
            leaves.run_act(cfg, "2026-08-16")       # covered == the frozen bundle only
            leaves.run_publish(absent, cfg)
        self.assertEqual(len(self.captured), 5)
        for i, leaf in ((1, "run_signoff"), (2, "run_signoff_batch"),
                        (3, "run_act"), (4, "run_publish")):
            self._admits_nothing(
                i, f"{leaf} admitted {self.captured[i]['extra_argv']} for a bundle whose "
                   "target is not checked out here — a session that already has a bundle "
                   "must never widen to the instance's other targets")

    # -- (iii) never a lane worktree --------------------------------------------
    def test_no_spawn_admits_a_lane_worktree(self) -> None:
        cfg = self._cfg(signoff=_CLAUDE, publisher=_CLAUDE, planner=_CLAUDE, act=_CLAUDE)
        wt = self.tmp / ("repo" + worktree.WT_SUFFIX)
        sp.run(["git", "init", "-q", str(wt)], check=True, capture_output=True)
        # The trap has to be live, or the negative proves nothing: run SERIALLY,
        # `lane.current()` is None (lane.py:26-28), so `worktree._wt_dir` names the
        # unsuffixed `<name>.pdca-wt` and `worktree.path()` hands it back — this is
        # exactly what a `_reviewer_target`-based resolver would have admitted.
        probe = self._bundle(cfg, "30")
        self.assertEqual(worktree.path(probe, cfg), wt)
        self._drive_all_six(cfg)
        for i in range(len(self.captured)):
            for admitted in self._admitted(i):
                self.assertNotIn(worktree.WT_SUFFIX, admitted,
                                 f"spawn {i} admitted a harness-owned worktree: {admitted}")

    # -- (iv) nothing wider ------------------------------------------------------
    def test_no_spawn_admits_a_directory_nobody_named(self) -> None:
        cfg = self._cfg(signoff=_CLAUDE, publisher=_CLAUDE, planner=_CLAUDE, act=_CLAUDE,
                        checkouts={"org/repo": str(self.target)})
        self._drive_all_six(cfg)
        allowed = {str(self.target), str(self.other)}
        for i in range(len(self.captured)):
            admitted = self._admitted(i)
            self.assertTrue(admitted, f"spawn {i} admitted nothing at all")
            self.assertLessEqual(set(admitted), allowed,
                                 f"spawn {i} admitted more than the config and the briefs "
                                 f"name: {admitted}")
            # Named explicitly, because these two are the failure modes that matter: an
            # unrelated checkout next door, and the parent directory holding them all.
            self.assertNotIn(str(self.unrelated), admitted)
            self.assertNotIn(str(self.tmp), admitted)

    def test_a_target_that_is_not_on_disk_is_not_admitted(self) -> None:
        # Existence-restricted (criterion i): a brief may name a repo this machine has
        # no checkout of — admitting the path anyway would be a grant to nothing. Here
        # for Plan, the one leaf with a fallback: an unresolvable brief contributes
        # nothing to the known-target set either.
        cfg = self._cfg(planner=_CLAUDE)
        self._bundle(cfg, "31", "org/absent @ main")
        with self._quiet():
            leaves.do_plan(self._unplanned(cfg, "32"), cfg)
        self._admits_nothing(0, "a checkout that is not on this disk must not be granted")

    # -- (v) a family with no grounding mechanism --------------------------------
    def test_generic_family_spawns_are_byte_identical(self) -> None:
        cfg = self._cfg(signoff=_GENERIC, publisher=_GENERIC, planner=_GENERIC,
                        act=_GENERIC, checkouts={"org/repo": str(self.target)})
        self._drive_all_six(cfg)
        for i, spawn in enumerate(self.captured):
            self._admits_nothing(
                i, f"spawn {i} ({_ALL_SIX[i]}) faked a grant for a family that has no "
                   f"grounding flag — the argv must be what it was before #494")
            self.assertEqual(spawn["workdir"], cfg.root)
        # (vi) again, on the leaf that merges two env sources: the exit contract still
        # reaches a publisher whose family has no native STOP guard.
        self.assertEqual((self.captured[5]["env"] or {}).get(handoff.ENV_ROLE), "publisher")


if __name__ == "__main__":
    unittest.main()
