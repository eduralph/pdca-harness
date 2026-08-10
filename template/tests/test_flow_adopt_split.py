"""A split must not strand its own children on the run that caused it (#469).

Before this, the drive set froze at the start of the run: `_drive_and_act` computed
`wave_list` once and drove exactly the bundles it was handed, so when a bundle reached
`close-disposition = split` mid-run the parent went terminal, the children materialised by
`pdca split --accept` sat PLANNED and undriven, and the operator restarted the whole thing
by hand with `pdca flow <child-ids>`.

Every drive here goes **through `cli._flow`** — never a hand-picked `flow.*` call — because
that is the surface the operator (and their automation) actually uses, and the surface
where four of #449's five iterations found their parity breaks. Both CLI shapes are the
same machinery since #468 (`cli.py:604-622`), so a run started either way exercises the one
drive path this feature lives on.

Everything is the ordinary offline driver suite: all six leaves stubbed (the fixture
mirrors `tests/test_flow_slice.py:32-55`), gates empty, no tracker / network / `gh` /
container. The split itself is not simulated — the tests call the PRODUCTION `split.accept`
(`split.py:525`), so the parent's close marker, its `split-lineage.json` children record
and the child bundles are byte-for-byte what `pdca split --accept` leaves on disk.

Modules are imported, never new symbols (`from pdca_harness import cli, flow, …`): a
`from pdca_harness.flow import <new helper>` would raise ImportError on the C4 red leg,
which `engine/scripts/run-verify.sh` classifies PDCA-UNVERIFIABLE rather than red.

    cd template && PYTHONPATH=src python3 -m unittest tests.test_flow_adopt_split
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

from pdca_harness import cli, flow, leaves, split, state
from pdca_harness.config import Config, LeafConfig


def _stub_config(root: Path) -> Config:
    """All six leaves stubbed, gates empty (all-PASS stub rows) — the same fixture shape as
    `tests/test_flow_slice.py:32-55`, including the hermetic toy checkout inside the tmp
    root (the sibling convention would resolve to a SHARED /tmp/example-repo)."""
    return Config(
        root=root,
        bundle_root=root / "results",
        process_dir=root / "process",
        templates_dir=root / "templates",  # empty → planner stub uses its fallback brief
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
    """An authored brief (a filled Slug, so `state` reads PLANNED, not a placeholder)."""
    return (f"# Brief — {slug}\n\n"
            f"- **Slug:** {slug}\n"
            f"- **Defect:** stub defect for {slug}.\n"
            "- **Success criterion:** the stub test passes.\n"
            "- **Repo + branch target:** example-repo @ main\n"
            "- **Test file:** test_stub.py\n"
            + "".join(line + "\n" for line in extra))


def _proposal(*bodies: str) -> str:
    """A `split-proposal.md` the production parser accepts (`split.parse`)."""
    out = "<!-- pdca:split-proposal v1 -->\n# Split proposal\n\n"
    for i, body in enumerate(bodies, 1):
        out += f"<!-- pdca:child child-{i} -->\n{body}\n<!-- pdca:end child-{i} -->\n\n"
    return out


#: child-2 declares an ordering edge on its sibling LABEL — `split.accept` rewrites it to
#: the real id, which is what makes the adopted children land in two waves.
_CHILD_ONE = _brief("child-first")
_CHILD_TWO = _brief("child-second", "- **Depends on:** child-1")
#: …and the independent sibling: no ordering edge, so the two children land in ONE wave.
_SIBLING_TWO = _brief("child-second")


class AdoptSplitChildren(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = self._instance()
        self.err = io.StringIO()
        self.out = io.StringIO()
        self.waves_driven: list[list[str]] = []
        self.pointed: list[list[str]] = []
        self.results: dict[str, str] = {}
        self.passes = 0                  # one `_build_all` call == one pass of one wave
        self.after_wave = None           # optional per-test hook, fired when a wave returns
        # `cli._flow` reaches sign-off through `leaves.run_signoff_batch` only — the
        # per-bundle `leaves.run_signoff` belongs to the single-bundle library driver and
        # is not on the CLI path since #468 (`flow.py:380-394`).
        self._orig = (leaves.do_plan, leaves.run_signoff_batch, flow._drive_wave,
                      flow._build_all, flow._point_at_integration)
        self._orig_ids = flow.flow_ids
        self.addCleanup(self._restore_flow_ids)

    def _restore_flow_ids(self) -> None:
        flow.flow_ids = self._orig_ids

    def tearDown(self) -> None:
        (leaves.do_plan, leaves.run_signoff_batch, flow._drive_wave, flow._build_all,
         flow._point_at_integration) = self._orig

    # -- instance + capture -------------------------------------------------------------

    def _instance(self) -> Config:
        """A fresh, hermetic instance root, removed at teardown."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return _stub_config(tmp)

    def _reset(self) -> None:
        """A second leg of the same test, on a clean instance and unpatched leaves."""
        (leaves.do_plan, leaves.run_signoff_batch, flow._drive_wave, flow._build_all,
         flow._point_at_integration) = self._orig
        flow.flow_ids = self._orig_ids
        self.cfg = self._instance()
        self.err = io.StringIO()
        self.out = io.StringIO()
        self.waves_driven = []
        self.pointed = []
        self.results = {}
        self.passes = 0
        self.after_wave = None

    def _args(self, ids: list[str], *, max_passes: int | None = None,
              no_publish: bool = True) -> SimpleNamespace:
        """The `pdca flow <ids…>` argv as `cli._flow` receives it — arity is the CLI's own
        presentation switch (`cli.py:622`), so it has to be the thing under test rather
        than a hand-picked `flow.*` call.

        `no_publish` defaults to the suite's ordinary `--no-publish` (no git remotes), and
        is turned OFF by the one test that has to watch the wave BOUNDARY — publish + fold
        — which only exists when the run sequences its waves."""
        return SimpleNamespace(issue_ids=ids, from_csv=None, from_briefs=None,
                               no_publish=no_publish, no_act=True, by="", lanes=None,
                               max_passes=max_passes)

    def _cli(self, ids: list[str], *, max_passes: int | None = None,
             no_publish: bool = True) -> int:
        """Run `pdca flow <ids…>` through `cli._flow` and return its exit code."""
        with redirect_stderr(self.err), redirect_stdout(self.out):
            return cli._flow(self.cfg,
                             self._args(ids, max_passes=max_passes, no_publish=no_publish))

    def _state(self, issue_id: str) -> str:
        return state.state(self.cfg.bundle(issue_id))

    def _adoptions(self) -> list[str]:
        return [ln.split("flow: ")[-1] for ln in self.err.getvalue().splitlines()
                if "split → adopted children" in ln]

    def _record(self, iid: str, children: list[str]) -> None:
        """Hand-edit a parent's `split-lineage.json` children edge, as an operator can —
        the premise of every "the record is not to be trusted" guard below."""
        path = self.cfg.bundle(iid) / split.LINEAGE
        record = json.loads(path.read_text(encoding="utf-8"))
        record["children"] = children
        path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    # -- the split, exactly as `pdca split --accept` leaves it --------------------------

    def _split_now(self, parent: Path, ids: list[str], bodies: list[str],
                   cfg: Config | None = None) -> None:
        """Decompose `parent` into `ids` through the PRODUCTION `split.accept`: child
        bundles + each child's lineage record, the parent's merged `children` record, its
        build-notes breadcrumb and its `close-disposition = split` marker."""
        parent.mkdir(parents=True, exist_ok=True)
        if not (parent / "brief.md").exists():
            (parent / "brief.md").write_text(_brief("parent-slice"), encoding="utf-8")
        (parent / split.PROPOSAL).write_text(_proposal(*bodies), encoding="utf-8")
        split.accept(parent, ids, cfg or self.cfg)

    def _arm(self, splits: dict[str, list[str]], *,
             bodies: dict[str, list[str]] | None = None, iterate_once: str = "",
             walk_away: str = "", after_split=None) -> None:
        """Stub the leaves so each id in `splits` splits mid-flight, into its child ids.

        The path walked is the documented Entry B: the sign-off session records
        `iterate-plan`, the driver re-opens the bundle to UNPLANNED, and the next pass's
        serial Plan pre-pass concludes it is too large and splits it. `iterate_once` makes
        one bundle cost a second pass (an ordinary `iterate-do` on its first sign-off) —
        how a wave is made to want more budget than the run has left. `walk_away` is the
        session that is never answered for one bundle at all, which halts it at
        AWAITING_SIGNOFF — the ordinary end of an interactive run.

        `bodies` is per parent (a run where two bundles split needs two proposals, of
        possibly different arity); `after_split` is called with the parent's id once its
        split is on disk, which is where the hand-edited-record guards do their editing."""
        bodies = bodies or {}
        real_plan, real_signoff_batch, _wave, _build_all, _point = self._orig
        done: set[str] = set()

        def splitting_plan(d: Path, cfg: Config, csv: str | None = None) -> None:
            iid = d.name.removeprefix("issue_")
            if iid in splits and f"split:{iid}" not in done:
                done.add(f"split:{iid}")
                self._split_now(d, splits[iid],
                                bodies.get(iid, [_CHILD_ONE, _CHILD_TWO]), cfg=cfg)
                if after_split is not None:
                    after_split(iid)
                return
            real_plan(d, cfg, csv)

        def decide(d: Path) -> bool:
            """Write this bundle's scripted decision, if it has one this pass. True ⇒ the
            session answered here; False ⇒ let the real stub clear §6 and accept."""
            iid = d.name.removeprefix("issue_")
            if iid in splits and f"replan:{iid}" not in done:
                done.add(f"replan:{iid}")
                (d / leaves.SIGNOFF_DECISION).write_text(
                    "iterate-plan\nthis slice is too large — decompose it\n",
                    encoding="utf-8")
                return True
            if iid == iterate_once and "iterate" not in done:
                done.add("iterate")
                (d / leaves.SIGNOFF_DECISION).write_text(
                    "iterate-do\none more round\n", encoding="utf-8")
                return True
            if iid == walk_away:
                # No decision written and the real session never offered it: the bundle
                # halts at AWAITING_SIGNOFF, pass after pass, until the wave stops making
                # progress (`flow._drive_wave`) and names it.
                return True
            return False

        def signoff_batch(cfg: Config, bundles: list[Path]) -> None:
            real_signoff_batch(cfg, [d for d in bundles if not decide(d)])

        leaves.do_plan = splitting_plan
        leaves.run_signoff_batch = signoff_batch
        self._instrument()

    def _instrument(self) -> None:
        """The wave / pass / integration spies, WITHOUT arming a split — for the runs that
        must be shown to behave exactly as they do today."""
        _plan, _signoff, real_wave, real_build_all, real_point = self._orig

        def counting(cfg: Config, wave: list[Path]) -> None:
            self.passes += 1
            real_build_all(cfg, wave)

        def spy_wave(cfg: Config, wave: list[Path], **kw):
            # The production return value is handed straight back — post-fix it is the
            # wave's pass count, which the run's shared budget is kept in.
            self.waves_driven.append([d.name for d in wave])
            used = real_wave(cfg, wave, **kw)
            if self.after_wave is not None:
                self.after_wave()      # fault injection BETWEEN the wave and adoption
            return used

        def spy_point(integ, runnable: list[Path]) -> None:
            self.pointed.append([d.name for d in runnable])
            return real_point(integ, runnable)

        flow._build_all = counting
        flow._drive_wave = spy_wave
        flow._point_at_integration = spy_point

    def _capture_results(self) -> None:
        """Record the results map `cli._flow` is handed, without becoming a second one.

        A pass-through wrapper around the PRODUCTION `flow.flow_ids`: it calls the real
        function and hands its exact return value on to `cli._flow`, so what is asserted
        is the map the CLI derives its report and exit code from (#468) — the only place
        "excluded from the results map" is observable."""
        real = self._orig_ids

        def capture(cfg, ids, **kw):
            results = real(cfg, ids, **kw)
            self.results = dict(results)
            return results

        flow.flow_ids = capture

    def _build_fails(self, iid: str) -> None:
        """One bundle's Do leaf raises on every pass — the ordinary way a wave STALLS.

        `_advance_one` contains it (`flow.py:459`), so the bundle's state never changes and
        nobody is left awaiting sign-off: the wave takes `_drive_wave`'s no-progress exit
        instead of running its allowance out. A pass-through spy — every OTHER bundle in
        the wave is built by the production leaf, so the fault is one injected failure, not
        a fixture that stops building."""
        real = leaves.do_build

        def failing(d: Path, cfg: Config) -> None:
            if d.name == f"issue_{iid}":
                raise RuntimeError(f"builder leaf failed for {d.name}")
            real(d, cfg)

        leaves.do_build = failing
        self.addCleanup(setattr, leaves, "do_build", real)

    def _silence_signoff(self) -> None:
        """A sign-off session the human walked away from: no decision written at all, so
        the bundle stays AWAITING_SIGNOFF — halted, and never terminal."""
        leaves.run_signoff_batch = lambda cfg, bundles: None

    def _briefed(self, iid: str, *extra: str) -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "brief.md").write_text(_brief(f"slice-{iid}", *extra), encoding="utf-8")
        return d

    # -- the criterion: a split INSIDE the run is driven by that run ---------------------

    def test_cli_flow_drives_the_children_of_a_mid_run_split(self) -> None:
        """`pdca flow 500`: the re-plan splits 500, and THIS run drives 601 and 602 to a
        terminal state — in waves AFTER the parent's, honouring the `Depends on` the split
        itself wrote into 602's brief, announcing each child's REAL wave read back from the
        recomputed schedule (601 in wave 1, 602 in wave 2), and reporting them in the one
        results map both CLI shapes present."""
        self._briefed("500")
        self._arm({"500": ["601", "602"]})

        rc = self._cli(["500"])

        self.assertEqual(self._state("500"), state.COMPLETE)
        self.assertEqual(self._state("601"), state.COMPLETE)  # adopted, not stranded
        self.assertEqual(self._state("602"), state.COMPLETE)
        self.assertEqual(rc, 0)
        # AFTER the parent's wave, and 602 after 601 — its own declared ordering.
        self.assertEqual(self.waves_driven,
                         [["issue_500"], ["issue_601"], ["issue_602"]])
        self.assertEqual(self._adoptions(), [
            "issue_500 split → adopted children issue_601 into wave 1",
            "issue_500 split → adopted children issue_602 into wave 2"])
        printed = self.out.getvalue()
        self.assertIn(f"{state.COMPLETE}\t{self.cfg.bundle('500')}", printed)

    def test_adopted_children_go_through_the_same_integration_reconciliation(self) -> None:
        """An adopted wave is an ORDINARY wave: its bundles are reconciled with this run's
        integration state by the same `_point_at_integration` call every other wave goes
        through (`flow.py:1424`), not by a second mechanism bolted onto adoption."""
        self._briefed("500")
        self._arm({"500": ["601", "602"]})

        self._cli(["500"])

        self.assertEqual(self.pointed,
                         [["issue_500"], ["issue_601"], ["issue_602"]])

    def test_the_wave_a_split_happened_in_still_folds_for_its_adopted_wave(self) -> None:
        """Adoption GROWS the schedule, so "is this the last wave?" has to be read live.

        The wave boundary — publish, then fold the cumulative accepted work onto the
        run-scoped integration branch the NEXT wave builds on — is skipped on the final
        wave (`tests/test_flow_slice.py:1137`: the last wave folds nothing). Answer that
        from a `len(wave_list)` cached before the loop and a one-wave run that adopts folds
        NOTHING: wave 0's accepted patch never reaches the integration branch, and every
        adopted child is built and verified against a base that is missing its own parent's
        work — silently, because each bundle is green on its own.

        So this is the one test that runs with publishing ON (`--no-publish` off, stub
        publisher ⇒ `integrate.fold`'s dry-run, no git remotes), spying the production fold
        exactly as the peer wave test does (`tests/test_flow_slice.py:1122-1128`)."""
        self._briefed("500")
        self._arm({"500": ["601", "602"]})
        folds: list[list[str]] = []
        real_fold = flow.integrate.fold

        def spy_fold(cfg: Config, accepted: list[Path], *, dry_run: bool = False,
                     locks=None):
            folds.append([d.name for d in accepted])
            return real_fold(cfg, accepted, dry_run=dry_run, locks=locks)

        flow.integrate.fold = spy_fold
        self.addCleanup(setattr, flow.integrate, "fold", real_fold)

        rc = self._cli(["500"], no_publish=False)

        self.assertEqual(self._state("601"), state.COMPLETE)
        self.assertEqual(self._state("602"), state.COMPLETE)
        self.assertEqual(rc, 0)
        # Wave 0 folds before the adopted wave 1 builds, and wave 1 before wave 2; the
        # final wave still folds nothing. `accepted` is cumulative, so the second fold
        # carries the parent's work too — that is the base 602 is meant to build on.
        self.assertEqual(folds, [["issue_500"], ["issue_500", "issue_601"]])

    # -- one run-wide pass budget --------------------------------------------------------

    def test_the_pass_budget_is_one_cap_for_the_whole_run(self) -> None:
        """`--max-passes` bounds the RUN, not each wave it grows into — otherwise every
        adopted wave silently multiplies the operator's budget. The parent's wave spends 2
        passes (build → iterate-plan → re-plan → split → accept) and 601's spends 1, so a
        run that set out to drive ONE wave at a budget of 3 is exhausted before 602's wave:
        it is left PLANNED and NAMED, never driven on borrowed budget. One more pass and
        the same run finishes it."""
        self._briefed("500")
        self._arm({"500": ["601", "602"]})

        self._cli(["500"], max_passes=3)

        self.assertEqual(self._state("601"), state.COMPLETE)   # adoption did happen
        self.assertEqual(self._state("602"), state.PLANNED)    # …but on the run's budget
        self.assertEqual(self.waves_driven, [["issue_500"], ["issue_601"]])
        err = self.err.getvalue()
        self.assertIn("the run's pass budget is spent (3 pass(es) over 2 wave(s))", err)
        self.assertIn("issue_602 [PLANNED] — resume with `pdca flow 602`", err)
        self.assertEqual(self.passes, 3)                       # spent, not overspent

        # The cap is what stopped it — not adoption. One more pass, everything lands.
        self._reset()
        self._briefed("500")
        self._arm({"500": ["601", "602"]})

        self._cli(["500"], max_passes=4)

        self.assertEqual(self._state("602"), state.COMPLETE)
        self.assertEqual(self.passes, 4)
        self.assertNotIn("pass budget is spent", self.err.getvalue())

    def test_an_adopted_wave_only_gets_what_is_left_of_the_run_budget(self) -> None:
        """The cap is not merely re-checked between waves, it is HANDED DOWN. 601 costs two
        passes (it iterates once), and the parent's wave already spent 2 of 3 — so 601 gets
        the ONE pass that is left, stops there and is named. Handing each wave the full
        budget again would finish 601 on a 4th pass, i.e. spend more than the operator
        allowed the run."""
        self._briefed("500")
        self._arm({"500": ["601", "602"]}, iterate_once="601")

        self._cli(["500"], max_passes=3)

        self.assertEqual(self.passes, 3)                        # never a 4th pass
        self.assertEqual(self._state("601"), state.ITERATE_DO)  # left mid-iteration…
        err = self.err.getvalue()
        self.assertIn("pass budget exhausted after 1 pass(es)", err)   # …its share of 3
        self.assertIn("issue_601 [ITERATE_DO] — resume with `pdca flow 601`", err)

    def test_a_wave_that_runs_its_allowance_out_still_charges_the_run_pool(self) -> None:
        """The pool only caps a run if a wave that does NOT finish charges it too.

        `_drive_wave` returns the passes it consumed on EVERY exit, and the budget-exhausted
        one is exactly where "one pool, never multiplied" bites: the wave that ran its
        allowance out is the wave that spent the most. Here 810's session is walked away
        from, so wave 0 never goes all-terminal and burns the operator's whole 4 — while the
        two independent children 500 split off ARE a runnable wave. The run must decline to
        open it, not spend a 5th pass on it.

        Asserted because it is unobservable from the finishing waves: an exhausted
        `_drive_wave` that reported 0 leaves `spent` untouched, so this same run drives both
        children to COMPLETE on 6 passes — over budget, and silently."""
        self._briefed("500")
        self._briefed("810")
        self._arm({"500": ["601", "602"]}, bodies={"500": [_CHILD_ONE, _SIBLING_TWO]},
                  walk_away="810")

        rc = self._cli(["500", "810"], max_passes=4)

        self.assertEqual(self.passes, 4)      # the operator's 4 — never a 5th
        self.assertEqual(self.waves_driven, [["issue_500", "issue_810"]])  # wave 1 unopened
        self.assertEqual(self._adoptions(),   # the children WERE adopted…
                         ["issue_500 split → adopted children issue_601, issue_602 into "
                          "wave 1"])
        self.assertEqual(self._state("601"), state.PLANNED)   # …and then left, not driven
        self.assertEqual(self._state("602"), state.PLANNED)
        err = self.err.getvalue()
        self.assertIn("the run's pass budget is spent (4 pass(es) over 1 wave(s))", err)
        self.assertIn("issue_601 [PLANNED] — resume with `pdca flow 601`", err)
        self.assertEqual(rc, 1)               # un-terminal work, named, never rc 0

    def test_a_wave_that_stalls_charges_the_run_pool_for_what_it_spent(self) -> None:
        """The same accounting on `_drive_wave`'s OTHER un-finished exit — the wave that
        stops making progress rather than running its allowance out.

        820's Do leaf fails every pass, so once 500 has split, a whole pass changes nothing
        and the wave gives up after 3 of the run's 4. What is left is ONE pass, and that is
        what the adopted wave gets: 601 iterates once, so it is left ITERATE_DO and named.
        A stalled wave that reported 0 passes would hand the children a fresh allowance of
        4 and finish 601 on a fifth pass the operator never allowed."""
        self._briefed("500")
        self._briefed("820")
        self._arm({"500": ["601", "602"]}, bodies={"500": [_CHILD_ONE, _SIBLING_TWO]},
                  iterate_once="601")
        self._build_fails("820")

        self._cli(["500", "820"], max_passes=4)

        self.assertEqual(self.passes, 4)                      # 3 stalled + the 1 left
        self.assertEqual(self.waves_driven,
                         [["issue_500", "issue_820"], ["issue_601", "issue_602"]])
        err = self.err.getvalue()
        self.assertIn("a full pass made no progress", err)    # …the stall really happened
        self.assertIn("pass budget exhausted after 1 pass(es)", err)   # …its share of 4
        self.assertEqual(self._state("601"), state.ITERATE_DO)  # stopped mid-iteration
        self.assertEqual(self._state("602"), state.COMPLETE)    # its sibling did land

    def test_a_run_that_adopts_nothing_keeps_a_full_budget_per_wave(self) -> None:
        """The run-wide pool must not become a NEW way for an ordinary batch to be
        truncated. A four-deep `Depends on` chain is four waves of one pass each: at
        `--max-passes 1` every one of them completes, exactly as before adoption existed,
        because the pool is sized off the schedule the run set out to drive (1 × 4) — not
        one allowance shared by however many waves there turn out to be, which would
        strand 810's three dependents at a setting that never truncated anything."""
        ids = ["810", "811", "812", "813"]
        for i, iid in enumerate(ids):
            self._briefed(iid, *([f"- **Depends on:** {ids[i - 1]}"] if i else []))
        self._instrument()

        rc = self._cli(ids, max_passes=1)

        self.assertEqual(self.waves_driven, [[f"issue_{i}"] for i in ids])
        self.assertEqual([self._state(i) for i in ids], [state.COMPLETE] * 4)
        self.assertEqual(rc, 0)
        self.assertEqual(self.passes, 4)                       # one per wave, as always
        self.assertNotIn("budget is spent", self.err.getvalue())

    def test_an_adopted_child_that_splits_again_is_re_adopted_and_bounded(self) -> None:
        """Recursion, on the same pool. 601 is adopted, then splits in ITS wave; its own
        children are adopted into a later wave of the SAME run and driven — and they draw
        from the same budget, so the recursion is bounded rather than reset. Both halves
        are asserted, because either alone is satisfiable by the wrong implementation: a
        budget that reset would still finish the grandchildren, and a run that never
        re-examined an adopted child would still respect the cap."""
        self._briefed("500")
        self._arm({"500": ["601", "602"], "601": ["701", "702"]})

        self._cli(["500"], max_passes=20)

        self.assertEqual(self._state("701"), state.COMPLETE)
        self.assertEqual(self._state("702"), state.COMPLETE)
        self.assertIn("issue_601 split → adopted children issue_701 into wave 2",
                      self.err.getvalue())
        self.assertEqual(self.passes, 6)   # 2 (500) + 2 (601) + 1 + 1 — one pool, no reset

        # …and the pool BINDS across the recursion: the same run, allowed 5 passes, stops
        # inside the grandchildren rather than borrowing a fresh budget for the new wave.
        self._reset()
        self._briefed("500")
        self._arm({"500": ["601", "602"], "601": ["701", "702"]})

        self._cli(["500"], max_passes=5)

        self.assertEqual(self.passes, 5)
        self.assertEqual(self._state("702"), state.PLANNED)
        self.assertIn("the run's pass budget is spent (5 pass(es) over 3 wave(s))",
                      self.err.getvalue())

    # -- scope: the lineage edge, never a disk sweep -------------------------------------

    def test_adoption_follows_the_lineage_edge_not_a_disk_sweep(self) -> None:
        """An explicit-id flow adopts the children of the ids it was GIVEN — never an
        unrelated in-flight bundle. The distinction between `flow_ids` and the CSV resume
        sweep is deliberate and must survive adoption."""
        self._briefed("500")
        self._briefed("STRANGER")
        self._arm({"500": ["601", "602"]})

        self._cli(["500"])

        self.assertEqual(self._state("601"), state.COMPLETE)  # the lineage edge WAS followed
        self.assertEqual(self._state("STRANGER"), state.PLANNED)   # the disk was NOT swept
        self.assertNotIn("STRANGER", self.out.getvalue())

    def test_a_named_id_in_the_re_scheduled_tail_is_held_not_lost(self) -> None:
        """What the splice's tolerance does to the operator's OWN un-driven ids — asserted,
        because it is the one place the id list stops being levelled strictly.

        The splice re-levels `remaining + children`, and `remaining` is the tail of the id
        list the operator typed. So a named id whose prerequisite this run left un-terminal
        is HELD and reported in the resume shape, where a run that never spliced would have
        reached it and let `_runnable` skip it ("prerequisite(s) not ready"). Same end state
        (PLANNED, and the run fails), different line. What must NOT change is the answer the
        operator gets: an id they named stays in the results map even when it is held —
        unlike an adopted child, which is excluded because it is work the run did not do."""
        self._briefed("500")
        self._briefed("810")
        self._briefed("811", "- **Depends on:** 810")
        self._arm({"500": ["601", "602"]}, walk_away="810")
        self._capture_results()

        rc = self._cli(["500", "810", "811"], max_passes=3)

        self.assertEqual(self._state("601"), state.COMPLETE)   # the run carried on…
        self.assertEqual(self._state("602"), state.COMPLETE)
        self.assertEqual(self._state("810"), state.AWAITING_SIGNOFF)   # …the prereq halted
        self.assertEqual(self._state("811"), state.PLANNED)    # …its dependent was held
        self.assertIn("issue_811 held this run — unresolved dependency (810); left "
                      "in-flight", self.err.getvalue())
        self.assertEqual(self.results.get("811"), state.PLANNED)   # still answered for
        self.assertEqual(rc, 1)

    # -- guards --------------------------------------------------------------------------

    def test_a_split_marked_but_non_terminal_parent_is_not_adopted_from(self) -> None:
        """The marker is not the predicate — TERMINAL + the marker is. `split.accept`
        writes `close-disposition = split`, but the human still confirms the decomposition
        at sign-off, so a parent still AWAITING_SIGNOFF is a split nobody has accepted yet
        and driving its children would spend whole cycles on work the next sign-off may
        reopen.

        Both legs run the SAME on-disk split; the only difference is whether the sign-off
        session answered. (AWAITING_SIGNOFF rather than an `iterate-do` because an iterate
        ARCHIVES the close marker — a bundle that iterated is not "split-marked and
        non-terminal" at all, so it could not exercise the guard.)"""
        # Leg A — the human walked away: halted at AWAITING_SIGNOFF, marker still there.
        self._split_now(self.cfg.bundle("500"), ["601", "602"],
                        [_CHILD_ONE, _CHILD_TWO])
        self._silence_signoff()
        self._instrument()

        self._cli(["500"], max_passes=1)

        self.assertEqual(self._state("500"), state.AWAITING_SIGNOFF)   # NOT terminal
        self.assertEqual((self.cfg.bundle("500") / state.CLOSE_MARKER).read_text(
            encoding="utf-8").strip(), "split")                        # …but split-marked
        self.assertEqual(self._state("601"), state.PLANNED)            # not adopted
        self.assertEqual(self._state("602"), state.PLANNED)
        self.assertEqual(self._adoptions(), [])

        # Leg B — same bytes, the session accepts: NOW the parent is terminal on the split
        # and the very same children are adopted. The guard is the terminality, nothing else.
        self._reset()
        self._split_now(self.cfg.bundle("500"), ["601", "602"],
                        [_CHILD_ONE, _CHILD_TWO])
        self._instrument()

        self._cli(["500"], max_passes=4)

        self.assertEqual(self._state("500"), state.COMPLETE)
        self.assertEqual(self._state("601"), state.COMPLETE)
        self.assertEqual(self._state("602"), state.COMPLETE)
        self.assertEqual(self._adoptions(), [
            "issue_500 split → adopted children issue_601 into wave 1",
            "issue_500 split → adopted children issue_602 into wave 2"])

    def test_a_lineage_child_id_that_escapes_the_bundle_root_is_skipped(self) -> None:
        """`split-lineage.json` is a file an operator can hand-edit, and `cfg.bundle` would
        happily build a path outside the bundle root from a traversal id — the hazard
        `split.validate` guards at WRITE time (`split.py:297-311`) and this reader must
        guard at READ time. The escaping id is reported and skipped; the legitimate sibling
        in the same record is still adopted, so one bad entry costs one child, not the
        run.

        The guard is the id SHAPE, not the resolved path, and the difference is why both
        exist: `realpath` NORMALISES `results/issue_../../etc` to `results/etc`, which a
        containment test on the resolved path reads as inside the bundle root. Only
        rejecting the id catches it — and rejecting it is also what keeps every report line
        below printable (see the newline case)."""
        # Independent children here: the escaping entry stands in for 601, so a 602 that
        # declared `Depends on: 601` would be held for the missing prerequisite and prove
        # nothing about the traversal guard.
        self._briefed("500")
        self._arm({"500": ["601", "602"]}, bodies={"500": [_CHILD_ONE, _SIBLING_TWO]},
                  after_split=lambda iid: self._record(iid, ["../../etc", "602"]))

        self._cli(["500"])

        err = self.err.getvalue()
        self.assertIn("ignoring child id '../../etc'", err)
        self.assertIn("is not a plain tracker id", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self._state("602"), state.COMPLETE)   # the sibling still ran
        self.assertEqual(self.waves_driven, [["issue_500"], ["issue_602"]])
        self.assertFalse((self.cfg.bundle_root / "issue_../../etc").exists())

    def test_a_child_bundle_symlinked_out_of_the_instance_is_skipped(self) -> None:
        """Containment is decided AFTER symlinks, because the lexical answer is always
        "inside": `cfg.bundle` builds `<bundle_root>/issue_<id>`, whose parent IS the bundle
        root no matter what that name points at. An `issue_<id>` symlinked to a directory
        outside the instance passes every id-shape check — it is a plain tracker id — and
        the run would drive, sign off and publish a bundle this instance does not own.

        The fixture is a bundle that is otherwise perfectly adoptable (briefed, PLANNED,
        named by the parent's real record): the ONLY reason to refuse it is where it lives.
        Its sibling in the same record is the control — one escaping entry costs one child,
        not the run — and the escapee is asserted untouched on disk, not merely unreported:
        a guard that announced a skip and drove it anyway would satisfy the stderr
        assertion alone."""
        self._briefed("500")
        outside = Path(tempfile.mkdtemp())        # a second root, outside cfg.bundle_root
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        elsewhere = outside / "issue_601"
        elsewhere.mkdir()
        (elsewhere / "brief.md").write_text(_brief("smuggled-child"), encoding="utf-8")

        def symlink_601(_iid: str) -> None:
            # The split really did create results/issue_601; an operator (or a restore
            # script) replaced it with a link to the bundle they kept elsewhere.
            shutil.rmtree(self.cfg.bundle("601"))
            self.cfg.bundle("601").symlink_to(elsewhere, target_is_directory=True)

        self._arm({"500": ["601", "602"]}, bodies={"500": [_CHILD_ONE, _SIBLING_TWO]},
                  after_split=symlink_601)

        rc = self._cli(["500"])

        err = self.err.getvalue()
        self.assertIn("ignoring child id '601'", err)
        self.assertIn("issue_601 resolves outside", err)
        self.assertNotIn("Traceback", err)
        # Not driven: the bundle behind the link is exactly as it was left.
        self.assertEqual(state.state(elsewhere), state.PLANNED)
        self.assertFalse((elsewhere / "patch.diff").exists())
        # …while the sibling in the same record is adopted, driven and announced.
        self.assertEqual(self._state("602"), state.COMPLETE)
        self.assertEqual(self.waves_driven, [["issue_500"], ["issue_602"]])
        self.assertEqual(self._adoptions(),
                         ["issue_500 split → adopted children issue_602 into wave 1"])
        self.assertEqual(rc, 0)

    def test_a_lineage_child_id_with_a_newline_cannot_break_the_report(self) -> None:
        """Every id-shaped value in a report is interpolated unquoted, so the reader has to
        refuse an id that is not a plain tracker token — not merely one that escapes.

        `_lineage_children` strips OUTER whitespace to stop an uncopyable `pdca flow " 469 "`
        hint (`flow.py:694`), but an interior newline survives it, and `issue_6\\n01` is a
        legal directory name directly under the bundle root: it passes containment, reaches
        the "no brief.md" branch and breaks one stderr line into two, the second of which
        reads as a stray line of driver output. The id-shape guard (`split.validate`'s own
        rule at write time, `split.py:297`) refuses it once, quoted, before any path or
        report is built from it."""
        self._briefed("500")
        self._arm({"500": ["601", "602"]}, bodies={"500": [_CHILD_ONE, _SIBLING_TWO]},
                  after_split=lambda iid: self._record(iid, ["6\n01", "602"]))

        rc = self._cli(["500"])

        err = self.err.getvalue()
        self.assertIn(r"ignoring child id '6\n01'", err)        # quoted: one line, copyable
        self.assertNotIn("issue_6\n01", err)                    # …never the raw split line
        self.assertEqual([ln for ln in err.splitlines() if ln.startswith("01")], [],
                         "a report line was broken in two by the id's newline")
        self.assertEqual(self._state("602"), state.COMPLETE)    # the sibling still ran
        self.assertEqual(self.waves_driven, [["issue_500"], ["issue_602"]])
        self.assertEqual(rc, 0)

    def test_a_lineage_id_with_no_bundle_and_one_already_settled_are_both_reported(
            self) -> None:
        """The two remaining reasons a named child is NOT drivable, both reported, neither
        fatal — the same filtering `flow_ids` applies to an id the operator typed.

        `999` has no bundle at all (UNPLANNED: a record naming a child whose Plan beat never
        ran, or one hand-edited onto an id that was never created). Driving it would hand
        `_build_all` a bundle with no brief, so it is named with the Plan hint instead.

        `900` is briefless with a `resolved` record — the tracker settled it outside a cycle
        (#302), which `state.state` reports as RESOLVED and `flow._TERMINAL` counts as
        terminal (`flow.py:676`). Adoption must not re-drive a bundle a run would have
        skipped had the operator named it, and least of all one the tracker has closed.

        Both are read from the same record as a legitimate child, so this also pins the
        blast radius: one unusable entry costs one child, never the sibling and never the
        run."""
        self._briefed("500")
        settled = self.cfg.bundle("900")
        settled.mkdir(parents=True, exist_ok=True)
        (settled / "notes.json").write_text(
            json.dumps({"resolved": {"github_state": "closed",
                                     "state_reason": "not_planned"}}) + "\n",
            encoding="utf-8")
        self.assertEqual(self._state("900"), state.RESOLVED)   # the fixture really is terminal
        self._arm({"500": ["601", "602"]}, bodies={"500": [_CHILD_ONE, _SIBLING_TWO]},
                  after_split=lambda iid: self._record(iid, ["601", "999", "900"]))

        rc = self._cli(["500"])

        err = self.err.getvalue()
        self.assertIn("issue_999 — child of issue_500 NOT adopted: no brief.md", err)
        self.assertIn("issue_900 — child of issue_500 NOT adopted: already terminal "
                      "(RESOLVED)", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self._state("601"), state.COMPLETE)   # the usable child still ran
        self.assertEqual(self._state("900"), state.RESOLVED)   # …and the settled one was left
        self.assertEqual(self.waves_driven, [["issue_500"], ["issue_601"]])
        self.assertEqual(self._adoptions(),
                         ["issue_500 split → adopted children issue_601 into wave 1"])
        self.assertEqual(rc, 0)

    def test_a_children_entry_that_is_not_an_id_is_reported_not_dropped_silently(
            self) -> None:
        """The entries that never reach the filters above still have to be accounted for.

        `_lineage_children` drops what it cannot format with — a non-string, an empty string —
        before adoption's loop sees it (`flow.py:696`), so a hand-edited `"children": [601,
        "602", "603"]` costs 601 in the ONE branch that printed nothing (`ids` is non-empty, so
        the "no readable children record" line does not fire either). The operator's whole log
        then reads `issue_602 held this run — unresolved dependency (601)`, which says 601 does
        not exist while a briefed, PLANNED 601 sits in the next directory: the stranded child
        this feature exists to end, reached quietly. So the shortfall is reported, beside the
        hold it explains. 603 is the control — one unusable entry costs its own child, not the
        record's usable half and not the run."""
        self._briefed("500")
        self._arm({"500": ["601", "602", "603"]},
                  bodies={"500": [_CHILD_ONE, _CHILD_TWO, _brief("child-third")]},
                  after_split=lambda iid: self._record(iid, [601, "602", "603"]))

        rc = self._cli(["500"])

        err = self.err.getvalue()
        self.assertIn("1 of the 3 entries in split-lineage.json's `children` name no "
                      "usable child id", err)
        self.assertIn("the record reads [601, '602', '603']", err)   # names the evidence
        # …beside the line that would otherwise be the only one, and read as "601 is gone".
        self.assertIn("issue_602 held this run — unresolved dependency (601)", err)
        self.assertEqual(self._state("601"), state.PLANNED)   # not driven — and not silent
        self.assertEqual(self._state("602"), state.PLANNED)
        self.assertEqual(self._state("603"), state.COMPLETE)  # the control: the run went on
        self.assertEqual(self.waves_driven, [["issue_500"], ["issue_603"]])
        self.assertEqual(rc, 0)
        self.assertNotIn("Traceback", err)

    def test_a_child_with_an_unresolvable_dependency_is_held_not_fatal(self) -> None:
        """Adopted children go through the resume path's tolerance: one whose declared
        prerequisite cannot be resolved is held loudly, EXCLUDED from the results map (it
        is work the run did not do, and a map that claimed it would be read as a
        disposition), and left in-flight while the run carries on with its sibling. A split
        must never abort the flow that caused it."""
        self._briefed("500")

        def break_602(_iid: str) -> None:
            # A child brief that names a prerequisite outside the proposal (hand-edited
            # after the split, or re-planned since) — unresolvable at adoption time.
            bp = self.cfg.bundle("602") / "brief.md"
            bp.write_text(bp.read_text(encoding="utf-8") + "- **Depends on:** GHOST\n",
                          encoding="utf-8")

        self._arm({"500": ["601", "602"]}, bodies={"500": [_CHILD_ONE, _SIBLING_TWO]},
                  after_split=break_602)
        self._capture_results()

        rc = self._cli(["500"])

        self.assertEqual(self._state("601"), state.COMPLETE)  # the run continued
        self.assertEqual(self._state("602"), state.PLANNED)   # held, left in-flight
        self.assertEqual(rc, 0)                               # …and never an abort
        self.assertEqual(self.results.get("601"), state.COMPLETE)   # adopted and driven
        self.assertNotIn("602", self.results)                 # …the held one is NOT work
        err = self.err.getvalue()
        self.assertIn("issue_602 held this run", err)
        self.assertIn("GHOST", err)
        # …and never announced as adopted into a wave it is not in.
        self.assertNotIn("issue_602 into wave", err)

    def test_a_child_held_by_a_later_reschedule_leaves_the_run(self) -> None:
        """The same promise at the other end of the run: a child adopted into a LATER wave
        that a LATER reschedule holds is dropped back out of the drive set, not left in the
        results map as a bundle this run neither drove nor scheduled.

        Every splice re-levels the whole un-driven tail, so a child can be schedulable when
        it is adopted and unschedulable by the time its wave comes round. Here 601 splits
        inside its own wave — the second splice — and 602, adopted into wave 2 and still
        un-driven, is re-planned onto a prerequisite this run cannot resolve just before
        that splice re-levels it.

        Without the drop the SAME situation reports two different ways, decided only by
        WHICH reschedule happened to hold the child: held on the first (the test above) it
        is out of the map and the run exits 0; held on a later one it stays in the map as
        PLANNED, keeps a stale "adopted into wave 2" line standing, and the run exits 1.
        Only ADOPTED children are dropped — an id the operator NAMED is still answered for
        when it is held (`test_a_named_id_in_the_re_scheduled_tail_is_held_not_lost`).

        "Out of the drive set" is asserted as BOTH halves of that set, because they are two
        representations of one thing: 801 splits in its turn onto a record that also names
        602, and the run must treat 602 as a bundle it is no longer driving — re-examining
        it (and holding it again, loudly) rather than dismissing it with the "already in
        this run's drive set" line, which by then would be false."""
        self._briefed("500")
        self._arm({"500": ["601", "602"], "601": ["801"], "801": ["901"]},
                  bodies={"601": [_CHILD_ONE], "801": [_CHILD_ONE]},
                  after_split=lambda iid: (self._record("801", ["901", "602"])
                                           if iid == "801" else None))
        self._capture_results()
        finished: list[str] = []

        def replan_602_after_the_adopted_wave() -> None:
            # `after_wave` fires between a wave finishing and adoption reading it. After
            # wave 1 — 601's, the one that just split — 602 has been adopted, announced into
            # wave 2 and not yet driven: the one window in which a re-plan can make an
            # already-adopted child unschedulable.
            finished.append("wave")
            if len(finished) == 2:
                (self.cfg.bundle("602") / "brief.md").write_text(
                    _brief("child-second", "- **Depends on:** GHOST"), encoding="utf-8")

        self.after_wave = replan_602_after_the_adopted_wave

        rc = self._cli(["500"])

        self.assertEqual(self._state("601"), state.COMPLETE)   # adopted and driven…
        self.assertEqual(self._state("801"), state.COMPLETE)   # …and its own child after it
        self.assertEqual(self._state("901"), state.COMPLETE)   # …and the run carried on
        self.assertEqual(self._state("602"), state.PLANNED)    # held, left in-flight
        self.assertEqual(self.waves_driven,                    # 602's wave never opened
                         [["issue_500"], ["issue_601"], ["issue_801"], ["issue_901"]])
        # The whole map, not a membership test: the run reports the work it did and nothing
        # else — 602 is neither claimed as a disposition nor counted against the run.
        self.assertEqual(self.results, {"500": state.COMPLETE, "601": state.COMPLETE,
                                        "801": state.COMPLETE, "901": state.COMPLETE})
        self.assertEqual(rc, 0)                                # …so rc matches the first-hold
        err = self.err.getvalue()
        self.assertIn("issue_602 held this run — unresolved dependency (GHOST); left "
                      "in-flight", err)                        # the existing held shape…
        self.assertIn("issue_602 — adopted earlier this run, now held: it is NOT scheduled "
                      "and NOT in this run's results", err)    # …and the retraction
        # …and 801's record, which also names 602, finds a bundle this run is NOT driving.
        self.assertNotIn("issue_602 — child of issue_801 not adopted again", err)
        self.assertNotIn("Traceback", err)

    def test_a_child_listed_twice_in_the_record_is_adopted_once(self) -> None:
        """`split-lineage.json` is a file on disk an operator can hand-edit, so the reader
        must not trust it to be a set. A child listed twice would ride into the reschedule
        twice, take two slots in the drive set the results map and the closing sweep are
        built from, and be announced twice — a run that reports more work than exists."""
        self._briefed("500")
        self._arm({"500": ["601", "602"]},
                  after_split=lambda iid: self._record(iid, ["601", "601", "602"]))

        self._cli(["500"])

        self.assertEqual(self.waves_driven,
                         [["issue_500"], ["issue_601"], ["issue_602"]])  # once each
        self.assertEqual(self._state("601"), state.COMPLETE)
        self.assertEqual(self._state("602"), state.COMPLETE)
        err = self.err.getvalue()
        self.assertEqual(
            err.count("issue_500 split → adopted children issue_601 into wave 1"), 1)
        self.assertNotIn("issue_601, issue_601", err)

    def test_a_symlinked_alias_of_a_bundle_this_run_drives_is_driven_once(self) -> None:
        """Two names can be ONE bundle, so the drive set has to dedupe on the directory.

        An `issue_<id>` symlinked to another bundle INSIDE the root stays adoptable by design
        — it names a bundle this instance owns. What must not follow is that directory entering
        one wave twice: two `_drive_wave` entries for one bundle, two results-map slots, and
        under `lanes > 1` two lanes writing one bundle dir.

        Leg 1 — the alias reaches a bundle the OPERATOR named that this run has not driven yet
        (910 depends on 500, so it is still in the tail when 500 splits). Refused, with both
        names in the line: looked up under `issue_601` the run would report that it does not
        own a directory it is about to drive as `issue_910`. Leg 2 — the alias is a second
        entry in the SAME record, so no name is in the drive set yet and only the resolved path
        can see it. 602 is the control in both: the record's honest entry is still adopted,
        driven and announced."""
        self._briefed("500")
        self._briefed("910", "- **Depends on:** 500")

        def alias_601_to_910(_iid: str) -> None:
            shutil.rmtree(self.cfg.bundle("601"))
            self.cfg.bundle("601").symlink_to(self.cfg.bundle("910"),
                                              target_is_directory=True)

        self._arm({"500": ["601", "602"]}, bodies={"500": [_CHILD_ONE, _SIBLING_TWO]},
                  after_split=alias_601_to_910)
        self._capture_results()

        rc = self._cli(["500", "910"])

        self.assertIn("issue_601 — child of issue_500 not adopted again: already in this "
                      "run's drive set (the same directory as issue_910)",
                      self.err.getvalue())
        self.assertEqual(self.waves_driven,        # …not [.., issue_601, issue_602, issue_910]
                         [["issue_500"], ["issue_602", "issue_910"]])
        self.assertEqual(self.results, {"500": state.COMPLETE, "910": state.COMPLETE,
                                        "602": state.COMPLETE})
        self.assertEqual(self._adoptions(),
                         ["issue_500 split → adopted children issue_602 into wave 1"])
        self.assertEqual(rc, 0)

        self._reset()
        self._briefed("500")

        def alias_610_to_601(iid: str) -> None:
            self.cfg.bundle("610").symlink_to(self.cfg.bundle("601"),
                                              target_is_directory=True)
            self._record(iid, ["601", "610", "602"])

        self._arm({"500": ["601", "602"]}, bodies={"500": [_CHILD_ONE, _SIBLING_TWO]},
                  after_split=alias_610_to_601)

        rc = self._cli(["500"])

        self.assertEqual(self.waves_driven,        # …601 once, under the name it came in as
                         [["issue_500"], ["issue_601", "issue_602"]])
        self.assertEqual(self._adoptions(),
                         ["issue_500 split → adopted children issue_601, issue_602 "
                          "into wave 1"])
        self.assertNotIn("issue_610", self.err.getvalue())
        self.assertEqual([self._state(i) for i in ("601", "602")], [state.COMPLETE] * 2)
        self.assertEqual(rc, 0)

    def test_a_child_already_named_in_the_run_is_not_adopted_twice(self) -> None:
        """A record that names a bundle the operator ALSO put on the command line. It is
        already in the drive set, so adopting it again would schedule, drive, count and
        announce one bundle twice. Skipped — and said out loud, because a child the
        operator also listed is the one skip they are most likely to look for in the log."""
        self._briefed("500")
        self._briefed("810")
        self._arm({"500": ["601", "602"]}, bodies={"500": [_CHILD_ONE, _SIBLING_TWO]},
                  after_split=lambda iid: self._record(iid, ["601", "810"]))

        rc = self._cli(["500", "810"])

        self.assertEqual(self.waves_driven,
                         [["issue_500", "issue_810"], ["issue_601"]])
        self.assertEqual(self._state("810"), state.COMPLETE)   # driven once, in wave 0
        self.assertEqual(self._state("601"), state.COMPLETE)   # the real child adopted
        self.assertEqual(rc, 0)
        self.assertIn("issue_810 — child of issue_500 not adopted again: already in this "
                      "run's drive set", self.err.getvalue())
        self.assertEqual(self._adoptions(),
                         ["issue_500 split → adopted children issue_601 into wave 1"])

    def test_a_child_adopted_earlier_is_not_re_adopted_by_a_later_parent(self) -> None:
        """The drive set has to REMEMBER what it adopted, from one adoption call to the next.

        One run, two splits in two different waves: 500 splits in wave 0 and its children
        join wave 1, then 700 splits inside wave 1 with a record that also names 602
        (hand-edited, or re-planned onto a slice its sibling already owns). The in-call
        `taken` set does not survive the call — only the run's drive set does — so a child
        adopted in an EARLIER call is skipped by the same "already in this run's drive set"
        rule that skips one the operator named. Without that, 602 is adopted twice: two
        slots in the drive set, two announcements, one bundle reported as work in two
        places."""
        self._briefed("500")
        self._briefed("700", "- **Depends on:** 500")
        self._arm({"500": ["601", "602"], "700": ["801"]},
                  bodies={"500": [_CHILD_ONE, _SIBLING_TWO], "700": [_CHILD_ONE]},
                  after_split=lambda iid: (self._record(iid, ["602", "801"])
                                           if iid == "700" else None))

        rc = self._cli(["500", "700"])

        self.assertIn("issue_602 — child of issue_700 not adopted again: already in this "
                      "run's drive set", self.err.getvalue())
        self.assertEqual(self._adoptions(), [
            "issue_500 split → adopted children issue_601, issue_602 into wave 1",
            "issue_700 split → adopted children issue_801 into wave 2"])
        self.assertEqual(self.waves_driven,                # …and 602 is driven ONCE
                         [["issue_500"],
                          ["issue_601", "issue_602", "issue_700"],
                          ["issue_801"]])
        self.assertEqual([self._state(i) for i in ("601", "602", "801")],
                         [state.COMPLETE] * 3)
        self.assertEqual(rc, 0)

    def test_two_parents_splitting_in_one_wave_adopt_a_shared_child_once(self) -> None:
        """The other half of the same rule, and the one the run's drive set cannot carry:
        two parents that split in the SAME wave, the second's record also naming the
        first's child.

        `batch_names` is grown once, at the END of the adoption call, so within the call it
        still describes the run as it was before either parent was read — which makes it
        blind to a child the parent examined a moment ago has already taken. Only the
        in-call `taken` set sees that. Drop it (`known = batch_names | taken` →
        `known = batch_names`) and 602 is adopted by BOTH parents: announced twice, entered
        into the reschedule and the drive set twice, one bundle reported as two pieces of
        work."""
        self._briefed("500")
        self._briefed("700")
        self._arm({"500": ["601", "602"], "700": ["801"]},
                  bodies={"500": [_CHILD_ONE, _SIBLING_TWO], "700": [_CHILD_ONE]},
                  after_split=lambda iid: (self._record(iid, ["602", "801"])
                                           if iid == "700" else None))

        rc = self._cli(["500", "700"])

        self.assertIn("issue_602 — child of issue_700 not adopted again: already in this "
                      "run's drive set", self.err.getvalue())
        self.assertEqual(self._adoptions(), [
            "issue_500 split → adopted children issue_601, issue_602 into wave 1",
            "issue_700 split → adopted children issue_801 into wave 1"])
        self.assertEqual(self.waves_driven,                # …one wave 1, 602 in it ONCE
                         [["issue_500", "issue_700"],
                          ["issue_601", "issue_602", "issue_801"]])
        self.assertEqual([self._state(i) for i in ("601", "602", "801")],
                         [state.COMPLETE] * 3)
        self.assertEqual(rc, 0)

    def test_a_shared_child_the_reschedule_holds_is_not_reported_as_driven(self) -> None:
        """The refusal and the hold are one situation, so they must not tell an operator two
        different things about who owns the child.

        Same shape as the shared-child test above — 500 and 700 split in one wave, 700's
        record also naming 500's child 602 — except that 602 has been re-planned onto a
        prerequisite this run cannot resolve, so the splice HOLDS it. `taken` is grown from
        what a parent CLAIMED, before the reschedule runs, so a claim is not yet a schedule:
        reported eagerly, 602 draws "already in this run's drive set" for 700 and "held this
        run … left in-flight" for itself, while sitting in neither the drive set nor the
        results map. An operator reading the log to find out who owns 602 is told, in the
        same paragraph, that the run has it and that the run does not.

        So the refusal is answered after the splice, against the drive set as it finally
        stands: nobody in this run owns 602, and the line says so and names the resume."""
        self._briefed("500")
        self._briefed("700")

        def rig(iid: str) -> None:
            if iid == "500":
                # 602 is adoptable when 500's record is read and unschedulable by the time
                # the splice levels it — the window the eager report gets wrong.
                bp = self.cfg.bundle("602") / "brief.md"
                bp.write_text(bp.read_text(encoding="utf-8") + "- **Depends on:** GHOST\n",
                              encoding="utf-8")
            if iid == "700":
                self._record("700", ["602", "801"])

        self._arm({"500": ["601", "602"], "700": ["801"]},
                  bodies={"500": [_CHILD_ONE, _SIBLING_TWO], "700": [_CHILD_ONE]},
                  after_split=rig)
        self._capture_results()

        rc = self._cli(["500", "700"])

        err = self.err.getvalue()
        # The claim the run cannot honour is never made…
        self.assertNotIn("issue_602 — child of issue_700 not adopted again: already in "
                         "this run's drive set", err)
        # …and the accurate one is, next to the hold, naming the parent that was refused.
        self.assertIn("issue_602 — child of issue_700 NOT adopted: another parent in this "
                      "run claimed it first and it is not scheduled — it is NOT in this "
                      "run's drive set or its results; resume it with `pdca flow 602`", err)
        self.assertIn("issue_602 held this run — unresolved dependency (GHOST); left "
                      "in-flight", err)
        self.assertNotIn("issue_602 into wave", err)          # never announced as adopted
        # The end state the two lines describe: 602 is left in flight and out of the map,
        # the other children are adopted and driven, and the run does not fail.
        self.assertEqual(self._state("602"), state.PLANNED)
        self.assertEqual(self.results, {"500": state.COMPLETE, "700": state.COMPLETE,
                                        "601": state.COMPLETE, "801": state.COMPLETE})
        self.assertEqual(self.waves_driven,
                         [["issue_500", "issue_700"], ["issue_601", "issue_801"]])
        self.assertEqual(rc, 0)

    def test_a_split_parent_without_a_children_record_is_reported_not_guessed(self) -> None:
        """No readable `split-lineage.json` ⇒ report it and degrade to today's behaviour
        (the operator's `pdca flow <child-ids>`). Never a crash, never a prose parse of the
        `build-notes.md` breadcrumb `split.accept` leaves for the human."""
        self._briefed("500")
        self._arm({"500": ["601", "602"]},
                  after_split=lambda iid: (self.cfg.bundle(iid) / split.LINEAGE).unlink())

        rc = self._cli(["500"])

        self.assertEqual(self._state("500"), state.COMPLETE)  # the run finished cleanly
        self.assertEqual(rc, 0)
        self.assertEqual(self._state("601"), state.PLANNED)   # not driven, not lost
        self.assertIn("no readable children record", self.err.getvalue())

    def test_an_unreadable_close_marker_never_kills_the_run(self) -> None:
        """The split probe is a HINT read, so no way of failing to read it may become a
        verdict. A `close-disposition` whose bytes are not UTF-8 raises `UnicodeDecodeError`
        — a `ValueError`, not the `OSError` a narrow handler expects — and a probe that let
        it out would report the parent's whole adoption as failed and leave the reason
        buried in an isolation notice. Unreadable means "not a split", full stop: the run
        drives on, THAT parent's children are simply not adopted, and nothing is announced.

        Two parents split in the same wave and only 500's marker is corrupted, because a
        run in which nothing is adopted at all satisfies every assertion here trivially —
        including a build with no adoption whatsoever (the C4 red leg). 700's child is the
        control: it must still be adopted and driven, so what is asserted is that one
        unreadable marker costs exactly its own parent's children and nothing else.

        The corruption is injected between the wave finishing and adoption reading the
        marker (`after_wave`), which is the only window in which the marker is both written
        (`split.accept`) and not yet re-read by the close path."""
        self._briefed("500")
        self._briefed("700")
        self._arm({"500": ["601", "602"], "700": ["801"]},
                  bodies={"500": [_CHILD_ONE, _SIBLING_TWO], "700": [_CHILD_ONE]})
        self.after_wave = lambda: (self.cfg.bundle("500") / state.CLOSE_MARKER
                                   ).write_bytes(b"split\xff\n")

        rc = self._cli(["500", "700"])

        self.assertEqual(self._state("500"), state.COMPLETE)  # the named ids still ran
        self.assertEqual(self._state("700"), state.COMPLETE)
        self.assertEqual(self._state("601"), state.PLANNED)   # not guessed at either
        self.assertEqual(self._state("602"), state.PLANNED)
        # The control: the readable sibling's child IS adopted, driven and announced, so a
        # green here means "the probe swallowed it", not "there is no probe".
        self.assertEqual(self._state("801"), state.COMPLETE)
        self.assertEqual(self.waves_driven,
                         [["issue_500", "issue_700"], ["issue_801"]])
        self.assertEqual(self._adoptions(),
                         ["issue_700 split → adopted children issue_801 into wave 1"])
        self.assertEqual(rc, 0)   # a run, not a crash
        err = self.err.getvalue()
        self.assertNotIn("Traceback", err)
        self.assertNotIn("split adoption failed", err)   # contained IN the probe, not around


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
