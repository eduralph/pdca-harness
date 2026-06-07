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
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pdca_harness import publish, signoff, state
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


class PublishSlice(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

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
        self.assertEqual(pj["pr_url"], "")                       # recorded, but empty
        self.assertEqual(pj["branch"], "fix/PUBFAIL-my-fix")     # branch was pushed

    def test_checkout_path_map_and_sibling_fallback(self) -> None:
        # sibling fallback: <root>/../<repo-last-segment>
        self.assertEqual(publish._checkout_path(self.cfg, "org/foo"),
                         (self.cfg.root.parent / "foo").resolve())
        # configured map wins; a relative path resolves against the project root
        self.cfg.repo_checkouts = {"org/foo": "../custom-foo"}
        self.assertEqual(publish._checkout_path(self.cfg, "org/foo"),
                         (self.cfg.root / "../custom-foo").resolve())


if __name__ == "__main__":
    unittest.main()
