"""The stack-mode live check (#625): what it judges, and what it refuses to touch.

``tools/live/stack_mode_live.py`` runs the #593 stack-mode scenario on the private sandbox
repository and records what GitHub showed in ``observations.json``; ``judge`` turns that file
into ``report.json`` / ``report.md``, one line per check (PASS, FAIL or INCOMPLETE), and exits
0 only if every check passes. These cases run the tool as a subprocess, by path, and never
import it, so with the tool absent each case still runs and fails on its assertions:

* ``judge`` on observation files written to a temp dir: a complete, matching file passes; each
  disagreeing observation FAILs its own check; a precondition that was not observed makes the
  checks that need it INCOMPLETE, never FAIL, and so does a PR file list ``run`` did not see
  settle on the commits it expected (after a push to the PR: the run base tip and the PR's
  head; with no push since the base moved, where GitHub keeps the PR's base commit and file
  list: the head unchanged and the mergeability recomputed against the run base tip); and a
  run 2 that exits non-zero or raises after its preconditions were observed is E3's FAIL.
* ``preflight`` and the guard of ``setup`` / ``run`` / ``teardown``, against a stand-in ``gh``
  first on ``PATH`` that logs each call with the ``GH_TOKEN`` it saw.
* ``teardown`` from a ``state.json`` written here: it deletes the run's branches and closes the
  run's PR, and never touches ``main``, the repository or its settings.

No network, no copier.

    python3 -m tests.run_root_suite tests.test_stack_mode_live_tool
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tools" / "live" / "stack_mode_live.py"
CHECKS = ["E0", "E1", "E1b", "E2", "E3", "E4", "E5"]
IDS = ["A", "B", "C", "D", "P", "Q", "S"]
REPO = "eduralph/pdca-stack-sandbox"
BASE = "live-20990101-000000"
# The run's integration prefix, as integrate.integration_branch(cfg, BASE) builds it
# (template/src/pdca_harness/integrate.py:140-157): every line and per-bundle base of the run
# starts with it.
PREFIX = "pdca-integration/live-h20990101-h000000"
LINE = f"{PREFIX}-r0123456789ab"
FILES = {iid: [f"stack-live/{iid.lower()}.txt"] for iid in IDS}
HEAD = {iid: c * 40 for iid, c in zip(IDS, "abcdef0")}
MERGE = {"A": "1" * 40, "C": "2" * 40, "B": "3" * 40, "P": "4" * 40, "S": "5" * 40}
# The run base's tip: main's commit at setup, then each merge commit on it.
START = "9" * 40
B_UPDATED = "7" * 40             # B's head after Update branch (GitHub merges the base in)
ABC = ["stack-live/a.txt", "stack-live/b.txt", "stack-live/c.txt"]
AB = ["stack-live/a.txt", "stack-live/b.txt"]
PQ = ["stack-live/p.txt", "stack-live/q.txt"]
B_OWN, Q_OWN = ["stack-live/b.txt"], ["stack-live/q.txt"]


def _branch(iid: str, base: str = BASE) -> str:
    return f"fix/{iid}-{base}-{iid.lower()}"


def _pr(iid: str, number: int) -> dict:
    return {"number": number, "url": f"https://github.com/{REPO}/pull/{number}",
            "state": "OPEN", "isDraft": True, "baseRefName": BASE,
            "headRefName": _branch(iid), "headRefOid": HEAD[iid], "pushed_head": HEAD[iid]}


def _run(ids: list[str], stderr: str = "") -> dict:
    return {"ids": ids, "driven": True, "raised": "", "rc": 0,
            "results": dict.fromkeys(ids, "COMPLETE"), "holds": {},
            "states": dict.fromkeys(ids, "COMPLETE"), "stderr": stderr, "stdout": ""}


def _read(files: list[str], base: str, head: str, merge_on: str, at: float) -> dict:
    """One read of a PR as ``run`` records it: GitHub's file list, the PR's ``baseRefOid`` and
    ``headRefOid``, and its mergeability with GitHub's test merge, a merge of the head into the
    base tip ``merge_on``."""
    return {"files": list(files), "baseRefOid": base, "headRefOid": head,
            "mergeable": "MERGEABLE",
            "potentialMergeCommit": {"oid": merge_on[:20] + head[:20], "parents": [merge_on, head]},
            "at": at}


def _reads(*lists: list[str], on: tuple[str, str], kept: str = "", merge_on: str = "",
           first_on: tuple[str, str] | None = None, synced: bool = True,
           compare: list[str] | None = None) -> dict:
    """What ``run`` records for one polled PR file list: every read, in order; the commits
    ``run`` expected the PR on (``on``: the run base tip just fetched, the PR's head);
    ``synced``; and GitHub's compare of those two commits (``compare``: its files, by default
    the last read's). Each read shows the PR on ``on`` and its test merge on the run base tip
    (``merge_on``, by default ``on``'s). ``kept``: the run base moved with no push to the PR,
    so every read shows the PR's ``baseRefOid`` GitHub kept, not the tip. With ``first_on``,
    the first read is still GitHub's answer from before the merge or update."""
    reads = []
    for n, files in enumerate(lists):
        tip, head = first_on if first_on and n == 0 else on
        merge_tip = tip if first_on and n == 0 else merge_on or tip
        reads.append(_read(files, kept or tip, head, merge_tip, 10.0 + 5.0 * n))
    return {"reads": reads, "expect": {"baseRefOid": on[0], "headRefOid": on[1]},
            "synced": synced, "seconds": 10.0 + 5.0 * len(lists), "window": 180.0,
            "compare": {"base": on[0], "head": on[1], "status": "ahead",
                        "files": list(lists[-1] if compare is None else compare)}}


def _merged(iid: str, method: str = "merge") -> dict:
    return {"method": method, "merged": True, "refused": "", "branch": _branch(iid),
            "merged_head": HEAD[iid], "deleted": True, "delete": "deleted"}


def _anc(value: bool | None) -> dict:
    """A recorded ``git merge-base --is-ancestor`` answer (None: exit 128)."""
    return {"value": value, "rc": {True: 0, False: 1}.get(value, 128)}


RUN2_LINE = f"{PREFIX}-rfedcba987654"
# The two success lines of integrate._gone_branch (integrate.py:1434-1436, :1509-1511).
GONE_A = (f"integrate: issue_A's branch origin/{_branch('A')} is gone and its PR merged at "
          f"{HEAD['A'][:12]} — {RUN2_LINE} already carries that commit; skipped.")
GONE_C = (f"integrate: issue_C's branch origin/{_branch('C')} is gone and its PR merged at "
          f"{HEAD['C'][:12]} — {RUN2_LINE} already carries that commit; skipped.")
GONE_C_THROUGH = (f"integrate: issue_C's branch origin/{_branch('C')} is gone and its PR merged "
                  f"at {HEAD['C'][:12]}, which {RUN2_LINE} did not have — the merged work "
                  f"reaches {RUN2_LINE} through origin/{BASE}.")
# The carry's line for a finished id it refused (flow.py:1575-1578), over the error of
# integrate.py:1491: it repeats the words the success lines open with.
REFUSED_A = (f"flow: issue_A (finished in an earlier run) is not carried onto {RUN2_LINE}: "
             f"issue_A's branch origin/{_branch('A')} is gone and its PR merged at "
             f"{HEAD['A'][:12]}, which {RUN2_LINE} does not have, but origin/{BASE}, which "
             f"carries that work, does not merge onto {RUN2_LINE} (conflicts in "
             f"stack-live/a.txt; update the clashing PR's branch from the base), then re-run "
             f"— holding issue_B this run; re-run once its PR is open.")
RUN2_ERR = "\n".join([
    "flow: issue_A — already terminal (COMPLETE), skipped",
    "flow: issue_B — already terminal (COMPLETE), skipped",
    "flow: issue_C — already terminal (COMPLETE), skipped",
    GONE_A, GONE_C,
    f"flow: carried issue_A, issue_B, issue_C (finished in an earlier run) onto {RUN2_LINE} "
    f"at {'8' * 40} before wave 0 — this run's {REPO} @ {BASE} bundles build on it."]) + "\n"


def _good() -> dict:
    """Observations of a finished run that match every check."""
    return {
        "tool": "stack_mode_live", "schema": 1, "repo": REPO, "run_base": BASE,
        "line_prefix": PREFIX, "files": FILES, "errors": [], "finished": True,
        "runs": {"run1": _run(["A", "C", "B"]), "run2": _run(["A", "B", "C", "D"], RUN2_ERR),
                 "run3": _run(["P", "Q"]), "run4": _run(["S"])},
        "prs": {iid: _pr(iid, n) for n, iid in enumerate(IDS, start=1)},
        "merged": {"A": _merged("A"), "C": _merged("C"), "P": _merged("P"),
                   "B": _merged("B"), "S": _merged("S", "squash")},
        # A first read can still be GitHub's answer from before the merge or update, on the
        # older base, head or test merge; the list the last two reads agree on, both on the
        # commits `run` expected, is the one judged. After a merge with no push to the PR
        # (B after A, B before Update branch, Q after P), GitHub keeps the PR's baseRefOid and
        # file list, as the sandbox showed; only its test merge moves to the run base tip.
        "B_files_opened": _reads(ABC, ABC, on=(START, HEAD["B"])),
        "B_files_after_A_merge": _reads(ABC, ABC, ABC, on=(MERGE["A"], HEAD["B"]), kept=START,
                                        first_on=(START, HEAD["B"])),
        "B_files_before_update": _reads(ABC, ABC, on=(MERGE["C"], HEAD["B"]), kept=START),
        "B_update_branch": {"ok": True, "head_before": HEAD["B"], "head_after": B_UPDATED},
        "B_files_after_update": _reads(ABC, B_OWN, B_OWN, on=(MERGE["C"], B_UPDATED),
                                       first_on=(START, HEAD["B"])),
        "A_after_merge": {"view": {"state": "MERGED", "headRefOid": HEAD["A"],
                                   "mergeCommit": {"oid": MERGE["A"]}},
                          "on_base": {"headRefOid": _anc(True), "mergeCommit": _anc(True)}},
        "D_built_on": {"commit": "8" * 40, "source": "stack-base tip",
                       "heads": {"A": HEAD["A"], "C": HEAD["C"]},
                       "holds": {"A": _anc(True), "C": _anc(True)}},
        "Q_files_opened": _reads(PQ, PQ, on=(MERGE["C"], HEAD["Q"])),
        "Q_files_after_P_merge": _reads(PQ, Q_OWN, Q_OWN, on=(MERGE["P"], HEAD["Q"]),
                                        kept=MERGE["C"], first_on=(MERGE["C"], HEAD["Q"])),
        "after_B_merge": {"on_base": {"A": True, "B": True, "C": True}},
        "S_after_squash": {"view": {"state": "MERGED", "headRefOid": HEAD["S"],
                                    "mergeCommit": {"oid": MERGE["S"]}},
                           "on_base": {"headRefOid": _anc(False), "mergeCommit": _anc(True)}},
    }


def _run2_not_driven(obs: dict, why: str) -> None:
    """What ``run`` records when run 2's preconditions were not observed: run 2 not driven,
    so no PR of D and nothing D was built on."""
    obs["runs"]["run2"] = {"ids": ["A", "B", "C", "D"], "driven": False, "skipped": why}
    del obs["prs"]["D"], obs["D_built_on"]


class JudgeLiveObservations(unittest.TestCase):

    def _judge(self, obs: dict) -> tuple[int, dict, str]:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "observations.json").write_text(json.dumps(obs), encoding="utf-8")
            r = subprocess.run([sys.executable, str(TOOL), "judge", "--workdir", str(work)],
                               capture_output=True, text=True, timeout=120)
            report = work / "report.json"
            data = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else {}
            md = work / "report.md"
            text = md.read_text(encoding="utf-8") if md.is_file() else ""
        return r.returncode, data, text + r.stdout + r.stderr

    def _expect(self, obs: dict, results: dict[str, str], overall: str
                ) -> tuple[dict[str, dict], dict, str]:
        """``judge`` gives exactly ``results`` (every other check PASS) and ``overall``,
        exiting 0 only when that is PASS (1 on a FAIL, 3 on an INCOMPLETE). Returns the
        checks by id."""
        rc, data, out = self._judge(obs)
        checks = {c["id"]: c for c in data.get("checks", [])}
        self.assertEqual(sorted(checks), sorted(CHECKS), out)
        want = {cid: results.get(cid, "PASS") for cid in CHECKS}
        self.assertEqual({cid: c["result"] for cid, c in checks.items()}, want, out)
        for cid, c in checks.items():
            self.assertTrue(c["expected"], (cid, out))
            if c["result"] == "INCOMPLETE":
                self.assertTrue(c["observed"].get("not observed"), (cid, out))
        self.assertEqual(data.get("result"), overall, out)
        self.assertEqual(rc, {"PASS": 0, "FAIL": 1, "INCOMPLETE": 3}[overall], out)
        return checks, data, out

    # (1) observations matching every check → exit 0, every check PASS
    def test_observations_matching_every_check_pass(self):
        checks, data, out = self._expect(_good(), {}, "PASS")
        for cid in CHECKS:
            self.assertIn(f"| {cid} | PASS |", out)      # report.md: one line per check
        self.assertEqual(checks["E1"]["observed"]["files"], ["stack-live/b.txt"], out)
        # Q's list settled with Q's base commit kept from before P merged: the reads after a
        # merge with no push to the PR settle on its head and its recomputed test merge.
        self.assertEqual(checks["E1b"]["observed"]["last read"]["baseRefOid"], MERGE["C"], out)
        self.assertEqual(checks["E1b"]["observed"]["compare"]["files"], Q_OWN, out)
        # The four lists recorded, not judged, settled too, each by its own rule: B after A
        # and B before Update branch kept B's base commit as Q's did.
        info = {i["id"]: i["observed"] for i in data["info"]}
        self.assertEqual(sorted(info), ["B after A", "B before Update branch", "B opened",
                                        "Q opened"], out)
        for iid, seen in info.items():
            self.assertNotIn("not settled", seen, (iid, out))
            self.assertIn("files", seen["compare"], (iid, out))
        self.assertEqual(info["B after A"]["last read"]["baseRefOid"], START, out)
        self.assertEqual(info["B before Update branch"]["last read"]["baseRefOid"], START, out)

    # (2) A, merged and its branch deleted, reports no headRefOid → E2 FAIL
    def test_a_merged_pr_reporting_no_head_oid_fails_e2(self):
        obs = _good()
        del obs["A_after_merge"]["view"]["headRefOid"]
        checks, _data, out = self._expect(obs, {"E2": "FAIL"}, "FAIL")
        self.assertIn("headRefOid", checks["E2"]["observed"], out)
        self.assertIsNone(checks["E2"]["observed"]["headRefOid"], out)
        self.assertEqual(checks["E2"]["observed"]["pushed head"], HEAD["A"], out)

    # (3) B's files after Update branch still list a file of A's → E1 FAIL
    def test_b_still_listing_a_file_of_a_after_update_branch_fails_e1(self):
        obs = _good()
        obs["B_files_after_update"] = _reads(AB, AB, AB, on=(MERGE["C"], B_UPDATED))
        checks, _data, out = self._expect(obs, {"E1": "FAIL"}, "FAIL")
        self.assertEqual(checks["E1"]["observed"]["files"], AB, out)

    # (4) a squash-merged S whose headRefOid is on the base → E4 FAIL
    def test_a_squash_merge_whose_head_is_on_the_base_fails_e4(self):
        obs = _good()
        obs["S_after_squash"]["on_base"]["headRefOid"] = _anc(True)
        checks, _data, out = self._expect(obs, {"E4": "FAIL"}, "FAIL")
        self.assertIs(checks["E4"]["observed"]["headRefOid on run base"], True, out)

    # (5) a PR against the integration line, or against main, instead of the run base → E0
    def test_a_pr_against_the_line_or_main_fails_e0(self):
        for iid, wrong in (("D", LINE), ("S", "main")):
            with self.subTest(iid=iid, base=wrong):
                obs = _good()
                obs["prs"][iid]["baseRefName"] = wrong
                checks, _data, out = self._expect(obs, {"E0": "FAIL"}, "FAIL")
                self.assertEqual(checks["E0"]["observed"]["wrong"], {iid: wrong}, out)

    # (6) Q's files after P merged list a file of P's → E1b FAIL. The shape the sandbox showed:
    # with no push to Q, GitHub kept Q's base commit and file list after P merged and moved
    # only Q's test merge to the run base tip; its compare of that tip and Q's head lists
    # q.txt alone, and is recorded beside the PR's list.
    def test_q_listing_a_file_of_p_after_p_merged_fails_e1b(self):
        obs = _good()
        obs["Q_files_after_P_merge"] = _reads(PQ, PQ, PQ, on=(MERGE["P"], HEAD["Q"]),
                                              kept=MERGE["C"], compare=Q_OWN)
        checks, _data, out = self._expect(obs, {"E1b": "FAIL"}, "FAIL")
        e1b = checks["E1b"]["observed"]
        self.assertEqual(e1b["files"], PQ, out)
        self.assertEqual(e1b["compare"]["files"], Q_OWN, out)
        self.assertEqual(e1b["last read"]["baseRefOid"], MERGE["C"], out)
        self.assertEqual(e1b["last read"]["potentialMergeCommit"]["parents"],
                         [MERGE["P"], HEAD["Q"]], out)

    # (7) a precondition not observed makes the checks that need it INCOMPLETE, not FAIL
    def test_missing_preconditions_are_incomplete_not_fail(self):
        def a_delete_unconfirmed(obs):
            obs["merged"]["A"].update(deleted=False, delete="refused: HTTP 403")
            _run2_not_driven(obs, "A's branch delete was not confirmed")

        def c_delete_unconfirmed(obs):
            obs["merged"]["C"].update(deleted=False, delete="deleted, but `git ls-remote` "
                                                            "still answers (rc 0)")
            _run2_not_driven(obs, "C's branch delete was not confirmed")

        def a_merge_refused(obs):        # nothing after A's merge happened
            obs["merged"]["A"] = {"method": "merge", "merged": False, "branch": _branch("A"),
                                  "refused": "reads OPEN after `gh pr merge --merge`: "
                                             "Resource not accessible by personal access token",
                                  "deleted": False}
            for key in ("B_files_after_A_merge", "B_files_before_update", "B_update_branch",
                        "B_files_after_update", "A_after_merge", "after_B_merge"):
                del obs[key]
            del obs["merged"]["B"]
            _run2_not_driven(obs, "A's merge was refused")

        def update_branch_refused(obs):
            obs["B_update_branch"] = {"ok": False, "head_before": HEAD["B"],
                                      "refused": "GraphQL: Update branch is not enabled"}
            del obs["B_files_after_update"]

        def p_merge_refused(obs):
            obs["merged"]["P"] = {"method": "merge", "merged": False, "branch": _branch("P"),
                                  "refused": "reads OPEN after `gh pr merge --merge`: HTTP 405",
                                  "deleted": False}
            del obs["Q_files_after_P_merge"]

        def q_files_never_settled(obs):
            obs["Q_files_after_P_merge"] = _reads(PQ, Q_OWN, PQ, on=(MERGE["P"], HEAD["Q"]),
                                                  kept=MERGE["C"])

        def s_head_unplaceable(obs):     # git merge-base --is-ancestor exited 128
            obs["S_after_squash"]["on_base"]["headRefOid"] = _anc(None)

        cases = {
            "A's branch delete not confirmed": (a_delete_unconfirmed,
                                                {"E2": "INCOMPLETE", "E3": "INCOMPLETE"}),
            "C's branch delete not confirmed": (c_delete_unconfirmed, {"E3": "INCOMPLETE"}),
            "GitHub refused A's merge": (a_merge_refused,
                                         dict.fromkeys(["E1", "E2", "E3", "E5"], "INCOMPLETE")),
            "GitHub refused Update branch": (update_branch_refused, {"E1": "INCOMPLETE"}),
            "GitHub refused P's merge": (p_merge_refused, {"E1b": "INCOMPLETE"}),
            "Q's file list never settled": (q_files_never_settled, {"E1b": "INCOMPLETE"}),
            "git could not place S's head": (s_head_unplaceable, {"E4": "INCOMPLETE"}),
        }
        for name, (shape, results) in cases.items():
            with self.subTest(name):
                obs = _good()
                shape(obs)
                _checks, data, out = self._expect(obs, results, "INCOMPLETE")
                self.assertTrue(data.get("incomplete"), out)
                self.assertIn("**INCOMPLETE**", out)

    # (7) a PR file list counts only when `run` saw it settle: the last two reads agree, both
    # are on the commits `run` expected, and `run` recorded the poll as synced. After a push to
    # the PR (B after Update branch) that means the PR's baseRefOid is the run base tip and its
    # head the one pushed. After a merge with no push to the PR (Q after P), GitHub keeps the
    # PR's baseRefOid, so it means the head unchanged and the mergeability recomputed: the test
    # merge on the run base tip and that head. Otherwise E1 / E1b are INCOMPLETE, never PASS or
    # FAIL: a poll that ran out of time can end on two identical reads GitHub served before it
    # caught up with a merge or an update, and those say nothing about the list after it.
    def test_a_file_list_not_seen_settled_on_the_expected_commits_is_incomplete(self):
        def b_not_synced(obs):           # the reads agree, on the expected commits
            obs["B_files_after_update"]["synced"] = False

        def b_read_before_the_last_on_the_old_head(obs):
            # The last read is on the expected commits, so `run` recorded the poll as synced;
            # the one before it still shows B's head from before Update branch.
            obs["B_files_after_update"]["reads"][-2]["headRefOid"] = HEAD["B"]

        def b_base_tip_not_recorded(obs):    # `run` could not read the run base's tip
            obs["B_files_after_update"]["expect"]["baseRefOid"] = ""

        def b_pushed_but_still_on_the_old_base(obs):
            # Update branch pushed to B, yet both reads still show B's base commit from before
            # (GitHub had not taken the push in); a test merge on the tip does not stand in
            # for that, as it does after a merge with no push. `synced` true, as a hand-edited
            # file may say: judge checks the reads' commits itself.
            obs["B_files_after_update"] = _reads(B_OWN, B_OWN, on=(MERGE["C"], B_UPDATED),
                                                 kept=START)

        def q_ran_out_of_time_before_github_recomputed(obs):
            # Three identical reads whose test merge is still on the run base from before P
            # merged: GitHub had not taken P's merge in when the poll ran out of time.
            obs["Q_files_after_P_merge"] = _reads(PQ, PQ, PQ, on=(MERGE["P"], HEAD["Q"]),
                                                  kept=MERGE["C"], merge_on=MERGE["C"],
                                                  synced=False)

        def q_mergeability_not_computed(obs):
            # GitHub was still working out Q's mergeability: UNKNOWN, and no test merge yet.
            rec = obs["Q_files_after_P_merge"]
            rec["synced"] = False
            for rd in rec["reads"]:
                rd.update(mergeable="UNKNOWN", potentialMergeCommit=None)

        def q_read_before_the_last_on_the_old_test_merge(obs):
            # The last read is fresh, so `run` recorded the poll as synced; the one before it
            # still has the test merge on the run base from before P merged.
            obs["Q_files_after_P_merge"]["reads"][-2]["potentialMergeCommit"]["parents"] = [
                MERGE["C"], HEAD["Q"]]

        def q_head_moved(obs):
            # Something pushed to Q after P merged: its head is not the one the run pushed, and
            # its test merge is still the one GitHub made before that push. Marked synced, as a
            # hand-edited file may say.
            for rd in obs["Q_files_after_P_merge"]["reads"]:
                rd["headRefOid"] = "6" * 40

        def q_test_merge_of_another_head(obs):
            # The PR's head is the one the run pushed, but GitHub's test merge on the tip was
            # made for another head (a push to Q and back, say): it says nothing about this one.
            for rd in obs["Q_files_after_P_merge"]["reads"]:
                rd["potentialMergeCommit"]["parents"] = [MERGE["P"], "6" * 40]

        def q_reads_without_mergeability(obs):
            # An observations file from before this rule: its reads carry no mergeability, so
            # nothing shows that GitHub took P's merge in (the live run of 2026-10-25).
            rec = obs["Q_files_after_P_merge"] = _reads(PQ, PQ, on=(MERGE["P"], HEAD["Q"]),
                                                        kept=MERGE["C"], synced=False)
            for rd in rec["reads"]:
                del rd["mergeable"], rd["potentialMergeCommit"]
            del rec["compare"]

        def q_not_synced(obs):           # the reads agree, on the expected commits
            obs["Q_files_after_P_merge"]["synced"] = False

        cases = {
            "B's reads agree, poll not synced": (b_not_synced, "E1", ["not synced"]),
            "B's read before the last is on the old head": (
                b_read_before_the_last_on_the_old_head, "E1",
                ["read 2 of 3", "headRefOid", HEAD["B"][:12], B_UPDATED[:12]]),
            "no run base tip to check B's reads against": (
                b_base_tip_not_recorded, "E1", ["baseRefOid"]),
            "B pushed to, reads still on its old base commit": (
                b_pushed_but_still_on_the_old_base, "E1",
                ["read 1 of 2", "read 2 of 2", "baseRefOid", START[:12], MERGE["C"][:12]]),
            "Q's poll ran out of time before GitHub recomputed": (
                q_ran_out_of_time_before_github_recomputed, "E1b",
                ["test merge on", MERGE["C"][:12], MERGE["P"][:12]]),
            "Q's mergeability not computed yet": (
                q_mergeability_not_computed, "E1b",
                ["read 2 of 3", "read 3 of 3", "mergeable UNKNOWN", "no test merge"]),
            "Q's read before the last on the old test merge": (
                q_read_before_the_last_on_the_old_test_merge, "E1b",
                ["read 2 of 3", "test merge on", MERGE["C"][:12]]),
            "Q's head moved": (
                q_head_moved, "E1b", ["headRefOid", "666666666666", HEAD["Q"][:12]]),
            "Q's test merge made for another head": (
                q_test_merge_of_another_head, "E1b",
                ["test merge on", "666666666666", HEAD["Q"][:12]]),
            "Q's reads carry no mergeability": (
                q_reads_without_mergeability, "E1b", ["mergeable null", "no test merge"]),
            "Q's reads agree, poll not synced": (q_not_synced, "E1b", ["not synced"]),
        }
        for name, (shape, cid, said) in cases.items():
            with self.subTest(name):
                obs = _good()
                shape(obs)
                checks, data, out = self._expect(obs, {cid: "INCOMPLETE"}, "INCOMPLETE")
                why = " ".join(checks[cid]["observed"]["not observed"])
                for words in said:
                    self.assertIn(words, why, out)
                self.assertTrue(data.get("incomplete"), out)
                self.assertIn("**INCOMPLETE**", out)

    # A list recorded, not judged, that did not settle (B after A as the 2026-10-25 live run
    # recorded it: stuck base, no mergeability, poll out of time) is reported as such and
    # changes no check.
    def test_a_list_recorded_not_judged_that_did_not_settle_changes_no_check(self):
        obs = _good()
        rec = obs["B_files_after_A_merge"] = _reads(ABC, ABC, on=(MERGE["A"], HEAD["B"]),
                                                    kept=START, synced=False)
        for rd in rec["reads"]:
            del rd["mergeable"], rd["potentialMergeCommit"]
        _checks, data, out = self._expect(obs, {}, "PASS")
        info = {i["id"]: i["observed"] for i in data["info"]}
        self.assertIn("no test merge", info["B after A"]["not settled"], out)
        self.assertNotIn("not settled", info["Q opened"], out)

    # (7b) run 2's preconditions observed, run 2 exits non-zero or raises → E3 FAIL
    def test_run_2_failing_after_its_preconditions_were_observed_fails_e3(self):
        planned = {"A": "COMPLETE", "B": "COMPLETE", "C": "COMPLETE", "D": "PLANNED"}
        cases = {
            "exited 1, A not carried": (
                {"rc": 1, "results": planned, "states": planned,
                 "stderr": RUN2_ERR.replace(GONE_A, REFUSED_A)},
                {"rc": 1}, REFUSED_A),
            "raised": (
                {"rc": None, "results": {}, "states": planned,
                 "raised": "Traceback (most recent call last):\n  File \"flow.py\"\n"
                           "RuntimeError: boom\n",
                 "stderr": "flow: issue_A — already terminal (COMPLETE), skipped\n"},
                {"exception": "RuntimeError: boom"}, "RuntimeError: boom"),
        }
        for name, (change, observed, tail) in cases.items():
            with self.subTest(name):
                obs = _good()
                obs["runs"]["run2"].update(change)
                del obs["prs"]["D"]                  # D never published
                obs["D_built_on"] = {"commit": "", "source": "", "heads": {},
                                     "holds": {"A": _anc(None), "C": _anc(None)}}
                checks, _data, out = self._expect(obs, {"E3": "FAIL"}, "FAIL")
                e3 = checks["E3"]["observed"]
                for key, value in observed.items():
                    self.assertEqual(e3[key], value, out)
                self.assertIn(tail, "\n".join(e3["stderr tail"]), out)
                self.assertNotIn("not observed", e3, out)

    # E3 takes either success line of _gone_branch, and never the opening they share with
    # the refusals (integrate.py:1443, :1449, :1491, :1503).
    def test_e3_reads_the_whole_success_line_never_the_shared_opening(self):
        obs = _good()
        obs["runs"]["run2"]["stderr"] = RUN2_ERR.replace(GONE_C, GONE_C_THROUGH)
        checks, _data, out = self._expect(obs, {}, "PASS")
        self.assertEqual(checks["E3"]["observed"]["gone branch lines"]["C"]["message"],
                         "reaches the line through the base", out)
        obs["runs"]["run2"]["stderr"] = RUN2_ERR.replace(GONE_A, REFUSED_A)
        checks, _data, out = self._expect(obs, {"E3": "FAIL"}, "FAIL")
        self.assertIsNone(checks["E3"]["observed"]["gone branch lines"]["A"], out)


# A stand-in for `gh`, first on PATH: it logs every call with the GH_TOKEN it saw, answers
# from world.json, and refuses any call it does not know, so an unexpected one shows up.
FAKE_GH = """\
import json, os, sys
from pathlib import Path

home = Path(os.environ["FAKE_GH_HOME"])
args = sys.argv[1:]
with (home / "calls.jsonl").open("a", encoding="utf-8") as log:
    log.write(json.dumps({"argv": args, "GH_TOKEN": os.environ.get("GH_TOKEN"),
                          "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN")}) + "\\n")
world = json.loads((home / "world.json").read_text(encoding="utf-8"))
repo = world["repo"]["full_name"]
refs = "/git/matching-refs/heads/"
if args == ["api", f"repos/{repo}"]:
    print(json.dumps(world["repo"]))
elif args[:2] == ["pr", "list"]:
    print(json.dumps(world["open_prs"]))
elif args[:2] == ["api", "--paginate"] and refs in args[2]:
    prefix = "refs/heads/" + args[2].split(refs, 1)[1]
    print("\\n".join(r for r in ("refs/heads/" + b for b in world["branches"])
                    if r.startswith(prefix)))
elif args[:3] == ["api", "-X", "DELETE"] and "/git/refs/heads/" in args[3]:
    branch = args[3].split("/git/refs/heads/", 1)[1]
    if branch not in world["branches"]:
        sys.exit("gh: Reference does not exist (HTTP 422)")
    world["branches"].remove(branch)
    (home / "world.json").write_text(json.dumps(world), encoding="utf-8")
elif args[:2] == ["pr", "close"]:
    pass
else:
    sys.exit(f"fake gh: unexpected call {args}")
"""


class GuardPreflightTeardown(unittest.TestCase):
    """``preflight``, the guard of the writing subcommands and ``teardown``, against the
    stand-in ``gh`` above. The ambient ``GH_TOKEN`` / ``GITHUB_TOKEN`` are set to values the
    tool must never pass on, and ``HOME`` is the temp dir, so no real token file is read."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bindir, self.gh_home = self.root / "bin", self.root / "gh-home"
        self.bindir.mkdir()
        self.gh_home.mkdir()
        gh = self.bindir / "gh"
        gh.write_text(f"#!{sys.executable}\n{FAKE_GH}", encoding="utf-8")
        gh.chmod(0o755)
        self.token = self.root / "sandbox-gh-token"
        self.token.write_text("sandbox-test-token\n", encoding="utf-8")
        kept = {k: v for k, v in os.environ.items() if not k.startswith(("GH_", "GITHUB_"))}
        self.env = {**kept, "PATH": f"{self.bindir}{os.pathsep}{os.environ.get('PATH', '')}",
                    "FAKE_GH_HOME": str(self.gh_home), "HOME": str(self.root),
                    "GH_TOKEN": "ambient", "GITHUB_TOKEN": "ambient-github"}
        # The real gh must never answer here.
        self.assertEqual(shutil.which("gh", path=self.env["PATH"]), str(gh))
        self.world({})
        self.work = self.root / "work"

    def world(self, repo: dict, **more: object) -> None:
        answer = {"full_name": REPO, "private": True, "default_branch": "main",
                  "permissions": {"admin": False, "push": True, "pull": True},
                  "allow_merge_commit": True, "allow_squash_merge": True, **repo}
        world = {"repo": answer, "open_prs": [], "branches": ["main"], **more}
        (self.gh_home / "world.json").write_text(json.dumps(world), encoding="utf-8")

    def tool(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True,
                              text=True, timeout=120, env=self.env, cwd=cwd or self.root)

    def calls(self) -> list[dict]:
        f = self.gh_home / "calls.jsonl"
        lines = f.read_text(encoding="utf-8").splitlines() if f.is_file() else []
        return [json.loads(x) for x in lines]

    def state(self, repo: str = REPO, **more: object) -> None:
        """A ``state.json`` as ``setup`` leaves it, for ``run`` and ``teardown``."""
        self.work.mkdir(exist_ok=True)
        state = {"tool": "stack_mode_live", "repo": repo, "run_base": BASE, "ready": True,
                 "branches": [], "prs": {}, **more}
        (self.work / "state.json").write_text(json.dumps(state), encoding="utf-8")

    # (8) the sandbox token reaches gh, never the ambient one; a private, pushable repo passes
    def test_preflight_passes_the_sandbox_token_to_gh_and_writes_nothing(self):
        cwd = self.root / "cwd"
        cwd.mkdir()
        r = self.tool("preflight", "--repo", REPO, "--token-file", str(self.token), cwd=cwd)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        calls = self.calls()
        self.assertIn(["api", f"repos/{REPO}"], [c["argv"] for c in calls], out)
        self.assertEqual({(c["GH_TOKEN"], c["GITHUB_TOKEN"]) for c in calls},
                         {("sandbox-test-token", None)}, out)
        self.assertEqual(list(cwd.iterdir()), [], out)          # preflight writes nothing

    # publish opens every PR as a draft: a private repo of a personal account is a warning
    def test_preflight_warns_that_a_personal_private_repo_may_refuse_drafts(self):
        self.world({"owner": {"login": "eduralph", "type": "User"}})
        r = self.tool("preflight", "--repo", REPO, "--token-file", str(self.token))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("draft pull requests", r.stderr)

    # (9) a repository that is not private is refused
    def test_preflight_refuses_a_repo_that_is_not_private(self):
        self.world({"private": False})
        r = self.tool("preflight", "--repo", REPO, "--token-file", str(self.token))
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("is not private", r.stderr)
        self.assertEqual([c["argv"] for c in self.calls()], [["api", f"repos/{REPO}"]])

    # (10) the harness's own repository is refused before gh is ever called
    def test_preflight_refuses_the_harness_repo_without_calling_gh(self):
        r = self.tool("preflight", "--repo", "eduralph/pdca-harness",
                      "--token-file", str(self.token))
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("refusing eduralph/pdca-harness", r.stderr)
        self.assertEqual(self.calls(), [])

    # (11) a missing or empty token file is refused before gh is ever called
    def test_preflight_refuses_a_missing_or_empty_token_file_without_calling_gh(self):
        empty = self.root / "empty-token"
        empty.write_text("\n", encoding="utf-8")
        for name, path, said in (("missing", self.root / "no-such-token", "cannot read"),
                                 ("empty", empty, "is empty")):
            with self.subTest(name):
                r = self.tool("preflight", "--repo", REPO, "--token-file", str(path))
                self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn(said, r.stderr)
                self.assertIn(str(path), r.stderr)
                self.assertEqual(self.calls(), [])

    # (12) a repository that forbids squash, or merge commits, is refused, naming the setting
    def test_preflight_refuses_a_repo_without_squash_or_merge_commits(self):
        for setting in ("allow_squash_merge", "allow_merge_commit"):
            with self.subTest(setting):
                self.world({setting: False})
                r = self.tool("preflight", "--repo", REPO, "--token-file", str(self.token))
                self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn(f"{setting} = false", r.stderr)

    def _writing(self, step: str, repo: str, token: Path) -> subprocess.CompletedProcess:
        if step == "setup":
            shutil.rmtree(self.work, ignore_errors=True)
        else:                          # a finished setup, so only the guard can stop it
            self.state(repo=repo)
        return self.tool(step, "--workdir", str(self.work), "--repo", repo,
                         "--token-file", str(token))

    # (13) setup, run and teardown refuse the harness's own repository before calling gh
    def test_writing_steps_refuse_the_harness_repo_without_calling_gh(self):
        for step in ("setup", "run", "teardown"):
            with self.subTest(step):
                r = self._writing(step, "eduralph/pdca-harness", self.token)
                self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn("refusing eduralph/pdca-harness", r.stderr)
                self.assertEqual(self.calls(), [])

    # (14) setup, run and teardown refuse a missing token file before calling gh
    def test_writing_steps_refuse_a_missing_token_file_without_calling_gh(self):
        missing = self.root / "no-such-token"
        for step in ("setup", "run", "teardown"):
            with self.subTest(step):
                r = self._writing(step, REPO, missing)
                self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn(f"cannot read the token file {missing}", r.stderr)
                self.assertEqual(self.calls(), [])

    def _teardown_world(self, *extra: str) -> None:
        """The sandbox as teardown finds it: this run's base and branches, another run's, and
        main; this run's open PR and another run's."""
        other = "live-20980101-000000"
        self.world({}, branches=["main", BASE, _branch("A"), LINE, *extra, other,
                                 _branch("A", other),
                                 "pdca-integration/live-h20980101-h000000-rfeedfacecafe"],
                   open_prs=[{"number": 7, "url": f"https://github.com/{REPO}/pull/7",
                              "headRefName": _branch("A")},
                             {"number": 9, "url": f"https://github.com/{REPO}/pull/9",
                              "headRefName": _branch("A", other)}])
        self.state(branches=[_branch("A"), LINE],
                   prs={"A": {"number": 7, "url": f"https://github.com/{REPO}/pull/7",
                              "headRefName": _branch("A")}})
        for name in ("checkout", "project", "checkout.pdca-integ-live-h20990101-h000000"):
            (self.work / name).mkdir()
        (self.work / "checkout.pdca-integ-live-h20990101-h000000.lock").write_text("")
        for name in ("observations.json", "report.md"):
            (self.work / name).write_text("{}\n", encoding="utf-8")

    def _deleted_and_closed(self) -> tuple[set[str], list[str]]:
        deleted, closed = set(), []
        for c in self.calls():
            argv = c["argv"]
            self.assertEqual(c["GH_TOKEN"], "sandbox-test-token", argv)
            self.assertNotIn("heads/main", " ".join(argv))
            self.assertNotEqual(argv[:2], ["repo", "delete"])
            self.assertFalse("PATCH" in argv and any(a.startswith(f"repos/{REPO}")
                                                     for a in argv), argv)
            if argv[:3] == ["api", "-X", "DELETE"]:
                deleted.add(argv[3].split("/git/refs/heads/", 1)[1])
            if argv[:2] == ["pr", "close"]:
                closed.append(argv[2])
        return deleted, closed

    # (15) teardown deletes exactly the run's base and recorded branches and closes its PR;
    # never main, the repository or its settings
    def test_teardown_removes_the_runs_branches_and_pr_and_nothing_else(self):
        self._teardown_world()
        r = self.tool("teardown", "--workdir", str(self.work), "--repo", REPO,
                      "--token-file", str(self.token))
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        deleted, closed = self._deleted_and_closed()
        self.assertEqual(deleted, {BASE, _branch("A"), LINE}, out)
        self.assertEqual(closed, ["7"], out)
        world = json.loads((self.gh_home / "world.json").read_text(encoding="utf-8"))
        self.assertIn("main", world["branches"])
        # The clone, its integration worktree and lock, and the bundle root are gone; the
        # record files stay, so the report can still be attached.
        self.assertEqual(sorted(p.name for p in self.work.iterdir()),
                         ["observations.json", "report.md", "state.json"], out)

    # A branch the driver made under the run's prefix (a per-bundle base, integrate.py:160)
    # that state.json never recorded is deleted too; another run's branches are not.
    def test_teardown_deletes_an_unrecorded_branch_under_the_runs_prefix(self):
        self._teardown_world(f"{LINE}-bD")
        r = self.tool("teardown", "--workdir", str(self.work), "--repo", REPO,
                      "--token-file", str(self.token))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        deleted, _closed = self._deleted_and_closed()
        self.assertEqual(deleted, {BASE, _branch("A"), LINE, f"{LINE}-bD"}, r.stdout)

    def test_teardown_keep_changes_nothing(self):
        self._teardown_world()
        r = self.tool("teardown", "--workdir", str(self.work), "--repo", REPO,
                      "--token-file", str(self.token), "--keep")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("--keep", r.stdout)
        self.assertEqual(self.calls(), [])
        self.assertTrue((self.work / "checkout").is_dir())


if __name__ == "__main__":
    unittest.main()
