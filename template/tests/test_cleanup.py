"""`pdca cleanup` — bundle ↔ tracker reconciliation (issue #300; offline, gh mocked).

Proves the reconciliation matrix (closed issue → RESOLVED / discontinue / report;
open issue → comment+close by disposition; merged-PR-but-unaccepted → report only,
never an auto-accept), the dry-run-default write-nothing contract, the fail-closed
gh preflight, and idempotence.
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

from pdca_harness import cleanup, signoff, state
from pdca_harness.config import Config, LeafConfig

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"

_CLOSED = {"state": "CLOSED", "stateReason": "completed", "closedAt": "2026-07-01T00:00:00Z"}
_CLOSED_NP = {"state": "CLOSED", "stateReason": "not_planned", "closedAt": "2026-07-01T00:00:00Z"}
_OPEN = {"state": "OPEN", "stateReason": "", "closedAt": ""}
_PR = "https://github.com/org/repo/pull/7"


def _cfg(root: Path) -> Config:
    return Config(
        root=root,
        bundle_root=root / "results",
        process_dir=root / "process",
        templates_dir=TEMPLATES,
        default_branch="main",
        tracker_system="github",
        tracker_url="",
        issue_id_example="1",
        builder=LeafConfig(mode="stub"),
        reviewer=LeafConfig(mode="stub"),
    )


class CleanupBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp)
        self.gh_calls: list[list[str]] = []
        self.issue_states: dict[str, dict] = {}
        self.pr_states: dict[str, str] = {}
        self.auth_ok = True

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- gh dispatcher (argv-keyed, the test_merged SimpleNamespace pattern) -----
    def _fake_run(self, cmd, capture_output=True, text=True):
        self.gh_calls.append(list(cmd))
        sub = cmd[1:]
        if sub[:2] == ["auth", "status"]:
            return SimpleNamespace(returncode=0 if self.auth_ok else 1, stdout="", stderr="")
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

    def _run(self, ids=(), **kw) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cleanup.subprocess, "run", side_effect=self._fake_run), \
                mock.patch.object(cleanup.shutil, "which", return_value="/usr/bin/gh"), \
                redirect_stdout(out), redirect_stderr(err):
            rc = cleanup.run(self.cfg, list(ids), today="2026-07-18", **kw)
        return rc, out.getvalue(), err.getvalue()

    def _mutations(self) -> list[list[str]]:
        return [c for c in self.gh_calls if c[1:3] in (["issue", "comment"], ["issue", "close"])]

    # --- bundle builders ---------------------------------------------------------
    def _tracker(self, iid: str, notes: str | None = '{"title": "q"}') -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        if notes is not None:
            (d / "notes.json").write_text(notes, encoding="utf-8")
        return d

    def _staged(self, iid: str, *, signoff_action: str | None, patch: str = "diff --git a/x b/x\n",
                pr_url: str | None = None) -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** s\n", encoding="utf-8")
        (d / "patch.diff").write_text(patch, encoding="utf-8")
        (d / "check-gates.json").write_text("{}", encoding="utf-8")
        shutil.copyfile(TEMPLATES / "SUMMARY.md.tpl", d / "SUMMARY.md")
        if signoff_action:
            signoff.record(d / "SUMMARY.md", action=signoff_action, by="T", date="2026-07-01")
        if pr_url is not None:
            (d / "publish.json").write_text(json.dumps({"pr_url": pr_url}), encoding="utf-8")
        return d


class ClosedIssueSide(CleanupBase):
    def test_dry_run_reports_and_writes_nothing(self) -> None:
        d = self._tracker("11")
        self.issue_states["11"] = _CLOSED
        before = (d / "notes.json").read_text(encoding="utf-8")
        rc, out, _err = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("would: mark RESOLVED", out)
        self.assertIn("--apply", out)
        self.assertEqual((d / "notes.json").read_text(encoding="utf-8"), before)
        self.assertEqual(self._mutations(), [])
        self.assertEqual(state.state(d), state.UNPLANNED)

    def test_apply_marks_briefless_tracker_resolved(self) -> None:
        d = self._tracker("11")
        self.issue_states["11"] = _CLOSED
        rc, _out, _err = self._run(apply=True)
        self.assertEqual(rc, 0)
        self.assertEqual(state.state(d), state.RESOLVED)
        data = json.loads((d / "notes.json").read_text(encoding="utf-8"))
        self.assertEqual(data["resolved"]["state_reason"], "completed")
        self.assertEqual(data["title"], "q")               # merged, not clobbered

    def test_unreadable_notes_is_skipped_never_clobbered(self) -> None:
        d = self._tracker("12", notes="{not json")
        self.issue_states["12"] = _CLOSED
        rc, out, _err = self._run(apply=True)
        self.assertEqual(rc, 0)
        self.assertIn("NOT marking resolved", out)
        self.assertEqual((d / "notes.json").read_text(encoding="utf-8"), "{not json")

    def test_apply_discontinues_awaiting_signoff(self) -> None:
        d = self._staged("13", signoff_action=None)
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)
        self.issue_states["13"] = _CLOSED_NP
        rc, _out, _err = self._run(apply=True)
        self.assertEqual(rc, 0)
        self.assertEqual(state.state(d), state.DISCONTINUED)
        self.assertIn("tracker issue closed upstream (not_planned",
                      signoff.iteration_delta(d / "SUMMARY.md"))

    def test_mid_flight_bundle_is_report_only(self) -> None:
        d = self._staged("14", signoff_action=None)
        (d / "SUMMARY.md").unlink()                        # BUILT-ish: no summary yet
        (d / "check-gates.json").unlink()
        self.assertEqual(state.state(d), state.BUILT)
        self.issue_states["14"] = _CLOSED
        rc, out, _err = self._run(apply=True)
        self.assertEqual(rc, 0)
        self.assertIn("finish or discontinue by hand", out)
        self.assertEqual(state.state(d), state.BUILT)      # untouched


class OpenIssueSide(CleanupBase):
    def test_complete_with_merged_pr_comments_and_closes_completed(self) -> None:
        self._staged("21", signoff_action="accept", pr_url=_PR)
        self.issue_states["21"] = _OPEN
        self.pr_states[_PR] = "MERGED"
        rc, _out, _err = self._run(apply=True)
        self.assertEqual(rc, 0)
        muts = self._mutations()
        self.assertEqual(muts[0][1:3], ["issue", "comment"])
        self.assertIn(f"Fixed by {_PR} (merged).", muts[0])
        self.assertEqual(muts[1][1:3], ["issue", "close"])
        self.assertIn("completed", muts[1])

    def test_complete_close_disposition_closes_not_planned(self) -> None:
        self._staged("22", signoff_action="accept", patch="   \n")
        self.issue_states["22"] = _OPEN
        rc, _out, _err = self._run(apply=True)
        self.assertEqual(rc, 0)
        close = [c for c in self._mutations() if c[1:3] == ["issue", "close"]][0]
        self.assertIn("not planned", close)                # the space form gh accepts

    def test_discontinued_closes_not_planned_with_rationale(self) -> None:
        d = self._staged("23", signoff_action=None)
        signoff.record(d / "SUMMARY.md", action="discontinue", by="T",
                       date="2026-07-01", delta="superseded by the v2 design")
        self.issue_states["23"] = _OPEN
        rc, _out, _err = self._run(apply=True)
        self.assertEqual(rc, 0)
        comment = [c for c in self._mutations() if c[1:3] == ["issue", "comment"]][0]
        self.assertTrue(any("superseded by the v2 design" in a for a in comment))

    def test_complete_with_unmerged_pr_is_report_only(self) -> None:
        self._staged("24", signoff_action="accept", pr_url=_PR)
        self.issue_states["24"] = _OPEN
        self.pr_states[_PR] = "OPEN"
        rc, out, _err = self._run(apply=True)
        self.assertEqual(rc, 0)
        self.assertIn("issue stays open until merge", out)
        self.assertEqual(self._mutations(), [])

    def test_bundle_tracker_comment_file_is_preferred(self) -> None:
        d = self._staged("25", signoff_action="accept", pr_url=_PR)
        (d / "tracker-comment.md").write_text("Hand-written closing note.\n", encoding="utf-8")
        self.issue_states["25"] = _OPEN
        self.pr_states[_PR] = "MERGED"
        self._run(apply=True)
        comment = [c for c in self._mutations() if c[1:3] == ["issue", "comment"]][0]
        self.assertIn("--body-file", comment)


class GuardsAndScope(CleanupBase):
    def test_merged_pr_on_unaccepted_bundle_never_auto_accepts(self) -> None:
        d = self._staged("31", signoff_action=None, pr_url=_PR)
        self.pr_states[_PR] = "MERGED"
        self.issue_states["31"] = _OPEN
        rc, out, _err = self._run(apply=True)
        self.assertEqual(rc, 0)
        self.assertIn("never forges the human verdict", out)
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)  # untouched
        self.assertEqual(self._mutations(), [])

    def test_gh_unauthenticated_aborts_before_any_write(self) -> None:
        self._tracker("41")
        self.issue_states["41"] = _CLOSED
        self.auth_ok = False
        rc, _out, err = self._run(apply=True)
        self.assertEqual(rc, 2)
        self.assertIn("gh auth login", err)
        self.assertEqual(self._mutations(), [])

    def test_non_numeric_id_is_skipped_with_note(self) -> None:
        self._tracker("add-dark-mode", notes=None)
        rc, out, _err = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("non-numeric id", out)

    def test_non_github_tracker_skips_issue_side_but_checks_prs(self) -> None:
        self.cfg.tracker_system = "gitlab"
        self._staged("51", signoff_action=None, pr_url=_PR)
        self.pr_states[_PR] = "MERGED"
        rc, out, err = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("not GitHub", err)
        self.assertIn("PR merged but bundle is", out)      # class (b) still ran
        self.assertFalse(any(c[1:3] == ["issue", "view"] for c in self.gh_calls))

    def test_gh_failure_on_one_issue_is_unknown_not_action(self) -> None:
        self._tracker("61")                                # no issue_states entry → gh fails
        rc, out, _err = self._run(apply=True)
        self.assertEqual(rc, 0)
        self.assertIn("tracker state unreadable", out)
        self.assertEqual(self._mutations(), [])

    def test_idempotent_second_run_reports_in_sync(self) -> None:
        d = self._tracker("71")
        self.issue_states["71"] = _CLOSED
        self._run(apply=True)
        self.assertEqual(state.state(d), state.RESOLVED)
        rc, out, _err = self._run(apply=True)
        self.assertEqual(rc, 0)
        self.assertIn("all in sync", out)

    def test_explicit_unknown_id_is_a_clean_error(self) -> None:
        rc, _out, err = self._run(ids=["999"])
        self.assertEqual(rc, 2)
        self.assertIn("no such bundle", err)


if __name__ == "__main__":
    unittest.main()
