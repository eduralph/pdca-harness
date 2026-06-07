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


if __name__ == "__main__":
    unittest.main()
