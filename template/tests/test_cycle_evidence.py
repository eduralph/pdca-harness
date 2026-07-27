"""A tracker `resolved` marker must not settle a bundle whose cycle actually ran (#334).

`is_resolved` read a `resolved` dict and returned True; `state()` consults it whenever
`brief.md` is absent, and RESOLVED is terminal — the bundle leaves the resume set and
`do_plan` returns early rather than briefing it (#302). Nothing checked that the bundle
was briefless-and-nothing-else.

The docstring said callers "scope this to BRIEFLESS bundles only" — but that is not a
guard a caller can honour: an iterate-to-Plan ARCHIVES `brief.md`, so a bundle mid-cycle
with a full `iteration-v*` history is briefless too. A marker arriving then (a stale
scrape, a duplicate closure, a human closing the ticket while the fix is in flight) made
a live cycle terminal, and the work was abandoned with nothing reported.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from pdca_harness import driver, state

_RESOLVED = json.dumps({"resolved": {"github_state": "closed",
                                     "state_reason": "completed",
                                     "closed_at": "2026-01-01T00:00:00Z"}})


class CycleEvidence(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundle(self, name: str, artifacts: dict[str, str]) -> Path:
        """A bundle stripped to the given artifacts plus a `resolved` notes.json."""
        d = self.tmp / f"issue_{name}"
        d.mkdir(parents=True)
        (d / "notes.json").write_text(_RESOLVED, encoding="utf-8")
        for rel, body in artifacts.items():
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
        return d

    # -- the genuinely-settled case still works -------------------------------------

    def test_notes_only_bundle_is_still_resolved(self) -> None:
        """The whole point of #302 — this must keep working."""
        d = self._bundle("notes-only", {})
        self.assertTrue(state.is_resolved(d))
        self.assertEqual(state.state(d), state.RESOLVED)

    def test_placeholder_brief_does_not_count_as_evidence(self) -> None:
        """An unfilled template copy is "never authored" — same standing as no brief, so
        the tracker's resolution still wins (#302 review)."""
        d = self._bundle("placeholder", {"brief.md": "- **Slug:** <fill-me>\n"})
        self.assertTrue(state.is_resolved(d))

    # -- the misclassifications this issue fixes ------------------------------------

    def test_authored_brief_blocks_resolution(self) -> None:
        d = self._bundle("authored", {"brief.md": "- **Slug:** real-work\n"})
        self.assertFalse(state.is_resolved(d))

    def test_slug_beneath_its_label_counts_as_authored(self) -> None:
        """The shape `brief.md.tpl` teaches and four briefs in one corpus use. Under the
        line-based accessor this read as a placeholder, so a live authored bundle carrying
        a stale marker was classified RESOLVED and abandoned (#336 + #334)."""
        d = self._bundle("beneath", {"brief.md": "- **Slug:**\n  reopened-live-work\n"})
        self.assertFalse(state.is_resolved(d),
                         "a Slug written beneath its label read as an unfilled template")

    def test_each_downstream_artifact_blocks_resolution(self) -> None:
        for name in state.DOWNSTREAM_OF_BRIEF:
            with self.subTest(artifact=name):
                d = self._bundle(f"dsb-{name}", {name: "x"})
                self.assertFalse(state.is_resolved(d))

    def test_advisory_and_error_log_block_resolution(self) -> None:
        """The two glob classes — the case the instance work already covered."""
        for name in ("check-advisory-adversary.md", "build.error.log"):
            with self.subTest(artifact=name):
                d = self._bundle(f"glob-{name}", {name: "x"})
                self.assertFalse(state.is_resolved(d))
                self.assertEqual(state.state(d), state.UNPLANNED)

    def test_accumulator_artifacts_block_resolution(self) -> None:
        """The gap the instance work did NOT cover.

        Both files are kept out of the archive so they accumulate across rebuilds, so
        neither counted as evidence — yet a bundle cannot hold either without having run
        a cycle. Before this fix both reported `is_resolved=True`.
        """
        for name in state.CYCLE_EVIDENCE_ONLY:
            with self.subTest(artifact=name):
                d = self._bundle(f"acc-{name}", {name: "{}"})
                self.assertFalse(state.is_resolved(d),
                                 f"{name} is proof a cycle ran, but did not count")
                self.assertEqual(state.state(d), state.UNPLANNED)

    def test_iteration_archive_blocks_resolution(self) -> None:
        """The case the docstring itself named: an iterate-to-Plan archives the brief, so
        a mid-cycle bundle is briefless — and was therefore resolvable."""
        d = self._bundle("iterated", {"iteration-v1/brief.md": "- **Slug:** was-here\n"})
        self.assertFalse(state.is_resolved(d))

    # -- the two sets must not drift ------------------------------------------------

    def test_archive_globs_are_the_shared_source_of_truth(self) -> None:
        """`_archive_iteration` and `is_resolved` read ONE constant, so "what an iterate
        moves" and "what proves a cycle ran" cannot be changed independently."""
        self.assertIs(driver.DOWNSTREAM_GLOBS, state.DOWNSTREAM_GLOBS)
        self.assertIs(driver.DOWNSTREAM_OF_BRIEF, state.DOWNSTREAM_OF_BRIEF)

    def test_accumulators_are_evidence_but_are_never_archived(self) -> None:
        """The one set where the two answers deliberately DIFFER, and the reason #334 is
        not a one-line addition.

        Adding these names to DOWNSTREAM_OF_BRIEF would fix the misclassification above
        and break termination: archiving `auto-iterate.json` resets the round budget every
        iterate, so auto-iterate would never terminate. So they must count as evidence
        WITHOUT being archived — asserted here so a later tidy-up cannot merge the sets.
        """
        for name in state.CYCLE_EVIDENCE_ONLY:
            with self.subTest(artifact=name):
                self.assertNotIn(name, state.DOWNSTREAM_OF_BRIEF)
                self.assertFalse(any(Path(name).match(p) for p in state.DOWNSTREAM_GLOBS),
                                 f"{name} would be archived by a DOWNSTREAM_GLOBS pattern")

    def test_accumulators_survive_a_real_archive(self) -> None:
        """Behavioural, not just declarative: run the archive step and check they stay."""
        d = self.tmp / "issue_arch"
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** s\n", encoding="utf-8")
        (d / "patch.diff").write_text("--- a\n+++ b\n", encoding="utf-8")
        (d / "check-advisory-adversary.md").write_text("x", encoding="utf-8")
        for name in state.CYCLE_EVIDENCE_ONLY:
            (d / name).write_text("{}", encoding="utf-8")

        driver._archive_iteration(d, 1, include_brief=False)

        self.assertFalse((d / "patch.diff").exists(), "downstream artifact not archived")
        self.assertFalse((d / "check-advisory-adversary.md").exists(),
                         "glob-matched artifact not archived")
        for name in state.CYCLE_EVIDENCE_ONLY:
            self.assertTrue((d / name).exists(),
                            f"{name} was archived — its accumulation is now broken")




class CleanupAgreesWithTheGuard(unittest.TestCase):
    """`cleanup` must share the notes-only test with `is_resolved` (PR #345 review).

    Otherwise the two disagree about one bundle: cleanup sees "briefless + UNPLANNED +
    tracker closed" and schedules `_mark_resolved`, while `is_resolved` refuses the marker
    on the cycle evidence. `cleanup --apply` then reports a successful mutation that
    changes nothing, and proposes the identical action on every subsequent run.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_an_evidenced_briefless_bundle_is_reported_not_resolved(self) -> None:
        from pdca_harness import cleanup
        d = self.tmp / "issue_9"
        (d / "iteration-v1").mkdir(parents=True)      # brief archived mid-cycle
        (d / "notes.json").write_text("{}", encoding="utf-8")
        self.assertEqual(state.state(d), state.UNPLANNED)
        self.assertTrue(state.has_cycle_evidence(d))

        from unittest import mock
        with mock.patch.object(cleanup, "_issue_state",
                               return_value={"state": "CLOSED"}):
            row = cleanup._plan_bundle(None, d, issue_side=True, repo="o/r",
                                       by="t", today="2026-01-01")
        self.assertIsNotNone(row, "an evidenced briefless bundle produced no row at all")
        self.assertIn("in-flight cycle", row.plan)
        self.assertEqual(row.apply, [],
                         "cleanup scheduled a mutation that is_resolved would refuse")

if __name__ == "__main__":
    unittest.main()
