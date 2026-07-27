"""The Remote Control seam is documented on the interactive leaves (issue #337).

The four `interactive = true` leaves hand the terminal to a REPL and block there, so a
rendered instance inherits a constraint nothing tells it about: the human must be at the
terminal the flow runs in, for the whole batch. Claude Code's `--remote-control` removes
it, and enabling it in one's OWN shell does not reach the leaves — each is a separate
subprocess whose argv comes from `pdca.toml`. That gap between "the feature exists" and
"it reaches the leaves" is exactly what makes it worth documenting.

No engine change: the value is entirely in the template making the seam visible.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[1]
# `pdca.toml.jinja` in the template checkout, `pdca.toml` in a rendered instance — this
# suite runs in both (tests/test_render_and_run drives the generated project's own tests).
TOML = next(TEMPLATE / n for n in ("pdca.toml.jinja", "pdca.toml")
            if (TEMPLATE / n).is_file())
RENDERED = TOML.name == "pdca.toml"


class RemoteControlDocs(unittest.TestCase):
    def setUp(self) -> None:
        self.text = TOML.read_text(encoding="utf-8")

    def test_the_seam_is_documented_beside_the_interactive_leaves(self) -> None:
        self.assertIn("--remote-control", self.text)
        planner = self.text.index("[leaves.planner]")
        note = self.text.index("--remote-control")
        self.assertLess(note, planner,
                        "the guidance must sit with the interactive leaves, not elsewhere")

    def test_it_says_the_flag_must_be_appended_not_re_declared(self) -> None:
        """A second `argv = [...]` line becomes a DUPLICATE KEY the moment a user
        uncomments it, and duplicate keys are a TOML parse error — every `pdca` command
        would then die at config load."""
        self.assertIn("APPEND", self.text)
        self.assertIn("do not add a second", self.text.lower())

    @unittest.skipUnless(RENDERED, "counts are only meaningful after Jinja branches resolve")
    def test_no_leaf_block_declares_argv_twice(self) -> None:
        """The failure the guidance warns about must not already be in the shipped file.

        Only meaningful on the RENDERED config: the template writes one `argv` line per
        `{% if interactive_family %}` branch and exactly one survives, so counting the
        source would flag every leaf. Commented lines never count — the commented example
        is the whole point.
        """
        blocks = re.split(r"^\[leaves\.", self.text, flags=re.M)[1:]
        for block in blocks:
            name = block.split("]", 1)[0]
            active = [ln for ln in block.split("\n[")[0].splitlines()
                      if re.match(r"\s*argv\s*=", ln)]
            with self.subTest(leaf=name):
                self.assertLessEqual(len(active), 1,
                                     f"[leaves.{name}] declares argv {len(active)} times")

    def test_it_is_scoped_to_interactive_claude_leaves(self) -> None:
        """The flag starts an INTERACTIVE session, so the headless builder/reviewer must
        not carry it — they have no human to reach."""
        self.assertIn("headless builder/reviewer must NOT carry it", self.text)
        self.assertIn("CLAUDE-ONLY", self.text)

    def test_it_stays_off_by_default(self) -> None:
        """Enrolment is required, and a flag that made a leaf refuse to start would block
        the flow on the one path that cannot be retried unattended."""
        for line in self.text.splitlines():
            if "--remote-control" in line:
                self.assertTrue(line.lstrip().startswith("#"),
                                f"--remote-control is active, not commented: {line!r}")


if __name__ == "__main__":
    unittest.main()
