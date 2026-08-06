"""Offline slice for `pdca publish` — Check's contribution arm (stdlib unittest).

Drives `publish.publish(dry_run=True)` over a stub **COMPLETE** bundle with a stub
publisher leaf: proves the guard (accepted-only), the contribution-artifact
generation, the upstream-based branch naming from the configured pattern, the
repo→checkout resolution, and that the dry run *plans* the git/gh commands without
pushing. No Claude, no git, no network.
"""

from __future__ import annotations

import io
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pdca_harness import cli, flow, gates, leaves, publish, signoff, state
from pdca_harness.config import Config, LeafConfig

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
_SRC = Path(cli.__file__).resolve().parents[1]          # …/template/src — the tree under test


def _shipped_t4_row_cmd() -> str:
    """The registered `T4-contribution` checker invocation, exactly as shipped (#384):
    the narrow --no-issue mode must reach an instance through the registered row, not
    through hand-editing its config. Read from `pdca.toml.jinja` in the template tree
    or the rendered `pdca.toml` when this suite runs inside a rendered instance (the
    root render suite copies it there); the instance-specific leading cli token is
    dropped so the caller can substitute the tree-under-test's own CLI."""
    root = Path(publish.__file__).resolve().parents[2]
    config = next(p for p in (root / "pdca.toml.jinja", root / "pdca.toml") if p.is_file())
    row = next(line for line in config.read_text(encoding="utf-8").splitlines()
               if '"T4-contribution"' in line)
    cmd = re.search(r'cmd = "([^"]+)"', row).group(1)
    return cmd[cmd.index("contribcheck"):]


def _cfg(root: Path) -> Config:
    """Stub leaves, no configured gates (T4 skipped), generic publish defaults."""
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
        # Hermetic: pin the toy target to a path inside this test's tmp root. Without a
        # mapping, the sibling convention resolves to `<tmp>/../example-repo` — a SHARED
        # /tmp path a stray dir (e.g. an earlier run's leftover with a broken .git) can
        # occupy, turning the deterministic "no checkout → run in place" fallback into a
        # fail-closed WorktreeError (#296) depending on host state.
        repo_checkouts={"example-org/example-repo": str(root / "example-repo")},
    )


def _bundle(cfg: Config, issue_id: str, *, brief_body: str, accepted: bool) -> Path:
    d = cfg.bundle(issue_id)
    d.mkdir(parents=True)
    (d / "brief.md").write_text(brief_body, encoding="utf-8")
    (d / "patch.diff").write_text("diff --git a/x b/x\n", encoding="utf-8")
    (d / "check-gates.json").write_text("{}", encoding="utf-8")
    shutil.copyfile(TEMPLATES / "SUMMARY.md.tpl", d / "SUMMARY.md")
    if accepted:
        signoff.record(d / "SUMMARY.md", action="accept", by="Tester", date="2026-06-05")
    return d


_FIX_BRIEF = (
    "- **Slug:** my-fix\n"
    "- **Repo + branch target:** example-org/example-repo @ main\n"
)

# Stack mode (issue #54): the same brief plus an `Onto branch` naming an existing PR head.
_STACK_BRIEF = _FIX_BRIEF + "- **Onto branch:** origin/feature/x\n"


_PR_42 = {"url": "https://github.com/example-org/example-repo/pull/42", "number": 42,
          "headRefName": "feature/x", "headRepositoryOwner": {"login": "example-org"}}


def _gh_pr_list(cmd: list[str], prs: list[dict]) -> SimpleNamespace:
    """Reproduce `gh pr list --head` filtering faithfully (#58): it matches the **bare**
    headRefName only — the `owner:branch` form is "not supported" and matches nothing."""
    head = cmd[cmd.index("--head") + 1]
    matched = [] if ":" in head else [p for p in prs if p["headRefName"] == head]
    return SimpleNamespace(returncode=0, stdout=json.dumps(matched), stderr="")


class PublishSlice(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_publish_prompt_directs_the_leaf_to_read_summary_section10(self) -> None:
        # #177: the per-invocation publish prompt must point the leaf at SUMMARY.md §10 so it
        # folds reviewer "PR description must include …" Act-candidate notes into the PR body.
        d = _bundle(self.cfg, "P10", brief_body=_FIX_BRIEF, accepted=True)
        prompt = leaves._publish_prompt(d, self.cfg)
        self.assertIn("SUMMARY.md", prompt)
        self.assertIn("§10", prompt)

    def test_publish_prompt_links_summary_and_keeps_trailer_bare(self) -> None:
        # #266/#233: with [tracker].issue_url_pattern set, the prompt tells the leaf to put the
        # clickable link on the Summary `Reported in [#id](url)` line AND keep the closing
        # `Fixes` trailer a bare id (a linked trailer defeats GitHub auto-close). Absent ⇒ none.
        d = _bundle(self.cfg, "266", brief_body=_FIX_BRIEF, accepted=True)   # a numeric ticket id
        self.cfg.issue_url_pattern = "https://tracker/view.php?id={id}"
        prompt = leaves._publish_prompt(d, self.cfg)
        self.assertIn("Reported in [#266](https://tracker/view.php?id=266)", prompt)
        self.assertIn("never a Markdown link", prompt)          # keep the closing trailer bare
        self.cfg.issue_url_pattern = ""
        self.assertNotIn("Reported in [#266]", leaves._publish_prompt(d, self.cfg))

    def test_publish_prompt_omits_link_for_a_slug_or_pending_bundle(self) -> None:
        # #192/#196: a slug bundle (fork issue), a `--no-issue`/id_pending placeholder, or any
        # non-numeric id has no real ticket number, so the pattern would format a broken link —
        # omit the clause even with issue_url_pattern set (the bare-number trailer is gated the
        # same way). Only a real ticket NUMBER links.
        self.cfg.issue_url_pattern = "https://tracker/view.php?id={id}"
        for iid in ("820-build-toolchain-coverage", "PEND"):     # slug, then a pending placeholder
            d = _bundle(self.cfg, iid, brief_body=_FIX_BRIEF, accepted=True)
            prompt = leaves._publish_prompt(d, self.cfg)
            self.assertNotIn("Reported in [#", prompt)                      # no link clause
            self.assertNotIn(f"view.php?id={iid}", prompt)                  # no broken URL
        # …but a real numeric ticket still gets the link.
        num = _bundle(self.cfg, "13865", brief_body=_FIX_BRIEF, accepted=True)
        self.assertIn("https://tracker/view.php?id=13865", leaves._publish_prompt(num, self.cfg))

    def test_dry_run_plans_commands_and_writes_artifacts(self) -> None:
        d = _bundle(self.cfg, "PUB", brief_body=_FIX_BRIEF, accepted=True)
        self.assertEqual(state.state(d), state.COMPLETE)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = publish.publish(self.cfg, "PUB", dry_run=True, by="Tester", today="2026-06-05")
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        # the publisher stub wrote the two contribution (T4) artifacts
        self.assertTrue((d / "commit-msg.txt").exists())
        self.assertTrue((d / "pr-description.md").exists())
        # branch from UPSTREAM/<base> using the default fix/{id}-{slug} pattern
        self.assertIn("checkout -B fix/PUB-my-fix upstream/main", out)
        self.assertIn("gh pr create", out)
        self.assertIn("--draft", out)
        # a dry run pushes nothing and records nothing
        self.assertFalse((d / "publish.json").exists())

    def test_refuses_unaccepted_bundle(self) -> None:
        d = _bundle(self.cfg, "NOPE", brief_body=_FIX_BRIEF, accepted=False)
        self.assertNotEqual(state.state(d), state.COMPLETE)  # AWAITING_SIGNOFF
        self.assertEqual(publish.publish(self.cfg, "NOPE", dry_run=True), 1)

    def test_enhancement_branch_category(self) -> None:
        body = (
            "- **Slug:** add-thing\n"
            "- **Kind:** enhancement (design proposal)\n"
            "- **Repo + branch target:** example-org/example-repo @ main\n"
        )
        _bundle(self.cfg, "FEAT", brief_body=body, accepted=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            publish.publish(self.cfg, "FEAT", dry_run=True)
        self.assertIn("checkout -B enhancement/FEAT-add-thing upstream/main", buf.getvalue())

    def test_skip_if_no_target_is_nonfatal(self) -> None:
        # A COMPLETE bundle whose brief names no target → the flow's tolerant skip.
        _bundle(self.cfg, "NOTGT", brief_body="- **Slug:** x\n", accepted=True)
        self.assertEqual(
            publish.publish(self.cfg, "NOTGT", dry_run=True, skip_if_no_target=True), 0)
        # …but a standalone publish (no skip) treats the missing target as an error.
        self.assertEqual(publish.publish(self.cfg, "NOTGT", dry_run=True), 1)

    def test_empty_patch_is_treated_as_close_disposition(self) -> None:
        """Regression (#95): a 0-byte / whitespace-only patch.diff is a close, not a
        broken contribution. `is_file()` alone let an empty patch past the guard, after
        which `git apply` was a no-op and the commit failed with 'nothing to commit'.

        The #95 shape is an *empty patch.diff present* — which the state machine reads
        as past-Do, so the bundle is COMPLETE and reaches publish (unlike a missing
        patch.diff + close marker, the issue #60 path). Both empty shapes must
        short-circuit to a non-fatal 0 and plan nothing."""
        for iid, content in (("CLOSE0", ""), ("CLOSE1", "\n  \n")):
            d = _bundle(self.cfg, iid, brief_body=_FIX_BRIEF, accepted=True)
            (d / "patch.diff").write_text(content, encoding="utf-8")
            self.assertEqual(state.state(d), state.COMPLETE)  # empty patch ⇒ past-Do
            buf = io.StringIO()
            with redirect_stderr(buf):
                self.assertEqual(publish.publish(self.cfg, iid, dry_run=True), 0)
            self.assertIn("no (non-empty) patch.diff", buf.getvalue())
            # the guard returns before any contribution is planned/recorded
            self.assertFalse((d / "commit-msg.txt").exists())
            self.assertFalse((d / "publish.json").exists())

    def test_commit_stages_patch_added_files(self) -> None:
        """Regression (#23a): the commit must stage patch-ADDED files (the new
        regression test), not only modified-tracked ones — `git apply` + `add --all`
        + `commit -F`, never `commit -aF`."""
        _bundle(self.cfg, "ADD", brief_body=_FIX_BRIEF, accepted=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            publish.publish(self.cfg, "ADD", dry_run=True)
        out = buf.getvalue()
        self.assertIn("add --all", out)        # stages new files (the regression test)
        self.assertNotIn("commit -aF", out)    # never the modified-only commit

    def test_commit_is_signed_off_both_paths(self) -> None:
        # DCO (#81): both the new-PR and stack-mode commits carry `-s`, so the
        # Signed-off-by trailer is present and a DCO-gated host accepts the PR.
        _bundle(self.cfg, "DCO", brief_body=_FIX_BRIEF, accepted=True)
        _bundle(self.cfg, "DCOSTK", brief_body=_STACK_BRIEF, accepted=True)
        for iid in ("DCO", "DCOSTK"):
            buf = io.StringIO()
            with redirect_stdout(buf):
                publish.publish(self.cfg, iid, dry_run=True)
            out = buf.getvalue()
            self.assertIn("commit -s -F", out, f"{iid}: commit not signed off")
            self.assertNotIn("commit -F", out)  # the unsigned form is gone

    def test_base_remote_is_configurable(self) -> None:
        # Own-repo (#83): branch the fix off `origin` (no `upstream` remote needed).
        self.cfg.base_remote = "origin"
        _bundle(self.cfg, "OWN", brief_body=_FIX_BRIEF, accepted=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            publish.publish(self.cfg, "OWN", dry_run=True)
        out = buf.getvalue()
        self.assertIn("fetch origin", out)
        self.assertIn("checkout -B fix/OWN-my-fix origin/main", out)
        self.assertNotIn("upstream", out)  # no upstream remote assumed

    def test_publish_succeeds_with_dirty_target_tree(self) -> None:
        # Own-repo, dirty tree (#83): Do/Check edit the target in place, so publish must
        # stash → publish off a clean checkout → restore, not abort on the dirty tree.
        import subprocess as sp
        repo = self.tmp / "checkout"
        origin = self.tmp / "origin.git"
        sp.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        sp.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
        run = lambda *a: sp.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
        run("config", "user.email", "t@example.com")
        run("config", "user.name", "T")
        run("config", "commit.gpgsign", "false")
        (repo / "file.txt").write_text("base\n", encoding="utf-8")
        run("add", "-A"); run("commit", "-q", "-m", "base")
        run("branch", "-M", "main"); run("push", "-q", "-u", "origin", "main")
        # The builder edits in place + leaves an untracked file (the dirty cycle state).
        (repo / "file.txt").write_text("base\nbuilder edit\n", encoding="utf-8")
        (repo / "untracked.txt").write_text("u\n", encoding="utf-8")

        self.cfg.base_remote = "origin"
        self.cfg.repo_checkouts = {"example-org/example-repo": str(repo)}
        d = _bundle(self.cfg, "DIRTY", brief_body=_FIX_BRIEF, accepted=True)
        (d / "patch.diff").write_text(
            "diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n"
            "@@ -1 +1,2 @@\n base\n+fix line\n", encoding="utf-8")

        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            rc = publish.publish(self.cfg, "DIRTY", open_pr=False, by="T", today="2026-06-05")
        self.assertEqual(rc, 0, buf.getvalue())
        # The operator's dirty edits are restored — edit-in-place survives publish.
        self.assertEqual((repo / "file.txt").read_text(encoding="utf-8"), "base\nbuilder edit\n")
        self.assertTrue((repo / "untracked.txt").exists())
        cur = sp.run(["git", "-C", str(repo), "branch", "--show-current"],
                     capture_output=True, text=True).stdout.strip()
        self.assertEqual(cur, "main")  # back on the original branch
        # The fix branch was pushed to origin.
        refs = sp.run(["git", "-C", str(repo), "ls-remote", "--heads", "origin"],
                      capture_output=True, text=True).stdout
        self.assertIn("fix/DIRTY-my-fix", refs)

    def test_republish_force_updates_existing_pr_branch(self) -> None:
        # iterate-do (#108): re-publishing a rebuilt bundle commits a FRESH branch off the
        # current base and pushes it to the EXISTING PR branch — not a fast-forward of the
        # prior attempt. A plain push is rejected (the re-Done bundle never publishes);
        # publish must force-with-lease so the branch is updated to the rebuilt commit.
        import subprocess as sp
        repo = self.tmp / "checkout"
        origin = self.tmp / "origin.git"
        sp.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        sp.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
        run = lambda *a: sp.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
        run("config", "user.email", "t@example.com")
        run("config", "user.name", "T")
        run("config", "commit.gpgsign", "false")
        (repo / "file.txt").write_text("base\n", encoding="utf-8")
        run("add", "-A"); run("commit", "-q", "-m", "base")
        run("branch", "-M", "main"); run("push", "-q", "-u", "origin", "main")

        self.cfg.base_remote = "origin"
        self.cfg.repo_checkouts = {"example-org/example-repo": str(repo)}
        d = _bundle(self.cfg, "REDO", brief_body=_FIX_BRIEF, accepted=True)

        def publish_fix(line: str) -> str:
            # A distinct fix off the same base → a sibling (non-ff) commit on re-publish.
            (d / "patch.diff").write_text(
                "diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n"
                f"@@ -1 +1,2 @@\n base\n+{line}\n", encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(buf):
                rc = publish.publish(self.cfg, "REDO", open_pr=False, by="T", today="2026-06-05")
            self.assertEqual(rc, 0, buf.getvalue())
            return sp.run(["git", "-C", str(repo), "ls-remote", "origin", "fix/REDO-my-fix"],
                          capture_output=True, text=True).stdout.split()[0]

        tip1 = publish_fix("first fix")
        tip2 = publish_fix("second fix")   # was rejected (non-fast-forward) before #108
        self.assertTrue(tip1 and tip2)
        self.assertNotEqual(tip1, tip2)    # the PR branch was force-updated to the rebuild

    def test_pr_head_is_fork_owner_qualified(self) -> None:
        """Regression (#23b): a fork-based PR's --head must be OWNER:BRANCH, else gh
        resolves the branch against the base repo and fails 'Head ref must be a
        branch'. (No real checkout here, so the owner falls back to the base owner —
        the assertion is on the OWNER:BRANCH *shape*, not the value.)"""
        _bundle(self.cfg, "HEAD", brief_body=_FIX_BRIEF, accepted=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            publish.publish(self.cfg, "HEAD", dry_run=True)
        out = buf.getvalue()
        self.assertRegex(out, r"--head \S+:fix/HEAD-my-fix\b")   # owner-qualified
        self.assertNotIn("--head fix/HEAD-my-fix", out)          # never a bare branch

    def test_fork_owner_parses_origin_url(self) -> None:
        """`_fork_owner` extracts the GitHub owner from origin (ssh + https forms),
        and is empty when the URL is undetectable."""
        for url, owner in (
            ("git@github.com:example-user/repo.git", "example-user"),
            ("https://github.com/example-user/repo.git", "example-user"),
            ("https://github.com/example-user/repo", "example-user"),
        ):
            with mock.patch.object(publish.subprocess, "run",
                                   return_value=SimpleNamespace(stdout=url + "\n", returncode=0)):
                self.assertEqual(publish._fork_owner(Path("/x")), owner)
        with mock.patch.object(publish.subprocess, "run",
                               return_value=SimpleNamespace(stdout="", returncode=0)):
            self.assertEqual(publish._fork_owner(Path("/x")), "")

    def test_open_pr_failure_exits_nonzero(self) -> None:
        """Regression (#23 note): when `gh pr create` fails after the branch is
        pushed, publish must NOT exit 0 with an empty pr_url — it returns non-zero
        (a partial run) and records the pushed branch with an empty pr_url."""
        d = _bundle(self.cfg, "PUBFAIL", brief_body=_FIX_BRIEF, accepted=True)

        def fake_run(cmd, *a, **k):  # every git step succeeds; `gh pr create` fails
            if cmd[:3] == ["gh", "pr", "create"]:
                return SimpleNamespace(returncode=1, stdout="", stderr="boom")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        buf = io.StringIO()
        with mock.patch.object(publish, "_check_repo", return_value=0), \
             mock.patch.object(publish.subprocess, "run", side_effect=fake_run), \
             redirect_stdout(buf):
            rc = publish.publish(self.cfg, "PUBFAIL", by="Tester", today="2026-06-05")
        self.assertEqual(rc, 1)                                   # partial run, not "done"
        pj = json.loads((d / "publish.json").read_text(encoding="utf-8"))
        self.assertEqual(pj["mode"], "new-pr")                   # default contribution shape
        self.assertEqual(pj["pr_url"], "")                       # recorded, but empty
        self.assertEqual(pj["branch"], "fix/PUBFAIL-my-fix")     # branch was pushed

    def _publish(self, iid: str, **kw) -> tuple[int, str]:
        """Dry-run publish; return (rc, stderr text)."""
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            rc = publish.publish(self.cfg, iid, dry_run=True, **kw)
        return rc, err.getvalue()

    def test_no_issue_no_longer_relaxes_a_failing_t4_to_a_flag(self) -> None:
        """Issue #384: `--no-issue` states one fact — the tracker id doesn't exist yet —
        so it may relax exactly the tracker-id requirement (the checker's own narrow
        mode, exercised below). A T4 failure for ANY other reason blocks the push in
        either mode; the old branch that waved the WHOLE failed gate through as a
        printed FLAGGED notice is gone."""
        self.cfg.gates_checks = [{"id": "T4-x", "tier": "T4", "cmd": "exit 1", "scope": "bundle"}]
        _bundle(self.cfg, "PEND", brief_body=_FIX_BRIEF, accepted=True)
        self.assertEqual(self._publish("PEND")[0], 1)          # default: aborts, as before
        rc, err = self._publish("PEND", pending_id=True)
        self.assertEqual(rc, 1, "a failing T4 must abort a --no-issue publish too")
        self.assertNotIn("FLAGGED", err)                        # the amnesty branch is gone
        self.assertIn("FAILED", err)

    def test_no_issue_mode_reaches_the_t4_gate_as_pdca_pending_id(self) -> None:
        """#384: the gate is TOLD which mode it runs in — $PDCA_PENDING_ID is exported
        under --no-issue and absent otherwise, derived from the flag on each run."""
        _bundle(self.cfg, "PEND2", brief_body=_FIX_BRIEF, accepted=True)
        self.cfg.gates_checks = [{"id": "T4-x", "tier": "T4", "scope": "bundle",
                                  "cmd": 'test -n "$PDCA_PENDING_ID"'}]
        rc, err = self._publish("PEND2", pending_id=True)
        self.assertEqual((rc, "FLAGGED" in err), (0, False))    # exported → gate passes
        self.assertEqual(self._publish("PEND2")[0], 1)          # default mode: not exported

    def test_ambient_pdca_pending_id_is_scrubbed_not_honoured(self) -> None:
        """#384: the mode comes from THIS run's flag, never the ambient environment — a
        stray export from an earlier --no-issue run must not relax a ticketed publish
        (mirrors gates._run_one deriving per-run env from driver state)."""
        _bundle(self.cfg, "PEND3", brief_body=_FIX_BRIEF, accepted=True)
        self.cfg.gates_checks = [{"id": "T4-x", "tier": "T4", "scope": "bundle",
                                  "cmd": 'test -z "$PDCA_PENDING_ID"'}]
        with mock.patch.dict(os.environ, {"PDCA_PENDING_ID": "1"}):
            self.assertEqual(self._publish("PEND3")[0], 0)      # inherited value scrubbed

    def test_shipped_row_gives_no_issue_only_the_tracker_id_amnesty(self) -> None:
        """#384 end to end through the SHIPPED `T4-contribution` row and the PRODUCTION
        checker: publish's derived $PDCA_PENDING_ID reaches `contribcheck` (which
        honours it as `--no-issue`) through the registered cmd UNCHANGED — rewriting
        that row line in place breaks `copier update` for instances that appended a row
        beside it (tests/test_update_compat.py). A bundle whose only T4 problem is the
        absent tracker id proceeds under --no-issue, the id-known mode still enforces
        the id, and any OTHER defect is refused in both modes."""
        row_cmd = _shipped_t4_row_cmd()
        self.assertTrue(row_cmd.startswith("contribcheck"))
        cli_cmd = (f"PYTHONPATH={shlex.quote(str(_SRC))} {shlex.quote(sys.executable)} "
                   "-m pdca_harness.cli")
        self.cfg.gates_checks = [{"id": "T4-contribution", "tier": "T4", "scope": "bundle",
                                  "cmd": f"{cli_cmd} {row_cmd}"}]
        (self.tmp / "pdca.toml").write_text("", encoding="utf-8")  # Config.load for the CLI
        d = _bundle(self.cfg, "384", brief_body=_FIX_BRIEF, accepted=True)
        (d / "pr-description.md").write_text(
            "## Summary\n**User impact:** x.\n\n## Root cause\nx.\n", encoding="utf-8")
        (d / "commit-msg.txt").write_text("Fix\n\nNo trailer yet.\n", encoding="utf-8")
        # Only defect = the not-yet-assigned tracker id: --no-issue proceeds…
        rc, err = self._publish("384", pending_id=True)
        self.assertEqual((rc, "FLAGGED" in err), (0, False))
        # …while the default (id-known) mode still enforces the id.
        self.assertEqual(self._publish("384")[0], 1)
        # A NON-tracker-id defect (no user-impact opener) is refused even under --no-issue.
        (d / "pr-description.md").write_text("## Summary\nno opener here.\n", encoding="utf-8")
        rc, err = self._publish("384", pending_id=True)
        self.assertEqual(rc, 1, "a malformed PR body must not be waved through as 'pending id'")
        self.assertIn("User impact", err)

    def _stacked_dry_run(self, *, base_remote: str) -> str:
        # A `Stacks on:` dependent whose parent has a published branch — dry-run publish.
        self.cfg.base_remote = base_remote
        parent = self.cfg.bundle("PARENT")
        parent.mkdir(parents=True)
        (parent / "publish.json").write_text(json.dumps({"branch": "fix/PARENT-my-fix"}),
                                             encoding="utf-8")
        _bundle(self.cfg, "DEP", brief_body=_FIX_BRIEF + "- **Stacks on:** PARENT\n",
                accepted=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = publish.publish(self.cfg, "DEP", dry_run=True, by="T", today="2026-06-05")
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_fork_stacked_pr_targets_upstream_base_with_cumulative_diff(self) -> None:
        # #185: a fork's parent/integration branch lives on origin (the fork) and can't be a
        # `gh --base` (which must be an UPSTREAM branch). So a fork stacked PR cuts its branch
        # off the parent (carrying the cumulative diff) but opens against the upstream base.
        out = self._stacked_dry_run(base_remote="upstream")
        self.assertIn("checkout -B fix/DEP-my-fix origin/fix/PARENT-my-fix", out)  # off parent
        self.assertIn("--base main", out)                          # PR base = upstream base, not the fork branch
        self.assertNotIn("--base fix/PARENT-my-fix", out)          # NOT the fork integration/parent branch
        self.assertIn("cumulative diff", out)

    def test_own_repo_stacked_pr_chains_onto_the_parent_branch(self) -> None:
        # Own-repo (base on origin): the parent/integration branch IS an upstream branch, so a
        # clean, increment-only stacked PR `--base`s onto it (#123 / #185).
        out = self._stacked_dry_run(base_remote="origin")
        self.assertIn("checkout -B fix/DEP-my-fix origin/fix/PARENT-my-fix", out)  # off parent
        self.assertIn("--base fix/PARENT-my-fix", out)             # PR base = parent branch
        self.assertNotIn("cumulative diff", out)

    def test_stacked_pr_without_published_parent_errors(self) -> None:
        # The dependent can't stack until its parent has published a branch — a standalone
        # publish before that is a loud error (the flow schedules so this can't happen).
        self.cfg.bundle("PARENT2").mkdir(parents=True)   # no publish.json
        _bundle(self.cfg, "DEP2", brief_body=_FIX_BRIEF + "- **Stacks on:** PARENT2\n",
                accepted=True)
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = publish.publish(self.cfg, "DEP2", dry_run=True, by="T", today="2026-06-05")
        self.assertEqual(rc, 1)
        self.assertIn("no published branch yet", buf.getvalue())

    def test_resolve_target_tolerates_backticks_and_prose(self) -> None:
        """Regression (#25): the brief target field is commonly written with markdown
        backticks and/or trailing prose; _resolve_target must isolate owner/repo and a
        clean base ref, not leak backticks or sentence text into the checkout/base."""
        d = self.cfg.bundle("TGT")
        d.mkdir(parents=True)
        (d / "brief.md").write_text(
            "- **Slug:** my-fix\n"
            "- **Repo + branch target:** `example-org/example-repo` @ `main`. "
            "Forward-merged later.\n", encoding="utf-8")
        self.assertEqual(publish._resolve_target(d),
                         ("example-org/example-repo", "main", "my-fix"))

        # a base ref legitimately containing a slash survives backtick stripping
        (d / "brief.md").write_text(
            "- **Slug:** my-fix\n"
            "- **Repo + branch target:** `addons-source` @ `maintenance/gramps60`\n",
            encoding="utf-8")
        self.assertEqual(publish._resolve_target(d),
                         ("addons-source", "maintenance/gramps60", "my-fix"))

    def test_resolve_target_parenthetical_base_takes_the_named_base(self) -> None:
        """#235: a base written `main (feature branch \\`feat/x\\`)` names the base `main`;
        the backticked span is a parenthetical aside about a *different* branch. Taking it
        resolved a nonexistent ref → worktree isolation silently ran in the primary checkout.
        `_clean_ref` honors a backtick span only when it STARTS the field, else the 1st token."""
        d = self.cfg.bundle("PAR")
        d.mkdir(parents=True)
        (d / "brief.md").write_text(
            "- **Slug:** m4\n"
            "- **Repo + branch target:** example-org/example-repo @ "
            "main (feature branch `feat/m4-production-metadata-backend`)\n", encoding="utf-8")
        self.assertEqual(publish._resolve_target(d),
                         ("example-org/example-repo", "main", "m4"))

    def test_resolve_target_backtick_in_trailing_aside_does_not_hijack_base(self) -> None:
        """#262 (downstream bundle #454): the mirror image of #235 — the base is the bare
        first token and the backtick span sits in a trailing prose aside naming a *different*
        branch. Taking the span would resolve `main`, so C4-verify validates the patch
        against the wrong base (false "patch does not apply — stale") and publish would open
        the slice PR against the wrong branch. Fixed by #235's `re.match` anchor; this pins
        the direction #235's own case did not exercise."""
        d = self.cfg.bundle("ASIDE")
        d.mkdir(parents=True)
        (d / "brief.md").write_text(
            "- **Slug:** m4\n"
            "- **Repo + branch target:** example-org/example-repo @ "
            "feat/m4-production-metadata-backend   (stacks here, not on `main`)\n",
            encoding="utf-8")
        self.assertEqual(
            publish._resolve_target(d),
            ("example-org/example-repo", "feat/m4-production-metadata-backend", "m4"))

    def test_checkout_path_map_and_sibling_fallback(self) -> None:
        # sibling fallback: <root>/../<repo-last-segment>
        self.assertEqual(publish._checkout_path(self.cfg, "org/foo"),
                         (self.cfg.root.parent / "foo").resolve())
        # configured map wins; a relative path resolves against the project root
        self.cfg.repo_checkouts = {"org/foo": "../custom-foo"}
        self.assertEqual(publish._checkout_path(self.cfg, "org/foo"),
                         (self.cfg.root / "../custom-foo").resolve())

    # --- tracker refs: bare closing trailer (auto-close, #233) + deterministic link (#238) ---
    _URL = "https://tracker/view.php?id={id}"

    def test_normalize_leaves_a_bare_fixes_untouched(self) -> None:
        # The correct form: a bare `Fixes #266` must survive — GitHub auto-closes only on a
        # bare id after the keyword. No URL pattern ⇒ no link work.
        d = _bundle(self.cfg, "266", brief_body=_FIX_BRIEF, accepted=True)
        (d / "pr-description.md").write_text(
            "## Summary\n**User impact:** users saw X.\n\nFixes #266\n", encoding="utf-8")
        publish._normalize_tracker_refs(self.cfg, d, "266")
        self.assertIn("\nFixes #266\n", (d / "pr-description.md").read_text(encoding="utf-8"))

    def test_normalize_strips_a_linked_fixes(self) -> None:
        # #233: a model that wrote `Fixes [#266](url)` would silently defeat auto-close; the
        # closing trailer is bared back to `Fixes #266`.
        d = _bundle(self.cfg, "266", brief_body=_FIX_BRIEF, accepted=True)
        (d / "pr-description.md").write_text(
            "Fixes [#266](https://tracker/view.php?id=266)\n", encoding="utf-8")
        publish._normalize_tracker_refs(self.cfg, d, "266")   # no pattern set ⇒ only bare
        self.assertEqual((d / "pr-description.md").read_text(encoding="utf-8"), "Fixes #266\n")

    def test_normalize_noop_for_nonnumeric_id(self) -> None:
        d = _bundle(self.cfg, "820-build", brief_body=_FIX_BRIEF, accepted=True)
        (d / "pr-description.md").write_text("Fixes #ABC\n", encoding="utf-8")
        publish._normalize_tracker_refs(self.cfg, d, "820-build")
        self.assertEqual((d / "pr-description.md").read_text(encoding="utf-8"), "Fixes #ABC\n")

    def test_normalize_inserts_deterministic_summary_link_when_absent(self) -> None:
        # #238 review: pattern set but the body has NO clickable ref (a weak/omitting model,
        # like the stub) → publish must INSERT a `Reported in [#id](url)` line off the trailer,
        # not leave the PR with no tracker URL. Trailer stays bare.
        self.cfg.issue_url_pattern = self._URL
        d = _bundle(self.cfg, "266", brief_body=_FIX_BRIEF, accepted=True)
        (d / "pr-description.md").write_text(
            "## Summary\n**User impact:** X.\n\none-line change.\n\n"
            "## What to look at\nY.\n\nFixes #266\n", encoding="utf-8")
        publish._normalize_tracker_refs(self.cfg, d, "266")
        body = (d / "pr-description.md").read_text(encoding="utf-8")
        self.assertIn("Reported in [#266](https://tracker/view.php?id=266).", body)  # inserted
        self.assertLess(body.index("Reported in"), body.index("## What to look at"))  # in Summary
        self.assertIn("\nFixes #266\n", body)                                         # trailer bare
        self.assertNotIn("Fixes [#266]", body)

    def test_normalize_links_an_existing_bare_reference(self) -> None:
        # A bare `#id` already in prose is linked in place (no separate insertion), trailer bare.
        self.cfg.issue_url_pattern = self._URL
        d = _bundle(self.cfg, "266", brief_body=_FIX_BRIEF, accepted=True)
        (d / "pr-description.md").write_text(
            "## Summary\nX. Reported in #266.\n\nFixes #266\n", encoding="utf-8")
        publish._normalize_tracker_refs(self.cfg, d, "266")
        body = (d / "pr-description.md").read_text(encoding="utf-8")
        self.assertIn("Reported in [#266](https://tracker/view.php?id=266).", body)
        self.assertEqual(body.count("Reported in"), 1)        # no duplicate line inserted
        self.assertIn("\nFixes #266\n", body)

    def test_normalize_keeps_an_existing_summary_link_and_bares_trailer(self) -> None:
        # An already-clickable Summary line is kept as-is (idempotent, no duplicate) while the
        # linked trailer is bared.
        self.cfg.issue_url_pattern = self._URL
        d = _bundle(self.cfg, "266", brief_body=_FIX_BRIEF, accepted=True)
        (d / "pr-description.md").write_text(
            "## Summary\nReported in [#266](https://tracker/view.php?id=266).\n\n"
            "Fixes [#266](https://tracker/view.php?id=266)\n", encoding="utf-8")
        publish._normalize_tracker_refs(self.cfg, d, "266")
        body = (d / "pr-description.md").read_text(encoding="utf-8")
        self.assertEqual(body.count("Reported in [#266]"), 1)   # kept, not duplicated
        self.assertIn("\nFixes #266\n", body)                   # trailer bared
        self.assertNotIn("Fixes [#266]", body)

    def test_publish_bares_trailer_and_guarantees_link_end_to_end(self) -> None:
        # The call site: a real publish run (dry) with a URL pattern set leaves the `Fixes`
        # trailer BARE (auto-close) AND guarantees a clickable link off it (the stub body
        # writes no Summary link), so click-through never depends on the model.
        self.cfg.issue_url_pattern = "https://mantis.example.com/view.php?id={id}"
        d = _bundle(self.cfg, "13865", brief_body=_FIX_BRIEF, accepted=True)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(publish.publish(self.cfg, "13865", dry_run=True), 0)
        body = (d / "pr-description.md").read_text(encoding="utf-8")
        self.assertIn("Fixes #13865", body)                  # bare closing keyword → auto-closes
        self.assertNotIn("Fixes [#13865]", body)             # never a linked closing trailer
        self.assertIn("[#13865](https://mantis.example.com/view.php?id=13865)", body)  # link present

    # --- stack mode (issue #54): commit onto an existing PR branch ---

    def test_stack_dry_run_plans_commit_onto_pr_branch(self) -> None:
        d = _bundle(self.cfg, "STK", brief_body=_STACK_BRIEF, accepted=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = publish.publish(self.cfg, "STK", dry_run=True, by="Tester", today="2026-06-05")
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("checkout -B feature/x origin/feature/x", out)
        self.assertIn("push origin HEAD:feature/x", out)
        self.assertIn("gh pr list", out)       # resolves the existing PR …
        self.assertNotIn("gh pr create", out)  # … never opens a new one
        self.assertFalse((d / "publish.json").exists())  # dry run records nothing

    def test_stack_real_run_records_existing_pr(self) -> None:
        d = _bundle(self.cfg, "STK2", brief_body=_STACK_BRIEF, accepted=True)

        def fake_run(cmd, *a, **k):  # gh-faithful PR lookup; every git step ok
            if cmd[:3] == ["gh", "pr", "list"]:
                return _gh_pr_list(cmd, [_PR_42])
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(publish, "_check_repo", return_value=0), \
             mock.patch.object(publish.subprocess, "run", side_effect=fake_run), \
             redirect_stdout(io.StringIO()):
            rc = publish.publish(self.cfg, "STK2", by="Tester", today="2026-06-05")
        self.assertEqual(rc, 0)  # regression #58: --head must be the bare branch to match
        pj = json.loads((d / "publish.json").read_text(encoding="utf-8"))
        self.assertEqual(pj["mode"], "stacked")
        self.assertEqual(pj["branch"], "feature/x")
        self.assertEqual(pj["base"], "origin/feature/x")
        self.assertEqual(pj["pr_url"], "https://github.com/example-org/example-repo/pull/42")

    def test_stack_branch_drift_aborts_without_push(self) -> None:
        # The PR exists, but the patch no longer applies to the (advanced) branch:
        # `git apply --check` fails → publish aborts BEFORE committing or pushing.
        d = _bundle(self.cfg, "STK3", brief_body=_STACK_BRIEF, accepted=True)
        calls: list[list[str]] = []

        def fake_run(cmd, *a, **k):
            calls.append(cmd)
            if cmd[:3] == ["gh", "pr", "list"]:
                return _gh_pr_list(cmd, [_PR_42])
            if cmd[3:5] == ["apply", "--check"]:
                return SimpleNamespace(returncode=1, stdout="", stderr="does not apply")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        err = io.StringIO()
        with mock.patch.object(publish, "_check_repo", return_value=0), \
             mock.patch.object(publish.subprocess, "run", side_effect=fake_run), \
             redirect_stderr(err), redirect_stdout(io.StringIO()):
            rc = publish.publish(self.cfg, "STK3", by="Tester", today="2026-06-05")
        self.assertEqual(rc, 1)
        self.assertFalse(any("push" in c for c in calls))   # never pushed
        self.assertFalse((d / "publish.json").exists())
        self.assertIn("no longer applies", err.getvalue())

    def test_stack_no_open_pr_refuses_to_push(self) -> None:
        # No open PR with that head → refuse to push a commit to a branch with no PR.
        d = _bundle(self.cfg, "STK4", brief_body=_STACK_BRIEF, accepted=True)
        calls: list[list[str]] = []

        def fake_run(cmd, *a, **k):
            calls.append(cmd)
            if cmd[:3] == ["gh", "pr", "list"]:
                return _gh_pr_list(cmd, [])   # nothing open for this head
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        err = io.StringIO()
        with mock.patch.object(publish, "_check_repo", return_value=0), \
             mock.patch.object(publish.subprocess, "run", side_effect=fake_run), \
             redirect_stderr(err), redirect_stdout(io.StringIO()):
            rc = publish.publish(self.cfg, "STK4", by="Tester", today="2026-06-05")
        self.assertEqual(rc, 1)
        self.assertFalse(any("push" in c for c in calls))   # never pushed
        self.assertFalse((d / "publish.json").exists())
        self.assertIn("no open PR", err.getvalue())

    def test_stack_disambiguates_pr_by_fork_owner(self) -> None:
        # gh's bare --head can return same-named branches across forks; only OUR fork's
        # PR (headRepositoryOwner.login == owner) may match — never a stranger's branch.
        d = _bundle(self.cfg, "STK5", brief_body=_STACK_BRIEF, accepted=True)
        other_fork = {"url": "https://github.com/someone-else/example-repo/pull/99",
                      "number": 99, "headRefName": "feature/x",
                      "headRepositoryOwner": {"login": "someone-else"}}
        calls: list[list[str]] = []

        def fake_run(cmd, *a, **k):
            calls.append(cmd)
            if cmd[:3] == ["gh", "pr", "list"]:        # both forks share the branch name
                return _gh_pr_list(cmd, [other_fork, _PR_42])
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(publish, "_check_repo", return_value=0), \
             mock.patch.object(publish.subprocess, "run", side_effect=fake_run), \
             redirect_stdout(io.StringIO()):
            rc = publish.publish(self.cfg, "STK5", by="Tester", today="2026-06-05")
        self.assertEqual(rc, 0)
        pj = json.loads((d / "publish.json").read_text(encoding="utf-8"))
        self.assertEqual(pj["pr_url"], _PR_42["url"])   # our fork's PR, not someone-else's

    def test_stack_exposes_pdca_base_to_bundle_gate(self) -> None:
        # The driver single-sources the test base from the same brief field publish reads:
        # a bundle-scoped gate sees $PDCA_BASE = <remote>/<branch> when Onto branch is set.
        echo_gate = [{"id": "C4", "tier": "C4", "label": "verify", "scope": "bundle",
                      "gating": True, "cmd": "echo BASE=$PDCA_BASE"}]
        d = _bundle(self.cfg, "STK6", brief_body=_STACK_BRIEF, accepted=True)
        self.cfg.gates_checks = echo_gate
        row = next(r for r in gates.run_gates(d, self.cfg)["rows"] if r["element"] == "C4")
        self.assertIn("BASE=origin/feature/x", row["path_line"])
        # absent field ⇒ PDCA_BASE unset
        d2 = _bundle(self.cfg, "STK6B", brief_body=_FIX_BRIEF, accepted=True)
        self.cfg.gates_checks = echo_gate
        row2 = next(r for r in gates.run_gates(d2, self.cfg)["rows"] if r["element"] == "C4")
        self.assertEqual(row2["path_line"].strip(), "BASE=")


class DraftTexts(unittest.TestCase):
    """#295: the text-drafting pre-pass — every accepted bundle's publishing texts are
    generated and T4-gated BEFORE any git/gh mechanics, per bundle, no mechanics here."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_accepted_bundle_drafts_both_artifacts_idempotently(self) -> None:
        d = _bundle(self.cfg, "D1", brief_body=_FIX_BRIEF, accepted=True)
        with redirect_stderr(io.StringIO()):
            self.assertTrue(publish.draft_texts(self.cfg, d))
        first = {n: (d / n).read_text(encoding="utf-8")
                 for n in ("commit-msg.txt", "pr-description.md")}
        self.assertTrue(all(first.values()))
        with redirect_stderr(io.StringIO()):
            self.assertTrue(publish.draft_texts(self.cfg, d))   # second call: only-if-missing
        for n, text in first.items():
            self.assertEqual((d / n).read_text(encoding="utf-8"), text)

    def test_nothing_to_draft_cases_are_ready_without_artifacts(self) -> None:
        # No target → non-contributing cycle; close/no-fix (whitespace patch) → nothing
        # to contribute; not COMPLETE → publish()'s guard will speak. All True, and the
        # publisher leaf is never invoked.
        no_target = _bundle(self.cfg, "D2", brief_body="- **Slug:** s\n", accepted=True)
        close = _bundle(self.cfg, "D3", brief_body=_FIX_BRIEF, accepted=True)
        (close / "patch.diff").write_text("   \n", encoding="utf-8")
        unaccepted = _bundle(self.cfg, "D4", brief_body=_FIX_BRIEF, accepted=False)
        with mock.patch.object(publish.leaves, "run_publish") as run_pub:
            for d in (no_target, close, unaccepted):
                self.assertTrue(publish.draft_texts(self.cfg, d), d.name)
                self.assertFalse((d / "commit-msg.txt").exists(), d.name)
        run_pub.assert_not_called()

    def test_t4_failure_blocks_readiness(self) -> None:
        self.cfg.gates_checks = [{"id": "T4-x", "tier": "T4", "cmd": "exit 1", "scope": "bundle"}]
        d = _bundle(self.cfg, "D5", brief_body=_FIX_BRIEF, accepted=True)
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertFalse(publish.draft_texts(self.cfg, d))
        self.assertIn("T4 contribution gate FAILED", err.getvalue())

    def test_prevalidated_mechanics_never_rerun_t4(self) -> None:
        # #295 review: a transient/stateful T4 that passed the pre-pass but failed the
        # in-publish re-run would recreate the half-published wave. With
        # texts_prevalidated the mechanics phase runs NO second T4; the direct path
        # (no flag) still gates.
        self.cfg.gates_checks = [{"id": "T4-x", "tier": "T4", "cmd": "exit 1", "scope": "bundle"}]
        d = _bundle(self.cfg, "D6", brief_body=_FIX_BRIEF, accepted=True)
        (d / "commit-msg.txt").write_text("feat: x\n\nFixes #6\n", encoding="utf-8")
        (d / "pr-description.md").write_text("body\n", encoding="utf-8")
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.assertEqual(publish.publish(self.cfg, "D6", dry_run=True), 1)  # gated
            self.assertEqual(publish.publish(self.cfg, "D6", dry_run=True,
                                             texts_prevalidated=True), 0)       # mechanics-only

    def test_prevalidated_still_refuses_vanished_texts(self) -> None:
        # Defensive: prevalidation promises the texts exist; if one vanished between the
        # pre-pass and mechanics, refuse rather than push without a commit message.
        _bundle(self.cfg, "D7", brief_body=_FIX_BRIEF, accepted=True)
        with mock.patch.object(publish.leaves, "run_publish") as run_pub, \
                redirect_stderr(io.StringIO()):
            rc = publish.publish(self.cfg, "D7", dry_run=True, texts_prevalidated=True)
        self.assertEqual(rc, 1)
        run_pub.assert_not_called()                        # mechanics-only: no drafting

    def test_draft_only_phase_skips_t4(self) -> None:
        # #295 review round 2: run_t4=False is the flow's draft-only phase — validation
        # is deferred until every publisher leaf has finished.
        self.cfg.gates_checks = [{"id": "T4-x", "tier": "T4", "cmd": "exit 1", "scope": "bundle"}]
        d = _bundle(self.cfg, "D8", brief_body=_FIX_BRIEF, accepted=True)
        with redirect_stderr(io.StringIO()):
            self.assertTrue(publish.draft_texts(self.cfg, d, run_t4=False))  # drafted only
            self.assertTrue((d / "commit-msg.txt").exists())
            self.assertFalse(publish.draft_texts(self.cfg, d))               # T4 phase gates

    def test_validation_phase_never_redrafts_a_deleted_text(self) -> None:
        # #295 review round 4: a text missing at VALIDATION means a later leaf deleted
        # it — re-drafting would invoke the publisher leaf mid-validation, reopening
        # the shared-root mutation window. Validation fails the bundle instead.
        d = _bundle(self.cfg, "D9", brief_body=_FIX_BRIEF, accepted=True)
        with redirect_stderr(io.StringIO()):
            self.assertTrue(publish.draft_texts(self.cfg, d, run_t4=False))  # drafted
        (d / "pr-description.md").unlink()             # a later leaf "deleted" it
        err = io.StringIO()
        with mock.patch.object(publish.leaves, "run_publish") as run_pub, \
                redirect_stderr(err):
            self.assertFalse(publish.draft_texts(self.cfg, d, draft=False))
        run_pub.assert_not_called()                    # never re-drafts mid-validation
        self.assertIn("NOT re-drafting", err.getvalue())

    def test_wave_drafts_all_then_validates_all_before_any_mechanics(self) -> None:
        # Two-bundle wave, three phases (#295 review round 2): EVERY draft precedes any
        # T4 validation (a later bundle's leaf may touch an earlier bundle's artifacts —
        # T4 must judge final contents), and every validation precedes the first publish.
        # A failed validation blocks only its own bundle.
        for iid in ("O1", "O2"):
            d = self.cfg.bundle(iid)
            d.mkdir(parents=True)
            (d / "brief.md").write_text(_FIX_BRIEF, encoding="utf-8")
        calls: list[tuple[str, str]] = []

        def fake_draft(_cfg, d, run_t4=True, draft=True):
            kind = "validate" if run_t4 else "draft"
            if kind == "validate":
                self.assertFalse(draft)  # validation never re-drafts (#295 review r4)
            calls.append((kind, d.name))
            return True if not run_t4 else d.name != "issue_O1"  # O1 fails validation

        def fake_publish(_cfg, issue_id, **kw):
            calls.append(("publish", f"issue_{issue_id}"))
            # The wave's mechanics phase must declare the pre-pass validation (#295
            # review) so publish never re-runs T4 mid-wave.
            self.assertTrue(kw.get("texts_prevalidated"))
            return 0

        err = io.StringIO()
        with mock.patch.object(flow.publish, "draft_texts", side_effect=fake_draft), \
                mock.patch.object(flow.publish, "publish", side_effect=fake_publish), \
                redirect_stderr(err), redirect_stdout(io.StringIO()):
            results = flow.flow_ids(self.cfg, ["O1", "O2"], do_publish=True, do_act=False,
                                    today="2026-07-18")
        self.assertEqual(set(results.values()), {state.COMPLETE})
        drafts = [i for i, c in enumerate(calls) if c[0] == "draft"]
        validates = [i for i, c in enumerate(calls) if c[0] == "validate"]
        publishes = [i for i, c in enumerate(calls) if c[0] == "publish"]
        self.assertEqual((len(drafts), len(validates)), (2, 2))
        self.assertTrue(max(drafts) < min(validates))         # all leaves finish first
        self.assertTrue(max(validates) < min(publishes))      # all T4 before mechanics
        self.assertEqual([calls[i][1] for i in publishes], ["issue_O2"])  # O1 blocked
        self.assertIn("issue_O1 — publish texts not ready", err.getvalue())
        self.assertIn("pdca publish O1", err.getvalue())


class ContributionTemplates(unittest.TestCase):
    """Both publisher templates must scaffold the tracker reference as a first-class
    line (issue #79) — the contribution gate lints commit-msg and PR body
    independently, so a slot missing from one reliably drops the id there."""

    def test_pr_description_has_tracker_reference_slot(self) -> None:
        commit_tpl = (TEMPLATES / "commit-msg.txt.tpl").read_text(encoding="utf-8")
        pr_tpl = (TEMPLATES / "pr-description.md.tpl").read_text(encoding="utf-8")
        # The commit template has always had the reference line; the PR body now mirrors it.
        self.assertIn("Fixes #<id>", commit_tpl)
        self.assertIn("Fixes #<id>", pr_tpl)
        # It sits after the body sections, with guidance on the ticketless case.
        self.assertLess(pr_tpl.index("## Verification"), pr_tpl.index("Fixes #<id>"))
        self.assertIn("declared-ticketless", pr_tpl)
        # The accessibility lead (#106): a plain-language Summary precedes the internals.
        self.assertLess(pr_tpl.index("## Summary"), pr_tpl.index("## Root cause"))
        # The user-visible effect leads as an explicit, copy-able label, before Root cause.
        self.assertIn("**User impact:**", pr_tpl)
        self.assertLess(pr_tpl.index("**User impact:**"), pr_tpl.index("## Root cause"))


_GOOD_PR = ("## Summary\n**User impact:** users saw a crash on save.\n\n"
            "One-line change.\n\n## Root cause\nx.\n\n## Fix\nx.\n\nFixes #266\n")
_GOOD_COMMIT = "Fix the save crash\n\nBody under eighty.\n\nFixes #266\n"


class ContribCheck(unittest.TestCase):
    """The T4 contribution gate (`pdca contribcheck`): the PR body must open with a
    `**User impact:**` line (before Root cause) and both artifacts must carry the tracker
    id — enforced even when a weak publisher model drops them."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundle_with(self, iid: str, pr_body: str, commit: str = _GOOD_COMMIT) -> Path:
        d = _bundle(self.cfg, iid, brief_body=_FIX_BRIEF, accepted=True)  # writes patch.diff
        (d / "pr-description.md").write_text(pr_body, encoding="utf-8")
        (d / "commit-msg.txt").write_text(commit, encoding="utf-8")
        return d

    def _run(self, iid: str, *, no_issue: bool = False) -> tuple[int, str]:
        args = SimpleNamespace(issue_id=iid, no_issue=no_issue)
        err = io.StringIO()
        with redirect_stderr(err):
            rc = cli._contribcheck(self.cfg, args)
        return rc, err.getvalue()

    def test_passes_on_well_formed_contribution(self) -> None:
        self._bundle_with("266", _GOOD_PR)
        self.assertEqual(self._run("266"), (0, ""))

    def test_fails_without_user_impact_opener(self) -> None:
        self._bundle_with("266", "## Summary\nsome change.\n\n## Root cause\nx.\n\nFixes #266\n")
        rc, err = self._run("266")
        self.assertEqual(rc, 1)
        self.assertIn("User impact", err)

    def test_fails_when_user_impact_falls_after_root_cause(self) -> None:
        self._bundle_with("266", "## Summary\nx.\n\n## Root cause\nx.\n\n"
                                 "**User impact:** late.\n\nFixes #266\n")
        rc, err = self._run("266")
        self.assertEqual(rc, 1)
        self.assertIn("before", err.lower())

    def test_fails_when_id_missing_from_commit(self) -> None:
        self._bundle_with("266", _GOOD_PR, commit="Fix the crash\n\nNo trailer here.\n")
        rc, err = self._run("266")
        self.assertEqual(rc, 1)
        self.assertIn("commit-msg.txt", err)

    def test_fails_when_id_missing_from_pr_body(self) -> None:
        self._bundle_with("266", "## Summary\n**User impact:** x.\n\n## Root cause\nx.\n")
        rc, err = self._run("266")
        self.assertEqual(rc, 1)
        self.assertIn("pr-description.md", err)

    def test_no_issue_relaxes_the_trailer_requirement(self) -> None:
        # pending-id: no tracker trailer required, but the user-impact opener still is.
        self._bundle_with("266", "## Summary\n**User impact:** x.\n\n## Root cause\nx.\n",
                          commit="Fix the crash\n\nNo trailer.\n")
        self.assertEqual(self._run("266", no_issue=True)[0], 0)

    def test_pdca_pending_id_env_is_the_no_issue_flag(self) -> None:
        # #384: publish tells the gate its mode via $PDCA_PENDING_ID (derived per run,
        # scrubbed when absent) rather than editing the shipped row's cmd line — an
        # in-place edit of that registered line breaks `copier update` for any instance
        # that appended a row beside it (tests/test_update_compat.py). The env form is
        # exactly the --no-issue flag: trailer waved, opener still enforced.
        self._bundle_with("266", "## Summary\n**User impact:** x.\n\n## Root cause\nx.\n",
                          commit="Fix the crash\n\nNo trailer.\n")
        self.assertEqual(self._run("266")[0], 1)                # id-known mode: enforced
        with mock.patch.dict(os.environ, {"PDCA_PENDING_ID": "1"}):
            self.assertEqual(self._run("266")[0], 0)            # pending-id via the env
            self._bundle_with("267", "## Summary\nno opener.\n")
            self.assertEqual(self._run("267")[0], 1)            # opener still enforced

    def test_slug_bundle_skips_the_trailer_requirement(self) -> None:
        # A non-numeric (slug) id has no real ticket number → only the opener is enforced.
        self._bundle_with("820-build", "## Summary\n**User impact:** x.\n\n## Root cause\nx.\n",
                          commit="Fix\n\nNo trailer.\n")
        self.assertEqual(self._run("820-build")[0], 0)

    def test_default_open_before_artifacts_are_drafted(self) -> None:
        # Run as a Check-time bundle gate before publish: no PR body yet ⇒ nothing to lint.
        # Exit code unchanged (0 — nothing failed), but the gate now DECLARES the deferral
        # instead of exiting mute, so the Check matrix records `deferred` rather than a
        # vacuous green (issue #401; the row's classification is pinned in
        # tests/test_gate_deferred.py). Marker composed from the production constant so this
        # module never emits it at a declaring position in its own captured output (#428).
        _bundle(self.cfg, "266", brief_body=_FIX_BRIEF, accepted=True)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(self._run("266"), (0, ""))  # no problems on stderr
        declared = [ln for ln in out.getvalue().splitlines()
                    if ln.startswith(gates.DEFERRED_MARKER)]
        self.assertEqual(len(declared), 1, out.getvalue())
        self.assertIn("publish", declared[0])

    def test_close_disposition_bundle_passes(self) -> None:
        d = _bundle(self.cfg, "266", brief_body=_FIX_BRIEF, accepted=True)
        (d / "patch.diff").write_text("", encoding="utf-8")   # no-fix / close bundle
        self.assertEqual(self._run("266"), (0, ""))

    def test_offline_stub_artifacts_pass_the_gate(self) -> None:
        # The stub publisher's output must keep passing the gate (guards the offline path).
        d = _bundle(self.cfg, "266", brief_body=_FIX_BRIEF, accepted=True)
        leaves._stub_publish(d, self.cfg)
        self.assertEqual(self._run("266"), (0, ""))


class PublisherGuard(unittest.TestCase):
    """A non-claude (codex) publisher has no PreToolUse STOP hook, so run_publish must give it
    the driver's `gh` PATH-shim; a claude publisher (native_guard) must not be shimmed."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = _cfg(self.tmp)

    def _env_passed_to_invoke(self, family: str):
        self.cfg.publisher = LeafConfig(mode="command", family=family,
                                        interactive=True, agent="publisher")
        captured: dict = {}
        with mock.patch.object(leaves, "_invoke",
                               side_effect=lambda *a, **k: captured.update(env=k.get("env"))), \
             mock.patch.object(leaves, "_publish_prompt", return_value="PROMPT"), \
             mock.patch.object(leaves.guard, "shim_env", return_value={"PATH": "SHIMMED"}):
            leaves.run_publish(self.cfg.bundle("X"), self.cfg)
        return captured["env"]

    def test_codex_publisher_gets_the_gh_shim(self) -> None:
        env = self._env_passed_to_invoke("codex")
        self.assertEqual(env.get("PATH"), "SHIMMED")
        # The #331 exit-contract session env rides the same dict.
        self.assertEqual(env.get("PDCA_HANDOFF_ROLE"), "publisher")

    def test_claude_publisher_is_not_shimmed(self) -> None:
        env = self._env_passed_to_invoke("claude")
        # No gh shim for claude (native PreToolUse guard) — but the #331 exit-contract
        # session env is present for every command-mode interactive publisher.
        self.assertNotIn("PATH", env or {})
        self.assertEqual((env or {}).get("PDCA_HANDOFF_ROLE"), "publisher")


class MergeModeBaseGuard(unittest.TestCase):
    """#411 — under `[driver].wave_mode = "merge"` the driver merges each accepted bundle's
    PR "into its base" (merge.py), unattended and mid-flow, whatever base that PR carries.
    So publish must REFUSE, fail-closed, to open a PR against a branch THIS run produced —
    naming both the branch the PR would have targeted and the target base — by either route
    that puts one there. The default `"stack"` mode is untouched: nothing merges for you
    there, and chaining onto a predecessor is the point.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = _cfg(self.tmp)
        self.cfg.base_remote = "origin"        # own-repo — the only mode `merge` supports

    def _publish(self, issue_id: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = publish.publish(self.cfg, issue_id, dry_run=True, by="T", today="2026-06-05")
        return rc, out.getvalue(), err.getvalue()

    def _published(self, issue_id: str, branch: str, **extra) -> Path:
        """A sibling bundle that already published `branch` in this batch."""
        p = self.cfg.bundle(issue_id)
        p.mkdir(parents=True)
        (p / "publish.json").write_text(json.dumps({"branch": branch, **extra}),
                                        encoding="utf-8")
        return p

    def _assert_nothing_published(self, out: str, d: Path) -> None:
        self.assertNotIn("gh pr create", out)                  # no PR
        self.assertNotIn("push", out)                          # nothing pushed
        self.assertFalse((d / "publish.json").exists())        # no recorded contribution

    def test_merge_mode_refuses_a_pr_based_on_a_stacked_prereq_branch(self) -> None:
        # Route 1: `Stacks on:` falls back to the prereq's own fix branch (_stack_base_branch),
        # and merge mode never records an integration branch — so that fallback is what picks
        # the base, and the merge would land the fix in the prereq's branch.
        self._published("PARENT", "fix/PARENT-my-fix")
        d = _bundle(self.cfg, "DEP", brief_body=_FIX_BRIEF + "- **Stacks on:** PARENT\n",
                    accepted=True)
        self.cfg.wave_mode = "merge"
        rc, out, err = self._publish("DEP")
        self.assertEqual(rc, 1)
        self.assertIn("fix/PARENT-my-fix", err)     # the branch the PR would have targeted
        self.assertIn("`main`", err)                # the target base it should have used
        self._assert_nothing_published(out, d)

    def test_merge_mode_refuses_a_target_base_another_bundle_produced(self) -> None:
        # Route 2: the brief's own `Repo + branch target` names a predecessor's branch (the
        # documented chained-brief practice for stack mode). pr_base then EQUALS the resolved
        # target base, so comparing the two sees nothing wrong — the batch's publish.json
        # records are what expose it, offline.
        self._published("PRED", "fix/PRED-groundwork", mode="new-pr", base="main",
                        repo="example-org/example-repo")
        d = _bundle(self.cfg, "CHAIN", brief_body=(
            "- **Slug:** my-fix\n"
            "- **Repo + branch target:** example-org/example-repo @ fix/PRED-groundwork\n"),
            accepted=True)
        self.cfg.wave_mode = "merge"
        rc, out, err = self._publish("CHAIN")
        self.assertEqual(rc, 1)
        self.assertIn("fix/PRED-groundwork", err)   # target == the branch PRED produced
        self.assertIn("issue_PRED", err)            # …and which bundle produced it
        self._assert_nothing_published(out, d)

    def test_merge_mode_publishes_an_ordinary_bundle_against_the_shared_base(self) -> None:
        # The guard is not a blanket stop: a bundle whose base is the shared target still
        # publishes in merge mode, even with other bundles' branches recorded in the batch.
        self._published("SIB", "fix/SIB-other", repo="example-org/example-repo")
        _bundle(self.cfg, "OK", brief_body=_FIX_BRIEF, accepted=True)
        self.cfg.wave_mode = "merge"
        rc, out, err = self._publish("OK")
        self.assertEqual(rc, 0, err)
        self.assertIn("--base main", out)

    def test_stack_mode_still_chains_and_still_accepts_a_branch_target(self) -> None:
        # The default mode is untouched by the guard, in both of the shapes it refuses above:
        # the stacked PR chains onto the parent branch (#123/#185), and a brief targeting a
        # predecessor's branch publishes against it.
        self._published("PARENT", "fix/PARENT-my-fix")
        _bundle(self.cfg, "DEP", brief_body=_FIX_BRIEF + "- **Stacks on:** PARENT\n",
                accepted=True)
        self.assertEqual(self.cfg.wave_mode, "stack")           # the default
        rc, out, err = self._publish("DEP")
        self.assertEqual(rc, 0, err)
        self.assertIn("--base fix/PARENT-my-fix", out)

        self._published("PRED", "fix/PRED-groundwork", repo="example-org/example-repo")
        _bundle(self.cfg, "CHAIN", brief_body=(
            "- **Slug:** my-fix\n"
            "- **Repo + branch target:** example-org/example-repo @ fix/PRED-groundwork\n"),
            accepted=True)
        rc, out, err = self._publish("CHAIN")
        self.assertEqual(rc, 0, err)
        self.assertIn("--base fix/PRED-groundwork", out)


if __name__ == "__main__":
    unittest.main()
