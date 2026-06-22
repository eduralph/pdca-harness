"""Unit tests for `brief.parse_fields` — the brief-field parser (stdlib unittest).

Regression for the `**Label:**` leak: the brief template and every real brief write
the colon INSIDE the bold (`- **Label:** value`), so the parser must not let the
closing `**` leak into the parsed value. No fixtures, no git, no network.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pdca_harness import brief


class ParseFields(unittest.TestCase):
    def _parse(self, body: str) -> dict[str, str]:
        f = Path(tempfile.mkdtemp()) / "brief.md"
        f.write_text(body, encoding="utf-8")
        return brief.parse_fields(f)

    def test_colon_inside_bold_does_not_leak(self) -> None:
        fields = self._parse(
            "- **Repo + branch target:** org/repo @ main\n"   # colon inside bold
            "- **Slug**: my-fix\n"                              # colon outside bold
            "- Kind: enhancement\n"                             # no bold
            "- **Defect:** value with **bold** inside\n"        # value keeps inner bold
        )
        self.assertEqual(fields["repo + branch target"], "org/repo @ main")
        self.assertEqual(fields["slug"], "my-fix")
        self.assertEqual(fields["kind"], "enhancement")
        self.assertEqual(fields["defect"], "value with **bold** inside")
        # no leaked markdown markers on the key or the front of the value
        for key, val in fields.items():
            self.assertFalse(key.startswith("*") or key.endswith("*"), key)
            self.assertFalse(val.startswith("*"), val)


class OrderingFields(unittest.TestCase):
    """The optional Depends on / Conflicts with fields (docs 09, issue #36)."""

    def _brief(self, body: str) -> Path:
        f = Path(tempfile.mkdtemp()) / "brief.md"
        f.write_text(body, encoding="utf-8")
        return f

    def test_depends_on_parses_comma_and_space_separated_ids(self) -> None:
        f = self._brief("- **Depends on:** #36, 11 issue_42\n")
        # leading '#' and the issue_ prefix are both normalised to bare ids
        self.assertEqual(brief.depends_on(f), ["36", "11", "42"])

    def test_conflicts_with_parses_list(self) -> None:
        f = self._brief("- **Conflicts with:** C1, T1\n")
        self.assertEqual(brief.conflicts_with(f), ["C1", "T1"])

    def test_absent_field_is_empty_list(self) -> None:
        f = self._brief("- **Slug:** no-ordering\n")
        self.assertEqual(brief.depends_on(f), [])
        self.assertEqual(brief.conflicts_with(f), [])

    def test_empty_value_is_empty_list(self) -> None:
        f = self._brief("- **Depends on:**\n- **Conflicts with:** \n")
        self.assertEqual(brief.depends_on(f), [])
        self.assertEqual(brief.conflicts_with(f), [])

    def test_trailing_parenthetical_rationale_is_ignored(self) -> None:
        # The crash in #103: the planner mimics the template's `value (explanation)`
        # hint, so the field carries a note after the id. Only the leading id parses.
        f = self._brief(
            "- **Depends on:** 139   (no data dependency, but PR-order is kept so this "
            "waits on #139)\n")
        self.assertEqual(brief.depends_on(f), ["139"])

    def test_trailing_em_dash_rationale_is_ignored(self) -> None:
        f = self._brief("- **Depends on:** 12, 13 — kept in PR order\n")
        self.assertEqual(brief.depends_on(f), ["12", "13"])

    def test_em_dash_only_value_means_none(self) -> None:
        # "—" is the conventional "none"; it must not parse to a bogus ['—'] id.
        f = self._brief("- **Conflicts with:** —\n")
        self.assertEqual(brief.conflicts_with(f), [])

    def test_non_numeric_ids_survive_a_trailing_rationale(self) -> None:
        # Ids needn't be numeric (the driver keys bundles by arbitrary id); a tracker-key
        # id is kept, while the lowercase rationale that follows is dropped.
        f = self._brief("- **Depends on:** AA, PROJ-12 — keep PR order\n")
        self.assertEqual(brief.depends_on(f), ["AA", "PROJ-12"])

    def test_depends_on_merged_is_its_own_field(self) -> None:
        # The merge-gated field (#107) parses independently of plain Depends on.
        f = self._brief("- **Depends on:** 7\n- **Depends on (merged):** 8, 9\n")
        self.assertEqual(brief.depends_on(f), ["7"])
        self.assertEqual(brief.depends_on_merged(f), ["8", "9"])

    def test_depends_on_merged_absent_is_empty(self) -> None:
        f = self._brief("- **Depends on:** 7\n")
        self.assertEqual(brief.depends_on_merged(f), [])


if __name__ == "__main__":
    unittest.main()
