"""An oversized interactive seed spills to a file instead of killing the driver (#313).

An interactive leaf seeds its REPL by passing the whole prompt as a single positional —
`claude "<prompt>"`. The OS bounds one argv string (Linux MAX_ARG_STRLEN ~128 KiB; this is
NOT total ARG_MAX), so past that the child never execs and the driver dies before the beat
runs:

    OSError: [Errno 7] Argument list too long: 'claude'

The Act leaf trips it first — its prompt embeds the whole cross-cycle ACT INDEX, which
grows with every frozen cycle (151,653 bytes observed on a mature instance) — so `pdca
flow` died the moment it auto-ran Act. Headless leaves are unaffected: their prompt rides
stdin, which has no argv limit.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pdca_harness import families, leaves
from pdca_harness.config import LeafConfig

REPO = Path(__file__).resolve().parents[1]

# A child that records the seed it was handed, and — while still running — the bytes of any
# file that seed points at. Proving the pointer RESOLVES is the whole point: asserting only
# that the process started would pass against a seed the REPL cannot act on.
#   argv = [python, capture.py, <out>]  and `_invoke` APPENDS the seed, so the seed is
#   argv[2] — the ordering is itself part of what this exercises.
_CAPTURE = r"""
import json, pathlib, sys
out, seed = sys.argv[1], sys.argv[2]
rec = {"seed": seed, "resolved": None}
for token in seed.replace("`", " ").split():
    p = pathlib.Path(token)
    if p.name.startswith(".pdca-prompt-") and p.exists():
        rec["resolved"] = p.read_text(encoding="utf-8")
        break
pathlib.Path(out).write_text(json.dumps(rec), encoding="utf-8")
"""


class SeedSpill(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_interactive(self, prompt: str) -> dict:
        """Drive the real `_invoke` interactive path with a child that records its seed."""
        script = self.tmp / "capture.py"
        script.write_text(_CAPTURE, encoding="utf-8")
        out = self.tmp / "seen.json"
        leaf = LeafConfig(mode="command", family="generic", interactive=True,
                          argv=[sys.executable, str(script), str(out)])
        leaves._invoke(leaf, self.tmp, prompt)
        import json
        return json.loads(out.read_text(encoding="utf-8"))

    # -- the crash itself -----------------------------------------------------------

    def test_oversized_seed_does_not_raise(self) -> None:
        """The reproduction from the issue: 200 KB used to die with E2BIG."""
        leaf = LeafConfig(mode="command", family="generic", interactive=True, argv=["true"])
        leaves._invoke(leaf, self.tmp, "X" * 200_000)  # must not raise

    # -- what the child actually receives -------------------------------------------

    def test_small_seed_is_passed_inline_byte_for_byte(self) -> None:
        prompt = "seed me — with a ünicode ✓ tail"
        rec = self._run_interactive(prompt)
        self.assertEqual(rec["seed"], prompt)
        self.assertIsNone(rec["resolved"])

    def test_oversized_seed_is_readable_in_full_from_the_pointer(self) -> None:
        """The seed must point at a file the child can open, containing the WHOLE prompt."""
        prompt = "ACT INDEX\n" + ("payload line\n" * 20_000)
        self.assertGreater(len(prompt.encode()), leaves._SEED_ARG_BUDGET)
        rec = self._run_interactive(prompt)
        self.assertLess(len(rec["seed"].encode()), 1024, "pointer seed should be short")
        self.assertEqual(rec["resolved"], prompt,
                         "the spilled file did not contain the prompt byte-for-byte")

    def test_role_prefix_is_inside_the_spilled_file(self) -> None:
        """`_invoke` prepends the role body BEFORE this branch, so the threshold and the
        spilled bytes must both cover the prefixed prompt, not the caller's argument."""
        prompt = "TASK\n" + ("x" * 200_000)
        rec = self._run_interactive(prompt)
        self.assertTrue(rec["resolved"].endswith("x" * 100))
        self.assertIn("TASK", rec["resolved"])

    def test_threshold_is_measured_in_bytes_not_characters(self) -> None:
        """A prompt just under the budget in CHARACTERS but over it in BYTES must spill —
        otherwise a mostly-non-ASCII prompt passes the check and still fails to exec."""
        multibyte = "é" * (leaves._SEED_ARG_BUDGET // 2 + 10)   # 2 bytes each
        self.assertLess(len(multibyte), leaves._SEED_ARG_BUDGET)
        self.assertGreater(len(multibyte.encode()), leaves._SEED_ARG_BUDGET)
        seed, spill = leaves._seed_positional(multibyte, self.tmp)
        self.assertIsNotNone(spill, "byte length ignored — a non-ASCII prompt would E2BIG")
        spill.unlink()

    # -- cleanup --------------------------------------------------------------------

    def test_spill_is_removed_after_a_normal_session(self) -> None:
        self._run_interactive("A" * 200_000)
        self.assertEqual(list(self.tmp.glob(".pdca-prompt-*")), [])

    def test_spill_is_removed_after_a_nonzero_exit(self) -> None:
        leaf = LeafConfig(mode="command", family="generic", interactive=True,
                          argv=[sys.executable, "-c", "raise SystemExit(3)"])
        leaves._invoke(leaf, self.tmp, "A" * 200_000)
        self.assertEqual(list(self.tmp.glob(".pdca-prompt-*")), [])

    def test_spill_is_removed_when_the_child_cannot_be_spawned(self) -> None:
        """`finally`, not a happy-path unlink: a missing binary raises before `run` returns."""
        leaf = LeafConfig(mode="command", family="generic", interactive=True,
                          argv=[str(self.tmp / "definitely-not-a-binary")])
        with self.assertRaises(OSError):
            leaves._invoke(leaf, self.tmp, "A" * 200_000)
        self.assertEqual(list(self.tmp.glob(".pdca-prompt-*")), [])

    # -- where it lands, and that the tree ignores it --------------------------------

    def test_spill_lands_inside_the_workdir(self) -> None:
        """It must be in the REPL's cwd, or the model needs an out-of-tree read grant."""
        seed, spill = leaves._seed_positional("A" * 200_000, self.tmp)
        self.assertEqual(spill.parent, self.tmp)
        self.assertIn(spill.name, seed)
        spill.unlink()

    def test_gitignore_covers_the_spill_name(self) -> None:
        """Every interactive leaf runs with the PROJECT ROOT as workdir (`cfg.root` at all
        seven call sites), so the spill lands in the instance's versioned tree — and the
        Act seed that triggers it is ~150 KB. Without the ignore rule every instance shows
        an untracked file mid-session, and a killed session orphans one.
        """
        # `.gitignore` in a RENDERED instance, `.gitignore.jinja` in the template checkout.
        # Both are checked because the rendered one is what actually protects an instance's
        # tree, and this suite runs in both places (tests/test_render_and_run drives the
        # generated project's own tests).
        ignore = next((REPO / n for n in (".gitignore", ".gitignore.jinja")
                       if (REPO / n).is_file()), None)
        self.assertIsNotNone(ignore, "no .gitignore to check the spill pattern against")
        patterns = ignore.read_text(encoding="utf-8").splitlines()
        _seed, spill = leaves._seed_positional("A" * 200_000, self.tmp)
        try:
            self.assertTrue(
                any(Path(spill.name).match(p.strip()) for p in patterns
                    if p.strip() and not p.startswith("#")),
                f"{spill.name} matches no .gitignore.jinja pattern — instances will see it")
        finally:
            spill.unlink()

    def test_budget_is_under_the_platform_limit(self) -> None:
        """Windows caps the whole command line at 32,767 chars, far below a POSIX budget."""
        self.assertLessEqual(leaves._SEED_ARG_BUDGET,
                             24 * 1024 if os.name == "nt" else 96 * 1024)

    # -- headless is untouched --------------------------------------------------------

    def test_headless_leaf_still_feeds_the_prompt_on_stdin(self) -> None:
        """Headless prompts ride stdin and have no argv limit — they must not spill."""
        echo = self.tmp / "echo.py"
        echo.write_text("import sys,pathlib;"
                        "pathlib.Path(sys.argv[1]).write_text(sys.stdin.read())\n",
                        encoding="utf-8")
        out = self.tmp / "stdin.txt"
        leaf = LeafConfig(mode="command", family="generic", interactive=False,
                          argv=[sys.executable, str(echo), str(out)])
        leaves._invoke(leaf, self.tmp, "B" * 200_000)
        self.assertEqual(out.read_text(encoding="utf-8"), "B" * 200_000)
        self.assertEqual(list(self.tmp.glob(".pdca-prompt-*")), [])


# --- the seed survives a trailing optional-value flag (issue #396) ---------------------
# The seed above is APPENDED to whatever argv the instance configured — and the template
# sanctions argvs that END in a flag (`--remote-control`, whose optional [name] value made
# the flag eat the entire seed as an RC session name: RC failed to start and the REPL
# opened unseeded). The fix is a family-declared end-of-options separator between argv and
# seed (`claude -- "<seed>"`, POSIX guideline 10), so the guarantee is argv-independent.


class SeedSeparator(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _spawn_argv(self, family: str, argv: list[str], prompt: str = "the seed") -> list[str]:
        """The argv `_invoke` actually spawns for an interactive leaf, recorded not run.

        Patches the one spawn site (`leaves.subprocess.run`) — the production `_invoke`
        path up to it (profile resolution, seed handling, argv assembly) runs for real."""
        seen: list[list[str]] = []

        def record(cmd, **kwargs):
            seen.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0)

        with mock.patch.object(leaves.subprocess, "run", record):
            leaf = LeafConfig(mode="command", family=family, interactive=True, argv=argv)
            leaves._invoke(leaf, self.tmp, prompt)
        self.assertEqual(len(seen), 1, "expected exactly one interactive spawn")
        return seen[0]

    def test_claude_family_separates_seed_from_argv(self) -> None:
        """The exact defect argv: the doc-sanctioned RC flag as the LAST token. The
        spawn must carry `--` between the configured argv and the seed, so the flag
        has nothing to swallow."""
        argv = ["claude", "--agent", "planner", "--permission-mode", "acceptEdits",
                "--remote-control"]
        spawned = self._spawn_argv("claude", argv, "plan issue 19")
        self.assertEqual(spawned, argv + ["--", "plan issue 19"],
                         "no end-of-options separator: a trailing optional-value flag "
                         "eats the seed as its value (issue #396)")

    def test_family_without_the_bit_keeps_the_bare_positional(self) -> None:
        """No verified separator ⇒ the spawn is byte-identical to before: argv + [seed]."""
        argv = ["some-cli", "--flag"]
        spawned = self._spawn_argv("generic", argv)
        self.assertEqual(spawned, argv + ["the seed"])

    def test_the_separator_is_a_families_profile_bit(self) -> None:
        """Declared as DATA on the profile — claude (verified `claude -- "<prompt>"` on
        2.1.222) carries `--`; the others stay unset until their CLI is verified."""
        self.assertEqual(families.resolve("claude").seed_separator, "--")
        for fam in ("codex", "gemini", "generic"):
            with self.subTest(family=fam):
                self.assertEqual(families.resolve(fam).seed_separator, "")


if __name__ == "__main__":
    unittest.main()
