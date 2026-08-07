"""Reviewer-target git-writability (issue #419, stdlib unittest, offline).

The review contract asks the reviewer for an independent red→green re-run against
``$PDCA_TARGET``: stash the patch away (pre-fix state), exercise it, unstash. The
target the harness used to hand over was the per-cycle LINKED worktree, whose git
metadata — its index included — lives under the PRIMARY checkout's
``.git/worktrees/<name>/`` (worktree.py:14-16), and stash writes objects into the
shared ``.git/objects`` — both outside the reviewer's granted dir and read-only in
its confinement, so every index-writing git op failed and the C4 claim fell to §6
NEEDS-HUMAN each cycle.

These tests reproduce the denial deterministically without any vendor sandbox: the
primary checkout's ``.git`` tree is chmod'ed read-only (restored in teardown), the
lane worktree carries the patch as a tracked modification (the state the reviewer
must stash — a clean stash no-ops green), and the PRODUCTION sandbox setup
(``leaves._run_review_sandboxed`` / ``_run_advisory_sandboxed``, with only the model
invocation faked) hands over ``$PDCA_TARGET``. The handed target must support the
pre-fix restore + re-apply, and the primary checkout — tree and git metadata — must
be byte-identical before/after.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess as sp
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pdca_harness import leaves, worktree
from pdca_harness.config import Config, LeafConfig


def _cfg(root: Path) -> Config:
    return Config(
        root=root,
        bundle_root=root / "results",
        process_dir=root / "process",
        templates_dir=root / "templates",
        default_branch="main",
        tracker_system="github",
        tracker_url="",
        issue_id_example="1",
        builder=LeafConfig(mode="stub"),
        reviewer=LeafConfig(mode="command"),
        base_remote="origin",  # own-repo: branch the worktree off origin/<base>
    )


class ReviewerTargetGitWritable(unittest.TestCase):
    """The target handed to the sandboxed review leaves supports the contract's
    pre-fix restore + re-apply while the primary checkout's .git is read-only."""

    _PATCH = (
        "diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n"
        "@@ -1 +1,2 @@\n base\n+patched\n"
    )

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
        # Hermetic against the ambient environment (#419 iteration 2): a project's T3
        # suite gate runs this suite with the DRIVER's inherited env — a lane-parallel
        # flow carries PDCA_LANE, an auto-iterate flow may carry PDCA_AUTO_ITERATE —
        # and the production flows driven below must see only THIS fixture's state.
        env_guard = mock.patch.dict(os.environ)
        env_guard.start()
        self.addCleanup(env_guard.stop)
        for key in [k for k in os.environ if k.startswith("PDCA_")]:
            del os.environ[key]
        self.cfg = _cfg(self.tmp)
        self.primary = self.tmp / "checkout"
        origin = self.tmp / "origin.git"
        sp.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        sp.run(["git", "clone", "-q", str(origin), str(self.primary)], check=True)
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "T")
        (self.primary / "file.txt").write_text("base\n", encoding="utf-8")
        self._git("add", "-A"); self._git("commit", "-q", "-m", "base")
        self._git("branch", "-M", "main"); self._git("push", "-q", "-u", "origin", "main")
        self.cfg.repo_checkouts = {"org/repo": str(self.primary)}
        self.d = self.cfg.bundle("RTG")
        self.d.mkdir(parents=True)
        (self.d / "brief.md").write_text(
            "- **Slug:** s\n- **Repo + branch target:** org/repo @ main\n",
            encoding="utf-8")
        (self.d / "patch.diff").write_text(self._PATCH, encoding="utf-8")

    def _cleanup(self) -> None:
        self._set_primary_git_writable(True)  # rmtree needs the bits back
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _git(self, *a: str) -> None:
        sp.run(["git", "-C", str(self.primary), *a], check=True, capture_output=True)

    def _set_primary_git_writable(self, writable: bool) -> None:
        """chmod the PRIMARY checkout's whole .git tree — the reviewer-confinement
        simulation the brief specifies: index writes for a linked worktree land under
        .git/worktrees/<name>/, objects under .git/objects, both in here."""
        git_dir = self.primary / ".git"
        if git_dir.exists():
            sp.run(["chmod", "-R", "u+w" if writable else "a-w", str(git_dir)],
                   check=True)

    def _snapshot(self, root: Path) -> dict[str, str]:
        """Content snapshot of every entry under ``root`` (tree + git metadata), for
        the byte-identical before/after assertion."""
        out: dict[str, str] = {}
        for p in sorted(root.rglob("*")):
            rel = str(p.relative_to(root))
            if p.is_symlink():
                out[rel] = "link:" + os.readlink(p)
            elif p.is_file():
                out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
            else:
                out[rel] = "dir"
        return out

    def _dirty_lane(self) -> Path:
        """The lane worktree in the state Check leaves it: base checked out, this
        bundle's patch applied as a TRACKED, UNCOMMITTED modification — the state the
        reviewer must stash away (a clean stash no-ops green and proves nothing)."""
        wt = worktree.rebuild_for_gate(self.d, self.cfg)
        self.assertIsNotNone(wt)
        self.assertEqual((wt / "file.txt").read_text(encoding="utf-8"),
                         "base\npatched\n")
        return wt

    def _fake_reviewer(self, seen: dict):
        """Stand-in for the MODEL invocation only (`_invoke_leaf_resilient`): performs,
        from inside the sandbox cwd, exactly the git ops the review contract asks of
        the leaf against $PDCA_TARGET. Everything up to the invocation — sandbox
        seeding, target resolution/materialization, env/argv — is production code."""
        def fake(leaf, cwd, prompt, **kw):
            tgt = Path((kw.get("env") or {}).get("PDCA_TARGET", ""))
            seen["target"] = tgt
            seen["extra_argv"] = list(kw.get("extra_argv") or [])
            stash = sp.run(["git", "-C", str(tgt), "stash"],
                           capture_output=True, text=True)
            seen["stash"] = stash
            if stash.returncode == 0:
                seen["prefix_tree"] = (tgt / "file.txt").read_text(encoding="utf-8")
                seen["pop"] = sp.run(["git", "-C", str(tgt), "stash", "pop"],
                                     capture_output=True, text=True)
                seen["repatched_tree"] = (tgt / "file.txt").read_text(encoding="utf-8")
            # the leaf's artifact, so the production caller completes its copy-back
            (Path(cwd) / seen["artifact"]).write_text("# fake review\n",
                                                      encoding="utf-8")
            return None
        return fake

    def _assert_rerun_supported(self, seen: dict) -> None:
        stash = seen.get("stash")
        self.assertIsNotNone(stash, "the leaf was never handed a $PDCA_TARGET")
        self.assertEqual(
            stash.returncode, 0,
            msg="pre-fix restore (`git stash`) failed inside the reviewer's "
                f"confinement — the handed target {seen.get('target')} is not "
                f"git-writable there:\n{stash.stderr}")
        self.assertEqual(seen["prefix_tree"], "base\n")  # pre-fix state restored
        self.assertEqual(seen["pop"].returncode, 0,
                         msg=f"re-apply (`git stash pop`) failed:\n{seen['pop'].stderr}")
        self.assertEqual(seen["repatched_tree"], "base\npatched\n")  # patch re-applied

    def test_review_target_supports_prefix_restore_primary_git_readonly(self) -> None:
        # The brief's success criterion, end to end: with the primary .git read-only
        # (simulating the confinement), the reviewer-target materialization yields a
        # target where the pre-fix restore succeeds — and the primary receives no writes.
        self._dirty_lane()
        snap = self._snapshot(self.primary)
        self._set_primary_git_writable(False)
        seen: dict = {"artifact": "check-review.md"}
        orig = leaves._invoke_leaf_resilient
        leaves._invoke_leaf_resilient = self._fake_reviewer(seen)
        try:
            leaves._run_review_sandboxed(self.d, self.cfg)
        finally:
            leaves._invoke_leaf_resilient = orig
            self._set_primary_git_writable(True)
        self._assert_rerun_supported(seen)
        # the review round completed on the evidence produced in the sandbox
        self.assertTrue((self.d / "check-review.md").exists())
        # …and the primary checkout — tree AND git metadata — is byte-identical
        self.assertEqual(self._snapshot(self.primary), snap)

    def test_advisory_target_supports_prefix_restore_primary_git_readonly(self) -> None:
        # The invariant is not satisfiable by guarding one module: the advisory leaves
        # share the reviewer's contract (and the codex adversary's --add-dir rationale
        # names git stash/unstash explicitly, families.py:112-113), so their handed
        # target must support the same re-run.
        self._dirty_lane()
        snap = self._snapshot(self.primary)
        self._set_primary_git_writable(False)
        seen: dict = {"artifact": "check-advisory-adv.md"}
        orig = leaves._invoke_leaf_resilient
        leaves._invoke_leaf_resilient = self._fake_reviewer(seen)
        try:
            leaves._run_advisory_sandboxed(
                self.d, self.cfg, LeafConfig(mode="command"), {"id": "adv"}, "adv")
        finally:
            leaves._invoke_leaf_resilient = orig
            self._set_primary_git_writable(True)
        self._assert_rerun_supported(seen)
        self.assertEqual(self._snapshot(self.primary), snap)

    def test_patchless_bundle_keeps_readonly_grounding_on_the_real_target(self) -> None:
        # Scope guard: with no patch there is nothing to stash — the leaf grounds on
        # the real target (full history) via the grounding grant, exactly as before.
        (self.d / "patch.diff").write_text("", encoding="utf-8")
        wt = worktree.rebuild_for_gate(self.d, self.cfg)
        self.assertIsNotNone(wt)
        seen: dict = {"artifact": "check-review.md"}
        orig = leaves._invoke_leaf_resilient
        leaves._invoke_leaf_resilient = self._fake_reviewer(seen)
        try:
            leaves._run_review_sandboxed(self.d, self.cfg)
        finally:
            leaves._invoke_leaf_resilient = orig
        self.assertEqual(seen["target"], wt)


if __name__ == "__main__":
    unittest.main()
