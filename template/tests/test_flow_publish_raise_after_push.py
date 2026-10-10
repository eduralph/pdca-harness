"""A publish whose push succeeded counts as pushed, even when `gh pr create` raises (#632).

`flow._publish_bundle` tells the wave fold which bundles pushed a branch this run from
`publish.json`. The new-PR path of `publish.publish` pushes the branch, then runs
`gh pr create`, then writes the record. A `gh pr create` that RETURNS rc 1 already ends
with the record written (empty `pr_url`) and the branch folded. One that RAISES (`gh` not
installed: `FileNotFoundError`; or any other exception) must end in the same state: the
branch is on origin, so the run must count it as pushed, fold it, and tell the operator
that the PR is still to be opened. A failure BEFORE the push must still count as unpushed.

Offline: `publish._check_repo` is patched to 0 and `subprocess.run` is stubbed, so no
checkout, network or `gh` is needed. The publisher is non-stub (a stub publisher makes
`_publish_bundle` a dry-run that pushes nothing) and the texts are pre-written with
`texts_prevalidated=True`, so no publisher leaf and no T4 run.
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

from pdca_harness import flow, integrate, publish, signoff
from pdca_harness.config import Config, LeafConfig

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"

_FIX_BRIEF = (
    "- **Slug:** my-fix\n"
    "- **Repo + branch target:** example-org/example-repo @ main\n"
)

_OK = SimpleNamespace(returncode=0, stdout="", stderr="")


# `_cfg` / `_bundle` copied from test_publish_slice.py:46-81 (the suite has no shared
# helper module), with the publisher non-stub: `_publish_bundle` passes
# dry_run=cfg.publisher.mode == "stub", and a dry-run pushes nothing.
def _cfg(root: Path) -> Config:
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
        publisher=LeafConfig(mode="command", interactive=True),
        act=LeafConfig(mode="stub", interactive=True),
        gates_checks=[],
        repo_checkouts={"example-org/example-repo": str(root / "example-repo")},
    )


def _bundle(cfg: Config, issue_id: str) -> Path:
    """An accepted (COMPLETE) bundle with both contribution texts already written."""
    d = cfg.bundle(issue_id)
    d.mkdir(parents=True)
    (d / "brief.md").write_text(_FIX_BRIEF, encoding="utf-8")
    (d / "patch.diff").write_text("diff --git a/x b/x\n", encoding="utf-8")
    (d / "check-gates.json").write_text("{}", encoding="utf-8")
    shutil.copyfile(TEMPLATES / "SUMMARY.md.tpl", d / "SUMMARY.md")
    signoff.record(d / "SUMMARY.md", action="accept", by="Tester", date="2026-06-05")
    (d / publish.COMMIT_MSG).write_text("Fix the thing\n\nFixes #1\n", encoding="utf-8")
    (d / publish.PR_BODY).write_text("Fixes the thing.\n", encoding="utf-8")
    return d


class PublishRaiseAfterPush(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _accepted(self, iid: str) -> Path:
        return _bundle(self.cfg, iid)

    def _run(self, d: Path, fake_run) -> tuple[bool, list[list[str]], str]:
        """`flow._publish_bundle` with git/gh stubbed; (pushed, commands seen, stderr)."""
        seen: list[list[str]] = []

        def recording(cmd, *a, **k):
            seen.append(list(cmd))
            return fake_run(cmd, *a, **k)

        err = io.StringIO()
        with mock.patch.object(publish, "_check_repo", return_value=0), \
             mock.patch.object(publish.subprocess, "run", side_effect=recording), \
             mock.patch.object(publish.leaves, "run_publish",
                               side_effect=AssertionError("no publisher leaf may run")), \
             redirect_stdout(io.StringIO()), redirect_stderr(err):
            pushed = flow._publish_bundle(self.cfg, d, by="Tester", today="2026-06-05",
                                          texts_prevalidated=True)
        return pushed, seen, err.getvalue()

    @staticmethod
    def _pushes(seen: list[list[str]]) -> list[list[str]]:
        return [c for c in seen if c[:1] == ["git"] and "push" in c]

    def _assert_pushed_and_folded(self, d: Path, exc: BaseException) -> None:
        def fake_run(cmd, *a, **k):  # every git step succeeds; `gh pr create` raises
            if cmd[:3] == ["gh", "pr", "create"]:
                raise exc
            return _OK

        pushed, seen, err = self._run(d, fake_run)
        # The call really reached the push and then `gh pr create` (not a dry-run).
        self.assertEqual(len(self._pushes(seen)), 1, seen)
        self.assertTrue(any(c[:3] == ["gh", "pr", "create"] for c in seen), seen)

        # (1) the branch was pushed this run, so the flow says so.
        self.assertTrue(pushed, f"push succeeded but _publish_bundle returned False\n{err}")

        # (2) the fold has that branch on record, from (1)'s answer.
        pushed_set = {d.name} if pushed else set()
        self.assertEqual(integrate.unpublished([d], pushed=pushed_set), [])
        branch = "fix/{}-my-fix".format(d.name.removeprefix("issue_"))
        self.assertEqual(integrate._published_ref(d), ("origin", f"origin/{branch}", False))
        pj = json.loads((d / "publish.json").read_text(encoding="utf-8"))
        self.assertEqual(pj["branch"], branch)
        self.assertEqual(pj["pr_url"], "")                      # no PR was opened

        # (3) the operator is told: the branch is on origin, no PR, how to finish, and
        # that publish did NOT complete.
        self.assertIn(branch, err)
        self.assertIn("origin", err)
        self.assertIn("no draft PR was opened", err)
        self.assertIn(type(exc).__name__, err)
        self.assertIn("pdca publish {}".format(d.name.removeprefix("issue_")), err)
        self.assertIn("did not complete", err)
        self.assertNotIn("Draft PR prepared", err)

    def test_gh_missing_after_push_counts_as_pushed(self) -> None:
        d = self._accepted("RAISEFNF")
        self._assert_pushed_and_folded(
            d, FileNotFoundError(2, "No such file or directory", "gh"))

    def test_non_oserror_after_push_counts_as_pushed(self) -> None:
        d = self._accepted("RAISERT")
        self._assert_pushed_and_folded(d, RuntimeError("gh exploded"))

    # (4) unchanged: a failure BEFORE the push pushes nothing and records no branch.
    def test_push_failure_is_not_pushed(self) -> None:
        d = self._accepted("PUSHFAIL")

        def fake_run(cmd, *a, **k):
            if cmd[:1] == ["git"] and "push" in cmd:
                return SimpleNamespace(returncode=1, stdout="", stderr="rejected")
            if cmd[:3] == ["gh", "pr", "create"]:
                raise AssertionError("gh pr create must not run after a failed push")
            return _OK

        pushed, seen, _err = self._run(d, fake_run)
        self.assertEqual(len(self._pushes(seen)), 1, seen)
        self.assertFalse(pushed)
        self.assertFalse((d / "publish.json").exists())
        self.assertEqual(integrate.unpublished([d], pushed=set()), [d])
        self.assertIsNone(integrate._published_ref(d))

    def test_apply_raise_is_not_pushed(self) -> None:
        d = self._accepted("APPLYRAISE")

        def fake_run(cmd, *a, **k):
            if cmd[:1] == ["git"] and "apply" in cmd:
                raise OSError("apply blew up")
            if cmd[:3] == ["gh", "pr", "create"]:
                raise AssertionError("gh pr create must not run after a failed apply")
            return _OK

        pushed, seen, _err = self._run(d, fake_run)
        self.assertEqual(self._pushes(seen), [])
        self.assertFalse(pushed)
        self.assertFalse((d / "publish.json").exists())
        self.assertEqual(integrate.unpublished([d], pushed=set()), [d])
        self.assertIsNone(integrate._published_ref(d))


if __name__ == "__main__":
    unittest.main()
