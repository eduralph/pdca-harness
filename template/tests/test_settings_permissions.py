"""Every file-path permission rule we ship is written in a form the checker matches.

Claude Code resolves file-permission rules through `Read(path)` / `Edit(path)` only --
`Edit` rules cover all file-editing tools, `Write` included. A rule spelled
`Write(<path>)` is therefore never consulted: it denies nothing, allows nothing, and the
runtime prints a red validation warning at the end of EVERY session that loads it:

    Permission deny rule (.claude/settings.json): Write(.env) is not matched by file
    permission checks - only Edit(path) rules are. Use Edit(.env) instead (Edit rules
    cover all file-editing tools).

The cost is not just noise. A reader of the settings file sees a protection that does not
exist, so the invariant is quantified over the whole category: no file-path rule addressed
to a file-editing tool other than `Edit` may appear in any `allow` / `ask` / `deny` list of
any `.claude/settings.json` this repo ships -- not merely the two `.env` rows that
introduced it.

The suite runs in two postures (template checkout and rendered instance), so the settings
file is resolved the way `test_remote_control_docs.py:19-24` resolves `pdca.toml`.
`.claude/settings.json` is a plain JSON file, not a `.jinja` template, and `copier.yml`'s
`_exclude` never conditions it, so the same relative path is present in both.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[1]
# `pdca.toml.jinja` in the template checkout, `pdca.toml` in a rendered instance -- this
# suite runs in both (tests/test_render_and_run drives the generated project's own tests).
TOML = next(TEMPLATE / n for n in ("pdca.toml.jinja", "pdca.toml")
            if (TEMPLATE / n).is_file())
RENDERED = TOML.name == "pdca.toml"

SETTINGS = TEMPLATE / ".claude" / "settings.json"
# The template checkout also carries the repo's OWN settings one level up; a rendered
# instance has no such parent, hence the same guard shape as test_remote_control_docs:45.
REPO_SETTINGS = TEMPLATE.parent / ".claude" / "settings.json"

RULE_LISTS = ("allow", "ask", "deny")
# Tools whose rules go through the file-permission checker. It only ever matches the
# first two, so a path rule addressed to any of the rest is dead on arrival.
MATCHED_FILE_TOOLS = frozenset({"Read", "Edit"})
UNMATCHED_FILE_TOOLS = frozenset({"Write", "MultiEdit", "NotebookEdit"})
# The .env protection this repo ships; it must survive untouched.
ENV_PROTECTION = ("Read(.env)", "Read(.env.*)", "Edit(.env)", "Edit(.env.*)")


def rules(path: Path) -> list[tuple[str, str]]:
    """Every (list-name, rule) pair in the file's permission block."""
    perms = json.loads(path.read_text(encoding="utf-8")).get("permissions", {})
    return [(name, rule) for name in RULE_LISTS for rule in perms.get(name, [])]


class SettingsPermissions(unittest.TestCase):
    def shipped(self) -> list[Path]:
        found = [SETTINGS]
        if not RENDERED:                       # template checkout: also the repo's own
            self.assertTrue(REPO_SETTINGS.is_file(),
                            f"{REPO_SETTINGS} is missing from the template checkout")
            found.append(REPO_SETTINGS)
        return found

    def test_the_settings_file_is_shipped_in_both_postures(self) -> None:
        """`_exclude` never conditions it, so absence is a defect, not a skip."""
        self.assertTrue(SETTINGS.is_file(), f"{SETTINGS} is missing")

    def test_no_file_rule_uses_a_tool_the_checker_cannot_match(self) -> None:
        for path in self.shipped():
            for list_name, rule in rules(path):
                tool = rule.split("(", 1)[0] if "(" in rule else rule
                with self.subTest(settings=path.name, list=list_name, rule=rule):
                    if "(" not in rule:
                        continue           # bare tool rule, e.g. "Write" -- not a path rule
                    self.assertNotIn(
                        tool, UNMATCHED_FILE_TOOLS,
                        f"{path}: {list_name} rule {rule!r} is never matched by the file "
                        f"permission checks -- only {sorted(MATCHED_FILE_TOOLS)}(path) "
                        f"rules are. Use Edit(...) (it covers all file-editing tools).")

    def test_the_env_protection_is_intact(self) -> None:
        """Dropping the dead rows must not weaken what is actually protected."""
        for path in self.shipped():
            deny = [rule for name, rule in rules(path) if name == "deny"]
            for rule in ENV_PROTECTION:
                with self.subTest(settings=path.name, rule=rule):
                    self.assertIn(rule, deny, f"{path}: lost the .env guard {rule!r}")


if __name__ == "__main__":
    unittest.main()
