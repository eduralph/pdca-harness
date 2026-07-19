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

from pdca_harness import cli, driver, flow, leaves, state
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
        tracker_url="",
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

        with mock.patch.object(leaves, "_invoke", side_effect=fake_invoke):
            leaves.do_plan_batch(self.cfg)
        self.assertFalse((d / "brief.md").exists())
        self.assertTrue((d / "brief.superseded-by-resolution.md").exists())  # kept, aside
        self.assertEqual(state.state(d), state.RESOLVED)
        # A second offending session gets its own destination (#302 review round 3) —
        # the first rejection artifact is never overwritten.
        with mock.patch.object(leaves, "_invoke", side_effect=fake_invoke):
            leaves.do_plan_batch(self.cfg)
        self.assertTrue((d / "brief.superseded-by-resolution.md").exists())
        self.assertTrue((d / "brief.superseded-by-resolution-2.md").exists())
        self.assertEqual(state.state(d), state.RESOLVED)

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
            '[paths]\nbundle_root = "results"\n[tracker]\nsystem = "github"\n',
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
