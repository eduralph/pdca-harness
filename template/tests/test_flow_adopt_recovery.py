"""A split's children must not stay stranded because the run that made them ended (#473).

Three gaps the adoption core (#469, `tests/test_flow_adopt_split.py`) left open, all of
them reached through `cli._flow` — the surface the operator and their automation use:

1. **Recovery.** Adoption only fired for a bundle that split *while the run was driving
   it*. Handed an id whose bundle was ALREADY terminal on a split — a crashed run, a `^C`,
   a split accepted in an earlier session — `flow_ids`' pre-run filter skipped it and the
   children stayed PLANNED, so the brood still needed a hand-typed `pdca flow <child-ids>`.
   The parent is still skipped (a terminal bundle has nothing to build), but it is now
   handed on as an adoption SEED, and the walk goes THROUGH a generation an earlier run
   already split.
2. **Budget.** The run's pass pool was sized once, off the schedule the run set out with,
   so a splice pushed work into waves the arithmetic never counted: `pdca flow 500 810
   --max-passes 2` left 810 — an id the operator TYPED — PLANNED with "the run's pass
   budget is spent". The pool is now read off the LIVE schedule, one allowance per wave.
3. **Stdout.** The single-id shape printed one `state<TAB>path` line for the id it was
   given while deriving its exit code from the WHOLE map, so `pdca flow 500` could print
   `COMPLETE` and exit 1 without naming the adopted bundle that failed — a caller reading
   the documented machine contract read success off a failed run.

Everything is the ordinary offline driver suite: all six leaves stubbed (the fixture
mirrors `tests/test_flow_adopt_split.py:43-64`), gates empty, no tracker / network / `gh` /
container. Splits are never simulated — the tests call the PRODUCTION `split.accept`
(`split.py:525`), and the "an earlier run stranded these" fixture is carried to COMPLETE by
the production `flow._drive_wave`, so the disk a recovery run starts from is what a real
interrupted run leaves.

Modules are imported, never new symbols (`from pdca_harness import cli, flow, …`): a
`from pdca_harness.flow import <new helper>` would raise ImportError on the C4 red leg,
which `engine/scripts/run-verify.sh` classifies PDCA-UNVERIFIABLE rather than red.

    cd template && PYTHONPATH=src python3 -m unittest tests.test_flow_adopt_recovery
"""

from __future__ import annotations

import io
import json
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from pdca_harness import cli, flow, leaves, signoff, split, state
from pdca_harness.config import Config, LeafConfig


def _stub_config(root: Path) -> Config:
    """All six leaves stubbed, gates empty (all-PASS stub rows) — the same fixture shape as
    `tests/test_flow_adopt_split.py:43-64`, including the hermetic toy checkout inside the
    tmp root (the sibling convention would resolve to a SHARED /tmp/example-repo)."""
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


class AdoptRecovery(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = self._instance()
        self.err = io.StringIO()
        self.out = io.StringIO()
        self.waves_driven: list[list[str]] = []
        self.passes = 0                  # one `_build_all` call == one pass of one wave
        self._orig = (leaves.do_plan, leaves.run_signoff_batch, flow._drive_wave,
                      flow._build_all, leaves.do_build)

    def tearDown(self) -> None:
        (leaves.do_plan, leaves.run_signoff_batch, flow._drive_wave, flow._build_all,
         leaves.do_build) = self._orig

    # -- instance + capture -------------------------------------------------------------

    def _instance(self) -> Config:
        """A fresh, hermetic instance root, removed at teardown."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return _stub_config(tmp)

    def _reset(self) -> None:
        """A second leg of the same test, on a clean instance and unpatched leaves."""
        (leaves.do_plan, leaves.run_signoff_batch, flow._drive_wave, flow._build_all,
         leaves.do_build) = self._orig
        self.cfg = self._instance()
        self.err = io.StringIO()
        self.out = io.StringIO()
        self.waves_driven = []
        self.passes = 0

    def _args(self, ids: list[str], *, max_passes: int | None = None) -> SimpleNamespace:
        """The `pdca flow <ids…>` argv as `cli._flow` receives it — arity is the CLI's own
        presentation switch (`cli.py:622`), so it has to be the thing under test rather than
        a hand-picked `flow.*` call. `--no-publish` throughout (no git remotes)."""
        return SimpleNamespace(issue_ids=ids, from_csv=None, from_briefs=None,
                               no_publish=True, no_act=True, by="", lanes=None,
                               max_passes=max_passes)

    def _cli(self, ids: list[str], *, max_passes: int | None = None) -> int:
        """Run `pdca flow <ids…>` through `cli._flow` and return its exit code."""
        with redirect_stderr(self.err), redirect_stdout(self.out):
            return cli._flow(self.cfg, self._args(ids, max_passes=max_passes))

    def _state(self, issue_id: str) -> str:
        return state.state(self.cfg.bundle(issue_id))

    def _adoptions(self) -> list[str]:
        return [ln.split("flow: ")[-1] for ln in self.err.getvalue().splitlines()
                if "split → adopted children" in ln]

    def _about_children(self, *ids: str) -> list[str]:
        """Every stderr line naming one of `ids`, with the absolute wave index made
        RELATIVE to the first wave adoption filled.

        A recovery run has no wave of its own for the parent — its children start at wave 0
        where a mid-run split's start at wave 1 — so the absolute index is the one thing
        that cannot be equal between the two shapes on equivalent disk. Everything else the
        run says about a child (which parent adopted it, which children share a wave, the
        order of the waves, and what it was left as) is compared verbatim."""
        names = tuple(f"issue_{i}" for i in ids)
        lines = [ln for ln in self.err.getvalue().splitlines()
                 if any(n in ln for n in names)]
        found = [int(m.group(1)) for ln in lines
                 if (m := re.search(r"into wave (\d+)$", ln))]
        base = min(found, default=0)
        return [re.sub(r"into wave (\d+)$",
                       lambda m: f"into wave +{int(m.group(1)) - base}", ln)
                for ln in lines]

    def _stdout(self) -> list[str]:
        """Captured stdout with this leg's tmp root masked, so two legs on two hermetic
        instances are comparable line for line."""
        return self.out.getvalue().replace(str(self.cfg.root), "<root>").splitlines()

    def _line(self, iid: str, st: str) -> str:
        """The `state<TAB>path` line `_stdout` should carry for one bundle."""
        return f"{st}\t<root>/results/issue_{iid}"

    def _record(self, iid: str, children: list[str]) -> None:
        """Hand-edit a parent's `split-lineage.json` children edge, as an operator can."""
        path = self.cfg.bundle(iid) / split.LINEAGE
        record = json.loads(path.read_text(encoding="utf-8"))
        record["children"] = children
        path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    # -- the split, exactly as `pdca split --accept` leaves it --------------------------

    def _split_now(self, parent: Path, ids: list[str], bodies: list[str]) -> None:
        """Decompose `parent` into `ids` through the PRODUCTION `split.accept`: child
        bundles + each child's lineage record, the parent's merged `children` record, its
        build-notes breadcrumb and its `close-disposition = split` marker."""
        parent.mkdir(parents=True, exist_ok=True)
        if not (parent / "brief.md").exists():
            (parent / "brief.md").write_text(_brief("parent-slice"), encoding="utf-8")
        (parent / split.PROPOSAL).write_text(_proposal(*bodies), encoding="utf-8")
        split.accept(parent, ids, self.cfg)

    def _drive_to_complete(self, d: Path, max_passes: int = 2) -> None:
        """Carry ONE bundle to COMPLETE with production code and no adoption of its own
        (`flow._drive_wave` is the per-wave driver; it has never looked for children) — so a
        stranded split is built without the very mechanism under test."""
        real_wave = self._orig[2]
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            real_wave(self.cfg, [d], by="t", today="2026-08-10", max_passes=max_passes)

    def _strand_a_split(self, parent: str = "500", ids: tuple[str, ...] = ("601", "602"),
                        bodies: list[str] | None = None) -> None:
        """Leave the instance exactly as an EARLIER run that split `parent` did: the parent
        terminal on `close-disposition = split` with a children record, its children sitting
        PLANNED and undriven. The fault the recovery run has to fix is genuinely on disk
        before it starts, and every byte of it was written by production code."""
        self._split_now(self.cfg.bundle(parent), list(ids),
                        bodies or [_CHILD_ONE, _CHILD_TWO])
        self._drive_to_complete(self.cfg.bundle(parent))
        self.assertEqual(state.state(self.cfg.bundle(parent)), state.COMPLETE)
        self.assertEqual((self.cfg.bundle(parent) / state.CLOSE_MARKER).read_text(
            encoding="utf-8").strip(), "split")
        for cid in ids:
            self.assertEqual(state.state(self.cfg.bundle(cid)), state.PLANNED)

    # -- leaf stubs -------------------------------------------------------------------

    def _arm(self, splits: dict[str, list[str]] | None = None, *,
             bodies: dict[str, list[str]] | None = None, iterate_once: str = "",
             walk_away: str = "") -> None:
        """Stub the leaves and install the wave / pass spies.

        With `splits`, each id in it splits mid-flight into its child ids, down the
        documented Entry B: the sign-off session records `iterate-plan`, the driver re-opens
        the bundle to UNPLANNED, and the next pass's Plan pre-pass concludes it is too large
        and splits it. Called with no `splits` it only instruments — which is what a run
        that starts from a split an EARLIER run accepted needs.

        `iterate_once` makes one bundle cost a second pass (an ordinary `iterate-do` on its
        first sign-off); `walk_away` is the session never answered for one bundle at all,
        which halts it at AWAITING_SIGNOFF — the ordinary end of an interactive run, and
        how a wave is made to want more passes than its allowance."""
        splits, bodies = splits or {}, bodies or {}
        real_plan, real_signoff_batch = self._orig[0], self._orig[1]
        done: set[str] = set()

        def splitting_plan(d: Path, cfg: Config, csv: str | None = None) -> None:
            iid = d.name.removeprefix("issue_")
            if iid in splits and f"split:{iid}" not in done:
                done.add(f"split:{iid}")
                self._split_now(d, splits[iid], bodies.get(iid, [_CHILD_ONE, _CHILD_TWO]))
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
                # halts at AWAITING_SIGNOFF, pass after pass, until its wave runs out.
                return True
            return False

        def signoff_batch(cfg: Config, bundles: list[Path]) -> None:
            real_signoff_batch(cfg, [d for d in bundles if not decide(d)])

        leaves.do_plan = splitting_plan
        leaves.run_signoff_batch = signoff_batch
        self._instrument()

    def _instrument(self) -> None:
        """The wave / pass spies. Pass-through: each calls the PRODUCTION function and hands
        its exact return value back, so nothing here stands in for what is under test."""
        real_wave, real_build_all = self._orig[2], self._orig[3]

        def counting(cfg: Config, wave: list[Path]) -> None:
            self.passes += 1
            real_build_all(cfg, wave)

        def spy_wave(cfg: Config, wave: list[Path], **kw):
            self.waves_driven.append([d.name for d in wave])
            return real_wave(cfg, wave, **kw)

        flow._build_all = counting
        flow._drive_wave = spy_wave

    def _build_fails(self, iid: str) -> None:
        """One bundle's Do leaf raises on every pass — the ordinary way a wave STALLS.

        `_advance_one` contains it (`flow.py:459`), so the bundle's state never changes and
        nobody is left awaiting sign-off. A pass-through spy: every OTHER bundle is built by
        the production leaf, so the fault is one injected failure, not a fixture that stops
        building. It falls through to the PRODUCTION leaf captured in `setUp`, so a second
        leg's spy never stacks on the first's."""
        real = self._orig[4]

        def failing(d: Path, cfg: Config) -> None:
            if d.name == f"issue_{iid}":
                raise RuntimeError(f"builder leaf failed for {d.name}")
            real(d, cfg)

        leaves.do_build = failing

    def _briefed(self, iid: str, *extra: str) -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "brief.md").write_text(_brief(f"slice-{iid}", *extra), encoding="utf-8")
        return d

    # -- criterion (1): recovery ---------------------------------------------------------

    def test_a_run_named_after_an_already_split_parent_drives_its_children(self) -> None:
        """`pdca flow 500` where 500 is ALREADY terminal on a split: an earlier run left
        601/602 PLANNED, and naming the parent again is the operator's recovery. The pre-run
        terminal filter still skips the parent — there is nothing to BUILD for a terminal
        bundle, and #468's non-destructive hint still prints — but it no longer swallows the
        id: the bundle is handed on as an adoption seed and this run drives the brood."""
        self._strand_a_split()
        self._arm()

        rc = self._cli(["500"])

        self.assertEqual(self._state("601"), state.COMPLETE)   # …not left where they were
        self.assertEqual(self._state("602"), state.COMPLETE)
        self.assertEqual(rc, 0)
        # The children are levelled by their OWN edges (602 `Depends on` 601), in front of
        # the whole schedule — the seed is not a wave, it is already finished.
        self.assertEqual(self.waves_driven, [["issue_601"], ["issue_602"]])
        self.assertEqual(self._adoptions(), [
            "issue_500 split → adopted children issue_601 into wave 0",
            "issue_500 split → adopted children issue_602 into wave 1"])
        err = self.err.getvalue()
        self.assertIn("already terminal (COMPLETE), skipped", err)   # still skipped…
        self.assertIn("its children are examined for adoption into THIS run", err)
        self.assertNotIn("rm -rf", err)        # …and never the destructive advice (#468)

    def test_recovery_walks_through_a_generation_an_earlier_run_already_split(self) -> None:
        """Recovery follows the lineage as far as it actually goes. A run that stopped
        part-way down a chain (500 split → 601, 602; 601 then split → 701, 702) leaves 601
        TERMINAL on a split — undrivable itself, but the only route to the grandchildren
        that are still stranded. Stopping where the terminal filter finds it strands 701/702
        forever, which is exactly the state a crashed run leaves behind."""
        self._strand_a_split()
        self._split_now(self.cfg.bundle("601"), ["701", "702"], [_CHILD_ONE, _CHILD_TWO])
        self._drive_to_complete(self.cfg.bundle("601"))
        self.assertEqual(self._state("601"), state.COMPLETE)   # terminal on ITS OWN split
        self.assertEqual(self._state("701"), state.PLANNED)    # …and the brood below it
        self._arm()

        rc = self._cli(["500"])

        self.assertEqual(self._state("602"), state.COMPLETE)   # the drivable child…
        self.assertEqual(self._state("701"), state.COMPLETE)   # …and the grandchildren
        self.assertEqual(self._state("702"), state.COMPLETE)
        self.assertEqual(rc, 0)
        err = self.err.getvalue()
        self.assertIn("issue_601 — child of issue_500 is itself terminal on a split; "
                      "examining it for children to adopt", err)
        # Attributed to the parent that actually declared them, never to the run's seed.
        self.assertIn("issue_601 split → adopted children issue_701", err)
        self.assertIn("issue_601 split → adopted children issue_702", err)

    def test_a_lineage_cycle_is_examined_once_and_the_run_returns(self) -> None:
        """The walk is a queue over a file an operator can hand-edit, so it has to be
        bounded by construction. A record naming an ANCESTOR — 601's `children` edited to
        list 500, its own parent — is a cycle; each candidate leaves the queue once, so it
        drains, the run returns, and the ancestor is neither adopted (it is terminal) nor
        re-examined. That bound is also the RUN's since #473, because the pass pool no
        longer truncates a schedule adoption grew."""
        self._strand_a_split()
        self._split_now(self.cfg.bundle("601"), ["701", "702"], [_CHILD_ONE, _CHILD_TWO])
        self._drive_to_complete(self.cfg.bundle("601"))
        self._record("601", ["701", "702", "500"])     # …the cycle, hand-edited
        self._arm()

        rc = self._cli(["500"])

        self.assertEqual(self._state("701"), state.COMPLETE)   # the run finished its work
        self.assertEqual(self._state("702"), state.COMPLETE)
        self.assertEqual(rc, 0)
        err = self.err.getvalue()
        self.assertEqual(err.count("issue_500 — child of issue_601 is itself terminal on a "
                                   "split; examining it for children to adopt"), 1)
        self.assertEqual(self._state("500"), state.COMPLETE)   # never re-driven
        self.assertEqual(self._adoptions(), sorted(set(self._adoptions())))

    def test_a_recovery_run_with_nothing_left_to_adopt_is_a_clean_no_op(self) -> None:
        """The other end of recovery: the brood an earlier run stranded has since been
        driven, so the seed offers nothing this run can take. A run with an EMPTY schedule
        is the one shape adoption never had before — it must stay the quiet, successful
        no-op that naming a finished bundle has always been: not a crash, not a wave of
        nothing, not an abandoned-budget report. Each child is still accounted for by name,
        which is what tells the operator the recovery had already happened."""
        self._strand_a_split()
        for cid in ("601", "602"):
            self._drive_to_complete(self.cfg.bundle(cid))
            self.assertEqual(self._state(cid), state.COMPLETE)
        self._arm()

        rc = self._cli(["500"])

        self.assertEqual(rc, 0)
        self.assertEqual(self.waves_driven, [])       # nothing to drive, nothing driven
        self.assertEqual(self.passes, 0)
        err = self.err.getvalue()
        for cid in ("601", "602"):
            self.assertIn(f"issue_{cid} — child of issue_500 NOT adopted: already "
                          f"terminal (COMPLETE)", err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("budget", err)
        self.assertEqual(self._stdout(), [self._line("500", state.COMPLETE)])

    def test_a_recovery_seed_is_re_levelled_together_with_the_ids_named_beside_it(
            self) -> None:
        """A recovery seed does not get a private schedule: `pdca flow 500 810`, with 500
        already split and 810 an ordinary briefed id, re-levels the ADOPTED children and the
        un-driven remainder of the operator's own list in one pass, by their own edges. 810
        declares `Conflicts with: 601`, so it must not share 601's wave — a splice that
        simply prepended the brood, or dropped the named tail while inserting it, would put
        two conflicting bundles in one wave or lose 810 outright. Every id named is still
        answered for, and the batch shape reports the adopted children as it always has."""
        self._strand_a_split()
        self._briefed("810", "- **Conflicts with:** 601")
        self._arm()

        rc = self._cli(["500", "810"])

        self.assertEqual(rc, 0)
        for iid in ("601", "602", "810"):
            self.assertEqual(self._state(iid), state.COMPLETE)
        # 601 first (the conflict orients the pair), then its dependent 602 beside 810.
        self.assertEqual(self.waves_driven,
                         [["issue_601"], ["issue_602", "issue_810"]])
        self.assertEqual(sorted(self._stdout()), sorted([
            f"{state.COMPLETE}\t500", f"{state.COMPLETE}\t601",
            f"{state.COMPLETE}\t602", f"{state.COMPLETE}\t810",
            "flow: 4/4 complete"]))

    def test_the_mid_run_and_recovery_shapes_agree_on_equivalent_disk(self) -> None:
        """One event, one description, one exit code — whether the split happens DURING the
        run or an earlier run left it on disk. Both legs decompose 500 into 601/602 with the
        production `split.accept`, and in both the same child (602) cannot be built, so the
        run does not finish: the two shapes must agree on the children's states, on
        everything the run says about them on stderr, on stdout, and on the exit code. Only
        the absolute wave index can differ (a recovery run drives no wave for the parent),
        which `_about_children` normalises and nothing else does."""
        seen: dict[str, tuple[dict[str, str], list[str], list[str], int]] = {}

        self._reset()                       # leg A — the split happens mid-run
        self._briefed("500")
        self._arm({"500": ["601", "602"]})
        self._build_fails("602")
        rc = self._cli(["500"])
        seen["mid-run"] = ({i: self._state(i) for i in ("601", "602")},
                           self._about_children("601", "602"), self._stdout(), rc)

        self._reset()                       # leg B — an earlier run left the same split
        self._strand_a_split()
        self._arm()
        self._build_fails("602")
        rc = self._cli(["500"])
        seen["recovery"] = ({i: self._state(i) for i in ("601", "602")},
                            self._about_children("601", "602"), self._stdout(), rc)

        self.assertEqual(seen["mid-run"][0], {"601": state.COMPLETE, "602": state.PLANNED})
        # Written out rather than merely compared, so the parity assertion below cannot pass
        # by comparing two empty lists: this is everything a run says about the children —
        # the adoption, every beat they are driven through, the injected failure, the hint.
        self.assertEqual(seen["mid-run"][1], [
            "flow: issue_500 split → adopted children issue_601 into wave +0",
            "flow: issue_500 split → adopted children issue_602 into wave +1",
            "→ issue_601: Do — builder writing patch.diff + test…",
            "→ issue_601: Check — running gates…",
            "→ issue_601: Check — advisory reviewer…",
            "→ issue_601: assembling SUMMARY…",
            "→ issue_602: Do — builder writing patch.diff + test…",
            "flow: issue_602 — build/check failed (RuntimeError: builder leaf failed for "
            "issue_602); skipping this bundle (left PLANNED)",
            "flow:   issue_602 [PLANNED] — resume with `pdca flow 602`"])
        self.assertEqual(seen["mid-run"][2], [self._line("500", state.COMPLETE),
                                              self._line("601", state.COMPLETE),
                                              self._line("602", state.PLANNED)])
        self.assertEqual(seen["mid-run"][3], 1)      # the run really did not finish
        self.assertEqual(seen["recovery"], seen["mid-run"])

    # -- criterion (2): the pool funds every wave the run grows into ---------------------

    def test_an_id_the_operator_typed_is_not_starved_by_a_wave_adoption_added(self) -> None:
        """The pool sized ONCE is a promise the run cannot keep, and the bundle it breaks it
        for can be one the operator TYPED.

        `pdca flow 500 810 --max-passes 2`: 810 declares `Depends on: 500` (wave 1) and
        `Conflicts with: 601`, so when 500 splits into 601 the splice puts 601 in wave 1 and
        pushes 810 — a named id — into wave 2. 601 iterates once, so it costs two passes,
        and a pool sized for the two waves the run set out with is spent before 810's wave
        ever opens: 810 left PLANNED, rc 1, "the run's pass budget is spent". Read off the
        live schedule, the wave the splice created is funded like every other and the id the
        operator asked for is answered."""
        self._briefed("500")
        self._briefed("810", "- **Depends on:** 500", "- **Conflicts with:** 601")
        self._arm({"500": ["601"]}, bodies={"500": [_CHILD_ONE]}, iterate_once="601")

        rc = self._cli(["500", "810"], max_passes=2)

        self.assertEqual(self._state("810"), state.COMPLETE)   # the typed id is answered
        self.assertEqual(self._state("601"), state.COMPLETE)   # …and the adopted child ran
        self.assertEqual(self._state("500"), state.COMPLETE)
        self.assertEqual(rc, 0)
        self.assertEqual(self.waves_driven,
                         [["issue_500"], ["issue_601"], ["issue_810"]])
        self.assertNotIn("pass budget is spent", self.err.getvalue())

    def test_a_recovery_run_funds_every_wave_its_adopted_children_need(self) -> None:
        """The same fault at the other end: a recovery run's OWN drive set is empty, so a
        pool sized off it funds nothing at all however many children the seed then hands
        over. Here `--max-passes 1` is the operator's allowance per wave and the brood needs
        two waves (602 `Depends on` 601): both are driven, one pass each, because the pool
        is read off the schedule adoption produced."""
        self._strand_a_split()
        self._arm()

        rc = self._cli(["500"], max_passes=1)

        self.assertEqual(self._state("601"), state.COMPLETE)
        self.assertEqual(self._state("602"), state.COMPLETE)   # the second wave is funded
        self.assertEqual(rc, 0)
        self.assertEqual(self.passes, 2)                       # one allowance per wave
        self.assertNotIn("pass budget is spent", self.err.getvalue())

    def test_a_wave_still_gets_no_more_than_the_operators_allowance(self) -> None:
        """Funding every wave is not a licence to spend: no wave gets a second allowance,
        and nothing gives back what the run has already spent. 601's sign-off is never
        answered, so its wave would pass forever and `--max-passes 1` gives it one: it stops
        there, AWAITING_SIGNOFF and NAMED with its resume hint. The NEXT wave (603, which
        `Depends on` the sibling that did finish) still gets the one pass that is its own
        allowance — two waves, two passes, neither borrowed from the other. Without the cap,
        re-sizing would be a licence to spend."""
        self._strand_a_split(ids=("601", "602", "603"),
                             bodies=[_CHILD_ONE, _SIBLING_TWO,
                                     _brief("child-third", "- **Depends on:** child-2")])
        self._arm(walk_away="601")

        rc = self._cli(["500"], max_passes=1)

        self.assertEqual(self._state("601"), state.AWAITING_SIGNOFF)  # stopped at its cap
        self.assertEqual(self._state("602"), state.COMPLETE)    # …its sibling still ran
        self.assertEqual(self._state("603"), state.COMPLETE)    # …and so did the next wave
        self.assertEqual(self.passes, 2)          # 1 per wave — never 2 for one wave
        # rc 0, and deliberately: a SINGLE-id run counts AWAITING_SIGNOFF as a successful
        # end (#468 — stopping for the human who just typed the command), and this change
        # does not touch that rule. The bundle is still named on stderr, below.
        self.assertEqual(rc, 0)
        err = self.err.getvalue()
        self.assertIn("pass budget exhausted after 1 pass(es)", err)
        self.assertIn("issue_601 [AWAITING_SIGNOFF] — resume with `pdca flow 601`", err)

    def test_no_wave_opens_on_budget_the_pool_does_not_hold(self) -> None:
        """The pool's admission rule, kept live rather than as decoration (#469's guard,
        re-sized by #473). With one allowance per live wave, every wave capped at that
        allowance and `spent` never reset, a run whose allowance is ≥ 1 can no longer reach
        it — that is the point of reading the pool off the live schedule. It stays because
        it is the invariant the arithmetic has to keep: a wave that came to spend more than
        it was handed must stop the run rather than silently overspend.

        Exercised at the one input that still reaches it: an allowance of 0. No operator can
        type that — `config.py:675` clamps `[driver].max_passes` (and `PDCA_MAX_PASSES`) to
        ≥ 1, `cli._flow` clamps `--max-passes` — so this is a `Config` built in-process, and
        the run must open no wave at all, name what it walked away from, and exit 1 (a NO-OP
        pin: the guard predates this change and is asserted here so re-sizing it cannot
        quietly turn it into dead code)."""
        self._briefed("810")
        self._arm()
        self.cfg.max_passes = 0

        rc = self._cli(["810"])          # no --max-passes: the CLI's own clamp is not hit

        self.assertEqual(self.waves_driven, [])           # not one wave opened…
        self.assertEqual(self.passes, 0)                  # …and not one pass spent
        self.assertEqual(self._state("810"), state.PLANNED)
        self.assertEqual(rc, 1)
        err = self.err.getvalue()
        self.assertIn("the run's pass budget is spent (0 pass(es) over 0 wave(s))", err)
        self.assertIn("issue_810 [PLANNED] — resume with `pdca flow 810`", err)

    # -- criterion (3): stdout and the exit code cannot disagree -------------------------

    def test_the_single_id_stdout_names_the_adopted_bundle_that_failed_the_run(self) -> None:
        """`pdca flow 500` prints `state<TAB>path` — the documented machine contract — and
        exits on the WHOLE results map, adopted children included. So a run whose adopted
        601 cannot be built printed `COMPLETE` and exited 1: automation reading stdout saw a
        successful run, and the bundle that failed it was named nowhere it was looking.
        Every bundle the map answers for is now printed in that one shape."""
        self._briefed("500")
        self._arm({"500": ["601", "602"]}, bodies={"500": [_CHILD_ONE, _SIBLING_TWO]})
        self._build_fails("601")

        rc = self._cli(["500"])

        self.assertEqual(rc, 1)                                # the run did NOT succeed…
        self.assertEqual(self._state("601"), state.PLANNED)
        self.assertEqual(self._state("602"), state.COMPLETE)
        # …and stdout says so, in the one shape a caller already parses: the named id first
        # (`tests/test_flow_entrypoint_parity.py:414` pins that a caller piping `cut -f2`
        # still reads a bundle path off line 1), then what the run did to the rest.
        self.assertEqual(self._stdout(), [self._line("500", state.COMPLETE),
                                          self._line("601", state.PLANNED),
                                          self._line("602", state.COMPLETE)])

    def test_a_recovery_run_never_reports_an_earlier_runs_success_as_its_own(self) -> None:
        """The sharpest form of the same disagreement, and the one recovery creates: the
        named id's `COMPLETE` was written by an EARLIER run. `pdca flow 500` on a stranded
        split whose child 601 cannot be built exits 1 while the only thing stdout had to say
        was a success this run did not deliver and did not even perform. The adopted bundles
        are now on stdout too, so no caller can read the machine contract of a failed
        recovery as a completed one."""
        self._strand_a_split(ids=("601", "602"), bodies=[_CHILD_ONE, _SIBLING_TWO])
        self._arm()
        self._build_fails("601")

        rc = self._cli(["500"])

        self.assertEqual(rc, 1)
        self.assertEqual(self._state("500"), state.COMPLETE)   # the earlier run's verdict
        self.assertEqual(self._state("601"), state.PLANNED)    # …this run's actual outcome
        self.assertEqual(self._stdout(), [self._line("500", state.COMPLETE),
                                          self._line("601", state.PLANNED),
                                          self._line("602", state.COMPLETE)])

    def test_stdout_names_an_adopted_child_left_waiting_for_the_human(self) -> None:
        """The disagreement rc alone cannot catch, which is why the report is not gated on
        one. A single-id run counts AWAITING_SIGNOFF as a successful end (#468), so a
        recovery whose adopted 601 halts for the human exits **0** — and a stdout that only
        printed the named id would say `COMPLETE`, about a bundle an earlier run finished,
        while the work this run actually did sits waiting for a sign-off nothing named. The
        child is printed in the same shape, §6 listing included, so the operator reads what
        is waiting for them off the same two fields."""
        self._strand_a_split(ids=("601", "602"), bodies=[_CHILD_ONE, _SIBLING_TWO])
        self._arm(walk_away="601")

        rc = self._cli(["500"], max_passes=1)

        self.assertEqual(rc, 0)                                # …a successful end, and yet
        self.assertEqual(self._state("601"), state.AWAITING_SIGNOFF)
        self.assertEqual(self._state("602"), state.COMPLETE)
        printed = self._stdout()
        self.assertEqual(printed[0], self._line("500", state.COMPLETE))
        self.assertIn(self._line("601", state.AWAITING_SIGNOFF), printed)
        self.assertIn(self._line("602", state.COMPLETE), printed)
        # The §6 items are the whole point of that line: the halted bundle is halted ON
        # them, and the named id's own AWAITING_SIGNOFF listing has always printed them.
        open_items = signoff.open_needs_human(self.cfg.bundle("601") / "SUMMARY.md")
        self.assertTrue(open_items)
        for it in open_items:
            self.assertIn(f"    {it}", printed)

    def test_a_single_id_run_that_adopts_nothing_still_prints_exactly_one_line(self) -> None:
        """The report grows only where the run does. `flow_ids` answers for exactly the ids
        it was given (#468), so a second entry in the map can only be an adopted child — and
        an ordinary `pdca flow <id>` therefore prints the single `state<TAB>path` line it
        always has (`tests/test_flow_entrypoint_parity.py` pins that shape), with nothing
        new for a caller to parse."""
        self._briefed("810")
        self._arm()

        rc = self._cli(["810"])

        self.assertEqual(rc, 0)
        self.assertEqual(self._state("810"), state.COMPLETE)
        self.assertEqual(self._stdout(), [self._line("810", state.COMPLETE)])


if __name__ == "__main__":
    unittest.main()
