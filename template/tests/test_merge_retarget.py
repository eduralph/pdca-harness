"""Merge mode merges a PR only into its bundle's own target base (issue #500).

#411 refuses, at publish, a merge-mode PR opened against a branch this run produced. A PR
that never passes through that check again (one an earlier run opened, or one whose base
was edited on the host since) used to be readied and merged into whatever base it carried.
`merge._merge_one` now reads the PR's base before the ready-mark and holds it to the
bundle's target base, as `publish._resolve_target` resolves it (the one parse publish uses):

- the same base: no `gh pr edit`, and the merge goes on as before (criterion d);
- another base: `gh pr edit --base <target>`, then a second read, and the merge goes on
  only if the host now reports the target (a). An edit that fails, or one that does not
  take, refuses naming both branches, with no `gh pr ready` (so no `--undo`) at all for
  that PR, and points at the PR on the host rather than at the brief (b);
- a target that a sibling bundle of the batch published (#411 route 2) is refused before
  the host is asked anything, whatever base the PR is on, with the way out publish's own
  #411 refusal gives (e);
- a malformed `expected_checks` is still refused first, with no `gh pr edit` (f);
- under `merge_requires = "all"`, a base edited on the host after that check (while the
  checks are read, or right after the ready-mark) is refused at the next read, ready-mark
  undone, never merged into and never updated against.

Each case runs under both `merge_requires` values where it applies (c). `gh` and git are
stubbed in front of `subprocess.run`, as `test_merge.py` does, so the run is offline. The
fake host records the base every `gh pr merge` landed on, so a test asserts the rule
itself: nothing is merged anywhere but the target base. Run from the template root:
    PYTHONPATH=src python -m unittest tests.test_merge_retarget
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

from pdca_harness import merge, merged, publish, state
from pdca_harness.config import Config, LeafConfig

PR = "https://github.com/org/repo/pull/7"
HEAD = "1" * 40
BASE_TIP = "b" * 40
OLD = "fix/old-parent"            # the wrong base the PR carries
REQUIRES = ("all", "required")
VIEW = ["gh", "pr", "view", PR, "--json", "headRefOid,baseRefName"]


def _cfg(root: Path, **overrides: object) -> Config:
    return Config(
        root=root, bundle_root=root / "results", process_dir=root / "process",
        templates_dir=root / "templates", default_branch="main", tracker_system="github",
        tracker_url="", issue_id_example="#1",
        builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
        base_remote="origin", repo_checkouts={"org/repo": str(root / "repo")},
        wave_mode="merge", **overrides)


def _res(code: int = 0, out: str = "", err: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=code, stdout=out, stderr=err)


def _at(calls: list[list[str]], prefix: list[str]) -> int:
    """Index of the first call that starts with ``prefix``, or -1."""
    return next((i for i, c in enumerate(calls) if c[:len(prefix)] == prefix), -1)


class _Host:
    """A fake `gh` and git for one PR, whose base the host reports as ``base``.

    ``gh pr edit --base X`` fails when ``edit_fails``; otherwise it moves the base to X,
    unless ``edit_ignored`` (the host accepts the edit and keeps reporting the old base).
    ``moves`` edits the base from outside the run: ``{(verb, n): branch}`` sets it to
    ``branch`` right after the n-th ``gh pr <verb>`` call. The rollup is green and the head
    up to date with whatever base it is on, so a PR that gets past the base checks merges;
    ``merged_into`` records the base each ``gh pr merge`` landed on."""

    def __init__(self, base: str, *, edit_fails: bool = False, edit_ignored: bool = False,
                 moves: dict[tuple[str, int], str] | None = None) -> None:
        self.base = base
        self.edit_fails = edit_fails
        self.edit_ignored = edit_ignored
        self.moves = dict(moves or {})
        self.calls: list[list[str]] = []
        self.merged_into: list[str] = []
        self._count: dict[str, int] = {}

    def __call__(self, cmd: list[str], **kw) -> SimpleNamespace:
        self.calls.append(list(cmd))
        if cmd[:1] == ["git"]:
            if "rev-parse" in cmd:
                return _res(out=BASE_TIP + "\n")
            return _res()                   # fetch, cat-file -e, merge-base --is-ancestor
        verb = cmd[2]
        result = self._gh(verb, cmd)
        self._count[verb] = self._count.get(verb, 0) + 1
        self.base = self.moves.get((verb, self._count[verb]), self.base)
        return result

    def _gh(self, verb: str, cmd: list[str]) -> SimpleNamespace:
        if verb == "view":
            return _res(out=json.dumps({"headRefOid": HEAD, "baseRefName": self.base}))
        if verb == "edit":
            if self.edit_fails:
                return _res(1, err="GraphQL: Resource not accessible by integration "
                                   "(updatePullRequest)")
            if not self.edit_ignored:
                self.base = cmd[cmd.index("--base") + 1]
            return _res()
        if verb == "checks":
            return _res(out=json.dumps([{"name": "ci", "bucket": "pass"}]))
        if verb == "merge":
            self.merged_into.append(self.base)
        return _res()                       # ready, ready --undo, merge, update-branch


class MergeHoldsThePrToItsTargetBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _bundle(self, cfg: Config, iid: str, *, target: str = "main",
                record: dict | None = None) -> Path:
        d = cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text(f"- **Repo + branch target:** org/repo @ {target}\n",
                                    encoding="utf-8")
        (d / "patch.diff").write_text("diff\n", encoding="utf-8")
        (d / "publish.json").write_text(json.dumps(record or {
            "mode": "new-pr", "branch": f"fix/{iid}-x", "pr_url": PR, "repo": "org/repo"}),
            encoding="utf-8")
        return d

    def _run(self, cfg: Config, host: _Host, d: Path) -> tuple[int, str]:
        """``d`` through the production ``merge.merge_wave`` against ``host``; returns the
        exit code and stderr. ``_sleep`` is patched, so the rollup wait costs no time."""
        with mock.patch("pdca_harness.merge.subprocess.run", side_effect=host), \
                mock.patch.object(merge, "_sleep"), \
                mock.patch.object(state, "state", return_value=state.COMPLETE), \
                mock.patch.object(merged, "is_merged", return_value=False), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            rc = merge.merge_wave(cfg, [d], method="merge")
        return rc, err.getvalue()

    # (a) + (c): a PR on another base is retargeted, its base read again, and only then
    # readied and merged, through the normal gates, into the target base.
    def test_wrong_base_is_retargeted_then_merged_into_the_target(self) -> None:
        for requires in REQUIRES:
            with self.subTest(merge_requires=requires):
                cfg = _cfg(self.tmp / requires, merge_requires=requires)
                host = _Host(OLD)
                rc, err = self._run(cfg, host, self._bundle(cfg, "500"))
                self.assertEqual(rc, 0, err)
                self.assertEqual(host.merged_into, ["main"])
                edit = _at(host.calls, ["gh", "pr", "edit", PR, "--base", "main"])
                ready = _at(host.calls, ["gh", "pr", "ready", PR])
                merge_at = _at(host.calls, ["gh", "pr", "merge", PR])
                self.assertNotEqual(edit, -1, host.calls)
                self.assertLess(edit, ready)
                self.assertLess(ready, merge_at)
                self.assertIn(VIEW, host.calls[edit + 1:ready])   # read again after the edit
                self.assertNotIn(["gh", "pr", "ready", PR, "--undo"], host.calls)
                if requires == "all":
                    self.assertIn(["gh", "pr", "checks", PR, "--json", "name,bucket"],
                                  host.calls[ready:merge_at])
                    self.assertEqual(host.calls[merge_at],
                                     ["gh", "pr", "merge", PR, "--merge",
                                      "--match-head-commit", HEAD])

    # (b) + (c): an edit that fails, or one the host ignores, stops the run with the PR as
    # it was — no ready-mark at all, so no undo, and no merge — and says to fix the PR on
    # the host: the brief's target is right, so pointing at the brief would mislead.
    def test_retarget_that_fails_or_does_not_take_refuses_with_the_pr_untouched(self) -> None:
        for how in ("edit_fails", "edit_ignored"):
            for requires in REQUIRES:
                with self.subTest(how=how, merge_requires=requires):
                    cfg = _cfg(self.tmp / how / requires, merge_requires=requires)
                    host = _Host(OLD, **{how: True})
                    rc, err = self._run(cfg, host, self._bundle(cfg, "500"))
                    self.assertEqual(rc, 1)
                    self.assertNotEqual(
                        _at(host.calls, ["gh", "pr", "edit", PR, "--base", "main"]), -1)
                    self.assertEqual(_at(host.calls, ["gh", "pr", "ready"]), -1, host.calls)
                    self.assertEqual(_at(host.calls, ["gh", "pr", "merge"]), -1, host.calls)
                    self.assertEqual(host.merged_into, [])
                    self.assertIn("NOT merged", err)
                    self.assertIn(f"`{OLD}`", err)
                    self.assertIn("`main`", err)
                    self.assertIn("Retarget the PR to `main` on the host", err)
                    self.assertIn("may edit it", err)
                    self.assertNotIn("Repo + branch target", err)

    # (d) + (c): a PR already on its target base makes no edit.
    def test_pr_on_its_target_base_makes_no_edit(self) -> None:
        for requires in REQUIRES:
            with self.subTest(merge_requires=requires):
                cfg = _cfg(self.tmp / requires, merge_requires=requires)
                host = _Host("main")
                rc, err = self._run(cfg, host, self._bundle(cfg, "500"))
                self.assertEqual(rc, 0, err)
                self.assertEqual(_at(host.calls, ["gh", "pr", "edit"]), -1, host.calls)
                self.assertEqual(host.merged_into, ["main"])

    # (e) + (c): a target base that a sibling bundle of this batch published (#411 route 2)
    # is refused before the host is asked anything, whether or not the host already reports
    # it as the PR's base, with the same way out as publish's own #411 refusal.
    def test_batch_produced_target_is_refused_before_any_gh_call(self) -> None:
        for host_base in ("feat/sibling", "main"):
            for requires in REQUIRES:
                with self.subTest(host_base=host_base, merge_requires=requires):
                    cfg = _cfg(self.tmp / host_base.replace("/", "-") / requires,
                               merge_requires=requires)
                    self._bundle(cfg, "499", record={
                        "mode": "new-pr", "branch": "feat/sibling", "repo": "org/repo",
                        "pr_url": "https://github.com/org/repo/pull/6"})
                    d = self._bundle(cfg, "500", target="feat/sibling")
                    host = _Host(host_base)
                    rc, err = self._run(cfg, host, d)
                    self.assertEqual(rc, 1)
                    self.assertEqual([c for c in host.calls if c[:2] == ["gh", "pr"]], [])
                    self.assertEqual(host.merged_into, [])
                    self.assertIn("issue_499", err)
                    self.assertIn("`feat/sibling`", err)
                    said = publish._merge_base_refusal(cfg, d, "org/repo", "feat/sibling",
                                                       "feat/sibling")
                    self.assertIn("issue_499", said)
                    self.assertIn(said.split("not a shared base. ", 1)[1].rstrip("."), err)

    # (f) + (c): a malformed expected_checks is refused first: no edit for a wrong-based PR.
    def test_malformed_expected_checks_is_refused_before_any_edit(self) -> None:
        for requires in REQUIRES:
            with self.subTest(merge_requires=requires):
                cfg = _cfg(self.tmp / requires, merge_requires=requires,
                           expected_checks_error="[driver].expected_checks is not a list")
                host = _Host(OLD)
                rc, err = self._run(cfg, host, self._bundle(cfg, "500"))
                self.assertEqual(rc, 1)
                self.assertIn("expected_checks is not a list", err)
                self.assertEqual(_at(host.calls, ["gh", "pr", "edit"]), -1, host.calls)
                self.assertEqual(host.merged_into, [])

    # Under "all", the base can be edited on the host again after the driver checked it.
    # Every later read holds it to the target: a base moved while the checks are read, or
    # right after the ready-mark, is refused at that read with the ready-mark undone —
    # never merged into, and never brought up to date against.
    def test_base_edited_on_the_host_after_the_check_is_never_merged_into(self) -> None:
        cases = {
            "retargeted then moved during the rollup wait": (OLD, ("checks", 1)),
            "retargeted then moved after the ready-mark": (OLD, ("ready", 1)),
            "on its target then moved during the rollup wait": ("main", ("checks", 1)),
        }
        for n, (name, (start, when)) in enumerate(cases.items()):
            with self.subTest(name):
                cfg = _cfg(self.tmp / f"race{n}")
                host = _Host(start, moves={when: "fix/elsewhere"})
                rc, err = self._run(cfg, host, self._bundle(cfg, "500"))
                self.assertEqual(rc, 1, err)
                self.assertEqual(host.merged_into, [])
                self.assertEqual(_at(host.calls, ["gh", "pr", "merge"]), -1, host.calls)
                self.assertEqual(_at(host.calls, ["gh", "pr", "update-branch"]), -1)
                self.assertIn(["gh", "pr", "ready", PR, "--undo"], host.calls)
                self.assertIn("`fix/elsewhere`", err)
                self.assertIn("`main`", err)

    # With no target to hold the PR to, it is refused untouched rather than merged blind.
    def test_bundle_with_no_target_base_is_refused_untouched(self) -> None:
        for brief in (None, "- **Slug:** no-target\n"):
            with self.subTest(brief=brief):
                cfg = _cfg(self.tmp / ("none" if brief is None else "empty"))
                d = self._bundle(cfg, "500")
                if brief is None:
                    (d / "brief.md").unlink()
                else:
                    (d / "brief.md").write_text(brief, encoding="utf-8")
                host = _Host(OLD)
                rc, err = self._run(cfg, host, d)
                self.assertEqual(rc, 1)
                self.assertEqual([c for c in host.calls if c[:2] == ["gh", "pr"]], [])
                self.assertIn("no target base", err)


if __name__ == "__main__":
    unittest.main()
