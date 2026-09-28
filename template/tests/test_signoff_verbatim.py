"""§9 records the human's sign-off text verbatim — it is data, never a regex template
(issue #529).

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

Run from ``template/``: ``PYTHONPATH=src python3 -m unittest tests.test_signoff_verbatim``
"""

from __future__ import annotations

import tempfile
import unittest
from io import StringIO
from contextlib import redirect_stderr
from pathlib import Path

from pdca_harness import flow, leaves, signoff
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


if __name__ == "__main__":
    unittest.main()
