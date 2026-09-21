"""`split --accept`'s closing line must not promise what a live run cannot guarantee, and
every child a split produces must be either driven or NAMED by the time the run that could
have reached it ends (#566).

Before this, `cli._split --accept` always closed with `"<parent> marked split; run <prog>
flow <ids> to drive the children"` — even while a live `pdca flow` run holds that very
parent (its own Plan/sign-off session, or another shell entirely) and is about to adopt
those children itself (#469/#473, `flow.py:1546-1548`, `:1626-1627`). And when a live run
does NOT reach a split parent's children — a split accepted from another shell on a
bundle the run has already walked away from un-terminal — nothing at the end of the run
named them at all: they sat PLANNED with no line pointing at them.

Every drive here goes through `cli._flow` / `cli._split`, never a hand-picked `flow.*`
call — the fixture shape of `tests/test_flow_adopt_split.py:43-63` and its mid-run
stand-in leaf (`:190-216`), copied, never imported. Where a run has to be held so a
genuinely separate process can act against it while it still holds a bundle, the second
process is REAL (`_spawn`) — the fixture shape of `tests/test_flow_single_driver.py`
(copied, never imported): a claim could be per-process, and an in-process stand-in would
pass the "another shell" case for the wrong reason. Every wait is bounded, so a red leg
fails instead of hanging C4.

Modules are imported, never new symbols (`from pdca_harness import cli, flow, split,
state`): the C4 red leg reverts this child's production hunks and keeps this file, and a
symbol only this patch adds would fail to import there (PDCA-UNVERIFIABLE, not red).
`drive_claim` is reached only as `cli.drive_claim` / `flow.drive_claim` for the same
reason — this child edits it too, so a bare module-level `from pdca_harness import
drive_claim` is avoided even though the module itself predates this child (#565).

    cd template && PYTHONPATH=src python3 -m unittest tests.test_split_hint_live_run
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pdca_harness import cli, flow, split, state
from pdca_harness.config import Config, LeafConfig

#: The package under test, for the child process (`template/src`).
_SRC = Path(__file__).resolve().parents[1] / "src"
#: Upper bound on every wait in this file — a red leg must fail, never hang the C4 gate.
_WAIT = 60.0


def _stub_config(root: Path) -> Config:
    """All six leaves stubbed, gates empty — the fixture shape of
    `tests/test_flow_adopt_split.py:43-63`, including the hermetic toy checkout inside the
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


def _briefed(cfg: Config, iid: str, *extra: str) -> Path:
    d = cfg.bundle(iid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "brief.md").write_text(_brief(f"slice-{iid}", *extra), encoding="utf-8")
    return d


def _args(ids: list[str], *, csv: str | None = None) -> SimpleNamespace:
    """The `pdca flow …` argv as `cli._flow` receives it (`--no-publish --no-act`)."""
    return SimpleNamespace(issue_ids=ids, from_csv=csv, from_briefs=None, no_publish=True,
                           no_act=True, by="", lanes=None, max_passes=None)


def _wait_for(path: Path, proc: subprocess.Popen | None = None) -> bool:
    deadline = time.monotonic() + _WAIT
    while time.monotonic() < deadline:
        if path.exists():
            return True
        if proc is not None and proc.poll() is not None:
            return False
        time.sleep(0.02)
    return False


def _quiet_plan(cfg: Config, csv: str | None = None, ids: list[str] | None = None) -> None:
    """A Plan session (pre-pass, or a CSV batch's) that briefs nothing — the child process's
    bundles are already briefed on disk before it is spawned."""


def _child_main(argv: list[str]) -> int:
    """Entry point of the child process (`_spawn`): `<root> <pause> <ids…>` runs `pdca flow
    <ids…>` through `cli._flow`, held right before its FIRST wave drives — a stand-in
    `flow._drive_wave` drops `<pause>/ready` and waits, bounded, for `<pause>/go` — so the
    named bundle is claimed and UNTOUCHED at the pause: exactly the moment `pdca split
    --accept` from "another shell" must still see it as held (#566)."""
    root, pause = Path(argv[0]), Path(argv[1])
    ids = argv[2:]
    cfg = _stub_config(root)
    leaves_do_plan_batch_patch = mock.patch(
        "pdca_harness.leaves.do_plan_batch", _quiet_plan)
    leaves_do_plan_batch_patch.start()
    real_drive_wave = flow._drive_wave

    def paused_drive_wave(cfg: Config, wave: list[Path], **kw):
        (pause / "ready").write_text("1", encoding="utf-8")
        _wait_for(pause / "go")
        return real_drive_wave(cfg, wave, **kw)

    flow._drive_wave = paused_drive_wave
    try:
        return cli._flow(cfg, _args(ids))
    finally:
        flow._drive_wave = real_drive_wave
        leaves_do_plan_batch_patch.stop()


#: The child's bootstrap: load THIS file by path and call `_child_main`.
_BOOT = ("import importlib.util, sys\n"
         "spec = importlib.util.spec_from_file_location('split_hint_live_run_child', "
         "sys.argv[1])\n"
         "mod = importlib.util.module_from_spec(spec)\n"
         "spec.loader.exec_module(mod)\n"
         "sys.exit(mod._child_main(sys.argv[2:]))\n")


class SplitHintLiveRun(unittest.TestCase):
    def setUp(self) -> None:
        # Hermetic environment: nothing the gate (or an enclosing flow) exported under
        # PDCA_* may reach this process or a spawned child.
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in [k for k in os.environ if k.startswith("PDCA_")]:
            del os.environ[key]
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = _stub_config(self.tmp / "instance")
        self.cfg.bundle_root.mkdir(parents=True)
        self._orig_do_plan_batch = __import__(
            "pdca_harness.leaves", fromlist=["do_plan_batch"]).do_plan_batch
        self._orig_run_signoff_batch = __import__(
            "pdca_harness.leaves", fromlist=["run_signoff_batch"]).run_signoff_batch
        self._orig_build_all = flow._build_all
        self.addCleanup(self._restore)
        self.err = io.StringIO()
        self.out = io.StringIO()

    def _restore(self) -> None:
        import pdca_harness.leaves as leaves
        leaves.do_plan_batch = self._orig_do_plan_batch
        leaves.run_signoff_batch = self._orig_run_signoff_batch
        flow._build_all = self._orig_build_all

    # -- running things --------------------------------------------------------------

    def _cli(self, ids: list[str], *, csv: str | None = None) -> int:
        """`pdca flow <ids…>` (or `--from-csv`) in THIS process — a fresh capture per call."""
        self.err, self.out = io.StringIO(), io.StringIO()
        with redirect_stderr(self.err), redirect_stdout(self.out):
            return cli._flow(self.cfg, _args(ids, csv=csv))

    def _write_proposal(self, iid: str, kids: list[str]) -> None:
        d = self.cfg.bundle(iid)
        bodies = [_brief(f"child-{k}") for k in kids]
        (d / split.PROPOSAL).write_text(_proposal(*bodies), encoding="utf-8")

    def _accept(self, iid: str, kids: list[str]) -> tuple[int, str]:
        """`pdca split <iid> --accept --ids <kids>` through `cli._split`, captured on its
        own — this call's own stderr, whatever else is being redirected around it."""
        buf = io.StringIO()
        with redirect_stderr(buf), redirect_stdout(io.StringIO()):
            rc = cli._split(self.cfg, SimpleNamespace(
                issue_id=iid, accept=True, ids=",".join(kids)))
        return rc, buf.getvalue()

    def _spawn(self, *argv: str, tag: str) -> subprocess.Popen:
        """A second process running `_child_main(argv)`, as a separate shell would: no
        `PDCA_*` inherited. Output goes to files (a paused child must never block on a full
        pipe); it is killed at teardown if still alive."""
        out = (self.tmp / f"{tag}.out").open("w")
        err = (self.tmp / f"{tag}.err").open("w")
        env = {k: v for k, v in os.environ.items() if not k.startswith("PDCA_")}
        env["PYTHONPATH"] = str(_SRC)
        proc = subprocess.Popen(
            [sys.executable, "-c", _BOOT, str(Path(__file__).resolve()), *argv],
            stdout=out, stderr=err, env=env, cwd=str(self.tmp))

        def reap() -> None:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=_WAIT)
            finally:
                out.close()
                err.close()

        self.addCleanup(reap)
        return proc

    def _state(self, iid: str) -> str:
        return state.state(self.cfg.bundle(iid))

    # -- wording helpers, built the same way `cli._split` builds them ------------------

    @staticmethod
    def _old_line(parent: str, kids: list[str]) -> str:
        prog = cli._prog()
        return f"{parent} marked split; run `{prog} flow {' '.join(kids)}` to drive the children"

    @staticmethod
    def _conditional_line(parent: str, kids: list[str]) -> str:
        prog = cli._prog()
        ids_str = " ".join(kids)
        return (f"{parent} marked split; a running `{prog} flow` holds {parent} and drives "
                f"its children ({ids_str}) if it reaches them — it lists any it did not "
                f"drive when it ends. After that run ends, drive any left with `{prog} flow "
                f"{ids_str}`")

    # -- (ii) baseline: no live run anywhere ------------------------------------------

    def test_standalone_accept_prints_todays_line_unconditionally(self) -> None:
        """No `pdca flow` process is alive at all: byte-identical to today's line."""
        _briefed(self.cfg, "550")
        self._write_proposal("550", ["551", "552"])
        rc, err = self._accept("550", ["551", "552"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(err.rstrip("\n").splitlines()[-1],
                         self._old_line("issue_550", ["551", "552"]))

    # -- (i)(a) in reach of a live run: its own Plan session --------------------------

    def test_in_run_accept_from_the_runs_own_plan_session_is_conditional(self) -> None:
        """The literal reproduction: bundle 500's stand-in Plan leaf calls `cli._split`
        while `cli._flow(["500"])` still holds 500's claim — the line must be (i)'s
        conditional one, never today's unconditional instruction."""
        import pdca_harness.leaves as leaves
        captured: dict[str, str] = {}

        def splitting_plan(cfg: Config, csv: str | None = None,
                           ids: list[str] | None = None) -> None:
            _briefed(cfg, "500")
            self._write_proposal("500", ["601", "602"])
            rc, err = self._accept("500", ["601", "602"])
            captured["rc"], captured["err"] = str(rc), err

        leaves.do_plan_batch = splitting_plan
        rc = self._cli(["500"])
        self.assertEqual(captured.get("rc"), "0", captured.get("err", ""))
        self.assertIn(self._conditional_line("issue_500", ["601", "602"]), captured["err"])
        self.assertNotIn(self._old_line("issue_500", ["601", "602"]), captured["err"])
        # The same run then drives the children it just adopted (#469/#473) — not this
        # brief's concern, but a crash here would say the reproduction was staged wrong.
        self.assertIn(self._state("601"), (state.COMPLETE, state.AWAITING_SIGNOFF))

    # -- (i)(a) in reach of a live run: from another shell entirely --------------------

    def test_accept_from_another_shell_while_a_live_run_holds_the_parent_is_conditional(
            self) -> None:
        """Run A (`pdca flow 500`) is a REAL second process, paused before its first build
        — 500 is claimed and untouched. From THIS process, `pdca split 500 --accept` must
        print (i)'s conditional line, never today's unconditional one, and must not be
        refused (#566's own invariant: the peek that answers this must never make A's own
        claim — or a third run's — see 500 as held by nobody)."""
        _briefed(self.cfg, "500")
        pause = self.tmp / "A-pause"
        pause.mkdir()
        proc = self._spawn(str(self.cfg.root), str(pause), "500", tag="A")
        if not _wait_for(pause / "ready", proc):
            self.fail(f"run A never reached its pause: rc={proc.poll()}\n"
                      + (self.tmp / "A.err").read_text(encoding="utf-8"))

        self._write_proposal("500", ["601", "602"])
        rc, err = self._accept("500", ["601", "602"])
        self.assertEqual(rc, 0, err)
        self.assertIn(self._conditional_line("issue_500", ["601", "602"]), err)
        self.assertNotIn(self._old_line("issue_500", ["601", "602"]), err)

        (pause / "go").write_text("1", encoding="utf-8")
        rc_a = proc.wait(timeout=_WAIT)
        self.assertIsNotNone(rc_a)  # the run ended — did not hang on the split beneath it

    # -- (ii) otherwise: after the run that held the parent has ended ------------------

    def test_accept_after_the_run_has_ended_prints_todays_line(self) -> None:
        """560's sign-off is never answered, so `cli._flow(["560"])` walks away from it
        un-terminal (#260) but still returns — and with it, `drive_claim.run`'s scope
        releases every claim it held, 560's included. An accept run AFTER that call
        returns must print exactly today's line."""
        import pdca_harness.leaves as leaves
        real_signoff_batch = leaves.run_signoff_batch

        def walk_away(cfg: Config, bundles: list[Path]) -> None:
            rest = [d for d in bundles if d.name != "issue_560"]
            if rest:
                real_signoff_batch(cfg, rest)

        leaves.run_signoff_batch = walk_away
        _briefed(self.cfg, "560")
        self._cli(["560"])
        self.assertEqual(self._state("560"), state.AWAITING_SIGNOFF)

        self._write_proposal("560", ["561", "562"])
        rc, err = self._accept("560", ["561", "562"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(err.rstrip("\n").splitlines()[-1],
                         self._old_line("issue_560", ["561", "562"]))

    # -- (ii) otherwise: a named id the live run already let go ------------------------

    def _discontinued(self, iid: str) -> Path:
        """A bundle closed OUTSIDE any split — an ordinary sign-off `discontinue`, which
        (unlike `split.accept`) writes no `close-disposition` marker, so a LATER split
        accept on it is not refused as "already marked" (`split.py:314-320`) — this test's
        release path (`flow.py:1904-1910`) needs a terminal, non-split bundle a fresh
        proposal can still target."""
        import pdca_harness.leaves as leaves
        d = _briefed(self.cfg, iid)
        (d / leaves.SIGNOFF_DECISION).write_text("discontinue\nnot needed\n",
                                                 encoding="utf-8")
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            flow._drive_wave(self.cfg, [d], by="t", today="2026-09-20", max_passes=2)
        self.assertEqual(state.state(d), state.DISCONTINUED)
        return d

    def test_accept_on_a_named_id_the_run_already_let_go_prints_todays_line(self) -> None:
        """`cli._flow(["999", "570"])`: 999 is ALREADY terminal (discontinued) when the run
        starts, so `flow_ids` releases its claim immediately (`flow.py:1904-1910`) while
        570 is still being driven. A split accepted on 999 — while the run is still very
        much alive, driving 570 — must print today's line: the run being alive is not what
        "held" means, only a bundle it still holds is."""
        _briefed(self.cfg, "570")
        self._discontinued("999")
        checked: dict[str, str] = {}
        real_build_all = flow._build_all

        def stand_in_build_all(cfg: Config, wave: list[Path]) -> None:
            if not checked and {d.name for d in wave} == {"issue_570"}:
                checked["done"] = "1"
                self._write_proposal("999", ["991", "992"])
                rc, err = self._accept("999", ["991", "992"])
                checked["rc"], checked["err"] = str(rc), err
            real_build_all(cfg, wave)

        flow._build_all = stand_in_build_all
        self._cli(["999", "570"])
        self.assertIn("done", checked)
        self.assertEqual(checked.get("rc"), "0", checked.get("err", ""))
        self.assertEqual(checked["err"].rstrip("\n").splitlines()[-1],
                         self._old_line("issue_999", ["991", "992"]))

    # -- (i)(b) a live CSV batch has not yet swept -------------------------------------

    def test_csv_batch_not_yet_swept_is_conditional(self) -> None:
        """A `pdca flow --from-csv` batch's own Plan session runs a `split --accept` on a
        bundle nothing has claimed yet (the sweep has not run) — the line must still be
        (i)'s conditional one, since the sweep about to run reaches EVERY in-flight
        bundle, this freshly split one included (`flow.py:1678`)."""
        import pdca_harness.leaves as leaves
        _briefed(self.cfg, "500")
        captured: dict[str, str] = {}

        def stand_in_plan_batch(cfg: Config, csv: str | None = None,
                                ids: list[str] | None = None) -> None:
            self._write_proposal("500", ["601", "602"])
            rc, err = self._accept("500", ["601", "602"])
            captured["rc"], captured["err"] = str(rc), err

        leaves.do_plan_batch = stand_in_plan_batch
        self._cli([], csv="tracker.csv")
        self.assertEqual(captured.get("rc"), "0", captured.get("err", ""))
        self.assertIn(self._conditional_line("issue_500", ["601", "602"]), captured["err"])
        self.assertNotIn(self._old_line("issue_500", ["601", "602"]), captured["err"])

    # -- (iii) the end-of-run report: the brief's own probe -----------------------------

    def test_end_of_run_report_names_split_children_the_run_never_reached(self) -> None:
        """`pdca flow 7 8`, 8 `Conflicts with: 7`; nobody answers 7's sign-off. While A
        drives 8, a split of 7 is accepted (the line is (i)'s conditional one, since A
        still holds 7 — asserted here too). When A ends it must name 7's children with
        `pdca flow 701 702` — the RED case on the base: A ends with 701/702 PLANNED and no
        line naming them."""
        import pdca_harness.leaves as leaves
        real_signoff_batch = leaves.run_signoff_batch

        def walk_away(cfg: Config, bundles: list[Path]) -> None:
            rest = [d for d in bundles if d.name != "issue_7"]
            if rest:
                real_signoff_batch(cfg, rest)

        leaves.run_signoff_batch = walk_away
        _briefed(self.cfg, "7")
        _briefed(self.cfg, "8", "- **Conflicts with:** 7")
        captured: dict[str, str] = {}
        real_build_all = flow._build_all

        def stand_in_build_all(cfg: Config, wave: list[Path]) -> None:
            if "done" not in captured and {d.name for d in wave} == {"issue_8"}:
                captured["done"] = "1"
                self._write_proposal("7", ["701", "702"])
                rc, err = self._accept("7", ["701", "702"])
                captured["rc"], captured["err"] = str(rc), err
            real_build_all(cfg, wave)

        flow._build_all = stand_in_build_all
        self._cli(["7", "8"])

        self.assertIn("done", captured)
        self.assertEqual(captured.get("rc"), "0", captured.get("err", ""))
        self.assertIn(self._conditional_line("issue_7", ["701", "702"]), captured["err"])

        end_of_run = self.err.getvalue()
        self.assertIn(
            "flow: issue_7 split; this run did not drive all of its children — 701, 702 "
            "left in-flight; drive them with `pdca flow 701 702`", end_of_run)
        self.assertEqual(self._state("701"), state.PLANNED)
        self.assertEqual(self._state("702"), state.PLANNED)

    # -- (iii) the end-of-run report: FINISHED children are not "left in-flight" --------

    def _drive_500_split_to_completion(self) -> None:
        """Run A: `pdca flow 500`, whose stand-in Plan leaf accepts a split of 500 into
        601/602. The run adopts both children and drives the whole brood to COMPLETE
        (#469/#473) — so afterwards 500 is a parent TERMINAL ON A SPLIT whose children are
        finished work, which is the fixture the two tests below need and no earlier test in
        this file builds."""
        import pdca_harness.leaves as leaves

        def splitting_plan(cfg: Config, csv: str | None = None,
                           ids: list[str] | None = None) -> None:
            _briefed(cfg, "500")
            self._write_proposal("500", ["601", "602"])
            self._accept("500", ["601", "602"])

        leaves.do_plan_batch = splitting_plan
        try:
            self._cli(["500"])
        finally:
            leaves.do_plan_batch = self._orig_do_plan_batch
        for iid in ("500", "601", "602"):
            self.assertEqual(self._state(iid), state.COMPLETE,
                             f"run A left {iid} at {self._state(iid)}: "
                             f"{self.err.getvalue()}")

    def test_end_of_run_report_is_silent_when_every_split_child_is_finished(self) -> None:
        """Brief (iii)'s "no such children → no line", on the shape that makes it bite: a
        split parent whose whole brood is already DONE.

        Run B names that finished parent again — `pdca flow 500 8` — so 500 enters as an
        adoption SEED (`flow.py:2033-2053`) and its COMPLETE children are dropped rather
        than adopted (`_adoptable`, `flow.py:1064-1076`). Nothing under 500 is in flight, so
        B must end without a word about it: a `pdca flow 601 602` here would point the
        operator at work that is already finished, which is the opposite of the invariant —
        it is about children NOTHING will drive, not children nothing needs to."""
        self._drive_500_split_to_completion()
        _briefed(self.cfg, "8")
        self._cli(["500", "8"])
        end_of_run = self.err.getvalue()
        self.assertEqual(self._state("8"), state.COMPLETE, end_of_run)   # B really ran
        self.assertNotIn("did not drive all of its children", end_of_run)
        self.assertNotIn("left in-flight", end_of_run)

    def _split_601_mid_run(self, confirm: bool) -> dict[str, str]:
        """Run B — `pdca flow 500 8` — with a stand-in build that splits the finished child
        601 into 801/802 while B drives 8: AFTER B's adoption pre-pass has already read 500
        (`flow.py:1652-1655`), so nothing in B will ever adopt them. ``confirm`` then drives
        601's own close through (`_drive_wave`, the close fast path) so it ends the run
        COMPLETE **on a split** — terminal AND marked — rather than merely marked (the
        accept archives the parent's SUMMARY, `split.py:942-952`, so a fresh split parent
        is BUILT, not terminal, until something drives that close)."""
        _briefed(self.cfg, "8")
        captured: dict[str, str] = {}
        real_build_all = flow._build_all

        def stand_in_build_all(cfg: Config, wave: list[Path]) -> None:
            if "done" not in captured and {d.name for d in wave} == {"issue_8"}:
                captured["done"] = "1"
                self._write_proposal("601", ["801", "802"])
                rc, err = self._accept("601", ["801", "802"])
                captured["rc"], captured["err"] = str(rc), err
                if confirm:
                    flow._drive_wave(cfg, [cfg.bundle("601")], by="t", today="2026-09-20",
                                     max_passes=2)
            real_build_all(cfg, wave)

        flow._build_all = stand_in_build_all
        self._cli(["500", "8"])
        self.assertIn("done", captured)
        self.assertEqual(captured.get("rc"), "0", captured.get("err", ""))
        self.assertEqual(self._state("801"), state.PLANNED)
        self.assertEqual(self._state("802"), state.PLANNED)
        return captured

    def test_end_of_run_report_walks_through_a_split_child_to_its_own_children(self) -> None:
        """The other half of the same decision: skipping a child must not cost the walk THROUGH
        it. 601 is split mid-run, so at the end 500's brood is 601 (split, its own close not
        driven yet) and 602 (COMPLETE). B must name 801/802 — reachable only through 601 —
        and say nothing about 602, which is finished work, not work B stranded."""
        self._drive_500_split_to_completion()
        self._split_601_mid_run(confirm=False)
        end_of_run = self.err.getvalue()
        self.assertIn(
            "flow: issue_500 split; this run did not drive all of its children — 801, 802 "
            "left in-flight; drive them with `pdca flow 801 802`", end_of_run)
        # 602 is COMPLETE, so it is NOT in that list — naming it would send `pdca flow` at
        # finished work; and 601 is walked THROUGH rather than named.
        named = end_of_run.split("did not drive all of its children")[1]
        self.assertNotIn("601", named)
        self.assertNotIn("602", named)

    def test_end_of_run_report_walks_through_a_child_terminal_on_a_split_of_its_own(
            self) -> None:
        """The same walk, with 601 driven all the way to COMPLETE **on its own split** — so
        it is terminal AND split-marked at once, exactly `_adoptable`'s "itself terminal on a
        split; examining it for children" case (`flow.py:1064-1076`).

        This is the order the two skips must be in. A split parent is terminal BY DESIGN once
        its close is confirmed, so testing "is it finished?" before "is it a split?" would cut
        the walk off at 601 and strand 801/802 in the silence the whole report exists to end —
        the "don't fix it by gating the whole walk on non-terminal" trap. 801/802 must still be
        named, and 602 (finished, not split) must still not be."""
        self._drive_500_split_to_completion()
        self._split_601_mid_run(confirm=True)
        self.assertEqual(self._state("601"), state.COMPLETE)          # terminal…
        self.assertEqual((self.cfg.bundle("601") / state.CLOSE_MARKER).read_text(
            encoding="utf-8").strip(), "split")                       # …and on a split
        end_of_run = self.err.getvalue()
        self.assertIn(
            "flow: issue_500 split; this run did not drive all of its children — 801, 802 "
            "left in-flight; drive them with `pdca flow 801 802`", end_of_run)
        named = end_of_run.split("did not drive all of its children")[1]
        self.assertNotIn("601", named)
        self.assertNotIn("602", named)

    # -- (iv) the peek must never cause a false refusal --------------------------------

    def test_take_retries_past_a_forced_peek_collision_instead_of_a_false_refusal(
            self) -> None:
        """`drive_claim.held` answers `cli._split`'s question by taking and releasing the
        SAME lock `Run.take` takes — there is no peek-without-acquiring primitive. Forced
        here on a fixed schedule (never wall-clock timing): the retry hook itself performs
        the release, so the collision is deterministic. On the base (no retry in `take`)
        this is a FALSE refusal — nobody really holds the bundle."""
        dc = flow.drive_claim
        cfg = self.cfg
        d = _briefed(cfg, "9")

        # The forced collision: a handle that holds the SAME lock `take` is about to
        # attempt, opened directly (mirroring `held`'s own open+lock, never a second lock
        # mechanism) so this test proves the retry against the real OS primitive.
        path = dc._claim_file(cfg, d)  # noqa: SLF001 — the module's own path, not a new one
        path.parent.mkdir(parents=True, exist_ok=True)
        blocker = path.open("a+", encoding="utf-8")
        from pdca_harness import act
        act._lock_exclusive(blocker, wait=False)

        released = {"n": 0}

        def release_on_first_retry(attempt: int) -> None:
            if released["n"] == 0:
                released["n"] += 1
                act._unlock(blocker)
                blocker.close()
            # Any further retry (there should be none) is a real, tiny sleep.
            else:
                time.sleep(0.001)

        run = dc.Run(cfg)
        with mock.patch.object(dc, "_retry_wait", release_on_first_retry):
            refusal = run.take(d)

        self.assertIsNone(refusal, "take() reported a bundle held that nobody really "
                                  f"holds: {refusal}")
        self.assertEqual(released["n"], 1, "the retry hook never ran — take() did not "
                                          "retry the contended lock at all")
        run.close()
