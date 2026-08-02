"""Every headless leaf's role body states who owns its filesystem (issue #379).

The harness owns the working files of these leaves *mechanically*: the reviewer and the
advisory leaves run with their cwd set to a ``tempfile.TemporaryDirectory`` that is deleted
when the leaf exits (``src/pdca_harness/leaves.py:1831`` ``pdca-review-``, ``:2145``
``pdca-advisory-``, ``:2437`` ``pdca-plan-advisory-``), and the builder edits the per-cycle
worktree the harness creates and reclaims (``:1290`` ``worktree.ensure`` plus
``[driver].sweep_worktrees``), writing its artifacts into the bundle dir it is granted
(``:1310-1315``).

None of that was ever *said* in the role bodies, so a conscientious model invented an
answer — creating scratch outside its cwd and then feeling obliged to delete it. codex
``exec`` refuses ``rm``-style commands unconditionally in this mode, so the self-cleanup
step got the whole compound command rejected before any of it ran
(``rejected: rm -f style commands are not permitted``).

This asserts the *property* per body, not one exact sentence: (a) a writable-roots
statement phrased over the roots the **harness** gives (so a future harness-provided root
is covered without rewording), naming that leaf's roots, plus the prohibition on writing
outside them; and (b) a cleanup statement — the harness disposes of those roots, so the
leaf never runs one. In the template checkout the bodies must render for *every* instance,
so the statement must also sit outside any ``{% if %}`` block.

Unlike ``test_role_prompts.py`` (which compares the rendered ``.claude/`` wrappers and so
skips in the template checkout), this suite resolves the body path posture-aware and runs
non-vacuously in BOTH the template checkout and a rendered instance.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[1]
AGENTS = TEMPLATE / "agents"

# The headless leaves whose working files the harness creates AND disposes of, mapped to
# the roots each one is given. The sandboxed leaves share one shape (their cwd); the
# builder's differs — it must keep writing patch.diff / the test / build-notes.md into the
# bundle dir, so a blanket "never write outside your cwd" would be wrong for it.
SANDBOX_ROOTS = (r"\bcwd\b|\bworking director", )
AGENT_ROOTS = {
    "reviewer": SANDBOX_ROOTS,
    "adversary": SANDBOX_ROOTS,
    "code-review": SANDBOX_ROOTS,
    "plan-reviewer": SANDBOX_ROOTS,
    "builder": (r"\$PDCA_WORKTREE", r"\bbundle\b"),
}

# (a) the roots are the HARNESS's to give — the phrasing #422 can extend without a rewrite.
GRANTED = re.compile(r"(?i)\broots?\b[^.]{0,60}\bharness\b|\bharness\b[^.]{0,60}\bgives? you\b")
WRITABLE = re.compile(r"(?i)\bwrit(e|es|ing|able)\b")
OUTSIDE = re.compile(r"(?i)\b(never|not|no|don't)\b[^.]{0,140}\boutside\b")
# (b) cleanup belongs to the harness, so no rm-style command is ever warranted.
DISPOSES = re.compile(r"(?i)\bharness\b[^.]{0,140}\b(delete|dispos|remov|reclaim|clean)")
NOT_THE_LEAF_S = re.compile(
    r"(?i)\b(clean-?up|cleaning|reclaim\w*|disposal)\b[^.]{0,80}\b(never|not)\b[^.]{0,40}"
    r"\b(you|your|yours|the leaf)"
    r"|\b(never|no|not)\b[^.]{0,80}`?\brm\b")


def body_path(agent: str) -> Path:
    """``agents/<agent>.md.jinja`` in the template checkout, ``agents/<agent>.md`` in a
    rendered instance — the posture-aware pick mirrored from
    ``tests/test_remote_control_docs.py:22-24``, so this suite runs in both."""
    return next(AGENTS / n for n in (f"{agent}.md.jinja", f"{agent}.md")
                if (AGENTS / n).is_file())


def paragraphs(text: str) -> list[tuple[int, str]]:
    """(offset, whitespace-normalized paragraph) for each blank-line-separated block."""
    out, pos = [], 0
    for block in text.split("\n\n"):
        out.append((pos, " ".join(block.split())))
        pos += len(block) + 2
    return [(off, p) for off, p in out if p]


def inside_a_conditional(text: str, offset: int) -> bool:
    """True when ``offset`` falls inside a ``{% if %} … {% endif %}`` block: text there
    renders for only some instances, and this statement must reach every vendor."""
    head = text[:offset]
    return len(re.findall(r"{%-?\s*if\b", head)) > len(re.findall(r"{%-?\s*endif\b", head))


class LeafScratchDiscipline(unittest.TestCase):
    def test_every_harness_owned_leaf_states_its_writable_roots(self) -> None:
        for agent, roots in AGENT_ROOTS.items():
            with self.subTest(agent=agent):
                path = body_path(agent)
                text = path.read_text(encoding="utf-8")
                hits = [(off, p) for off, p in paragraphs(text)
                        if GRANTED.search(p) and WRITABLE.search(p)]
                self.assertTrue(
                    hits,
                    f"{path.name}: no statement of which roots the harness gives this leaf "
                    f"to write in — the leaf is left to invent its own filesystem")
                hits = [(off, p) for off, p in hits
                        if all(re.search(pattern, p) for pattern in roots)]
                self.assertTrue(
                    hits,
                    f"{path.name}: the writable-roots statement never names this leaf's own "
                    f"roots ({', '.join(roots)}), so it does not describe THIS leaf")
                hits = [(off, p) for off, p in hits if OUTSIDE.search(p)]
                self.assertTrue(
                    hits,
                    f"{path.name}: the roots are named but nothing forbids creating files "
                    f"outside them")
                self.assertFalse(
                    all(inside_a_conditional(text, off) for off, _ in hits),
                    f"{path.name}: the ownership statement sits inside a {{% if %}} block, "
                    f"so it renders for only some instances")

    def test_every_harness_owned_leaf_states_cleanup_is_not_its_job(self) -> None:
        for agent in AGENT_ROOTS:
            with self.subTest(agent=agent):
                path = body_path(agent)
                text = path.read_text(encoding="utf-8")
                hits = [(off, p) for off, p in paragraphs(text)
                        if DISPOSES.search(p) and NOT_THE_LEAF_S.search(p)]
                self.assertTrue(
                    hits,
                    f"{path.name}: nothing says the harness disposes of these roots and the "
                    f"leaf therefore never runs a cleanup — so an invented rm-style command "
                    f"stays plausible (and codex `exec` rejects the command it rides on)")
                self.assertFalse(
                    all(inside_a_conditional(text, off) for off, _ in hits),
                    f"{path.name}: the cleanup statement sits inside a {{% if %}} block, "
                    f"so it renders for only some instances")


if __name__ == "__main__":
    unittest.main()
