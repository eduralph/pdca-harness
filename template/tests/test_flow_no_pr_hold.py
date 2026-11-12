"""An accepted bundle whose accepted patch is on no PR holds the run (#926).

Before #926 a run reported success while an accepted bundle's patch was on no PR: its branch
was pushed but ``gh pr create`` failed, or it was accepted and never pushed (its texts failed
T4), or it was rebuilt after its PR was opened and the rebuild was never published. The
stack fold's ``"unpushed"`` hold (#593) only ran at a non-final wave boundary and never read
the PR; a re-run of the same ``pdca flow <ids>`` printed ``already complete … rm -rf <dir>``,
pointing at deleting the accepted work. Now such a bundle is held ``"no-pr"`` at the end of
the run that accepted it, and again by every ``pdca flow`` that names it (or re-runs the
split parent that adopted it), and the hold line says to ``pdca publish`` it.

The target a record is judged against is the brief's, from the files publish reads: never
``[driver].wave_mode`` (a later switch moves no PR), and for a hand-declared ``Stacks on``
the branch the parent published, read wherever the parent is, archived included. With no
record of that branch the base is not known and the bundle is held for that, never judged
against the brief's own base. A record with no ``repo`` field (from before publish wrote
one) is this repo's. The patch is matched by every digest the record carries, a merge-mode
marker's included, and a ``patch.diff`` that cannot be read is held. A finished id held on
a re-run is not carried onto the run's line, so what depends on it waits. Each bundle is
read once a run, however many ways the run reaches it, and a record with no digest gets
one note a run, also when the run counts its merged PR for a dependent (#865's note). The
step a hold line gives is one that clears it: where publish would refuse the bundle as it
stands (a ``Stacks on`` parent archived to ``completed/``, merge mode's base guard, an
``Onto branch`` with no open PR) the line says what must come first, and where a PR is open
on the bundle's branch that the record does not name (one opened by hand, or an earlier
attempt's, which ``gh pr create`` will not duplicate) it says to put that PR's URL in the
record, since ``pdca publish`` cannot open a second one.

These cases drive the WHOLE CLI route, ``cli._flow``, on the ``StackFoldGit`` fixture of
tests/test_integrate_stack_bases.py (a bare ``origin`` and a primary checkout), loaded by
path as tests/test_flow_same_wave_conflict.py does. Only the build and sign-off are stubbed
(``flow._drive_wave`` writes a ``patch.diff`` and the two publish texts, then accepts), and
``gh`` is answered by a stub in front of ``subprocess.run``, which keeps the open PRs and,
as GitHub does, opens no second PR from a branch into the same base. Publish itself is NOT
stubbed: the publisher is in ``command`` mode, so ``flow._publish_bundle`` runs the real
``publish.publish`` — the real ``git push`` to the bare ``origin``, then the stubbed
``gh pr create``, which also fails for the ids in ``fail_create``. The exit code is the one
``cli._flow`` returns; the hold kind is read from the ``holds`` map ``cli._flow`` hands to
``flow.flow_ids`` / ``flow.flow_batch`` (and then to ``cli._results_rc``).

Only modules are imported (never a symbol the fix adds), so with the production change
reverted these cases still load and run — and fail on their assertions.
    cd template && PYTHONPATH=src python3 -m unittest tests.test_flow_no_pr_hold
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import io
import json
import subprocess
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pdca_harness import cli, driver, flow, leaves, publish, split, state

_SPEC = importlib.util.spec_from_file_location(
    "stack_bases_fixture", Path(__file__).with_name("test_integrate_stack_bases.py"))
sb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sb)

TODAY = "2026-11-11"
#: The words of the hold line that tell it apart from every other line naming the bundle
#: (publish's own "re-run `pdca publish <id>`" after a failed `gh pr create` included).
_NO_PR = "is on no PR against its target"
#: The #593 hold line, printed at a non-final wave's fold for an accepted bundle that pushed
#: no branch this run.
_UNPUSHED = "pushed no branch this run"
#: The `Onto branch` PR the `gh pr list` stub reports for `feature/x`.
ONTO_PR = "https://example.test/org/repo/pull/7"


class NoPrHold(unittest.TestCase):

    def setUp(self) -> None:
        self.fx = sb.StackFoldGit()          # the fixture's real-git setUp/tearDown and helpers
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.cfg = self.fx.cfg
        self.cfg.publisher = dataclasses.replace(self.cfg.publisher, mode="command")
        self.files: dict[str, dict[str, str]] = {}   # id → the files its build adds
        self.fail_create: set[str] = set()           # ids whose `gh pr create` fails
        self.fail_texts: set[str] = set()            # ids whose publish texts fail (draft/T4)
        self.gh_calls: list[list[str]] = []
        self.opened: list[str] = []                  # the PR urls `gh pr create` returned
        self.prs: dict[tuple[str, str], str] = {}    # (head branch, base) → its open PR's url
        self.onto_prs = {"feature/x": ONTO_PR}       # head → the open PR `gh pr list` finds
        self.merged_prs: set[str] = set()            # PR urls `gh pr view` reads MERGED
        self._real_run = subprocess.run
        # What `gh pr list` matches an `Onto branch` PR's head owner against (publish reads
        # it off origin's URL), read once here with the real `git`.
        self.owner = publish._fork_owner(self.fx.primary, "origin")

    # -- fixture ---------------------------------------------------------------------------

    def _plan(self, iid: str, files: dict[str, str], *, target: str = "org/repo @ main",
              extra: str = "") -> Path:
        """A briefed bundle (PLANNED) whose build adds ``files`` (none: a close/no-fix, an
        empty patch); ``target`` "" writes no `Repo + branch target` at all."""
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "brief.md").write_text(
            "- **Slug:** fix\n"
            + (f"- **Repo + branch target:** {target}\n" if target else "") + extra,
            encoding="utf-8")
        self.files[iid] = files
        return d

    def _patch(self, iid: str) -> str:
        return "".join(sb._new_file_patch(name, text)
                       for name, text in self.files[iid].items())

    def _build(self, d: Path) -> None:
        """What Do and sign-off leave for an accepted bundle: its patch, its two publish
        texts (drafted and T4-gated by the stubbed pre-pass), the accept."""
        iid = d.name.removeprefix("issue_")
        (d / "patch.diff").write_text(self._patch(iid), encoding="utf-8")
        (d / publish.COMMIT_MSG).write_text(f"Fix {iid}\n\nFixes #1\n", encoding="utf-8")
        (d / publish.PR_BODY).write_text(f"Fixes {iid}.\n\nFixes #1\n", encoding="utf-8")
        sb._accept(d)

    def _iterate(self, iid: str, files: dict[str, str]) -> None:
        """``signoff --iterate-do`` on an accepted bundle, as the driver applies it: the
        attempt is archived (``driver._archive_iteration``), the bundle is PLANNED again, and
        its ``publish.json`` stays. The rebuild will add ``files``."""
        d = self.cfg.bundle(iid)
        driver._archive_iteration(d, driver._next_iteration_no(d), include_brief=False)
        self.assertEqual(state.state(d), state.PLANNED)
        self.assertTrue((d / "publish.json").exists())
        self.files[iid] = files

    def _record(self, iid: str) -> dict:
        return json.loads((self.cfg.bundle(iid) / "publish.json").read_text(encoding="utf-8"))

    def _digest(self, iid: str) -> str:
        return hashlib.sha256((self.cfg.bundle(iid) / "patch.diff").read_bytes()).hexdigest()

    def _state(self, iid: str) -> str:
        return state.state(self.cfg.bundle(iid))

    def _legacy(self, d: Path) -> str:
        """Write the record an earlier publish of ``d`` left before publish recorded a patch
        digest (no ``patch_digest``), for a PR on the brief's target; returns that PR's url."""
        url = f"https://example.test/pr/{d.name.removeprefix('issue_')}"
        (d / "publish.json").write_text(json.dumps({
            "mode": "new-pr", "branch": f"fix/{d.name.removeprefix('issue_')}-fix",
            "pr_url": url, "base": "main", "repo": "org/repo", "by": "T",
            "date": "2026-01-02", "id_pending": False}), encoding="utf-8")
        return url

    def _notes(self, iid: str) -> list[str]:
        return [x for x in self.err.splitlines()
                if x.startswith("flow: note") and f"issue_{iid}'s" in x]

    def _open_by_hand(self, head: str, base: str) -> str:
        """A PR from ``head`` into ``base`` opened on the host by hand; returns its url."""
        url = self.prs[head, base] = f"https://example.test/org/repo/pull/{900 + len(self.prs)}"
        return url

    def _put_pr_url(self, iid: str, url: str) -> None:
        """The step a hold line gives for a PR open on the bundle's branch that its record does
        not name: that PR's url in publish.json's ``pr_url``, the rest of the record kept."""
        rec = self._record(iid)
        rec["pr_url"] = url
        (self.cfg.bundle(iid) / "publish.json").write_text(json.dumps(rec), encoding="utf-8")

    # -- stubs -----------------------------------------------------------------------------

    def _gh(self, args, *a, **kw):
        """``gh pr create`` opens a PR (stdout: its url) and keeps it open, unless one from
        the same head branch into the same base is open already — refused as ``gh`` refuses
        it, naming that PR — or the head is the branch of an id in ``fail_create``. ``gh pr
        list`` reports the open PR ``onto_prs`` names for its head (the `Onto branch` PR);
        ``gh pr view`` of a url in ``merged_prs`` reads MERGED, and any other ``gh`` call
        OPEN, with the head of a PR it opened as GitHub reports it: its branch's tip on
        origin (what a re-run's carry of a finished id reads, #646). Everything that is not
        ``gh`` runs for real."""
        if not (isinstance(args, list) and args[:1] == ["gh"]):
            return self._real_run(args, *a, **kw)
        self.gh_calls.append(list(args))
        if args[1:3] == ["pr", "create"]:
            label = args[args.index("--head") + 1]
            head, base = label.partition(":")[2], args[args.index("--base") + 1]
            if (head, base) in self.prs:
                return subprocess.CompletedProcess(
                    args, 1, "", f'a pull request for branch "{label}" into branch "{base}" '
                                 f'already exists:\n{self.prs[head, base]}\n')
            if any(head.startswith(f"fix/{iid}-") for iid in self.fail_create):
                return subprocess.CompletedProcess(
                    args, 1, "", "GraphQL: something went wrong (createPullRequest)")
            url = f"https://example.test/org/repo/pull/{100 + len(self.opened)}"
            self.prs[head, base] = url
            self.opened.append(url)
            return subprocess.CompletedProcess(args, 0, url + "\n", "")
        if args[1:3] == ["pr", "list"]:
            head = args[args.index("--head") + 1]
            prs = ([{"url": self.onto_prs[head], "number": 7, "headRefName": head,
                     "headRepositoryOwner": {"login": self.owner}}]
                   if head in self.onto_prs else [])
            return subprocess.CompletedProcess(args, 0, json.dumps(prs), "")
        view = args[1:3] == ["pr", "view"]
        merged = view and args[3] in self.merged_prs
        branch = next((h for (h, _b), url in self.prs.items() if view and url == args[3]), "")
        head = self.fx._tip(branch) if branch and self.fx._has(branch) else ""
        return subprocess.CompletedProcess(
            args, 0, json.dumps({"state": "MERGED" if merged else "OPEN", "headRefOid": head}),
            "")

    def _drive_wave(self, cfg, wave: list[Path], **_kw) -> int:
        for d in wave:
            self._build(d)
        return 1

    def _texts(self, cfg, d: Path, **_kw) -> bool:
        """The flow's draft + T4 pre-pass: fails for the ids in ``fail_texts``."""
        return d.name.removeprefix("issue_") not in self.fail_texts

    def _stubs(self, stack: ExitStack) -> None:
        stack.enter_context(mock.patch.object(subprocess, "run", self._gh))
        stack.enter_context(mock.patch.object(publish, "_warn_if_squash_only",
                                              lambda *a, **k: None))

    def _flow(self, ids: list[str], *, csv: str | None = None,
              no_publish: bool = False) -> int:
        """One ``pdca flow <ids…>`` (or, with no ids, ``pdca flow --from-csv <csv>``), Act
        off, through ``cli._flow``; returns its exit code. ``self.holds`` is the ``holds``
        map ``cli._flow`` collected, ``self.err`` the run's stderr."""
        args = SimpleNamespace(issue_ids=list(ids), from_csv=csv, from_briefs=None,
                               no_publish=no_publish, no_act=True, by="T", lanes=None)
        seen: list[dict[str, str]] = []
        real_ids, real_batch = flow.flow_ids, flow.flow_batch

        def ids_spy(*a, **k):
            seen.append(k["holds"])
            return real_ids(*a, **k)

        def batch_spy(*a, **k):
            seen.append(k["holds"])
            return real_batch(*a, **k)

        self.gh_calls.clear()
        err, out = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            self._stubs(stack)
            stack.enter_context(mock.patch.object(leaves, "do_plan_batch",
                                                  lambda *a, **k: None))
            stack.enter_context(mock.patch.object(flow, "_drive_wave", self._drive_wave))
            stack.enter_context(mock.patch.object(publish, "draft_texts", self._texts))
            stack.enter_context(mock.patch.object(flow, "flow_ids", ids_spy))
            stack.enter_context(mock.patch.object(flow, "flow_batch", batch_spy))
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            rc = cli._flow(self.cfg, args)
        self.err, self.out = err.getvalue(), out.getvalue()
        self.holds = dict(seen[-1]) if seen else {}
        self.assertNotIn("Traceback", self.err)
        return rc

    def _publish(self, iid: str) -> int:
        """A real ``pdca publish <iid>`` of an accepted bundle, ``gh`` stubbed; its stderr is
        kept in ``self.publish_err``."""
        err = io.StringIO()
        with ExitStack() as stack:
            self._stubs(stack)
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(err))
            rc = publish.publish(self.cfg, iid, by="T", today=TODAY)
        self.publish_err = err.getvalue()
        return rc

    def _hold_lines(self, iid: str) -> list[str]:
        return [x for x in self.err.splitlines() if f"issue_{iid} " in x and _NO_PR in x]

    def _assert_held(self, rc: int, iid: str) -> None:
        """The run exited 3, held ``iid`` (and nothing else) as ``"no-pr"``, said so on ONE
        line that gives the way out, and never told the operator to delete the bundle."""
        self.assertEqual(rc, 3, self.err)
        self.assertEqual(self.holds, {iid: "no-pr"}, self.err)
        lines = self._hold_lines(iid)
        self.assertEqual(len(lines), 1, self.err)
        self.assertIn(f"`pdca publish {iid}`", lines[0])
        self.assertNotIn("rm -rf", self.err)

    def _assert_clean(self, rc: int) -> None:
        self.assertEqual(rc, 0, self.err)
        self.assertEqual(self.holds, {}, self.err)
        self.assertNotIn(_NO_PR, self.err)

    # -- (a) pushed, but `gh pr create` failed: held, and held again on every re-run ---------

    def test_a_pushed_bundle_whose_pr_was_not_opened_holds_the_run_and_each_rerun(self):
        self._plan("A", {"a.txt": "a"})
        self.fail_create.add("A")
        rc = self._flow(["A"])
        self.assertEqual(self._state("A"), state.COMPLETE, self.err)
        rec = self._record("A")
        self.assertTrue(self.fx._has(rec["branch"]), rec)       # really pushed to origin
        self.assertEqual(rec["pr_url"], "", rec)                # but no PR was opened
        self._assert_held(rc, "A")

        # The same command again: the terminal filter re-checks the record, holds it, and
        # gives the hold line in place of `already complete … rm -rf <dir>`. It reads
        # files only — no `gh` call.
        rc = self._flow(["A"])
        self._assert_held(rc, "A")
        self.assertIn("issue_A — already terminal (COMPLETE), skipped", self.err)
        self.assertEqual(self.gh_calls, [])

        # …and beside a new id, which is driven and published as usual.
        self._plan("B", {"b.txt": "b"})
        rc = self._flow(["A", "B"])
        self.assertEqual(self._state("B"), state.COMPLETE, self.err)
        self.assertTrue(self._record("B")["pr_url"], self.err)
        self._assert_held(rc, "A")

    # -- a PR open on the branch that the record does not name: `pdca publish` cannot open a
    # -- second one, so the line says to put that PR's url in the record ------------------

    def test_a_rebuild_published_while_its_pr_is_open_is_held_until_that_pr_is_on_record(self):
        # Iterating while the PR is in review: I's rebuild is accepted and the flow publishes
        # it. The push puts it on the PR's branch, so the open PR holds the accepted patch, but
        # `gh pr create` opens no second PR from that branch and the record names none.
        self._plan("I", {"i.txt": "first"})
        self._assert_clean(self._flow(["I"]))
        url = self._record("I")["pr_url"]
        self._iterate("I", {"i2.txt": "rebuilt"})
        rc = self._flow(["I"])
        rec = self._record("I")
        self.assertEqual((rec["pr_url"], rec["patch_digest"]), ("", self._digest("I")), rec)
        self.assertEqual(self.fx._show(f"refs/heads/{rec['branch']}", "i2.txt"), "rebuilt\n")
        self.assertIn(f"into branch \"main\" already exists:\n{url}", self.err)   # gh's refusal
        self._assert_held(rc, "I")
        line = self._hold_lines("I")[0]
        self.assertIn(f"its publish pushed the accepted patch.diff to {rec['branch']}", line)
        self.assertIn(f"If a PR from {rec['branch']} into main is open", line)
        self.assertIn("put its URL in publish.json's `pr_url`, then re-run", line)
        # `pdca publish I` cannot clear it while that PR is open: gh opens no second one.
        self.assertEqual(self._publish("I"), 1)
        self.assertIn(f"already exists:\n{url}", self.publish_err)
        self._assert_held(self._flow(["I"]), "I")
        self._put_pr_url("I", url)                     # the step the line gives
        self._assert_clean(self._flow(["I"]))

    def test_a_pr_opened_by_hand_after_a_failed_create_clears_once_its_url_is_on_record(self):
        # Publish's own advice after a failed `gh pr create` is to open the PR by hand, and it
        # says that PR's url goes in publish.json. Until it does, the run holds the bundle, and
        # `pdca publish A` cannot clear it: gh opens no second PR from that branch.
        self._published_with_no_pr("A")
        rec = self._record("A")
        self.assertIn(f"{self.cfg.bundle('A') / 'publish.json'}'s `pr_url`", self.err)
        url = self._open_by_hand(rec["branch"], "main")
        self.fail_create.clear()
        self.assertEqual(self._publish("A"), 1)
        self.assertIn(f"already exists:\n{url}", self.publish_err)
        rc = self._flow(["A"])
        self._assert_held(rc, "A")
        self.assertIn("put its URL in publish.json's `pr_url`", self._hold_lines("A")[0])
        self._put_pr_url("A", url)
        self._assert_clean(self._flow(["A"]))

    # -- (b) a rebuild never published: the old PR holds the rejected patch --------------

    def _rebuilt_never_published(self, iid: str) -> None:
        """``iid`` published (its PR opened), then iterated, rebuilt and accepted, with its
        texts failing T4, so the rebuild is never published: its record still names the
        old PR, for the old patch."""
        rc = self._flow([iid])
        self._assert_clean(rc)
        old = self._record(iid)
        self.assertTrue(old["pr_url"], old)
        self._iterate(iid, {f"{iid.lower()}2.txt": "rebuilt"})
        self.fail_texts.add(iid)
        self._flow([iid])
        self.assertEqual(self._state(iid), state.COMPLETE, self.err)
        self.assertEqual(self._record(iid), old)              # nothing published this run
        self.assertNotEqual(self._digest(iid), old.get("patch_digest"))

    def test_an_iterated_bundle_whose_rebuild_was_never_published_is_held_on_rerun(self):
        self._plan("I", {"i.txt": "first"})
        self._rebuilt_never_published("I")
        # The re-run: nothing of this run was pushed or accepted, so only the patch digest
        # on the record can tell that its PR holds the earlier attempt.
        rc = self._flow(["I"])
        self._assert_held(rc, "I")

    def test_a_finished_prerequisite_on_no_pr_is_not_carried_into_its_dependent(self):
        # A's PR holds its earlier attempt (a.txt); the accepted rebuild (a2.txt) was never
        # published. B depends on A. A re-issued `pdca flow A B` must not carry A's branch onto
        # the line B is built on: B's PR would ship the attempt the human rejected. B waits, as
        # the dependent of any finished id the carry holds does (#646), until A's PR holds the
        # accepted patch; then the carry puts that on the line, and B is built on it.
        self._plan("A", {"a.txt": "rejected"})
        self._rebuilt_never_published("A")
        url = self._record("A")["pr_url"]
        self._plan("B", {"b.txt": "b"}, extra="- **Depends on:** A\n")
        rc = self._flow(["A", "B"])
        self.assertEqual(self._state("B"), state.PLANNED, self.err)
        self.assertFalse((self.cfg.bundle("B") / "publish.json").exists(), self.err)
        self.assertNotIn("carried issue_A", self.err)
        self.assertIn("issue_A (finished in an earlier run) is not carried onto", self.err)
        self.assertIn("issue_B skipped — prerequisite(s) not ready (A)", self.err)
        self.assertEqual(self.holds, {"A": "no-pr"}, self.err)
        self.assertEqual(len(self._hold_lines("A")), 1, self.err)
        self.assertEqual(rc, 1, self.err)           # B left unbuilt: 1 wins over 3
        # A's line's step: its publish pushes the rebuild to the open PR's branch (gh opens no
        # second PR from it), then that PR's url goes back in A's record.
        self.assertEqual(self._publish("A"), 1)
        self.assertIn(f"already exists:\n{url}", self.publish_err)
        self._put_pr_url("A", url)
        rc = self._flow(["A", "B"])
        self._assert_clean(rc)
        self.assertIn("carried issue_A", self.err)
        self.assertEqual(self._state("B"), state.COMPLETE, self.err)
        shown = self.fx._diff_files("main", f"refs/heads/{self._record('B')['branch']}")
        self.assertEqual(sorted(shown), ["a2.txt", "b.txt"], self.err)   # the accepted A

    def test_a_finished_id_built_on_one_held_on_no_pr_is_not_carried_either(self):
        # C was built on A's earlier attempt and published, so C's branch has that attempt's
        # commit (a.txt). A's rebuild was never published. A dependent of C must not get the
        # rejected attempt through C's branch: C is not carried either, and the dependent waits.
        self._plan("A", {"a.txt": "rejected"})
        self._plan("C", {"c.txt": "c"}, extra="- **Depends on:** A\n")   # a second wave
        self._assert_clean(self._flow(["A", "C"]))
        c_branch = f"refs/heads/{self._record('C')['branch']}"
        self.assertEqual(sorted(self.fx._diff_files("main", c_branch)), ["a.txt", "c.txt"])
        self._iterate("A", {"a2.txt": "rebuilt"})
        self.fail_texts.add("A")
        self._flow(["A"])
        self.assertEqual(self._state("A"), state.COMPLETE, self.err)
        self._plan("B", {"b.txt": "b"}, extra="- **Depends on:** C\n")
        rc = self._flow(["A", "C", "B"])
        self.assertEqual(self._state("B"), state.PLANNED, self.err)
        self.assertNotIn("carried issue_", self.err)
        self.assertIn("issue_C (finished in an earlier run) is not carried onto", self.err)
        self.assertIn("its `Depends on` names issue_A, whose work its branch carries", self.err)
        self.assertEqual(self.holds, {"A": "no-pr"}, self.err)
        self.assertEqual(rc, 1, self.err)

    def test_an_iterated_onto_branch_bundle_whose_rebuild_was_never_published_is_held(self):
        self.fx._push_from_human({"feature.txt": "feature"}, to="feature/x")
        self._plan("O", {"o.txt": "first"}, extra="- **Onto branch:** origin/feature/x\n")
        self._rebuilt_never_published("O")
        self.assertEqual(self._record("O")["mode"], "stacked")
        self.assertEqual(self._record("O")["pr_url"], ONTO_PR)
        rc = self._flow(["O"])
        self._assert_held(rc, "O")

    # -- (c) R1: a run that pushed nothing is held over an old record; a re-run trusts it --

    def test_a_run_that_did_not_push_is_held_over_an_old_record_and_a_rerun_trusts_it(self):
        d = self._plan("L", {"l.txt": "l"})
        self._legacy(d)         # an earlier attempt's record, from before patch digests
        self.fail_texts.add("L")
        rc = self._flow(["L"])
        self.assertEqual(self._state("L"), state.COMPLETE, self.err)
        self._assert_held(rc, "L")

        rc = self._flow(["L"])
        self._assert_clean(rc)
        notes = [x for x in self.err.splitlines() if "issue_L" in x and "patch digest" in x]
        self.assertEqual(len(notes), 1, self.err)
        # The note gives the step for a bundle rebuilt after its PR, as #865's note does.
        self.assertIn("re-publish it with `pdca publish L`", notes[0])

    def _named_and_released(self, field: str) -> None:
        """``pdca flow L X``: L, COMPLETE, its record from before digests, its PR merged; X
        names L in ``field``. The run reads L's record twice — the terminal filter's no-PR
        check trusts it, and X's prerequisite check counts its merge (#865) — and says it
        once: the two share one set of notes."""
        d = self._plan("L", {"l.txt": "l"})
        self._build(d)
        self.merged_prs.add(self._legacy(d))
        self._plan("X", {"x.txt": "x"}, extra=f"- **{field}:** L\n")
        rc = self._flow(["L", "X"])
        self._assert_clean(rc)
        self.assertEqual(self._state("X"), state.COMPLETE, self.err)   # L's merge counted
        self.assertTrue(self._record("X")["pr_url"], self.err)
        notes = self._notes("L")
        self.assertEqual(len(notes), 1, self.err)
        self.assertIn("`pdca publish L`", notes[0])     # the step for a rebuilt L stays

    def test_a_legacy_record_named_and_counted_as_a_merged_prerequisite_gets_one_note(self):
        self._named_and_released("Depends on (merged)")

    def test_merge_mode_gives_a_legacy_named_prerequisite_one_note_too(self):
        self.cfg.wave_mode = "merge"
        self._named_and_released("Depends on")

    # -- (d) the right patch, but a PR for another target -----------------------------------

    def test_a_record_for_another_target_is_held_on_rerun(self):
        d = self._plan("R", {"r.txt": "r"})
        self._assert_clean(self._flow(["R"]))
        self.assertEqual(self._record("R")["patch_digest"], self._digest("R"))
        brief = (d / "brief.md").read_text(encoding="utf-8")
        (d / "brief.md").write_text(brief.replace("org/repo @ main", "org/repo @ release"),
                                    encoding="utf-8")
        rc = self._flow(["R"])
        self._assert_held(rc, "R")

    def test_a_record_with_no_repo_field_is_this_repos(self):
        # A record from before publish wrote `repo` is this repo's, as publish's own reader of
        # records takes it: on target, and with no digest either, trusted with one note. Held
        # as off-target, its line's step (`pdca publish L`) would fail on its open PR and
        # leave its record naming no PR.
        d = self._plan("L", {"l.txt": "l"})
        self._build(d)
        url = self._legacy(d)
        rec = self._record("L")
        del rec["repo"]
        (d / "publish.json").write_text(json.dumps(rec), encoding="utf-8")
        self.prs["fix/L-fix", "main"] = url              # its PR is open
        self._assert_clean(self._flow(["L"]))
        self.assertEqual(len(self._notes("L")), 1, self.err)
        self.assertEqual(self._record("L"), rec)          # its PR's url kept
        # Its base still counts: retargeted, the same record is held off-target.
        brief = (d / "brief.md").read_text(encoding="utf-8")
        (d / "brief.md").write_text(brief.replace("org/repo @ main", "org/repo @ release"),
                                    encoding="utf-8")
        rc = self._flow(["L"])
        self._assert_held(rc, "L")
        self.assertIn(f"its PR ({url}) is for org/repo @ main, but its brief now targets "
                      f"org/repo @ release", self._hold_lines("L")[0])

    # -- (e) re-running a split parent checks the COMPLETE children it reaches ----------

    def _split_parent(self) -> None:
        """Split parent SP, accepted, with children K1 and K2 on disk (PLANNED): the next
        ``pdca flow SP`` adopts and drives them."""
        parent = self.cfg.bundle("SP")
        sb._brief(parent)
        proposal = "<!-- pdca:split-proposal v1 -->\n\n"
        for n, cid in enumerate(("K1", "K2"), 1):
            proposal += (f"<!-- pdca:child child-{n} -->\n- **Slug:** fix\n"
                         f"- **Repo + branch target:** org/repo @ main\n"
                         f"<!-- pdca:end child-{n} -->\n\n")
            self.files[cid] = {f"{cid.lower()}.txt": cid}
        (parent / split.PROPOSAL).write_text(proposal, encoding="utf-8")
        split.accept(parent, ["K1", "K2"], self.cfg)
        sb._accept(parent)

    def test_rerunning_a_split_parent_holds_an_adopted_child_with_no_pr(self):
        self._split_parent()
        self.fail_create.add("K1")
        self._flow(["SP"])                       # adopts K1 and K2 and drives them
        for cid in ("K1", "K2"):
            self.assertEqual(self._state(cid), state.COMPLETE, (cid, self.err))
        self.assertEqual(self._record("K1")["pr_url"], "")

        rc = self._flow(["SP"])
        self.assertIn("issue_K1 — child of issue_SP NOT adopted: already terminal", self.err)
        self._assert_held(rc, "K1")

    def test_a_finished_child_named_beside_its_split_parent_is_read_once(self):
        # `pdca flow SP K1 K2` reaches each child twice: as a named id (the terminal filter)
        # and as SP's child (adoption). One read each: K2, on no PR, gets one hold line; K1,
        # whose record predates digests, one note.
        self._split_parent()
        self.fail_create.add("K2")
        self._flow(["SP"])
        for cid in ("K1", "K2"):
            self.assertEqual(self._state(cid), state.COMPLETE, (cid, self.err))
        rec = self._record("K1")
        self.assertTrue(rec["pr_url"], rec)
        del rec["patch_digest"]                   # as a record from before digests reads
        (self.cfg.bundle("K1") / "publish.json").write_text(json.dumps(rec), encoding="utf-8")
        self.fail_create.clear()

        rc = self._flow(["SP", "K1", "K2"])
        self.assertIn("issue_K2 — child of issue_SP NOT adopted: already terminal", self.err)
        self.assertEqual(rc, 3, self.err)
        self.assertEqual(self.holds, {"K2": "no-pr"}, self.err)
        self.assertEqual(len(self._hold_lines("K2")), 1, self.err)
        notes = [x for x in self.err.splitlines() if "issue_K1" in x and "patch digest" in x]
        self.assertEqual(len(notes), 1, self.err)
        self.assertEqual(self.gh_calls, [])

    # -- (f) an `Onto branch` publish records the patch it pushed ----------------------------

    def test_an_onto_branch_publish_records_its_patch_digest(self):
        self.fx._push_from_human({"feature.txt": "feature"}, to="feature/x")
        d = self._plan("F", {"f.txt": "f"}, extra="- **Onto branch:** origin/feature/x\n")
        self._build(d)
        self.assertEqual(self._publish("F"), 0)
        rec = self._record("F")
        self.assertEqual(rec["mode"], "stacked")
        self.assertEqual(rec["base"], "origin/feature/x")
        self.assertEqual(rec.get("patch_digest"), self._digest("F"), rec)

    # -- (g) GUARD: a bundle the fold held "unpushed" keeps that one hold -------------------

    def test_a_bundle_the_fold_held_unpushed_keeps_that_one_hold(self):
        self._plan("G", {"g.txt": "g"})
        self._plan("C", {"c.txt": "c"})
        self._plan("D", {"d.txt": "d"}, extra="- **Depends on:** C\n")   # a second wave
        self.fail_texts.add("G")
        rc = self._flow(["C", "D", "G"])
        self.assertEqual(rc, 3, self.err)
        self.assertEqual(self.holds, {"G": "unpushed"}, self.err)
        unpushed = [x for x in self.err.splitlines() if "issue_G" in x and _UNPUSHED in x]
        self.assertEqual(len(unpushed), 1, self.err)
        self.assertEqual(self._hold_lines("G"), [], self.err)
        for iid in ("C", "D", "G"):
            self.assertEqual(self._state(iid), state.COMPLETE, (iid, self.err))

    # -- (h) merge mode: the failed create in the final (only) wave --------------------------

    def test_merge_mode_holds_a_bundle_of_its_final_wave_whose_pr_was_not_opened(self):
        self.cfg.wave_mode = "merge"
        self._plan("M", {"m.txt": "m"})
        self.fail_create.add("M")
        rc = self._flow(["M"])
        self.assertEqual(self._record("M")["pr_url"], "")
        self._assert_held(rc, "M")

    # -- (h2) GUARD: in a non-final merge wave the merge stops the run, exit 1 as before ---

    def test_merge_mode_stops_on_a_non_final_wave_bundle_with_no_pr(self):
        self.cfg.wave_mode = "merge"
        self._plan("N", {"n.txt": "n"})
        self._plan("Q", {"q.txt": "q"}, extra="- **Depends on:** N\n")
        self.fail_create.add("N")
        rc = self._flow(["N", "Q"])
        self.assertIn("wave 0 did not merge; STOPPING", self.err)
        self.assertEqual(self._state("Q"), state.PLANNED, self.err)
        self.assertEqual(rc, 1, self.err)

    # -- (i) GUARDS --------------------------------------------------------------------------

    def test_a_close_or_no_fix_bundle_and_a_no_target_bundle_are_clean(self):
        self._plan("Z", {})                                     # an empty patch: no-fix
        self._plan("T", {"t.txt": "t"}, target="")              # no usable target
        self._assert_clean(self._flow(["T", "Z"]))
        for iid in ("T", "Z"):
            self.assertEqual(self._state(iid), state.COMPLETE, (iid, self.err))
            self.assertFalse((self.cfg.bundle(iid) / "publish.json").exists())
        self._assert_clean(self._flow(["T", "Z"]))

    def _stacked_on_parent(self) -> None:
        """P published, then S (a hand-declared ``Stacks on: P``, no ``stack-base``) published
        by a real ``pdca publish``, its PR chained onto P's branch."""
        p = self._plan("P", {"p.txt": "p"})
        s = self._plan("S", {"s.txt": "s"}, extra="- **Stacks on:** P\n")
        self._build(p)
        self.assertEqual(self._publish("P"), 0)
        self._build(s)
        self.assertEqual(self._publish("S"), 0)
        rec = self._record("S")
        self.assertEqual(rec["mode"], "stacked-pr")
        self.assertEqual(rec["base"], self._record("P")["branch"])   # chained on its parent
        self.assertEqual(rec["patch_digest"], self._digest("S"))
        self.assertFalse((s / publish.STACK_BASE_FILE).exists())

    def test_a_hand_declared_stacks_on_pr_on_its_parents_branch_is_clean_on_rerun(self):
        self._stacked_on_parent()
        self._assert_clean(self._flow(["S"]))
        self._assert_clean(self._flow(["P", "S"]))

    def test_a_stacks_on_pr_stays_clean_once_its_parent_is_archived(self):
        # The parent's branch is read wherever the parent is: archived to `completed/` (#171)
        # it is still the base S's PR belongs on, not the brief's own `main`.
        self._stacked_on_parent()
        archive = self.cfg.bundle_root / "completed"
        archive.mkdir()
        self.cfg.bundle("P").rename(archive / "issue_P")
        self._assert_clean(self._flow(["S"]))

    def test_an_archived_stacks_on_parent_comes_first_in_the_step_for_a_rebuild(self):
        # S was rebuilt and accepted after its PR was opened, the rebuild never published, and
        # then P was archived. S is held (its PR holds the earlier attempt), and publish refuses
        # S while P is archived (a `Stacks on` parent must be an active bundle), so the line
        # says to bring P back first, or drop the edge. Following it clears the hold.
        self._stacked_on_parent()
        old = self._record("S")
        self._iterate("S", {"s2.txt": "rebuilt"})
        self._build(self.cfg.bundle("S"))
        archive = self.cfg.bundle_root / "completed"
        archive.mkdir()
        active = self.cfg.bundle("P")
        active.rename(archive / "issue_P")
        rc = self._flow(["S"])
        self._assert_held(rc, "S")
        line = self._hold_lines("S")[0]
        self.assertIn("another patch.diff", line)
        self.assertIn("its `Stacks on` parent P is archived", line)
        self.assertIn(f"move {archive / 'issue_P'} back to {active}, or drop that `Stacks on`",
                      line)
        self.assertEqual(self._publish("S"), 1)         # refused while P is archived…
        self.assertIn("`Stacks on` a prereq with no published branch", self.publish_err)
        self.assertEqual(self._record("S"), old)        # …before it pushed anything
        (archive / "issue_P").rename(active)
        # S's PR is still open: the publish pushes the rebuild to its branch, and `gh pr
        # create` opens no second PR, so the line's step for that is to put the PR's url back.
        self.assertEqual(self._publish("S"), 1)
        self.assertIn(f"already exists:\n{old['pr_url']}", self.publish_err)
        rec = self._record("S")
        self.assertEqual((rec["pr_url"], rec["patch_digest"]), ("", self._digest("S")), rec)
        self.assertEqual(self.fx._show(f"refs/heads/{rec['branch']}", "s2.txt"), "rebuilt\n")
        self._put_pr_url("S", old["pr_url"])
        self._assert_clean(self._flow(["S"]))

    def test_a_stacks_on_pr_whose_parent_has_no_branch_on_record_is_held_for_that(self):
        # With no record of P's branch the base S's PR belongs on is not known: held, and the
        # line says that — it never judges the PR against the brief's own base instead.
        self._stacked_on_parent()
        (self.cfg.bundle("P") / "publish.json").unlink()
        rc = self._flow(["S"])
        self.assertEqual(rc, 3, self.err)
        self.assertEqual(self.holds, {"S": "no-pr"}, self.err)
        lines = self._hold_lines("S")
        self.assertEqual(len(lines), 1, self.err)
        self.assertIn("no publish record of P names a branch", lines[0])
        self.assertNotIn("@ main", lines[0])
        self.assertNotIn("rm -rf", self.err)
        # Whatever S's own record says, publish refuses S until P's branch is on record, so the
        # line never sends the operator to `pdca publish S` first.
        (self.cfg.bundle("S") / "publish.json").unlink()
        rc = self._flow(["S"])
        self.assertEqual(rc, 3, self.err)
        lines = self._hold_lines("S")
        self.assertEqual(len(lines), 1, self.err)
        self.assertIn("no publish record of P names a branch", lines[0])
        self.assertNotIn("pdca publish S", lines[0])
        self.assertEqual(self._publish("S"), 1)         # refused, as the line says
        self.assertIn("`Stacks on` a prereq with no published branch", self.publish_err)

    def test_merge_mode_says_what_publish_needs_first_for_a_stacks_on_bundle(self):
        # Merge mode opens no PR against another bundle's branch (#411), so publish refuses a
        # hand-declared `Stacks on` PR there, every time: the line says what must change first
        # (back to stack mode, or no `Stacks on`), and following it clears the hold.
        p = self._plan("P", {"p.txt": "p"})
        s = self._plan("S", {"s.txt": "s"}, extra="- **Stacks on:** P\n")
        self._build(p)
        self.assertEqual(self._publish("P"), 0)
        self._build(s)
        self.cfg.wave_mode = "merge"
        rc = self._flow(["S"])
        self._assert_held(rc, "S")
        line = self._hold_lines("S")[0]
        self.assertIn("merge mode opens no PR against a `Stacks on` parent's branch", line)
        self.assertIn('set `[driver].wave_mode = "stack"`, or drop that `Stacks on` from the '
                      'brief', line)
        self.assertEqual(self._publish("S"), 1)          # refused, as the line says
        self.assertIn("that branch is one this run produced", self.publish_err)
        self.assertFalse((s / "publish.json").exists())
        self.cfg.wave_mode = "stack"
        self.assertEqual(self._publish("S"), 0)
        self.assertEqual(self._record("S")["base"], self._record("P")["branch"])
        self._assert_clean(self._flow(["S"]))

    def test_a_fork_stacks_on_bundle_gets_the_parent_step_publish_needs_first(self):
        # On a fork the PR opens against the target base, so the check judges it as any other
        # PR, but publish still cuts its branch from the `Stacks on` parent's and refuses while
        # no active parent names one: the line names that step first, archived or not.
        p = self._plan("P", {"p.txt": "p"})
        s = self._plan("S", {"s.txt": "s"}, extra="- **Stacks on:** P\n")
        self._build(p)
        self.assertEqual(self._publish("P"), 0)
        self._build(s)
        self.cfg.base_remote = "upstream"
        archive = self.cfg.bundle_root / "completed"
        archive.mkdir()
        p.rename(archive / "issue_P")
        rc = self._flow(["S"])
        self._assert_held(rc, "S")
        self.assertIn(f"its `Stacks on` parent P is archived, and publish chains a PR only onto "
                      f"an active parent's branch: move {archive / 'issue_P'} back to {p}",
                      self._hold_lines("S")[0])
        self.assertEqual(self._publish("S"), 1)          # refused, as the line says
        self.assertIn("`Stacks on` a prereq with no published branch", self.publish_err)
        (archive / "issue_P").rename(p)
        (p / "publish.json").unlink()
        rc = self._flow(["S"])
        self._assert_held(rc, "S")
        self.assertIn("no publish record of its `Stacks on` parent P names a branch: publish P",
                      self._hold_lines("S")[0])
        self.assertEqual(self._publish("S"), 1)
        self.assertIn("`Stacks on` a prereq with no published branch", self.publish_err)

    def test_merge_mode_says_so_when_the_target_base_is_a_branch_another_bundle_published(self):
        # The stack-mode practice of targeting a predecessor's branch: merge mode's guard
        # refuses that base too, and the line gives publish's own refusal and way out.
        p = self._plan("P", {"p.txt": "p"})
        self._build(p)
        self.assertEqual(self._publish("P"), 0)
        branch = self._record("P")["branch"]
        self._plan("W", {"w.txt": "w"}, target=f"org/repo @ {branch}")
        self.cfg.wave_mode = "merge"
        rc = self._flow(["W"])
        self.assertIn("this run accepted it, but no publish of it completed", self.err)
        self._assert_held(rc, "W")
        line = self._hold_lines("W")[0]
        self.assertIn("but that base is the branch issue_P produced in this batch", line)
        self.assertIn('use the default wave_mode = "stack"', line)
        self.assertEqual(self._publish("W"), 1)
        self.assertIn("produced in this batch", self.publish_err)

    def test_an_onto_branch_with_no_open_pr_is_held_with_the_step_publish_needs(self):
        # Publish adds an `Onto branch` commit only to an open PR on that branch, and refuses
        # while none is: the line says to open that PR first (or drop the `Onto branch`).
        self.fx._push_from_human({"feature.txt": "y"}, to="feature/y")
        self._plan("O", {"o.txt": "o"}, extra="- **Onto branch:** origin/feature/y\n")
        rc = self._flow(["O"])
        self.assertEqual(self._state("O"), state.COMPLETE, self.err)
        self.assertIn("publish: no open PR with head", self.err)     # publish refused it
        self._assert_held(rc, "O")
        self.assertIn("it adds a commit to the open PR whose head is feature/y, and refuses "
                      "while none is: open that PR first", self._hold_lines("O")[0])
        self.assertEqual(self._publish("O"), 1)
        self.assertIn("no open PR with head", self.publish_err)
        self._assert_held(self._flow(["O"]), "O")
        self.onto_prs["feature/y"] = "https://example.test/org/repo/pull/8"   # opened
        self.assertEqual(self._publish("O"), 0)
        self.assertEqual(self._record("O")["patch_digest"], self._digest("O"))
        self._assert_clean(self._flow(["O"]))

    def test_a_patch_diff_that_cannot_be_read_is_held_not_trusted(self):
        # Fail-closed: with its patch.diff unreadable nothing can say a PR holds the accepted
        # patch, not even a record from before digests, which is otherwise trusted.
        d = self._plan("U", {"u.txt": "u"})
        self._build(d)
        self._legacy(d)
        (d / "patch.diff").write_bytes(b"\xff\xfe not utf-8\n")
        rc = self._flow(["U"])
        self.assertEqual(rc, 3, self.err)
        self.assertEqual(self.holds, {"U": "no-pr"}, self.err)
        lines = self._hold_lines("U")
        self.assertEqual(len(lines), 1, self.err)
        self.assertIn("its patch.diff cannot be read", lines[0])
        self.assertNotIn("cannot be confirmed", self.err)
        self.assertNotIn("rm -rf", self.err)

    def test_a_wave_stacked_pr_stays_clean_after_a_switch_to_merge_mode(self):
        # D is published by a stack-mode run with its wave's `stack-base` marker, its PR on the
        # real base. A later merge-mode run does not move that PR, so it is still on target.
        self._plan("C", {"c.txt": "c"})
        self._plan("D", {"d.txt": "d"}, extra="- **Depends on:** C\n")   # a second wave
        self._assert_clean(self._flow(["C", "D"]))
        self.assertTrue((self.cfg.bundle("D") / publish.STACK_BASE_FILE).exists())
        rec = self._record("D")
        self.assertEqual((rec["mode"], rec["base"]), ("stacked-pr", "main"), rec)
        self.cfg.wave_mode = "merge"
        self._assert_clean(self._flow(["D"]))
        self._assert_clean(self._flow(["C", "D"]))

    def test_a_legacy_record_whose_merge_marker_is_for_another_patch_is_held(self):
        # A record from before `patch_digest` that merge mode marked (#839) carries the digest
        # of the patch it marked: for another patch, the record is known to be an older
        # attempt's, not one whose contents cannot be confirmed.
        d = self._plan("L", {"l.txt": "rebuilt"})
        self._build(d)
        first = sb._new_file_patch("l.txt", "first").encode("utf-8")
        legacy = {"mode": "new-pr", "branch": "fix/L-fix", "pr_url": "https://example.test/pr/9",
                  "base": "main", "repo": "org/repo", "by": "T", "date": "2026-01-02",
                  "id_pending": False, "merge_eligible": True,
                  "merge_eligible_patch": hashlib.sha256(first).hexdigest()}
        (d / "publish.json").write_text(json.dumps(legacy), encoding="utf-8")
        rc = self._flow(["L"])
        self._assert_held(rc, "L")
        self.assertIn("another patch.diff", self._hold_lines("L")[0])
        self.assertNotIn("cannot be confirmed", self.err)
        # Marked for the accepted patch, the same record is confirmed by it: clean, no note.
        legacy["merge_eligible_patch"] = self._digest("L")
        (d / "publish.json").write_text(json.dumps(legacy), encoding="utf-8")
        self._assert_clean(self._flow(["L"]))
        self.assertNotIn("cannot be confirmed", self.err)

    def _published_with_no_pr(self, iid: str) -> None:
        """``iid`` accepted and published by a run whose ``gh pr create`` failed: its branch
        is on origin, its record names no PR (that run's exit code is (a)'s, not read here)."""
        self._plan(iid, {f"{iid.lower()}.txt": iid})
        self.fail_create.add(iid)
        self._flow([iid])
        self.assertEqual(self._state(iid), state.COMPLETE, self.err)
        self.assertEqual(self._record(iid)["pr_url"], "", self.err)

    def test_a_dry_run_reports_no_missing_pr(self):
        self._published_with_no_pr("A")
        # --no-publish: no publish, so nothing is asked of the host or reported missing.
        self._plan("B", {"b.txt": "b"})
        for ids in (["A"], ["B"]):
            self._assert_clean(self._flow(ids, no_publish=True))
            self.assertEqual(self.gh_calls, [])
        self.assertFalse((self.cfg.bundle("B") / "publish.json").exists())
        # A stub publisher: every publish is a dry run, which pushes and records nothing.
        self.cfg.publisher = dataclasses.replace(self.cfg.publisher, mode="stub")
        self._plan("C", {"c.txt": "c"})
        for ids in (["A"], ["C"], ["A", "B", "C"]):
            self._assert_clean(self._flow(ids))
            self.assertEqual(self.gh_calls, [])
        self.assertFalse((self.cfg.bundle("C") / "publish.json").exists())

    def test_a_csv_sweep_rerun_does_not_see_a_complete_bundle(self):
        # Out of scope by design: the sweep resumes in-flight bundles only, so the re-check
        # of a COMPLETE one belongs to `pdca flow <ids>` (docs 08, 11).
        self._published_with_no_pr("A")
        rc = self._flow([], csv="tracker.csv")
        self.assertIn("nothing to do", self.err)
        self._assert_clean(rc)


if __name__ == "__main__":
    unittest.main()
