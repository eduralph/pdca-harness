"""§9 is the authority section — what may and may not grant a sign-off (issues #327, #328).

``SUMMARY.md`` §9 is the only place a verdict lives, and ``state.state`` reads it directly to
decide COMPLETE, which in turn releases ``publish``. That makes this parse a security boundary,
not a convenience:

* The C6 accept-guard covers the **write** path (``flow._apply_decision`` refuses to accept
  while §6 has open items). Nothing guards the **read** path — an outcome token already sitting
  in the file is simply believed. So any route that gets a valid token in front of the reader
  bypasses the human touch point entirely, and these tests pin the two routes that did.
* The failure direction is asymmetric and the code depends on it: §9 must fail CLOSED (no
  heading ⇒ not signed off), §6 must fail OPEN (no heading ⇒ scan everything, find more open
  items, block harder). A fix that tightened both would convert a fail-safe into a fail-open,
  so both directions are asserted here.

Run from the project root:
    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pdca_harness import act, signoff, state

# A bundle that has reached AWAITING_SIGNOFF: brief, patch, gates. Only SUMMARY.md varies.
_BRIEF = "- **Slug:** demo\n- **Defect:** off by one.\n"


def _bundle(summary: str) -> Path:
    d = Path(tempfile.mkdtemp())
    (d / "brief.md").write_text(_BRIEF, encoding="utf-8")
    (d / "patch.diff").write_text("", encoding="utf-8")
    (d / "check-gates.json").write_text('{"rows": []}', encoding="utf-8")
    (d / "SUMMARY.md").write_text(summary, encoding="utf-8")
    return d


_SIGNED_OFF_ELSEWHERE = (
    "# Result\n\n"
    "## 6. NEEDS-HUMAN — items the human must clear before sign-off\n"
    "- [ ] C5 causal adequacy — a human must confirm the root cause\n\n"
    "## 5. Advisory review\n"
    "The reviewer quoted the template it was handed:\n"
    "- Outcome: accepted\n"
)


class NoSectionNineIsNotSignedOff(unittest.TestCase):
    """A leaf with Write/Bash can leave any bundle file malformed (``flow._isolate``), so a
    SUMMARY without a well-formed §9 is a live input. It must read as unsigned."""

    def test_a_token_outside_section_9_does_not_sign_the_bundle_off(self):
        """#327: the whole-document fallback let any ``- Outcome:`` line grant a sign-off.
        Here the token sits in the reviewer's prose and §6 is still open."""
        d = _bundle(_SIGNED_OFF_ELSEWHERE)
        self.assertEqual(signoff.outcome_token(d / "SUMMARY.md"), "")
        self.assertFalse(signoff.is_set(d / "SUMMARY.md"))
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)

    def test_a_section_9_demoted_to_h3_does_not_sign_the_bundle_off(self):
        """``_section`` matches ``## `` only, so a model regenerating the summary one level
        down used to drop straight through to the whole-document scan."""
        d = _bundle("# Result\n\n## 6. NEEDS-HUMAN\n- [ ] T5 judgment — human call\n\n"
                    "### 9. Check sign-off\n- Outcome: accepted\n- By / date: bot / 2026-07-25\n")
        self.assertEqual(signoff.outcome_token(d / "SUMMARY.md"), "")
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)

    def test_publish_is_never_released_by_a_malformed_summary(self):
        """The consequence that made #327 high severity: COMPLETE is what gates publish."""
        self.assertNotEqual(state.state(_bundle(_SIGNED_OFF_ELSEWHERE)), state.COMPLETE)

    def test_a_well_formed_section_9_still_signs_off(self):
        """The guard must not break the ordinary path — including with the same token
        present earlier in the document, which is exactly what scoping is for."""
        d = _bundle("# Result\n\n## 5. Advisory review\nquoting: `- Outcome: accepted`\n\n"
                    "## 9. Check sign-off\n- Outcome: accepted\n- By / date: eddie / 2026-07-25\n")
        self.assertEqual(signoff.outcome_token(d / "SUMMARY.md"), "accepted")
        self.assertEqual(state.state(d), state.COMPLETE)

    def test_iteration_delta_is_scoped_the_same_way(self):
        d = _bundle("# Result\n\n## 4. Notes\n- Iteration delta (if iterating): not a verdict\n")
        self.assertEqual(signoff.iteration_delta(d / "SUMMARY.md"), "")


class SectionSixStillFailsSafe(unittest.TestCase):
    """The other half of #327: §6 keeps the lenient fallback on purpose."""

    def test_open_items_are_found_even_with_no_section_6_heading(self):
        """Scanning the whole document can only find MORE open items, so it blocks accept
        harder. Tightening this in sympathy with the §9 fix would report zero open items on
        a malformed summary — a fail-open."""
        d = _bundle("# Result\n\nno headings at all\n- [ ] C5 causal adequacy — human call\n")
        self.assertEqual(len(signoff.open_needs_human(d / "SUMMARY.md")), 1)

    def test_a_ticked_item_is_not_open(self):
        d = _bundle("# Result\n\n## 6. NEEDS-HUMAN\n- [x] C5 — cleared by the human\n")
        self.assertEqual(signoff.open_needs_human(d / "SUMMARY.md"), [])


class EmptyOutcomeFieldStopsAtTheLineEnd(unittest.TestCase):
    """#328: ``_OUTCOME_RE`` used ``\\s*``, which matches ``\\n``, so an empty field captured
    the FOLLOWING line. Its sibling ``_DELTA_RE`` already used ``[ \\t]`` for this reason."""

    def test_an_empty_outcome_reads_as_empty_not_as_the_next_line(self):
        d = _bundle("# Result\n\n## 9. Check sign-off\n"
                    "- Disposition confirmed / overridden:\n- Outcome:\n"
                    "- Iteration delta (if iterating):\n- By / date:\n")
        self.assertEqual(signoff.outcome_token(d / "SUMMARY.md"), "")
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)

    def test_a_bare_token_on_the_following_line_does_not_sign_off(self):
        """The live-fire version: with the old regex this captured ``accepted``."""
        d = _bundle("# Result\n\n## 9. Check sign-off\n- Outcome:\naccepted\n")
        self.assertEqual(signoff.outcome_token(d / "SUMMARY.md"), "")
        self.assertNotEqual(state.state(d), state.COMPLETE)

    def test_a_value_with_trailing_whitespace_is_still_read(self):
        d = _bundle("# Result\n\n## 9. Check sign-off\n- Outcome: accepted   \n")
        self.assertEqual(signoff.outcome_token(d / "SUMMARY.md"), "accepted")


class ActLedgerReadsTheOutcomeTheSameWay(unittest.TestCase):
    """``act._extract`` carried its own copy of the #328 defect (``\\s*`` before the value), so
    an empty ``- Outcome:`` made the Act ledger record ``"- Iteration delta (if iterating):"``
    as the bundle's outcome. Not a safety hole — Act reports, it does not grant — but it is the
    same bug, and fixing one sibling while leaving the other is how #328 survived."""

    def test_an_empty_outcome_is_not_reported_as_the_next_line(self):
        d = _bundle("# Result\n\n## 9. Check sign-off\n"
                    "- Disposition confirmed / overridden:\n- Outcome:\n"
                    "- Iteration delta (if iterating):\n- By / date: eddie / 2026-07-25\n")
        entry = act._extract(d / "SUMMARY.md", d)
        self.assertEqual(entry.outcome, "")

    def test_a_real_outcome_is_still_reported(self):
        d = _bundle("# Result\n\n## 9. Check sign-off\n"
                    "- Outcome: merged-wider\n- By / date: eddie / 2026-07-25\n")
        self.assertEqual(act._extract(d / "SUMMARY.md", d).outcome, "merged-wider")


class RecordRefusesAMalformedSummary(unittest.TestCase):
    """Writing a decision the strict reader cannot see would silently never take effect, so
    ``record`` refuses rather than leaving the human's accept apparently ignored. It would
    also corrupt the file: ``text.replace("", ...)`` inserts at position 0."""

    def test_record_raises_when_there_is_no_section_9(self):
        d = _bundle("# custom summary — no canonical section 9\n")
        with self.assertRaises(ValueError) as caught:
            signoff.record(d / "SUMMARY.md", action="accept", by="eddie", date="2026-07-25")
        self.assertIn("9. Check sign-off", str(caught.exception))

    def test_the_summary_is_left_untouched_when_record_refuses(self):
        original = "# custom summary — no canonical section 9\n"
        d = _bundle(original)
        with self.assertRaises(ValueError):
            signoff.record(d / "SUMMARY.md", action="accept", by="eddie", date="2026-07-25")
        self.assertEqual((d / "SUMMARY.md").read_text(encoding="utf-8"), original)

    def test_record_still_writes_a_well_formed_summary(self):
        d = _bundle("# Result\n\n## 9. Check sign-off\n- Outcome:\n"
                    "- Iteration delta (if iterating):\n- By / date:\n")
        signoff.record(d / "SUMMARY.md", action="iterate-do", by="eddie", date="2026-07-25",
                       delta="the fix addressed the symptom")
        self.assertEqual(signoff.outcome_token(d / "SUMMARY.md"), "iterated-to-Do")
        self.assertEqual(signoff.iteration_delta(d / "SUMMARY.md"),
                         "the fix addressed the symptom")


class MalformedSummaryIsReportedNotRaisedAtTheBoundaries(unittest.TestCase):
    """``record`` refusing is only safe if every caller expects it. The batch sweep has
    ``flow._isolate``; the two DIRECT boundaries do not, and there a raise would surface as a
    traceback and abandon the run instead of reporting one bad bundle. Mirrors the precedent
    in ``flow._maybe_auto_iterate``, which already catches for exactly this reason."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "results").mkdir()

    def _cfg(self):
        from pdca_harness.config import Config, LeafConfig
        return Config(
            root=self.root, bundle_root=self.root / "results",
            process_dir=self.root / "process", templates_dir=self.root / "templates",
            default_branch="main", tracker_system="github", tracker_url="",
            issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"))

    def _malformed(self, iid: str) -> Path:
        d = self.root / "results" / f"issue_{iid}"
        d.mkdir(parents=True)
        (d / "brief.md").write_text(_BRIEF, encoding="utf-8")
        (d / "patch.diff").write_text("", encoding="utf-8")
        (d / "check-gates.json").write_text('{"rows": []}', encoding="utf-8")
        (d / "SUMMARY.md").write_text("# custom — no section 9\n", encoding="utf-8")
        return d

    def test_pdca_signoff_reports_and_exits_non_zero(self):
        import argparse
        from pdca_harness import cli
        d = self._malformed("70")
        args = argparse.Namespace(issue_id="70", accept=False, iterate_do=True,
                                  iterate_plan=False, by="eddie", delta="", no_publish=True)
        import contextlib, io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cli._signoff(self._cfg(), args)
        self.assertEqual(rc, 1)
        self.assertIn("9. Check sign-off", err.getvalue())
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)  # untouched

    def test_flow_apply_decision_returns_the_bundle_to_a_reassemblable_state(self):
        """Reporting is not enough: with SUMMARY.md still in place the bundle sits at
        AWAITING_SIGNOFF, a HALTED state, so nothing reassembles it — the single-issue flow
        would stop and the batch sweep would re-present the same unusable summary every pass
        until the budget ran out. Moving it aside drops the bundle to CHECKED, which the
        next beat rebuilds."""
        import contextlib, io
        from pdca_harness import flow, leaves
        d = self._malformed("71")
        (d / leaves.SIGNOFF_DECISION).write_text("accept\n", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            got = flow._apply_decision(self._cfg(), d, by="eddie", today="2026-07-25",
                                       apply_now=True)
        self.assertEqual(got, flow.REASSEMBLE)
        self.assertIn("not recorded", err.getvalue())
        self.assertFalse((d / leaves.SIGNOFF_DECISION).exists())  # stale decision dropped
        self.assertFalse((d / "SUMMARY.md").exists())
        self.assertEqual(state.state(d), state.CHECKED)  # a beat can act on this

    def test_the_malformed_summary_is_kept_not_deleted(self):
        """It may carry §6 boxes the human ticked, and it is evidence about the leaf that
        wrote it."""
        import contextlib, io
        from pdca_harness import flow, leaves
        d = self._malformed("72")
        (d / leaves.SIGNOFF_DECISION).write_text("accept\n", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            flow._apply_decision(self._cfg(), d, by="eddie", today="2026-07-25",
                                 apply_now=True)
        kept = d / "SUMMARY.malformed-2026-07-25.md"
        self.assertTrue(kept.exists())
        self.assertEqual(kept.read_text(encoding="utf-8"), "# custom — no section 9\n")

    def test_a_second_incident_the_same_day_does_not_overwrite_the_first(self):
        import contextlib, io
        from pdca_harness import flow, leaves
        d = self._malformed("73")
        (d / "SUMMARY.malformed-2026-07-25.md").write_text("the first one\n", encoding="utf-8")
        (d / leaves.SIGNOFF_DECISION).write_text("accept\n", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            flow._apply_decision(self._cfg(), d, by="eddie", today="2026-07-25",
                                 apply_now=True)
        self.assertEqual((d / "SUMMARY.malformed-2026-07-25.md").read_text(encoding="utf-8"),
                         "the first one\n")
        self.assertTrue((d / "SUMMARY.malformed-2026-07-25-2.md").exists())

    def test_reassemble_is_distinct_from_the_stop_signals(self):
        """The single-issue caller breaks on None/'blocked'; REASSEMBLE must not be either,
        or the bundle is stranded one beat short of a fresh SUMMARY."""
        from pdca_harness import flow
        self.assertNotIn(flow.REASSEMBLE, (None, "blocked"))


if __name__ == "__main__":
    unittest.main()
