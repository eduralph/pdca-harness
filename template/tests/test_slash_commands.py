"""No shell expansion in a rendered slash command's `!` pre-execution block (issue #508).

Claude Code's shell-permission checker will not match ANY allow-rule against a command
containing a shell expansion inside a `!` pre-execution block — it refuses to match, it
does not fail to find a match (`Error: Shell command permission check failed ... Contains
simple_expansion`). `/handoff`'s block carried `$CLAUDE_PROJECT_DIR` and `$1`, so every
rendered instance shipped a command whose own self-check could never run. The fix removes
shell expansions from `!` blocks; this test enumerates the WHOLE commands directory (not
just handoff.md.jinja) so a future command — `/abandon` (#404) among them — cannot ship
the same broken shape.

RED on a tree without the fix: `handoff.md.jinja`'s `!` block contains `$CLAUDE_PROJECT_DIR`
and `$1`, both shell expansions, so the assertion below fails on the base tree.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

TEMPLATE_ROOT = Path(__file__).resolve().parents[1]
COMMANDS_DIR = TEMPLATE_ROOT / ".claude" / "commands"

# A `!`-prefixed pre-execution block: `!` immediately followed by a backtick-quoted
# command, greedy to the LAST backtick on the line so a nested backtick inside the
# quoted command (itself a form the permission checker cannot match) stays part of the
# captured block rather than truncating the match early.
BANG_BLOCK = re.compile(r"(?m)^!`(.*)`\s*$")

# Any of: `$NAME`, `${...}`, `$1`/`$ARGUMENTS` (both are `\$\w+`), `$(...)`, or a nested
# backtick — the shapes the issue names as unmatchable by the permission checker.
SHELL_EXPANSION = re.compile(r"\$\w+|\$\{[^}]*\}|\$\([^)]*\)|`")


class NoShellExpansionInBangBlocks(unittest.TestCase):
    def test_no_command_ships_a_bang_block_the_permission_checker_cannot_match(self) -> None:
        self.assertTrue(COMMANDS_DIR.is_dir(), f"missing {COMMANDS_DIR}")
        files = sorted(p for p in COMMANDS_DIR.iterdir() if p.is_file())
        self.assertTrue(files, f"no command files found under {COMMANDS_DIR}")
        for path in files:
            text = path.read_text(encoding="utf-8")
            blocks = BANG_BLOCK.findall(text)
            for block in blocks:
                with self.subTest(command=path.name, block=block):
                    found = SHELL_EXPANSION.search(block)
                    msg = (
                        f"{path.name}: a `!` pre-execution block carries a shell "
                        f"expansion the permission checker cannot match "
                        f"({found.group(0)!r} in {block!r})" if found else None
                    )
                    self.assertIsNone(found, msg)


if __name__ == "__main__":
    unittest.main()
