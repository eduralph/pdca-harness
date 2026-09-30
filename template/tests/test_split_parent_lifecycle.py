"""A split parent's tracker issue, and how deep a split may go (issue #545; offline).

Two behaviours:

* ``pdca cleanup`` keeps a finished split parent's issue OPEN while any child issue is
  open or unreadable, and closes it — with a comment naming every child — once all of
  them are closed: ``completed`` if at least one child was completed, else
  ``not planned``. A ``children`` list it cannot read whole never closes it. Before #545
  the parent fell into the generic "empty patch → not planned" close, with its children
  still open.
* ``pdca split <id> --accept`` refuses a bundle whose recorded lineage depth is 2 or
  more unless ``--force`` is given.

Only ``gh`` is faked; the production entry points (``cleanup.run``, ``cli._split``,
``cli.main``) run.
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pdca_harness import cleanup, cli, signoff, split, state
from pdca_harness.config import Config, LeafConfig

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
AGENTS = Path(__file__).resolve().parents[1] / "agents"
REPO = "example-org/example-repo"
PR = "https://github.com/org/repo/pull/7"

# The real gh prints the reason in upper case (`COMPLETED`, `NOT_PLANNED`).
OPEN = {"state": "OPEN", "stateReason": "", "closedAt": ""}
DONE = {"state": "CLOSED", "stateReason": "COMPLETED", "closedAt": "2026-07-01T00:00:00Z"}
DROPPED = {"state": "CLOSED", "stateReason": "NOT_PLANNED", "closedAt": "2026-07-01T00:00:00Z"}


def _cfg(root: Path, tracker_url: str) -> Config:
    return Config(
        root=root, bundle_root=root / "results", process_dir=root / "process",
        templates_dir=TEMPLATES, default_branch="main", tracker_system="github",
        tracker_url=tracker_url, issue_id_example="1",
        builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"))


class SplitParentBase(unittest.TestCase):
    """Mirrors ``CleanupBase`` (``test_cleanup.py:49-116``): an argv-keyed fake ``gh``."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp, f"https://github.com/{REPO}/issues")
        self.gh_calls: list[list[str]] = []
        self.issue_states: dict[str, dict] = {"500": OPEN}
        self.pr_states: dict[str, str] = {}

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_run(self, cmd, capture_output=True, text=True):
        self.gh_calls.append(list(cmd))
        sub = cmd[1:]
        if sub[:2] == ["auth", "status"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if sub[:2] == ["issue", "view"]:
            st = self.issue_states.get(sub[2])
            if st is None:
                return SimpleNamespace(returncode=1, stdout="", stderr="not found")
            return SimpleNamespace(returncode=0, stdout=json.dumps(st), stderr="")
        if sub[:2] == ["pr", "view"]:
            s = self.pr_states.get(sub[2], "")
            if not s:
                return SimpleNamespace(returncode=1, stdout="", stderr="no pr")
            return SimpleNamespace(returncode=0, stdout=json.dumps({"state": s}), stderr="")
        if sub[:2] in (["issue", "comment"], ["issue", "close"]):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected gh call")

    def _run(self, *, apply: bool = True) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cleanup.subprocess, "run", side_effect=self._fake_run), \
                mock.patch.object(cleanup.shutil, "which", return_value="/usr/bin/gh"), \
                redirect_stdout(out), redirect_stderr(err):
            rc = cleanup.run(self.cfg, [], apply=apply, today="2026-07-18")
        return rc, out.getvalue(), err.getvalue()

    def _closes(self) -> list[list[str]]:
        return [c for c in self.gh_calls if c[1:3] == ["issue", "close"]]

    def _mutations_for(self, number: str) -> list[list[str]]:
        return [c for c in self.gh_calls
                if c[1:3] in (["issue", "comment"], ["issue", "close"]) and c[3] == number]

    def _line(self, out: str, bundle: str = "issue_500") -> str:
        lines = [ln for ln in out.splitlines() if ln.startswith(bundle + " ")]
        self.assertEqual(len(lines), 1, out)
        return lines[0]

    def _complete(self, iid: str, *, patch: str = "", pr_url: str | None = None) -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** s\n", encoding="utf-8")
        (d / "patch.diff").write_text(patch, encoding="utf-8")
        (d / "check-gates.json").write_text("{}", encoding="utf-8")
        shutil.copyfile(TEMPLATES / "SUMMARY.md.tpl", d / "SUMMARY.md")
        signoff.record(d / "SUMMARY.md", action="accept", by="T", date="2026-07-01")
        if pr_url is not None:
            (d / "publish.json").write_text(json.dumps({"pr_url": pr_url}), encoding="utf-8")
        return d

    def _split_parent(self, *, lineage: str | None = None, pr_url: str | None = None) -> Path:
        d = self._complete("500", pr_url=pr_url)
        (d / state.CLOSE_MARKER).write_text("split\n", encoding="utf-8")
        if lineage is None:
            lineage = json.dumps({"version": split.LINEAGE_VERSION, "id": "500",
                                  "children": ["601", "602"]})
        if lineage != "<absent>":
            (d / split.LINEAGE).write_text(lineage, encoding="utf-8")
        self.assertEqual(state.state(d), state.COMPLETE)
        return d


class TheParentWaitsOnItsChildren(SplitParentBase):
    def test_p1_one_child_open_makes_no_tracker_change(self) -> None:
        self._split_parent()
        self.issue_states.update({"601": OPEN, "602": DONE})
        rc, out, _err = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(self._mutations_for("500"), [])
        line = self._line(out)
        self.assertIn("#601", line)
        self.assertNotIn("#602", line)

    def test_p2_all_closed_one_completed_closes_completed_naming_every_child(self) -> None:
        self._split_parent()
        self.issue_states.update({"601": DONE, "602": DROPPED})
        rc, _out, _err = self._run()
        self.assertEqual(rc, 0)
        closes = self._closes()
        self.assertEqual(len(closes), 1)
        close = closes[0]
        self.assertEqual(close[3], "500")
        self.assertEqual(close[close.index("--repo") + 1], REPO)
        self.assertEqual(close[close.index("--reason") + 1], "completed")
        comment = close[close.index("--comment") + 1]
        self.assertIn("#601", comment)
        self.assertIn("#602", comment)
        # Each child is listed with its own close reason, in words rather than gh's enum.
        self.assertIn("#601 (completed)", comment)
        self.assertIn("#602 (not planned)", comment)

    def test_p3_all_closed_none_completed_closes_not_planned(self) -> None:
        self._split_parent()
        self.issue_states.update({"601": DROPPED, "602": DROPPED})
        rc, _out, _err = self._run()
        self.assertEqual(rc, 0)
        closes = self._closes()
        self.assertEqual(len(closes), 1)
        close = closes[0]
        self.assertEqual(close[3], "500")
        self.assertEqual(close[close.index("--reason") + 1], "not planned")
        comment = close[close.index("--comment") + 1]
        self.assertIn("#601", comment)
        self.assertIn("#602", comment)

    def test_completed_is_read_without_regard_to_case(self) -> None:
        self._split_parent()
        self.issue_states.update({"601": dict(DONE, stateReason="completed"), "602": DROPPED})
        self._run()
        close = self._closes()[0]
        self.assertEqual(close[close.index("--reason") + 1], "completed")

    def test_p4_unreadable_child_state_never_closes(self) -> None:
        self._split_parent()
        self.issue_states.update({"601": DONE})          # 602's `gh issue view` fails
        rc, out, _err = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(self._mutations_for("500"), [])
        line = self._line(out)
        self.assertIn("unknown", line)
        self.assertIn("#602", line)

    def test_p5_unknown_children_never_close(self) -> None:
        cases = {
            "missing": "<absent>",
            "not json": "{not json",
            "empty children": json.dumps({"version": split.LINEAGE_VERSION, "children": []}),
            "no children key": json.dumps({"version": split.LINEAGE_VERSION, "depth": 1}),
            "children not a list": json.dumps({"version": split.LINEAGE_VERSION,
                                               "children": 7}),
        }
        for why, lineage in cases.items():
            with self.subTest(case=why):
                self.tearDown()
                self.setUp()
                self._split_parent(lineage=lineage)
                self.issue_states.update({"601": DONE, "602": DONE})
                rc, out, _err = self._run()
                self.assertEqual(rc, 0)
                self.assertEqual(self._mutations_for("500"), [])
                line = self._line(out)
                self.assertIn("children are unknown", line)
                self.assertIn("by hand", line)

    def test_p5_a_partly_damaged_children_list_never_closes(self) -> None:
        """One usable id beside one unusable entry is NOT a list of one child.

        The flow's tolerant reader drops the null / number / blank and keeps "601"; if
        cleanup closed on what is left, #601 alone would close the parent while the child
        the damaged entry stood for was never read. Every child here is closed as
        completed, so the ONLY reason not to close is the damaged entry.
        """
        cases = {
            "null entry": ["601", None],
            "number entry": ["601", 602],
            "blank entry": ["601", "  "],
        }
        for why, children in cases.items():
            with self.subTest(case=why):
                self.tearDown()
                self.setUp()
                self._split_parent(lineage=json.dumps(
                    {"version": split.LINEAGE_VERSION, "id": "500", "children": children}))
                self.issue_states.update({"601": DONE, "602": DONE})
                rc, out, _err = self._run()
                self.assertEqual(rc, 0)
                self.assertEqual(self._mutations_for("500"), [])
                line = self._line(out)
                self.assertIn("children are unknown", line)
                self.assertIn("by hand", line)

    def test_a_child_id_that_is_not_a_tracker_number_is_closed_by_hand(self) -> None:
        """`MANT-7` is a valid lineage id (a non-GitHub tracker's shape) that no
        `gh issue view` can read, so waiting never resolves it: the report says so."""
        self._split_parent(lineage=json.dumps(
            {"version": split.LINEAGE_VERSION, "id": "500", "children": ["601", "MANT-7"]}))
        self.issue_states.update({"601": DONE})
        rc, out, _err = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(self._mutations_for("500"), [])
        line = self._line(out)
        self.assertIn("MANT-7", line)
        self.assertIn("not a tracker issue number", line)
        self.assertIn("by hand", line)

    def test_p6_dry_run_says_would_and_closes_nothing(self) -> None:
        self._split_parent()
        self.issue_states.update({"601": DONE, "602": DONE})
        rc, out, _err = self._run(apply=False)
        self.assertEqual(rc, 0)
        self.assertEqual(self._closes(), [])
        self.assertIn("would:", self._line(out))

    def test_p7_unsplit_empty_patch_still_closes_not_planned(self) -> None:
        self._complete("22", patch="   \n")
        self.issue_states["22"] = OPEN
        rc, _out, _err = self._run()
        self.assertEqual(rc, 0)
        closes = self._closes()
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0][3], "22")
        self.assertEqual(closes[0][closes[0].index("--reason") + 1], "not planned")

    def test_p8_tracker_comment_file_does_not_drop_the_children(self) -> None:
        d = self._split_parent()
        (d / "tracker-comment.md").write_text("Hand-written closing note.\n", encoding="utf-8")
        self.issue_states.update({"601": DONE, "602": DONE})
        self._run()
        closes = self._closes()
        self.assertEqual(len(closes), 1)
        comment = closes[0][closes[0].index("--comment") + 1]
        self.assertIn("#601", comment)
        self.assertIn("#602", comment)
        self.assertIn("Hand-written closing note.", comment)   # kept, not replaced

    def test_p9_merged_pr_from_before_the_split_still_waits_on_children(self) -> None:
        self._split_parent(pr_url=PR)
        self.pr_states[PR] = "MERGED"
        self.issue_states.update({"601": OPEN, "602": DONE})
        rc, out, _err = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(self._mutations_for("500"), [])
        self.assertIn("#601", self._line(out))


def _proposal(*children: str) -> str:
    body = "<!-- pdca:split-proposal v1 -->\n# Split proposal\n\n"
    for i, child in enumerate(children, 1):
        body += f"<!-- pdca:child child-{i} -->\n{child}\n<!-- pdca:end child-{i} -->\n\n"
    return body


_ONE = "- **Slug:** first\n- **Defect / goal:** a\n"
_TWO = "- **Slug:** second\n- **Defect / goal:** b\n"


class SplitDepthIsBounded(unittest.TestCase):
    """Driven through ``cli._split`` with only ``gh`` faked — the ``TheWholeChainUnmocked``
    pattern (``test_split.py:1397-1448``)."""

    def _setup(self, lineage: str | None) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = _cfg(self.tmp, "https://github.com/acme/widgets")
        self.parent = self.cfg.bundle("500")
        self.parent.mkdir(parents=True)
        (self.parent / "brief.md").write_text("- **Slug:** parent\n", encoding="utf-8")
        (self.parent / split.PROPOSAL).write_text(_proposal(_ONE, _TWO), encoding="utf-8")
        if lineage is not None:
            (self.parent / split.LINEAGE).write_text(lineage, encoding="utf-8")
        self.creates: list[list[str]] = []

    def _fake_gh(self, cmd, capture_output=False, text=False, cwd=None):
        if list(cmd[:3]) == ["gh", "issue", "view"]:
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")
        self.creates.append(list(cmd))
        n = str(600 + len(self.creates))
        return SimpleNamespace(returncode=0, stderr="",
                               stdout=f"https://github.com/acme/widgets/issues/{n}\n")

    def _accept(self, **extra) -> tuple[int, str]:
        err = io.StringIO()
        with mock.patch("pdca_harness.split.subprocess", SimpleNamespace(run=self._fake_gh)), \
             mock.patch("pdca_harness.split.shutil.which", return_value="/usr/bin/gh"), \
             redirect_stderr(err), redirect_stdout(io.StringIO()):
            rc = cli._split(self.cfg, SimpleNamespace(issue_id="500", accept=True, ids="",
                                                      **extra))
        return rc, err.getvalue()

    @staticmethod
    def _record(depth) -> str:
        return json.dumps({"version": split.LINEAGE_VERSION, "id": "500", "parent": "400",
                           "siblings": [], "depth": depth})

    def _child_depth(self, iid: str) -> int:
        record = split.read_lineage(self.cfg.bundle(iid))
        self.assertIsNotNone(record, f"child {iid} has no lineage record")
        return record["depth"]

    def test_d1_depth_two_or_more_is_refused_without_force(self) -> None:
        for depth in (2, 3):
            with self.subTest(depth=depth):
                self._setup(self._record(depth))
                rc, err = self._accept()
                self.assertNotEqual(rc, 0)
                self.assertEqual(self.creates, [], "a tracker issue was filed")
                self.assertFalse(self.cfg.bundle("601").exists())
                self.assertFalse((self.parent / state.CLOSE_MARKER).exists())
                self.assertIn(f"depth {depth}", err)
                self.assertIn("--force", err)

    def _main(self, *flags: str) -> tuple[int, str]:
        """The real CLI entry, argparse included, so ``--force`` is the flag an operator
        types rather than an attribute a test builds by hand. ``Config.load`` is patched
        the way ``test_autoiterate.py:776`` does it."""
        err = io.StringIO()
        with mock.patch.object(cli.Config, "load", return_value=self.cfg), \
             mock.patch("pdca_harness.split.subprocess", SimpleNamespace(run=self._fake_gh)), \
             mock.patch("pdca_harness.split.shutil.which", return_value="/usr/bin/gh"), \
             redirect_stderr(err), redirect_stdout(io.StringIO()):
            try:
                rc = cli.main(["split", "500", "--accept", *flags])
            except SystemExit as exc:                    # argparse refused the argv
                self.fail(f"`split` rejected {flags!r} (exit {exc.code}): {err.getvalue()}")
        return rc, err.getvalue()

    def test_d1_the_real_cli_without_force_is_refused(self) -> None:
        self._setup(self._record(2))
        rc, err = self._main()
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.creates, [], "a tracker issue was filed")
        self.assertFalse(self.cfg.bundle("601").exists())
        self.assertIn("--force", err)

    def test_d2_the_real_cli_accepts_with_force(self) -> None:
        self._setup(self._record(2))
        rc, err = self._main("--force")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._child_depth("601"), 3)
        self.assertEqual(self._child_depth("602"), 3)

    def test_d2_force_accepts_and_children_record_the_next_depth(self) -> None:
        for depth in (2, 3):
            with self.subTest(depth=depth):
                self._setup(self._record(depth))
                rc, err = self._accept(force=True)
                self.assertEqual(rc, 0, err)
                self.assertEqual(self._child_depth("601"), depth + 1)
                self.assertEqual(self._child_depth("602"), depth + 1)

    def test_d3_shallow_parents_are_accepted_as_today(self) -> None:
        for lineage, child_depth in ((None, 1), (self._record(1), 2)):
            with self.subTest(lineage=lineage):
                self._setup(lineage)
                rc, err = self._accept()
                self.assertEqual(rc, 0, err)
                self.assertEqual(self._child_depth("601"), child_depth)

    def test_d4_a_damaged_record_counts_as_depth_zero(self) -> None:
        for lineage in ("{not json", self._record("one"), self._record(None),
                        self._record(True)):
            with self.subTest(lineage=lineage):
                self._setup(lineage)
                rc, err = self._accept()
                self.assertEqual(rc, 0, err)
                self.assertTrue(self.cfg.bundle("601").is_dir())


class TheWordsSayIt(unittest.TestCase):
    def _role(self, name: str) -> str:
        # The way `ThePlannerIsToldItOwnsTheSplit._role` reads it (test_split.py:982-987),
        # so this works on the template and on a rendered instance.
        for candidate in (f"{name}.md.jinja", f"{name}.md"):
            path = AGENTS / candidate
            if path.is_file():
                return path.read_text(encoding="utf-8")
        raise AssertionError(f"no role prompt for {name!r}")

    def test_s1_planner_is_told_the_parent_stays_open_and_force_is_the_humans(self) -> None:
        text = self._role("planner")
        self.assertIn("stays open", text)
        self.assertIn("--force", text)
        paragraphs = [p for p in text.split("\n\n") if "--force" in p]
        self.assertTrue(paragraphs)
        for p in paragraphs:
            self.assertIn("human", p)

    def test_the_planner_is_not_promised_a_completed_close(self) -> None:
        """The close reason depends on the children (none completed → not planned), and
        only a COMPLETE parent waits for them. A paragraph that says "stays open" and then
        "closes it as completed" promises more than `cleanup` does."""
        paragraphs = [p for p in self._role("planner").split("\n\n") if "stays open" in p]
        self.assertTrue(paragraphs)
        for p in paragraphs:
            self.assertIn("not planned", p)
            self.assertIn("COMPLETE", p)

    def test_s2_cleanup_matrix_names_the_split_parent_row(self) -> None:
        self.assertIn("split", cleanup.__doc__)
        self.assertIn("children", cleanup.__doc__)


if __name__ == "__main__":
    unittest.main()
