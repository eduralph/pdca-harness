"""Template-repo test: every workflow pins its actions and leaves no credentials behind.

Covers the repo's own workflows (.github/workflows/*.yml) and the one the template renders
into every instance (template/.github/workflows/*.jinja), issue #816:

- each `uses:` names a full commit hash with its release as a comment
  (`actions/checkout@<40-char sha> # v7.0.1`), never a tag or branch, which can be moved;
- each actions/checkout sets `persist-credentials: false`, so the workflow token isn't
  left in the checkout's git config for later steps;
- the template uses the same pin as the repo for each action it shares with it.
  Dependabot updates only .github/workflows/*.yml, so its PR fails here until the
  template follows.

Reads files only; no copier, no network.
Run with: python -m unittest tests.test_workflows
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
USES = re.compile(r"^(\s*)(?:#\s*)?(-\s*)?uses:\s*(\S+)(.*)$")
PINNED = re.compile(r"^([\w.-]+/[\w./-]+)@([0-9a-f]{40})$")


def workflows(root: Path) -> list[Path]:
    return sorted((root / ".github" / "workflows").glob("*.yml"))


def template_workflows(root: Path) -> list[Path]:
    return sorted((root / "template" / ".github" / "workflows").glob("*.jinja"))


def problems(path: Path) -> list[str]:
    """What is wrong with one workflow file, as `file:line: message`."""
    out = []
    lines = path.read_text(encoding="utf-8").splitlines()
    where = path.relative_to(REPO)
    for n, line in enumerate(lines, 1):
        m = USES.match(line)
        if not m or m[3].startswith("./"):
            continue
        if not PINNED.match(m[3]) or not re.match(r"\s*#\s*v\d", m[4]):
            out.append(f"{where}:{n}: pin {m[3]} to a commit hash, with the release as "
                       "a comment (`@<sha> # vX.Y.Z`)")
        if m[3].startswith("actions/checkout@") and not line.lstrip().startswith("#"):
            # The step's other keys sit two columns right of its `- `.
            indent = len(m[1]) + (len(m[2]) if m[2] else 0)
            block = []
            for later in lines[n:]:
                if later.strip() and (len(later) - len(later.lstrip()) < indent
                                      or later.lstrip().startswith("- ")):
                    break
                block.append(later.strip())
            if "persist-credentials: false" not in block:
                out.append(f"{where}:{n}: actions/checkout without "
                           "`persist-credentials: false`")
    return out


def pins(paths: list[Path]) -> dict[str, set[str]]:
    """Action -> the `<sha> # vX` pins it is used with across `paths`."""
    out: dict[str, set[str]] = {}
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            m = USES.match(line)
            if m and not line.lstrip().startswith("#"):
                p = PINNED.match(m[3])
                if p:
                    out.setdefault(p[1], set()).add(f"{p[2]}{m[4]}")
    return out


class Workflows(unittest.TestCase):
    def test_actions_pinned_and_credentials_not_persisted(self) -> None:
        files = workflows(REPO) + template_workflows(REPO)
        self.assertTrue(template_workflows(REPO), "no template workflow found")
        found = [p for path in files for p in problems(path)]
        self.assertEqual(found, [])

    def test_template_uses_the_repos_pins(self) -> None:
        own = pins(workflows(REPO))
        for action, used in pins(template_workflows(REPO)).items():
            with self.subTest(action):
                if action in own:
                    self.assertEqual(used, own[action],
                                     f"template/.github/workflows pins {action} differently "
                                     "from .github/workflows")


if __name__ == "__main__":
    unittest.main()
