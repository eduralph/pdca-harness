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
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pdca_harness import gates, leaves, publish, signoff, state
from pdca_harness.config import Config, LeafConfig

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


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

    def test_publish_prompt_hyperlinks_tracker_when_pattern_set(self) -> None:
        # #266: with [tracker].issue_url_pattern set, the publish prompt instructs the leaf
        # to hyperlink the resolved ticket URL (not just the bare id); absent ⇒ no link clause.
        d = _bundle(self.cfg, "266", brief_body=_FIX_BRIEF, accepted=True)   # a numeric ticket id
        self.cfg.issue_url_pattern = "https://tracker/view.php?id={id}"
        prompt = leaves._publish_prompt(d, self.cfg)
        self.assertIn("https://tracker/view.php?id=266", prompt)
        self.assertIn("Hyperlink the tracker ticket", prompt)
        self.cfg.issue_url_pattern = ""
        self.assertNotIn("Hyperlink the tracker ticket", leaves._publish_prompt(d, self.cfg))

    def test_publish_prompt_omits_link_for_a_slug_or_pending_bundle(self) -> None:
        # #192/#196: a slug bundle (fork issue), a `--no-issue`/id_pending placeholder, or any
        # non-numeric id has no real ticket number, so the pattern would format a broken link —
        # omit the clause even with issue_url_pattern set (the bare-number trailer is gated the
        # same way). Only a real ticket NUMBER links.
        self.cfg.issue_url_pattern = "https://tracker/view.php?id={id}"
        for iid in ("820-build-toolchain-coverage", "PEND"):     # slug, then a pending placeholder
            d = _bundle(self.cfg, iid, brief_body=_FIX_BRIEF, accepted=True)
            prompt = leaves._publish_prompt(d, self.cfg)
            self.assertNotIn("Hyperlink the tracker ticket", prompt)        # no link clause
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

    def test_no_issue_relaxes_failing_t4_to_a_flag(self) -> None:
        """Issue #7 item3: `--no-issue` (pending_id) relaxes a failing T4 to a flag
        instead of aborting — publish proceeds and flags it; without it a failing T4
        still aborts. The first-class 'no tracker id yet' path (vs a magic #0000)."""
        self.cfg.gates_checks = [{"id": "T4-x", "tier": "T4", "cmd": "exit 1", "scope": "bundle"}]
        _bundle(self.cfg, "PEND", brief_body=_FIX_BRIEF, accepted=True)
        # default: a failing T4 aborts the publish
        self.assertEqual(publish.publish(self.cfg, "PEND", dry_run=True), 1)
        # --no-issue: the failing T4 is relaxed to a flag; publish proceeds
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            rc = publish.publish(self.cfg, "PEND", dry_run=True, pending_id=True)
        self.assertEqual(rc, 0)
        self.assertIn("pending-id", err.getvalue().lower())

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

    def test_checkout_path_map_and_sibling_fallback(self) -> None:
        # sibling fallback: <root>/../<repo-last-segment>
        self.assertEqual(publish._checkout_path(self.cfg, "org/foo"),
                         (self.cfg.root.parent / "foo").resolve())
        # configured map wins; a relative path resolves against the project root
        self.cfg.repo_checkouts = {"org/foo": "../custom-foo"}
        self.assertEqual(publish._checkout_path(self.cfg, "org/foo"),
                         (self.cfg.root / "../custom-foo").resolve())

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


if __name__ == "__main__":
    unittest.main()
