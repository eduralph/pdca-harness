"""Offline slice for `pdca record` (issue #317) — stdlib unittest, no git/gh/network.

The verb commits terminal-finished result bundles to the instance repo. Proves the
#317 success criteria: (a) selection is exactly the terminal-finished states —
COMPLETE, DISCONTINUED, RESOLVED — excluding UNPLANNED, AWAITING_SIGNOFF and every
in-motion state; (b) the batch is staged and committed as ONE commit with the
configured conventional subject; (c) `[records] mode = "pr"` additionally branches,
pushes and opens one PR for the whole batch (git/gh stubbed, as the publish slice
does); (d) `mode = "off"` — the default — is byte-identical to today, including the
publish path; (e) classification is `state.state` consumed via `state.TERMINAL`,
never a re-enumeration in the new module, and the selection follows the state files.
"""

from __future__ import annotations

import inspect
import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pdca_harness import cli, publish, record, signoff, state
from pdca_harness.config import Config, LeafConfig

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


def _cfg(root: Path, *, mode: str = "off", **records) -> Config:
    """Stub leaves, no configured gates; [records] keys via ``records``."""
    return Config(
        root=root,
        bundle_root=root / "results",
        process_dir=root / "process",
        templates_dir=TEMPLATES,
        default_branch="main",
        tracker_system="github",
        tracker_url="https://example.org/issues",
        issue_id_example="1",
        builder=LeafConfig(mode="stub"),
        reviewer=LeafConfig(mode="stub"),
        planner=LeafConfig(mode="stub", interactive=True),
        signoff=LeafConfig(mode="stub", interactive=True),
        publisher=LeafConfig(mode="stub", interactive=True),
        act=LeafConfig(mode="stub", interactive=True),
        gates_checks=[],
        records_mode=mode,
        **records,
    )


_BRIEF = "- **Slug:** my-fix\n- **Repo + branch target:** example-org/example-repo @ main\n"


def _cycle_bundle(cfg: Config, iid: str, *, outcome: str | None) -> Path:
    """A bundle with a full cycle's artifacts. ``outcome`` None ⇒ AWAITING_SIGNOFF
    (SUMMARY assembled, §9 empty); else the §9 action ("accept" / "discontinue")."""
    d = cfg.bundle(iid)
    d.mkdir(parents=True)
    (d / "brief.md").write_text(_BRIEF, encoding="utf-8")
    (d / "patch.diff").write_text("diff --git a/x b/x\n", encoding="utf-8")
    (d / "check-gates.json").write_text("{}", encoding="utf-8")
    shutil.copyfile(TEMPLATES / "SUMMARY.md.tpl", d / "SUMMARY.md")
    if outcome:
        signoff.record(d / "SUMMARY.md", action=outcome, by="Tester", date="2026-08-02")
    return d


def _resolved_bundle(cfg: Config, iid: str) -> Path:
    """A briefless tracker bundle settled outside a cycle (#302) — RESOLVED."""
    d = cfg.bundle(iid)
    d.mkdir(parents=True)
    (d / "notes.json").write_text(
        '{"resolved": {"github_state": "closed", "state_reason": "completed"}}',
        encoding="utf-8")
    return d


def _ok(cmd, *a, **k):
    return SimpleNamespace(returncode=0, stdout="", stderr="")


class RecordSelection(unittest.TestCase):
    """Criterion (a) + (e): terminal-finished only, via state.state / state.TERMINAL."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp, mode="commit")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_terminal_set_is_the_finished_states_only(self) -> None:
        # The set record consumes is defined ONCE, in state (the module that owns the
        # names), and is exactly the terminal-finished trio — never the halted-for-a-
        # human states, which HALTED also contains.
        self.assertEqual(state.TERMINAL,
                         {state.COMPLETE, state.DISCONTINUED, state.RESOLVED})
        self.assertTrue(state.TERMINAL.isdisjoint(
            {state.UNPLANNED, state.AWAITING_SIGNOFF, state.PLANNED, state.BUILT,
             state.CHECKED, state.ITERATE_DO, state.ITERATE_PLAN}))

    def test_selection_is_exactly_the_terminal_finished_bundles(self) -> None:
        _cycle_bundle(self.cfg, "C", outcome="accept")            # COMPLETE
        _cycle_bundle(self.cfg, "D", outcome="discontinue")       # DISCONTINUED
        _resolved_bundle(self.cfg, "R")                           # RESOLVED
        _cycle_bundle(self.cfg, "A", outcome=None)                # AWAITING_SIGNOFF
        self.cfg.bundle("U").mkdir(parents=True)                  # UNPLANNED (no brief)
        planned = self.cfg.bundle("P")                            # PLANNED (in motion)
        planned.mkdir(parents=True)
        (planned / "brief.md").write_text(_BRIEF, encoding="utf-8")
        built = self.cfg.bundle("B")                              # BUILT (in motion)
        built.mkdir(parents=True)
        (built / "brief.md").write_text(_BRIEF, encoding="utf-8")
        (built / "patch.diff").write_text("diff --git a/x b/x\n", encoding="utf-8")

        picked = {d.name for d in record.select(self.cfg)}
        self.assertEqual(picked, {"issue_C", "issue_D", "issue_R"})

    def test_selection_follows_the_state_files(self) -> None:
        # Criterion (e): the selection changes when the STATE FILES change — the
        # classification is state.state, not a snapshot or a re-implementation.
        d = _cycle_bundle(self.cfg, "A", outcome=None)            # AWAITING_SIGNOFF
        self.assertEqual(record.select(self.cfg), [])
        signoff.record(d / "SUMMARY.md", action="accept", by="T", date="2026-08-02")
        self.assertEqual([b.name for b in record.select(self.cfg)], ["issue_A"])

    def test_no_duplicated_state_enumeration_in_the_module(self) -> None:
        # Criterion (e): the new module CONSUMES state.TERMINAL; it never re-spells
        # the state names in code (the drift an instance script exhibits).
        src = inspect.getsource(record)
        self.assertIn("state.TERMINAL", src)
        for ref in ("state.COMPLETE", "state.DISCONTINUED", "state.RESOLVED",
                    "state.AWAITING_SIGNOFF", "state.UNPLANNED", "state.HALTED",
                    '"COMPLETE"', "'COMPLETE'", '"DISCONTINUED"', "'DISCONTINUED'",
                    '"RESOLVED"', "'RESOLVED'"):
            self.assertNotIn(ref, src)

    def test_explicit_non_terminal_id_is_excluded_loudly(self) -> None:
        # An explicit id never overrides the safety predicate: a bundle in motion is
        # reported and excluded, not frozen into the repo mid-cycle.
        _cycle_bundle(self.cfg, "A", outcome=None)
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(record.select(self.cfg, ["A"]), [])
        self.assertIn("not terminal-finished", err.getvalue())


class RecordCommit(unittest.TestCase):
    """Criteria (b) + (c): one staged batch commit; pr mode adds branch/push/one PR."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_run(self, calls: list):
        def fake(cmd, *a, **k):
            calls.append(list(cmd))
            if cmd[3:5] == ["diff", "--cached"]:      # staged-changes probe: 1 = changes
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            if cmd[:3] == ["gh", "pr", "create"]:
                return SimpleNamespace(returncode=0, stdout="https://x/pr/1\n", stderr="")
            return _ok(cmd)
        return fake

    def test_commit_mode_stages_and_commits_one_batch_commit(self) -> None:
        cfg = _cfg(self.tmp, mode="commit",
                   records_subject="chore(records): record {n} bundle(s) ({ids})")
        _cycle_bundle(cfg, "1", outcome="accept")
        _cycle_bundle(cfg, "2", outcome="discontinue")
        calls: list = []
        with mock.patch.object(record.subprocess, "run", side_effect=self._fake_run(calls)), \
             redirect_stdout(io.StringIO()):
            rc = record.record(cfg, today="2026-08-02")
        self.assertEqual(rc, 0)
        adds = [c for c in calls if c[3] == "add"]
        commits = [c for c in calls if c[3] == "commit"]
        self.assertEqual(len(adds), 1)
        self.assertEqual(len(commits), 1)                       # ONE commit for the batch
        self.assertEqual(commits[0][:3], ["git", "-C", str(self.tmp)])  # the instance repo
        # the configured conventional subject, formatted
        self.assertIn("chore(records): record 2 bundle(s) (1, 2)", commits[0])
        # both bundles staged AND committed (pathspec-scoped)
        for c in adds + commits:
            self.assertIn("results/issue_1", c)
            self.assertIn("results/issue_2", c)
        # commit mode never touches a remote
        self.assertFalse(any(c[3] == "push" for c in calls if c[0] == "git"))
        self.assertFalse(any(c[:2] == ["gh", "pr"] for c in calls))

    def test_nothing_new_to_commit_is_a_quiet_success(self) -> None:
        cfg = _cfg(self.tmp, mode="commit")
        _cycle_bundle(cfg, "1", outcome="accept")

        def fake(cmd, *a, **k):                       # probe says: index clean (rc 0)
            return _ok(cmd)

        buf = io.StringIO()
        with mock.patch.object(record.subprocess, "run", side_effect=fake), \
             redirect_stdout(buf):
            rc = record.record(cfg, today="2026-08-02")
        self.assertEqual(rc, 0)
        self.assertIn("nothing new to commit", buf.getvalue())

    def test_pr_mode_branches_pushes_and_opens_one_pr_for_the_batch(self) -> None:
        cfg = _cfg(self.tmp, mode="pr", records_issue="99",
                   records_branch="records/{date}")
        _cycle_bundle(cfg, "1", outcome="accept")
        _cycle_bundle(cfg, "2", outcome="discontinue")
        calls: list = []
        with mock.patch.object(record.subprocess, "run", side_effect=self._fake_run(calls)), \
             redirect_stdout(io.StringIO()):
            rc = record.record(cfg, today="2026-08-02")
        self.assertEqual(rc, 0)
        self.assertEqual(len([c for c in calls if c[3] == "commit"]), 1)
        branches = [c for c in calls if c[3] == "branch"]
        pushes = [c for c in calls if c[3] == "push"]
        self.assertEqual(len(branches), 1)
        self.assertIn("records/2026-08-02", branches[0])
        self.assertEqual(len(pushes), 1)
        self.assertIn("records/2026-08-02", pushes[0])
        self.assertIn("--force-with-lease", pushes[0])          # re-runnable, like publish
        prs = [c for c in calls if c[:3] == ["gh", "pr", "create"]]
        self.assertEqual(len(prs), 1)                           # ONE PR for the batch
        pr = prs[0]
        self.assertIn("--draft", pr)
        self.assertEqual(pr[pr.index("--base") + 1], "main")
        self.assertEqual(pr[pr.index("--head") + 1], "records/2026-08-02")
        body = pr[pr.index("--body") + 1]
        self.assertIn("issue_1", body)
        self.assertIn("issue_2", body)
        self.assertIn("Fixes #99", body)                        # [records] issue = 99

    def test_pr_mode_issue_ask_headless_records_commit_only_and_reports(self) -> None:
        # The brief's open question, resolved: issue = "ask" with no interactive
        # terminal skips PR mode — the commit still lands, the skip is reported.
        cfg = _cfg(self.tmp, mode="pr", records_issue="ask")
        _cycle_bundle(cfg, "1", outcome="accept")
        calls: list = []
        err = io.StringIO()
        with mock.patch.object(record.subprocess, "run", side_effect=self._fake_run(calls)), \
             redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = record.record(cfg, today="2026-08-02", interactive=False)
        self.assertEqual(rc, 0)
        self.assertEqual(len([c for c in calls if c[3] == "commit"]), 1)
        self.assertFalse(any(c[3] == "push" for c in calls if c[0] == "git"))
        self.assertFalse(any(c[:2] == ["gh", "pr"] for c in calls))
        self.assertIn('issue = "ask"', err.getvalue())


class RecordModeOff(unittest.TestCase):
    """Criterion (d): mode = "off" — the default — is byte-identical to today."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_off_is_the_default_and_runs_nothing(self) -> None:
        cfg = _cfg(self.tmp)                          # no mode given
        self.assertEqual(cfg.records_mode, "off")     # the dataclass default
        _cycle_bundle(cfg, "1", outcome="accept")
        with mock.patch.object(record.subprocess, "run") as run, \
             redirect_stderr(io.StringIO()):
            self.assertEqual(record.record(cfg), 2)   # explicit verb: refuse with a hint
        run.assert_not_called()                       # no git, ever — results/ may be unversioned

    def test_after_publish_is_a_no_op_under_off(self) -> None:
        cfg = _cfg(self.tmp)
        with mock.patch.object(record, "record") as rec:
            record.after_publish(cfg)
        rec.assert_not_called()


class RecordPublishCallIn(unittest.TestCase):
    """The publish call-in: strictly AFTER publish() writes publish.json (#317)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _publish(self, cfg: Config, iid: str):
        cfg.repo_checkouts = {"example-org/example-repo": str(self.tmp / "example-repo")}
        d = _cycle_bundle(cfg, iid, outcome="accept")
        calls: list = []
        seen: dict = {}

        def fake_run(cmd, *a, **k):
            calls.append(list(cmd))
            return SimpleNamespace(returncode=0, stdout="https://x/pr/9\n", stderr="")

        def spy(_cfg):
            seen["publish_json_written"] = (d / "publish.json").exists()

        with mock.patch.object(publish, "_check_repo", return_value=0), \
             mock.patch.object(publish.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(record, "after_publish", side_effect=spy) as hook, \
             redirect_stdout(io.StringIO()):
            rc = publish.publish(cfg, iid, by="T", today="2026-08-02")
        return rc, d, calls, seen, hook

    def test_publish_triggers_recording_only_after_publish_json(self) -> None:
        cfg = _cfg(self.tmp, mode="commit")
        rc, d, _calls, seen, hook = self._publish(cfg, "PUB")
        self.assertEqual(rc, 0)
        hook.assert_called_once()
        self.assertTrue((d / "publish.json").exists())
        self.assertTrue(seen["publish_json_written"])  # the write PRECEDED the call-in

    def test_publish_under_default_off_never_touches_the_instance_repo(self) -> None:
        # Criterion (d) on the publish path: with the default config the call-in
        # no-ops — no git command ever targets the instance root (cfg.root).
        cfg = _cfg(self.tmp)                           # records_mode = "off"
        cfg.repo_checkouts = {"example-org/example-repo": str(self.tmp / "example-repo")}
        d = _cycle_bundle(cfg, "OFF", outcome="accept")
        calls: list = []

        def fake_run(cmd, *a, **k):
            calls.append(list(cmd))
            return SimpleNamespace(returncode=0, stdout="https://x/pr/9\n", stderr="")

        with mock.patch.object(publish, "_check_repo", return_value=0), \
             mock.patch.object(publish.subprocess, "run", side_effect=fake_run), \
             redirect_stdout(io.StringIO()):
            rc = publish.publish(cfg, "OFF", by="T", today="2026-08-02")
        self.assertEqual(rc, 0)
        self.assertTrue((d / "publish.json").exists())  # publish itself is unchanged
        self.assertFalse(any(c[:3] == ["git", "-C", str(self.tmp)] for c in calls))


class RecordCliAndConfig(unittest.TestCase):
    """The cli wiring (`pdca record`) and the [records] config table."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self._cwd = Path.cwd()
        os.chdir(self.tmp)

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_record_verb_is_wired_and_dry_run_plans_only(self) -> None:
        (self.tmp / "pdca.toml").write_text(
            '[paths]\nbundle_root = "results"\n\n[records]\nmode = "commit"\n',
            encoding="utf-8")
        cfg = _cfg(self.tmp, mode="commit")
        _cycle_bundle(cfg, "7", outcome="accept")
        buf = io.StringIO()
        with mock.patch.object(record.subprocess, "run") as run, \
             redirect_stdout(buf):
            rc = cli.main(["record", "--dry-run"])
        self.assertEqual(rc, 0)
        run.assert_not_called()                       # a dry run plans, never executes
        out = buf.getvalue()
        self.assertIn("add", out)
        self.assertIn("commit", out)
        self.assertIn("results/issue_7", out)

    def test_config_parses_records_table_and_fails_closed_on_unknown_mode(self) -> None:
        (self.tmp / "pdca.toml").write_text(
            "[records]\n"
            'mode = "pr"\nbranch = "rec/{date}"\nsubject = "chore: {n}"\nissue = 42\n',
            encoding="utf-8")
        cfg = Config.load(self.tmp)
        self.assertEqual(cfg.records_mode, "pr")
        self.assertEqual(cfg.records_branch, "rec/{date}")
        self.assertEqual(cfg.records_subject, "chore: {n}")
        self.assertEqual(cfg.records_issue, "42")
        (self.tmp / "pdca.toml").write_text('[records]\nmode = "yolo"\n', encoding="utf-8")
        err = io.StringIO()
        with redirect_stderr(err):
            cfg = Config.load(self.tmp)
        self.assertEqual(cfg.records_mode, "off")     # unknown ⇒ fail CLOSED, loudly
        self.assertIn("unknown [records] mode", err.getvalue())


if __name__ == "__main__":
    unittest.main()
