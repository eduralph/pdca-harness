"""§9 records the human's sign-off text verbatim — it is data, never a regex template
(issue #529) — except that line breaks become spaces, so every §9 value stays on one line
(issue #604).

``signoff.record``'s ``set_field`` helper (``signoff.py:180-185`` pre-fix) passed the
human's rationale / ``by`` straight to ``re.subn`` as the ``repl`` argument — a string
``repl`` has its OWN backslash-escape syntax (``\\g<1>``, ``\\1``, ...), documented by
`re.sub` in contrast to a callable ``repl``, whose return value is used as-is. Three
failures followed, all reproduced here against the recordable SUMMARY shape from
``test_handoff.py:72-80`` (``_SUMMARY``):

  (a) a rationale containing an ordinary regex escape (``^\\W*``, quoting the code
      under review — the normal shape of a rejection rationale) raised ``re.error``;
  (b) a rationale that happened to spell a valid group reference (``\\g<1>``) was
      silently EXPANDED instead of recorded, doubling the field's own label into the
      text and dropping the human's words;
  (c) the same applies to ``by`` (``f"{by} / {date}"``, ``signoff.py:203``), e.g. a
      Windows-style domain login ``CORP\\dev``.

RED on the pre-fix tree: (a)/(c)/(d) raise ``re.error`` at ``signoff.py:184`` (NOT
asserted by exception type — ``re.PatternError`` is a 3.13-only name, and the brief's
falsifiability clause forbids naming it), and (b) records the label twice instead of
the literal text once.

#604: a multi-line ``--delta`` (or ``--by``) holding a ``## 6. NEEDS-HUMAN`` line put that
heading into §9, after assemble's §6 — so it became the LAST §6 heading, the one every §6
reader takes, and its ``- [x]`` rows retired deferred findings the human never cleared
(``driver._retire_cleared_deferrals`` → ``autoiterate.retire_cleared``). ``record`` now
replaces each run of ``str.splitlines`` line boundaries with one space; a value without one
is still recorded byte-for-byte. RED on the pre-fix tree: the forged legs of
``ForgedSection6InSignoffTextRetiresNothing`` empty the ledger (``[]`` / ``[E2]``).

Run from ``template/``: ``PYTHONPATH=src python3 -m unittest tests.test_signoff_verbatim``
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import unittest
from io import StringIO
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from pdca_harness import assemble, autoiterate, cli, flow, gates, leaves, signoff, state
from pdca_harness.config import Config, LeafConfig

# The recordable SUMMARY fixture, byte-identical to `test_handoff.py:72-80` (peer
# callsite named in the brief): §9 with an Outcome field and a non-empty §6.
_SUMMARY = (
    "# SUMMARY\n\n"
    "## 6. NEEDS-HUMAN\n"
    "- [x] cleared by the human\n\n"
    "## 9. Check sign-off\n"
    "- Outcome:\n"
    "- By / date:\n"
    "- Iteration delta (if iterating):\n"
)


def _bundle() -> Path:
    d = Path(tempfile.mkdtemp())
    (d / "SUMMARY.md").write_text(_SUMMARY, encoding="utf-8")
    return d


def _section9(summary_text: str) -> str:
    return summary_text.split("## 9. Check sign-off", 1)[1]


def _line(summary_text: str, label: str) -> str:
    """The full §9 line for ``label`` — e.g. ``"- Outcome:"``."""
    section = _section9(summary_text)
    for raw in section.splitlines():
        if raw.strip().startswith(f"- {label}:"):
            return raw
    raise AssertionError(f"no {label!r} line in §9:\n{section}")


class RationaleWithARegexEscapeIsRecordedVerbatim(unittest.TestCase):
    """(a): a rationale quoting a regex — the normal shape of a rejection rationale
    (the brief's own example, observed on pdca-pdca issue_506: `^\\W*`)."""

    def test_a_leading_caret_escape_does_not_raise_and_is_recorded_byte_for_byte(self):
        d = _bundle()
        delta = r"the _ERROR_LEAD_RE's `^\W*` lead"
        signoff.record(d / "SUMMARY.md", action="iterate-do", by="T",
                       date="2026-09-15", delta=delta)
        line = _line((d / "SUMMARY.md").read_text(encoding="utf-8"),
                     "Iteration delta (if iterating)")
        self.assertTrue(line.endswith(delta), f"expected line to end with {delta!r}: {line!r}")


class RationaleWithAValidGroupReferenceIsNotExpanded(unittest.TestCase):
    """(b): `\\g<1>` is a VALID re.sub template reference — it must not be silently
    expanded into the field's own label, doubling it into the recorded text."""

    def test_group_reference_syntax_is_kept_literal(self):
        d = _bundle()
        delta = r"\g<1> literal"
        signoff.record(d / "SUMMARY.md", action="iterate-do", by="T",
                       date="2026-09-15", delta=delta)
        text = (d / "SUMMARY.md").read_text(encoding="utf-8")
        line = _line(text, "Iteration delta (if iterating)")
        self.assertIn(r"\g<1>", line)
        # The pre-fix bug wrote the label TWICE — once for the field itself, once as
        # the "expansion" of \g<1> — so guard against a second copy of the label.
        self.assertEqual(line.count("Iteration delta (if iterating):"), 1)


class ByWithABackslashEscapeIsRecordedVerbatim(unittest.TestCase):
    """(c): `by` (e.g. a `CORP\\dev`-style domain login) is recorded literally in
    `- By / date:`, for every action — including `accept`, which the pre-fix
    template-string path treated no differently from an iterate."""

    def test_by_is_literal_for_every_action(self):
        for action in ("accept", "iterate-do", "iterate-plan", "discontinue"):
            with self.subTest(action=action):
                d = _bundle()
                signoff.record(d / "SUMMARY.md", action=action, by=r"CORP\dev",
                               date="2026-09-15")
                line = _line((d / "SUMMARY.md").read_text(encoding="utf-8"), "By / date")
                self.assertIn(r"CORP\dev / 2026-09-15", line)


class EndToEndThroughApplyDecision(unittest.TestCase):
    """(d): mirrors `test_flow_captures_the_full_rationale_before_the_unlink`
    (`test_handoff.py:411-424`, the peer callsite named in the brief) — drives
    `flow._apply_decision` with a `signoff-decision` file, not `signoff.record`
    directly, so the production caller path (including the pre-fix `re.error`
    that escaped to `flow._apply_decision`'s own `except ValueError`, #529 item 3)
    is exercised too."""

    def test_iterate_do_with_a_regex_escaping_rationale_records_and_unlinks(self):
        d = _bundle()
        (d / leaves.SIGNOFF_DECISION).write_text("iterate-do\nthe ^\\W* lead\n",
                                                  encoding="utf-8")
        cfg = Config(
            root=d, bundle_root=d, process_dir=d, templates_dir=d,
            default_branch="main", tracker_system="github", tracker_url="",
            issue_id_example="#1", builder=LeafConfig(), reviewer=LeafConfig(),
        )
        with redirect_stderr(StringIO()):
            action = flow._apply_decision(cfg, d, by="T", today="2026-09-15",
                                          apply_now=False)
        self.assertEqual(action, "iterate-do")
        self.assertFalse((d / leaves.SIGNOFF_DECISION).exists())  # consumed
        text = (d / "SUMMARY.md").read_text(encoding="utf-8")
        line = _line(text, "Iteration delta (if iterating)")
        self.assertTrue(line.endswith("the ^\\W* lead"), line)


def _stub_config(root: Path) -> Config:
    """Stub leaves, real gate commands — the shape of `test_autoiterate._stub_config`."""
    return Config(
        root=root, bundle_root=root / "results", process_dir=root / "process",
        templates_dir=root / "templates", default_branch="main", tracker_system="github",
        tracker_url="", issue_id_example="#1",
        builder=LeafConfig(mode="stub", family="claude"),
        reviewer=LeafConfig(mode="stub", family="codex"),
    )


_PASS_GATE = {"id": "C4", "tier": "C4", "label": "verify", "scope": "bundle", "gating": True,
              "cmd": "true"}


class ForgedSection6InSignoffTextRetiresNothing(unittest.TestCase):
    """#604: text the human types into §9 cannot change which §6 the driver reads.

    The bundle lives at ``cfg.bundle(id)`` (``cli._signoff`` resolves it there) and carries
    every Check artifact ``assemble.collect_needs_human`` reads when
    ``driver._retire_cleared_deferrals`` calls it — built the way
    ``test_autoiterate._Base._bundle`` builds one. The ledger holds E1 and E2, written before
    assembly so assemble's §6 renders both as open ``- [ ]`` rows.
    """

    ID = "604"
    LEDGER = ["E1", "E2"]

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)
        d = self.cfg.bundle(self.ID)
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** forged-section6\n", encoding="utf-8")
        (d / "patch.diff").write_text("--- a\n+++ b\n", encoding="utf-8")
        (d / "check-review.md").write_text("All advisory items PASS.\n", encoding="utf-8")
        (d / autoiterate.DEFERRED_FILE).write_text(
            json.dumps({"items": self.LEDGER}), encoding="utf-8")
        self.cfg.gates_checks = [_PASS_GATE]
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            gates.run_gates(d, self.cfg)
            assemble.assemble_summary(d, self.cfg)
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)
        self.d = d
        # The fixture includes the fault's target: both entries are OPEN in assemble's §6.
        sec6 = signoff._needs_human_section(self._summary(), whole_on_missing=False)
        for entry in self.LEDGER:
            self.assertIn(f"- [ ] {entry}", sec6)
        self.assertEqual(autoiterate.deferred(d), self.LEDGER)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _summary(self) -> str:
        return (self.d / "SUMMARY.md").read_text(encoding="utf-8")

    def _signoff_then_advance(self, *, by: str, delta: str) -> str:
        """Sign off through the CLI path and return the SUMMARY the sign-off wrote.

        ``cli._signoff`` applies the transition itself (``driver.run_issue``, which calls
        ``driver.advance`` until the bundle halts): the ITERATE_DO beat retires cleared
        deferrals, archives the signed attempt to ``iteration-v1/``, then the stub builder
        and Check run again. So the signed SUMMARY is the archived one.
        """
        args = argparse.Namespace(issue_id=self.ID, accept=False, iterate_do=True,
                                  iterate_plan=False, discontinue=False, by=by,
                                  no_publish=True, delta=delta)
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(cli._signoff(self.cfg, args), 0)
        archived = self.d / "iteration-v1" / "SUMMARY.md"
        self.assertTrue(archived.exists(), "the ITERATE_DO beat did not run")
        signed = archived.read_text(encoding="utf-8")
        self.assertEqual(signoff.outcome_token(archived), "iterated-to-Do")
        # Back at a halt, as an ordinary iterate leaves it; one more advance is a no-op
        # for the ledger whatever it does.
        self.assertEqual(state.state(self.d), state.AWAITING_SIGNOFF)
        return signed

    def _assert_one_section6_heading(self, signed: str) -> None:
        heads = [ln for ln in signed.splitlines() if ln.startswith("## 6. NEEDS-HUMAN")]
        self.assertEqual(len(heads), 1, heads)

    def test_a_multi_line_delta_cannot_forge_section6(self) -> None:
        signed = self._signoff_then_advance(
            by="human", delta="notes\n## 6. NEEDS-HUMAN (from v1)\n- [x] E1\n- [x] E2")
        self.assertEqual(autoiterate.deferred(self.d), ["E1", "E2"],
                         "a §6 block typed into --delta retired deferred findings")
        self._assert_one_section6_heading(signed)
        line = _line(signed, "Iteration delta (if iterating)")
        self.assertIn("notes", line)
        self.assertIn("E1", line)
        self.assertIn("human", _line(signed, "By / date"))

    def _assert_by_cannot_forge_section6(self, by: str) -> None:
        signed = self._signoff_then_advance(by=by, delta="notes")
        self.assertEqual(autoiterate.deferred(self.d), ["E1", "E2"],
                         "a §6 block typed into --by retired a deferred finding")
        self._assert_one_section6_heading(signed)
        self.assertIn("notes", _line(signed, "Iteration delta (if iterating)"))
        by_line = _line(signed, "By / date")
        self.assertIn("me", by_line)
        self.assertIn("E1", by_line)

    def test_a_multi_line_by_cannot_forge_section6(self) -> None:
        # Pre-fix the forged row reads `- [x] E1 / <date>` (record appends ` / <date>` to
        # `by`), so this value forges a second §6 heading but not a retiring tick.
        self._assert_by_cannot_forge_section6("me\n## 6. NEEDS-HUMAN\n- [x] E1")

    def test_a_multi_line_by_ending_in_a_line_break_cannot_retire_an_entry(self) -> None:
        # The trailing break pushes ` / <date>` off the forged row, which pre-fix reads
        # `- [x] E1` exactly and retires E1 (the ledger was `[E2]`).
        self._assert_by_cannot_forge_section6("me\n## 6. NEEDS-HUMAN\n- [x] E1\n")

    def test_positive_control_a_real_tick_retires_its_entry(self) -> None:
        """Proves the retire step runs on this fixture, so the forged legs' RED is real."""
        summ = self.d / "SUMMARY.md"
        text = self._summary()
        self.assertEqual(text.count("- [ ] E1\n"), 1)
        summ.write_text(text.replace("- [ ] E1\n", "- [x] E1\n"), encoding="utf-8")
        self._signoff_then_advance(by="human", delta="notes")
        self.assertEqual(autoiterate.deferred(self.d), ["E2"])

    def test_every_splitlines_boundary_is_flattened_and_plain_text_is_untouched(self) -> None:
        """"One line" means one line to `str.splitlines`, the split every reader uses — not
        just `\\n`. And a value with no line break is still recorded byte-for-byte (#529)."""
        boundaries = [c for c in map(chr, range(0x110000)) if len(f"a{c}b".splitlines()) > 1]
        self.assertIn("\u2028", boundaries)
        for c in boundaries + ["\r\n", "\n\n\r"]:
            with self.subTest(boundary=repr(c)):
                d = _bundle()
                signoff.record(d / "SUMMARY.md", action="iterate-do", by=f"me{c}x",
                               date="2026-10-10", delta=f"notes{c}## 6. NEEDS-HUMAN{c}- [x] E1")
                text = (d / "SUMMARY.md").read_text(encoding="utf-8")
                self.assertEqual(text.count("\n## 6. NEEDS-HUMAN"), 1)
                self.assertEqual(len([ln for ln in text.splitlines()
                                      if ln.startswith("## 6. NEEDS-HUMAN")]), 1)
                self.assertTrue(_line(text, "Iteration delta (if iterating)").endswith(
                    "notes ## 6. NEEDS-HUMAN - [x] E1"))
                self.assertIn("me x / 2026-10-10", _line(text, "By / date"))
        d = _bundle()
        plain = "tabs\tand  double  spaces \\W kept"
        signoff.record(d / "SUMMARY.md", action="iterate-do", by="T", date="2026-10-10",
                       delta=plain)
        self.assertTrue(_line((d / "SUMMARY.md").read_text(encoding="utf-8"),
                              "Iteration delta (if iterating)").endswith(" " + plain))


if __name__ == "__main__":
    unittest.main()
