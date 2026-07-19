"""RESOLVED terminal state for notes-only trackers (issue #302).

A briefless bundle whose notes.json carries a top-level ``resolved`` object was settled
in the tracker outside a cycle — terminal, not pending. Proves the defensive contract
(malformed / non-object input never crashes and never resolves) and that RESOLVED is
threaded through every terminal set (driver HALTED, flow terminals, status ordering).
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from unittest import mock

from pdca_harness import cli, driver, flow, leaves, sources, state
from pdca_harness.config import Config, LeafConfig

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


def _cfg(root: Path) -> Config:
    return Config(
        root=root,
        bundle_root=root / "results",
        process_dir=root / "process",
        templates_dir=TEMPLATES,
        default_branch="main",
        tracker_system="github",
        tracker_url="https://github.com/example-org/example-repo/issues",
        issue_id_example="1",
        builder=LeafConfig(mode="stub"),
        reviewer=LeafConfig(mode="stub"),
    )


_RESOLVED = {"resolved": {"github_state": "closed", "state_reason": "completed",
                          "closed_at": "2026-07-01T00:00:00Z", "note": "settled in-issue"}}


class StateResolved(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundle(self, iid: str, notes: str | None) -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        if notes is not None:
            (d / "notes.json").write_text(notes, encoding="utf-8")
        return d

    def test_waves_excludes_resolved_bundles(self) -> None:
        # #302 review round 8: `pdca waves` (no ids) must not report a terminal
        # RESOLVED bundle (e.g. one keeping a placeholder brief) as a runnable
        # wave — the preview must match the flow's actual drive set.
        import io
        from contextlib import redirect_stdout
        d = self._bundle("9", json.dumps(_RESOLVED))
        (d / "brief.md").write_text("- **Slug:** <fill-me>\n", encoding="utf-8")
        self.assertEqual(state.state(d), state.RESOLVED)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli._waves(self.cfg, [])
        self.assertEqual(rc, 0)
        self.assertIn("no briefed bundles", out.getvalue())
        # The explicit-id branch agrees with the no-id scan (#302 review round 9):
        # naming the RESOLVED bundle outright must not resurrect it as a wave.
        from contextlib import redirect_stderr
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli._waves(self.cfg, ["9"])
        self.assertEqual(rc, 0)
        self.assertIn("no briefed bundles", out.getvalue())
        self.assertIn("already terminal", err.getvalue())

    def test_briefless_with_resolved_object_is_resolved(self) -> None:
        d = self._bundle("1", json.dumps(_RESOLVED))
        self.assertEqual(state.state(d), state.RESOLVED)

    def test_briefless_without_resolved_stays_unplanned(self) -> None:
        d = self._bundle("2", json.dumps({"title": "open question"}))
        self.assertEqual(state.state(d), state.UNPLANNED)

    def test_malformed_notes_is_unplanned_not_a_crash(self) -> None:
        d = self._bundle("3", "{not json")
        self.assertEqual(state.state(d), state.UNPLANNED)

    def test_non_object_resolved_is_unplanned(self) -> None:
        for iid, value in (("4", json.dumps({"resolved": "closed"})),
                           ("5", json.dumps({"resolved": True})),
                           ("6", json.dumps(["resolved"]))):
            d = self._bundle(iid, value)
            self.assertEqual(state.state(d), state.UNPLANNED, msg=value)

    def test_no_notes_at_all_is_unplanned(self) -> None:
        d = self._bundle("7", None)
        self.assertEqual(state.state(d), state.UNPLANNED)

    def test_briefed_bundle_with_stray_resolved_key_is_not_reclassified(self) -> None:
        d = self._bundle("8", json.dumps(_RESOLVED))
        (d / "brief.md").write_text("- **Slug:** real-work\n", encoding="utf-8")
        self.assertEqual(state.state(d), state.PLANNED)

    def test_placeholder_brief_does_not_unresolve_a_resolved_tracker(self) -> None:
        # #302 review: a stray unfilled template copy (e.g. the stub/batch planner
        # copying brief.md.tpl) is "never authored" — the same standing as no brief —
        # so the tracker's terminal resolution still wins and the bundle must not
        # reappear as pending. Without the resolved marker it stays UNPLANNED (#113).
        d = self._bundle("9", json.dumps(_RESOLVED))
        (d / "brief.md").write_text("- **Slug:** <fill-me>\n", encoding="utf-8")
        self.assertEqual(state.state(d), state.RESOLVED)
        (d / "notes.json").unlink()
        self.assertEqual(state.state(d), state.UNPLANNED)


class ResolvedIsTerminal(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_resolved_in_every_terminal_set(self) -> None:
        self.assertIn(state.RESOLVED, state.HALTED)
        self.assertIn(state.RESOLVED, flow._TERMINAL)
        self.assertIn(state.RESOLVED, cli._STATE_ORDER)
        # Status ordering groups RESOLVED with the terminals, at the very bottom.
        self.assertGreater(cli._STATE_ORDER.index(state.RESOLVED),
                           cli._STATE_ORDER.index(state.DISCONTINUED))

    def test_driver_halts_immediately_on_resolved(self) -> None:
        d = self.cfg.bundle("9")
        d.mkdir(parents=True)
        (d / "notes.json").write_text(json.dumps(_RESOLVED), encoding="utf-8")
        self.assertEqual(driver.run_issue(d, self.cfg), state.RESOLVED)

    def test_flow_ids_skips_resolved_as_terminal(self) -> None:
        d = self.cfg.bundle("10")
        d.mkdir(parents=True)
        (d / "notes.json").write_text(json.dumps(_RESOLVED), encoding="utf-8")
        self.assertEqual(flow.flow_ids(self.cfg, ["10"], plan_missing=False), {})


class PlanNeverReopensResolved(unittest.TestCase):
    """#302 review: Plan must not re-open a settled ticket — not when seeding first
    reveals the resolution, not via the id-seeded batch, not via a CSV-session brief
    (an authored brief deliberately overrides the marker, so the guard sits in Plan)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp)
        self.cfg.bundle_root.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _resolved(self, iid: str) -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "notes.json").write_text(json.dumps(_RESOLVED), encoding="utf-8")
        return d

    def test_do_plan_skips_a_tracker_resolved_at_seed_time(self) -> None:
        # The seed can be what FIRST writes the resolved notes (a notes_cmd / tracker
        # source during `pdca flow <id>`) — the planner must not run after it.
        d = self._resolved("21")
        leaves.do_plan(d, self.cfg)                       # stub planner would write a brief
        self.assertFalse((d / "brief.md").exists())
        self.assertEqual(state.state(d), state.RESOLVED)

    def test_do_plan_batch_excludes_resolved_ids(self) -> None:
        self._resolved("22")
        leaves.do_plan_batch(self.cfg, ids=["22", "23"])
        self.assertFalse((self.cfg.bundle("22") / "brief.md").exists())
        self.assertEqual(state.state(self.cfg.bundle("22")), state.RESOLVED)
        self.assertTrue((self.cfg.bundle("23") / "brief.md").exists())  # sibling briefed

    def test_csv_session_brief_for_a_resolved_bundle_is_set_aside(self) -> None:
        # CSV/default path: the planner picks ids MID-session, so the up-front filter
        # can't protect the bundle — a brief it authors for one is rejected afterwards.
        d = self._resolved("24")
        self.cfg.planner = LeafConfig(mode="command", interactive=True, argv=["x"])

        def fake_invoke(leaf, cwd, prompt, **kw):
            (d / "brief.md").write_text("- **Slug:** reopened\n- **Defect:** x.\n",
                                        encoding="utf-8")

        # No gh on PATH → the reopen revalidation stays conservative (False) offline.
        with mock.patch.object(leaves, "_invoke", side_effect=fake_invoke), \
                mock.patch.object(sources.shutil, "which", return_value=None):
            leaves.do_plan_batch(self.cfg)
        self.assertFalse((d / "brief.md").exists())
        self.assertTrue((d / "brief.superseded-by-resolution.md").exists())  # kept, aside
        self.assertEqual(state.state(d), state.RESOLVED)
        # A second offending session gets its own destination (#302 review round 3) —
        # the first rejection artifact is never overwritten.
        with mock.patch.object(leaves, "_invoke", side_effect=fake_invoke), \
                mock.patch.object(sources.shutil, "which", return_value=None):
            leaves.do_plan_batch(self.cfg)
        self.assertTrue((d / "brief.superseded-by-resolution.md").exists())
        self.assertTrue((d / "brief.superseded-by-resolution-2.md").exists())
        self.assertEqual(state.state(d), state.RESOLVED)

    def test_csv_session_brief_for_a_reopened_tracker_is_deferred(self) -> None:
        # #302 review rounds 6/10: the CSV/zero-id path has no up-front id filter and
        # the marker is a cache — when the live tracker says OPEN, the bundle must
        # NOT stay locked out (marker cleared, closure-era notes aside), but the
        # session's brief was authored from those STALE notes, so it is set aside
        # too and the bundle DEFERS to the next Plan instead of driving Do/Check on
        # context that missed the reopen discussion.
        from types import SimpleNamespace
        d = self._resolved("29")
        self.cfg.planner = LeafConfig(mode="command", interactive=True, argv=["x"])

        def fake_invoke(leaf, cwd, prompt, **kw):
            (d / "brief.md").write_text("- **Slug:** reopened\n- **Defect:** x.\n",
                                        encoding="utf-8")

        gh_open = SimpleNamespace(returncode=0, stdout=json.dumps({"state": "OPEN"}),
                                  stderr="")
        with mock.patch.object(leaves, "_invoke", side_effect=fake_invoke), \
                mock.patch.object(sources.subprocess, "run", return_value=gh_open), \
                mock.patch.object(sources.shutil, "which", return_value="/usr/bin/gh"):
            leaves.do_plan_batch(self.cfg)
        self.assertFalse((d / "brief.md").exists())         # stale-context brief aside
        self.assertTrue((d / "brief.stale-reopen-context.md").exists())
        self.assertFalse((d / "notes.json").exists())       # stale closure notes aside
        self.assertTrue((d / "notes.superseded-by-reopen.json").exists())
        self.assertEqual(state.state(d), state.UNPLANNED)   # deferred to the next Plan

    def test_reopen_probe_scopes_gh_to_the_tracker_repo(self) -> None:
        # #302 review round 7: on the legacy [tracker] path the repo is DERIVED from
        # the tracker URL and always passed via --repo — gh's checkout-default repo
        # could hold a wrong same-numbered issue, and a wrong OPEN there would clear
        # a genuine resolution.
        from types import SimpleNamespace
        self._resolved("31")
        self.assertEqual(sources.tracker_github_repo(self.cfg),
                         (True, "example-org/example-repo"))
        seen: list[list[str]] = []

        def record(cmd, **kw):
            seen.append(list(cmd))
            return SimpleNamespace(returncode=0,
                                   stdout=json.dumps({"state": "OPEN"}), stderr="")

        with mock.patch.object(sources.subprocess, "run", side_effect=record), \
                mock.patch.object(sources.shutil, "which", return_value="/usr/bin/gh"):
            self.assertTrue(sources.tracker_issue_reopened(self.cfg, "31"))
        self.assertIn("--repo", seen[0])
        self.assertIn("example-org/example-repo", seen[0])

    def test_command_tracker_source_keeps_the_configured_tracker_system(self) -> None:
        # #300 review round 15 (filed on PR #308): a `type = "command"` tracker
        # source is just the FETCH mechanism (a GitHub notes_cmd moved into a Plan
        # source) — it must not suppress [tracker].system, or reopened RESOLVED
        # issues stay terminal forever and cleanup skips issue-side reconciliation.
        self.cfg.plan_sources = [{"type": "command", "role": "tracker",
                                  "cmd": "scrape {id}"}]
        self.assertEqual(sources.tracker_github_repo(self.cfg),
                         (True, "example-org/example-repo"))
        # An explicitly different provider still suppresses github.
        self.cfg.plan_sources = [{"type": "gitlab", "role": "tracker"}]
        self.assertEqual(sources.tracker_github_repo(self.cfg), (False, ""))

    def test_repo_less_github_tracker_source_derives_from_the_url(self) -> None:
        # #302 review round 14: a documented `type = "github"` tracker source may
        # omit `repo` (gh's default serves the SEED) — the reopen probe must then
        # fall back to the same [tracker].url derivation as the legacy path, not
        # permanently disable revalidation via the `not repo` guard.
        self.cfg.plan_sources = [{"type": "github", "role": "tracker"}]
        self.assertEqual(sources.tracker_github_repo(self.cfg),
                         (True, "example-org/example-repo"))
        self.cfg.tracker_url = ""                          # nothing to derive from
        self.assertEqual(sources.tracker_github_repo(self.cfg), (True, ""))

    def test_unremovable_brief_fails_closed_without_aborting_the_batch(self) -> None:
        # #302 review round 14: when the set-aside rename fails, the authored brief
        # must not survive (it would shadow the marker as PLANNED next run) — it is
        # deleted instead; and when even that fails, the loud manual-intervention
        # path continues with the REMAINING bundles instead of aborting the session.
        import io
        from contextlib import redirect_stderr
        d = self._resolved("35")
        (d / "brief.md").write_text("- **Slug:** stale\n", encoding="utf-8")
        sibling = self._resolved("36")
        (sibling / "brief.md").write_text("- **Slug:** stale2\n", encoding="utf-8")
        err = io.StringIO()
        with mock.patch.object(leaves.Path, "rename",
                               side_effect=OSError("locked")), \
                mock.patch.object(sources.shutil, "which", return_value=None), \
                redirect_stderr(err):
            leaves._reject_resolved_briefs(
                self.cfg, {"issue_35", "issue_36"})        # no abort mid-loop
        self.assertFalse((d / "brief.md").exists())        # deleted: cannot drive
        self.assertFalse((sibling / "brief.md").exists())  # sibling still processed
        self.assertIn("DELETED", err.getvalue())
        self.assertEqual(state.state(d), state.RESOLVED)   # fail-closed terminal

    def test_reopen_deletion_fallback_still_clears_the_marker(self) -> None:
        # #302 review round 16: when the rename fails but the fallback DELETION
        # empties the brief slot, the deferral has succeeded — the marker is
        # cleared and the fresh thread can re-seed, instead of the reopened issue
        # staying terminal whenever renaming is unavailable.
        from types import SimpleNamespace
        d = self._resolved("38")
        (d / "brief.md").write_text("- **Slug:** stale\n", encoding="utf-8")
        gh_open = SimpleNamespace(returncode=0, stdout=json.dumps({"state": "OPEN"}),
                                  stderr="")

        def delete_only(bp, stem):
            bp.unlink()
            return bp                                      # the deletion-fallback result

        with mock.patch.object(leaves, "_brief_aside", side_effect=delete_only), \
                mock.patch.object(sources.subprocess, "run", return_value=gh_open), \
                mock.patch.object(sources.shutil, "which", return_value="/usr/bin/gh"):
            leaves._reject_resolved_briefs(self.cfg, {"issue_38"})
        self.assertFalse((d / "notes.json").exists())      # marker CLEARED
        self.assertTrue((d / "notes.superseded-by-reopen.json").exists())
        self.assertEqual(state.state(d), state.UNPLANNED)  # deferred, re-seedable

    def test_reopen_keeps_the_marker_when_the_brief_cannot_be_set_aside(self) -> None:
        # #302 review round 15: brief FIRST, marker SECOND — clearing the marker
        # while the stale brief could not be moved would read PLANNED and drive the
        # stale context this deferral exists to keep out.
        from types import SimpleNamespace
        d = self._resolved("37")
        (d / "brief.md").write_text("- **Slug:** stale\n", encoding="utf-8")
        gh_open = SimpleNamespace(returncode=0, stdout=json.dumps({"state": "OPEN"}),
                                  stderr="")
        with mock.patch.object(leaves, "_brief_aside", return_value=None), \
                mock.patch.object(sources.subprocess, "run", return_value=gh_open), \
                mock.patch.object(sources.shutil, "which", return_value="/usr/bin/gh"):
            leaves._reject_resolved_briefs(self.cfg, {"issue_37"})
        self.assertTrue((d / "notes.json").exists())       # marker NOT cleared
        data = json.loads((d / "notes.json").read_text(encoding="utf-8"))
        self.assertIn("resolved", data)                    # terminal state retained

    def test_reopen_probe_refuses_without_a_derivable_repo(self) -> None:
        # No tracker URL to derive the repo from → the probe cannot know WHICH repo's
        # issue to read, so it refuses (conservative False) instead of letting gh
        # fall back to the checkout's default repository.
        self.cfg.tracker_url = ""
        with mock.patch.object(sources.subprocess, "run") as run, \
                mock.patch.object(sources.shutil, "which", return_value="/usr/bin/gh"):
            self.assertFalse(sources.tracker_issue_reopened(self.cfg, "31"))
        run.assert_not_called()

    def test_reopen_probe_tolerates_non_object_gh_json(self) -> None:
        # #302 review round 7: a successful gh (or shim) emitting `null`/`[]` must
        # read as "unknown ⇒ False", never crash the flow with AttributeError.
        from types import SimpleNamespace
        for payload in ("null", "[]", '"OPEN"'):
            fake = SimpleNamespace(returncode=0, stdout=payload, stderr="")
            with mock.patch.object(sources.subprocess, "run", return_value=fake), \
                    mock.patch.object(sources.shutil, "which",
                                      return_value="/usr/bin/gh"):
                self.assertFalse(sources.tracker_issue_reopened(self.cfg, "31"),
                                 payload)

    def test_report_batch_counts_resolved_as_success(self) -> None:
        # #302 review round 11: a bundle ending RESOLVED mid-batch is a successful
        # terminal exactly as on the single-id path — automation must not read the
        # batch flow as failed over it.
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli._report_batch({"1": state.COMPLETE, "2": state.RESOLVED})
        self.assertEqual(rc, 0)
        self.assertIn("2/2 complete (1 resolved in the tracker)", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli._report_batch({"1": state.COMPLETE, "2": state.PLANNED})
        self.assertEqual(rc, 1)                            # genuine non-terminal fails

    def test_failed_marker_clear_is_reported_not_swallowed(self) -> None:
        # #302 review round 11 (filed on PR #308): an un-renamable notes.json must
        # surface as False + a loud line — callers would otherwise announce
        # "cleared — planning it" while the bundle silently stays RESOLVED.
        # The failure is MOCKED, not chmod'd (#302 review round 12): root — the
        # common containerized-CI user — ignores mode bits, so a permissions-based
        # setup silently inverts the test there.
        import io
        from contextlib import redirect_stderr
        d = self._resolved("33")
        err = io.StringIO()
        with mock.patch.object(sources.Path, "rename",
                               side_effect=OSError("read-only bundle dir")), \
                redirect_stderr(err):
            ok = sources.clear_resolved_marker(d)
        self.assertFalse(ok)
        self.assertIn("STAYS", err.getvalue())
        self.assertEqual(state.state(d), state.RESOLVED)   # honestly still resolved

    def test_single_id_flow_exits_zero_on_a_resolved_bundle(self) -> None:
        # #302 review round 3: parity with the multi-id path — a settled tracker item
        # correctly skipped is a successful no-op, not a failed flow.
        import io
        import os
        from contextlib import redirect_stderr, redirect_stdout
        self._resolved("25")
        (self.tmp / "pdca.toml").write_text('[paths]\nbundle_root = "results"\n',
                                            encoding="utf-8")
        cwd = Path.cwd()
        os.chdir(self.tmp)
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                rc = cli.main(["flow", "25"])
        finally:
            os.chdir(cwd)
        self.assertEqual(rc, 0)
        self.assertIn("resolved outside a cycle", err.getvalue())

    def test_single_id_flow_revalidates_a_reopened_tracker(self) -> None:
        # #302 review round 4: the marker is a cache — the seed never refreshes an
        # existing notes.json, so `pdca flow <id>` revalidates against the live tracker
        # and a REOPENED issue clears the marker and proceeds to a real flow.
        import io
        import os
        from contextlib import redirect_stderr, redirect_stdout
        from types import SimpleNamespace
        d = self._resolved("26")
        (self.tmp / "pdca.toml").write_text(
            '[paths]\nbundle_root = "results"\n[tracker]\nsystem = "github"\n'
            'url = "https://github.com/example-org/example-repo/issues"\n',
            encoding="utf-8")
        cwd = Path.cwd()
        os.chdir(self.tmp)
        os.environ["PDCA_NO_INHIBIT"] = "1"   # the mocked which/run must not fake an inhibitor
        gh_open = SimpleNamespace(returncode=0, stdout=json.dumps({"state": "OPEN"}),
                                  stderr="")
        from pdca_harness import sources
        try:
            with mock.patch.object(sources.subprocess, "run", return_value=gh_open), \
                    mock.patch.object(sources.shutil, "which", return_value="/usr/bin/gh"), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                rc = cli.main(["flow", "26", "--no-publish"])
        finally:
            os.chdir(cwd)
            os.environ.pop("PDCA_NO_INHIBIT", None)
        self.assertEqual(rc, 0)
        self.assertIn("OPEN again", err.getvalue())
        # #302 review round 5: the closure-era notes are set ASIDE wholesale — deleting
        # only the key would leave ensure_notes/the tracker-role seed refusing to
        # refresh, and the planner would brief on the pre-closure thread.
        self.assertFalse((d / "notes.json").exists())
        self.assertTrue((d / "notes.superseded-by-reopen.json").exists())
        self.assertTrue((d / "brief.md").exists())         # the flow really planned it

    def test_multi_id_flow_revalidates_reopened_trackers_too(self) -> None:
        # #302 review round 5: `pdca flow 27 28` must apply the same live-state check —
        # the terminal skip would otherwise exclude reopened issues from batch planning
        # forever.
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from types import SimpleNamespace
        from pdca_harness import sources
        self.cfg.tracker_system = "github"
        # A missing templates dir makes the stub planner AUTHOR its fallback brief
        # (a template copy would read as a placeholder → UNPLANNED, outside the point
        # under test here, which is the revalidation re-entry).
        self.cfg.templates_dir = self.tmp / "no-templates"
        d = self._resolved("27")
        gh_open = SimpleNamespace(returncode=0, stdout=json.dumps({"state": "OPEN"}),
                                  stderr="")
        with mock.patch.object(sources.subprocess, "run", return_value=gh_open), \
                mock.patch.object(sources.shutil, "which", return_value="/usr/bin/gh"), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            results = flow.flow_ids(self.cfg, ["27"], plan_missing=True,
                                    do_publish=False, do_act=False, today="2026-07-19")
        self.assertFalse((d / "notes.json").exists())      # stale notes set aside
        self.assertTrue((d / "brief.md").exists())         # re-entered THIS run's plan
        self.assertIn("27", results)
        self.assertNotEqual(results.get("27"), state.RESOLVED)


if __name__ == "__main__":
    unittest.main()
