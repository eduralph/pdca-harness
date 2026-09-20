"""One live driver per bundle (#565).

Bundle state is nothing but the files in `results/issue_<id>/` — "nothing is hidden in a
database" — and no drive path took any ownership of a bundle, so two `pdca flow` runs over
overlapping ids wrote the same artifacts with no ordering between them. Act's session lock
(`act.act_session`, `act.py:125-159`) states the rule for Act's one shared resource; these
tests state it for bundles, on every CLI drive path: the ids you name, a CSV batch's
in-flight sweep, and the children a split hands back to the run that caused it.

Every run here goes through the CLI entry point (`cli._flow`), with all six leaves stubbed
and gates empty — the fixture shape of `tests/test_flow_adopt_split.py:43-63` (copied, never
imported). Where a run has to be held mid-drive so a second one can be tried against it, a
stand-in builder waits on a file before handing back to the PRODUCTION stub leaf, and the
competing driver is a REAL second process (`_spawn`): a claim could be per-process, and then
an in-process thread would pass for the wrong reason. Child processes get
`PYTHONPATH=<checkout>/template/src` and none of the gate's `PDCA_*` environment. Every wait
is bounded, so a red leg fails instead of hanging.

What goes red on the base: a second run drives a bundle a live run holds; a run with no way
to record a claim drives anyway; the sweep and adoption drive bundles another live run holds;
a run tells the operator to `pdca flow N` a bundle it would itself refuse. The "let go"
tests go the other way — each pins ONE release point, and fails if that release is deleted,
because the second run in it would then be refused by a run that had stopped driving the
bundle.

Modules are imported, never new symbols (`from pdca_harness import act, cli, flow, …`):
the C4 red leg reverts the production hunks and keeps this file, and a symbol this patch
adds would fail to import there (`engine/scripts/run-verify.sh` records that as
PDCA-UNVERIFIABLE, not red).

    cd template && PYTHONPATH=src python3 -m unittest tests.test_flow_single_driver
"""

from __future__ import annotations

import errno
import hashlib
import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pdca_harness import act, cli, flow, leaves, split, state, waves
from pdca_harness.config import Config, LeafConfig

#: The package under test, for the child processes (`template/src`, or a rendered `src`).
_SRC = Path(__file__).resolve().parents[1] / "src"
#: The render's ignore rules: `.gitignore.jinja` in the template checkout, `.gitignore` in a
#: rendered instance (the same dual home `test_seed_spill` reads).
_IGNORE = next((Path(__file__).resolve().parents[1] / n
                for n in (".gitignore", ".gitignore.jinja")
                if (Path(__file__).resolve().parents[1] / n).is_file()), None)
#: Where the docs say a run keeps its claims (docs 07 §Lanes), under the process dir.
_CLAIMS = ".drive-claims"
#: Upper bound on every wait in this file — a red leg must fail, never hang the C4 gate.
_WAIT = 60.0


def _stub_config(root: Path) -> Config:
    """All six leaves stubbed, gates empty — the shape of `test_flow_adopt_split.py:43-63`,
    including the hermetic toy checkout inside the tmp root."""
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


#: Two independent children — one wave, both adopted together.
_TWO_FREE = (_brief("child-first"), _brief("child-second"))
#: …and the ordered pair: child-2 declares an edge on its sibling LABEL, which `split.accept`
#: rewrites to the real id, so the two children land in two waves.
_TWO_ORDERED = (_brief("child-first"),
                _brief("child-second", "- **Depends on:** child-1"))


def _args(ids: list[str], *, csv: str | None = None) -> SimpleNamespace:
    """The `pdca flow …` argv as `cli._flow` receives it (`--no-publish --no-act`)."""
    return SimpleNamespace(issue_ids=ids, from_csv=csv, from_briefs=None, no_publish=True,
                           no_act=True, by="", lanes=None, max_passes=None)


def _briefed(cfg: Config, iid: str, *extra: str) -> Path:
    d = cfg.bundle(iid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "brief.md").write_text(_brief(f"slice-{iid}", *extra), encoding="utf-8")
    return d


def _drive_to_complete(cfg: Config, d: Path) -> None:
    """Carry ONE bundle to COMPLETE with production code and no adoption of its own —
    `flow._drive_wave` is the per-wave driver and has never looked for children (the shape
    `test_flow_adopt_recovery.py:202-208` uses to build a fixture without the mechanism
    under test)."""
    with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
        flow._drive_wave(cfg, [d], by="t", today="2026-09-20", max_passes=2)


def _split_now(cfg: Config, parent_id: str, ids: list[str],
               bodies: tuple[str, ...] = _TWO_FREE, *, strand: bool = False) -> Path:
    """Decompose a parent through the PRODUCTION `split.accept` (`split.py:857`): the child
    bundles, each child's lineage record, the parent's `children` record and its
    `close-disposition = split` marker are byte-for-byte what `pdca split --accept` leaves.

    `strand=True` also closes the parent, leaving the instance exactly as an EARLIER run
    that split it did — terminal on `split`, children PLANNED and undriven — which is what
    makes a later run treat it as an adoption SEED."""
    d = _briefed(cfg, parent_id)
    (d / split.PROPOSAL).write_text(_proposal(*bodies), encoding="utf-8")
    split.accept(d, ids, cfg)
    if strand:
        _drive_to_complete(cfg, d)
    return d


def _terminal(cfg: Config, iid: str) -> Path:
    """A bundle already closed OUTSIDE a split — `pdca flow` skips it as terminal and has
    nothing left to do with it."""
    d = _briefed(cfg, iid)
    _drive_to_complete(cfg, d)
    return d


def _wait_for(path: Path, proc: subprocess.Popen | None = None) -> bool:
    deadline = time.monotonic() + _WAIT
    while time.monotonic() < deadline:
        if path.exists():
            return True
        if proc is not None and proc.poll() is not None:
            return False
        time.sleep(0.02)
    return False


def _pause_here(pause: Path, what: str) -> None:
    """Drop `pause/ready`, then wait (bounded) for `pause/go`: the run is held right here."""
    (pause / "ready").write_text(what, encoding="utf-8")
    _wait_for(pause / "go")


def _pausing_build(pause: Path, on: str | None = None):
    """Install a builder that pauses (`_pause_here`) on its FIRST call — or on the first
    build of bundle `on` — and then runs the PRODUCTION stub builder. Returns the original,
    for the caller to restore. This is how a run is held mid-drive."""
    real = leaves.do_build
    first = [True]

    def build(d: Path, cfg: Config) -> None:
        if first[0] and on in (None, d.name):
            first[0] = False
            _pause_here(pause, d.name)
        real(d, cfg)

    leaves.do_build = build
    return real


def _walking_away(name: str) -> None:
    """Install a sign-off that never offers bundle `name` a session: it stops at
    AWAITING_SIGNOFF, and its wave ends with it named as walked away from — the ordinary
    end of an interactive run whose human did not answer for it."""
    real = leaves.run_signoff_batch

    def signoff(cfg: Config, bundles: list[Path]) -> None:
        rest = [d for d in bundles if d.name != name]
        if rest:
            real(cfg, rest)

    leaves.run_signoff_batch = signoff


def _splitting_signoff(parent: str, kids: list[str], also=None):
    """Install a sign-off that decomposes bundle `parent` instead of accepting it — the
    ordinary way a bundle reaches `close-disposition = split` MID-WAVE, so the splice after
    that wave has children to adopt. `also` runs at the same moment, which is the one window
    in which an already-adopted child of a LATER wave can be made unschedulable."""
    real = leaves.run_signoff_batch
    done = [False]

    def signoff(cfg: Config, bundles: list[Path]) -> None:
        rest = []
        for d in bundles:
            if d.name == f"issue_{parent}" and not done[0]:
                done[0] = True
                (d / split.PROPOSAL).write_text(_proposal(*_TWO_FREE[:len(kids)]),
                                                encoding="utf-8")
                split.accept(d, kids, cfg)
                if also is not None:
                    also(cfg)
            else:
                rest.append(d)
        if rest:
            real(cfg, rest)

    leaves.run_signoff_batch = signoff


def _quiet_plan(cfg: Config, csv: str | None = None, ids: list[str] | None = None) -> None:
    """A Plan session (pre-pass, or a CSV batch's) that briefs nothing."""


def _child_main(argv: list[str]) -> int:
    """Entry point of a child process (`_spawn`):

    * `split <root> <parent> <ids>` — `pdca split <parent> --accept --ids <ids>` through
      `cli._split`;
    * `flow <root> <pause|-> [options…] <ids…>` — `pdca flow <ids…>` through `cli._flow`;
      its Plan pre-pass briefs nothing, and with a pause dir its first build (of
      `--pause-on`, if given) is held;
    * `csv <root> <pause|->` — `pdca flow --from-csv …` the same way.

    Options: `--pause-on=<bundle>` where to hold the run; `--walk-away=<bundle>` a sign-off
    nobody answers; `--split-signoff=<parent>:<child>` a sign-off that decomposes a bundle
    MID-WAVE; `--retract=<id>` re-plans that bundle onto a ghost prerequisite at the same
    moment; `--break-reschedule` makes the levelling itself raise; `--max-passes=N`.
    """
    mode, cfg = argv[0], _stub_config(Path(argv[1]))
    if mode == "split":
        return cli._split(cfg, SimpleNamespace(issue_id=argv[2], accept=True, ids=argv[3]))
    pause = None if argv[2] == "-" else Path(argv[2])
    opts = dict(a[2:].split("=", 1) if "=" in a else (a[2:], "")
                for a in argv[3:] if a.startswith("--"))
    ids = [a for a in argv[3:] if not a.startswith("--")]
    leaves.do_plan_batch = _quiet_plan
    if pause is not None:
        _pausing_build(pause, on=opts.get("pause-on"))
    if "walk-away" in opts:
        _walking_away(opts["walk-away"])
    if "split-signoff" in opts:
        parent, kid = opts["split-signoff"].split(":", 1)
        retract = opts.get("retract")

        def replan(cfg: Config) -> None:
            if retract:
                (cfg.bundle(retract) / "brief.md").write_text(
                    _brief(f"slice-{retract}", "- **Depends on:** GHOST"), encoding="utf-8")

        _splitting_signoff(parent, [kid], also=replan)
    if "break-reschedule" in opts:
        # The one branch `_reschedule` cannot be driven into from the outside: it answers
        # None only when the levelling itself RAISES (`flow.py:1098-1101`). Injected here,
        # in the run's own process, so the production splice really takes that path.
        def boom(cfg, bundles):
            raise RuntimeError("levelling is unavailable")

        waves.partition_schedulable = boom
    args = _args(ids)
    if "max-passes" in opts:
        args.max_passes = int(opts["max-passes"])
    if mode == "csv":
        return cli._flow(cfg, _args([], csv="tracker.csv"))
    return cli._flow(cfg, args)


#: The child's bootstrap: load THIS file by path and call `_child_main` — one fixture,
#: in one module, for both processes.
_BOOT = ("import importlib.util, sys\n"
         "spec = importlib.util.spec_from_file_location('single_driver_child', sys.argv[1])\n"
         "mod = importlib.util.module_from_spec(spec)\n"
         "spec.loader.exec_module(mod)\n"
         "sys.exit(mod._child_main(sys.argv[2:]))\n")


def _fingerprint(root: Path) -> dict[str, tuple]:
    """Every path under `root`, with enough of each file to see ANY write to it."""
    out: dict[str, tuple] = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_dir():
            out[rel] = ("dir",)
        else:
            st = p.stat()
            out[rel] = (st.st_size, st.st_mtime_ns,
                        hashlib.sha256(p.read_bytes()).hexdigest())
    return out


class SingleDriver(unittest.TestCase):
    def setUp(self) -> None:
        # Hermetic environment: nothing the gate (or an enclosing flow) exported under
        # PDCA_* may reach these runs, in this process or in any child.
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in [k for k in os.environ if k.startswith("PDCA_")]:
            del os.environ[key]
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = _stub_config(self.tmp / "instance")
        self.cfg.bundle_root.mkdir(parents=True)
        self._orig = (leaves.do_plan_batch, leaves.do_build, leaves.run_signoff_batch,
                      cli._report_batch)
        self.addCleanup(self._restore)
        self.err = io.StringIO()
        self.out = io.StringIO()

    def _restore(self) -> None:
        (leaves.do_plan_batch, leaves.do_build, leaves.run_signoff_batch,
         cli._report_batch) = self._orig

    # -- running things ------------------------------------------------------------------

    def _cli(self, ids: list[str], *, csv: str | None = None) -> int:
        """`pdca flow <ids…>` (or `pdca flow --from-csv`) in THIS process — a fresh capture
        per call."""
        self.err, self.out = io.StringIO(), io.StringIO()
        with redirect_stderr(self.err), redirect_stdout(self.out):
            return cli._flow(self.cfg, _args(ids, csv=csv))

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

    def _finish(self, proc: subprocess.Popen, tag: str) -> tuple[int, str]:
        rc = proc.wait(timeout=_WAIT)
        return rc, (self.tmp / f"{tag}.err").read_text(encoding="utf-8")

    def _run_child(self, *argv: str, tag: str) -> tuple[int, str]:
        return self._finish(self._spawn(*argv, tag=tag), tag)

    def _hold(self, mode: str, *ids: str, tag: str) -> tuple[subprocess.Popen, Path]:
        """Run A in ANOTHER process — `mode` is a `_child_main` flow mode, `ids` its ids and
        options — and return once it is paused inside a build, holding whatever it holds.
        Writing `<pause>/go` lets it finish."""
        pause = self.tmp / f"{tag}-pause"
        pause.mkdir()
        proc = self._spawn(mode, str(self.cfg.root), str(pause), *ids, tag=tag)
        if not _wait_for(pause / "ready", proc):
            self.fail(f"run {tag} never reached its pause: rc={proc.poll()}\n"
                      + (self.tmp / f"{tag}.err").read_text(encoding="utf-8"))
        return proc, pause

    def _state(self, iid: str) -> str:
        return state.state(self.cfg.bundle(iid))

    def _broken_claims_dir(self) -> None:
        """A regular file where the run keeps its claims: no claim can be opened."""
        self.cfg.process_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.process_dir / _CLAIMS).write_text("not a directory\n", encoding="utf-8")

    @staticmethod
    def _held_lines(text: str, name: str) -> list[str]:
        return [ln for ln in text.splitlines() if name in ln and "held by another" in ln]

    @staticmethod
    def _unclaimable_lines(text: str, name: str) -> list[str]:
        return [ln for ln in text.splitlines() if name in ln and "could not record" in ln]

    def _assert_not_refused(self, iid: str, err: str) -> None:
        """This run reached the bundle at all: nothing refused it, and nothing named it as
        another run's. The assertion every "let go" test turns on."""
        self.assertEqual(self._held_lines(err, f"issue_{iid}"), [],
                         f"issue_{iid} is still held by the run that stopped driving "
                         f"it:\n{err}")
        self.assertNotIn("refusing to start a second driver", err)

    # -- (i) a second driver over a bundle a live run holds is refused ---------------------

    def test_a_second_flow_over_a_bundle_a_live_run_holds_is_refused(self) -> None:
        """Run A (`pdca flow 7`) is paused mid-drive; run B — `pdca flow 7`, a separate
        process — must exit non-zero, name issue_7 as held by another run, and change
        nothing under results/. On the base B simply drives 7 to COMPLETE under A.

        Then (iii), the normal-return half: once A has returned, a new run over 7 is not
        refused."""
        _briefed(self.cfg, "7")
        pause = self.tmp / "pause"
        pause.mkdir()
        orig_build = _pausing_build(pause)
        self.addCleanup(setattr, leaves, "do_build", orig_build)
        result: dict[str, int] = {}
        a = threading.Thread(target=lambda: result.update(rc=self._cli(["7"])),
                             name="run-A", daemon=True)
        a.start()

        def release_a() -> None:
            (pause / "go").write_text("", encoding="utf-8")
            a.join(timeout=_WAIT)

        self.addCleanup(release_a)
        self.assertTrue(_wait_for(pause / "ready"), "run A never reached its build")

        before = _fingerprint(self.cfg.bundle_root)
        rc_b, err_b = self._run_child("flow", str(self.cfg.root), "-", "7", tag="B")

        self.assertNotEqual(rc_b, 0, f"run B drove a bundle run A holds:\n{err_b}")
        self.assertTrue(self._held_lines(err_b, "issue_7"),
                        f"run B did not name issue_7 as held by another run:\n{err_b}")
        self.assertEqual(_fingerprint(self.cfg.bundle_root), before,
                         "run B changed results/ while run A held the bundle")

        release_a()
        self.assertFalse(a.is_alive(), "run A did not finish")
        self.assertEqual(result.get("rc"), 0, self.err.getvalue())
        self.assertEqual(self._state("7"), state.COMPLETE)

        # (iii) A has returned: its claim went with it.
        rc_c, err_c = self._run_child("flow", str(self.cfg.root), "-", "7", tag="C")
        self.assertEqual(rc_c, 0, err_c)
        self.assertEqual(self._held_lines(err_c, "issue_7"), [], err_c)

    def test_a_named_id_list_is_refused_whole_before_its_plan_pre_pass(self) -> None:
        """`pdca flow 3 7` while another process holds 7: refused before ANY bundle is
        touched — 3's Plan pre-pass (it has no brief yet) never runs, and no bundle
        directory is created for it."""
        _briefed(self.cfg, "7")
        a, _pause = self._hold("flow", "7", tag="A")
        planned: list[list[str]] = []
        leaves.do_plan_batch = lambda cfg, csv=None, ids=None: planned.append(list(ids or []))
        before = _fingerprint(self.cfg.bundle_root)

        rc = self._cli(["3", "7"])

        err = self.err.getvalue()
        self.assertEqual(rc, 1, err)
        self.assertTrue(self._held_lines(err, "issue_7"), err)
        self.assertEqual(planned, [], "the Plan pre-pass ran for a refused run")
        self.assertEqual(_fingerprint(self.cfg.bundle_root), before)
        self.assertIsNone(a.poll(), "run A should still be alive and holding 7")

    # -- (v) fail closed: a claim that cannot be recorded is never driven around -------------

    def test_a_run_that_cannot_open_its_claim_refuses_rather_than_drive_unclaimed(
            self) -> None:
        """No claim file can be opened (a regular file sits where the claims live). A bundle
        driven unclaimed has nothing keeping a second run off it, so `pdca flow 7` refuses —
        non-zero, naming issue_7, nothing under results/ changed. On the base it drives."""
        _briefed(self.cfg, "7")
        self._broken_claims_dir()
        before = _fingerprint(self.cfg.bundle_root)

        rc = self._cli(["7"])

        err = self.err.getvalue()
        self.assertEqual(rc, 1, err)
        self.assertTrue(self._unclaimable_lines(err, "issue_7"), err)
        self.assertEqual(_fingerprint(self.cfg.bundle_root), before)
        self.assertEqual(self._state("7"), state.PLANNED)

    def test_a_run_whose_filesystem_cannot_lock_refuses_rather_than_drive_unclaimed(
            self) -> None:
        """The claim file opens but the lock itself fails for a reason that is NOT another
        holder (ENOLCK — a filesystem without lock support). Same answer: refused, not
        driven unclaimed. Injected on `act._lock_exclusive`, the one cross-platform lock
        primitive the claim shares with Act's session (`act.py:29-62`)."""
        _briefed(self.cfg, "7")
        before = _fingerprint(self.cfg.bundle_root)

        def no_locks(fh, *, wait: bool = True) -> None:
            raise OSError(errno.ENOLCK, "No locks available")

        with mock.patch.object(act, "_lock_exclusive", no_locks):
            rc = self._cli(["7"])

        err = self.err.getvalue()
        self.assertEqual(rc, 1, err)
        self.assertTrue(self._unclaimable_lines(err, "issue_7"), err)
        self.assertIn("No locks available", err)
        self.assertEqual(_fingerprint(self.cfg.bundle_root), before)

    # -- (iii) the claim ends with the run -------------------------------------------------

    def test_a_run_that_raises_leaves_nothing_behind(self) -> None:
        """A run that ends by RAISING (a ^C inside the builder) releases its claim: the next
        run over the same bundle, in another process, drives it."""
        _briefed(self.cfg, "7")

        def interrupted(d: Path, cfg: Config) -> None:
            raise KeyboardInterrupt

        leaves.do_build = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self._cli(["7"])
        self._restore()

        rc, err = self._run_child("flow", str(self.cfg.root), "-", "7", tag="after-raise")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._held_lines(err, "issue_7"), [], err)
        self.assertEqual(self._state("7"), state.COMPLETE)

    @unittest.skipUnless(hasattr(signal, "SIGKILL"), "needs SIGKILL")
    def test_a_killed_run_holds_nothing_and_blocks_nothing(self) -> None:
        """While run A (another process) holds 7, a run here is refused and changes nothing;
        once A is SIGKILLed mid-drive — no cleanup of its own at all — a run here drives 7."""
        _briefed(self.cfg, "7")
        a, _pause = self._hold("flow", "7", tag="A")
        before = _fingerprint(self.cfg.bundle_root)

        rc = self._cli(["7"])

        err = self.err.getvalue()
        self.assertEqual(rc, 1, f"a second driver ran while another process held 7:\n{err}")
        self.assertTrue(self._held_lines(err, "issue_7"), err)
        self.assertEqual(_fingerprint(self.cfg.bundle_root), before)

        a.send_signal(signal.SIGKILL)
        a.wait(timeout=_WAIT)

        rc = self._cli(["7"])

        err = self.err.getvalue()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._held_lines(err, "issue_7"), [], err)
        self.assertEqual(self._state("7"), state.COMPLETE)

    # -- (ii) implicit reach is skipped, not refused ----------------------------------------

    def test_the_csv_sweep_skips_a_bundle_another_live_run_holds(self) -> None:
        """A CSV batch sweeps every in-flight bundle after its Plan session. With 7 held by a
        live run in another process, the sweep names 7, leaves it untouched, and drives the
        rest (8) to COMPLETE."""
        _briefed(self.cfg, "7")
        _briefed(self.cfg, "8")
        a, _pause = self._hold("flow", "7", tag="A")
        leaves.do_plan_batch = _quiet_plan
        seven = self.cfg.bundle("7")
        before = _fingerprint(seven)

        rc = self._cli([], csv="tracker.csv")

        err = self.err.getvalue()
        self.assertTrue([ln for ln in self._held_lines(err, "issue_7") if "NOT driven" in ln],
                        err)
        self.assertEqual(_fingerprint(seven), before, "the sweep drove a bundle A holds")
        self.assertEqual(self._state("8"), state.COMPLETE)
        self.assertNotIn("\t7", self.out.getvalue())       # not in this run's results
        self.assertEqual(rc, 0, err)
        self.assertIsNone(a.poll())

    def test_the_csv_sweep_skips_every_bundle_it_cannot_claim(self) -> None:
        """Fail closed on the implicit path: with no claim file openable, the sweep drives
        NOTHING — each in-flight bundle is named and left as it was, rather than driven with
        nothing keeping a second run off it. On the base both are driven."""
        _briefed(self.cfg, "7")
        _briefed(self.cfg, "8")
        self._broken_claims_dir()
        leaves.do_plan_batch = _quiet_plan
        before = _fingerprint(self.cfg.bundle_root)

        self._cli([], csv="tracker.csv")

        err = self.err.getvalue()
        for name in ("issue_7", "issue_8"):
            self.assertTrue([ln for ln in self._unclaimable_lines(err, name)
                             if "NOT driven" in ln], err)
        self.assertEqual(_fingerprint(self.cfg.bundle_root), before)

    def test_split_adoption_skips_a_child_another_live_run_holds(self) -> None:
        """`pdca flow 500` splits 500 at its Plan pre-pass into 601 and 602; before the
        run gets to adopt them, another process starts driving 601. Adoption names 601 as
        NOT adopted, leaves it alone, and still adopts and drives 602."""
        holder: list[subprocess.Popen] = []

        def plan(cfg: Config, csv: str | None = None, ids: list[str] | None = None) -> None:
            _split_now(cfg, "500", ["601", "602"])
            holder.append(self._hold("flow", "601", tag="A")[0])

        leaves.do_plan_batch = plan

        rc = self._cli(["500"])

        err = self.err.getvalue()
        self.assertEqual(len(holder), 1, err)
        self.assertTrue([ln for ln in self._held_lines(err, "issue_601")
                         if "NOT adopted" in ln], err)
        self.assertEqual(self._state("601"), state.PLANNED, "601 was driven under its holder")
        self.assertEqual(self._state("602"), state.COMPLETE, err)
        self.assertEqual(rc, 0, err)
        self.assertIsNone(holder[0].poll())

    def test_split_adoption_skips_a_child_it_cannot_claim(self) -> None:
        """Fail closed in adoption: the claim on 601 cannot be LOCKED (a non-contention
        failure, ENOLCK); 601 is named as NOT adopted and left PLANNED, and 602 is still
        adopted and driven. On the base 601 is adopted and driven."""
        real = act._lock_exclusive

        def no_lock_for_601(fh, *, wait: bool = True) -> None:
            if Path(fh.name).name.startswith("issue_601-"):
                raise OSError(errno.ENOLCK, "No locks available")
            real(fh, wait=wait)

        leaves.do_plan_batch = lambda cfg, csv=None, ids=None: _split_now(
            cfg, "500", ["601", "602"])

        with mock.patch.object(act, "_lock_exclusive", no_lock_for_601):
            rc = self._cli(["500"])

        err = self.err.getvalue()
        self.assertTrue([ln for ln in self._unclaimable_lines(err, "issue_601")
                         if "NOT adopted" in ln], err)
        self.assertEqual(self._state("601"), state.PLANNED, err)
        self.assertEqual(self._state("602"), state.COMPLETE, err)
        self.assertEqual(rc, 0, err)

    # -- (iv) a run never refuses itself ----------------------------------------------------

    def test_a_run_drives_the_children_it_adopts_and_the_split_it_spawns_is_not_blocked(
            self) -> None:
        """`pdca flow 500`, whose Plan session runs `pdca split 500 --accept --ids 601,602`
        as a REAL child process (as an interactive planner does). That command must not be
        blocked by the claim the run already holds on 500 — it returns promptly, rc 0 — and
        the run then adopts and drives both children without refusing itself anything."""
        spawned: list[tuple[int, str]] = []

        def plan(cfg: Config, csv: str | None = None, ids: list[str] | None = None) -> None:
            d = _briefed(cfg, "500")
            (d / split.PROPOSAL).write_text(_proposal(*_TWO_FREE), encoding="utf-8")
            spawned.append(self._run_child("split", str(cfg.root), "500", "601,602",
                                           tag="accept"))

        leaves.do_plan_batch = plan

        rc = self._cli(["500"])

        err = self.err.getvalue()
        self.assertEqual(len(spawned), 1, err)
        rc_split, err_split = spawned[0]
        self.assertEqual(rc_split, 0, f"the run's own `split --accept` was blocked or "
                                      f"refused:\n{err_split}")
        self.assertEqual(self._state("601"), state.COMPLETE, err)
        self.assertEqual(self._state("602"), state.COMPLETE, err)
        self.assertEqual(self._held_lines(err, "issue_"), [], err)
        self.assertEqual(rc, 0, err)

    def test_a_run_naming_one_bundle_under_two_names_does_not_refuse_itself(self) -> None:
        """`pdca flow 7 9` where `issue_9` is a symlink to `issue_7`: ONE directory, named
        twice. Both names resolve to the same claim, and a run must never be refused by a
        claim it is holding itself — it drives the bundle exactly once."""
        _briefed(self.cfg, "7")
        try:
            (self.cfg.bundle_root / "issue_9").symlink_to(self.cfg.bundle("7"),
                                                          target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("this filesystem has no symlinks")
        leaves.do_plan_batch = _quiet_plan

        rc = self._cli(["7", "9"])

        err = self.err.getvalue()
        self.assertEqual(self._held_lines(err, "issue_"), [],
                         f"the run refused itself over its own claim:\n{err}")
        self.assertNotIn("refusing to start a second driver", err)
        self.assertEqual(self._state("7"), state.COMPLETE, err)
        self.assertEqual(rc, 0, err)

    # -- (vi) a bundle the run decides not to drive is let go at that decision ---------------
    #
    # One test per release point, each shaped the same way: run A is held mid-drive AFTER the
    # decision, and a second run over the bundle A let go must get through. Delete the
    # release and the second run is refused by a run that had already stopped driving it.

    def test_a_named_id_with_no_brief_is_let_go_at_the_skip(self) -> None:
        """Release point: `flow_ids`' "no brief.md, skipped" (`flow.py:1895-1896`). Run A is
        `pdca flow 7 9` and its Plan pre-pass briefs nothing, so A skips 9 and drives only 7.
        With A still inside 7's build, 9 is briefed and driven from here."""
        _briefed(self.cfg, "7")
        a, _pause = self._hold("flow", "7", "9", tag="A")
        self.assertIn("issue_9 — no brief.md, skipped",
                      (self.tmp / "A.err").read_text(encoding="utf-8"))
        _briefed(self.cfg, "9")

        rc = self._cli(["9"])

        err = self.err.getvalue()
        self._assert_not_refused("9", err)
        self.assertEqual(self._state("9"), state.COMPLETE, err)
        self.assertEqual(rc, 0, err)
        self.assertIsNone(a.poll(), "run A should still be alive, driving 7")

    def test_a_named_id_already_terminal_is_let_go_at_the_skip(self) -> None:
        """Release point: `flow_ids`' "already terminal, skipped" (`flow.py:1904-1910`). Run
        A is `pdca flow 7 8` with 8 already closed, so A skips 8 and drives only 7. With A
        still inside 7's build, a run here over 8 is not refused — it reads 8 as terminal,
        which is the answer 8 actually has."""
        _briefed(self.cfg, "7")
        _terminal(self.cfg, "8")
        a, _pause = self._hold("flow", "7", "8", tag="A")
        self.assertIn("issue_8 — already terminal",
                      (self.tmp / "A.err").read_text(encoding="utf-8"))

        self._cli(["8"])

        err = self.err.getvalue()
        self._assert_not_refused("8", err)
        self.assertIn("issue_8 — already terminal", err)
        self.assertIsNone(a.poll(), "run A should still be alive, driving 7")

    def test_a_split_parent_seed_is_let_go_once_its_adoption_pre_pass_is_done(self) -> None:
        """Release point: the adoption SEED, released once the pre-pass over it has run
        (`flow.py:1549-1556`). Run A is `pdca flow 500`, a parent an earlier run left split
        into 601 and 602: A adopts both and drives them, and 500 itself is nothing A drives.
        With A inside 601's build, `pdca flow 500` from here is NOT refused — it reaches
        adoption and is told, per bundle, that the children are another run's."""
        _split_now(self.cfg, "500", ["601", "602"], strand=True)
        a, _pause = self._hold("flow", "--pause-on=issue_601", "500", tag="A")

        self._cli(["500"])

        err = self.err.getvalue()
        self.assertNotIn("refusing to start a second driver", err)
        self.assertIn("issue_500 — already terminal", err,
                      f"the seed is still held after its adoption pre-pass:\n{err}")
        # …and the children really are still A's, which is what makes the release specific
        # to the parent rather than a claim that was never taken.
        self.assertTrue([ln for ln in self._held_lines(err, "issue_601")
                         if "NOT adopted" in ln], err)
        self.assertIsNone(a.poll(), "run A should still be alive, driving 601")

    def test_a_swept_bundle_the_scheduler_holds_is_let_go_while_the_batch_runs(self) -> None:
        """Release point: `flow_batch`'s held sweep (`flow.py:1780-1787`). A CSV batch
        (another process) sweeps 7 and 8, but 8 declares a dependency nothing can satisfy, so
        the scheduler holds it and the batch tells the operator to resolve it and re-run.
        While the batch is still driving 7: `pdca flow 7` is refused, and — once 8's brief is
        fixed — `pdca flow 8` is NOT refused by the batch that stopped driving it."""
        _briefed(self.cfg, "7")
        eight = _briefed(self.cfg, "8", "- **Depends on:** 999")
        a, _pause = self._hold("csv", tag="batch")

        rc = self._cli(["7"])
        self.assertEqual(rc, 1, self.err.getvalue())
        self.assertTrue(self._held_lines(self.err.getvalue(), "issue_7"), self.err.getvalue())

        (eight / "brief.md").write_text(_brief("slice-8"), encoding="utf-8")
        rc = self._cli(["8"])

        err = self.err.getvalue()
        self._assert_not_refused("8", err)
        self.assertEqual(self._state("8"), state.COMPLETE, err)
        self.assertEqual(rc, 0, err)
        self.assertIsNone(a.poll(), "the batch should still be alive, driving 7")
        batch_err = (self.tmp / "batch.err").read_text(encoding="utf-8")
        self.assertIn("issue_8 held this run — unresolved dependency (999)", batch_err)

    def test_a_swept_bundle_is_let_go_before_the_nothing_schedulable_return(self) -> None:
        """The same release, on the branch that returns immediately: EVERY swept bundle is
        held, so `flow_batch` reports "nothing schedulable" and returns. The release has to
        happen before that return, which is only observable from inside the run — so the
        probe runs in `cli._report_batch`, a pass-through spy called with the batch's result
        while the run's claim scope is still open (`cli.py:573-574`, `:634-636`)."""
        _briefed(self.cfg, "8", "- **Depends on:** 999")
        leaves.do_plan_batch = _quiet_plan
        real_report, probe = cli._report_batch, {}

        def report(results):
            # Still inside the run: fix 8's brief and drive it from a SECOND process. A
            # released claim lets that through; a claim held to the end of the run does not.
            (self.cfg.bundle("8") / "brief.md").write_text(_brief("slice-8"),
                                                           encoding="utf-8")
            probe["rc"], probe["err"] = self._run_child(
                "flow", str(self.cfg.root), "-", "8", tag="probe")
            return real_report(results)

        cli._report_batch = report

        self._cli([], csv="tracker.csv")

        self.assertIn("nothing schedulable", self.err.getvalue())
        self._assert_not_refused("8", probe.get("err", ""))
        self.assertEqual(probe.get("rc"), 0, probe.get("err"))
        self.assertEqual(self._state("8"), state.COMPLETE)

    def test_children_a_failed_reschedule_leaves_in_flight_are_let_go(self) -> None:
        """Release point: the splice whose reschedule could not be computed at all
        (`flow.py:1286-1289`, released at `:1323-1325`). Run A is `pdca flow 500 7`; 500 is a split parent, and A's
        levelling raises, so the children are never spliced and A prints `pdca flow 601 602`
        for them. A goes on to drive 7 — and while it is inside 7's build, that very command
        must work from here."""
        _split_now(self.cfg, "500", ["601", "602"], strand=True)
        _briefed(self.cfg, "7")
        a, _pause = self._hold("flow", "--break-reschedule", "--pause-on=issue_7", "500", "7",
                               tag="A")
        self.assertIn("could not be scheduled",
                      (self.tmp / "A.err").read_text(encoding="utf-8"))

        rc = self._cli(["601", "602"])

        err = self.err.getvalue()
        self._assert_not_refused("601", err)
        self._assert_not_refused("602", err)
        self.assertEqual(self._state("601"), state.COMPLETE, err)
        self.assertEqual(self._state("602"), state.COMPLETE, err)
        self.assertEqual(rc, 0, err)
        self.assertIsNone(a.poll(), "run A should still be alive, driving 7")

    def test_an_adopted_child_the_reschedule_holds_is_let_go_while_the_run_goes_on(
            self) -> None:
        """Release point: a child the splice's own reschedule held (`flow.py:1315-1316`, the
        children half). Run A drives 500, already split into 601 and 602; 602's brief
        declares a dependency nothing can satisfy, so the reschedule holds it and A drives
        only 601. While A is inside 601's build: `pdca flow 601` is refused — an adopted
        child is A's — and, once 602's brief is fixed, `pdca flow 602` is NOT refused by the
        run that dropped it."""
        _split_now(self.cfg, "500", ["601", "602"], strand=True)
        six02 = self.cfg.bundle("602") / "brief.md"
        fixed = six02.read_text(encoding="utf-8")
        six02.write_text(fixed + "- **Depends on:** 999\n", encoding="utf-8")
        a, pause = self._hold("flow", "500", tag="A")
        self.assertEqual((pause / "ready").read_text(encoding="utf-8"), "issue_601")

        rc = self._cli(["601"])
        self.assertEqual(rc, 1, self.err.getvalue())
        self.assertTrue(self._held_lines(self.err.getvalue(), "issue_601"),
                        self.err.getvalue())

        six02.write_text(fixed, encoding="utf-8")
        rc = self._cli(["602"])

        err = self.err.getvalue()
        self._assert_not_refused("602", err)
        self.assertEqual(self._state("602"), state.COMPLETE, err)
        self.assertEqual(rc, 0, err)
        self.assertIsNone(a.poll(), "run A should still be alive, driving 601")

    def test_a_child_a_later_reschedule_retracts_is_let_go(self) -> None:
        """Release point: a child adopted EARLIER that a later splice retracts
        (`flow.py:1315-1316`, the remaining half). Run A drives 500, split into 601 and 602
        with 602 behind 601, so both are adopted and 602 waits in the next wave. 601 then
        splits in its own wave, and just before that splice re-levels the tail, 602 is
        re-planned onto a prerequisite nothing can satisfy: A retracts it by name and drives
        801 instead. While A is inside 801's build, a run here over 602 must not be refused
        by the run that retracted it."""
        _split_now(self.cfg, "500", ["601", "602"], bodies=_TWO_ORDERED, strand=True)
        six02 = self.cfg.bundle("602") / "brief.md"
        fixed = six02.read_text(encoding="utf-8")
        a, _pause = self._hold("flow", "--pause-on=issue_801", "--split-signoff=601:801",
                               "--retract=602", "500", tag="A")
        a_err = (self.tmp / "A.err").read_text(encoding="utf-8")
        self.assertIn("issue_602 — adopted earlier this run, now held", a_err)

        six02.write_text(fixed, encoding="utf-8")
        rc = self._cli(["602"])

        err = self.err.getvalue()
        self._assert_not_refused("602", err)
        self.assertEqual(self._state("602"), state.COMPLETE, err)
        self.assertEqual(rc, 0, err)
        self.assertIsNone(a.poll(), "run A should still be alive, driving 801")

    # -- (vii) a resume line agrees with the refusal ------------------------------------------

    def test_a_resume_line_for_a_bundle_the_run_still_holds_says_when_it_applies(self) -> None:
        """Run A is `pdca flow 7 8` with 8 conflicting with 7, so 8 gets a wave of its own.
        Nobody answers 7's sign-off, so A gives up on 7 mid-run and moves to 8 — still
        holding 7, still sweeping and reporting it. The resume line it prints for 7 has to
        say when that command applies, because typed now it is refused (asserted here, from
        another shell). On the base the line reads as an instruction for right now."""
        _briefed(self.cfg, "7")
        _briefed(self.cfg, "8", "- **Conflicts with:** 7")
        a, _pause = self._hold("flow", "--pause-on=issue_8", "--walk-away=issue_7",
                               "--max-passes=1", "7", "8", tag="A")

        a_err = (self.tmp / "A.err").read_text(encoding="utf-8")
        self.assertIn("issue_7 [AWAITING_SIGNOFF] — resume with `pdca flow 7` once this run "
                      "has ended", a_err)

        rc = self._cli(["7"])
        err = self.err.getvalue()
        self.assertEqual(rc, 1, err)
        self.assertTrue(self._held_lines(err, "issue_7"), err)
        self.assertIsNone(a.poll(), "run A should still be alive, driving 8")

    # -- (viii) nothing else changes ----------------------------------------------------------

    def test_a_single_run_with_no_second_driver_says_nothing_about_claims(self) -> None:
        """The ordinary case: one run, nothing contending. It drives both bundles to
        COMPLETE, exits 0, and says not one word about claims, holders or refusals."""
        _briefed(self.cfg, "7")
        _briefed(self.cfg, "8")
        leaves.do_plan_batch = _quiet_plan

        rc = self._cli(["7", "8"])

        err = self.err.getvalue()
        for word in ("held by another", "could not record", "refusing to", "NOT driven"):
            self.assertNotIn(word, err)
        self.assertEqual(self._state("7"), state.COMPLETE, err)
        self.assertEqual(self._state("8"), state.COMPLETE, err)
        self.assertEqual(rc, 0, err)

    def test_claims_live_outside_every_bundle_and_are_gitignored_in_the_render(self) -> None:
        """A batch that claims bundles and adopts a split leaves its ownership record ONLY
        under the process dir (docs 07 §Lanes names `process/.drive-claims/`) — never inside
        a bundle, where `state.state` and a results commit would see it — and everything it
        leaves there is ignored by the render's `.gitignore`."""
        git = shutil.which("git")
        if git is None or _IGNORE is None:
            self.skipTest("needs git and the render's .gitignore")
        _briefed(self.cfg, "7")
        leaves.do_plan_batch = lambda cfg, csv=None, ids=None: _split_now(
            cfg, "500", ["601", "602"])

        rc = self._cli([], csv="tracker.csv")
        self.assertEqual(rc, 0, self.err.getvalue())

        in_bundles = [p for p in self.cfg.bundle_root.rglob("*") if _CLAIMS in p.parts]
        self.assertEqual(in_bundles, [])
        left = ([p for p in self.cfg.process_dir.rglob("*") if p.is_file()]
                if self.cfg.process_dir.is_dir() else [])
        # The record exists — this is not a vacuous pass over nothing (red on the base,
        # which records no ownership at all) …
        self.assertTrue([p for p in left if _CLAIMS in p.parts],
                        f"no ownership record under {self.cfg.process_dir}")
        # … and every bit of it is ignored by the render, so no commit can carry it.
        repo = self.tmp / "ignore-check"
        repo.mkdir()
        subprocess.run([git, "init", "-q", str(repo)], check=True, capture_output=True)
        shutil.copyfile(_IGNORE, repo / ".gitignore")
        for p in left:
            rel = p.relative_to(self.cfg.root).as_posix()
            probe = subprocess.run([git, "-C", str(repo), "check-ignore", "-q", "--no-index",
                                    rel], capture_output=True)
            self.assertEqual(probe.returncode, 0, f"{rel} is not ignored by {_IGNORE.name}")


if __name__ == "__main__":
    unittest.main()
