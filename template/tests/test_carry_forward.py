"""Iterate carry-forward — the §9 rationale + failing gates fold into brief.md so the
next attempt isn't blind (stdlib unittest, no deps).

Covers: the sign-off decision file carries a token (line 1) + an optional rationale
(below); §9 'Iteration delta' is read back; and driver._carry_forward_into_brief
appends an '## Iteration N' block to brief.md before the iterate clear.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pdca_harness import driver, leaves, signoff


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


_SUMMARY_WITH_DELTA = (
    "## 9. Check sign-off\n"
    "- Outcome: iterated-to-Do\n"
    "- By / date: Tester / 2026-06-07\n"
    "- Iteration delta (if iterating): guard was a symptom; remove the import-time cause\n"
)
_GATES_ONE_FAIL = (
    '{"rows": [{"check": "C4-verify", "result": "fail", "path_line": "test_x.py:3", '
    '"gating": true}, {"check": "T2-shape", "result": "pass", "gating": false}]}'
)


class SignoffDecisionFile(unittest.TestCase):
    def test_token_on_line1_rationale_below(self) -> None:
        d = _tmp()
        (d / leaves.SIGNOFF_DECISION).write_text(
            "iterate-do\nthe probe hid the real cause; remove it\nand compute lazily\n",
            encoding="utf-8")
        self.assertEqual(leaves.signoff_decision(d), "iterate-do")  # first line only
        self.assertEqual(
            leaves.signoff_rationale(d),
            "the probe hid the real cause; remove it\nand compute lazily")

    def test_token_only_has_empty_rationale(self) -> None:
        d = _tmp()
        (d / leaves.SIGNOFF_DECISION).write_text("accept\n", encoding="utf-8")
        self.assertEqual(leaves.signoff_decision(d), "accept")
        self.assertEqual(leaves.signoff_rationale(d), "")


class IterationDelta(unittest.TestCase):
    def test_reads_section9_delta(self) -> None:
        d = _tmp()
        (d / "SUMMARY.md").write_text(_SUMMARY_WITH_DELTA, encoding="utf-8")
        self.assertEqual(
            signoff.iteration_delta(d / "SUMMARY.md"),
            "guard was a symptom; remove the import-time cause")

    def test_absent_summary_is_empty(self) -> None:
        self.assertEqual(signoff.iteration_delta(_tmp() / "nope.md"), "")


class CarryForwardIntoBrief(unittest.TestCase):
    def test_appends_block_with_rationale_and_failing_gate(self) -> None:
        d = _tmp()
        (d / "brief.md").write_text("# Brief\n- **Slug:** x\n", encoding="utf-8")
        (d / "SUMMARY.md").write_text(_SUMMARY_WITH_DELTA, encoding="utf-8")
        (d / "check-gates.json").write_text(_GATES_ONE_FAIL, encoding="utf-8")
        driver._carry_forward_into_brief(d, 1)
        brief = (d / "brief.md").read_text(encoding="utf-8")
        self.assertIn("## Iteration 1 — carry-forward", brief)
        self.assertIn("Sign-off rationale: guard was a symptom", brief)
        self.assertIn("Failing gate: C4-verify — test_x.py:3", brief)
        self.assertIn("preserved in `iteration-v1/`", brief)  # points at the archive
        self.assertNotIn("T2-shape", brief)  # only failing rows carry forward
        # A second iterate appends the block for its own iteration number.
        driver._carry_forward_into_brief(d, 2)
        self.assertIn("## Iteration 2 — carry-forward", (d / "brief.md").read_text())

    def test_no_brief_or_nothing_to_carry_is_a_noop(self) -> None:
        d = _tmp()  # no brief.md → no-op, no crash
        driver._carry_forward_into_brief(d, 1)
        self.assertFalse((d / "brief.md").exists())
        d2 = _tmp()
        (d2 / "brief.md").write_text("# Brief\n", encoding="utf-8")  # nothing to carry
        driver._carry_forward_into_brief(d2, 1)
        self.assertNotIn("Iteration", (d2 / "brief.md").read_text(encoding="utf-8"))


class BuildPromptMandate(unittest.TestCase):
    def test_prompt_mandates_success_criterion_and_carry_forward(self) -> None:
        prompt = leaves._build_prompt(Path("/tmp/results/issue_X"))
        self.assertIn("Success criterion", prompt)
        self.assertIn("carry-forward", prompt)


if __name__ == "__main__":
    unittest.main()
