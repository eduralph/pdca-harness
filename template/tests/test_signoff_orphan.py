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

That exception is also why the apply's own stderr notice ("… no new session") may only be
printed once the apply has HAPPENED. Announced before `_apply_decision` ran, it promised
the operator something the very next step could withdraw — C6 refusing the accept, or the
record being dropped / the summary repaired — and each of those already reports itself.

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

# The claim `_apply_recorded_decision` may make only about a decision it really did record
# without a session (flow.py:257-259 post-fix). Matched as a phrase because the defect is
# the CLAIM, not the wording: printed for any other outcome — C6 refusing the accept, a
# dropped or a repaired record — it promises something the very next step withdraws.
NO_SESSION = "no new session"

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

    def _announced(self, d: Path, needle: str) -> list[str]:
        """The stderr lines naming BOTH this bundle and ``needle`` (the action applied to
        it, or any other phrase the run is required — or forbidden — to say about it) — the
        brief's "never silent" requirement for a decision applied without a session."""
        return [ln for ln in self.err.getvalue().splitlines()
                if d.name in ln and needle in ln]


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
        # The other half of the notice contract: here the decision really WAS applied with
        # no session, so the run must still say exactly that. Without this, a "fix" that
        # simply deleted the notice would pass the C6 case below.
        self.assertTrue(self._announced(d, NO_SESSION),
                        "a decision genuinely applied without a session must still be "
                        "reported as applied with no new session")

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
        # The accept C6 PERMITS is the other side of the case below: applied, no session —
        # so the operator must be told exactly that, in the same terms.
        self.assertTrue(self._announced(d, NO_SESSION),
                        "an accept applied without a session must still be reported as "
                        "applied with no new session")

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
        # …and because it does, the run must never have said it wouldn't: the notice used
        # to be printed BEFORE the C6 guard ran, so the operator read the promise and the
        # guard's refusal one line apart, on the path that asks them to come back.
        self.assertFalse(self._announced(d, NO_SESSION),
                         "the run claimed no new session would be opened, then C6 refused "
                         f"the accept and opened one: {self.err.getvalue()}")

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
        self.assertTrue(self._announced(d, NO_SESSION),
                        "a decision genuinely applied without a session must still be "
                        "reported as applied with no new session")

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
        self.assertFalse(self._announced(d, NO_SESSION),
                         "the run claimed no new session would be opened, then C6 refused "
                         f"the accept and opened one: {self.err.getvalue()}")

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


class NotRecorded(_Base):
    """C6 is not the only step downstream of the notice that can withdraw it.

    ``_apply_decision`` has two more outcomes where the decision is explicitly NOT recorded
    — the stale token dropped because the bundle lost its SUMMARY.md (flow.py:161-165), and
    an unsignable SUMMARY.md moved aside so a later beat rebuilds it
    (``_repair_unsignable``, flow.py:114-130). Both are reached through
    ``_apply_recorded_decision``, i.e. with no session in sight, and both print their own
    line saying the decision was NOT recorded. Announcing "applied … no new session" beside
    either is the same claim-then-withdraw defect as the C6 one, one guard further down.
    """

    def test_a_dropped_decision_is_not_announced_as_applied(self) -> None:
        # An over-reaching leaf cleared the bundle's downstream: the decision is still on
        # disk, SUMMARY.md is gone, so there is nothing to record §9 into.
        d = self._halted_bundle("ORPHANNOSUM", "accept\n")
        (d / "SUMMARY.md").unlink()
        before = state.state(d)
        sessions: list[str] = []

        # Records only: with the summary gone there is nothing left for a session to
        # clobber, so the thing that must not happen here is the session itself.
        def spying_signoff(bundle: Path, cfg: Config) -> None:
            sessions.append(bundle.name)

        with mock.patch.object(leaves, "run_signoff", spying_signoff), \
                redirect_stderr(self.err), redirect_stdout(io.StringIO()):
            applied = flow._signoff_and_apply(self.cfg, d, by="t", today="2026-01-01",
                                              apply_now=False)

        # Unchanged behaviour: dropped, not applied, no session, bundle left to re-drive.
        self.assertIsNone(applied)
        self.assertEqual(sessions, [])
        self.assertFalse((d / leaves.SIGNOFF_DECISION).exists())   # dropped, not recorded
        self.assertEqual(state.state(d), before)
        self.assertTrue(self._announced(d, "skipping record"),
                        "dropping a decision must still say so, naming the bundle")
        self.assertFalse(self._announced(d, NO_SESSION),
                         "the run reported the decision as applied one line after saying "
                         f"it was not recorded at all: {self.err.getvalue()}")

    def test_a_repaired_unsignable_summary_is_not_announced_as_applied(self) -> None:
        # The other one: SUMMARY.md is present but unsignable — §9, the section `record`
        # writes into, was truncated away — so it is quarantined and the bundle drops back
        # to a state a later beat reassembles.
        d = self._halted_bundle("ORPHANMANGLED", "accept\n")
        summary = d / "SUMMARY.md"
        head, sep, _ = summary.read_text(encoding="utf-8").partition(
            f"## {signoff.SIGNOFF_HEADING}")
        self.assertTrue(sep, "fixture must start from a summary that HAS a §9 to lose")
        summary.write_text(head, encoding="utf-8")
        self.assertTrue(signoff.unrecordable(summary),
                        "fixture must be genuinely unsignable, else it proves nothing")
        sessions: list[str] = []

        def spying_signoff(bundle: Path, cfg: Config) -> None:
            sessions.append(bundle.name)

        with mock.patch.object(leaves, "run_signoff", spying_signoff), \
                redirect_stderr(self.err), redirect_stdout(io.StringIO()):
            applied = flow._signoff_and_apply(self.cfg, d, by="t", today="2026-01-01",
                                              apply_now=False)

        # Unchanged behaviour: repaired, not applied, no session.
        self.assertEqual(applied, flow.REASSEMBLE)
        self.assertEqual(sessions, [])
        self.assertFalse(summary.exists())
        self.assertTrue([p for p in d.iterdir() if p.name.startswith("SUMMARY.malformed-")],
                        "the unsignable summary must be kept aside as evidence")
        self.assertTrue(self._announced(d, "to reassemble"),
                        "a repaired bundle must still say so, naming the bundle")
        self.assertFalse(self._announced(d, NO_SESSION),
                         "the run reported the decision as applied one line after saying "
                         f"it was not recorded at all: {self.err.getvalue()}")


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
