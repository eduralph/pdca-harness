"""The Remote Control seam is documented on the interactive leaves (issue #337).

The four `interactive = true` leaves hand the terminal to a REPL and block there, so a
rendered instance inherits a constraint nothing tells it about: the human must be at the
terminal the flow runs in, for the whole batch. Claude Code's `--remote-control` removes
it, and enabling it in one's OWN shell does not reach the leaves — each is a separate
subprocess whose argv comes from `pdca.toml`. That gap between "the feature exists" and
"it reaches the leaves" is exactly what makes it worth documenting.

No engine change: the value is entirely in the template making the seam visible.

This suite ships INTO rendered instances, so it may assert only what holds in every
posture the template sanctions (issue #386). "The flag is commented out" is the
template's *default*, not an invariant: an instance that enables the seam — which is the
whole point of documenting it — is a sanctioned posture. What holds everywhere is the
*protection*: the flag may ride only an `interactive = true` leaf.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[1]
# `pdca.toml.jinja` in the template checkout, `pdca.toml` in a rendered instance — this
# suite runs in both (tests/test_render_and_run drives the generated project's own tests).
TOML = next(TEMPLATE / n for n in ("pdca.toml.jinja", "pdca.toml")
            if (TEMPLATE / n).is_file())
RENDERED = TOML.name == "pdca.toml"


def _sections(text: str) -> list[tuple[str, str]]:
    """(header, body) for every `[...]` section of a TOML-shaped text, in file order.

    The leading tuple is the pre-header preamble, headed by "". Same line-anchored
    split the duplicate-argv check uses, widened from `[leaves.` to every table so an
    active flag parked outside a leaf block cannot slip past.
    """
    chunks = re.split(r"^\[", text, flags=re.M)
    out = [("", chunks[0])]
    for chunk in chunks[1:]:
        head, _, body = chunk.partition("]")
        out.append((head, body))
    return out


def _example_argvs(text: str) -> list[list[str]]:
    """Every COMMENTED `argv = [...]` example in the doc text, as quoted-token lists.

    An example may wrap across comment lines (the RC one does), so the comment lines
    are joined — leading `#` stripped — before each bracket pair is read. Active
    (uncommented) argv lines are deliberately not examples: an instance's own argv is
    protected by the driver's seed separator, while the example is what gets copied."""
    commented = "\n".join(ln.lstrip()[1:] for ln in text.splitlines()
                          if ln.lstrip().startswith("#"))
    out = []
    for m in re.finditer(r"argv\s*=\s*\[(.*?)\]", commented, flags=re.S):
        tokens = re.findall(r'"([^"]*)"', m.group(1))
        if tokens:
            out.append(tokens)
    return out


def _rc_comment_blocks(text: str) -> list[str]:
    """Each maximal run of consecutive `#` lines that mentions `--remote-control`.

    The placement-rationale assertion is anchored to THESE blocks — not the whole
    file, where common words would match vacuously — and matches ingredient words
    case-insensitively rather than one exact phrase, so rewording the comment
    (or another change landing in the same file) cannot turn the suite red while
    the rule itself still stands."""
    blocks: list[str] = []
    cur: list[str] = []
    for ln in text.splitlines():
        if ln.lstrip().startswith("#"):
            cur.append(ln)
        else:
            if cur:
                blocks.append("\n".join(cur))
            cur = []
    if cur:
        blocks.append("\n".join(cur))
    return [b for b in blocks if "--remote-control" in b]


def remote_control_offenders(text: str) -> list[str]:
    """Active (uncommented) `--remote-control` lines that are NOT in an interactive leaf.

    This is the posture-INDEPENDENT property (issue #386): the flag starts an
    *interactive* Claude session, so it may only ride a leaf that has a human at the
    other end. A commented line never counts — the template ships the flag as a
    commented example, an enabled instance uncomments it on its interactive leaves, and
    both are legitimate. A headless leaf carrying it is not: nothing answers the session
    and the flow hangs.
    """
    offenders: list[str] = []
    for name, body in _sections(text):
        lines = body.splitlines()
        interactive = any(re.match(r"\s*interactive\s*=\s*true\b", ln) for ln in lines)
        if name.startswith("leaves.") and interactive:
            continue
        offenders += [f"[{name}] {ln.strip()}" if name else ln.strip()
                      for ln in lines
                      if "--remote-control" in ln and not ln.lstrip().startswith("#")]
    return offenders


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

    def test_the_example_never_shows_the_flag_last(self) -> None:
        """The driver seeds an interactive leaf by APPENDING the prompt as one
        positional after the configured argv, and `--remote-control` takes an
        optional [name] value — as the argv-final token it swallows the whole seed
        as the RC session name: RC fails to start and the REPL opens unseeded
        (issue #396). The shipped example is what instances copy verbatim, so it
        must show the flag NON-last, and the doc must say why."""
        rc_examples = [ex for ex in _example_argvs(self.text)
                       if "--remote-control" in ex]
        self.assertTrue(rc_examples, "the doc block lost its worked example")
        for ex in rc_examples:
            with self.subTest(example=ex):
                self.assertNotEqual(
                    ex[-1], "--remote-control",
                    "the example ends in an optional-value flag — following it "
                    "verbatim makes the flag eat the seed prompt (issue #396)")
        stated = "\n".join(_rc_comment_blocks(self.text))
        self.assertTrue(stated, "the flag's doc comment block is gone")
        for needle in ("last", "seed", "optional"):
            self.assertRegex(
                stated, re.compile(needle, re.I),
                "the placement rule must be STATED, not just modelled: the doc "
                "comment must say the seed is appended after the argv and that "
                f"an optional-value flag must not sit last (missing {needle!r})")

    def test_it_is_scoped_to_interactive_claude_leaves(self) -> None:
        """The flag starts an INTERACTIVE session, so the headless builder/reviewer must
        not carry it — they have no human to reach."""
        self.assertIn("headless builder/reviewer must NOT carry it", self.text)
        self.assertIn("CLAUDE-ONLY", self.text)

    def test_the_flag_rides_only_an_interactive_leaf(self) -> None:
        """Binds BOTH postures — the property the docs promise, not the default.

        Unrendered template: every occurrence is a commented example, so there is
        nothing to flag. Rendered instance: an uncommented flag on planner / signoff /
        publisher / act is the sanctioned enrolled posture and passes; one on the
        headless builder / reviewer / any advisory leaf starts an interactive session
        with no human to reach and hangs the flow, so it fails here (issue #386).
        """
        self.assertEqual(remote_control_offenders(self.text), [],
                         "--remote-control starts an INTERACTIVE session: a headless "
                         "leaf carrying it hangs the flow with nobody to answer it")

    @unittest.skipIf(RENDERED, "off-by-default is the TEMPLATE's default, not an "
                               "invariant: an instance MAY enrol its interactive "
                               "leaves (issue #386) — the protection above binds both")
    def test_it_stays_off_by_default(self) -> None:
        """Binds the UNRENDERED template only.

        Enrolment is required, and a flag that made a leaf refuse to start would block
        the flow on the one path that cannot be retried unattended — so the template
        ships the example commented. A rendered instance that uncommented it on its
        interactive leaves did exactly what this file documents, and must not inherit a
        permanently red test for it.
        """
        for line in self.text.splitlines():
            if "--remote-control" in line:
                self.assertTrue(line.lstrip().startswith("#"),
                                f"--remote-control is active, not commented: {line!r}")


# --- posture regressions (issue #386) ------------------------------------------------
# The suite above can only see the posture of the checkout it runs in. These cases build
# the OTHER postures as synthetic configs in a temp dir, so both legs — an enabled
# instance is green, a headless leaf carrying the flag is red — are falsifiable in one
# run of `cd template && PYTHONPATH=src python3 -m unittest tests.test_remote_control_docs`.

_CHILD = "PDCA_REMOTE_CONTROL_POSTURE_CHILD"

_DOC_BLOCK = """\
# REMOTE CONTROL — answer an interactive leaf from another device (issue #337).
#
# To enable, APPEND the flag to the argv line below — do not add a second `argv = [...]`,
# which would be a duplicate key the moment you uncomment it. Put it anywhere but LAST —
# the driver appends the seed prompt as a positional after this argv, and a trailing
# optional-value flag would swallow it (issue #396):
#
#   argv = ["claude", "--remote-control", "--agent", "planner",
#           "--permission-mode", "acceptEdits"]
#
# CLAUDE-ONLY, and interactive-only. The headless builder/reviewer must NOT carry it: it
# starts an *interactive* session and they have no human to reach.
"""


def _rendered_config(enabled_leaf: str) -> str:
    """A rendered `pdca.toml` shaped like this template's output, with `--remote-control`
    UNCOMMENTED on `enabled_leaf`'s argv — the posture under test."""
    def leaf(name: str, interactive: bool) -> str:
        argv = ["claude"] + ([] if interactive else ["-p"]) + [
            "--agent", name, "--permission-mode", "acceptEdits"]
        if name == enabled_leaf:
            argv.append("--remote-control")
        quoted = ", ".join('"%s"' % a for a in argv)
        return (f"[leaves.{name}]\n"
                'mode = "command"\n'
                'family = "claude"\n'
                f"interactive = {'true' if interactive else 'false'}\n"
                f'agent = "{name}"\n'
                f"argv = [{quoted}]\n")

    headless = leaf("builder", False) + "\n" + leaf("reviewer", False) + "\n"
    seeded = "\n".join(leaf(n, True) for n in ("planner", "signoff", "publisher", "act"))
    return '[project]\nname = "demo"\n\n' + headless + _DOC_BLOCK + seeded


class RemoteControlPostures(unittest.TestCase):
    """Both rendered postures, built rather than assumed."""

    def test_an_enabled_interactive_leaf_is_not_an_offender(self) -> None:
        """Posture (b): the sanctioned enrolled instance. Red before issue #386."""
        for leaf in ("planner", "signoff", "publisher", "act"):
            with self.subTest(leaf=leaf):
                self.assertEqual(remote_control_offenders(_rendered_config(leaf)), [])

    def test_a_headless_leaf_carrying_the_flag_is_an_offender(self) -> None:
        """Posture (c), the protective half: unasserted before issue #386, so this is
        the leg that can go red and must stay able to."""
        for leaf in ("builder", "reviewer"):
            with self.subTest(leaf=leaf):
                offenders = remote_control_offenders(_rendered_config(leaf))
                self.assertEqual(len(offenders), 1, offenders)
                self.assertIn(f"leaves.{leaf}", offenders[0])

    def _run_this_module_against(self, config: str) -> subprocess.CompletedProcess:
        """Run this very module the way a rendered instance runs it: a temp checkout
        whose `pdca.toml` is `config`, with this file copied in as its `tests/` module."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "pdca.toml").write_text(config, encoding="utf-8")
        (root / "tests").mkdir()
        name = Path(__file__).name
        shutil.copy(Path(__file__).resolve(), root / "tests" / name)
        return subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", name],
            cwd=root, env={**os.environ, _CHILD: "1"},
            capture_output=True, text=True, timeout=300, check=False)

    @unittest.skipIf(os.environ.get(_CHILD), "child run of the posture harness")
    def test_the_whole_suite_passes_on_an_enrolled_instance(self) -> None:
        """Posture (b) end to end: an instance that enrolled its interactive leaves runs
        this shipped suite green — no local test delta to carry forever."""
        proc = self._run_this_module_against(_rendered_config("planner"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    @unittest.skipIf(os.environ.get(_CHILD), "child run of the posture harness")
    def test_the_whole_suite_fails_on_a_headless_leaf_carrying_the_flag(self) -> None:
        """Posture (c) end to end: the dangerous config the docs warn about must still
        break the suite, naming the leaf."""
        proc = self._run_this_module_against(_rendered_config("builder"))
        self.assertNotEqual(proc.returncode, 0,
                            "a headless leaf carrying --remote-control must fail the suite")
        self.assertIn("leaves.builder", proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
