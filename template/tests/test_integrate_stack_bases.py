"""Stack mode lands every wave on the target (#593). Before: every fold rebuilt
`pdca-integration/<base>` from the base, re-applying each `patch.diff` as a NEW commit,
and force-pushed (a wave-1 PR `--base`d on it found its own change there as a different
commit); and a wave>0 own-repo PR targeted that branch, which never reaches the target.

These cases pin the fix: the fold MERGES each accepted bundle's published PR branch (its
own commits, same SHAs), append-only within a run and unforced after the run's first fold;
a branch that moved since the last fold has its new tip merged; a merged prerequisite whose
branch is gone is judged by the head commit its PR merged with — skipped when the line has
that commit, else carried in through the base once the base is checked to have it (a PR
merged into another branch stops the fold, and so does an `Onto branch` record), with the
base fetched last so its snapshot is never older than a deletion the fold sees; a
line another run moved is refused; a wave>0 PR targets the real base and is cut from the
line commit recorded for it, and a late publish refuses a recorded commit the line no
longer holds. They also pin the diff a dependent PR shows once the earlier waves merge —
its own change for a plain chain, and, where the line joined histories the base got
separately (two branches of one wave; a base that moved before the first fold), several
merge bases until "Update branch". Real git against a bare ``origin`` + a primary checkout
(the ``FoldGit`` shape of ``tests/test_integrate.py``), plus offline cases; ``gh`` is
always patched.

Only modules are imported (never a symbol the fix adds), and the new ``merged.merged_head``
is patched with ``create=True``, so with the production change reverted these cases still
load and run — and fail.
    PYTHONPATH=src python -m unittest tests.test_integrate_stack_bases
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from pdca_harness import integrate, merged, publish, signoff
from pdca_harness.config import Config, LeafConfig, _normalize_host_ci

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
TARGET = ("org/repo", "main")
LINE = "pdca-integration/main"
# A well-formed commit id no test repository ever holds.
ABSENT = "0123456789abcdef0123456789abcdef01234567"


def _cfg(root: Path, primary: Path, *, base_remote: str = "origin") -> Config:
    return Config(
        root=root, bundle_root=root / "results", process_dir=root / "process",
        templates_dir=TEMPLATES, default_branch="main", tracker_system="github",
        tracker_url="", issue_id_example="#1",
        builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
        publisher=LeafConfig(mode="stub", interactive=True), gates_checks=[],
        base_remote=base_remote, repo_checkouts={"org/repo": str(primary)})


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _git(repo: Path, *args: str) -> str:
    r = _run(repo, *args)
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {repo}: {r.stderr}")
    return r.stdout


def _identity(repo: Path) -> None:
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Tester")
    _git(repo, "config", "commit.gpgsign", "false")
    # Pin the host-independent default, so the fold's own `--prune` is what is tested.
    _git(repo, "config", "fetch.prune", "false")


def _brief(d: Path, *, extra: str = "") -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "brief.md").write_text(
        f"- **Slug:** {d.name.removeprefix('issue_').lower()}\n"
        f"- **Repo + branch target:** org/repo @ main\n{extra}", encoding="utf-8")


def _new_file_patch(name: str, text: str) -> str:
    return (f"diff --git a/{name} b/{name}\nnew file mode 100644\n--- /dev/null\n"
            f"+++ b/{name}\n@@ -0,0 +1 @@\n+{text}\n")


def _accept(d: Path) -> None:
    """Make bundle ``d`` COMPLETE (accepted at sign-off) — publish's precondition."""
    (d / "check-gates.json").write_text("{}", encoding="utf-8")
    shutil.copyfile(TEMPLATES / "SUMMARY.md.tpl", d / "SUMMARY.md")
    signoff.record(d / "SUMMARY.md", action="accept", by="Tester", date="2026-10-03")


@contextlib.contextmanager
def _merged_heads(heads: dict[str, str | None]):
    """The host's answers about the bundles' PRs: ``heads`` maps a bundle id to the head
    commit its PR merged with (absent / None: not merged). Patches the new
    ``merged.merged_head`` (``create=True``: absent before the fix) and, consistently,
    ``merged.is_merged`` — so a fold that asks only "is it merged?" is judged on the same
    facts. Yields the ``merged_head`` mock."""
    with mock.patch.object(merged, "merged_head", create=True,
                           side_effect=lambda _cfg, iid: heads.get(iid)) as head, \
            mock.patch.object(merged, "is_merged",
                              side_effect=lambda _cfg, iid: bool(heads.get(iid))):
        yield head


class StackFoldGit(unittest.TestCase):
    """Real git. A scratch clone (``human``) plays the people outside the harness: the
    maintainer merging PRs, a reviewer pushing a fixup, a foreign run, an existing PR."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        self.primary = self.tmp / "repo"
        subprocess.run(["git", "init", "--bare", "-q", str(self.origin)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.primary)], check=True)
        _identity(self.primary)
        (self.primary / "base.txt").write_text("base\n", encoding="utf-8")
        _git(self.primary, "add", "-A")
        _git(self.primary, "commit", "-q", "-m", "base")
        _git(self.primary, "remote", "add", "origin", str(self.origin))
        _git(self.primary, "push", "-q", "origin", "main")
        self.human = self.tmp / "human"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.human)],
                       check=True, capture_output=True)
        _identity(self.human)
        self.cfg = _cfg(self.tmp, self.primary)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -------------------------------------------------------------------------

    def _publish(self, iid: str, files: dict[str, str], *, cut_from: str = "origin/main") -> Path:
        """What `publish` does for an accepted bundle: cut ``fix/<iid>`` off ``cut_from``
        in the primary checkout, commit the change signed off, push it to origin, and
        record it in publish.json. The bundle's patch.diff is that commit's diff (so the
        pre-fix fold, which re-applied patch.diff, can still apply it)."""
        d = self.cfg.bundle(iid)
        _brief(d)
        branch = f"fix/{iid}"
        _git(self.primary, "fetch", "-q", "origin")
        _git(self.primary, "checkout", "-q", "-B", branch, cut_from)
        for name, text in files.items():
            (self.primary / name).write_text(text, encoding="utf-8")
        _git(self.primary, "add", "--all")
        _git(self.primary, "commit", "-q", "-s", "-m", f"fix {iid}")
        (d / "patch.diff").write_text(_git(self.primary, "diff", "HEAD~1", "HEAD"),
                                      encoding="utf-8")
        _git(self.primary, "push", "-q", "origin", branch)
        _git(self.primary, "checkout", "-q", "main")
        mode = "new-pr" if cut_from == "origin/main" else "stacked-pr"
        (d / "publish.json").write_text(json.dumps(
            {"mode": mode, "branch": branch, "base": "main", "repo": "org/repo",
             "pr_url": f"https://example.test/pr/{iid}"}), encoding="utf-8")
        return d

    def _push_from_human(self, files: dict[str, str], *, to: str, url: str = "",
                         off: str = "origin/main", force: bool = False) -> str:
        """Commit ``files`` off ``off`` in the scratch clone and push it to branch ``to``
        on ``url`` (origin by default); return the pushed commit."""
        _git(self.human, "fetch", "-q", "origin")
        _git(self.human, "checkout", "-q", "-B", "scratch", off)
        for name, text in files.items():
            (self.human / name).write_text(text, encoding="utf-8")
        _git(self.human, "add", "--all")
        _git(self.human, "commit", "-q", "-s", "-m", f"commit for {to}")
        _git(self.human, "push", "-q", *(["--force"] if force else []),
             url or "origin", f"HEAD:refs/heads/{to}")
        return _git(self.human, "rev-parse", "HEAD").strip()

    def _merge_pr(self, iid: str, *, into: str = "main") -> None:
        """The maintainer merges ``fix/<iid>``'s PR into ``into`` with a merge commit."""
        _git(self.human, "fetch", "-q", "origin")
        _git(self.human, "checkout", "-q", "-B", into, f"origin/{into}")
        _git(self.human, "merge", "-q", "--no-ff", "--no-edit", f"origin/fix/{iid}")
        _git(self.human, "push", "-q", "origin", into)

    def _delete(self, *branches: str) -> None:
        """"Delete branch" on the host (what a merged PR's branch commonly gets)."""
        _git(self.origin, "branch", "-D", *branches)

    def _update_branch(self, iid: str) -> None:
        """GitHub's "Update branch": merge the base into ``fix/<iid>``."""
        _git(self.human, "fetch", "-q", "origin")
        _git(self.human, "checkout", "-q", "-B", f"fix/{iid}", f"origin/fix/{iid}")
        _git(self.human, "merge", "-q", "--no-ff", "--no-edit", "origin/main")
        _git(self.human, "push", "-q", "origin", f"fix/{iid}")

    def _tip(self, branch: str) -> str:
        return _git(self.origin, "rev-parse", f"refs/heads/{branch}").strip()

    def _has(self, branch: str) -> bool:
        return _run(self.origin, "rev-parse", "--verify", "--quiet",
                    f"refs/heads/{branch}").returncode == 0

    def _ancestor(self, commit: str, of: str) -> bool:
        return _run(self.origin, "merge-base", "--is-ancestor", commit, of).returncode == 0

    def _show(self, rev: str, path: str) -> str:
        return _git(self.origin, "show", f"{rev}:{path}")

    def _diff_files(self, base: str, head: str) -> list[str]:
        """What the PR view shows: the three-dot diff of ``head`` against ``base``."""
        return _git(self.origin, "diff", "--name-only", f"{base}...{head}").split()

    def _pushes(self, fn) -> list[list[str]]:
        """The argument lists of every `git push` ``fn`` makes through ``integrate._git``."""
        pushes: list[list[str]] = []
        real = integrate._git

        def spy(repo: Path, *args: str) -> int:
            if args[:1] == ("push",):
                pushes.append(list(args))
            return real(repo, *args)

        with mock.patch.object(integrate, "_git", spy):
            fn()
        return pushes

    def _bundle(self, iid: str, files: dict[str, str]) -> Path:
        """An accepted bundle not yet published: a brief, a patch adding ``files``."""
        d = self.cfg.bundle(iid)
        _brief(d)
        (d / "patch.diff").write_text("".join(_new_file_patch(n, t.rstrip("\n"))
                                              for n, t in files.items()), encoding="utf-8")
        _accept(d)
        return d

    def _late_publish(self, d: Path) -> tuple[int, str]:
        """A real `pdca publish` of ``d`` (no PR opened, ``gh`` never called); returns its
        rc and stderr."""
        err = io.StringIO()
        with mock.patch.object(publish, "_warn_if_squash_only"), \
                redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = publish.publish(self.cfg, d.name.removeprefix("issue_"), open_pr=False,
                                 by="T", today="2026-10-03")
        return rc, err.getvalue()

    # -- (ii)/(iii) the line carries the PR commits, append-only ---------------------------

    def test_a_later_fold_appends_to_the_line_and_carries_the_pr_commits(self) -> None:
        # Case 1. On an origin refusing non-fast-forwards, the second fold must land, and
        # T1 plus both PRs' own commits must be in the line it pushed. The pre-fix fold
        # rebuilt the line from the base as new commits and force-pushed it: refused here,
        # unless the rebuilt commits come out byte-identical to T1's (same second) — and
        # either way the PR branches' own commits are not in its line.
        _git(self.origin, "config", "receive.denyNonFastForwards", "true")
        a = self._publish("A", {"a.txt": "a\n"})
        integrate.fold(self.cfg, [a])
        t1 = self._tip(LINE)
        b = self._publish("B", {"b.txt": "b\n"}, cut_from=f"origin/{LINE}")
        integrate.fold(self.cfg, [a, b])                     # no keyword: a direct caller
        tip = self._tip(LINE)
        self.assertTrue(self._ancestor(t1, tip), "the line was rewritten, not appended to")
        self.assertTrue(self._ancestor(self._tip("fix/A"), tip))   # A's own commit
        self.assertTrue(self._ancestor(self._tip("fix/B"), tip))   # B's own commit

    def test_a_continuing_fold_pushes_without_force(self) -> None:
        # Case 2. receive.denyNonFastForwards ACCEPTS a forced push that happens to
        # fast-forward, so the flag itself is what this pins.
        a = self._publish("A", {"a.txt": "a\n"})
        integrate.fold(self.cfg, [a])
        b = self._publish("B", {"b.txt": "b\n"}, cut_from=f"origin/{LINE}")
        pushes = self._pushes(lambda: integrate.fold(self.cfg, [a, b]))
        self.assertEqual(len(pushes), 1, pushes)
        self.assertEqual([x for x in pushes[0] if x.startswith("--force")], [], pushes[0])

    def test_a_listed_target_continues_this_runs_tip_without_force(self) -> None:
        _git(self.origin, "config", "receive.denyNonFastForwards", "true")
        a = self._publish("A", {"a.txt": "a\n"})
        integrate.fold(self.cfg, [a], folded_this_run={})
        t1 = self._tip(LINE)
        b = self._publish("B", {"b.txt": "b\n"}, cut_from=f"origin/{LINE}")
        pushes = self._pushes(
            lambda: integrate.fold(self.cfg, [a, b], folded_this_run={TARGET: t1}))
        self.assertEqual([x for x in pushes[0] if x.startswith("--force")], [], pushes[0])
        tip = self._tip(LINE)
        self.assertTrue(self._ancestor(t1, tip))
        self.assertTrue(self._ancestor(self._tip("fix/B"), tip))
        # A branch already in the line is not merged again: one new merge commit, B's.
        self.assertEqual(_git(self.origin, "rev-list", "--count", f"{t1}..{tip}").strip(),
                         "2")                                # B's commit + its merge

    def test_a_runs_first_fold_starts_fresh_over_an_earlier_runs_line(self) -> None:
        # Case 3.
        old = self._push_from_human({"old.txt": "old\n"}, to=LINE)     # an earlier run's
        a = self._publish("A", {"a.txt": "a\n"})
        pushes = self._pushes(lambda: integrate.fold(self.cfg, [a], folded_this_run={}))
        tip = self._tip(LINE)
        self.assertFalse(self._ancestor(old, tip))           # replaced, not built on
        self.assertTrue(self._ancestor(self._tip("main"), tip))      # off origin/main
        self.assertTrue(self._ancestor(self._tip("fix/A"), tip))
        self.assertIn("--force", pushes[0])                  # replacing a line is forced

    def test_a_line_another_run_moved_is_refused(self) -> None:
        # Case 4.
        a = self._publish("A", {"a.txt": "a\n"})
        integrate.fold(self.cfg, [a], folded_this_run={})
        t1 = self._tip(LINE)
        other = self._push_from_human({"other.txt": "x\n"}, to=LINE, force=True)  # #591
        b = self._publish("B", {"b.txt": "b\n"})
        with self.assertRaises(integrate.IntegrationError) as ctx:
            integrate.fold(self.cfg, [a, b], folded_this_run={TARGET: t1})
        msg = str(ctx.exception)
        for part in (LINE, t1, other, "#591"):
            self.assertIn(part, msg)
        self.assertEqual(self._tip(LINE), other)             # origin untouched

    # -- (ii) record-mode resolution ------------------------------------------------------

    def test_an_onto_branch_record_merges_that_remotes_branch_not_origins(self) -> None:
        # Case 5.
        upstream = self.tmp / "upstream.git"
        subprocess.run(["git", "init", "--bare", "-q", str(upstream)], check=True)
        _git(self.primary, "remote", "add", "upstream", str(upstream))
        # The existing PR branch the fix was committed onto (`Onto branch: upstream/feat`)…
        _git(self.human, "checkout", "-q", "-B", "work", "origin/main")
        (self.human / "feat.txt").write_text("feat\n", encoding="utf-8")
        _git(self.human, "add", "--all")
        _git(self.human, "commit", "-q", "-s", "-m", "someone's PR")
        (self.human / "fix.txt").write_text("fix\n", encoding="utf-8")
        _git(self.human, "add", "--all")
        _git(self.human, "commit", "-q", "-s", "-m", "the fix, stacked onto it")
        patch = _git(self.human, "diff", "HEAD~1", "HEAD")
        _git(self.human, "push", "-q", str(upstream), "HEAD:refs/heads/feat")
        feat = _git(self.human, "rev-parse", "HEAD").strip()
        # …and an unrelated same-named branch on origin the fold must never look up.
        decoy = self._push_from_human({"decoy.txt": "decoy\n"}, to="feat")
        d = self.cfg.bundle("S")
        _brief(d, extra="- **Onto branch:** upstream/feat\n")
        (d / "patch.diff").write_text(patch, encoding="utf-8")
        (d / "publish.json").write_text(json.dumps(
            {"mode": "stacked", "branch": "feat", "base": "upstream/feat",
             "repo": "org/repo"}), encoding="utf-8")
        integrate.fold(self.cfg, [d])
        tip = self._tip(LINE)
        self.assertTrue(self._ancestor(feat, tip))           # the PR branch, all of it
        self.assertFalse(self._ancestor(decoy, tip))         # never origin's `feat`

    # -- (ii) commits pushed onto a PR branch after the fold carried it --------------------

    def test_a_fixup_pushed_onto_a_live_branch_is_merged_by_the_next_fold(self) -> None:
        # Case 8, branch kept: a reviewer pushes a fixup onto fix/A after the fold carried
        # it. The next fold merges fix/A's NEW tip, so the next wave builds on the fixup.
        a = self._publish("A", {"a.txt": "a\n"})
        integrate.fold(self.cfg, [a], folded_this_run={})
        t1 = self._tip(LINE)
        fixup = self._push_from_human({"a.txt": "a, after review\n"}, to="fix/A",
                                      off="origin/fix/A")
        b = self._publish("B", {"b.txt": "b\n"}, cut_from=f"origin/{LINE}")
        with _merged_heads({}) as asked:
            integrate.fold(self.cfg, [a, b], folded_this_run={TARGET: t1})
        asked.assert_not_called()                            # the branch is there: no host
        tip = self._tip(LINE)
        self.assertEqual(self._show(tip, "a.txt"), "a, after review\n")
        self.assertTrue(self._ancestor(fixup, tip))          # the fixup's own commit
        self.assertTrue(self._ancestor(t1, tip))

    def test_a_fixup_merged_with_its_deleted_branch_reaches_the_line(self) -> None:
        # Case 8, branch deleted (round 5 §6 item 4): the fixup lands on main with fix/A's
        # merge, and fix/A is deleted. The line carries A's ORIGINAL commit, not the head the
        # PR merged with, so the fixup's work must come in through the base — never "A is
        # carried already, skip it", which would drop the fixup.
        _git(self.origin, "config", "receive.denyNonFastForwards", "true")
        a = self._publish("A", {"a.txt": "a\n"})
        integrate.fold(self.cfg, [a], folded_this_run={})
        t1 = self._tip(LINE)
        fixup = self._push_from_human({"a.txt": "a, after review\n"}, to="fix/A",
                                      off="origin/fix/A")
        self._merge_pr("A")
        self._delete("fix/A")
        b = self._publish("B", {"b.txt": "b\n"}, cut_from=f"origin/{LINE}")
        err = io.StringIO()
        with _merged_heads({"A": fixup}), redirect_stderr(err):
            pushes = self._pushes(
                lambda: integrate.fold(self.cfg, [a, b], folded_this_run={TARGET: t1}))
        tip = self._tip(LINE)
        self.assertEqual(self._show(tip, "a.txt"), "a, after review\n")
        self.assertEqual(self._show(tip, "b.txt"), "b\n")
        self.assertTrue(self._ancestor(self._tip("main"), tip))   # through the base
        self.assertTrue(self._ancestor(t1, tip), "the line was rewritten, not appended to")
        self.assertEqual([x for p in pushes for x in p if x.startswith("--force")], [], pushes)
        self.assertIn(f"merged at {fixup[:12]}", err.getvalue())

    # -- (ii-b) a published branch that is gone, decided by commit --------------------------

    def test_merged_prerequisites_already_on_the_line_are_skipped_by_commit(self) -> None:
        # Case 7, the reviewer/adversary repro (round 5 §6 item 3). Waves {A} → {B dep A, C}.
        # A's and B's PRs merge with merge commits and their branches are deleted; then main
        # gets a c.txt that clashes with C, whose PR is still open. The line already has the
        # heads both PRs merged with, so the fold skips them and must NOT merge main in — it
        # would stop the run on a clash that has nothing to do with A or B. No commit
        # message, and no `main..HEAD` range, may decide this.
        a = self._publish("A", {"a.txt": "a\n"})
        integrate.fold(self.cfg, [a], folded_this_run={})
        t1 = self._tip(LINE)
        b = self._publish("B", {"b.txt": "b\n"}, cut_from=f"origin/{LINE}")
        c = self._publish("C", {"c.txt": "c\n"}, cut_from=f"origin/{LINE}")
        integrate.fold(self.cfg, [a, b, c], folded_this_run={TARGET: t1})
        t2 = self._tip(LINE)
        heads = {"A": self._tip("fix/A"), "B": self._tip("fix/B")}
        self._merge_pr("A")
        self._merge_pr("B")
        self._delete("fix/A", "fix/B")
        self._push_from_human({"c.txt": "someone else's c\n"}, to="main")
        err = io.StringIO()
        with _merged_heads(heads), redirect_stderr(err):
            folded = integrate.fold(self.cfg, [a, b, c], folded_this_run={TARGET: t2})
        self.assertEqual(folded[TARGET][0], LINE)
        self.assertEqual(self._tip(LINE), t2)                # nothing merged, base included
        self.assertFalse(self._ancestor(self._tip("main"), t2))
        log = err.getvalue()
        for iid in ("A", "B"):
            self.assertIn(f"issue_{iid}'s branch origin/fix/{iid} is gone and its PR merged "
                          f"at {heads[iid][:12]} — {LINE} already carries that commit", log)

    def test_a_merged_branch_the_line_never_carried_comes_in_through_the_base(self) -> None:
        # Case 9. A's PR merged and its branch was deleted before any fold of this run put
        # it on the line: the head it merged with is not on the line, so the line takes the
        # base in (appended, unforced) and A's work reaches the next wave.
        _git(self.origin, "config", "receive.denyNonFastForwards", "true")
        z = self._publish("Z", {"z.txt": "z\n"})
        integrate.fold(self.cfg, [z], folded_this_run={})
        t = self._tip(LINE)
        a = self._publish("A", {"a.txt": "a\n"})
        head = self._tip("fix/A")
        self._merge_pr("A")
        self._delete("fix/A")
        err = io.StringIO()
        with _merged_heads({"A": head}), redirect_stderr(err):
            pushes = self._pushes(
                lambda: integrate.fold(self.cfg, [z, a], folded_this_run={TARGET: t}))
        tip = self._tip(LINE)
        self.assertEqual(self._show(tip, "a.txt"), "a\n")
        self.assertTrue(self._ancestor(self._tip("main"), tip))
        self.assertTrue(self._ancestor(t, tip), "the line was rewritten, not appended to")
        self.assertEqual([x for p in pushes for x in p if x.startswith("--force")], [], pushes)
        self.assertIn(f"reaches {LINE} through origin/main", err.getvalue())

    def test_a_merged_head_missing_from_the_clone_is_not_carried(self) -> None:
        # Case 11. The host names a head commit this clone does not have, though the fold
        # fetched the base. That is "not on the line", not a git failure (git's exit 128
        # for a missing commit is never read as one). Nor can it be in the base, which the
        # fetch would have brought it with: the PR merged somewhere else (or was squashed
        # or rebased), so the fold stops rather than take the base in, and pushes nothing.
        z = self._publish("Z", {"z.txt": "z\n"})
        integrate.fold(self.cfg, [z], folded_this_run={})
        t = self._tip(LINE)
        a = self._publish("A", {"a.txt": "a\n"})
        self._merge_pr("A")
        self._delete("fix/A")
        self.assertNotEqual(_run(self.primary, "cat-file", "-e", ABSENT).returncode, 0)
        with _merged_heads({"A": ABSENT}):
            with self.assertRaises(integrate.IntegrationError) as ctx:
                integrate.fold(self.cfg, [z, a], folded_this_run={TARGET: t})
        self._assert_merged_elsewhere(ctx.exception, "A", ABSENT)
        self.assertNotIn("git step failed", str(ctx.exception))
        self.assertEqual(self._tip(LINE), t)                 # nothing pushed

    def _assert_merged_elsewhere(self, exc: Exception, iid: str, head: str) -> None:
        """The stop for a merged head the base does not have names the bundle, its PR, the
        head and the base."""
        for part in (f"issue_{iid}", f"https://example.test/pr/{iid}", head,
                     "merged somewhere other than origin/main"):
            self.assertIn(part, str(exc))

    def test_a_fixup_merged_into_another_branch_stops_the_fold(self) -> None:
        # Round 6 §6 item 3, the adversary repro (a): a fixup lands on fix/A after the fold
        # carried it, then A's PR is merged into `release`, not main (its base was edited on
        # the host), and fix/A is deleted. Main has neither the fixup nor the merge, so
        # taking main in would not carry them, and the next wave would build without the
        # fixup. The fold must stop, naming why, and push nothing.
        _git(self.origin, "branch", "release", "main")
        a = self._publish("A", {"a.txt": "a\n"})
        integrate.fold(self.cfg, [a], folded_this_run={})
        t1 = self._tip(LINE)
        fixup = self._push_from_human({"a.txt": "a, after review\n"}, to="fix/A",
                                      off="origin/fix/A")
        self._merge_pr("A", into="release")
        self._delete("fix/A")
        b = self._publish("B", {"b.txt": "b\n"}, cut_from=f"origin/{LINE}")
        with _merged_heads({"A": fixup}), redirect_stderr(io.StringIO()):
            with self.assertRaises(integrate.IntegrationError) as ctx:
                integrate.fold(self.cfg, [a, b], folded_this_run={TARGET: t1})
        self._assert_merged_elsewhere(ctx.exception, "A", fixup)
        self.assertEqual(self._tip(LINE), t1)                # nothing pushed

    def test_a_branch_never_carried_and_merged_into_another_branch_stops_the_fold(
            self) -> None:
        # The adversary repro (b), case 9's shape: A's PR merged before any fold carried it,
        # but into `release`. Taking main in would leave the line with none of A.
        _git(self.origin, "branch", "release", "main")
        z = self._publish("Z", {"z.txt": "z\n"})
        integrate.fold(self.cfg, [z], folded_this_run={})
        t = self._tip(LINE)
        a = self._publish("A", {"a.txt": "a\n"})
        head = self._tip("fix/A")
        self._merge_pr("A", into="release")
        self._delete("fix/A")
        with _merged_heads({"A": head}), redirect_stderr(io.StringIO()):
            with self.assertRaises(integrate.IntegrationError) as ctx:
                integrate.fold(self.cfg, [z, a], folded_this_run={TARGET: t})
        self._assert_merged_elsewhere(ctx.exception, "A", head)
        self.assertEqual(self._tip(LINE), t)                 # nothing pushed

    def test_a_failing_base_check_is_a_git_step_failure(self) -> None:
        # Before taking the base in, the fold asks git whether the base has the merged head.
        # Exit 1 means no; anything else is git failing, which must stop the fold as a
        # failed git step, never be read as yes or no.
        z = self._publish("Z", {"z.txt": "z\n"})
        integrate.fold(self.cfg, [z], folded_this_run={})
        t = self._tip(LINE)
        a = self._publish("A", {"a.txt": "a\n"})
        head = self._tip("fix/A")
        self._merge_pr("A")
        self._delete("fix/A")
        real = integrate._git

        def broken(repo: Path, *args: str) -> int:
            if args == ("merge-base", "--is-ancestor", head, "origin/main"):
                return 128                                   # git's fatal exit code
            return real(repo, *args)

        with _merged_heads({"A": head}), mock.patch.object(integrate, "_git", broken), \
                redirect_stderr(io.StringIO()):
            with self.assertRaises(integrate.IntegrationError) as ctx:
                integrate.fold(self.cfg, [z, a], folded_this_run={TARGET: t})
        msg = str(ctx.exception)
        self.assertIn("exited 128", msg)
        self.assertIn("git step failed", msg)
        self.assertNotIn("merged somewhere other than", msg)
        self.assertEqual(self._tip(LINE), t)                 # nothing pushed

    def test_a_pr_merged_and_deleted_between_the_folds_fetches_is_still_carried(
            self) -> None:
        # The fold fetches the base remote LAST. A fork: the base is upstream's main, the PR
        # branches live on origin. A's PR merges into upstream's main and fix/A is deleted
        # while the fold is between its fetches. Fetched first, the base would be a snapshot
        # from before that merge while fix/A already shows as gone, so the fold would take
        # in a base without A (or, checking it, stop for nothing). Fetched last, a branch
        # that shows as gone always comes with a base that has its merge; here fix/A still
        # resolves, so the fold merges it as usual.
        upstream = self.tmp / "upstream.git"
        subprocess.run(["git", "init", "--bare", "-q", str(upstream)], check=True)
        _git(self.primary, "remote", "add", "upstream", str(upstream))
        _git(self.primary, "push", "-q", "upstream", "main")
        _git(self.human, "remote", "add", "upstream", str(upstream))
        self.cfg.base_remote = "upstream"
        z = self._publish("Z", {"z.txt": "z\n"})
        integrate.fold(self.cfg, [z], folded_this_run={})
        t = self._tip(LINE)
        a = self._publish("A", {"a.txt": "a\n"})
        head = self._tip("fix/A")
        real, fetches = integrate._git, []

        def racing(repo: Path, *args: str) -> int:
            rc = real(repo, *args)
            if args[:1] == ("fetch",):
                fetches.append(args[-1])
                if len(fetches) == 1:                        # between the first and second
                    for step in (("fetch", "-q", "origin"), ("fetch", "-q", "upstream"),
                                 ("checkout", "-q", "-B", "main", "upstream/main"),
                                 ("merge", "-q", "--no-ff", "--no-edit", "origin/fix/A"),
                                 ("push", "-q", "upstream", "main")):
                        _git(self.human, *step)
                    self._delete("fix/A")
            return rc

        with _merged_heads({"A": head}) as asked, \
                mock.patch.object(integrate, "_git", racing), redirect_stderr(io.StringIO()):
            integrate.fold(self.cfg, [z, a], folded_this_run={TARGET: t})
        tip = self._tip(LINE)
        self.assertIn("a.txt", _git(self.origin, "ls-tree", "--name-only", tip).split(),
                      "the line went on without A's work")
        self.assertEqual(self._show(tip, "a.txt"), "a\n")
        self.assertTrue(self._ancestor(t, tip), "the line was rewritten, not appended to")
        self.assertEqual(fetches[-1], "upstream")            # the base remote last
        asked.assert_not_called()                            # fix/A resolved: no host

    def _gone(self, iid: str) -> Path:
        """A published bundle whose branch was then deleted on origin, while the primary
        still holds a stale tracking ref for it (the fold's pruning fetch must drop it)."""
        d = self._publish(iid, {f"{iid}.txt": "x\n"})
        _git(self.primary, "fetch", "-q", "origin")
        self._delete(f"fix/{iid}")
        return d

    def test_a_gone_branch_not_known_merged_stops_naming_the_ref(self) -> None:
        # Case 10. The host does not report the PR merged (open, or unreadable): stop.
        d = self._gone("H")
        with _merged_heads({"H": None}) as asked:
            with self.assertRaises(integrate.IntegrationError) as ctx:
                integrate.fold(self.cfg, [d])
        asked.assert_called_once_with(self.cfg, "H")
        self.assertIn("issue_H", str(ctx.exception))
        self.assertIn("origin/fix/H", str(ctx.exception))    # the ref actually looked up
        self.assertFalse(self._has(LINE))                    # nothing pushed

    def test_a_gone_branch_whose_pr_targeted_another_branch_is_not_taken_from_the_base(
            self) -> None:
        # Case 10, the same rule for a legacy `Stacks on:` PR: it targets its parent's
        # branch, not main, so a merge of it is not in main and merging main in would not
        # carry it. The fold stops, naming that branch, and pushes nothing.
        d = self._gone("L")
        rec = json.loads((d / "publish.json").read_text(encoding="utf-8"))
        rec.update(mode="stacked-pr", base="fix/P")
        (d / "publish.json").write_text(json.dumps(rec), encoding="utf-8")
        with _merged_heads({"L": ABSENT}):
            with self.assertRaises(integrate.IntegrationError) as ctx:
                integrate.fold(self.cfg, [d])
        self.assertIn("a PR against fix/P, not main", str(ctx.exception))
        self.assertFalse(self._has(LINE))                    # nothing pushed

    def test_a_gone_onto_branch_record_is_skipped_only_when_the_line_has_its_head(
            self) -> None:
        # Case 10. An `Onto branch` record's PR is someone else's PR, which may target
        # another base: merged does not put its work in THIS base. The line has its head:
        # skipped. A line without it (a new run's fresh line): the fold stops, naming it,
        # rather than claim the base carries it.
        upstream = self.tmp / "upstream.git"
        subprocess.run(["git", "init", "--bare", "-q", str(upstream)], check=True)
        _git(self.primary, "remote", "add", "upstream", str(upstream))
        feat = self._push_from_human({"feat.txt": "feat\n"}, to="feat", url=str(upstream))
        d = self.cfg.bundle("S")
        _brief(d, extra="- **Onto branch:** upstream/feat\n")
        (d / "patch.diff").write_text(_new_file_patch("feat.txt", "feat"), encoding="utf-8")
        (d / "publish.json").write_text(json.dumps(
            {"mode": "stacked", "branch": "feat", "base": "upstream/feat",
             "repo": "org/repo", "pr_url": "https://example.test/pr/S"}), encoding="utf-8")
        integrate.fold(self.cfg, [d], folded_this_run={})
        t1 = self._tip(LINE)
        _git(upstream, "branch", "-D", "feat")               # the other PR merged elsewhere
        err = io.StringIO()
        with _merged_heads({"S": feat}), redirect_stderr(err):
            integrate.fold(self.cfg, [d], folded_this_run={TARGET: t1})
            self.assertEqual(self._tip(LINE), t1)            # carried already: skipped
            self.assertIn("already carries that commit", err.getvalue())
            with self.assertRaises(integrate.IntegrationError) as ctx:
                integrate.fold(self.cfg, [d], folded_this_run={})    # a new run's line
        for part in ("issue_S", "upstream/feat", "Onto branch"):
            self.assertIn(part, str(ctx.exception))
        self.assertEqual(self._tip(LINE), t1)                # nothing pushed

    # -- (iv) a bundle with no published branch ---------------------------------------------

    def test_an_unpublished_bundle_stops_the_fold_before_any_git_step(self) -> None:
        # Case 6.
        a = self._publish("A", {"a.txt": "a\n"})
        u = self.cfg.bundle("U")
        _brief(u)
        (u / "patch.diff").write_text(_new_file_patch("u.txt", "u"), encoding="utf-8")
        with mock.patch("pdca_harness.integrate.subprocess.run") as run:
            with self.assertRaises(integrate.IntegrationError) as ctx:
                integrate.fold(self.cfg, [a, u])
        run.assert_not_called()
        self.assertIn("issue_U", str(ctx.exception))
        self.assertNotIn("issue_A", str(ctx.exception))
        # The flow's hold and the fold's refusal read ONE definition of "unpublished".
        self.assertEqual([d.name for d in integrate.unpublished([a, u])], ["issue_U"])

    # -- failures are named for what they are ----------------------------------------------

    def test_a_failing_ancestry_check_is_a_git_step_failure_not_an_overlap(self) -> None:
        # `git merge-base --is-ancestor` exits 1 for "not an ancestor" and 128 when git
        # itself fails. Only 1 may lead on to the merge; 128 must stop the fold as a failed
        # git step, never fall through and be misreported (or merged past) as an overlap.
        a = self._publish("A", {"a.txt": "a\n"})
        real = integrate._git

        def broken(repo: Path, *args: str) -> int:
            if args[:2] == ("merge-base", "--is-ancestor"):
                return 128                                   # git's fatal exit code
            return real(repo, *args)

        with mock.patch.object(integrate, "_git", broken):
            with self.assertRaises(integrate.IntegrationError) as ctx:
                integrate.fold(self.cfg, [a])
        msg = str(ctx.exception)
        self.assertIn("exited 128", msg)
        self.assertIn("git step failed", msg)
        self.assertNotIn("does not merge cleanly", msg)
        self.assertFalse(self._has(LINE))                    # nothing pushed

    def test_a_merge_that_fails_without_a_conflict_is_not_called_an_overlap(self) -> None:
        # A branch with no history in common with the line: `git merge` refuses, leaving no
        # conflicting path — a failed git step, not an overlap between two briefs.
        _git(self.human, "checkout", "-q", "--orphan", "stray")
        _git(self.human, "rm", "-rqf", "--ignore-unmatch", ".")
        (self.human / "x.txt").write_text("x\n", encoding="utf-8")
        _git(self.human, "add", "--all")
        _git(self.human, "commit", "-q", "-s", "-m", "unrelated history")
        _git(self.human, "push", "-q", "origin", "HEAD:refs/heads/fix/X")
        d = self.cfg.bundle("X")
        _brief(d)
        (d / "patch.diff").write_text(_new_file_patch("x.txt", "x"), encoding="utf-8")
        (d / "publish.json").write_text(json.dumps(
            {"mode": "new-pr", "branch": "fix/X", "base": "main", "repo": "org/repo"}),
            encoding="utf-8")
        with self.assertRaises(integrate.IntegrationError) as ctx:
            integrate.fold(self.cfg, [d])
        msg = str(ctx.exception)
        self.assertIn("issue_X", msg)
        self.assertIn("exited 128", msg)                     # git's own refusal, a die()
        self.assertIn("not an overlap", msg)
        self.assertNotIn("does not merge cleanly", msg)
        self.assertFalse(self._has(LINE))                    # nothing pushed

    def test_a_conflict_names_the_conflicting_paths(self) -> None:
        a = self._publish("A", {"base.txt": "one\n"})
        b = self._publish("B", {"base.txt": "two\n"})
        with self.assertRaises(integrate.IntegrationError) as ctx:
            integrate.fold(self.cfg, [a, b])
        msg = str(ctx.exception)
        self.assertIn("does not merge cleanly", msg)
        self.assertIn("conflicts in base.txt", msg)
        self.assertFalse(self._has(LINE))                    # nothing pushed

    # -- (v) the diff a dependent PR shows --------------------------------------------------

    def test_a_chain_dependents_diff_shrinks_once_its_prerequisite_merges(self) -> None:
        # Case 12.
        a = self._publish("A", {"a.txt": "a\n"})
        integrate.fold(self.cfg, [a])
        self._publish("B", {"b.txt": "b\n"}, cut_from=f"origin/{LINE}")
        self.assertEqual(self._diff_files("main", "fix/B"), ["a.txt", "b.txt"])  # A rides
        self._merge_pr("A")
        self.assertEqual(self._diff_files("main", "fix/B"), ["b.txt"])

    def test_a_dependent_on_two_siblings_shrinks_after_a_base_update(self) -> None:
        # Case 13.
        a = self._publish("A", {"a.txt": "a\n"})
        c = self._publish("C", {"c.txt": "c\n"})
        integrate.fold(self.cfg, [a, c])
        self._publish("B", {"b.txt": "b\n"}, cut_from=f"origin/{LINE}")
        self._merge_pr("A")
        self._merge_pr("C")
        # The documented limitation: the fold's merge commit joining A and C never reaches
        # main, so B has two merge bases with it (A's and C's own tips).
        bases = _git(self.origin, "merge-base", "--all", "main", "fix/B").split()
        self.assertEqual(sorted(bases), sorted([self._tip("fix/A"), self._tip("fix/C")]))
        # The documented remedy: update B's branch from the base.
        self._update_branch("B")
        self.assertEqual(self._diff_files("main", "fix/B"), ["b.txt"])

    def test_a_chain_on_a_moved_base_keeps_a_second_merge_base_until_updated(self) -> None:
        # Case 13. The base moves between A's publish and the run's first fold, so the fold
        # starts the line from the NEWER main and merges A (cut from the older one) onto it.
        # That joining merge commit never reaches main: once A's PR merges, B — a plain
        # chain on A alone — has two merge bases with main (the moved main and A's tip), and
        # its diff shows more than its own change. The documented exception, and its remedy.
        a = self._publish("A", {"a.txt": "a\n"})
        moved = self._push_from_human({"other.txt": "someone else's change\n"}, to="main")
        integrate.fold(self.cfg, [a])
        self._publish("B", {"b.txt": "b\n"}, cut_from=f"origin/{LINE}")
        self._merge_pr("A")
        bases = _git(self.origin, "merge-base", "--all", "main", "fix/B").split()
        self.assertEqual(sorted(bases), sorted([moved, self._tip("fix/A")]))
        shown = self._diff_files("main", "fix/B")
        self.assertIn("b.txt", shown)
        self.assertGreater(len(shown), 1, shown)              # more than B's own change
        # The documented remedy: update B's branch from the base.
        self._update_branch("B")
        self.assertEqual(self._diff_files("main", "fix/B"), ["b.txt"])

    # -- (i) the PR a wave>0 bundle opens ---------------------------------------------------

    def test_a_wave_bundle_publishes_against_the_real_base(self) -> None:
        # Case 14.
        a = self._publish("A", {"a.txt": "a\n"})
        integrate.fold(self.cfg, [a])
        w = self._bundle("W", {"w.txt": "w\n"})
        publish.write_stack_base(w, LINE)                     # a wave>0 bundle
        rc, err = self._late_publish(w)
        self.assertEqual(rc, 0, err)
        rec = json.loads((w / "publish.json").read_text(encoding="utf-8"))
        self.assertEqual((rec["mode"], rec["base"]), ("stacked-pr", "main"))
        # Still cut from the line, so it carries A — as A's own commit.
        self.assertTrue(self._ancestor(self._tip("fix/A"), self._tip(rec["branch"])))

    def _held_wave_then_two_more(self) -> tuple[Path, str]:
        """Waves {A} → {U, J} → {K}: U's publish failed, so it is held, while J and then K
        are folded onto the line. U's stack base records T1, the tip it was built on."""
        a = self._publish("A", {"a.txt": "a\n"})
        integrate.fold(self.cfg, [a], folded_this_run={})
        t1 = self._tip(LINE)
        u = self._bundle("U", {"u.txt": "u\n"})
        publish.write_stack_base(u, LINE, t1)
        j = self._publish("J", {"j.txt": "j\n"}, cut_from=f"origin/{LINE}")
        integrate.fold(self.cfg, [a, j], folded_this_run={TARGET: t1})
        t2 = self._tip(LINE)
        k = self._publish("K", {"k.txt": "k\n"}, cut_from=f"origin/{LINE}")
        integrate.fold(self.cfg, [a, j, k], folded_this_run={TARGET: t2})
        self.assertTrue(self._ancestor(self._tip("fix/K"), LINE))   # the line grew past U
        return u, t1

    def test_a_late_publish_is_cut_from_the_recorded_line_tip(self) -> None:
        # Case 14: published after the line grew, U's PR branch is cut from T1, so its PR
        # against main carries none of J's or K's work.
        u, t1 = self._held_wave_then_two_more()
        rc, err = self._late_publish(u)
        self.assertEqual(rc, 0, err)
        rec = json.loads((u / "publish.json").read_text(encoding="utf-8"))
        self.assertEqual((rec["mode"], rec["base"]), ("stacked-pr", "main"))
        self.assertEqual(self._tip(rec["branch"] + "~1"), t1)
        self.assertFalse(self._ancestor(self._tip("fix/K"), rec["branch"]))
        self.assertFalse(self._ancestor(self._tip("fix/J"), rec["branch"]))
        self.assertEqual(self._diff_files("main", rec["branch"]), ["a.txt", "u.txt"])

    def test_a_late_publish_through_host_ci_pins_the_recorded_line_tip(self) -> None:
        # Case 14b: with `[gates] host_ci` declared, publish fetches and PINS the commit it
        # certifies, then rebuilds its `checkout -B` on that pinned commit. The recorded tip
        # is what must be pinned, certified and built on — never the line as it is now.
        self.cfg.host_ci_checks = _normalize_host_ci(["true"])
        u, t1 = self._held_wave_then_two_more()
        rc, err = self._late_publish(u)
        self.assertEqual(rc, 0, err)
        rec = json.loads((u / "publish.json").read_text(encoding="utf-8"))
        self.assertEqual(self._tip(rec["branch"] + "~1"), t1)
        self.assertFalse(self._ancestor(self._tip("fix/K"), rec["branch"]))
        self.assertEqual(rec["host_ci_base"], t1)

    def test_a_late_publish_refuses_a_tip_the_line_no_longer_holds(self) -> None:
        # Case 15: a later run's first fold has force-pushed a fresh line (one that does not
        # hold U's recorded tip). Publishing U must refuse, push nothing, and send the human
        # to re-drive it (#616) — never cut a PR from an orphaned commit.
        a = self._publish("A", {"a.txt": "a\n"})
        integrate.fold(self.cfg, [a], folded_this_run={})
        t1 = self._tip(LINE)
        u = self._bundle("U", {"u.txt": "u\n"})
        publish.write_stack_base(u, LINE, t1)
        self._push_from_human({"later.txt": "a later run\n"}, to=LINE, force=True)
        self.assertFalse(self._ancestor(t1, LINE))
        rc, err = self._late_publish(u)
        self.assertNotEqual(rc, 0)
        for part in ("issue_U", t1, LINE, "#616", "re-drive it in a new run"):
            self.assertIn(part.lower(), err.lower())
        self.assertFalse(self._has("fix/U-u"))               # nothing pushed
        self.assertFalse((u / "publish.json").exists())
        # The same when the line is gone altogether.
        self._delete(LINE)
        rc, err = self._late_publish(u)
        self.assertNotEqual(rc, 0)
        self.assertIn("#616", err)
        self.assertFalse(self._has("fix/U-u"))


class StackPublishDryRun(unittest.TestCase):
    """(i) offline: the `--base` a stacked PR gets, from `publish --dry-run`."""

    _FIX = "- **Slug:** my-fix\n- **Repo + branch target:** example-org/example-repo @ main\n"

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp, self.tmp / "example-repo")
        self.cfg.repo_checkouts = {"example-org/example-repo": str(self.tmp / "example-repo")}

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _plan(self, *, base_remote: str = "origin", stacks_on: bool = False,
              wave: bool = True, tip: str = "") -> str:
        self.cfg.base_remote = base_remote
        parent = self.cfg.bundle("PARENT")
        parent.mkdir(parents=True, exist_ok=True)
        (parent / "publish.json").write_text(json.dumps({"branch": "fix/PARENT-my-fix"}),
                                             encoding="utf-8")
        d = self.cfg.bundle("DEP")
        d.mkdir(parents=True)
        (d / "brief.md").write_text(self._FIX + ("- **Stacks on:** PARENT\n" if stacks_on
                                                 else ""), encoding="utf-8")
        (d / "patch.diff").write_text("diff --git a/x b/x\n", encoding="utf-8")
        _accept(d)
        if wave:
            publish.write_stack_base(d, LINE, *([tip] if tip else []))
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            rc = publish.publish(self.cfg, "DEP", dry_run=True, by="T", today="2026-10-03")
        self.assertEqual(rc, 0)
        return out.getvalue()

    def test_an_own_repo_wave_pr_targets_the_real_base(self) -> None:
        out = self._plan()
        self.assertIn(f"checkout -B fix/DEP-my-fix origin/{LINE}", out)   # cut from the line
        self.assertIn("--base main", out)
        self.assertNotIn(f"--base {LINE}", out)

    def test_a_recorded_stack_base_wins_over_a_stacks_on_parent(self) -> None:
        out = self._plan(stacks_on=True)
        self.assertIn(f"checkout -B fix/DEP-my-fix origin/{LINE}", out)
        self.assertIn("--base main", out)
        self.assertNotIn("--base fix/PARENT-my-fix", out)

    def test_a_legacy_stacks_on_parent_keeps_its_branch_base(self) -> None:
        out = self._plan(stacks_on=True, wave=False)
        self.assertIn("--base fix/PARENT-my-fix", out)

    def test_the_fork_path_is_unchanged(self) -> None:
        out = self._plan(base_remote="upstream")
        self.assertIn(f"checkout -B fix/DEP-my-fix origin/{LINE}", out)
        self.assertIn("--base main", out)
        self.assertIn("fork: cumulative diff vs base", out)

    def test_a_recorded_tip_is_the_cut_point_and_the_plan_shows_its_guard(self) -> None:
        out = self._plan(tip=ABSENT)
        self.assertIn(f"checkout -B fix/DEP-my-fix {ABSENT}", out)
        self.assertIn("--base main", out)
        self.assertIn(f"recorded tip {ABSENT} is still on origin/{LINE}", out)
        self.assertIn("#616", out)


class StackBaseTip(unittest.TestCase):
    """Case 14c, offline: the line tip lives in its own file beside a one-line `stack-base`,
    so `read_stack_base` (and `$PDCA_VERIFY_BASE`) still read just the branch."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.d = self.tmp / "issue_T"
        self.d.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_read_and_clear(self) -> None:
        base, tip = self.d / "stack-base", self.d / "stack-base-tip"
        publish.write_stack_base(self.d, LINE, ABSENT)
        self.assertEqual(base.read_text(encoding="utf-8"), LINE + "\n")   # one line, as ever
        self.assertEqual(tip.read_text(encoding="utf-8"), ABSENT + "\n")
        self.assertEqual(publish.read_stack_base(self.d), LINE)
        self.assertEqual(publish.read_stack_base_tip(self.d), ABSENT)
        publish.write_stack_base(self.d, LINE)               # no tip (a dry-run fold)
        self.assertFalse(tip.exists())                       # the earlier tip goes
        self.assertEqual(publish.read_stack_base_tip(self.d), "")
        publish.write_stack_base(self.d, LINE, ABSENT)
        publish.clear_stack_base(self.d)
        self.assertFalse(base.exists())
        self.assertFalse(tip.exists())
        tip.write_text(ABSENT + "\n", encoding="utf-8")      # a tip with no stack-base…
        self.assertEqual(publish.read_stack_base_tip(self.d), "")   # …is ignored


class StackFoldDryRun(unittest.TestCase):
    """Case 19 / (vii), offline: a dry-run fold prints the real plan and shells nothing."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp, self.tmp / "repo")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bare(self, iid: str) -> Path:
        d = self.cfg.bundle(iid)
        _brief(d)
        (d / "patch.diff").write_text("diff --git a/f.py b/f.py\n@@ -1 +1 @@\n-x\n+y\n",
                                      encoding="utf-8")
        return d

    def _dry(self, bundles: list[Path], **kw) -> tuple[dict, str]:
        out = io.StringIO()
        with mock.patch("pdca_harness.integrate.subprocess.run") as run, redirect_stdout(out):
            folded = integrate.fold(self.cfg, bundles, dry_run=True, **kw)
        run.assert_not_called()
        return folded, out.getvalue()

    def test_an_unpublished_bundle_is_planned_as_its_would_be_branch(self) -> None:
        d = self._bare("D1")
        folded, out = self._dry([d])
        self.assertEqual(folded, {TARGET: (LINE, None)})
        would_be = publish._branch_name(self.cfg, d, publish._resolve_target(d)[2])
        self.assertIn(f"git merge --no-ff --signoff origin/{would_be}", out)
        self.assertIn(f"continue {LINE} if origin has it, else start fresh from origin/main",
                      out)

    def test_a_first_fold_starts_fresh_and_a_later_one_continues(self) -> None:
        d = self._bare("D2")
        _f, first = self._dry([d], folded_this_run={})
        self.assertIn(f"start {LINE} fresh from origin/main", first)
        self.assertIn(f"git push --force origin {LINE}", first)
        _f, later = self._dry([d], folded_this_run={TARGET: None})
        self.assertIn(f"continue {LINE} from this run's tip", later)
        self.assertIn(f"git push origin {LINE}", later)
        self.assertNotIn("--force", later)

    def test_a_recorded_branch_is_what_the_plan_merges(self) -> None:
        d = self._bare("D3")
        (d / "publish.json").write_text(json.dumps(
            {"mode": "stacked", "branch": "feat", "base": "upstream/feat"}), encoding="utf-8")
        _f, out = self._dry([d])
        self.assertIn("git merge --no-ff --signoff upstream/feat", out)
        self.assertNotIn("origin/feat", out)


class UnpublishedSet(unittest.TestCase):
    """(iv) offline: which accepted bundles the flow must hold out of the fold. A record on
    disk is not proof of THIS run's publish (an iterate keeps publish.json), so only the
    bundles the caller reports as pushed this run count — and only a bundle the fold would
    carry at all (a patch and a usable target) can be held."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp, self.tmp / "repo")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundle(self, iid: str, *, record: bool, patch: bool = True,
                target: bool = True) -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text(
            f"- **Slug:** {iid.lower()}\n"
            + ("- **Repo + branch target:** org/repo @ main\n" if target else ""),
            encoding="utf-8")
        if patch:
            (d / "patch.diff").write_text("diff --git a/f.py b/f.py\n", encoding="utf-8")
        if record:
            (d / "publish.json").write_text(json.dumps(
                {"mode": "new-pr", "branch": f"fix/{iid}", "base": "main"}), encoding="utf-8")
        return d

    def test_only_a_branch_pushed_this_run_is_folded(self) -> None:
        bundles = [self._bundle("OK", record=True),          # pushed this run: folded
                   self._bundle("OLD", record=True),         # an earlier run's record only
                   self._bundle("NONE", record=False),       # no record at all
                   self._bundle("NOTGT", record=False, target=False),   # no target
                   self._bundle("NOPATCH", record=False, patch=False)]  # close / no-fix
        held = integrate.unpublished(bundles, pushed={"issue_OK"})
        self.assertEqual([d.name for d in held], ["issue_OLD", "issue_NONE"])
        # Without the run's pushed set, the earlier record would pass for a publish.
        self.assertEqual([d.name for d in integrate.unpublished(bundles)], ["issue_NONE"])

    def test_a_stacked_record_must_name_its_remote_branch(self) -> None:
        # An `Onto branch` record's `base` is `<remote>/<branch>` (publish._publish_stacked);
        # any other names no remote to fetch from: no branch on record, never a guess.
        good = self._bundle("GOOD", record=False)
        (good / "publish.json").write_text(json.dumps(
            {"mode": "stacked", "branch": "feat", "base": "upstream/feat"}), encoding="utf-8")
        bad = self._bundle("BAD", record=False)
        (bad / "publish.json").write_text(json.dumps(
            {"mode": "stacked", "branch": "feat", "base": "upstream/other"}), encoding="utf-8")
        self.assertEqual([d.name for d in integrate.unpublished([good, bad])], ["issue_BAD"])


class MergedHead(unittest.TestCase):
    """Case 20, offline: `merged.merged_head` reads the merged PR's head commit from `gh`,
    fail-closed exactly where `merged.is_merged` is — which keeps its own call."""

    URL = "https://example.test/pr/7"

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp, self.tmp / "repo")
        self.d = self.cfg.bundle("P")
        _brief(self.d)
        (self.d / "patch.diff").write_text(_new_file_patch("p.txt", "p"), encoding="utf-8")
        (self.d / "publish.json").write_text(json.dumps(
            {"mode": "new-pr", "branch": "fix/P", "base": "main", "pr_url": self.URL}),
            encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _gh(self, stdout: str = "", rc: int = 0, **kw):
        done = subprocess.CompletedProcess(["gh"], rc, stdout=stdout, stderr="")
        return mock.patch("pdca_harness.merged.subprocess.run", return_value=done, **kw)

    def _head(self) -> str | None:
        with redirect_stderr(io.StringIO()):
            return merged.merged_head(self.cfg, "P")

    def test_a_merged_pr_gives_its_head_commit(self) -> None:
        with self._gh('{"state": "MERGED", "headRefOid": "abc"}') as run:
            self.assertEqual(self._head(), "abc")
        self.assertEqual(run.call_args.args[0],
                         ["gh", "pr", "view", self.URL, "--json", "state,headRefOid"])

    def test_anything_but_merged_is_none(self) -> None:
        for out, rc in (('{"state": "OPEN", "headRefOid": "abc"}', 0),
                        ('{"state": "CLOSED", "headRefOid": "abc"}', 0),
                        ('{"state": "MERGED"}', 0),                  # no head given
                        ('{"state": "MERGED", "headRefOid": "abc"}', 1),   # gh failed
                        ("not json", 0), ("[]", 0)):
            with self.subTest(out=out, rc=rc), self._gh(out, rc):
                self.assertIsNone(self._head())
        with self._gh(side_effect=FileNotFoundError("gh")):           # gh not installed
            self.assertIsNone(self._head())

    def test_no_recorded_pr_asks_nothing(self) -> None:
        (self.d / "publish.json").write_text(json.dumps({"branch": "fix/P"}),
                                             encoding="utf-8")
        with self._gh('{"state": "MERGED", "headRefOid": "abc"}') as run:
            self.assertIsNone(self._head())
        run.assert_not_called()

    def test_is_merged_keeps_its_own_call(self) -> None:
        _accept(self.d)
        with self._gh('{"state": "MERGED"}') as run:
            self.assertTrue(merged.is_merged(self.cfg, "P"))
        self.assertEqual(run.call_args.args[0], ["gh", "pr", "view", self.URL, "--json", "state"])


if __name__ == "__main__":
    unittest.main()
