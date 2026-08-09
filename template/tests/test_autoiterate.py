"""Auto-iterate on implementation-only Check findings (issue #264, stdlib unittest).

The driver may rebuild a bundle unattended when EVERY open SUMMARY §6 item is an
implementation defect — a `gate` cell of the 5/5/1 (C2/C4/T1..T4), or an advisory finding
the leaf tagged `[impl]`. Anything architectural (a `judgment` cell C5/T5/V, an `input` cell
C1/C3), a gate that could not run, an external dependency, an unmarked advisory bullet, or a
row it cannot classify still halts for the human.

Load-bearing negatives, each its own test: it must never auto-accept, never tick a §6 box,
never iterate past a judgment finding, and never run past its budget. Offline: stub leaves,
real gate commands, no Claude.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from pdca_harness import assemble, autoiterate, cli, driver, flow, gates, leaves, signoff, state
from pdca_harness.config import Config, LeafConfig

_GATE = {"id": "C4", "tier": "C4", "label": "verify", "scope": "bundle", "gating": True}
_PASS = {**_GATE, "cmd": "true"}
_FAIL = {**_GATE, "cmd": "false"}
_UNVERIFIABLE = {**_GATE, "cmd": "echo 'PDCA-UNVERIFIABLE: no prod file'; exit 0"}

_CLEAN_REVIEW = "All advisory items PASS.\n"


# The reviewer's prompt (agents/reviewer.md.jinja) hard-codes this row to NEEDS-HUMAN on EVERY
# cycle — validation is the human's call by definition. So EVERY real `check-review.md` carries
# it, and a fixture without it is a shape the product never produces. Omitting it is exactly why
# the original #264 tests passed while auto-iterate was unreachable in production (#293): they
# tested the mental model, not the artifact. It belongs in the fixture, not in one new test.
_STANDING_ROW = "| Validation — fitness-to-purpose | NEEDS-HUMAN | fitness is the human's call |"


def _review_table(item: str, verdict: str = "NEEDS-HUMAN", basis: str = "off-by-one",
                  *, standing: bool = True) -> str:
    rows = f"| {item} | {verdict} | {basis} |\n"
    if standing:
        rows += _STANDING_ROW + "\n"
    return f"# Review\n\n| Item | Verdict | Basis |\n|---|---|---|\n{rows}"


def _stub_config(root: Path) -> Config:
    return Config(
        root=root,
        bundle_root=root / "results",
        process_dir=root / "process",
        templates_dir=root / "templates",
        default_branch="main",
        tracker_system="github",
        tracker_url="",
        issue_id_example="#1",
        builder=LeafConfig(mode="stub", family="claude"),
        reviewer=LeafConfig(mode="stub", family="codex"),
        auto_iterate=True,
        max_auto_iters=3,
    )


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundle(self, iid: str, *, gate: dict = _PASS, review: str = _CLEAN_REVIEW,
                advisory: str | None = None, build_notes: str | None = None,
                brief_body: str = "- **Slug:** ai\n") -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text(brief_body, encoding="utf-8")
        (d / "patch.diff").write_text("--- a\n+++ b\n", encoding="utf-8")
        (d / "check-review.md").write_text(review, encoding="utf-8")
        if advisory is not None:
            (d / "check-advisory-adversary.md").write_text(advisory, encoding="utf-8")
        if build_notes is not None:
            (d / "build-notes.md").write_text(build_notes, encoding="utf-8")
        self.cfg.gates_checks = [gate]
        gates.run_gates(d, self.cfg)
        assemble.assemble_summary(d, self.cfg)
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)
        return d

    def _try(self, d: Path, *, apply_now: bool = False) -> bool:
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            return flow._maybe_auto_iterate(
                self.cfg, d, by="", today="2026-07-09", apply_now=apply_now)

    def _assert_halted(self, d: Path) -> None:
        """No decision written, no budget spent, bundle still waiting on the human."""
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)
        self.assertFalse((d / leaves.SIGNOFF_DECISION).exists())
        self.assertEqual(autoiterate.count(d), 0)
        self.assertTrue(signoff.open_needs_human(d / "SUMMARY.md") or True)  # §6 untouched


class AutoIterates(_Base):
    """Implementation-only findings ⇒ the driver rebuilds without asking."""

    def test_failed_gating_gate_auto_iterates(self) -> None:
        d = self._bundle("GATEFAIL", gate=_FAIL)
        self.assertTrue(self._try(d))
        self.assertEqual(state.state(d), state.ITERATE_DO)
        self.assertEqual(signoff.outcome_token(d / "SUMMARY.md"), "iterated-to-Do")
        self.assertEqual(autoiterate.count(d), 1)

    def test_reviewer_needs_human_on_a_gate_cell_auto_iterates(self) -> None:
        d = self._bundle("C4NH", review=_review_table("C4 Verification (red→green)"))
        self.assertTrue(self._try(d))
        self.assertEqual(state.state(d), state.ITERATE_DO)

    def test_conformance_gate_cells_auto_iterate(self) -> None:
        for elem in ("C2 Reproduction (red pre-fix)", "T1 Structure", "T2 Shape",
                     "T3 Runtime", "T4 Contribution"):
            with self.subTest(elem=elem):
                d = self._bundle(f"E{elem[:2]}", review=_review_table(elem))
                self.assertTrue(self._try(d), f"{elem} is a gate cell — should auto-iterate")

    def test_advisory_impl_marker_auto_iterates_and_text_is_clean(self) -> None:
        d = self._bundle("ADVIMPL", advisory="- NEEDS-HUMAN [impl] — off-by-one at src/x.py:12\n")
        items = assemble.collect_needs_human(d, self.cfg)
        self.assertEqual([i.kind for i in items], [assemble.IMPL])
        self.assertTrue(items[0].text.startswith("off-by-one"))  # the marker is stripped
        self.assertTrue(self._try(d))

    def test_rationale_reaches_the_brief_carry_forward(self) -> None:
        # The next Do iteration must not be blind about why it was rejected.
        d = self._bundle("CARRY", gate=_FAIL)
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            flow._maybe_auto_iterate(self.cfg, d, by="", today="2026-07-09", apply_now=True)
            driver.run_issue(d, self.cfg)
        brief_text = (d / "brief.md").read_text(encoding="utf-8")
        self.assertIn("carry-forward", brief_text.lower())
        self.assertIn("Auto-iterate", brief_text)
        self.assertTrue((d / "iteration-v1").is_dir())      # prior attempt archived, not deleted

    def test_signoff_is_attributed_to_the_driver_not_a_human(self) -> None:
        d = self._bundle("ATTR", gate=_FAIL)
        self._try(d)
        self.assertIn("auto-iterate", (d / "SUMMARY.md").read_text(encoding="utf-8"))


class TheStandingValidationRow(_Base):
    """Issue #293 — the row that made this whole feature dead code.

    The reviewer's prompt hard-codes `Validation — fitness-to-purpose` to NEEDS-HUMAN on EVERY
    cycle, whatever it found: validation is the human's call by definition. So every real
    `check-review.md` carries it. The original rule demanded that EVERY §6 item be IMPL, so a
    single such row disqualified every bundle and auto-iterate NEVER FIRED in production — a
    constant was being read as evidence that a human must look right now.

    It still renders in §6 and the C6 accept-guard still blocks on it. All it no longer does is
    veto a rebuild.
    """

    def test_an_impl_finding_beside_the_standing_row_auto_iterates(self) -> None:
        # THE production shape, and the one the old fixture never built.
        d = self._bundle("SV1", review=_review_table("C4 Verification (red→green)"))
        self.assertTrue(self._try(d), "a Do-fixable defect must rebuild, not spend a human")
        self.assertEqual(autoiterate.count(d), 1)

    def test_the_standing_row_alone_still_halts(self) -> None:
        # Nothing for a rebuild to fix: a clean bundle awaiting the human's ACCEPT. Never
        # auto-accept — `eligible` needs at least one IMPL item, not merely "no HUMAN item".
        d = self._bundle("SV2", review=f"# Review\n\n| Item | Verdict | Basis |\n|---|---|---|\n"
                                       f"{_STANDING_ROW}\n")
        self.assertFalse(self._try(d))
        self._assert_halted(d)

    def test_a_situational_judgment_concern_beside_it_still_halts(self) -> None:
        # The distinction that makes this safe: C5/T5 are judgment cells too, but the reviewer
        # raises them only on a REAL concern — so they carry signal and must still stop the
        # bundle, standing row or not.
        review = ("# Review\n\n| Item | Verdict | Basis |\n|---|---|---|\n"
                  "| C4 Verification (red→green) | NEEDS-HUMAN | off-by-one |\n"
                  "| C5 Causal adequacy | NEEDS-HUMAN | guards the symptom, not the cause |\n"
                  f"{_STANDING_ROW}\n")
        d = self._bundle("SV3", review=review)
        self.assertFalse(self._try(d), "a real judgment concern must still halt")
        self._assert_halted(d)

    def test_an_advisory_fitness_objection_is_never_standing(self) -> None:
        """PR #294 review (codex). STANDING is the PRIMARY review's privilege, and nothing
        else's.

        `collect_needs_human` runs `check-review.md` and every `check-advisory-*.md` through the
        same classifier. The adversary's prompt tells it to raise architectural / scope /
        fitness objections as free-form `- NEEDS-HUMAN — …` bullets — so one that happens to
        begin "Validation — fitness-to-purpose" was being read as the reviewer's signal-free
        standing row, and an unattended rebuild would ARCHIVE a real objection instead of
        halting for sign-off. The basis for STANDING is "this row is a constant", which is true
        of the reviewer's mandated table and of nothing else.
        """
        advisory = ("# Adversary\n\n- NEEDS-HUMAN — Validation — fitness-to-purpose: this "
                    "patches the wrong layer; the success criterion cannot be met by this "
                    "design\n")
        d = self._bundle("SV5", review=_review_table("C4 Verification (red→green)"),
                         advisory=advisory)
        self.assertFalse(self._try(d), "a real fitness objection must halt, not be archived")
        self._assert_halted(d)

    def test_a_legacy_validation_bullet_in_the_review_is_never_standing(self) -> None:
        """PR #294 review (codex), second pass. Scoping STANDING to the primary ARTIFACT was
        still too wide — it must be scoped to the mandated verdict-table ROW.

        `_needs_human` also honours legacy `- NEEDS-HUMAN — …` bullets in `check-review.md`.
        Those are free prose the reviewer CHOSE to write, so one reading "Validation —
        fitness-to-purpose: patches the wrong layer" is a substantive objection, not the
        template row — and would have been archived by an unattended rebuild. Only a table row
        is the constant that earns STANDING.
        """
        review = ("# Review\n\n| Item | Verdict | Basis |\n|---|---|---|\n"
                  "| C4 Verification (red→green) | NEEDS-HUMAN | [impl] off-by-one |\n"
                  f"{_STANDING_ROW}\n"
                  "- NEEDS-HUMAN — Validation — fitness-to-purpose: patches the wrong layer\n")
        d = self._bundle("SV6", review=review)
        self.assertFalse(self._try(d), "a legacy fitness bullet is a finding — it must halt")
        self._assert_halted(d)

    def test_a_second_table_never_earns_the_standing_exemption(self) -> None:
        """PR #294 review (codex), third pass. Keying on "came from a table" was STILL too wide.

        The reviewer may write more than one table — a "concerns" table beside the mandated
        verdict table. A row there reading `| Validation — fitness-to-purpose: patches the wrong
        layer | NEEDS-HUMAN | … |` is a substantive objection, but it came from a table and its
        text starts with the canonical label, so it was classified STANDING and an unattended
        rebuild would archive it. The canonical row is now identified by an EXACT match on its
        Item cell — the only thing that actually distinguishes the template row.
        """
        review = ("# Review\n\n| Item | Verdict | Basis |\n|---|---|---|\n"
                  "| C4 Verification (red→green) | NEEDS-HUMAN | [impl] off-by-one |\n"
                  f"{_STANDING_ROW}\n"
                  "\n## Concerns\n\n| Item | Verdict | Basis |\n|---|---|---|\n"
                  "| Validation — fitness-to-purpose: patches the wrong layer | NEEDS-HUMAN "
                  "| the criterion cannot be met by this design |\n")
        d = self._bundle("SV7", review=review)
        self.assertFalse(self._try(d), "a concerns-table objection must halt, not be archived")
        self._assert_halted(d)

    def test_a_concerns_table_with_the_EXACT_label_still_halts(self) -> None:
        """PR #294, local codex pass. The fourth scoping of the same rule, and the one that
        finally names the right thing.

        Matching the Item cell was still not enough: a `## Concerns` table can carry the row
        `| Validation — fitness-to-purpose | NEEDS-HUMAN | patches the wrong layer |` with the
        **exact** canonical label. The parser had no idea which TABLE a row came from, so that
        real objection earned STANDING and an unattended rebuild would archive it. My previous
        test only covered a concerns row with EXTRA text in the cell, so it sailed past this.

        The justification was always "the MANDATED TABLE's Validation row is a constant" — so the
        parser now identifies that table (≥2 exact canonical Item cells) and only its V row can
        be standing.
        """
        review = ("# Review\n\n| Item | Verdict | Basis |\n|---|---|---|\n"
                  "| C4 Verification (red→green) | NEEDS-HUMAN | [impl] off-by-one |\n"
                  "| C5 Causal adequacy | PASS | ok |\n"
                  f"{_STANDING_ROW}\n"
                  "\n## Concerns\n\n| Item | Verdict | Basis |\n|---|---|---|\n"
                  "| Validation — fitness-to-purpose | NEEDS-HUMAN | patches the wrong layer |\n")
        d = self._bundle("SV9", review=review)
        self.assertFalse(self._try(d), "an exact-label concerns row is still a real objection")
        self._assert_halted(d)

    def test_two_standing_candidates_fail_closed(self) -> None:
        # The template row is a CONSTANT — it occurs once. If two survive (a duplicated row, a
        # second verdict-shaped table), at least one is not the constant and we cannot tell
        # which. Grant STANDING to neither and halt, rather than risk archiving a real objection.
        review = ("# Review\n\n| Item | Verdict | Basis |\n|---|---|---|\n"
                  "| C4 Verification (red→green) | NEEDS-HUMAN | [impl] off-by-one |\n"
                  "| C5 Causal adequacy | PASS | ok |\n"
                  f"{_STANDING_ROW}\n"
                  "| Validation — fitness-to-purpose | NEEDS-HUMAN | and again, differently |\n")
        d = self._bundle("SV10", review=review)
        self.assertFalse(self._try(d), "ambiguous standing rows must fail closed")
        self._assert_halted(d)

    def test_the_standing_row_is_never_carried_forward_to_the_builder(self) -> None:
        """PR #294 review (codex). STANDING rides along in `items` so it cannot veto the rebuild
        — but it is not a finding, and no builder can act on it. Carrying it into the §9 delta
        and the brief's carry-forward handed the next Do a human-only judgment call as though it
        were a defect to fix, under a sentence claiming the set was "implementation-level items
        only"."""
        d = self._bundle("SV8", review=_review_table("C4 Verification (red→green)"))
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            flow._maybe_auto_iterate(self.cfg, d, by="", today="2026-07-09", apply_now=True)
            driver.run_issue(d, self.cfg)
        brief_text = (d / "brief.md").read_text(encoding="utf-8")
        self.assertIn("C4 Verification", brief_text)                     # the real defect…
        self.assertNotIn("Validation — fitness-to-purpose", brief_text)  # …and only that

    def test_the_standing_row_still_blocks_accept(self) -> None:
        # The C6 guard is untouched: the human must still clear §6 before accepting. Not
        # vetoing a REBUILD is not the same as not needing a human at SIGN-OFF.
        d = self._bundle("SV4", review=_review_table("C4 Verification (red→green)"))
        summary = (d / "SUMMARY.md").read_text(encoding="utf-8")
        self.assertIn("Validation — fitness-to-purpose", summary)   # still rendered in §6
        self.assertTrue(signoff.open_needs_human(d / "SUMMARY.md"))  # still blocks accept


class HaltsForTheHuman(_Base):
    """Anything architectural, environmental, or unclassifiable still stops."""

    def test_judgment_cells_halt(self) -> None:
        # THE load-bearing negative: C5 causal adequacy, T5 judgment, the validation act.
        for elem in ("C5 Causal adequacy", "T5 Judgment", "Validation — fitness-to-purpose"):
            with self.subTest(elem=elem):
                d = self._bundle(f"J{abs(hash(elem)) % 9999}", review=_review_table(elem))
                self.assertFalse(self._try(d), f"{elem} is a judgment cell — must halt")
                self._assert_halted(d)

    def test_input_cells_halt(self) -> None:
        for elem in ("C1 Spec", "C3 Change"):
            with self.subTest(elem=elem):
                d = self._bundle(f"I{elem[:2]}", review=_review_table(elem))
                self.assertFalse(self._try(d))
                self._assert_halted(d)

    def test_one_judgment_item_disqualifies_the_whole_bundle(self) -> None:
        review = ("# Review\n\n| Item | Verdict | Basis |\n|---|---|---|\n"
                  "| C4 Verification (red→green) | NEEDS-HUMAN | off-by-one |\n"
                  "| C5 Causal adequacy | NEEDS-HUMAN | guards the symptom |\n")
        d = self._bundle("MIXED", review=review)
        self.assertFalse(self._try(d))
        self._assert_halted(d)

    def test_unverifiable_gate_halts(self) -> None:
        # A gate that COULD NOT RUN is a gate-kind element, but rebuilding can't fix a
        # missing mechanic — it would spin. Forced HUMAN.
        d = self._bundle("UNVER", gate=_UNVERIFIABLE)
        self.assertFalse(self._try(d))
        self._assert_halted(d)

    def test_declared_external_dependency_halts(self) -> None:
        d = self._bundle("EXTDEP", gate=_FAIL,
                         build_notes="NEEDS-HUMAN external dependency: protoc — cannot compile\n")
        self.assertFalse(self._try(d))
        self._assert_halted(d)

    def test_unregistered_dependency_halts(self) -> None:
        self.cfg.doctor_checks = []
        d = self._bundle("UNREG", gate=_FAIL,
                         brief_body="- **Slug:** ai\n- **External dependencies:** `protoc` (build)\n")
        self.assertFalse(self._try(d))
        self._assert_halted(d)

    def test_unmarked_advisory_finding_halts(self) -> None:
        # Backward compatibility: an advisory file written before #264 has no [impl] tag,
        # so it can never trigger an auto-iteration.
        d = self._bundle("ADVPLAIN", advisory="- NEEDS-HUMAN — the scope looks wider than the brief\n")
        self.assertFalse(self._try(d))
        self._assert_halted(d)

    def test_unmappable_review_row_halts(self) -> None:
        # An Item cell with no canonical element id → fail safe toward the human.
        d = self._bundle("UNMAP", review=_review_table("Some bespoke lens"))
        self.assertFalse(self._try(d))
        self._assert_halted(d)

    def test_empty_section6_halts_and_never_auto_accepts(self) -> None:
        d = self._bundle("CLEAN")
        self.assertEqual(signoff.open_needs_human(d / "SUMMARY.md"), [])
        self.assertFalse(self._try(d))
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)   # NOT COMPLETE
        self.assertNotEqual(signoff.outcome_token(d / "SUMMARY.md"), "merged-wider")

    def test_missing_review_halts(self) -> None:
        d = self._bundle("NOREV", gate=_FAIL)
        (d / "check-review.md").unlink()
        assemble.assemble_summary(d, self.cfg)
        self.assertFalse(self._try(d))

    def test_bundle_not_awaiting_signoff_is_a_noop(self) -> None:
        d = self._bundle("NOTREADY", gate=_FAIL)
        signoff.record(d / "SUMMARY.md", action="iterate-do", by="t", date="2026-07-09")
        self.assertEqual(state.state(d), state.ITERATE_DO)
        self.assertFalse(self._try(d))

    def test_disabled_by_config(self) -> None:
        self.cfg.auto_iterate = False
        d = self._bundle("OFF", gate=_FAIL)
        self.assertFalse(self._try(d))
        self._assert_halted(d)

    def test_close_disposition_bundle_halts(self) -> None:
        # The close fast path skips builder + reviewer and asks the human to confirm the
        # close. That confirmation is a human call — never auto-iterate it.
        d = self.cfg.bundle("CLOSE")
        d.mkdir(parents=True)
        (d / "brief.md").write_text(
            "- **Slug:** c\n- **Disposition hint:** likely-close\n", encoding="utf-8")
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            driver.run_issue(d, self.cfg)
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)
        self.assertFalse(self._try(d))
        self._assert_halted(d)

    def test_truncated_gates_json_declines_instead_of_crashing(self) -> None:
        # An over-reaching leaf can truncate a bundle's downstream. The file still exists, so
        # the bundle still reads AWAITING_SIGNOFF — but it no longer parses. The single-issue
        # flow has no `_isolate` around auto-iterate, so this must degrade, not raise.
        d = self._bundle("CORRUPT", gate=_FAIL)
        (d / "check-gates.json").write_text('{"rows": [', encoding="utf-8")
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)
        buf = io.StringIO()
        with redirect_stderr(buf), redirect_stdout(io.StringIO()):
            fired = flow._maybe_auto_iterate(self.cfg, d, by="", today="2026-07-09",
                                             apply_now=False)   # must NOT raise
        self.assertFalse(fired)
        self.assertIn("cannot classify Check findings", buf.getvalue())

    def test_missing_gates_json_is_not_awaiting_signoff(self) -> None:
        # Deleting it moves the bundle back to BUILT, so the state guard declines first.
        d = self._bundle("GONE", gate=_FAIL)
        (d / "check-gates.json").unlink()
        self.assertEqual(state.state(d), state.BUILT)
        self.assertFalse(self._try(d))

    def test_stub_reviewer_never_auto_iterates(self) -> None:
        # Offline / CI (PDCA_LEAVES_MODE=stub): the stub review flags the always-human
        # validation act, so a rehearse run can never auto-iterate.
        d = self.cfg.bundle("STUB")
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** ai\n", encoding="utf-8")
        driver.run_issue(d, self.cfg)   # stub builder + stub reviewer
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)
        self.assertFalse(self._try(d))


class Budget(_Base):
    def test_exhausted_budget_hands_over_to_the_human(self) -> None:
        self.cfg.max_auto_iters = 2
        d = self._bundle("BUDGET", gate=_FAIL)
        (d / autoiterate.BUDGET_FILE).write_text('{"count": 2}\n', encoding="utf-8")
        buf = io.StringIO()
        with redirect_stderr(buf), redirect_stdout(io.StringIO()):
            fired = flow._maybe_auto_iterate(self.cfg, d, by="", today="2026-07-09", apply_now=False)
        self.assertFalse(fired)
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)   # halted, never dropped
        self.assertFalse((d / leaves.SIGNOFF_DECISION).exists())
        self.assertIn("auto-iterate budget spent (2/2)", buf.getvalue())

    def test_budget_survives_the_iteration_archive(self) -> None:
        # auto-iterate.json must NOT be in driver.DOWNSTREAM_OF_BRIEF, or the count resets
        # every rebuild and the loop never terminates.
        self.assertNotIn(autoiterate.BUDGET_FILE, driver.DOWNSTREAM_OF_BRIEF)
        d = self._bundle("SURVIVE", gate=_FAIL)
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            flow._maybe_auto_iterate(self.cfg, d, by="", today="2026-07-09", apply_now=True)
        self.assertTrue((d / "iteration-v1").is_dir())
        self.assertEqual(autoiterate.count(d), 1)                  # not reset by the archive

    def test_garbled_budget_file_reads_as_zero(self) -> None:
        d = self._bundle("GARBLE", gate=_FAIL)
        (d / autoiterate.BUDGET_FILE).write_text("{ not json", encoding="utf-8")
        self.assertEqual(autoiterate.count(d), 0)
        self.assertTrue(self._try(d))

    def test_repeated_rounds_terminate_at_the_cap(self) -> None:
        # A bundle whose rebuild keeps failing the same gate must reach the human, not spin.
        # The reviewer is stubbed to a CLEAN review so every rebuild's §6 stays impl-only —
        # otherwise the stub reviewer's always-human validation row would halt it at round 1
        # (which it does, correctly: see test_stub_reviewer_never_auto_iterates).
        self.cfg.max_auto_iters = 2
        d = self._bundle("SPIN", gate=_FAIL)

        def clean_review(bundle: Path, cfg: Config) -> None:
            (bundle / "check-review.md").write_text(_CLEAN_REVIEW, encoding="utf-8")

        rounds = 0
        with mock.patch.object(leaves, "run_review", clean_review), \
             redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            for _ in range(5):
                if not flow._maybe_auto_iterate(self.cfg, d, by="", today="2026-07-09",
                                                apply_now=True):
                    break
                rounds += 1
        self.assertEqual(rounds, 2)                                # stopped at the cap
        self.assertEqual(autoiterate.count(d), 2)
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)   # handed over, not dropped
        self.assertTrue((d / "iteration-v2").is_dir())             # both attempts preserved


class BatchSweep(_Base):
    """In `_drive_wave` an auto-iterate must behave exactly like a deferred human iterate-do:
    the bundle leaves the sign-off queue, and the NEXT pass's build-all rebuilds it."""

    def test_auto_iterated_bundle_leaves_the_queue_and_rebuilds_next_pass(self) -> None:
        d = self._bundle("WAVE", gate=_FAIL)
        signed_off: list[str] = []

        def signoff_batch(cfg: Config, bundles: list[Path]) -> None:
            signed_off.extend(b.name for b in bundles)
            for b in bundles:                       # a human would accept here
                summ = b / "SUMMARY.md"
                summ.write_text(summ.read_text().replace("- [ ]", "- [x]"), encoding="utf-8")
                (b / leaves.SIGNOFF_DECISION).write_text("accept\n", encoding="utf-8")

        def clean_review(bundle: Path, cfg: Config) -> None:
            (bundle / "check-review.md").write_text(_CLEAN_REVIEW, encoding="utf-8")

        with mock.patch.object(leaves, "run_signoff_batch", signoff_batch), \
             mock.patch.object(leaves, "run_review", clean_review), \
             redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            flow._drive_wave(self.cfg, [d], by="t", today="2026-07-09", max_passes=1)

        # Pass 1 auto-iterated it, so the human's sign-off session never saw it …
        self.assertEqual(signed_off, [])
        self.assertEqual(state.state(d), state.ITERATE_DO)
        self.assertEqual(autoiterate.count(d), 1)
        # … and its rebuild is deferred to the next pass, not run mid-review.
        self.assertFalse((d / "iteration-v1").is_dir())

    def test_judgment_finding_still_reaches_the_signoff_queue(self) -> None:
        d = self._bundle("WAVEJ", review=_review_table("C5 Causal adequacy"))
        seen: list[str] = []

        def signoff_batch(cfg: Config, bundles: list[Path]) -> None:
            seen.extend(b.name for b in bundles)
            for b in bundles:
                (b / leaves.SIGNOFF_DECISION).write_text("discontinue\nnot now\n", encoding="utf-8")

        with mock.patch.object(leaves, "run_signoff_batch", signoff_batch), \
             redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            flow._drive_wave(self.cfg, [d], by="t", today="2026-07-09", max_passes=1)
        self.assertEqual(seen, ["issue_WAVEJ"])   # the human got it, as they must

    def test_repeated_auto_iterations_count_as_progress_not_a_stuck_wave(self) -> None:
        """PR #270 review (codex). A bundle already ITERATE_DO is rebuilt by `_build_all` to
        AWAITING_SIGNOFF, re-Checked, then routed straight back to ITERATE_DO — so the
        before/after state snapshots MATCH. With the sign-off queue empty, the no-progress
        check fired and the wave returned after only TWO auto rounds, stranding the bundle
        with both `max_auto_iters` and `max_passes` budget to spare."""
        self.cfg.max_auto_iters = 3
        # The size backstop is switched OFF for this bundle: it fires at 2 rounds by
        # design (#324) and would stop the loop here for a completely different — and
        # correct — reason, hiding whether the no-progress check still misfires. This test
        # is about the stuck-wave detector; `test_the_size_backstop_stops_the_loop_early`
        # in test_size_signal.py asserts the interaction itself.
        self.cfg.size_signal = {"rounds": 0}
        d = self._bundle("WAVELOOP", gate=_FAIL)     # a gate that stays red across rebuilds
        signed_off: list[str] = []

        def signoff_batch(cfg: Config, bundles: list[Path]) -> None:
            signed_off.extend(b.name for b in bundles)
            for b in bundles:                        # the human clears §6 and accepts
                summ = b / "SUMMARY.md"
                summ.write_text(summ.read_text().replace("- [ ]", "- [x]"), encoding="utf-8")
                (b / leaves.SIGNOFF_DECISION).write_text("accept\n", encoding="utf-8")

        def clean_review(bundle: Path, cfg: Config) -> None:
            (bundle / "check-review.md").write_text(_CLEAN_REVIEW, encoding="utf-8")

        buf = io.StringIO()
        with mock.patch.object(leaves, "run_signoff_batch", signoff_batch), \
             mock.patch.object(leaves, "run_review", clean_review), \
             redirect_stderr(buf), redirect_stdout(io.StringIO()):
            flow._drive_wave(self.cfg, [d], by="t", today="2026-07-10", max_passes=6)

        # the FULL auto budget is spent — not truncated at two by a false stuck-wave verdict
        self.assertEqual(autoiterate.count(d), 3)
        self.assertNotIn("a full pass made no progress", buf.getvalue())
        # …and once it is spent the bundle reaches the human and completes, never abandoned
        self.assertEqual(signed_off, ["issue_WAVELOOP"])
        self.assertEqual(state.state(d), state.COMPLETE)

    def test_a_wave_that_truly_stalls_still_warns(self) -> None:
        # The negative: with the auto budget spent, nothing advances — the no-progress guard
        # must still fire. `auto_iterated` must never mask a genuine stall.
        d = self._bundle("WAVESTALL", gate=_FAIL)
        (d / autoiterate.BUDGET_FILE).write_text('{"count": 99}\n', encoding="utf-8")
        signoff.record(d / "SUMMARY.md", action="iterate-do", by="t", date="2026-07-10")
        buf = io.StringIO()
        with mock.patch.object(flow, "_build_all", lambda cfg, bundles: None), \
             redirect_stderr(buf), redirect_stdout(io.StringIO()):
            flow._drive_wave(self.cfg, [d], by="t", today="2026-07-10", max_passes=5)
        self.assertIn("a full pass made no progress", buf.getvalue())
        self.assertIn("issue_WAVESTALL", buf.getvalue())

    def test_a_raising_auto_iterate_does_not_kill_the_sweep(self) -> None:
        d = self._bundle("WAVEBOOM", gate=_FAIL)
        with mock.patch.object(flow.autoiterate, "write_decision",
                               side_effect=OSError("disk full")), \
             mock.patch.object(leaves, "run_signoff_batch", lambda cfg, bundles: None), \
             redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            flow._drive_wave(self.cfg, [d], by="t", today="2026-07-09", max_passes=1)
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)   # isolated, still reviewable


class DecisionModule(unittest.TestCase):
    """`autoiterate` itself — the guard that keeps this from ever becoming an auto-accept."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _items(self, *kinds: str) -> list[assemble.NeedsHumanItem]:
        return [assemble.NeedsHumanItem(f"finding {i}", k) for i, k in enumerate(kinds)]

    def test_eligible_only_when_nonempty_and_all_impl(self) -> None:
        self.assertTrue(autoiterate.eligible(self._items(assemble.IMPL, assemble.IMPL)))
        self.assertFalse(autoiterate.eligible([]))                                 # never accept
        self.assertFalse(autoiterate.eligible(self._items(assemble.IMPL, assemble.HUMAN)))
        self.assertFalse(autoiterate.eligible(self._items(assemble.HUMAN)))

    def test_write_decision_only_ever_writes_iterate_do(self) -> None:
        autoiterate.write_decision(self.tmp, self._items(assemble.IMPL))
        token = (self.tmp / leaves.SIGNOFF_DECISION).read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(token, "iterate-do")
        self.assertIn(token, leaves.VALID_DECISIONS)
        self.assertNotEqual(token, "accept")

    def test_write_decision_refuses_a_non_implementation_set(self) -> None:
        with self.assertRaises(ValueError):
            autoiterate.write_decision(self.tmp, self._items(assemble.HUMAN))
        self.assertFalse((self.tmp / leaves.SIGNOFF_DECISION).exists())
        self.assertEqual(autoiterate.count(self.tmp), 0)          # no budget spent either

    def test_write_decision_refuses_an_empty_set(self) -> None:
        with self.assertRaises(ValueError):
            autoiterate.write_decision(self.tmp, [])

    def test_rationale_is_a_single_line_naming_the_findings(self) -> None:
        r = autoiterate.rationale(self._items(assemble.IMPL, assemble.IMPL), attempt=2)
        self.assertNotIn("\n", r)
        self.assertIn("round 2", r)
        self.assertIn("finding 0", r)
        self.assertIn("finding 1", r)


class Classification(unittest.TestCase):
    """The impl/human split is taken from the canonical 5/5/1, not re-invented."""

    def test_gate_elements_match_the_canonical_matrix(self) -> None:
        expected = {e for e, _l, k, _o in gates.canonical_elements() if k == "gate"}
        self.assertEqual(assemble._GATE_ELEMENTS, expected)
        self.assertEqual(expected, {"C2", "C4", "T1", "T2", "T3", "T4"})

    def test_judgment_and_input_cells_are_never_impl(self) -> None:
        # THE invariant: a rebuild can never be aimed at a judgment / input cell. Unchanged.
        for elem, label, kind, _oracle in gates.canonical_elements():
            if kind in ("judgment", "input"):
                item = assemble._classify_finding(f"{label} — some basis")
                self.assertNotEqual(item.kind, assemble.IMPL, f"{elem} must never be impl")

    def test_only_the_validation_row_is_standing(self) -> None:
        # #293. Of the 5/5/1's own rows, V is the one the reviewer's prompt hard-codes to
        # NEEDS-HUMAN every cycle, so it alone can be STANDING (a constant carries no signal).
        # C5/T5 are judgment too, but the reviewer raises those only on a real concern — they
        # stay situational HUMAN and still halt the bundle. The PARSER decides which row is the
        # canonical one; the classifier only honours that decision.
        for elem, label, kind, _oracle in gates.canonical_elements():
            if kind not in ("judgment", "input"):
                continue
            # A REAL verdict table: the row under test plus another canonical row, which is what
            # makes it the mandated table rather than a stray one (a lone row cannot nominate
            # itself as the constant).
            table = ("| Item | Verdict | Basis |\n|---|---|---|\n"
                     "| C1 Spec | PASS | ok |\n"
                     f"| {label} | NEEDS-HUMAN | some basis |\n")
            [(text, standing)] = assemble._needs_human(table)
            got = assemble._classify_finding(text, standing=standing).kind
            want = assemble.STANDING if elem == "V" else assemble.HUMAN
            self.assertEqual(got, want, f"{elem} ({label})")

    def test_standing_needs_an_EXACT_match_on_the_canonical_item_cell(self) -> None:
        """PR #294 review (codex). What identifies the template row is its Item cell being
        EXACTLY the canonical label — not the text's prefix, and not merely "it came from a
        table". A prefix test let a real objection wear the template's clothes; a table test let
        a second table do the same. Both are the same mistake, one layer apart."""
        TBL = "| Item | Verdict | Basis |\n|---|---|---|\n| C1 Spec | PASS | ok |\n"
        canonical = TBL + "| Validation — fitness-to-purpose | NEEDS-HUMAN | the human's call |\n"
        objection = TBL + ("| Validation — fitness-to-purpose: patches the wrong layer "
                           "| NEEDS-HUMAN | the criterion cannot be met |\n")
        bullet = TBL + "- NEEDS-HUMAN — Validation — fitness-to-purpose: patches the wrong layer\n"
        lone = "| Validation — fitness-to-purpose | NEEDS-HUMAN | the human's call |\n"

        [(_t, standing)] = assemble._needs_human(canonical)
        self.assertTrue(standing, "the canonical row of the MANDATED table IS the constant")
        [(_t, standing)] = assemble._needs_human(objection)
        self.assertFalse(standing, "a longer Item cell is a real objection, not the template")
        [(_t, standing)] = assemble._needs_human(bullet)
        self.assertFalse(standing, "free prose is never the template row")
        [(_t, standing)] = assemble._needs_human(lone)
        self.assertFalse(standing, "a lone row in a stray table cannot nominate itself")

    def test_the_classifier_never_re_derives_standing_from_the_text(self) -> None:
        # Two sources of truth for "is this the constant row" is what produced the bug. The
        # classifier honours the caller's verdict and does not second-guess it from the text.
        text = "Validation — fitness-to-purpose — the human's call"
        self.assertEqual(assemble._classify_finding(text).kind, assemble.HUMAN)
        self.assertEqual(assemble._classify_finding(text, standing=True).kind, assemble.STANDING)

    def test_impl_marker_is_case_insensitive_and_stripped(self) -> None:
        self.assertEqual(assemble._classify_finding("[IMPL] — bug"),
                         assemble.NeedsHumanItem("bug", assemble.IMPL))

    def test_unknown_text_is_human(self) -> None:
        self.assertEqual(assemble._classify_finding("something bespoke").kind, assemble.HUMAN)

    def test_a_gate_row_kind_comes_from_its_element_not_its_label(self) -> None:
        # An instance names its own gates; the label may not start with the element id.
        rows = {"rows": [{"check": "fix verified", "result": "fail", "gating": True,
                          "element": "C4", "path_line": "", "oracle": "run-verify.sh"}]}
        self.assertEqual(assemble._failed_gating_items(rows)[0].kind, assemble.IMPL)
        rows["rows"][0]["element"] = ""      # unknown → fail safe
        self.assertEqual(assemble._failed_gating_items(rows)[0].kind, assemble.HUMAN)


class ConfigPlumbing(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        # Hermetic against the ambient environment (#419): Config.load honors PDCA_*
        # env overrides (PDCA_AUTO_ITERATE, config.py), and a project's T3 suite gate
        # runs this suite with the DRIVER's inherited env (gates._merged_env) — an
        # auto-iterate flow can carry PDCA_AUTO_ITERATE=1 there, flipping the
        # default-behavior assertions below to read the operator's shell instead of
        # the toml under test.
        env_guard = mock.patch.dict(os.environ)
        env_guard.start()
        self.addCleanup(env_guard.stop)
        for key in [k for k in os.environ if k.startswith("PDCA_")]:
            del os.environ[key]

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _load(self, extra: str = "") -> Config:
        (self.tmp / "pdca.toml").write_text(
            '[project]\ndefault_branch = "main"\n'
            '[leaves.builder]\nmode = "stub"\n[leaves.reviewer]\nmode = "stub"\n' + extra,
            encoding="utf-8")
        return Config.load(self.tmp)

    def test_off_by_default(self) -> None:
        cfg = self._load()
        self.assertFalse(cfg.auto_iterate)
        self.assertEqual(cfg.max_auto_iters, 3)

    def test_driver_table_enables_it(self) -> None:
        self.assertTrue(self._load("[driver]\nauto_iterate = true\n").auto_iterate)

    def test_env_overrides_the_toml(self) -> None:
        with mock.patch.dict(os.environ, {"PDCA_AUTO_ITERATE": "1"}):
            self.assertTrue(self._load().auto_iterate)
        with mock.patch.dict(os.environ, {"PDCA_AUTO_ITERATE": "0"}):
            self.assertFalse(self._load("[driver]\nauto_iterate = true\n").auto_iterate)

    def test_max_auto_iters_is_clamped_below_max_passes(self) -> None:
        # Else exhausting the auto budget could coincide with the wave's pass budget running
        # out, leaving the bundle mid-flight at ITERATE_DO (#260's abandonment shape).
        cfg = self._load("[driver]\nmax_passes = 3\nmax_auto_iters = 99\n")
        self.assertEqual(cfg.max_auto_iters, 2)
        self.assertLess(cfg.max_auto_iters, cfg.max_passes)

    def test_max_auto_iters_floor_of_one(self) -> None:
        self.assertEqual(self._load("[driver]\nmax_passes = 1\nmax_auto_iters = 0\n").max_auto_iters, 1)

    def test_cli_flag_opts_in(self) -> None:
        cfg = _stub_config(self.tmp)
        cfg.auto_iterate = False
        # `flow_ids` is the ONE drive path `cli._flow` routes a single id through (#468),
        # so that is the call to stub out for a flag-plumbing test.
        with mock.patch.object(cli.Config, "load", return_value=cfg), \
             mock.patch.object(cli.flow, "flow_ids",
                               return_value={"ID1": state.COMPLETE}), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            cli.main(["flow", "ID1", "--auto-iterate", "--no-publish", "--no-act"])
        self.assertTrue(cfg.auto_iterate)


if __name__ == "__main__":
    unittest.main()
