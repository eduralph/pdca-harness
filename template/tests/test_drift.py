"""Drift sweep (issue #206) — re-check a published bundle's patch against the CURRENT base.

Real git: a bare `origin` + a primary checkout. A patch that still applies to the current
`origin/main` reports `ok`; after upstream moves the same lines, the same patch reports
`needs-rebase`. Report-only — the sweep never mutates the bundle. Skips accepted-but-
unpublished and close/no-fix bundles. Run from the project root:
    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pdca_harness import drift, signoff, state
from pdca_harness.config import Config, LeafConfig

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"

# A patch that rewrites base.txt's single line — applies iff base.txt is exactly "base\n".
_PATCH = """\
diff --git a/base.txt b/base.txt
--- a/base.txt
+++ b/base.txt
@@ -1 +1 @@
-base
+base-fix
"""


class Drift(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        self.primary = self.tmp / "repo"
        subprocess.run(["git", "init", "--bare", "-q", str(self.origin)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.primary)], check=True)
        self._cfg_git(self.primary)
        (self.primary / "base.txt").write_text("base\n", encoding="utf-8")
        self._git(self.primary, "add", "-A")
        self._git(self.primary, "commit", "-q", "-m", "base")
        self._git(self.primary, "remote", "add", "origin", str(self.origin))
        self._git(self.primary, "push", "-q", "origin", "main")
        self.cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results", process_dir=self.tmp / "process",
            templates_dir=TEMPLATES, default_branch="main", tracker_system="github",
            tracker_url="", issue_id_example="1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
            base_remote="origin", repo_checkouts={"org/repo": str(self.primary)})

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _git(self, repo: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True, text=True)

    def _cfg_git(self, repo: Path) -> None:
        self._git(repo, "config", "user.email", "t@example.com")
        self._git(repo, "config", "user.name", "Tester")
        self._git(repo, "config", "commit.gpgsign", "false")

    def _bundle(self, iid: str, *, patch: str | None = _PATCH,
                pr_url: str | None = "https://github.com/org/repo/pull/1") -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text(
            "- **Slug:** s\n- **Repo + branch target:** org/repo @ main\n", encoding="utf-8")
        if patch is not None:
            (d / "patch.diff").write_text(patch, encoding="utf-8")
        (d / "check-gates.json").write_text("{}", encoding="utf-8")
        shutil.copyfile(TEMPLATES / "SUMMARY.md.tpl", d / "SUMMARY.md")
        signoff.record(d / "SUMMARY.md", action="accept", by="T", date="2026-06-05")
        if pr_url is not None:
            (d / "publish.json").write_text(json.dumps({"pr_url": pr_url}), encoding="utf-8")
        return d

    def _move_upstream(self) -> None:
        """Rewrite base.txt on main and push, so the bundle patch no longer applies."""
        (self.primary / "base.txt").write_text("upstream-moved\n", encoding="utf-8")
        self._git(self.primary, "commit", "-qam", "upstream moves the same line")
        self._git(self.primary, "push", "-q", "origin", "main")

    def test_clean_patch_reports_ok(self) -> None:
        d = self._bundle("A")
        self.assertEqual(state.state(d), state.COMPLETE)
        rows = drift.sweep(self.cfg, fetch=True)
        self.assertEqual([r["status"] for r in rows], ["ok"])

    def test_moved_base_flags_needs_rebase(self) -> None:
        self._bundle("A")
        self._move_upstream()
        rows = drift.sweep(self.cfg, fetch=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "needs-rebase")
        self.assertEqual(rows[0]["pr_url"], "https://github.com/org/repo/pull/1")

    def test_report_only_does_not_touch_the_bundle(self) -> None:
        d = self._bundle("A")
        self._move_upstream()
        before = (d / "patch.diff").read_text(encoding="utf-8")
        drift.sweep(self.cfg, fetch=True)
        self.assertEqual((d / "patch.diff").read_text(encoding="utf-8"), before)
        self.assertFalse((d / "drift.json").exists())  # writes nothing into the bundle

    def test_unpublished_bundle_is_skipped(self) -> None:
        self._bundle("A", pr_url=None)  # accepted but no PR → not a drift case
        self.assertEqual(drift.sweep(self.cfg, fetch=True), [])

    def test_stacked_pr_resolves_its_own_base_not_the_brief_base(self) -> None:
        # An `Onto branch` bundle's PR is applied onto that branch, not upstream/main; drift
        # must check the branch the PR really depends on (PR #211 review).
        d = self._bundle("A")
        (d / "brief.md").write_text(
            "- **Slug:** s\n- **Repo + branch target:** org/repo @ main\n"
            "- **Onto branch:** origin/pr-42\n", encoding="utf-8")
        self.assertEqual(drift._resolve_base(self.cfg, d, "main"),
                         ("origin", "pr-42", "origin/pr-42"))
        # No `Onto branch` → the target base.
        (d / "brief.md").write_text(
            "- **Slug:** s\n- **Repo + branch target:** org/repo @ main\n", encoding="utf-8")
        self.assertEqual(drift._resolve_base(self.cfg, d, "main"),
                         ("origin", "main", "origin/main"))

    def test_failed_fetch_reports_error_not_stale_clean(self) -> None:
        # A base branch that doesn't exist upstream → `git fetch` fails → error, never a
        # false apply-clean against a stale ref (PR #211 review).
        d = self._bundle("A")
        (d / "brief.md").write_text(
            "- **Slug:** s\n- **Repo + branch target:** org/repo @ gone\n", encoding="utf-8")
        rows = drift.sweep(self.cfg, fetch=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "error")
        self.assertIn("fetch", rows[0]["detail"])

    def test_close_no_fix_bundle_is_skipped(self) -> None:
        # No patch to apply — a close disposition ships nothing. Needs the close marker so
        # state() still reads COMPLETE without a patch.diff.
        d = self._bundle("A", patch=None)
        (d / state.CLOSE_MARKER).write_text("closed\n", encoding="utf-8")
        self.assertEqual(state.state(d), state.COMPLETE)
        self.assertEqual(drift.sweep(self.cfg, fetch=True), [])


if __name__ == "__main__":
    unittest.main()
