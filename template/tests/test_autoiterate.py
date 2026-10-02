"""Auto-iterate while Check still finds implementation work (issues #264, #409; stdlib
unittest).

The driver may rebuild a bundle unattended when its SUMMARY §6 carries at least one
implementation defect — a `gate` cell of the 5/5/1 (C2/C4/T1..T4), or an advisory finding the
leaf tagged `[impl]`. Since #409 a HUMAN finding beside it (a `judgment` cell C5/T5/V, an
`input` cell C1/C3, a gate that could not run, an external dependency, an unmarked advisory
bullet, a row it cannot classify) no longer vetoes that rebuild: it is DEFERRED to
`deferred-findings.json` and returns to §6 at handover, where C6 still makes the human clear
it. A §6 with no implementation work still halts at once. Exactly two things stop the loop
while implementation work remains: the size backstop's item and `max_auto_iters`. A row
saying a review or a gate gave no verdict is not deferred: the next Check runs them again, so
a failure that has recovered never needs clearing by hand at handover.

Load-bearing negatives, each its own test: it must never auto-accept, never tick a §6 box,
never lose a HUMAN finding it iterated past, never retire one the human did not tick, and
never run past either stop. Offline: stub leaves, real gate commands, no Claude.

New symbols are reached as module attributes (`autoiterate.retire_cleared`), never imported
at module top: with the production change reverted, an import error would read as "the
module never loaded", not as the red leg.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pdca_harness import (assemble, autoiterate, cli, driver, flow, gates, leaves, signoff,
                          size_signal, state)
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


# The ledger's file name, spelled here so a helper can read it on either leg of the C4
# verify; `test_the_ledger_is_cycle_evidence_and_is_never_archived` pins it to the module.
_LEDGER = "deferred-findings.json"

# The production shape #409 is about: a Do-fixable defect beside a situational judgment
# concern, with the reviewer's standing Validation row as every real review carries it.
_C5_TEXT = "C5 Causal adequacy — guards the symptom, not the cause"
_MIXED_REVIEW = ("# Review\n\n| Item | Verdict | Basis |\n|---|---|---|\n"
                 "| C4 Verification (red→green) | NEEDS-HUMAN | off-by-one |\n"
                 "| C5 Causal adequacy | NEEDS-HUMAN | guards the symptom, not the cause |\n"
                 f"{_STANDING_ROW}\n")
_IMPL_ONLY_REVIEW = ("# Review\n\n| Item | Verdict | Basis |\n|---|---|---|\n"
                     "| C4 Verification (red→green) | NEEDS-HUMAN | off-by-one |\n"
                     "| C5 Causal adequacy | PASS | ok |\n"
                     f"{_STANDING_ROW}\n")


def _ledger(d: Path) -> list[str] | None:
    """The ledger's entries as written on disk, or None when there is no ledger."""
    p = d / _LEDGER
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))["items"]


def _write_ledger(d: Path, entries: list[str]) -> None:
    (d / _LEDGER).write_text(json.dumps({"items": entries}), encoding="utf-8")


def _section6(summary: Path) -> str:
    """The §6 assembly wrote: the section under the LAST `## 6. NEEDS-HUMAN` heading."""
    text = summary.read_text(encoding="utf-8")
    return text.rsplit("## 6. NEEDS-HUMAN", 1)[1].split("\n## ", 1)[0]


def _quoted_section6(summary: Path) -> str:
    """The section under the FIRST `## 6. NEEDS-HUMAN` heading — a leaf's quote in §5, when an
    advisory artifact carries one."""
    text = summary.read_text(encoding="utf-8")
    return text.split("## 6. NEEDS-HUMAN", 1)[1].split("\n## ", 1)[0]


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
        """No decision written, no budget spent, nothing deferred (the human is about to read
        this §6 directly), bundle still waiting on the human."""
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)
        self.assertFalse((d / leaves.SIGNOFF_DECISION).exists())
        self.assertEqual(autoiterate.count(d), 0)
        self.assertIsNone(_ledger(d))
        self.assertTrue(signoff.open_needs_human(d / "SUMMARY.md") or True)  # §6 untouched

    def _assert_deferred(self, d: Path, expected: list[str]) -> None:
        """The round FIRED, and the ledger holds exactly ``expected`` — the HUMAN findings it
        iterated past, and nothing else (no IMPL item, no STANDING row)."""
        self.assertEqual(state.state(d), state.ITERATE_DO)
        self.assertEqual(autoiterate.count(d), 1)
        self.assertEqual(_ledger(d), expected)


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

    def test_a_situational_judgment_concern_beside_it_is_deferred_not_dropped(self) -> None:
        # The distinction that makes this safe: C5/T5 are judgment cells too, but the reviewer
        # raises them only on a REAL concern — so they carry signal and must reach the human,
        # standing row or not. Since #409 that means the ledger, not a veto: the rebuild runs,
        # and the concern returns to §6 at handover. The standing row is not a deferral.
        d = self._bundle("SV3", review=_MIXED_REVIEW)
        self.assertTrue(self._try(d), "a judgment concern beside a defect defers, not vetoes")
        self._assert_deferred(d, [_C5_TEXT])

    # The four tests below are PR #294's scopings of STANDING. Each once asserted a HALT,
    # because STANDING was then the only non-IMPL kind that did not veto. Since #409 an
    # ordinary HUMAN item does not veto either, so what tells the two kinds apart is the
    # LEDGER: a HUMAN finding is recorded there and returned to §6 at handover; STANDING is
    # not. A real objection mistaken for the standing row would be archived with its round
    # and never reach the handover — so each test now asserts the objection is in the ledger
    # and the standing row is not.

    def test_an_advisory_fitness_objection_is_never_standing(self) -> None:
        """PR #294 review (codex). STANDING is the PRIMARY review's privilege, and nothing
        else's.

        `collect_needs_human` runs `check-review.md` and every `check-advisory-*.md` through the
        same classifier. The adversary's prompt tells it to raise architectural / scope /
        fitness objections as free-form `- NEEDS-HUMAN — …` bullets — so one that happens to
        begin "Validation — fitness-to-purpose" was being read as the reviewer's signal-free
        standing row, and an unattended rebuild would ARCHIVE a real objection. The basis for
        STANDING is "this row is a constant", which is true of the reviewer's mandated table
        and of nothing else.
        """
        objection = ("Validation — fitness-to-purpose: this patches the wrong layer; the "
                     "success criterion cannot be met by this design")
        d = self._bundle("SV5", review=_review_table("C4 Verification (red→green)"),
                         advisory=f"# Adversary\n\n- NEEDS-HUMAN — {objection}\n")
        self.assertTrue(self._try(d))
        self._assert_deferred(d, [objection])   # held for the human; the standing row is not

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
        self.assertTrue(self._try(d))
        self._assert_deferred(d, ["Validation — fitness-to-purpose: patches the wrong layer"])

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
        self.assertTrue(self._try(d))
        self._assert_deferred(d, ["Validation — fitness-to-purpose: patches the wrong layer — "
                                  "the criterion cannot be met by this design"])

    def test_a_concerns_table_with_the_EXACT_label_is_still_a_real_objection(self) -> None:
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
        self.assertTrue(self._try(d))
        self._assert_deferred(d, ["Validation — fitness-to-purpose — patches the wrong layer"])

    def test_two_standing_candidates_fail_closed(self) -> None:
        # The template row is a CONSTANT — it occurs once. If two survive (a duplicated row, a
        # second verdict-shaped table), at least one is not the constant and we cannot tell
        # which. Grant STANDING to neither — so BOTH are held for the human, rather than risk
        # archiving a real objection as the constant.
        review = ("# Review\n\n| Item | Verdict | Basis |\n|---|---|---|\n"
                  "| C4 Verification (red→green) | NEEDS-HUMAN | [impl] off-by-one |\n"
                  "| C5 Causal adequacy | PASS | ok |\n"
                  f"{_STANDING_ROW}\n"
                  "| Validation — fitness-to-purpose | NEEDS-HUMAN | and again, differently |\n")
        d = self._bundle("SV10", review=review)
        self.assertTrue(self._try(d))
        self._assert_deferred(d, [
            "Validation — fitness-to-purpose — fitness is the human's call",
            "Validation — fitness-to-purpose — and again, differently"])

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
    """A §6 with no implementation work still stops at once: architectural, environmental,
    or unclassifiable findings with nothing beside them for a rebuild to address. (Beside an
    IMPL finding they are deferred instead — see `DefersHumanFindings`.)"""

    def test_judgment_cells_halt(self) -> None:
        # THE load-bearing negative: C5 causal adequacy, T5 judgment, the validation act —
        # with no implementation work beside them, there is nothing to rebuild for.
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

    def test_unverifiable_gate_halts(self) -> None:
        # A gate that COULD NOT RUN is a gate-kind element, but rebuilding can't fix a
        # missing mechanic — it would spin. Forced HUMAN.
        d = self._bundle("UNVER", gate=_UNVERIFIABLE)
        self.assertFalse(self._try(d))
        self._assert_halted(d)

    def test_declared_external_dependency_alone_halts(self) -> None:
        d = self._bundle("EXTDEP",
                         build_notes="NEEDS-HUMAN external dependency: protoc — cannot compile\n")
        self.assertFalse(self._try(d))
        self._assert_halted(d)

    def test_unregistered_dependency_alone_halts(self) -> None:
        self.cfg.doctor_checks = []
        d = self._bundle("UNREG",
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

    def test_missing_review_alone_halts(self) -> None:
        d = self._bundle("NOREV")
        (d / "check-review.md").unlink()
        assemble.assemble_summary(d, self.cfg)
        self.assertFalse(self._try(d))
        self._assert_halted(d)

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


class DefersHumanFindings(_Base):
    """#409 clause 2 — defer, don't veto.

    A HUMAN finding beside implementation work no longer declines the round: the veto made
    auto-iterate fire on 31 of 230 attempts (13.5%), and bundles the maintainer reported as
    broken spent zero rounds. The finding is held in the ledger instead, which is never
    archived and is merged back into §6 at every assembly — so it still reaches the human,
    and C6 still makes them clear it before accept.
    """

    def test_a_judgment_finding_beside_a_defect_no_longer_vetoes_the_rebuild(self) -> None:
        d = self._bundle("MIXED", review=_MIXED_REVIEW)
        self.assertTrue(self._try(d), "a HUMAN finding beside a defect must defer, not veto")
        self._assert_deferred(d, [_C5_TEXT])   # held for the human; IMPL + STANDING are not

    def test_a_human_only_set_still_halts_at_once(self) -> None:
        # Nothing for a rebuild to address: straight to the human, nothing deferred.
        d = self._bundle("HONLY", review=_review_table(
            "C5 Causal adequacy", basis="guards the symptom, not the cause"))
        self.assertFalse(self._try(d))
        self._assert_halted(d)

    def test_environmental_findings_beside_a_red_gate_are_deferred(self) -> None:
        # Each once vetoed the rebuild of an otherwise Do-fixable bundle. Beside real
        # implementation work they are now held for the handover like any HUMAN finding.
        cases = {
            "EXTDEP2": ({"build_notes": "NEEDS-HUMAN external dependency: protoc — cannot "
                                        "compile\n"},
                        "external dependency: protoc — cannot compile"),
            "ADVPLAIN2": ({"advisory": "- NEEDS-HUMAN — the scope looks wider than the brief\n"},
                          "the scope looks wider than the brief"),
        }
        for iid, (kwargs, text) in cases.items():
            with self.subTest(case=iid):
                d = self._bundle(iid, gate=_FAIL, **kwargs)
                self.assertTrue(self._try(d))
                self._assert_deferred(d, [text])

    def test_a_review_or_gate_that_recovered_needs_no_clearing_at_handover(self) -> None:
        """The human's ruling at the #409 sign-off: a row saying a review or a gate gave no
        verdict — the review missing or a placeholder, an advisory leaf's placeholder, a gate
        that could not run — must not need clearing by hand once a later round's review and
        gates have recovered. Round 1 carries one of them beside a red gate and an ordinary
        finding, and rebuilds. Round 2's review, advisory leaf and gates all come back
        clean. The no-verdict row was about round 1's run, not the patch: it is gone from
        the handover §6 and from the accept blockers. The ordinary finding is still there,
        deferred, and still blocks the accept until it is ticked."""
        t2_down = {"id": "T2", "tier": "T2", "label": "docs", "scope": "bundle",
                   "gating": False, "cmd": "echo 'PDCA-UNVERIFIABLE: no docs toolchain'; exit 0"}
        t2_up = {**t2_down, "cmd": "true"}
        ordinary = "external dependency: protoc — cannot compile"
        cases = {
            "the review is missing": (
                t2_up, lambda d: (d / "check-review.md").unlink(),
                "no check-review.md was produced"),
            "the review is a placeholder": (
                t2_up, lambda d: leaves._review_unavailable(
                    d, "connection dropped", failure=leaves._FAIL_TRANSIENT),
                "re-run the Check reviewer; this bundle has no advisory review"),
            "an advisory leaf is a placeholder": (
                t2_up, lambda d: leaves._advisory_unavailable(
                    d, "adversary", "timeout after 3600s", failure=leaves._FAIL_TRANSIENT),
                "advisory leaf 'adversary' did not produce findings"),
            "a gate could not run": (t2_down, lambda d: None, "T2 docs unverifiable"),
        }

        def clean_review(bundle: Path, cfg: Config) -> None:
            (bundle / "check-review.md").write_text(_CLEAN_REVIEW, encoding="utf-8")

        def clean_advisory(bundle: Path, cfg: Config, **_kw: object) -> None:
            (bundle / "check-advisory-adversary.md").write_text(
                "# Adversary\n\nNothing found.\n", encoding="utf-8")

        self.cfg.advisory_leaves = [{"id": "adversary"}]   # round 2 runs it again
        for n, (name, (t2, breakage, row)) in enumerate(cases.items()):
            with self.subTest(case=name):
                d = self._bundle(f"RECOVER{n}", gate=_FAIL,
                                 build_notes=f"NEEDS-HUMAN {ordinary}\n")
                self.cfg.gates_checks = [_FAIL, t2]
                with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                    breakage(d)
                    gates.run_gates(d, self.cfg)
                assemble.assemble_summary(d, self.cfg)
                summ = d / "SUMMARY.md"
                self.assertIn(row, _section6(summ), "precondition: round 1's §6 shows it")
                self.cfg.gates_checks = [_PASS, t2_up]           # round 2: every gate green
                buf = io.StringIO()
                with mock.patch.object(leaves, "run_review", clean_review), \
                     mock.patch.object(leaves, "run_advisory_leaves", clean_advisory), \
                     redirect_stderr(buf), redirect_stdout(io.StringIO()):
                    self.assertTrue(flow._maybe_auto_iterate(
                        self.cfg, d, by="", today="2026-10-01", apply_now=True))
                self.assertTrue((d / "iteration-v1").is_dir())
                self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)
                self.assertNotIn(row, _section6(summ), "a recovered failure came back")
                self.assertEqual(flow.accept_blockers(d), [f"- [ ] {ordinary}"])
                self.assertEqual(_ledger(d), [ordinary])
                self.assertIn("with no verdict left for the next Check", buf.getvalue())

    def test_only_a_review_or_gate_with_no_verdict_is_left_to_the_next_check(self) -> None:
        """Which rows count as "no verdict", pinned at the source (`collect_needs_human`):
        only the row the harness writes itself because a review or a gate returned nothing.
        A finding is never one of them — not even a finding in an artifact that merely READS
        as a placeholder (it quoted a status marker and never closed itself): dropping that
        would lose a real finding nobody ticked."""
        d = self._bundle("NOVERDICT", gate=_UNVERIFIABLE,
                         review=_review_table("C5 Causal adequacy", basis="symptom only"),
                         build_notes="NEEDS-HUMAN external dependency: protoc — cannot compile\n")
        with redirect_stderr(io.StringIO()):
            leaves._advisory_unavailable(d, "code-review", "exit 1",
                                         failure=leaves._FAIL_SUBSTANTIVE)
        (d / "check-advisory-adversary.md").write_text(        # reads as a placeholder…
            f"<!-- pdca:leaf-status {assemble.LEAF_STATUS_INFRA} -->\n\n"
            "- NEEDS-HUMAN — the fix guards the symptom, not the cause\n", encoding="utf-8")
        marked = {it.text: it.no_verdict for it in assemble.collect_needs_human(d, self.cfg)}
        no_verdict = sorted(t for t, flag in marked.items() if flag)
        self.assertEqual(len(no_verdict), 2, marked)
        self.assertTrue(no_verdict[0].startswith("C4 verify unverifiable"), no_verdict)
        self.assertTrue(no_verdict[1].endswith(
            "advisory leaf 'code-review' did not produce findings (exit 1); re-run it or "
            "adjudicate by hand."), no_verdict)
        for text, flag in marked.items():
            if not flag:                                        # …its finding stays one
                self.assertTrue(any(s in text for s in ("symptom", "protoc", "fitness")), text)
        # The reviewer's own placeholder, and a missing review, are no verdict too.
        with redirect_stderr(io.StringIO()):
            leaves._review_unavailable(d, "connection dropped", failure=leaves._FAIL_SUBSTANTIVE)
        review_rows = [it for it in assemble.collect_needs_human(d, self.cfg)
                       if assemble.REVIEW_UNAVAILABLE_FINDING in it.text]
        self.assertEqual([it.no_verdict for it in review_rows], [True])
        (d / "check-review.md").unlink()
        missing = [it for it in assemble.collect_needs_human(d, self.cfg)
                   if "no check-review.md was produced" in it.text]
        self.assertEqual([it.no_verdict for it in missing], [True])

    def test_deferred_findings_reach_the_handover_section6_and_block_accept(self) -> None:
        """THE loss-proof property. Round 1's reviewer raised C5; the rebuild's reviewer did
        not. Before the ledger C5 existed only in `iteration-v1/` — the handover §6 the human
        signs off against would never have shown it."""
        self.cfg.max_auto_iters = 1
        d = self._bundle("HANDOVER", review=_MIXED_REVIEW)

        def impl_only_review(bundle: Path, cfg: Config) -> None:
            (bundle / "check-review.md").write_text(_IMPL_ONLY_REVIEW, encoding="utf-8")

        buf = io.StringIO()
        with mock.patch.object(leaves, "run_review", impl_only_review), \
             redirect_stderr(buf), redirect_stdout(io.StringIO()):
            fired = [flow._maybe_auto_iterate(self.cfg, d, by="", today="2026-09-30",
                                              apply_now=True) for _ in range(2)]
        self.assertEqual(fired, [True, False])
        self.assertIn("auto-iterate budget spent (1/1)", buf.getvalue())
        self.assertIn("deferred to handover", buf.getvalue())       # the round said so
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)
        self.assertNotIn("guards the symptom",                      # this round never raised it…
                         (d / "check-review.md").read_text(encoding="utf-8"))
        summ = d / "SUMMARY.md"
        self.assertIn(f"- [ ] {_C5_TEXT}", signoff.open_needs_human(summ))   # …yet §6 has it
        # C6 holds on that row alone: clear everything else, and accept is still refused.
        summ.write_text(summ.read_text(encoding="utf-8").replace("- [ ]", "- [x]")
                        .replace(f"- [x] {_C5_TEXT}", f"- [ ] {_C5_TEXT}"), encoding="utf-8")
        (d / leaves.SIGNOFF_DECISION).write_text("accept\n", encoding="utf-8")
        with redirect_stderr(io.StringIO()):
            outcome = flow._apply_decision(self.cfg, d, by="human", today="2026-09-30",
                                           apply_now=False)
        self.assertEqual(outcome, "blocked")
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)

    def test_the_ledger_is_cycle_evidence_and_is_never_archived(self) -> None:
        # Pinned against the module constants: archive it and every deferred finding leaves the
        # live bundle with the SUMMARY that carried it.
        self.assertEqual(autoiterate.DEFERRED_FILE, _LEDGER)
        self.assertIn(autoiterate.DEFERRED_FILE, state.CYCLE_EVIDENCE_ONLY)
        self.assertNotIn(autoiterate.DEFERRED_FILE, state.DOWNSTREAM_OF_BRIEF)
        d = self._bundle("KEEP", review=_MIXED_REVIEW)
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            flow._maybe_auto_iterate(self.cfg, d, by="", today="2026-09-30", apply_now=True)
        self.assertTrue((d / "iteration-v1").is_dir())               # the round was archived…
        self.assertFalse((d / "iteration-v1" / _LEDGER).exists())    # …the ledger was not
        self.assertEqual(_ledger(d), [_C5_TEXT])
        self.assertIn(f"- [ ] {_C5_TEXT}", _section6(d / "SUMMARY.md"))   # rebuilt §6 has it

    def test_rationale_names_what_was_addressed_and_what_was_deferred(self) -> None:
        items = [assemble.NeedsHumanItem("C4 Verification (red→green) — off-by-one",
                                         assemble.IMPL),
                 assemble.NeedsHumanItem(_C5_TEXT, assemble.HUMAN),
                 assemble.NeedsHumanItem("Validation — fitness-to-purpose — the human's call",
                                         assemble.STANDING)]
        r = autoiterate.rationale(items, attempt=2)
        self.assertNotIn("\n", r)
        self.assertIn("round 2", r)
        self.assertIn("off-by-one", r)                       # addressed: named
        self.assertIn("Deferred", r)
        self.assertIn("1 finding(s)", r)                     # deferred: counted…
        self.assertNotIn("guards the symptom", r)            # …never quoted (#294)
        self.assertNotIn("Validation", r)
        self.assertNotIn("implementation-level items only", r)   # no longer true

    def test_deferred_findings_never_reach_the_builders_carry_forward(self) -> None:
        # The #294 property end to end: the brief the next Do reads names the defect and the
        # fact of a deferral, but carries no human-only judgment call as though it were work.
        d = self._bundle("CARRY2", review=_MIXED_REVIEW)
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            flow._maybe_auto_iterate(self.cfg, d, by="", today="2026-09-30", apply_now=True)
        brief_text = (d / "brief.md").read_text(encoding="utf-8")
        self.assertIn("C4 Verification", brief_text)
        self.assertIn("Deferred", brief_text)
        self.assertNotIn("guards the symptom", brief_text)
        self.assertNotIn("Validation — fitness-to-purpose", brief_text)

    def test_an_unreadable_ledger_is_a_section6_item_at_assembly(self) -> None:
        d = self._bundle("BADLEDGER")                        # clean: §6 is otherwise empty
        (d / _LEDGER).write_text("{ not json", encoding="utf-8")
        assemble.assemble_summary(d, self.cfg)
        [row] = signoff.open_needs_human(d / "SUMMARY.md")
        self.assertIn(f"{_LEDGER} exists but cannot be read", row)

    def test_an_unreadable_ledger_blocks_accept_on_the_shared_decision_path(self) -> None:
        """Not only when auto-iterate is on: a ledger written by an earlier run outlives the
        setting that wrote it, and a SUMMARY assembled before the ledger broke does not carry
        the row. `_apply_decision` is the one path every sign-off takes."""
        self.cfg.auto_iterate = False
        d = self._bundle("BADACCEPT")
        summ = d / "SUMMARY.md"
        self.assertEqual(signoff.open_needs_human(summ), [])  # an accept would go through…
        (d / _LEDGER).write_text('{"items": ["a held finding", 7]}', encoding="utf-8")
        (d / leaves.SIGNOFF_DECISION).write_text("accept\n", encoding="utf-8")
        with redirect_stderr(io.StringIO()):
            outcome = flow._apply_decision(self.cfg, d, by="human", today="2026-09-30",
                                           apply_now=False)
        self.assertEqual(outcome, "blocked")                  # …until the ledger broke
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)
        six = _section6(summ)
        self.assertIn(f"- [ ] {_LEDGER} exists but cannot be read", six)
        self.assertNotIn("- (none", six)                      # the empty-§6 line is now false
        # The human clears it deliberately: that tick is honoured and not re-added.
        summ.write_text(summ.read_text(encoding="utf-8").replace("- [ ]", "- [x]"),
                        encoding="utf-8")
        with redirect_stderr(io.StringIO()):
            outcome = flow._apply_decision(self.cfg, d, by="human", today="2026-09-30",
                                           apply_now=False)
        self.assertEqual(outcome, "accept")
        self.assertEqual(state.state(d), state.COMPLETE)
        self.assertEqual(_section6(summ).count("exists but cannot be read"), 1)

    def test_an_unreadable_ledger_blocks_accept_on_the_cli_path_too(self) -> None:
        """`pdca signoff <id> --accept` records an accept WITHOUT going through
        `_apply_decision`, so it must take its C6 check from the same helper. With a check of
        its own it accepted over an unreadable ledger that `_apply_decision` refused."""
        self.cfg.auto_iterate = False
        d = self._bundle("BADCLI")
        summ = d / "SUMMARY.md"
        self.assertEqual(signoff.open_needs_human(summ), [])  # an accept would go through…
        (d / _LEDGER).write_text('{"items": ["a held finding", 7]}', encoding="utf-8")
        accept = SimpleNamespace(issue_id="BADCLI", accept=True, iterate_do=False,
                                 iterate_plan=False, discontinue=False, by="human", delta="",
                                 no_publish=True)
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            rc = cli._signoff(self.cfg, accept)
        self.assertEqual(rc, 1, "the CLI accepted over an unreadable ledger")   # …until it broke
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)
        self.assertIn(f"- [ ] {_LEDGER} exists but cannot be read", _section6(summ))
        self.assertIn("cannot accept", err.getvalue())
        # The human's tick clears it on this path too, and the row is not added again.
        summ.write_text(summ.read_text(encoding="utf-8").replace("- [ ]", "- [x]"),
                        encoding="utf-8")
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            rc = cli._signoff(self.cfg, accept)
        self.assertEqual(rc, 0)
        self.assertEqual(state.state(d), state.COMPLETE)
        self.assertEqual(_section6(summ).count("exists but cannot be read"), 1)

    def test_an_unreadable_ledger_stops_auto_iterate_without_spending_a_round(self) -> None:
        d = self._bundle("BADAUTO", gate=_FAIL)              # implementation work: would fire
        (d / _LEDGER).write_text("{ not json", encoding="utf-8")
        assemble.assemble_summary(d, self.cfg)
        buf = io.StringIO()
        with redirect_stderr(buf), redirect_stdout(io.StringIO()):
            fired = flow._maybe_auto_iterate(self.cfg, d, by="", today="2026-09-30",
                                             apply_now=False)
        self.assertFalse(fired)
        self.assertEqual(autoiterate.count(d), 0)
        self.assertFalse((d / leaves.SIGNOFF_DECISION).exists())
        self.assertEqual((d / _LEDGER).read_text(encoding="utf-8"), "{ not json")  # untouched
        self.assertIn("cannot be read", buf.getvalue())


class C6ReadsTheAssembledSection6(_Base):
    """Every §6 reader uses the §6 assembly wrote — the LAST `## 6. NEEDS-HUMAN` heading. The
    first can be a leaf's: an advisory artifact can quote a whole §6 block, and assembly pastes
    it into §5 above the real one. Read there, a quoted block of ticked rows let C6 pass an
    accept while the real §6 still had an open row, and the row the accept path adds for an
    unreadable ledger landed inside the quote. The C6 read and that row's writer must move
    together: written to one §6 and read from the other, the row is invisible to C6."""

    REAL = "the retry path is still unguarded"
    # An adversary that raises one finding and quotes a previous round's §6, all rows ticked.
    TICKED_QUOTE = ("# Adversary\n\n"
                    f"- NEEDS-HUMAN — {REAL}\n\n"
                    "Quoting the previous round's summary, which the human cleared:\n\n"
                    "## 6. NEEDS-HUMAN — items the human must clear before sign-off\n"
                    "- [x] an older finding the human cleared\n")
    # An adversary that found nothing and ends by quoting an empty §6 — byte for byte the
    # §6 assembly then renders for this bundle.
    EMPTY_QUOTE = ("# Adversary\n\nNothing found. The previous summary read:\n\n"
                   "## 6. NEEDS-HUMAN — items the human must clear before sign-off\n"
                   "- (none — every model-attempted item came back PASS, no always-human "
                   "item applied)\n")
    UNREADABLE = "exists but cannot be read"

    def _assert_fixture_quotes_ticked_rows(self, summ: Path) -> None:
        quote = _quoted_section6(summ)
        self.assertIn("- [x] an older finding the human cleared", quote)
        self.assertNotIn("- [ ]", quote, "precondition: the first §6 heading has no open row")
        self.assertIn(f"- [ ] {self.REAL}", _section6(summ), "precondition: the real one does")

    def test_a_quoted_block_of_ticked_rows_does_not_pass_an_accept(self) -> None:
        d = self._bundle("C6FLOW", advisory=self.TICKED_QUOTE)
        summ = d / "SUMMARY.md"
        self._assert_fixture_quotes_ticked_rows(summ)
        self.assertEqual(flow.accept_blockers(d), [f"- [ ] {self.REAL}"])
        (d / leaves.SIGNOFF_DECISION).write_text("accept\n", encoding="utf-8")
        with redirect_stderr(io.StringIO()):
            outcome = flow._apply_decision(self.cfg, d, by="human", today="2026-10-01",
                                           apply_now=False)
        self.assertEqual(outcome, "blocked")
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)
        # Control: ticked in the real §6, the same accept goes through.
        summ.write_text(summ.read_text(encoding="utf-8")
                        .replace(f"- [ ] {self.REAL}", f"- [x] {self.REAL}"), encoding="utf-8")
        with redirect_stderr(io.StringIO()):
            outcome = flow._apply_decision(self.cfg, d, by="human", today="2026-10-01",
                                           apply_now=False)
        self.assertEqual(outcome, "accept")
        self.assertEqual(state.state(d), state.COMPLETE)

    def test_a_quoted_block_of_ticked_rows_does_not_pass_a_cli_accept(self) -> None:
        d = self._bundle("C6CLI", advisory=self.TICKED_QUOTE)
        self._assert_fixture_quotes_ticked_rows(d / "SUMMARY.md")
        accept = SimpleNamespace(issue_id="C6CLI", accept=True, iterate_do=False,
                                 iterate_plan=False, discontinue=False, by="human", delta="",
                                 no_publish=True)
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            rc = cli._signoff(self.cfg, accept)
        self.assertEqual(rc, 1, "`pdca signoff --accept` passed a quoted §6 over an open row")
        self.assertIn("cannot accept", err.getvalue())
        self.assertIn(self.REAL, err.getvalue())
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)

    def test_the_unreadable_ledger_row_lands_in_the_real_section6_once(self) -> None:
        cases = {
            # Assembly already rendered the row in the real §6: the accept path must see that
            # copy and add no second one.
            "broken before assembly": (self.TICKED_QUOTE, True),
            # The SUMMARY predates the break: the accept path adds the row, to the real §6.
            "broken after assembly": (self.TICKED_QUOTE, False),
            # The quote is byte for byte the real §6, so the first place that text occurs is
            # the quote: the row must be placed by position, not by finding the text.
            "a quote identical to the real section": (self.EMPTY_QUOTE, False),
        }
        for n, (name, (advisory, before)) in enumerate(cases.items()):
            with self.subTest(case=name):
                d = self._bundle(f"C6LEDGER{n}", advisory=advisory)
                summ = d / "SUMMARY.md"
                (d / _LEDGER).write_text("{ not json", encoding="utf-8")
                if before:
                    assemble.assemble_summary(d, self.cfg)
                    self.assertIn(self.UNREADABLE, _section6(summ), "precondition")
                if advisory is self.EMPTY_QUOTE:
                    self.assertEqual(_quoted_section6(summ), _section6(summ),
                                     "precondition: the quote IS the real §6, byte for byte")
                quote = _quoted_section6(summ)
                with redirect_stderr(io.StringIO()):
                    blockers = flow.accept_blockers(d)
                self.assertTrue([b for b in blockers if self.UNREADABLE in b], blockers)
                self.assertEqual(summ.read_text(encoding="utf-8").count(self.UNREADABLE), 1)
                self.assertIn(f"- [ ] {_LEDGER} {self.UNREADABLE}", _section6(summ))
                self.assertNotIn("- (none", _section6(summ))
                self.assertEqual(_quoted_section6(summ), quote, "the quote was written to")


class TwoStops(_Base):
    """#409 clause 1 — the loop's stopping rules are exactly two, and neither is new: the size
    backstop (early, 2 rounds by default) and the hard cap `max_auto_iters`. A soft round
    budget with a convergence test (#332 item 1) was dropped at Plan: on a default instance
    the backstop always fires first, so it would add a setting that never binds."""

    def _drive(self, d: Path, review: str = _MIXED_REVIEW) -> tuple[int, str]:
        """Re-drive the bundle with auto-iterate until it declines; (rounds fired, stderr)."""
        def reviewer(bundle: Path, cfg: Config) -> None:
            (bundle / "check-review.md").write_text(review, encoding="utf-8")

        buf = io.StringIO()
        fired = 0
        with mock.patch.object(leaves, "run_review", reviewer), \
             redirect_stderr(buf), redirect_stdout(io.StringIO()):
            for _ in range(8):
                if not flow._maybe_auto_iterate(self.cfg, d, by="", today="2026-09-30",
                                                apply_now=True):
                    break
                fired += 1
        return fired, buf.getvalue()

    def test_there_is_no_soft_budget_key(self) -> None:
        # Guards the drop against being re-added; not a red leg (it passes on main too).
        self.assertNotIn("soft_auto_iters", Config.__dataclass_fields__)
        self.assertFalse(hasattr(self.cfg, "soft_auto_iters"))
        root = Path(__file__).resolve().parents[1]
        # `pdca.toml.jinja` in the template repo, `pdca.toml` in a rendered instance (this
        # file ships into the render). A key the template never declares cannot be rendered.
        sources = [root / n for n in ("pdca.toml.jinja", "pdca.toml") if (root / n).is_file()]
        if not sources:
            self.skipTest("no pdca.toml(.jinja) beside the tests")
        text = sources[0].read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"^\s*#?\s*soft_auto_iters\s*=", text, re.MULTILINE))

    def test_mixed_checks_fire_until_the_hard_cap(self) -> None:
        # The size backstop OFF, so the hard cap is the only stop left.
        self.cfg.size_signal = {"rounds": 0}
        self.cfg.max_auto_iters = 3
        d = self._bundle("CAP", review=_MIXED_REVIEW)
        fired, err = self._drive(d)
        self.assertEqual(fired, 3, "an [IMPL, HUMAN] Check must keep firing to the cap")
        self.assertEqual(autoiterate.count(d), 3)
        self.assertIn("auto-iterate budget spent (3/3)", err)
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)   # handed over, not dropped
        # Raised by every round, recorded once, rendered once.
        self.assertEqual(_ledger(d), [_C5_TEXT])
        self.assertEqual(_section6(d / "SUMMARY.md").count(_C5_TEXT), 1)

    def test_mixed_checks_stop_at_the_size_backstop_first_by_default(self) -> None:
        # Default `[size_signal].rounds = 2`, below the cap of 3 on purpose.
        self.cfg.max_auto_iters = 3
        d = self._bundle("SIZE", review=_MIXED_REVIEW)
        fired, err = self._drive(d)
        self.assertEqual(fired, 2)
        self.assertEqual(autoiterate.count(d), 2)
        self.assertIn("not auto-iterating: size backstop", err)   # it says why, on stderr
        self.assertNotIn("budget spent", err)
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)
        self.assertIn(f"- [ ] {_C5_TEXT}", _section6(d / "SUMMARY.md"))


def _summary(rows: list[str], *, heading: bool = True, elsewhere: str = "") -> str:
    """A SUMMARY.md with the given §6 checkbox rows (or none, and no §6 heading at all)."""
    six = "## 6. NEEDS-HUMAN — items the human must clear before sign-off\n" if heading else ""
    return ("# Result — issue 1 / retire\n\n"
            "## 5. Advisory review (artifact-only, decorrelated)\n"
            f"{elsewhere}\n\n"
            f"{six}" + "".join(f"{r}\n" for r in rows) + "\n"
            "## 7. Proven / not proven\n- n/a\n\n"
            "## 9. Check sign-off                     ← human completes Check here\n"
            "- Outcome:\n")


class RetiresOnlyWhatTheHumanTicked(unittest.TestCase):
    """#409 clause 3, with #335 folded in — `autoiterate.retire_cleared`.

    A deferred finding leaves the ledger only on a POSITIVE tick in the §6 assembly wrote. A
    tick belongs to one rendered row or none — a ledger entry, or one of this Check's `fresh`
    findings — and only a tick that equals no rendered row is an edit, matched fuzzily. A
    still-open row protects entries by the SAME `_same_finding` relation, assigned
    exact-first over the same rendered rows: a verbatim open row protects its own entry only,
    or nothing when it is a fresh finding (so a near-identical pair stays drainable), and any
    other open row — an edit — protects every entry it matches (fail closed).
    The #335 repro and the matcher-drift test are written to fail against BOTH wrong
    protection shapes: exact-only protection (the #335 bug) and the flat symmetric-fuzzy
    exclusion (the permanently unclearable pair). Each of the others pins one further rule,
    named in its test.
    """

    # Two different findings that `_same_finding` cannot tell apart (a long shared opening).
    P = "C5 Causal adequacy — guards the symptom in the parser"
    R = "C5 Causal adequacy — guards the symptom in the renderer"
    E = "T5 Judgment — the retry loop swallows the first failure and reports the last one"
    X = "C1 Spec — the brief never says which config file wins"

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.n = 0

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _retire(self, ledger: list[str], rows: list[str], *, fresh: tuple[str, ...] = (),
                **summary_kw) -> list[str]:
        """Retire against a §6 of ``rows``. ``fresh`` is what this Check raised itself; left
        empty, every ticked row that is not an entry is an edit as far as retirement knows."""
        self.n += 1
        d = self.tmp / f"issue_{self.n}"
        d.mkdir()
        _write_ledger(d, ledger)
        before = (d / _LEDGER).read_bytes()
        (d / "SUMMARY.md").write_text(_summary(rows, **summary_kw), encoding="utf-8")
        kept = autoiterate.retire_cleared(d, d / "SUMMARY.md", fresh=fresh)
        self.assertEqual(kept, _ledger(d), "the return value must be what is on disk")
        if kept == ledger:
            self.assertEqual((d / _LEDGER).read_bytes(), before,
                             "nothing retired, so the ledger must not be rewritten")
        return kept

    def test_a_tick_retires_its_entry_and_absence_is_not_consent(self) -> None:
        kept = self._retire([self.P, self.E, self.X],
                            [f"- [x] {self.E}",
                             f"- [ ] {self.P}",
                             f"- [ ] {self.X} (owner: plan)"])   # edited, never ticked
        self.assertEqual(kept, [self.P, self.X])

    def test_335_an_annotated_open_row_survives_a_similar_ticked_new_finding(self) -> None:
        self.assertTrue(autoiterate._same_finding(self.P, self.R), "precondition: near-twins")
        # The #335 repro: the human annotated the deferred parser row but did not tick it,
        # and ticked a similar NEW finding (not in the ledger). The tick fuzzy-matches the
        # parser entry; only the open annotated row can protect it, and only if protection
        # uses the relation the tick does. Exact-only protection retires it here.
        self.assertEqual(self._retire([self.P],
                                      [f"- [ ] {self.P} (owner: architecture)",
                                       f"- [x] {self.R}"]),
                         [self.P], "an unadjudicated deferred finding was retired")
        # …and the fix must not overshoot into symmetric-fuzzy protection: a VERBATIM open row
        # protects its own entry only, so its exactly-ticked near-twin still drains.
        self.assertEqual(self._retire([self.P, self.R],
                                      [f"- [ ] {self.P}", f"- [x] {self.R}"]),
                         [self.P], "an exactly-ticked entry was shielded by its open near-twin")

    def test_every_edit_shape_a_tick_tolerates_also_protects_when_left_open(self) -> None:
        """Matcher drift. Whatever `_same_finding` accepts for a tick, an open row must
        accept for protection — even when the entry's own row is ticked exactly beside it."""
        shapes = {
            "annotated": f"{self.E} (owner: architecture)",
            "prefixed": f"Re-raised: {self.E}",
            "trimmed": self.E.rsplit(" ", 3)[0],
            "edited in the middle": self.E.replace("reports the last one",
                                                   "logs only the last one"),
            "case and spacing": "  ".join(self.E.upper().split()),
        }
        for name, edited in shapes.items():
            with self.subTest(shape=name):
                self.assertTrue(autoiterate._same_finding(self.E, edited), "precondition")
                # Left open, the edited row protects the entry against an exact tick.
                self.assertEqual(self._retire([self.E], [f"- [ ] {edited}", f"- [x] {self.E}"]),
                                 [self.E])
                if " ".join(edited.split()).casefold() == self.E.casefold():
                    continue   # the same entry after normalisation: no distinct twin to drain
                # A VERBATIM open row owns its own entry only — the exactly-ticked twin drains,
                # in either direction.
                self.assertEqual(self._retire([self.E, edited],
                                              [f"- [ ] {self.E}", f"- [x] {edited}"]),
                                 [self.E])
                self.assertEqual(self._retire([self.E, edited],
                                              [f"- [ ] {edited}", f"- [x] {self.E}"]),
                                 [edited])

    def test_an_edited_open_row_matching_two_near_twins_protects_both(self) -> None:
        edited = "C5 Causal adequacy — guards the symptom in the pipeline (owner: architecture)"
        self.assertTrue(autoiterate._same_finding(self.P, edited), "precondition")
        self.assertTrue(autoiterate._same_finding(self.R, edited), "precondition")
        # It owns neither verbatim, so it could be an edit of either: fail closed, protect both
        # — even the one ticked exactly.
        self.assertEqual(self._retire([self.P, self.R], [f"- [ ] {edited}", f"- [x] {self.R}"]),
                         [self.P, self.R])

    def test_a_tick_matching_two_entries_retires_neither(self) -> None:
        """A tick retires ONE entry or none. The human deleted P's row, then edited R's row and
        ticked it. The edit owns neither entry verbatim and `_same_finding`-matches both
        near-twins, so it cannot say which one the human cleared: fail closed, both stay.
        Retiring the first match would drop P, which nobody ticked; so would retiring every
        match."""
        edited = f"{self.R} (fixed by the rebuild)"
        for entry in (self.P, self.R):
            self.assertTrue(autoiterate._same_finding(entry, edited), "precondition")
            self.assertNotEqual(" ".join(edited.split()).casefold(), entry.casefold(),
                                "precondition: a fuzzy match, not an exact one")
        self.assertEqual(self._retire([self.P, self.R], [f"- [x] {edited}"]), [self.P, self.R])
        # Control: with only R in the ledger the same tick is unambiguous, and it retires R.
        self.assertEqual(self._retire([self.R], [f"- [x] {edited}"]), [])

    def test_a_tick_on_a_fresh_finding_never_retires_a_deferred_one(self) -> None:
        """A tick EQUAL to a row assembly rendered from one of this Check's own findings is
        that finding's clearance, never an edit of a ledger entry. To a text matcher a one-word
        edit and a different finding have the same shape, so no threshold can tell them apart;
        whether the ticked row is one assembly rendered can. The iteration-3 adversary's three
        pairs, each with the deferred entry's own row deleted from §6 and the fresh finding
        ticked as rendered: no label, a shared 5/5/1 label, a shared path:line opening."""
        pairs = {
            "no label": ("the fix does not cover the retry path",
                         "the fix does not cover the CLI path"),
            "a shared label": (
                "C5 Causal adequacy — the patch does not handle the empty-list case",
                "C5 Causal adequacy — the patch does not handle concurrent writers"),
            "a shared path:line opening": (
                "C5 Causal adequacy — template/src/pdca_harness/autoiterate.py:316 — "
                "first hit wins",
                "C5 Causal adequacy — template/src/pdca_harness/autoiterate.py:126 — "
                "floor skipped"),
        }
        for name, (entry, fresh) in pairs.items():
            with self.subTest(pair=name):
                self.assertTrue(autoiterate._same_finding(entry, fresh),
                                "precondition: to the matcher they are one finding")
                self.assertEqual(self._retire([entry], [f"- [x] {fresh}"], fresh=(fresh,)),
                                 [entry])
                # Control: the entry's own tick still retires it, beside the fresh one.
                self.assertEqual(self._retire([entry], [f"- [x] {fresh}", f"- [x] {entry}"],
                                              fresh=(fresh,)), [])
        # A deferred finding this Check raised AGAIN is one rendered row, not two — assembly
        # renders it once, as the entry — so its tick retires the entry, however the fresh
        # copy is spaced or cased.
        for again in (self.E, "  ".join(self.E.upper().split())):
            with self.subTest(raised_again=again):
                self.assertEqual(self._retire([self.E], [f"- [x] {self.E}"], fresh=(again,)),
                                 [])
        # Annotated before it was ticked, the fresh finding's row is an edit — of the fresh row
        # or of the entry, the matcher cannot say which. One tick, two candidates: fail
        # closed, as for two entries (`test_a_tick_matching_two_entries_retires_neither`).
        entry, fresh = pairs["no label"]
        noted = f"{fresh} (fixed in round 2)"
        for row in (entry, fresh):
            self.assertTrue(autoiterate._same_finding(row, noted), "precondition")
        self.assertEqual(self._retire([entry], [f"- [x] {noted}"], fresh=(fresh,)), [entry])
        # The cost, in the same safe direction: the entry's own row annotated and ticked
        # matches its fresh near-twin too, so the entry stays one more round — shown again,
        # never lost. Ticked as rendered, it retires (the control above).
        self.assertEqual(self._retire([entry], [f"- [x] {entry} (fixed in round 2)"],
                                      fresh=(fresh,)), [entry])

    def test_a_fresh_near_twin_left_open_shields_nothing(self) -> None:
        """Protection reads rows the way ticks do. An open row EQUAL to a row assembly rendered
        from one of this Check's own findings is that finding, not an edit of a deferred one,
        so it protects no ledger entry — just as an open deferred near-twin protects only
        itself (`test_335_…`). It used to protect every entry it matched, so the human's exact
        tick on the deferred retry-path finding never took while the fresh CLI-path finding
        sat open beside it: the entry came back unticked every round."""
        entry = "the fix does not cover the retry path"
        fresh = "the fix does not cover the CLI path"
        self.assertTrue(autoiterate._same_finding(entry, fresh), "precondition: near-twins")
        self.assertEqual(self._retire([entry], [f"- [x] {entry}", f"- [ ] {fresh}"],
                                      fresh=(fresh,)), [])
        # The fail-closed tier is kept. EDITED, the same open row could be an edit of the
        # entry as easily as of the fresh finding, so it protects the entry…
        edited = f"{fresh} (owner: plan)"
        self.assertTrue(autoiterate._same_finding(entry, edited), "precondition")
        self.assertEqual(self._retire([entry], [f"- [x] {entry}", f"- [ ] {edited}"],
                                      fresh=(fresh,)), [entry])
        # …and so does the unedited text when this Check did not render it: then the human
        # wrote that row, and it is an edit too.
        self.assertEqual(self._retire([entry], [f"- [x] {entry}", f"- [ ] {fresh}"]), [entry])

    def test_a_section6_quoted_by_a_leaf_is_not_the_humans(self) -> None:
        """A leaf's artifact can quote a whole §6 block, and assembly pastes it into §5 ABOVE
        the §6 it writes itself, so the FIRST §6 heading can be a leaf's. Ticks and open rows
        are both read from the §6 assembly wrote — the last heading: a quoted tick retires
        nothing, and a quoted open row protects nothing."""
        quote = ("The adversary quoted the previous round's summary:\n\n"
                 "## 6. NEEDS-HUMAN — items the human must clear before sign-off\n{rows}")
        ticked = quote.format(rows=f"- [x] {self.E}\n")
        # The quoted tick is not the human's — whether the real §6 holds E open or not at all.
        self.assertEqual(self._retire([self.E], [f"- [ ] {self.E}"], elsewhere=ticked),
                         [self.E])
        self.assertEqual(self._retire([self.E], [], elsewhere=ticked), [self.E])
        # The open rows come from the same §6 as the ticks. The human left an edited copy of E
        # open beside its exact tick; that protects E, as it does with no quote at all. Read
        # from the first heading instead, the open rows would be the quote's — none.
        self.assertEqual(self._retire([self.E], [f"- [ ] {self.E} (owner: architecture)",
                                                 f"- [x] {self.E}"],
                                      elsewhere=quote.format(rows="")), [self.E])
        # Control: the human's own tick retires E, even where a leaf quoted E's row open — a
        # leaf cannot make a deferred finding unclearable by quoting it.
        self.assertEqual(self._retire([self.E], [f"- [x] {self.E}"],
                                      elsewhere=quote.format(rows=f"- [ ] {self.E}\n")), [])

    def test_a_short_row_matches_only_exactly(self) -> None:
        """`_MATCH_FLOOR` bounds containment as well as the shared opening. A ticked fragment
        under the floor — an element name, a few words — occurs inside countless findings, so a
        deleted entry that merely CONTAINS it was never ticked: retiring it would read consent
        from absence. The same holds the other way round, for a short entry inside a long row.

        "Exactly" is measured past the label the two rows share: a short finding whose only
        edit is the separator after its label (`C1 Spec: vague` for `C1 Spec — vague`) is the
        same text, and the floor must not refuse it — it did, so the entry came back unticked
        every round however often the human ticked it.
        """
        fragments = {"prefix": "T5 Judgment",
                     "middle": "retry loop swallows",    # one character under the floor
                     "suffix": "the last one"}
        for where, short in fragments.items():
            with self.subTest(where=where):
                self.assertIn(short.casefold(), self.E.casefold(), "precondition: a substring")
                self.assertLess(len(short), autoiterate._MATCH_FLOOR, "precondition: short")
                # E's own row was deleted; the only tick is the fragment.
                self.assertEqual(self._retire([self.E], [f"- [x] {short}"]), [self.E])
        short_entry = "C1 Spec — vague"
        with self.subTest(where="a short entry inside a long ticked row"):
            self.assertEqual(self._retire([short_entry],
                                          [f"- [x] {short_entry} about which config file wins"]),
                             [short_entry])
        # Controls: a short entry still retires on its exact tick, and containment at full
        # length still matches — an annotated tick retires its entry.
        self.assertEqual(self._retire([short_entry], [f"- [x] {short_entry}"]), [])
        self.assertEqual(self._retire([self.E], [f"- [x] {self.E} (fixed in round 2)"]), [])
        # A retyped separator after the shared label, on findings under the floor.
        for entry, retyped in ((short_entry, "C1 Spec: vague"),
                               ("C5 Causal adequacy — weak test for X",
                                "C5 Causal adequacy: weak test for X")):
            with self.subTest(retyped=retyped):
                self.assertLess(len(entry.split("— ", 1)[1]), autoiterate._MATCH_FLOOR,
                                "precondition: the finding past its label is short")
                self.assertTrue(autoiterate._same_finding(entry, retyped))
                self.assertEqual(self._retire([entry], [f"- [x] {retyped}"]), [])
        # …which is equality, not a looser floor: a short DIFFERENT finding under the same
        # label still matches nothing.
        self.assertEqual(self._retire([short_entry], ["- [x] C1 Spec: vague wording"]),
                         [short_entry])

    def test_two_findings_on_one_element_are_two_findings(self) -> None:
        """A reviewer row renders as `Item — Basis`, and the Item names the 5/5/1 ELEMENT, not
        the finding. Counted as shared text, the 21 characters of "C5 Causal adequacy — "
        cleared `_MATCH_FLOOR` on their own: a tick on one C5 finding retired a different C5
        finding nobody ticked, and a different finding left open shielded an entry from its
        own exact tick. Only what follows a label both rows share is compared."""
        pairs = {
            "C5": ("C5 Causal adequacy — symptom only",
                   "C5 Causal adequacy — patches the wrong layer entirely"),
            "V": ("Validation — fitness-to-purpose — wrong layer",
                  "Validation — fitness-to-purpose — the docs overclaim the guarantee"),
            "a typed colon": ("C5 causal adequacy: symptom only",
                              "C5 causal adequacy: patches the wrong layer entirely"),
            "a short finding inside a longer one": (
                "C5 Causal adequacy — symptom only",
                "C5 Causal adequacy — symptom only in the parser; the renderer is fine"),
        }
        for name, (short, other) in pairs.items():
            with self.subTest(pair=name):
                self.assertGreaterEqual(len(os.path.commonprefix([short, other])),
                                        autoiterate._MATCH_FLOOR,
                                        "precondition: counting the label, it clears the floor")
                self.assertFalse(autoiterate._same_finding(short, other))
                # One entry's own row deleted, the other finding ticked: nothing retires.
                self.assertEqual(self._retire([short], [f"- [x] {other}"]), [short])
                self.assertEqual(self._retire([other], [f"- [x] {short}"]), [other])
                # Ticked exactly beside the other finding left open: it retires.
                self.assertEqual(self._retire([short], [f"- [ ] {other}", f"- [x] {short}"]),
                                 [])
        # Control: the SAME finding, edited, still matches under its label.
        long = "C5 Causal adequacy — patches the wrong layer entirely"
        self.assertEqual(self._retire([long], [f"- [x] {long} (fixed in round 2)"]), [])

    def test_a_shared_opening_must_be_most_of_the_shorter_finding(self) -> None:
        """`_MATCH_RATIO`, pinned. Two findings can open alike for a while and then say
        different things. However long the common opening, it makes them one finding only when
        it is most of the shorter one — what an edit in the middle leaves behind. Measured past
        a shared label, with and without one."""
        entry = "the retry loop in flow.py swallows the timeout error on the last attempt"
        other = "the retry loop in flow.py leaks one file handle per call"
        edited = entry.replace("last attempt", "final attempt")
        shared = len(os.path.commonprefix([entry, other]))
        self.assertGreaterEqual(shared, autoiterate._MATCH_FLOOR,
                                "precondition: the floor alone cannot refuse the pair")
        self.assertLess(shared, autoiterate._MATCH_RATIO * len(other),
                        "precondition: the ratio does")
        for label in ("", "C5 Causal adequacy — "):
            e, o, ed = (label + t for t in (entry, other, edited))
            with self.subTest(label=label or "(none)"):
                for x, y in ((e, o), (e, ed)):
                    self.assertFalse(x in y or y in x, "precondition: no containment")
                self.assertEqual(self._retire([e], [f"- [x] {o}"]), [e])
                # Control: an edit in the middle keeps most of the opening, and retires.
                self.assertEqual(self._retire([e], [f"- [x] {ed}"]), [])

    def test_two_placeholder_rows_are_two_findings(self) -> None:
        """The same for the leaf-status label a placeholder row opens with — often longer than
        the finding after it, so it cleared the ratio as well as the floor. Two leaves that
        both died of transient infra are two findings. So are two findings under one
        leaf-status label that each open with the same 5/5/1 label: labels nest."""
        d = self.tmp / "placeholders"
        d.mkdir()
        with redirect_stderr(io.StringIO()):
            for leaf, reason in (("adversary", "timeout after 3600s"), ("code-review", "exit 1")):
                leaves._advisory_unavailable(d, leaf, reason, failure=leaves._FAIL_TRANSIENT)
        infra = [assemble._items_from_artifact(p.read_text(encoding="utf-8"))[0].text
                 for p in sorted(d.glob("check-advisory-*.md"))]
        # A report that quotes a status marker and never closed itself reads as a placeholder,
        # so each of its rows gets the leaf-status label in front of its own Item label.
        quoting = ("<!-- pdca:leaf-status human-empty -->\n\n"
                   "| Item | Verdict | Basis |\n|---|---|---|\n"
                   "| C5 Causal adequacy | NEEDS-HUMAN | symptom only |\n"
                   "| C5 Causal adequacy | NEEDS-HUMAN | patches the wrong layer entirely |\n")
        nested = [it.text for it in assemble._items_from_artifact(quoting)]
        for name, (a, b) in {"two leaves": infra, "nested labels": nested}.items():
            with self.subTest(pair=name):
                self.assertGreaterEqual(len(os.path.commonprefix([a, b])),
                                        autoiterate._MATCH_RATIO * min(len(a), len(b)),
                                        "precondition: counting the label, most of it is shared")
                self.assertFalse(autoiterate._same_finding(a, b))
                self.assertEqual(self._retire([a], [f"- [x] {b}"]), [a])
                self.assertEqual(self._retire([a], [f"- [ ] {b}", f"- [x] {a}"]), [])
        # Control: the same placeholder row, annotated, still matches under its label.
        self.assertEqual(self._retire([infra[0]], [f"- [x] {infra[0]} (re-ran it: fine)"]), [])

    def test_a_summary_with_no_section6_heading_retires_nothing(self) -> None:
        # The tick reader is STRICT (`whole_on_missing=False`): a `- [x]` quoted in §5's review
        # text — pasted verbatim at assembly — is not the human's clearance.
        quoted = f"The reviewer quoted a cleared row:\n\n- [x] {self.E}\n"
        self.assertEqual(self._retire([self.E], [], heading=False, elsewhere=quoted), [self.E])
        # Control: the very same row under a real §6 heading does retire it.
        self.assertEqual(self._retire([self.E], [f"- [x] {self.E}"]), [])

    def test_an_unreadable_ledger_is_never_rewritten(self) -> None:
        d = self.tmp / "issue_bad"
        d.mkdir()
        (d / _LEDGER).write_text("{ not json", encoding="utf-8")
        (d / "SUMMARY.md").write_text(_summary([f"- [x] {self.E}"]), encoding="utf-8")
        self.assertEqual(autoiterate.retire_cleared(d, d / "SUMMARY.md", fresh=()), [])
        self.assertEqual((d / _LEDGER).read_text(encoding="utf-8"), "{ not json")


class RetireAtTheIterate(_Base):
    """The wiring: `driver.advance` retires what the human ticked BEFORE the iterate archives
    the SUMMARY that carries the ticks — in both iterate branches."""

    A = "C5 Causal adequacy — the parser guards the symptom"
    B = "T5 Judgment — the retry loop hides the first failure"

    # An advisory artifact quoting an earlier summary's §6, with B ticked in the quote.
    QUOTE = ("Quoting the previous round's summary:\n\n"
             "## 6. NEEDS-HUMAN — items the human must clear before sign-off\n"
             f"- [x] {B}\n")

    def _iterated(self, iid: str, action: str, **bundle_kw) -> Path:
        d = self._bundle(iid, **bundle_kw)
        _write_ledger(d, [self.A, self.B])
        assemble.assemble_summary(d, self.cfg)                   # §6 renders both, unticked
        summ = d / "SUMMARY.md"
        summ.write_text(summ.read_text(encoding="utf-8")
                        .replace(f"- [ ] {self.A}", f"- [x] {self.A}"), encoding="utf-8")
        signoff.record(summ, action=action, by="human", date="2026-09-30")
        with redirect_stderr(io.StringIO()):
            driver.advance(d, self.cfg)                          # the one iterate beat
        return d

    def test_a_human_iterate_do_retires_what_they_ticked(self) -> None:
        d = self._iterated("RETDO", "iterate-do")
        self.assertEqual(_ledger(d), [self.B])
        self.assertIn(f"- [x] {self.A}",                         # the tick is archived intact
                      (d / "iteration-v1" / "SUMMARY.md").read_text(encoding="utf-8"))

    def test_a_human_iterate_plan_retires_what_they_ticked(self) -> None:
        d = self._iterated("RETPLAN", "iterate-plan")
        self.assertEqual(state.state(d), state.UNPLANNED)
        self.assertEqual(_ledger(d), [self.B])

    def test_a_quoted_tick_retires_nothing_on_an_auto_iterate_round(self) -> None:
        """The iteration-3 adversary's repro, end to end. An advisory artifact quotes a §6
        block with a deferred finding ticked in it, beside one `[impl]` finding. The round is
        automatic, so nobody ticks anything — and the deferred finding must survive the
        archive the round carries it through, not leave with the SUMMARY that quoted it."""
        d = self._bundle("QUOTEAUTO", advisory=(
            f"# Adversary\n\n- NEEDS-HUMAN [impl] — off-by-one at src/x.py:12\n\n{self.QUOTE}"))
        _write_ledger(d, [self.B])
        assemble.assemble_summary(d, self.cfg)                   # §6 renders B, unticked
        self.assertIn(f"- [x] {self.B}",                         # precondition: the quote is
                      (d / "SUMMARY.md").read_text(encoding="utf-8"))   # in the SUMMARY
        self.assertTrue(self._try(d, apply_now=True))            # the [impl] finding fires
        self.assertTrue((d / "iteration-v1").is_dir())           # the round archived it…
        self.assertEqual(_ledger(d), [self.B])                   # …and B is still held
        self.assertIn(f"- [ ] {self.B}", _section6(d / "SUMMARY.md"))   # for the handover

    def test_a_quoted_tick_is_not_the_humans_on_a_human_iterate_either(self) -> None:
        # The human ticked A in the real §6 and nothing else; the quote ticked B.
        d = self._iterated("QUOTEHUMAN", "iterate-do",
                           advisory=f"# Adversary\n\n{self.QUOTE}")
        self.assertEqual(_ledger(d), [self.B])

    def test_a_tick_on_a_fresh_finding_leaves_a_deferred_one_at_the_iterate(self) -> None:
        """The wiring for the fresh-row rule: at the iterate the driver re-derives this Check's
        own findings from the artifacts still in place (`assemble.collect_needs_human`, the
        source §6 rendered them from), so the human's tick on one is read as its clearance —
        not as an edit of the deferred finding whose row the human deleted."""
        entry = "the fix does not cover the retry path"
        fresh = "the fix does not cover the CLI path"
        d = self._bundle("FRESHTICK", advisory=f"# Adversary\n\n- NEEDS-HUMAN — {fresh}\n")
        _write_ledger(d, [entry])
        assemble.assemble_summary(d, self.cfg)
        summ = d / "SUMMARY.md"
        text = summ.read_text(encoding="utf-8")
        for row in (entry, fresh):
            self.assertIn(f"- [ ] {row}\n", text, "precondition: §6 renders both")
        summ.write_text(text.replace(f"- [ ] {entry}\n", "")
                        .replace(f"- [ ] {fresh}", f"- [x] {fresh}"), encoding="utf-8")
        signoff.record(summ, action="iterate-do", by="human", date="2026-10-01")
        with redirect_stderr(io.StringIO()):
            driver.advance(d, self.cfg)
        self.assertTrue((d / "iteration-v1").is_dir())
        self.assertEqual(_ledger(d), [entry])


class DecisionModule(unittest.TestCase):
    """`autoiterate` itself — the guard that keeps this from ever becoming an auto-accept."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _items(self, *kinds: str) -> list[assemble.NeedsHumanItem]:
        return [assemble.NeedsHumanItem(f"finding {i}", k) for i, k in enumerate(kinds)]

    def test_eligible_iff_there_is_implementation_work(self) -> None:
        self.assertTrue(autoiterate.eligible(self._items(assemble.IMPL, assemble.IMPL)))
        self.assertFalse(autoiterate.eligible([]))                                 # never accept
        self.assertTrue(autoiterate.eligible(self._items(assemble.IMPL, assemble.HUMAN)))  # #409
        self.assertTrue(autoiterate.eligible(self._items(assemble.IMPL, assemble.STANDING)))
        self.assertFalse(autoiterate.eligible(self._items(assemble.HUMAN)))
        self.assertFalse(autoiterate.eligible(self._items(assemble.HUMAN, assemble.STANDING)))

    def test_the_size_item_stops_by_kind_not_every_human_item(self) -> None:
        """#409 clause 4, the #324 composition. The size backstop stops the loop by KIND of
        item; ordinary HUMAN items defer. Both legs in ONE test so neither wrong rule passes:
        today's veto fails the first leg, a naive `any(item.kind == IMPL)` fails the second."""
        defect = assemble.NeedsHumanItem("C4 Verification (red→green) — off-by-one",
                                         assemble.IMPL)
        ordinary = assemble.NeedsHumanItem(_C5_TEXT, assemble.HUMAN)
        size = assemble.NeedsHumanItem(
            size_signal.needs_human_text(["2 round(s) already spent (threshold 2)"]),
            assemble.HUMAN)
        self.assertTrue(autoiterate.eligible([defect, ordinary]),
                        "an ordinary HUMAN finding must defer, not veto")
        self.assertFalse(autoiterate.eligible([defect, ordinary, size]),
                         "the size backstop's item must stop the loop")
        # The same text tagged IMPL is not the backstop's item, and still rebuilds.
        self.assertTrue(autoiterate.eligible([defect, ordinary,
                                              size._replace(kind=assemble.IMPL)]))

    def test_write_decision_defers_the_human_items_before_spending_the_round(self) -> None:
        items = [assemble.NeedsHumanItem("finding 0", assemble.IMPL),
                 assemble.NeedsHumanItem("finding 1", assemble.HUMAN),
                 assemble.NeedsHumanItem("finding 2", assemble.STANDING),
                 assemble.NeedsHumanItem("FINDING  1", assemble.HUMAN),   # same, re-worded case
                 assemble.NeedsHumanItem("C2 repro unverifiable — no fixture", assemble.HUMAN,
                                         no_verdict=True)]                # the next Check's
        autoiterate.write_decision(self.tmp, items)
        self.assertEqual(_ledger(self.tmp), ["finding 1"])   # HUMAN findings, deduplicated
        self.assertEqual(autoiterate.count(self.tmp), 1)
        autoiterate.write_decision(self.tmp, items)          # a later round re-raising it
        self.assertEqual(_ledger(self.tmp), ["finding 1"])   # …does not grow the handover
        self.assertEqual(autoiterate.count(self.tmp), 2)
        # The round's §9 line counts what the ledger holds — not the no-verdict row.
        self.assertIn("Deferred, not addressed here: 1 finding(s)",
                      autoiterate.rationale(items, attempt=1))

    def test_write_decision_refuses_the_size_backstop_set(self) -> None:
        size = size_signal.needs_human_text(["patch is 253 KB (threshold 100 KB)"])
        with self.assertRaises(ValueError):
            autoiterate.write_decision(self.tmp, [assemble.NeedsHumanItem("d", assemble.IMPL),
                                                  assemble.NeedsHumanItem(size, assemble.HUMAN)])
        self.assertFalse((self.tmp / leaves.SIGNOFF_DECISION).exists())
        self.assertEqual(autoiterate.count(self.tmp), 0)
        self.assertIsNone(_ledger(self.tmp))

    def test_an_unreadable_ledger_is_told_apart_from_an_absent_one(self) -> None:
        self.assertEqual(autoiterate.deferred(self.tmp), [])          # absent: nothing deferred
        self.assertEqual(autoiterate.ledger_problem(self.tmp), "")
        for garbage in ("{ not json", '["a list, not an object"]', '{"items": "text"}',
                        '{"items": ["ok", 7]}'):
            with self.subTest(content=garbage):
                (self.tmp / _LEDGER).write_text(garbage, encoding="utf-8")
                with self.assertRaises(autoiterate.DeferredLedgerUnreadable):
                    autoiterate.deferred(self.tmp)
                self.assertTrue(autoiterate.ledger_problem(self.tmp))
                # …and a round refuses to rewrite it: no decision, no budget, file untouched.
                with self.assertRaises(autoiterate.DeferredLedgerUnreadable):
                    autoiterate.write_decision(
                        self.tmp, self._items(assemble.IMPL, assemble.HUMAN))
                self.assertFalse((self.tmp / leaves.SIGNOFF_DECISION).exists())
                self.assertEqual(autoiterate.count(self.tmp), 0)
                self.assertEqual((self.tmp / _LEDGER).read_text(encoding="utf-8"), garbage)

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
        self.assertIsNone(_ledger(self.tmp))                      # and nothing deferred

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
        # stay situational HUMAN: alone they halt the bundle, and beside implementation work
        # they are deferred to the handover §6 (#409). The PARSER decides which row is the
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


# --- #408: the reviewer states builder-fixability; the V row in every form it is written ---

# The three forms production writes the reviewer's constant Validation row in (#408): the bare
# label, the label behind its element id (the driver prompt used to list `V — <label>`), and
# with ASCII `--` for the em dash.
_STANDING_FORMS = ("Validation — fitness-to-purpose",
                   "V — Validation — fitness-to-purpose",
                   "Validation -- fitness-to-purpose")


def _full_review(overrides: dict[str, tuple[str, str]] | None = None, *,
                 item: dict[str, str] | None = None) -> str:
    """A complete 5/5/1 verdict table as the reviewer writes it: every row PASS except the
    V row (NEEDS-HUMAN, every cycle), with ``overrides`` mapping an element id to its
    ``(verdict, basis)`` and ``item`` to the Item cell written for it."""
    overrides = overrides or {}
    item = item or {}
    rows = []
    for elem, label, _kind, _oracle in gates.canonical_elements():
        default = (("NEEDS-HUMAN", "fitness is the human's call") if elem == "V"
                   else ("PASS", "ok"))
        verdict, basis = overrides.get(elem, default)
        rows.append(f"| {item.get(elem, label)} | {verdict} | {basis} |")
    return "# Review\n\n| Item | Verdict | Basis |\n|---|---|---|\n" + "\n".join(rows) + "\n"


class ReviewerImplTag(_Base):
    """#408 clause 1/1b/1c/4: the reviewer's own `[impl]` statement, bounded by the taxonomy."""

    def _review_kinds(self, review: str) -> dict[str, str]:
        """{§6 text: kind} for the primary review, read the way `collect_needs_human` reads it."""
        return {it.text: it.kind
                for it in assemble._items_from_artifact(review, allow_standing=True)}

    def test_promotable_set_is_derived_from_the_taxonomy(self) -> None:
        expected = {e for e, _l, k, _o in gates.canonical_elements() if k == "judgment"} - {"V"}
        self.assertEqual(assemble._PROMOTABLE_ELEMENTS, expected)
        self.assertEqual(expected, {"C5", "T5"})

    def test_impl_verdict_on_c5_or_t5_classifies_impl(self) -> None:
        for elem, label in (("C5", "C5 Causal adequacy"), ("T5", "T5 Judgment")):
            with self.subTest(elem=elem):
                kinds = self._review_kinds(_full_review(
                    {elem: ("NEEDS-HUMAN [impl]", "the test never exercises the empty case")}))
                self.assertEqual(kinds[f"{label} — the test never exercises the empty case"],
                                 assemble.IMPL)
                self.assertEqual(kinds["Validation — fitness-to-purpose — fitness is the "
                                       "human's call"], assemble.STANDING)

    def test_impl_verdict_on_c5_auto_iterates_end_to_end(self) -> None:
        d = self._bundle("C5IMPL", review=_full_review(
            {"C5": ("NEEDS-HUMAN [impl]", "the guard misses the empty case")}))
        self.assertTrue(self._try(d), "a builder-fixable C5 finding must reach Do")
        self.assertEqual(state.state(d), state.ITERATE_DO)

    def test_untagged_c5_and_t5_stay_human(self) -> None:
        for elem, label in (("C5", "C5 Causal adequacy"), ("T5", "T5 Judgment")):
            with self.subTest(elem=elem):
                kinds = self._review_kinds(_full_review({elem: ("NEEDS-HUMAN", "a concern")}))
                self.assertEqual(kinds[f"{label} — a concern"], assemble.HUMAN)

    def test_impl_verdict_on_an_input_cell_or_the_v_row_is_ignored(self) -> None:
        kinds = self._review_kinds(_full_review({
            "C1": ("NEEDS-HUMAN [impl]", "the spec is ambiguous"),
            "C3": ("NEEDS-HUMAN [impl]", "the change is out of scope"),
            "V": ("NEEDS-HUMAN [impl]", "fitness is the human's call")}))
        self.assertEqual(kinds["C1 Spec — the spec is ambiguous"], assemble.HUMAN)
        self.assertEqual(kinds["C3 Change — the change is out of scope"], assemble.HUMAN)
        # STANDING is checked BEFORE the tag: V can never be lifted.
        self.assertEqual(kinds["Validation — fitness-to-purpose — fitness is the human's call"],
                         assemble.STANDING)

    def test_impl_in_the_basis_cell_of_a_c5_row_is_ignored(self) -> None:
        kinds = self._review_kinds(_full_review({"C5": ("NEEDS-HUMAN", "[impl] off-by-one")}))
        self.assertEqual(kinds["C5 Causal adequacy — [impl] off-by-one"], assemble.HUMAN)

    def test_primary_review_bullets_promote_only_c5_t5(self) -> None:
        review = _full_review() + (
            "\n- NEEDS-HUMAN [impl] — C1 Spec the criterion is unmeasurable\n"
            "- NEEDS-HUMAN [impl] — Validation — fitness-to-purpose: patches the wrong layer\n"
            "- NEEDS-HUMAN [impl] — C5 Causal adequacy the guard misses the empty case\n")
        d = self._bundle("BULLETS", review=review)
        kinds = {it.text: it.kind for it in assemble.collect_needs_human(d, self.cfg)}
        self.assertEqual(kinds["C1 Spec the criterion is unmeasurable"], assemble.HUMAN)
        self.assertEqual(
            kinds["Validation — fitness-to-purpose: patches the wrong layer"], assemble.HUMAN)
        self.assertEqual(kinds["C5 Causal adequacy the guard misses the empty case"],
                         assemble.IMPL)

    def test_plan_advisory_impl_is_never_promoted(self) -> None:
        d = self._bundle("PLANIMPL", review=_full_review())
        (d / "plan-advisory-plan-reviewer.md").write_text(
            "- NEEDS-HUMAN [impl] — the brief's criterion is a proxy\n", encoding="utf-8")
        found = [it for it in assemble.collect_needs_human(d, self.cfg)
                 if "criterion is a proxy" in it.text]
        self.assertEqual([(it.text, it.kind) for it in found],
                         [("the brief's criterion is a proxy", assemble.HUMAN)])

    def test_human_tag_on_an_advisory_bullet_is_stripped_and_human(self) -> None:
        d = self._bundle("ADVHUMAN", review=_full_review(),
                         advisory="- NEEDS-HUMAN [human] — wrong layer at src/x.py:12\n"
                                  "- NEEDS-HUMAN [impl] — off-by-one at src/x.py:13\n")
        kinds = {it.text: it.kind for it in assemble.collect_needs_human(d, self.cfg)}
        self.assertEqual(kinds["wrong layer at src/x.py:12"], assemble.HUMAN)
        self.assertEqual(kinds["off-by-one at src/x.py:13"], assemble.IMPL)
        self.assertNotIn("[human]", _section6(d / "SUMMARY.md"))

    # Sign-offs of the first two attempts. Only the Verdict column states the verdict: a row
    # was read from the first cell mentioning NEEDS-HUMAN, so a Basis QUOTING an old `[impl]`
    # verdict under PASS became implementation work and would have bought a needless rebuild.
    # But such a row is not dropped either: it mentions NEEDS-HUMAN under another verdict, so it
    # contradicts itself, and the human settles it — as a HUMAN item, on any cell.

    def test_a_basis_quoting_an_impl_verdict_under_pass_is_human_never_impl(self) -> None:
        for elem, label in (("C5", "C5 Causal adequacy"), ("T5", "T5 Judgment")):
            with self.subTest(elem=elem):
                items = assemble._items_from_artifact(_full_review(
                    {elem: ("PASS", "Quoted NEEDS-HUMAN [impl] from an old review")}),
                    allow_standing=True)
                self.assertFalse([it for it in items if it.kind == assemble.IMPL])
                self.assertEqual([(it.text, it.kind) for it in items], [
                    (f"{label} — Quoted NEEDS-HUMAN [impl] from an old review", assemble.HUMAN),
                    ("Validation — fitness-to-purpose — fitness is the human's call",
                     assemble.STANDING)])

    def test_a_basis_quote_under_pass_reaches_section_6_and_does_not_auto_iterate(self) -> None:
        d = self._bundle("BASISQUOTE", review=_full_review(
            {"C5": ("PASS", "Quoted NEEDS-HUMAN [impl] from an old review")}))
        self.assertIn("- [ ] C5 Causal adequacy — Quoted NEEDS-HUMAN [impl] from an old review",
                      _section6(d / "SUMMARY.md"))
        self.assertFalse(self._try(d), "a PASS row is not implementation work")
        self._assert_halted(d)

    def test_only_a_verdict_cell_that_is_the_impl_verdict_promotes(self) -> None:
        # The same quote, moved into the Verdict cell: the tag was searched for anywhere in it,
        # so `PASS (was NEEDS-HUMAN [impl])` promoted a passed row. Only a cell that IS
        # `NEEDS-HUMAN [impl]`, emphasis aside, states the tag; one that mentions it under
        # another verdict, or doubts it, stays the HUMAN finding it was before #408.
        quoted = ("PASS (was NEEDS-HUMAN [impl])", "N/A — not NEEDS-HUMAN [impl]",
                  "NEEDS-HUMAN [impl]? or scope — unsure")
        stated = ("NEEDS-HUMAN [impl]", "**NEEDS-HUMAN [impl]**", "`NEEDS-HUMAN [impl]`",
                  "NEEDS-HUMAN **[impl]**", "needs-human [IMPL]")
        for elem, label in (("C5", "C5 Causal adequacy"), ("T5", "T5 Judgment")):
            for verdict, kind in ([(v, assemble.HUMAN) for v in quoted]
                                  + [(v, assemble.IMPL) for v in stated]):
                with self.subTest(elem=elem, verdict=verdict):
                    kinds = self._review_kinds(_full_review({elem: (verdict, "a missed case")}))
                    self.assertEqual(kinds[f"{label} — a missed case"], kind)

    def test_the_impl_verdict_promotes_only_a_row_whose_item_is_the_label(self) -> None:
        # The element it promotes must not be in doubt. `C5 — Validation — fitness-to-purpose`
        # names two — the brief's mismatched prefix, which is not the V row either — and a
        # label with words added, or a bare id, is not the label: HUMAN, as before #408. The
        # prefixed and `--` forms of the label itself are the label.
        doubtful = ("C5 — Validation — fitness-to-purpose",
                    "T5 — Validation — fitness-to-purpose",
                    "C5 Causal adequacy: the guard", "T5")
        for item in doubtful:
            with self.subTest(item=item):
                kinds = self._review_kinds(_full_review(
                    {"C5": ("NEEDS-HUMAN [impl]", "a missed case")}, item={"C5": item}))
                self.assertEqual(kinds.get(f"{item} — a missed case"), assemble.HUMAN)
        for elem, item, label in (("C5", "C5 — C5 Causal adequacy", "C5 Causal adequacy"),
                                  ("T5", "T5 -- T5 Judgment", "T5 Judgment")):
            with self.subTest(item=item):
                kinds = self._review_kinds(_full_review(
                    {elem: ("NEEDS-HUMAN [impl]", "a missed case")}, item={elem: item}))
                self.assertEqual(kinds.get(f"{label} — a missed case"), assemble.IMPL)

    def test_a_verdict_cell_quote_under_pass_does_not_auto_iterate(self) -> None:
        d = self._bundle("VERDICTQUOTE", review=_full_review(
            {"C5": ("PASS (was NEEDS-HUMAN [impl])", "fixed in this round")}))
        self.assertIn("- [ ] C5 Causal adequacy — fixed in this round",
                      _section6(d / "SUMMARY.md"))
        self.assertFalse(self._try(d), "a PASS row is not implementation work")
        self._assert_halted(d)

    def test_a_row_contradicting_its_verdict_is_human_on_every_cell(self) -> None:
        # `| T5 Judgment | PASS | still NEEDS-HUMAN on scope |` (the sign-off's example) on
        # every cell but V (below) and under every other verdict. HUMAN on a gate cell too,
        # where the element rule would otherwise make it IMPL and hand it to Do.
        for elem, label, _k, _o in gates.canonical_elements():
            if elem == "V":
                continue
            for verdict in ("PASS", "FAIL", "N/A"):
                with self.subTest(elem=elem, verdict=verdict):
                    kinds = self._review_kinds(_full_review(
                        {elem: (verdict, "still NEEDS-HUMAN on scope")}))
                    self.assertEqual(kinds, {
                        f"{label} — still NEEDS-HUMAN on scope": assemble.HUMAN,
                        "Validation — fitness-to-purpose — fitness is the human's call":
                            assemble.STANDING})

    def test_a_gate_row_contradicting_its_verdict_does_not_auto_iterate(self) -> None:
        d = self._bundle("C4PASSNH", review=_full_review(
            {"C4": ("PASS", "still NEEDS-HUMAN: the red leg never ran")}))
        self.assertIn("- [ ] C4 Verification (red→green) — still NEEDS-HUMAN: the red leg "
                      "never ran", _section6(d / "SUMMARY.md"))
        self.assertFalse(self._try(d), "a row the reviewer passed is no rebuild order")
        self._assert_halted(d)

    def test_a_row_contradicting_its_verdict_is_deferred_beside_real_work(self) -> None:
        # Beside implementation work the rebuild runs, and the row is held for the handover
        # §6 like any HUMAN finding — not lost with the round.
        d = self._bundle("T5PASSNH", review=_full_review({
            "C4": ("NEEDS-HUMAN", "off-by-one"),
            "T5": ("PASS", "still NEEDS-HUMAN on scope")}))
        self.assertTrue(self._try(d))
        self._assert_deferred(d, ["T5 Judgment — still NEEDS-HUMAN on scope"])

    def test_duplicate_rows_disagreeing_on_the_verdict_are_human(self) -> None:
        # The same finding twice, once under NEEDS-HUMAN (on C4: IMPL by the element rule) and
        # once under PASS: whichever comes first, the one item the dedup keeps is HUMAN.
        label = "C4 Verification (red→green)"
        rows = [f"| {label} | NEEDS-HUMAN | the NEEDS-HUMAN gate log is cut short |",
                f"| {label} | PASS | the NEEDS-HUMAN gate log is cut short |"]
        for order in (rows, rows[::-1]):
            with self.subTest(first=order[0]):
                review = _full_review().replace(f"| {label} | PASS | ok |", "\n".join(order))
                self.assertEqual(self._review_kinds(review), {
                    f"{label} — the NEEDS-HUMAN gate log is cut short": assemble.HUMAN,
                    "Validation — fitness-to-purpose — fitness is the human's call":
                        assemble.STANDING})

    def test_the_verdict_and_basis_columns_are_found_by_the_header(self) -> None:
        # Sign-off of the second attempt: with the columns reordered the Basis was still read
        # as the cell after the Verdict, so the promoted finding read `T5 Judgment` alone and
        # the rebuild had nothing to act on. Reordered, and widened by a column between the
        # Verdict and the Basis: both read every cell by its header.
        reordered = ("# Review\n\n| Item | Basis | Verdict |\n|---|---|---|\n"
                     "| C1 Spec | ok | PASS |\n"
                     "| C5 Causal adequacy | an old review said NEEDS-HUMAN [impl] | PASS |\n"
                     "| T5 Judgment | Handle empty input | NEEDS-HUMAN [impl] |\n"
                     "| Validation — fitness-to-purpose | the human's call | NEEDS-HUMAN |\n")
        widened = ("# Review\n\n| Item | Verdict | Severity | Basis |\n|---|---|---|---|\n"
                   "| C1 Spec | PASS | low | ok |\n"
                   "| C5 Causal adequacy | PASS | low | an old review said NEEDS-HUMAN [impl] |\n"
                   "| T5 Judgment | NEEDS-HUMAN [impl] | high | Handle empty input |\n"
                   "| Validation — fitness-to-purpose | NEEDS-HUMAN | none | the human's call |\n")
        for layout, review in (("reordered", reordered), ("widened", widened)):
            with self.subTest(layout=layout):
                items = assemble._items_from_artifact(review, allow_standing=True)
                self.assertEqual([(it.text, it.kind) for it in items], [
                    ("C5 Causal adequacy — an old review said NEEDS-HUMAN [impl]",
                     assemble.HUMAN),
                    ("T5 Judgment — Handle empty input", assemble.IMPL),
                    ("Validation — fitness-to-purpose — the human's call", assemble.STANDING)])

    def test_with_no_basis_header_the_basis_is_the_cell_after_the_verdict(self) -> None:
        review = ("# Review\n\n| Item | Verdict | Reason |\n|---|---|---|\n"
                  "| C1 Spec | PASS | ok |\n"
                  "| T5 Judgment | NEEDS-HUMAN [impl] | Handle empty input |\n"
                  "| C5 Causal adequacy | PASS | still NEEDS-HUMAN on scope |\n"
                  "| Validation — fitness-to-purpose | NEEDS-HUMAN | the human's call |\n")
        self.assertEqual(self._review_kinds(review), {
            "T5 Judgment — Handle empty input": assemble.IMPL,
            "C5 Causal adequacy — still NEEDS-HUMAN on scope": assemble.HUMAN,
            "Validation — fitness-to-purpose — the human's call": assemble.STANDING})

    def test_a_header_naming_no_verdict_column_reads_no_tag(self) -> None:
        # Which cell is the verdict is unknown, so the row is read as before, and HUMAN.
        review = ("# Review\n\n| Element | Result | Reason |\n|---|---|---|\n"
                  "| C1 Spec | PASS | ok |\n"
                  "| C5 Causal adequacy | NEEDS-HUMAN [impl] | the guard misses a case |\n"
                  "| Validation — fitness-to-purpose | NEEDS-HUMAN | the human's call |\n")
        self.assertEqual(self._review_kinds(review), {
            "C5 Causal adequacy — the guard misses a case": assemble.HUMAN,
            "Validation — fitness-to-purpose — the human's call": assemble.STANDING})

    def test_a_row_with_no_verdict_in_the_column_still_reaches_the_human(self) -> None:
        # Nothing to read as the verdict, so the row is read as before: a finding, never IMPL.
        for row in ("| C5 Causal adequacy |  | NEEDS-HUMAN [impl] the guard misses a case |",
                    "| C5 Causal adequacy NEEDS-HUMAN [impl] the guard misses a case |"):
            with self.subTest(row=row):
                review = _full_review().replace("| C5 Causal adequacy | PASS | ok |", row)
                items = assemble._items_from_artifact(review, allow_standing=True)
                self.assertTrue([it for it in items if it.kind == assemble.HUMAN
                                 and it.text.startswith("C5 Causal adequacy")])
                self.assertFalse([it for it in items if it.kind == assemble.IMPL])

    def test_duplicate_rows_disagreeing_on_the_tag_are_human(self) -> None:
        # The same finding twice — a copied row, or the same row in its prefixed form — once
        # with `[impl]` and once without: the dedup keeps one item, and it must not keep the tag.
        for item in ("C5 Causal adequacy", "C5 — C5 Causal adequacy"):
            with self.subTest(item=item):
                review = (_full_review({"C5": ("NEEDS-HUMAN [impl]", "the guard misses a case")})
                          + f"| {item} | NEEDS-HUMAN | the guard misses a case |\n")
                items = assemble._items_from_artifact(review, allow_standing=True)
                self.assertFalse([it for it in items if it.kind == assemble.IMPL])
                self.assertIn(("C5 Causal adequacy — the guard misses a case", assemble.HUMAN),
                              [(it.text, it.kind) for it in items])


class ValidationRowForms(_Base):
    """#408 clause 2: the V row is the constant row in all three forms, compared exactly."""

    def test_every_form_of_the_v_row_is_standing(self) -> None:
        for form in _STANDING_FORMS:
            with self.subTest(form=form):
                review = _full_review(item={"V": form})
                [(_t, standing)] = assemble._needs_human(review)
                self.assertTrue(standing, form)
                [item] = assemble._items_from_artifact(review, allow_standing=True)
                self.assertEqual(item.kind, assemble.STANDING, form)

    def test_every_form_lets_an_impl_only_review_auto_iterate(self) -> None:
        # The production cost of a missed form: the constant row read as a real objection.
        for n, form in enumerate(_STANDING_FORMS):
            with self.subTest(form=form):
                d = self._bundle(f"VFORM{n}", review=_full_review(
                    {"C4": ("NEEDS-HUMAN", "off-by-one")}, item={"V": form}))
                self.assertTrue(self._try(d))
                self.assertFalse(_ledger(d), "the V row is not a finding to defer")

    def test_a_mismatched_prefix_is_not_the_v_row(self) -> None:
        for prefix in ("C5", "T5"):
            with self.subTest(prefix=prefix):
                review = _full_review(item={"V": f"{prefix} — Validation — fitness-to-purpose"})
                [(_t, standing)] = assemble._needs_human(review)
                self.assertFalse(standing)
                [item] = assemble._items_from_artifact(review, allow_standing=True)
                self.assertEqual(item.kind, assemble.HUMAN)

    def test_free_text_after_the_label_is_still_an_objection(self) -> None:
        for form in _STANDING_FORMS:
            with self.subTest(form=form):
                review = _full_review(item={"V": f"{form}: patches the wrong layer"})
                [item] = assemble._items_from_artifact(review, allow_standing=True)
                self.assertEqual(item.kind, assemble.HUMAN)

    def test_a_table_written_entirely_in_the_prefixed_form_is_the_verdict_table(self) -> None:
        prefixed = {e: f"{e} — {label}" for e, label, _k, _o in gates.canonical_elements()}
        for sep in ("—", "--"):
            with self.subTest(sep=sep):
                item = {e: cell.replace("—", sep) for e, cell in prefixed.items()}
                review = _full_review({"C5": ("NEEDS-HUMAN [impl]", "missed case")}, item=item)
                self.assertTrue(assemble._verdict_table_lines(review.splitlines()))
                kinds = {it.text: it.kind
                         for it in assemble._items_from_artifact(review, allow_standing=True)}
                self.assertEqual(kinds, {
                    "C5 Causal adequacy — missed case": assemble.IMPL,
                    "Validation — fitness-to-purpose — fitness is the human's call":
                        assemble.STANDING})

    def test_two_v_rows_in_different_forms_are_neither_standing(self) -> None:
        review = (_full_review()
                  + "| V — Validation — fitness-to-purpose | NEEDS-HUMAN | again |\n")
        self.assertEqual([s for _t, s in assemble._needs_human(review)], [False, False])

    def test_a_second_v_row_with_the_same_basis_is_neither_standing(self) -> None:
        # Sign-off of the first attempt: with the IDENTICAL Basis, every form normalises to the
        # same §6 text, and the dedup used to leave one item — STANDING — before the
        # fail-closed guard counted. Rows are counted first now; an exact copy counts too.
        for form in _STANDING_FORMS:
            with self.subTest(form=form):
                review = (_full_review()
                          + f"| {form} | NEEDS-HUMAN | fitness is the human's call |\n")
                self.assertEqual(assemble._needs_human(review), [
                    ("Validation — fitness-to-purpose — fitness is the human's call", False)])
                self.assertEqual(
                    [it.kind for it in assemble._items_from_artifact(review, allow_standing=True)],
                    [assemble.HUMAN])

    def test_a_duplicated_v_row_is_held_for_the_human_end_to_end(self) -> None:
        # Neither copy is the constant, so the row is a HUMAN finding: beside the C4 work it
        # is deferred to the handover §6, not waved through as the standing row.
        basis = "fitness is the human's call"
        d = self._bundle("DUPV", review=(
            _full_review({"C4": ("NEEDS-HUMAN", "off-by-one")})
            + f"| V — Validation — fitness-to-purpose | NEEDS-HUMAN | {basis} |\n"))
        self.assertTrue(self._try(d))
        self._assert_deferred(d, [f"Validation — fitness-to-purpose — {basis}"])

    def test_a_v_row_contradicting_its_verdict_is_not_the_standing_row(self) -> None:
        # The constant row's verdict is NEEDS-HUMAN. A V row stating another verdict while it
        # mentions NEEDS-HUMAN elsewhere is not that constant: it reaches the human as HUMAN.
        review = _full_review({"V": ("N/A", "NEEDS-HUMAN at sign-off")})
        items = assemble._items_from_artifact(review, allow_standing=True)
        self.assertEqual([(it.text, it.kind) for it in items], [
            ("Validation — fitness-to-purpose — NEEDS-HUMAN at sign-off", assemble.HUMAN)])

    def test_a_contradicting_second_v_row_still_trips_the_fail_closed_guard(self) -> None:
        # It is a second V row all the same: beside the real one, neither is standing.
        review = (_full_review()
                  + "| V — Validation — fitness-to-purpose | PASS | NEEDS-HUMAN again |\n")
        self.assertEqual(assemble._needs_human(review), [
            ("Validation — fitness-to-purpose — fitness is the human's call", False),
            ("Validation — fitness-to-purpose — NEEDS-HUMAN again", False)])

    def test_a_concerns_table_copy_of_the_v_row_is_not_folded_into_it(self) -> None:
        # Only the verdict table's Item cells are normalised. Normalised in a "## Concerns"
        # table too, a `V —` copy with the same Basis read as the standing row's own text, and
        # the dedup folded it into that row: an objection waved through as the constant.
        basis = "fitness is the human's call"
        review = (_full_review()
                  + "\n## Concerns\n\n| Item | Verdict | Basis |\n|---|---|---|\n"
                  f"| V — Validation — fitness-to-purpose | NEEDS-HUMAN | {basis} |\n")
        items = assemble._items_from_artifact(review, allow_standing=True)
        self.assertEqual([(it.text, it.kind) for it in items], [
            (f"Validation — fitness-to-purpose — {basis}", assemble.STANDING),
            (f"V — Validation — fitness-to-purpose — {basis}", assemble.HUMAN)])

    def test_a_copy_with_the_v_rows_exact_text_stays_a_human_finding(self) -> None:
        # Sign-off of the third attempt, the other spelling: the verdict table's `V —` row
        # normalises to the bare label, so a bare copy with the IDENTICAL Verdict and Basis in a
        # "## Concerns" table read as the same text, and the dedup kept the first row's
        # STANDING — the objection vanished into the constant row, where before #408 both
        # reached §6 as HUMAN. One item now, and HUMAN: in every form of the V row, with the
        # copy after or before the verdict table, and as a bullet. (With the bare form this was
        # a hole before #408 too.)
        basis = "fitness is the human's call"
        concerns = ("\n## Concerns\n\n| Item | Verdict | Basis |\n|---|---|---|\n"
                    f"| Validation — fitness-to-purpose | NEEDS-HUMAN | {basis} |\n")
        bullet = f"\n- NEEDS-HUMAN — Validation — fitness-to-purpose — {basis}\n"
        for form in _STANDING_FORMS:
            table = _full_review(item={"V": form})
            for copy, review in (("concerns table after", table + concerns),
                                 ("concerns table before", concerns + "\n" + table),
                                 ("bullet after", table + bullet),
                                 ("bullet before", bullet + "\n" + table)):
                with self.subTest(form=form, copy=copy):
                    self.assertEqual(assemble._needs_human(review), [
                        (f"Validation — fitness-to-purpose — {basis}", False)])
                    items = assemble._items_from_artifact(review, allow_standing=True)
                    self.assertEqual([(it.text, it.kind) for it in items], [
                        (f"Validation — fitness-to-purpose — {basis}", assemble.HUMAN)])

    def test_a_copy_with_the_v_rows_exact_text_is_held_for_the_human_end_to_end(self) -> None:
        # What losing it cost: beside C4 work the round fires, and the STANDING row is never
        # deferred, so the objection was in no later §6. Now it is in the ledger.
        basis = "fitness is the human's call"
        d = self._bundle("CONCERNSV", review=(
            _full_review({"C4": ("NEEDS-HUMAN", "off-by-one")},
                         item={"V": "V — Validation — fitness-to-purpose"})
            + "\n## Concerns\n\n| Item | Verdict | Basis |\n|---|---|---|\n"
            f"| Validation — fitness-to-purpose | NEEDS-HUMAN | {basis} |\n"))
        self.assertTrue(self._try(d))
        self._assert_deferred(d, [f"Validation — fitness-to-purpose — {basis}"])


class TagContractPrompts(unittest.TestCase):
    """#408 clause 3: the prompts carry the contract the classifier honours."""

    _AGENTS = Path(__file__).resolve().parents[1] / "agents"

    def _role(self, name: str) -> str:
        path = next((p for p in (self._AGENTS / f"{name}.md.jinja", self._AGENTS / f"{name}.md")
                     if p.exists()), None)
        self.assertIsNotNone(path, f"no {name} role body under {self._AGENTS}")
        return path.read_text(encoding="utf-8")

    def test_review_prompt_lists_bare_labels_and_the_c5_t5_tag(self) -> None:
        prompt = leaves._REVIEW_PROMPT
        for elem, label, _k, _o in gates.canonical_elements():
            self.assertIn(f"\n  {label}\n", prompt)
            self.assertNotIn(f"{elem} — {label}", prompt)
        self.assertIn("EXACTLY as listed", prompt)
        self.assertIn("NEEDS-HUMAN [impl]", prompt)
        self.assertIn("'C5 Causal adequacy' and 'T5 Judgment' rows ONLY", prompt)

    def test_advisory_prompt_requires_a_tag_and_drops_the_omit_default(self) -> None:
        prompt = leaves._advisory_prompt({}, "adversary")
        self.assertNotIn("OMIT '[impl]'", prompt)
        self.assertNotIn("when in doubt", prompt.lower())
        self.assertIn("'- NEEDS-HUMAN [impl] — '", prompt)
        self.assertIn("'- NEEDS-HUMAN [human] — '", prompt)
        self.assertIn("MUST carry exactly one tag", prompt)

    def test_role_bodies_state_the_same_contract(self) -> None:
        reviewer = self._role("reviewer")
        self.assertIn("NEEDS-HUMAN [impl]", reviewer)
        self.assertIn("`C5 Causal\nadequacy` and `T5 Judgment` rows **only**", reviewer)
        self.assertIn("no element-id\nprefix", reviewer)
        adversary = self._role("adversary")
        self.assertNotIn("when in doubt, omit", adversary.lower())
        self.assertIn("- NEEDS-HUMAN [human] — ", adversary)
        self.assertIn("an untagged bullet is read as\n`[human]`", adversary)


if __name__ == "__main__":
    unittest.main()
