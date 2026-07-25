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

from pdca_harness import cleanup, cli, signoff, state
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
        # A derivable tracker URL (#300 review round 14): issue-side reconciliation
        # fails closed without a known repo, so the fixture supplies one.
        tracker_url="https://github.com/example-org/example-repo/issues",
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

    def _closes(self) -> list[list[str]]:
        return [c for c in self.gh_calls if c[1:3] == ["issue", "close"]]

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


class ApplyFailureHonesty(CleanupBase):
    def test_discontinue_on_a_summary_with_no_section_9_is_a_reported_failure(self) -> None:
        # #300 review round 7: a customized SUMMARY.md without §9 must not let
        # `cleanup --apply` exit 0 over a bundle it did not reconcile.
        #
        # #327 moved WHERE this is caught. signoff.record now refuses a summary with no §9
        # outright — a decision written where the (now strict) outcome read cannot see it
        # would never take effect — so the failure is named at its cause instead of being
        # inferred afterwards from the unchanged state. The properties this test exists for
        # are unchanged: non-zero rc, the bundle untouched, and the reason on stderr.
        d = self._staged("61", signoff_action=None)
        (d / "SUMMARY.md").write_text("# custom summary — no canonical section 9\n",
                                      encoding="utf-8")
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)
        self.issue_states["61"] = _CLOSED
        rc, _out, err = self._run(apply=True)
        self.assertEqual(rc, 1)
        self.assertIn("no '## 9. Check sign-off' section", err)
        self.assertIn("action failed", err)
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)  # honestly reported

    def test_discontinue_on_a_section_9_with_no_outcome_field_is_a_reported_failure(self) -> None:
        """§9 is PRESENT here but carries no canonical `- Outcome:` line, so `set_field` used
        to substitute nothing and `record` returned success over a bundle it had not signed
        off — `cleanup` only noticed via its post-hoc state check, and `pdca signoff --accept`
        did not notice at all, exiting 0 (#330 review round 3). `record` now refuses this the
        same way as a missing §9, so the failure is named at its cause.

        `cleanup._discontinue`'s post-hoc state check is left in place as defence in depth: it
        no longer has a constructible trigger, which is the point of tightening `record`."""
        d = self._staged("62", signoff_action=None)
        (d / "SUMMARY.md").write_text(
            "# custom summary\n\n## 9. Check sign-off\nfree prose, none of the fields\n",
            encoding="utf-8")
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)
        self.issue_states["62"] = _CLOSED
        rc, _out, err = self._run(apply=True)
        self.assertEqual(rc, 1)
        self.assertIn("no '- Outcome:' field", err)
        self.assertIn("action failed", err)
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)

    def test_non_object_comment_probe_still_closes_with_the_comment(self) -> None:
        # #300 review round 9: a comment probe returning `null` (or null entries in
        # `comments`) must degrade to already=False — the close proceeds WITH the
        # comment — never AttributeError marking the row failed without closing.
        self._staged("98", signoff_action="accept", pr_url=_PR)
        self.issue_states["98"] = _OPEN
        self.pr_states[_PR] = "MERGED"
        real = self._fake_run

        def null_comments(cmd, **kw):
            if cmd[1:3] == ["issue", "view"] and "comments" in cmd[-1]:
                return SimpleNamespace(returncode=0, stdout="null", stderr="")
            return real(cmd, **kw)

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cleanup.subprocess, "run", side_effect=null_comments), \
                mock.patch.object(cleanup.shutil, "which", return_value="/usr/bin/gh"), \
                redirect_stdout(out), redirect_stderr(err):
            rc = cleanup.run(self.cfg, [], today="2026-07-18", apply=True)
        self.assertEqual(rc, 0)
        closes = self._closes()
        self.assertEqual(len(closes), 1)
        self.assertIn("--comment", closes[0])              # posted with the close

    def test_non_object_pr_json_reads_as_unknown(self) -> None:
        # #300 review round 8: a successful gh (or shim) emitting `null`/`[]` for
        # `pr view` must read as "unknown" (no row action), never an AttributeError
        # aborting the whole sweep while rows are being planned.
        d = self._staged("97", signoff_action=None, pr_url=_PR)
        self.issue_states["97"] = _OPEN
        real = self._fake_run

        def null_pr(cmd, **kw):
            if cmd[1:3] == ["pr", "view"]:
                return SimpleNamespace(returncode=0, stdout="null", stderr="")
            return real(cmd, **kw)

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cleanup.subprocess, "run", side_effect=null_pr), \
                mock.patch.object(cleanup.shutil, "which", return_value="/usr/bin/gh"), \
                redirect_stdout(out), redirect_stderr(err):
            rc = cleanup.run(self.cfg, [], today="2026-07-18")
        self.assertEqual(rc, 0)                            # planned, never crashed
        del d  # (fixture bookkeeping)

    def test_unknown_tracker_repo_disables_issue_side_reconciliation(self) -> None:
        # #300 review round 14: a GitHub tracker whose repo cannot be derived must
        # NOT let gh fall back to the checkout-default repository — under --apply
        # that could close an unrelated same-numbered issue. Issue-side classes are
        # skipped loudly; --repo re-enables them.
        self.cfg.tracker_url = ""                          # nothing to derive from
        self._tracker("87")
        self.issue_states["87"] = _CLOSED
        rc, _out, err = self._run(apply=True)
        self.assertEqual(rc, 0)
        self.assertIn("could not be derived", err)
        self.assertEqual(state.state(self.cfg.bundle("87")), state.UNPLANNED)  # untouched
        rc, _out, _err = self._run(apply=True, repo="example-org/example-repo")
        self.assertEqual(rc, 0)
        self.assertEqual(state.state(self.cfg.bundle("87")), state.RESOLVED)  # --repo works

    def test_unreadable_pr_state_defers_the_empty_patch_close(self) -> None:
        # #300 review round 15: a recorded pr_url whose state cannot be read (a
        # transient gh failure) must be report-only — the PR may in fact be merged
        # and the blank patch mere local damage; falling through would close the
        # issue as 'not planned' with a wrong reason and misleading comment.
        self._staged("89", signoff_action="accept", patch="   \n", pr_url=_PR)
        self.issue_states["89"] = _OPEN                    # pr_states LACKS _PR → rc 1
        rc, out, _err = self._run(apply=True)
        self.assertEqual(rc, 0)
        self.assertIn("unreadable", out)                   # report-only row
        self.assertEqual(self._closes(), [])               # nothing closed

    def test_merged_pr_evidence_beats_a_blank_patch(self) -> None:
        # #300 review round 14: a COMPLETE bundle whose patch.diff was damaged
        # (deleted/truncated) but whose recorded PR is MERGED shipped a real fix —
        # close as completed with the fixed-by comment, never "not planned".
        d = self._staged("88", signoff_action="accept", patch="   \n", pr_url=_PR)
        self.issue_states["88"] = _OPEN
        self.pr_states[_PR] = "MERGED"
        rc, _out, _err = self._run(apply=True)
        self.assertEqual(rc, 0)
        close = self._closes()[0]
        self.assertIn("completed", close)                  # not "not planned"
        self.assertTrue(any("Fixed by" in a for a in close))
        del d  # (fixture bookkeeping)

    def test_unreadable_artifact_becomes_a_report_only_row(self) -> None:
        # #300 review round 10: a non-UTF-8 patch.diff makes _plan_bundle raise while
        # rows are PLANNED — that must become the damaged bundle's own report-only
        # row, never abort the sweep before healthy siblings are reconciled. (The PR
        # is deliberately NOT merged here: a merged PR now short-circuits before the
        # patch read, #300 review round 14 — that case has its own test.)
        broken = self._staged("93", signoff_action="accept", pr_url=_PR)
        (broken / "patch.diff").write_bytes(b"\xff\xfe not utf-8 \x00")
        healthy = self._tracker("94")
        self.issue_states["93"] = _OPEN
        self.issue_states["94"] = _CLOSED
        self.pr_states[_PR] = "OPEN"
        rc, out, _err = self._run(apply=True)
        self.assertEqual(rc, 0)
        self.assertIn("planning failed", out)              # the damaged row reported
        self.assertEqual(state.state(healthy), state.RESOLVED)  # sibling reconciled

    def test_one_raising_row_does_not_abort_the_sweep(self) -> None:
        # #300 review round 7: an exception from one row's action (permission/disk
        # error mid-write) is isolated to that row — reported as its failure while
        # the remaining healthy bundles still reconcile.
        self._tracker("95")
        b = self._tracker("96")
        self.issue_states["95"] = _CLOSED
        self.issue_states["96"] = _CLOSED
        real = cleanup._mark_resolved

        def boom(d, remote, today):
            if d.name == "issue_95":
                raise RuntimeError("disk full")
            return real(d, remote, today)

        with mock.patch.object(cleanup, "_mark_resolved", side_effect=boom):
            rc, _out, err = self._run(apply=True)
        self.assertEqual(rc, 1)
        self.assertIn("RuntimeError", err)
        self.assertEqual(state.state(b), state.RESOLVED)   # sibling still reconciled


class OpenIssueSide(CleanupBase):
    def test_complete_with_merged_pr_closes_completed_with_comment_attached(self) -> None:
        # #300 review: comment + close is ONE gh call, so a transient failure never
        # leaves a posted comment behind for a retry to duplicate.
        self._staged("21", signoff_action="accept", pr_url=_PR)
        self.issue_states["21"] = _OPEN
        self.pr_states[_PR] = "MERGED"
        rc, _out, _err = self._run(apply=True)
        self.assertEqual(rc, 0)
        muts = self._mutations()
        self.assertEqual(len(muts), 1)                     # a single atomic mutation
        close = muts[0]
        self.assertEqual(close[1:3], ["issue", "close"])
        self.assertIn("completed", close)
        self.assertIn("--comment", close)
        self.assertIn(f"Fixed by {_PR} (merged).", close)

    def test_complete_close_disposition_closes_not_planned(self) -> None:
        self._staged("22", signoff_action="accept", patch="   \n")
        self.issue_states["22"] = _OPEN
        rc, _out, _err = self._run(apply=True)
        self.assertEqual(rc, 0)
        close = self._closes()[0]
        self.assertIn("not planned", close)                # the space form gh accepts

    def test_discontinued_closes_not_planned_with_rationale(self) -> None:
        d = self._staged("23", signoff_action=None)
        signoff.record(d / "SUMMARY.md", action="discontinue", by="T",
                       date="2026-07-01", delta="superseded by the v2 design")
        self.issue_states["23"] = _OPEN
        rc, _out, _err = self._run(apply=True)
        self.assertEqual(rc, 0)
        close = self._closes()[0]
        self.assertTrue(any("superseded by the v2 design" in a for a in close))

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
        close = self._closes()[0]
        self.assertIn("Hand-written closing note.", close)  # inlined as the close comment


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

    def test_github_source_without_tracker_role_is_not_canonical(self) -> None:
        # #300 review: only a [[plan.source]] github provider with role = "tracker" is
        # the canonical tracker (sources._is_tracker). A github source WITHOUT the role
        # (supplementary reading) must not drive issue reconciliation — closing an
        # unrelated same-numbered issue in that repo would be a real write to the wrong
        # place.
        self.cfg.tracker_system = "gitlab"
        self.cfg.plan_sources = [{"type": "github", "repo": "other/spec-repo"}]  # no role
        self._tracker("81")
        self.issue_states["81"] = _CLOSED
        rc, out, err = self._run(apply=True)
        self.assertEqual(rc, 0)
        self.assertIn("not GitHub", err)                   # issue side skipped
        self.assertFalse(any(c[1:3] == ["issue", "view"] for c in self.gh_calls))
        # …and WITH the role, the provider's repo is used for the issue reads.
        self.cfg.plan_sources = [{"type": "github", "role": "tracker",
                                  "repo": "org/tracker-repo"}]
        rc, _out, _err = self._run(apply=True)
        self.assertEqual(rc, 0)
        view = next(c for c in self.gh_calls if c[1:3] == ["issue", "view"])
        self.assertIn("org/tracker-repo", view)

    def test_tracker_source_type_is_normalized_like_sources_py(self) -> None:
        # #300 review round 4: `type = "GitHub"` is a valid tracker source for
        # sources.seed — an exact compare here dropped its repo and pointed gh at the
        # CURRENT repository's same-numbered issues.
        self.cfg.tracker_system = "github"
        self.cfg.plan_sources = [{"type": " GitHub ", "role": "Tracker",
                                  "repo": "org/tracker-repo"}]
        self._tracker("82")
        self.issue_states["82"] = _CLOSED
        rc, _out, _err = self._run(apply=True)
        self.assertEqual(rc, 0)
        view = next(c for c in self.gh_calls if c[1:3] == ["issue", "view"])
        self.assertIn("org/tracker-repo", view)            # the configured repo, not cwd

    def test_non_github_tracker_role_source_suppresses_legacy_fallback(self) -> None:
        # #300 review round 5: sources.py treats ANY tracker-role source as canonical —
        # a gitlab tracker-role source means the tracker is gitlab even when a stale
        # [tracker].system = "github" remains, and falling back would close the current
        # GitHub repo's same-numbered issue.
        self.cfg.tracker_system = "github"                 # stale legacy setting
        self.cfg.plan_sources = [{"type": "gitlab", "role": "tracker"}]
        self._tracker("84")
        rc, _out, err = self._run(apply=True)
        self.assertEqual(rc, 0)
        self.assertIn("not GitHub", err)                   # issue side skipped
        self.assertFalse(any(c[1:3] == ["issue", "view"] for c in self.gh_calls))

    def test_close_retry_never_reposts_an_existing_comment(self) -> None:
        # #300 review round 5: `gh issue close --comment` is two API operations — the
        # comment can land while the close fails. A retry probes for our exact body
        # and closes WITHOUT the comment when it is already there.
        self._staged("185", signoff_action="accept", pr_url=_PR)
        self.issue_states["185"] = dict(_OPEN)
        # Simulate the partial state: the closing comment already exists on the issue.
        self.issue_states["185"]["comments"] = [
            {"body": f"Fixed by {_PR} (merged)."}]
        self.pr_states[_PR] = "MERGED"
        rc, _out, _err = self._run(apply=True)
        self.assertEqual(rc, 0)
        close = self._closes()[0]
        self.assertNotIn("--comment", close)               # no repost on the retry

    def test_repeated_explicit_ids_are_deduplicated(self) -> None:
        # #300 review round 4: `cleanup 21 21 --apply` must not run the close twice.
        self._staged("83", signoff_action="accept", pr_url=_PR)
        self.issue_states["83"] = _OPEN
        self.pr_states[_PR] = "MERGED"
        rc, _out, _err = self._run(ids=["83", "83"], apply=True)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self._closes()), 1)           # one mutation, not two

    def test_reopened_issue_clears_the_resolved_marker(self) -> None:
        # #300 review: RESOLVED is in HALTED, so a tracker REOPEN after resolution
        # would otherwise be suppressed forever. Cleanup re-checks the remote and,
        # under --apply, clears the marker so the item is pending again.
        d = self._tracker("91")
        self.issue_states["91"] = _CLOSED
        self._run(apply=True)
        self.assertEqual(state.state(d), state.RESOLVED)
        self.issue_states["91"] = _OPEN                    # the tracker reopened it
        rc, out, _err = self._run(apply=True)
        self.assertEqual(rc, 0)
        self.assertEqual(state.state(d), state.UNPLANNED)  # pending again
        # #300 review round 6: the WHOLE closure-era notes.json is set aside (the
        # sources.clear_resolved_marker contract) — a surviving stale file would make
        # ensure_notes refuse the re-fetch and Plan would brief pre-reopen context.
        self.assertFalse((d / "notes.json").exists())
        aside = d / "notes.superseded-by-reopen.json"
        self.assertTrue(aside.exists())
        data = json.loads(aside.read_text(encoding="utf-8"))
        self.assertIn("resolved", data)                    # kept inspectable, unedited
        self.assertEqual(data["title"], "q")
        # Still-closed stays in sync (no row, no write).
        self.issue_states["91"] = _CLOSED
        self._run(apply=True)
        self.assertEqual(state.state(d), state.RESOLVED)   # re-resolved by class a1

    def test_placeholder_brief_bundle_still_takes_the_resolved_path(self) -> None:
        # #300 review round 2: an unfilled template copy is "never authored" (#113) —
        # the same placeholder semantics as state.state(). A bare existence test left
        # the bundle UNPLANNED with no row, unreconcilable forever.
        d = self._tracker("85")
        (d / "brief.md").write_text("- **Slug:** <fill-me>\n", encoding="utf-8")
        self.issue_states["85"] = _CLOSED
        rc, _out, _err = self._run(apply=True)
        self.assertEqual(rc, 0)
        self.assertEqual(state.state(d), state.RESOLVED)

    def test_damaged_publish_record_never_aborts_the_sweep(self) -> None:
        # #300 review round 2: a decodable-but-non-object publish.json ([] / null) must
        # read as "no record" — one damaged bundle must not block every other bundle.
        broken = self._staged("86", signoff_action=None)
        (broken / "publish.json").write_text("[]", encoding="utf-8")
        healthy = self._tracker("87")
        self.issue_states["86"] = _OPEN
        self.issue_states["87"] = _CLOSED
        rc, _out, _err = self._run(apply=True)
        self.assertEqual(rc, 0)                            # no crash, sweep completed
        self.assertEqual(state.state(healthy), state.RESOLVED)  # sibling still reconciled

    def test_waves_excludes_resolved_placeholder_bundles(self) -> None:
        # #302 review round 2 (filed on PR #308): a resolved bundle with a stray
        # placeholder brief has brief.md on disk — `pdca waves` must filter on the
        # terminal set, not the file test, or settled work reads as schedulable.
        d = self._tracker("88")
        (d / "brief.md").write_text("- **Slug:** <fill-me>\n", encoding="utf-8")
        self.issue_states["88"] = _CLOSED
        self._run(apply=True)
        self.assertEqual(state.state(d), state.RESOLVED)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli._waves(self.cfg, [])
        self.assertEqual(rc, 0)
        self.assertIn("no briefed bundles to schedule", out.getvalue())

    def test_active_bundle_shadows_its_stale_archived_copy(self) -> None:
        # #300 review round 3: an issue reopened into a NEW active cycle must be
        # reconciled against that cycle only — its stale archived COMPLETE copy (merged
        # PR) could otherwise close the reopened tracker issue mid-flight.
        arch = self.cfg.bundle_root / "completed" / "issue_93"
        arch.mkdir(parents=True)
        (arch / "brief.md").write_text("- **Slug:** s\n", encoding="utf-8")
        (arch / "patch.diff").write_text("diff --git a/x b/x\n", encoding="utf-8")
        (arch / "check-gates.json").write_text("{}", encoding="utf-8")
        shutil.copyfile(TEMPLATES / "SUMMARY.md.tpl", arch / "SUMMARY.md")
        signoff.record(arch / "SUMMARY.md", action="accept", by="T", date="2026-07-01")
        (arch / "publish.json").write_text(json.dumps({"pr_url": _PR}), encoding="utf-8")
        active = self._staged("93", signoff_action=None)     # the reopened active cycle
        self.assertEqual(state.state(active), state.AWAITING_SIGNOFF)
        self.issue_states["93"] = _OPEN
        self.pr_states[_PR] = "MERGED"
        rc, _out, _err = self._run(apply=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self._closes(), [])                 # NOT closed by the stale copy

    def test_archived_completed_bundles_are_reconciled_too(self) -> None:
        # #300 review: a bundle archived to results/completed/ (#171) is exactly the
        # locally-terminal case class (c) exists to close — the sweep must visit it.
        d = self.cfg.bundle_root / "completed" / "issue_95"
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** s\n", encoding="utf-8")
        (d / "patch.diff").write_text("diff --git a/x b/x\n", encoding="utf-8")
        (d / "check-gates.json").write_text("{}", encoding="utf-8")
        shutil.copyfile(TEMPLATES / "SUMMARY.md.tpl", d / "SUMMARY.md")
        signoff.record(d / "SUMMARY.md", action="accept", by="T", date="2026-07-01")
        (d / "publish.json").write_text(json.dumps({"pr_url": _PR}), encoding="utf-8")
        self.issue_states["95"] = _OPEN
        self.pr_states[_PR] = "MERGED"
        rc, _out, _err = self._run(apply=True)
        self.assertEqual(rc, 0)
        close = self._closes()[0]
        self.assertIn("95", close)
        self.assertIn("completed", close)


if __name__ == "__main__":
    unittest.main()
