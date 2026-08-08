"""Offline slice for issue #453 — a `signoff-decision` orphaned by an interrupted
session is un-consumed INPUT to the driver, not a by-product of the session that wrote
it (stdlib unittest).

The sign-off leaf writes its decision durably, but the driver used to consume it only
in-process, in the same call that launched the session. When the run dies in between — a
`^C` raises KeyboardInterrupt, which `flow._isolate` deliberately does not contain — the
decision is orphaned on disk with §9 unrecorded and the bundle still AWAITING_SIGNOFF.
Every later pass and every later run then re-presented that bundle and opened a FRESH
session for a decision the human had already made, whose write clobbered it (the reporting
instance saw one decision made, re-issued and re-affirmed, none recorded).

Post-fix, both drive paths — the batch `flow._drive_wave` and the single-issue
`flow._signoff_and_apply` — record §9 and transition the bundle WITHOUT invoking any
sign-off leaf, and `flow._maybe_auto_iterate` declines (writes no decision, spends no
budget) while such a file exists. The one exception: an `accept` C6 refuses (§6
NEEDS-HUMAN still open) still falls through to a fresh session, because there the human
genuinely must return.

Run from the project root:
    PYTHONPATH=src python3 -m unittest tests.test_signoff_orphan
"""

from __future__ import annotations

import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from pdca_harness import assemble, autoiterate, driver, flow, leaves, signoff, state
from pdca_harness.config import Config, LeafConfig

# What the interrupted session wrote and the driver never read: the token plus the human's
# rationale, which §9's "Iteration delta" must carry.
ORPHANED = "iterate-do\nnot yet — the gate is wrong\n"

# An implementation-level finding, in the form a real advisory leaf emits (leaves.py:2402).
# Only with one of these is auto-iterate genuinely ELIGIBLE, so the test below exercises the
# real classifier rather than a mocked verdict — pre-fix it really does overwrite the
# human's decision with its own.
IMPL_FINDING = "# Advisory\n\n- NEEDS-HUMAN [impl] — off-by-one at src/x.py:12\n"


def _stub_config(root: Path, *, auto_iterate: bool = False) -> Config:
    """All six leaves stubbed, gates empty (all-PASS stub rows) — the offline shape of
    ``tests.test_flow_slice._stub_config``. No Claude, no TTY, no network."""
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
        auto_iterate=auto_iterate,
        # Hermetic: pin the toy target inside this test's tmp root (the sibling default
        # would resolve to a SHARED /tmp/example-repo).
        repo_checkouts={"example-org/example-repo": str(root / "example-repo")},
    )


def _clear_needs_human(d: Path) -> None:
    """What the human did in the session that then died: tick every §6 box."""
    summary = d / "SUMMARY.md"
    summary.write_text(summary.read_text(encoding="utf-8").replace("- [ ]", "- [x]"),
                       encoding="utf-8")


def _session_writes_accept(d: Path) -> None:
    """What a sign-off session does to the bundle — clear §6 and write its OWN decision
    (``leaves._stub_signoff``, leaves.py:2974-2980). Which is exactly how an orphaned
    decision gets clobbered: if a session is opened at all, the human's call is gone."""
    _clear_needs_human(d)
    (d / leaves.SIGNOFF_DECISION).write_text("accept\n", encoding="utf-8")


class _Base(unittest.TestCase):
    auto_iterate = False

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp, auto_iterate=self.auto_iterate)
        self.err = io.StringIO()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _halted_bundle(self, issue_id: str, decision: str = "") -> Path:
        """A bundle driven (stub Plan→Do→Check) to a genuine AWAITING_SIGNOFF halt, then
        carrying ``decision`` on disk with §9 unrecorded — the exact artifact state an
        interrupted sign-off session leaves behind."""
        d = self.cfg.bundle(issue_id)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertTrue(flow._plan_if_unplanned(self.cfg, d, None))
            self.assertEqual(driver.run_issue(d, self.cfg), state.AWAITING_SIGNOFF)
        if decision:
            (d / leaves.SIGNOFF_DECISION).write_text(decision, encoding="utf-8")
        return d

    def _announced(self, d: Path, action: str) -> list[str]:
        """The stderr lines naming BOTH this bundle and the action applied to it — the
        brief's "never silent" requirement for a decision applied without a session."""
        return [ln for ln in self.err.getvalue().splitlines()
                if d.name in ln and action in ln]


class DriveWave(_Base):
    """The batch drive path: ``flow._drive_wave``."""

    def test_applies_orphaned_decision_without_a_session(self) -> None:
        d = self._halted_bundle("ORPHANWAVE", ORPHANED)
        sessions: list[list[str]] = []

        def spying_batch(cfg: Config, bundles: list[Path]) -> None:
            sessions.append([b.name for b in bundles])
            for b in bundles:
                _session_writes_accept(b)  # the clobber, reproduced — not curated out

        with mock.patch.object(leaves, "run_signoff_batch", spying_batch), \
                redirect_stderr(self.err), redirect_stdout(io.StringIO()):
            # ONE pass: the orphaned decision must be applied within the very pass that
            # finds it, before any chunk is offered a session.
            flow._drive_wave(self.cfg, [d], by="t", today="2026-01-01", max_passes=1)

        # Read first, so the failure message can show WHAT the session recorded instead.
        outcome = signoff.outcome_token(d / "SUMMARY.md")
        self.assertEqual(sessions, [], "a fresh sign-off session was opened for a bundle "
                                       f"that already carried a decision on disk; §9 now "
                                       f"records '{outcome}'")
        self.assertEqual(outcome, "iterated-to-Do")
        self.assertEqual(signoff.iteration_delta(d / "SUMMARY.md"),
                         "not yet — the gate is wrong")   # the HUMAN's rationale, carried
        self.assertEqual(state.state(d), state.ITERATE_DO)
        self.assertFalse((d / leaves.SIGNOFF_DECISION).exists())  # consumed
        self.assertTrue(self._announced(d, "iterate-do"),
                        "an apply with no session must name the bundle and the action")

    def test_orphaned_accept_reaches_complete_without_a_session(self) -> None:
        # The end result on the accept path: §6 was cleared and `accept` written by the
        # session that died; the wave must record it and finish the bundle, not re-ask.
        d = self._halted_bundle("ORPHANWAVEOK", "accept\n")
        _clear_needs_human(d)
        sessions: list[list[str]] = []

        def spying_batch(cfg: Config, bundles: list[Path]) -> None:
            sessions.append([b.name for b in bundles])

        with mock.patch.object(leaves, "run_signoff_batch", spying_batch), \
                redirect_stderr(self.err), redirect_stdout(io.StringIO()):
            flow._drive_wave(self.cfg, [d], by="t", today="2026-01-01", max_passes=1)

        self.assertEqual(sessions, [], "a fresh sign-off session was opened for a bundle "
                                       "the human had already accepted")
        self.assertEqual(state.state(d), state.COMPLETE)
        self.assertEqual(signoff.outcome_token(d / "SUMMARY.md"), "merged-wider")

    def test_c6_refused_accept_still_gets_a_fresh_session(self) -> None:
        # The one exception: an `accept` C6 refuses (§6 NEEDS-HUMAN still open) leaves the
        # bundle needing the human, so the wave still offers it a session.
        d = self._halted_bundle("ORPHANWAVEC6", "accept\n")  # §6 deliberately left open
        sessions: list[list[str]] = []

        def returning_human(cfg: Config, bundles: list[Path]) -> None:
            sessions.append([b.name for b in bundles])
            for b in bundles:
                _session_writes_accept(b)  # they come back, clear §6 and accept for real

        with mock.patch.object(leaves, "run_signoff_batch", returning_human), \
                redirect_stderr(self.err), redirect_stdout(io.StringIO()):
            flow._drive_wave(self.cfg, [d], by="t", today="2026-01-01", max_passes=1)

        self.assertEqual(sessions, [[d.name]], "a C6-refused accept must still fall "
                                               "through to a fresh session")
        self.assertEqual(state.state(d), state.COMPLETE)

    def test_only_the_undecided_bundle_of_a_wave_is_offered_a_session(self) -> None:
        # The pre-apply FILTERS the queue, it is not all-or-nothing: in a wave holding both
        # kinds, the decided bundle is recorded from disk and the undecided one — and only
        # it — reaches the human, in the same pass.
        decided = self._halted_bundle("ORPHANMIXA", ORPHANED)
        undecided = self._halted_bundle("ORPHANMIXB")
        sessions: list[list[str]] = []

        def spying_batch(cfg: Config, bundles: list[Path]) -> None:
            sessions.append([b.name for b in bundles])
            for b in bundles:
                _session_writes_accept(b)

        with mock.patch.object(leaves, "run_signoff_batch", spying_batch), \
                redirect_stderr(self.err), redirect_stdout(io.StringIO()):
            flow._drive_wave(self.cfg, [decided, undecided], by="t", today="2026-01-01",
                             max_passes=1)

        self.assertEqual(sessions, [[undecided.name]],
                         "only the bundle with no decision on disk owes a session")
        self.assertEqual(signoff.outcome_token(decided / "SUMMARY.md"), "iterated-to-Do")
        self.assertEqual(state.state(decided), state.ITERATE_DO)
        self.assertEqual(state.state(undecided), state.COMPLETE)


class SignoffAndApply(_Base):
    """The single-issue drive path: ``flow._signoff_and_apply``."""

    def test_applies_orphaned_decision_without_a_session(self) -> None:
        d = self._halted_bundle("ORPHANSOLO", ORPHANED)
        sessions: list[str] = []

        def spying_signoff(bundle: Path, cfg: Config) -> None:
            sessions.append(bundle.name)
            _session_writes_accept(bundle)  # the clobber, reproduced

        with mock.patch.object(leaves, "run_signoff", spying_signoff), \
                redirect_stderr(self.err), redirect_stdout(io.StringIO()):
            applied = flow._signoff_and_apply(self.cfg, d, by="t", today="2026-01-01",
                                              apply_now=False)

        outcome = signoff.outcome_token(d / "SUMMARY.md")
        self.assertEqual(sessions, [], "a fresh sign-off session was opened for a bundle "
                                       f"that already carried a decision on disk; §9 now "
                                       f"records '{outcome}'")
        self.assertEqual(applied, "iterate-do")
        self.assertEqual(outcome, "iterated-to-Do")
        self.assertEqual(signoff.iteration_delta(d / "SUMMARY.md"),
                         "not yet — the gate is wrong")
        self.assertEqual(state.state(d), state.ITERATE_DO)
        self.assertFalse((d / leaves.SIGNOFF_DECISION).exists())  # consumed
        self.assertTrue(self._announced(d, "iterate-do"),
                        "an apply with no session must name the bundle and the action")

    def test_c6_refused_accept_still_gets_a_fresh_session(self) -> None:
        d = self._halted_bundle("ORPHANSOLOC6", "accept\n")  # §6 deliberately left open
        sessions: list[str] = []

        def returning_human(bundle: Path, cfg: Config) -> None:
            sessions.append(bundle.name)
            _session_writes_accept(bundle)

        with mock.patch.object(leaves, "run_signoff", returning_human), \
                redirect_stderr(self.err), redirect_stdout(io.StringIO()):
            applied = flow._signoff_and_apply(self.cfg, d, by="t", today="2026-01-01",
                                              apply_now=False)

        self.assertEqual(sessions, [d.name], "a C6-refused accept must still fall through "
                                             "to a fresh session")
        self.assertEqual(applied, "accept")
        self.assertEqual(signoff.outcome_token(d / "SUMMARY.md"), "merged-wider")

    def test_flow_completes_an_orphaned_accept_without_reopening_signoff(self) -> None:
        # End-to-end through the public entry (`pdca flow <id>` on a bundle whose sign-off
        # session was ^C'd after the decision was written): no session, §9 recorded, the
        # bundle finished.
        d = self._halted_bundle("ORPHANFLOW", "accept\n")
        _clear_needs_human(d)
        sessions: list[str] = []

        def spying_signoff(bundle: Path, cfg: Config) -> None:
            sessions.append(bundle.name)

        with mock.patch.object(leaves, "run_signoff", spying_signoff), \
                redirect_stderr(self.err), redirect_stdout(io.StringIO()):
            final = flow.flow(self.cfg, "ORPHANFLOW", by="t", today="2026-01-01")

        self.assertEqual(sessions, [], "`pdca flow` re-opened sign-off for a bundle the "
                                       "human had already decided")
        self.assertEqual(final, state.COMPLETE)
        self.assertEqual(signoff.outcome_token(d / "SUMMARY.md"), "merged-wider")
        self.assertFalse((d / leaves.SIGNOFF_DECISION).exists())  # consumed


class AutoIterate(_Base):
    """``flow._maybe_auto_iterate`` must never author a decision over one it did not
    write — ``autoiterate.write_decision`` is unconditional (flow.py:271 pre-fix)."""

    auto_iterate = True

    def test_declines_while_a_human_decision_is_unconsumed(self) -> None:
        d = self._halted_bundle("ORPHANAUTO", "accept\n")
        # A REAL implementation-level finding, so the real classifier says "eligible" and
        # the pre-fix path genuinely overwrites the human's `accept` with its own
        # `iterate-do`. No mocked verdict — `collect_needs_human` reads this artifact.
        (d / "check-advisory-adversary.md").write_text(IMPL_FINDING, encoding="utf-8")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            assemble.assemble_summary(d, self.cfg)   # §6 now renders that finding too
        self.assertTrue(autoiterate.eligible(assemble.collect_needs_human(d, self.cfg)),
                        "fixture must be genuinely auto-iterable, else it proves nothing")

        with redirect_stderr(self.err), redirect_stdout(io.StringIO()):
            routed = flow._maybe_auto_iterate(self.cfg, d, by="auto-iterate",
                                              today="2026-01-01", apply_now=False)

        self.assertFalse(routed)
        self.assertEqual(leaves.signoff_decision(d), "accept",   # NOT clobbered
                         "auto-iterate overwrote a decision it did not author")
        self.assertEqual(autoiterate.count(d), 0, "auto-iterate must spend no budget on a "
                                                  "bundle that is already decided")
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)  # §9 still the human's
        self.assertTrue(self._announced(d, "not auto-iterating"),
                        "declining must say why, naming the bundle")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
