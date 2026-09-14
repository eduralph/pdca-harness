"""A stub-produced split proposal must never reach the tracker (issue #466).

With `[leaves.splitter] mode = "stub"` (the vertical-slice default), `_stub_split`
writes an OFFLINE PLACEHOLDER proposal that is byte-identical in shape to a real one —
same version marker, same delimiters. Nothing on disk recorded where it came from, and
`--accept` runs in a different process from `do_split`, where an in-memory flag could
not reach it. Live evidence: `getwyrd/wyrd#708` / `#709`, filed as real sub-issues
titled literally `stub-child-one` / `stub-child-two`.

Tracker issues cannot be withdrawn (`split.py` docstring, #358 / #459) — the same
irreversibility the whole `--accept` order is built around. So the fix is two-sided:
the stub marks its OWN output (`leaves._stub_split`), and the filing branch of
`--accept` refuses a marked proposal BEFORE `split.can_file` is consulted and before
any `gh issue create` (`cli._split`'s `if not ids:` branch). `--ids` must stay
untouched: that path files nothing, and the operator supplied the ids deliberately
(#358's offline round-trip).

Reuses `test_split.py`'s harness: a `Config` built from stdlib-only pieces, `subprocess`
/ `shutil` replaced on the `pdca_harness.split` module so no real `gh` is ever invoked.
"""

from __future__ import annotations

import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pdca_harness import cli, leaves, split, state
from pdca_harness.config import Config, LeafConfig

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


class StubProposalsNeverReachTheTracker(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process", templates_dir=TEMPLATES,
            default_branch="main", tracker_system="github",
            tracker_url="https://github.com/acme/widgets", issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
            splitter=LeafConfig(mode="stub"),
        )
        self.parent = self.cfg.bundle("500")
        self.parent.mkdir(parents=True)
        (self.parent / "brief.md").write_text("- **Slug:** parent\n", encoding="utf-8")
        self.calls: list[list[str]] = []

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _do_split(self) -> str:
        """Run the REAL stub splitter leaf; return what it printed to stderr."""
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = leaves.do_split(self.parent, self.cfg)
        self.assertEqual(rc, 0)
        return err.getvalue()

    def _args(self, ids: str = "") -> SimpleNamespace:
        return SimpleNamespace(issue_id="500", accept=True, ids=ids)

    def _gh(self):
        """A fake `gh` that records its argv — same shape as test_split.py's `_gh`."""
        def run(cmd, capture_output=False, text=False, cwd=None):
            self.calls.append(list(cmd))
            n = len(self.calls)
            return SimpleNamespace(
                returncode=0,
                stdout=f"https://github.com/acme/widgets/issues/{900 + n}\n", stderr="")
        return run

    def _patched(self, *, can_file_ok: bool = True):
        """Everything the filing path could reach, all recorded/faked — never real `gh`."""
        return (
            mock.patch.multiple(
                "pdca_harness.split",
                subprocess=SimpleNamespace(run=self._gh()),
                shutil=SimpleNamespace(which=lambda _n: "/usr/bin/gh",
                                       rmtree=shutil.rmtree, move=shutil.move)),
            mock.patch.object(split, "can_file",
                              return_value=(True, "acme/widgets") if can_file_ok
                              else (False, "unreachable")))

    # -- (d) the stub announces itself on stderr the moment it runs ----------------------

    def test_do_split_announces_the_stub_on_stderr_at_the_moment_it_runs(self) -> None:
        err = self._do_split()
        self.assertIn("stub", err.lower())
        self.assertIn(self.parent.name, err)

    def test_command_mode_prints_no_stub_notice(self) -> None:
        """The notice belongs to the branch that CHOSE the stub — a real splitter run
        must not carry a message that tells the operator the opposite of what happened."""
        self.cfg.splitter = LeafConfig(mode="command", family="claude", argv=["true"])
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err), \
             mock.patch.object(leaves, "_invoke",
                               lambda *a, **k: (self.parent / split.PROPOSAL).write_text(
                                   "<!-- pdca:split-proposal v1 -->\n"
                                   "<!-- pdca:child child-1 -->\n- **Slug:** s\n"
                                   "<!-- pdca:end child-1 -->\n", encoding="utf-8")):
            leaves.do_split(self.parent, self.cfg)
        self.assertNotIn("stub", err.getvalue().lower())

    # -- (a) the proposal is self-identifying on disk -------------------------------------

    def test_the_stub_proposal_is_self_identifying_on_disk(self) -> None:
        """Not a slug-name sniff, not an in-memory flag: read the proposal FRESH, exactly
        as `--accept` (a different process) would, and it must still be recognisable."""
        self._do_split()
        text = (self.parent / split.PROPOSAL).read_text(encoding="utf-8")
        self.assertTrue(split.is_stub_proposal(text),
                        "a stub-authored proposal is not marked as such on disk")

    # -- (b) + (e): the filing branch refuses BEFORE can_file / gh issue create ----------

    def test_accept_without_ids_refuses_before_filing_anything(self) -> None:
        self._do_split()
        patches = self._patched(can_file_ok=True)
        err = io.StringIO()
        with patches[0], patches[1], \
             redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = cli._split(self.cfg, self._args())

        self.assertNotEqual(rc, 0, "a stub-authored proposal was accepted for filing")
        self.assertEqual(self.calls, [],
                         "gh issue create was invoked against a stub-authored proposal")
        self.assertIn("stub", err.getvalue().lower())
        self.assertIn('mode = "command"', err.getvalue(),
                      "the refusal does not name the remedy")
        # The filing branch refused, not `accept` itself: no child bundle, and the
        # parent was never marked split.
        self.assertEqual(list(self.cfg.bundle_root.glob("issue_[0-9]*")), [self.parent],
                         "a child bundle was materialised despite the refusal")
        self.assertFalse((self.parent / state.CLOSE_MARKER).exists(),
                         "the parent was marked split despite the refusal")

    def test_the_refusal_does_not_depend_on_can_file_failing(self) -> None:
        """The failure mode worth locking: a refusal that filed the first child before
        erroring. `can_file` says the tracker IS reachable, and still nothing is filed."""
        self._do_split()
        patches = self._patched(can_file_ok=True)
        with patches[0], patches[1], \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = cli._split(self.cfg, self._args())
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.calls, [])

    # -- (c) --ids stays byte-identical for a stub-marked proposal -----------------------

    def test_ids_still_accept_a_stub_marked_proposal(self) -> None:
        self._do_split()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = cli._split(self.cfg, self._args(ids="601,602"))
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls, [], "--ids must never call gh")
        self.assertTrue(self.cfg.bundle("601").is_dir())
        self.assertTrue(self.cfg.bundle("602").is_dir())
        self.assertTrue((self.parent / state.CLOSE_MARKER).exists())
