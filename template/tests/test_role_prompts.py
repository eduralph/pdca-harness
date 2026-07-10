"""Guard: each Claude agent wrapper stays in sync with its canonical role prompt (#274).

A leaf's role prompt has one canonical, vendor-neutral source at ``agents/<name>.md``. A
Claude leaf additionally needs ``.claude/agents/<name>.md`` — frontmatter plus the same
body — because ``claude --agent <name>`` resolves that path. Upstream they are
single-sourced (the wrapper is ``frontmatter + {% include "template/agents/<name>.md" %}``),
but a RENDERED instance holds two independent files and nothing keeps them in sync: a
``claude`` leaf (``role_injection == "flag"``) reads the wrapper while a ``codex`` leaf
(``inline``) reads the canonical file, so hand-editing one silently gives the two vendors
different instructions.

This asserts ``strip_frontmatter(wrapper) == canonical`` for every wrapper that rendered.

It runs in a RENDERED instance (and in CI via ``test_render_and_run``, which renders then
runs this suite). In the template checkout the agent files are still ``.md.jinja``, so
there is nothing to compare — the test skips cleanly rather than passing vacuously.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from pdca_harness import families

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "agents"
CLAUDE_AGENTS = ROOT / ".claude" / "agents"


class RolePromptSync(unittest.TestCase):
    def test_each_claude_wrapper_matches_its_canonical_body(self) -> None:
        # Iterate the wrappers that actually rendered — copier omits the
        # planner/signoff/act/publisher wrappers for a non-claude interactive family, so
        # `.claude/agents/` legitimately holds a subset. builder/reviewer/adversary/
        # code-review always render, so a real render always checks at least those four.
        wrappers = sorted(CLAUDE_AGENTS.glob("*.md")) if CLAUDE_AGENTS.is_dir() else []
        if not wrappers:
            self.skipTest("role prompts unrendered (.md.jinja) — this guard runs in a "
                          "rendered instance / render-check, not the template checkout")

        for wrapper in wrappers:
            canonical = AGENTS / wrapper.name
            with self.subTest(agent=wrapper.stem):
                self.assertTrue(
                    canonical.is_file(),
                    f".claude/agents/{wrapper.name} has no canonical agents/{wrapper.name}")
                body = families.strip_frontmatter(wrapper.read_text(encoding="utf-8")).strip()
                self.assertEqual(
                    body, canonical.read_text(encoding="utf-8").strip(),
                    f"agents/{wrapper.name} and .claude/agents/{wrapper.name} have drifted — "
                    f"edit the canonical agents/{wrapper.name} (the wrapper is generated from "
                    f"it) so both vendors read the same role prompt")


if __name__ == "__main__":
    unittest.main()
