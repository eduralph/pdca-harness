"""A verification leg is judged by TWO facts — the runner's exit code AND whether any test
actually ran (issue #434).

A test runner exits non-zero for two unrelated reasons: the test ran and failed (the C4 red
leg's proof), or no test ran at all — it failed to compile/import/collect, or the runner
died. A leg judged on the exit code alone cannot tell those apart, so it reports PASS for a
bundle whose test never executed. That is an everyday shape: reverting the fix also removes
any symbol the fix introduced, so a test calling one fails to build on exactly that leg.

The harness does not run anyone's gate — it publishes the instructions each rendered
instance writes its gate from (`engine/scripts/run-verify.sh`, an outline; the longer
explanation in `engine/README.md.jinja`). So the rule lives in that wording, and this suite
holds the wording in place, the same way `test_verify_base.py`
(`test_the_c4_skeleton_names_the_export_as_the_last_rung`) holds the base ladder in place.

Invariant: a gate never turns "no evidence" into a pass. A step in which no test ran is
UNVERIFIABLE (exit 77 -> SUMMARY §6 NEEDS-HUMAN), never a pass and never a fail.

No model/network. Run from the project root:
    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

# The C4 outline every rendered instance fills in: what it says about deciding a leg's
# verdict IS the rule, because nothing else in the harness can decide it (how many tests ran
# depends on the project's language and runner).
_TEMPLATE_ROOT = Path(__file__).resolve().parents[1]
_ENGINE = _TEMPLATE_ROOT / "engine"
# `run-verify.sh` carries no `.jinja` suffix, so copier copies it verbatim (copier.yml:14).
# Unlike `pdca.toml`, this file's own NAME never signals which posture it is in — every
# instance is instructed to overwrite it with its own gate — so the posture is read off the
# project root's `pdca.toml(.jinja)` instead, the same signal `test_families.py` and
# `test_remote_control_docs.py` use (issue #507).
_SKELETON = _ENGINE / "scripts" / "run-verify.sh"
_TOML = next((_TEMPLATE_ROOT / n for n in ("pdca.toml.jinja", "pdca.toml")
             if (_TEMPLATE_ROOT / n).is_file()), None)
RENDERED = _TOML is not None and _TOML.name == "pdca.toml"
# …and the prose that carries the longer explanation, §"The two gate shapes that matter".
# `README.md.jinja` in the template checkout, `README.md` in a rendered instance — this
# suite ships to instances and the render/update-compat suites run it there.
_ENGINE_README = next(_ENGINE / n for n in ("README.md.jinja", "README.md")
                      if (_ENGINE / n).exists())

# The outline's verdict table: `<exit code> | <tests ran> | <what to report>`.
_ROW = r"^[#\s]*{exit}\s*\|\s*{ran}\s*\|(?P<verdict>.*)$"
# …and the README's, as a Markdown table row (indented under the C4 bullet).
_MD_ROW = r"^ *\| *(0|non-zero) *\| *(0|>0) *\| *(.+?) *\|$"


def _prose(text: str) -> str:
    """Sentences, free of shell comment markers and of where the author happened to wrap.

    A rule stated in prose is asserted as prose: re-flowing a paragraph must not break this
    suite, while changing what it SAYS must."""
    return re.sub(r"\s+", " ", re.sub(r"(?m)^\s*#", "", text)).strip()


class _WordingCase(unittest.TestCase):
    """Assertions about what a published file SAYS, reported as the missing sentence."""

    def assertSays(self, needle: str, haystack: str, where: str) -> None:
        """`assertIn` names the whole haystack on failure — a page of file dumped into the
        gate log, burying the one sentence that is missing. Name the sentence instead."""
        if needle not in haystack:
            self.fail(f"{where} does not state: {needle!r}")


class C4RedLegVerdictRule(_WordingCase):
    """The published instructions must make "no test ran" its own outcome, not a red.

    Binds the TEMPLATE CHECKOUT ONLY (issue #507): `run-verify.sh` is the one file every
    instance is *told* to replace (`engine/scripts/run-verify.sh:2`, "SKELETON. Fill this
    in for your project."; `engine/README.md.jinja:31,84`) — what the harness *publishes*
    in its skeleton, not what an instance's own filled-in gate must go on quoting."""

    #: Overridable by `C4SkeletonWordingPostures` below to drive this suite's own
    #: assertions against synthetic text/posture in a temp dir, without touching the real
    #: checkout and without a subprocess (issue #507's fork-storm constraint).
    SKELETON_TEXT: str | None = None
    RENDERED: bool = RENDERED

    def setUp(self) -> None:
        if self.RENDERED:
            self.skipTest("run-verify.sh is instructed to become the instance's own "
                          "filled-in gate once rendered — the skeleton wording binds "
                          "the template checkout only (issue #507)")
        self.text = self.SKELETON_TEXT if self.SKELETON_TEXT is not None \
            else _SKELETON.read_text(encoding="utf-8")

    def _two_factor_block(self) -> str:
        """The section of the outline that states the verdict rule.

        Scoped, so an assertion about the exit-77 vocabulary cannot be satisfied by the
        unrelated #165 classification paragraph further down the same file."""
        start = "JUDGE EVERY LEG BY TWO FACTS"
        end = "CLASSIFY THE PATCH FIRST"
        self.assertSays(start, self.text, "the C4 outline")
        self.assertSays(end, self.text, "the C4 outline")
        return self.text[self.text.index(start):self.text.index(end)]

    def _verdict_for(self, exit_code: str, tests_ran: str) -> str:
        """The verdict cell of the table row for (exit code, tests ran)."""
        pattern = _ROW.format(exit=re.escape(exit_code), ran=re.escape(tests_ran))
        m = re.search(pattern, self._two_factor_block(), re.MULTILINE)
        self.assertIsNotNone(
            m, f"no published verdict for exit={exit_code}, tests ran={tests_ran}")
        return m.group("verdict")

    def test_a_leg_is_decided_by_the_exit_code_and_by_whether_a_test_ran(self) -> None:
        """Two facts, named as such — an exit code alone cannot carry the verdict."""
        block = _prose(self._two_factor_block())
        for sentence in ("the runner's exit code AND how many tests actually ran",
                         "COUNT of executed tests",
                         "Never infer that count from the exit code."):
            self.assertSays(sentence, block, "the C4 outline")
        # …and the pseudocode no longer tells a project to take any FAIL as the red.
        self.assertNotIn("expect FAIL (red)", self.text)

    def test_nonzero_exit_with_no_test_run_is_unverifiable_never_pass(self) -> None:
        """The row this issue exists for: the runner died before running anything, so its
        non-zero exit proves nothing — routing it to §6 is the only honest answer."""
        verdict = self._verdict_for("non-zero", "0")
        self.assertIn("PDCA-UNVERIFIABLE", verdict)
        self.assertIn("77", verdict)
        self.assertIn("NEVER PASS", verdict)

    def test_a_real_red_still_needs_a_test_that_ran_and_failed(self) -> None:
        """The neighbouring row stays intact: non-zero WITH tests executed is the red."""
        self.assertIn("the red you want", self._verdict_for("non-zero", ">0"))

    def test_a_clean_exit_with_no_test_run_stays_unverifiable_too(self) -> None:
        verdict = self._verdict_for("0", "0")
        self.assertIn("PDCA-UNVERIFIABLE", verdict)
        self.assertIn("77", verdict)

    def test_the_two_no_test_ran_cases_stay_distinguishable(self) -> None:
        """Same verdict, different causes: nothing was selected vs the test did not build.
        §6 needs different things from the human, so the reason must say which."""
        block = _prose(self._two_factor_block())
        for sentence in ("distinguishable",
                         "no test executed (runner exited 0:",
                         "no test executed (runner exited <rc>:"):
            self.assertSays(sentence, block, "the C4 outline")

    def test_the_rule_is_stated_for_every_verification_step(self) -> None:
        """Not a one-off patch to one leg of one script: the invariant is published for
        every step, and says explicitly that the outcome is neither a pass nor a fail."""
        block = _prose(self._two_factor_block())
        for sentence in ("for every leg you add here and for every other verification step",
                         "never a pass and never a fail",
                         'A gate never turns "no evidence" into a verdict.'):
            self.assertSays(sentence, block, "the C4 outline")

    def test_the_rule_reuses_the_existing_unverifiable_channel(self) -> None:
        """Exit 77 / the `PDCA-UNVERIFIABLE:` marker -> §6 NEEDS-HUMAN, non-gating — the
        harness's existing "no verdict was earned" channel, not a second vocabulary."""
        block = _prose(self._two_factor_block())
        for sentence in ("exit 77", "PDCA-UNVERIFIABLE: <reason>",
                         "SUMMARY §6", "NEEDS-HUMAN, non-gating"):
            self.assertSays(sentence, block, "the C4 outline")


class C4SkeletonWordingPostures(unittest.TestCase):
    """Posture regressions (issue #507). Drives the REAL `C4RedLegVerdictRule` suite
    in-process against synthetic text/posture — no subprocess (the brief's fork-storm
    constraint; the Success criterion already mandates this shape) — by pointing its
    overridable `SKELETON_TEXT`/`RENDERED` at synthetic values instead of the checkout's
    own files, the same `RemoteControlPostures`-style construction #386 used, applied to
    a whole TestCase run rather than one pure function."""

    #: A filled-in project gate that quotes none of the skeleton's wording — the shape
    #: `engine/README.md.jinja:31,84` instructs every instance to replace the skeleton
    #: with (a real apply/run/revert script for the project's own runner).
    _FILLED_IN = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "# our project's own C4 gate\n"
        "pytest -q --junitxml=report.xml\n"
        "exit $?\n"
    )

    def _run(self, text: str, rendered: bool) -> unittest.TestResult:
        suite = unittest.TestSuite()
        for name in unittest.defaultTestLoader.getTestCaseNames(C4RedLegVerdictRule):
            case = C4RedLegVerdictRule(name)
            case.SKELETON_TEXT = text
            case.RENDERED = rendered
            suite.addTest(case)
        result = unittest.TestResult()
        suite.run(result)
        return result

    def test_the_unrendered_posture_still_requires_the_full_wording(self) -> None:
        """Posture (i): today's green, unchanged — the real skeleton text against the
        template checkout."""
        result = self._run(_SKELETON.read_text(encoding="utf-8"), rendered=False)
        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)

    def test_a_filled_in_gate_missing_the_wording_is_not_flagged_once_rendered(self) -> None:
        """Posture (iv): a rendered instance replaced the skeleton with its own real gate
        that quotes none of the skeleton's wording — 8 failures today (these 7 plus the
        base-ladder case in `test_verify_base.py`). Must be green: the property is scoped
        to what the harness PUBLISHES, not to what every instance is instructed to
        overwrite (issue #507)."""
        result = self._run(self._FILLED_IN, rendered=True)
        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
        self.assertEqual(result.testsRun, len(result.skipped))

    def test_the_same_filled_in_text_would_fail_if_it_were_still_bound(self) -> None:
        """Negative control: without the posture scope this fixture fails loudly (it
        quotes none of the skeleton's wording), so the previous test is exercising the
        skip, not a fixture that happens to pass anyway."""
        result = self._run(self._FILLED_IN, rendered=False)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.failures), 7, result.failures)


class EngineReadmeExplainsTheRule(_WordingCase):
    """The outline is terse by design; the engine README carries the reasoning a project
    needs to write the check, in §"The two gate shapes that matter"."""

    def setUp(self) -> None:
        self.text = _ENGINE_README.read_text(encoding="utf-8")
        self.prose = _prose(self.text)

    def test_the_c4_section_explains_judging_a_leg_by_two_facts(self) -> None:
        for sentence in ("The two gate shapes that matter",
                         "the exit code AND how many tests actually ran",
                         "count of executed tests"):
            self.assertSays(sentence, self.prose, f"engine/{_ENGINE_README.name}")

    def test_the_readme_names_the_wrong_verdict_it_prevents(self) -> None:
        self.assertSays("PASS for a bundle whose test never executed", self.prose,
                        f"engine/{_ENGINE_README.name}")

    def test_the_readme_publishes_the_same_four_outcomes(self) -> None:
        rows = re.findall(_MD_ROW, self.text, re.MULTILINE)
        verdicts = {(e, r): v for e, r, v in rows}
        self.assertEqual(len(verdicts), 4, f"not all four outcomes are published: {verdicts}")
        self.assertIn("UNVERIFIABLE", verdicts[("non-zero", "0")])
        self.assertIn("never PASS", verdicts[("non-zero", "0")])
        self.assertIn("UNVERIFIABLE", verdicts[("0", "0")])

    def test_the_readme_generalises_the_rule_beyond_the_red_leg(self) -> None:
        for sentence in ("every** verification step, not just C4's red leg",
                         'A gate never turns "no evidence" into a verdict.'):
            self.assertSays(sentence, self.prose, f"engine/{_ENGINE_README.name}")


if __name__ == "__main__":
    unittest.main()
