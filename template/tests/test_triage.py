"""Offline slice for `pdca triage` (#316) — external PR-review ingestion into Act.

Drives ``triage.run`` (and the CLI wiring) against CANNED ``gh`` output — the gh
subprocesses are stubbed the same way the publish/cleanup slices stub theirs. Proves
the four criteria: (a) the PR's reviews AND review comments are pulled via `gh api`;
(b) each finding is keyword-classified into BUG / CONVENTION / NOISE / TEST-GAP;
(c) routing — a BUG on a merged PR files a tracker issue whose body carries a
carry-forward note, CONVENTION appends a candidate gate row / rubric line to the act
log, NOISE a candidate rubric-exclusion entry; (d) EVERY finding is registered
through ``act.register_signals`` under a class-keyed signal name, and
``act.recurrences`` flags the class when the same signal reappears after its process
delta was applied. No Claude, no git, no network.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pdca_harness import act, cli, split, triage
from pdca_harness.config import Config, LeafConfig

_REPO = "example-org/example-repo"
_PR_URL = f"https://github.com/{_REPO}/pull/7"

# One finding per class, worded to hit exactly one keyword group each; plus a
# body-less APPROVED review, which carries no finding text and must be skipped.
_REVIEWS = [
    {"user": {"login": "codex"}, "state": "CHANGES_REQUESTED",
     "body": "This branch is untested — please cover the revert path too.",
     "html_url": f"{_PR_URL}#pullrequestreview-1"},
    {"user": {"login": "codex"}, "state": "APPROVED", "body": "",
     "html_url": f"{_PR_URL}#pullrequestreview-2"},
]
_COMMENTS = [
    {"user": {"login": "codex"}, "path": "src/marker.py",
     "body": "This crashes when the marker file is empty.",
     "html_url": f"{_PR_URL}#discussion_r1"},
    {"user": {"login": "codex"}, "path": "src/cli.py",
     "body": "House style: the docstring should be imperative mood.",
     "html_url": f"{_PR_URL}#discussion_r2"},
    {"user": {"login": "codex"}, "path": "src/cli.py",
     "body": "Nit: double blank line here, cosmetic only.",
     "html_url": f"{_PR_URL}#discussion_r3"},
]


def _meta(number: int, merged: bool) -> dict:
    return {"number": number, "merged": merged,
            "merged_at": "2026-06-15T10:00:00Z" if merged else None,
            "html_url": f"https://github.com/{_REPO}/pull/{number}",
            "title": "a published fix"}


def _api_map(number: int = 7, merged: bool = True, reviews=None, comments=None) -> dict:
    base = f"repos/{_REPO}/pulls/{number}"
    return {base: _meta(number, merged),
            f"{base}/reviews": _REVIEWS if reviews is None else reviews,
            f"{base}/comments": _COMMENTS if comments is None else comments}


class TriageSlice(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(
            root=self.tmp,
            bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process",
            templates_dir=self.tmp / "templates",
            default_branch="main",
            tracker_system="github",   # tracker repo derives from the URL (sources)
            tracker_url=f"https://github.com/{_REPO}/issues",
            issue_id_example="1",
            builder=LeafConfig(mode="stub"),
            reviewer=LeafConfig(mode="stub"),
        )
        self.gh_calls: list[list[str]] = []

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_gh(self, api_map: dict, issue_numbers=("901",)):
        """A gh stub: canned `gh api` payloads; `gh issue create` prints an issue URL
        (the bare-URL line split's machinery parses). A map value of
        ``{"pages": [page1, page2, …]}`` serves one page per ``page=N`` request
        (``[]`` beyond the last) — the multi-page shape the pagination fix must
        follow to the end. Everything is recorded."""
        it = iter(issue_numbers)

        def fake(cmd, *args, **kwargs):
            self.gh_calls.append(list(cmd))
            if cmd[:2] == ["gh", "api"]:
                path, _, query = cmd[2].partition("?")
                if path in api_map:
                    data = api_map[path]
                    if isinstance(data, dict) and "pages" in data:
                        params = dict(kv.split("=", 1)
                                      for kv in query.split("&") if "=" in kv)
                        page, pages = int(params.get("page", 1)), data["pages"]
                        data = pages[page - 1] if page <= len(pages) else []
                    return SimpleNamespace(returncode=0,
                                           stdout=json.dumps(data), stderr="")
                return SimpleNamespace(returncode=1, stdout="",
                                       stderr=f"HTTP 404: {path}")
            if cmd[:3] == ["gh", "issue", "create"]:
                n = next(it)
                return SimpleNamespace(
                    returncode=0,
                    stdout=f"https://github.com/{_REPO}/issues/{n}\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return fake

    def _run(self, pr: str = _PR_URL, *, date: str = "2026-06-20",
             api_map: dict | None = None, issue_numbers=("901", "902"),
             ) -> tuple[int, str, str]:
        fake = self._fake_gh(api_map if api_map is not None else _api_map(),
                             issue_numbers)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(triage.subprocess, "run", side_effect=fake), \
                mock.patch.object(split.subprocess, "run", side_effect=fake), \
                mock.patch.object(split.shutil, "which", return_value="/usr/bin/gh"), \
                redirect_stdout(out), redirect_stderr(err):
            rc = triage.run(self.cfg, pr, date=date)
        return rc, out.getvalue(), err.getvalue()

    def _record(self) -> dict:
        recs = sorted((self.cfg.process_dir / "triage").glob("pr-*.json"))
        self.assertEqual(len(recs), 1, recs)
        return json.loads(recs[0].read_text(encoding="utf-8"))

    def _act_log(self) -> str:
        return (self.cfg.process_dir / "act-log.md").read_text(encoding="utf-8")

    def _issue_creates(self) -> list[list[str]]:
        return [c for c in self.gh_calls if c[:3] == ["gh", "issue", "create"]]

    # ---- (a) pull -----------------------------------------------------------
    def test_pulls_reviews_and_review_comments(self) -> None:
        rc, _, _ = self._run()
        self.assertEqual(rc, 0)
        findings = self._record()["findings"]
        self.assertEqual({f["source"] for f in findings},
                         {"review", "review-comment"})
        self.assertEqual(len(findings), 4)  # the body-less APPROVED review is no finding
        api_paths = [c[2] for c in self.gh_calls if c[:2] == ["gh", "api"]]
        self.assertTrue(any("/pulls/7/reviews" in p for p in api_paths))
        self.assertTrue(any("/pulls/7/comments" in p for p in api_paths))

    def test_paginates_reviews_and_comments_beyond_100(self) -> None:
        # "Register EVERY finding" includes the 101st: page 1 of BOTH endpoints is
        # full (100 items), and the only BUG / TEST-GAP findings live on page 2 — a
        # single per_page=100 fetch would silently drop them (the C5 defect).
        page1_reviews = [dict(_REVIEWS[1], html_url=f"{_PR_URL}#pullrequestreview-{i}")
                         for i in range(100)]              # body-less: items, not findings
        page1_comments = [dict(_COMMENTS[2], body=f"Nit: cosmetic issue {i}.",
                               html_url=f"{_PR_URL}#discussion_r{i}")
                          for i in range(100)]
        api = _api_map(reviews={"pages": [page1_reviews, [_REVIEWS[0]]]},
                       comments={"pages": [page1_comments, [_COMMENTS[0]]]})
        rc, _, _ = self._run(api_map=api)
        self.assertEqual(rc, 0)
        api_paths = [c[2] for c in self.gh_calls if c[:2] == ["gh", "api"]]
        self.assertTrue(any("/pulls/7/reviews" in p and "page=2" in p for p in api_paths))
        self.assertTrue(any("/pulls/7/comments" in p and "page=2" in p for p in api_paths))
        findings = self._record()["findings"]
        self.assertEqual(len(findings), 102)               # 100 nits + page-2 BUG + TEST-GAP
        signals = {e["signal"] for e in act.load_ledger(self.cfg)}
        self.assertLessEqual({"codex-pr:bug-crashes",       # page-2-only signals: dropped
                              "codex-pr:test-gap-untested"  # pages ⇒ these go missing
                              }, signals)
        self.assertEqual(len(self._issue_creates()), 1)    # the page-2 BUG still files

    def test_unreadable_pull_aborts_without_ingesting(self) -> None:
        # Fail CLOSED: a half-ingested PR silently under-registers signals.
        broken = _api_map()
        del broken[f"repos/{_REPO}/pulls/7/comments"]
        rc, _, err = self._run(api_map=broken)
        self.assertEqual(rc, 1)
        self.assertIn("nothing ingested", err)
        self.assertFalse((self.cfg.process_dir / "triage").exists())

    # ---- (b) classify -------------------------------------------------------
    def test_classifies_each_finding_into_the_four_classes(self) -> None:
        self._run()
        by_text = {f["text"]: f for f in self._record()["findings"]}
        self.assertEqual(by_text[_COMMENTS[0]["body"]]["cls"], "BUG")
        self.assertEqual(by_text[_COMMENTS[1]["body"]]["cls"], "CONVENTION")
        self.assertEqual(by_text[_COMMENTS[2]["body"]]["cls"], "NOISE")
        self.assertEqual(by_text[_REVIEWS[0]["body"]]["cls"], "TEST-GAP")
        # The class-keyed signal grammar: codex-pr:<class-slug>-<keyword-slug>,
        # derived from the keyword table (never the free text) so it is stable.
        self.assertEqual(by_text[_COMMENTS[0]["body"]]["signal"], "codex-pr:bug-crashes")
        self.assertEqual(by_text[_REVIEWS[0]["body"]]["signal"],
                         "codex-pr:test-gap-untested")

    def test_precedence_is_severity_first(self) -> None:
        # "nit: … crashes …" is a bug someone softened — BUG outranks NOISE.
        f = triage.Finding(source="review-comment", author="codex",
                           text="Nit: this crashes on empty input.")
        triage.classify(f, triage.class_keywords())
        self.assertEqual(f.cls, "BUG")

    def test_rubric_class_list_overrides_one_class_keywords(self) -> None:
        table = triage.class_keywords("## Review rubric\n- BUG: kaboom\n")
        self.assertEqual(table["BUG"], ("kaboom",))
        self.assertEqual(table["NOISE"], triage.DEFAULT_KEYWORDS["NOISE"])  # untouched

    # ---- (c) route ----------------------------------------------------------
    def test_bug_on_merged_pr_files_tracker_issue_with_carry_forward(self) -> None:
        rc, _, _ = self._run()
        self.assertEqual(rc, 0)
        creates = self._issue_creates()
        self.assertEqual(len(creates), 1)  # exactly the one BUG finding
        cmd = creates[0]
        self.assertEqual(cmd[cmd.index("--repo") + 1], _REPO)
        body = cmd[cmd.index("--body") + 1]
        self.assertIn("Carry-forward", body)                 # the note Plan will read
        self.assertIn(_PR_URL, body)                         # provenance
        self.assertIn(_COMMENTS[0]["body"], body)            # the finding itself
        self.assertIn("codex-pr:bug-crashes", body)          # its ledger signal
        self.assertIn("filed tracker issue #901", self._act_log())

    def test_bug_on_unmerged_pr_files_nothing(self) -> None:
        rc, _, _ = self._run(api_map=_api_map(merged=False))
        self.assertEqual(rc, 0)
        self.assertEqual(self._issue_creates(), [])
        self.assertIn("NOT filed", self._act_log())          # candidate kept, loudly

    def test_convention_and_noise_route_to_act_log_candidates(self) -> None:
        self._run()
        log = self._act_log()
        self.assertIn("candidate gate row / rubric line", log)   # CONVENTION routing
        self.assertIn("codex-pr:convention-style", log)
        self.assertIn("candidate rubric-exclusion entry", log)   # NOISE routing
        self.assertIn("codex-pr:noise-nit", log)

    def test_rerun_ingests_only_new_findings_and_never_refiles(self) -> None:
        self._run()
        rc, out, _ = self._run(date="2026-06-21")            # same canned PR again
        self.assertEqual(rc, 0)
        self.assertIn("no new findings", out)
        self.assertEqual(len(self._issue_creates()), 1)      # ONE issue across both runs

    def test_lock_contention_then_rerun_registers_recorded_findings(self) -> None:
        # The reviewer-demonstrated C3/T3 trap: a held Act session interrupts run 1
        # AFTER the issue is filed and the record written (exit 1); the prescribed
        # re-run then found "no new findings" and exited 0 with the ledger
        # permanently empty. The record's `pending` flags make the re-run finish
        # the interrupted job: register from the record history, append the lost
        # act-log entry, and never re-file.
        with act.act_session(self.cfg) as held:              # a concurrent Act session
            self.assertTrue(held)
            rc, _, err = self._run()
        self.assertEqual(rc, 1)
        self.assertIn("another Act session", err)
        self.assertEqual(len(self._issue_creates()), 1)      # filing already happened…
        self.assertEqual(act.load_ledger(self.cfg), [])      # …registration did NOT
        self.assertFalse((self.cfg.process_dir / "act-log.md").exists())
        self.assertTrue(all(f["pending"] for f in self._record()["findings"]))
        rc, out, _ = self._run(date="2026-06-21")            # lock released: re-run
        self.assertEqual(rc, 0)
        self.assertIn("4 recovered", out)
        signals = {e["signal"] for e in act.load_ledger(self.cfg)}
        self.assertLessEqual({"codex-pr:bug-crashes", "codex-pr:convention-style",
                              "codex-pr:noise-nit", "codex-pr:test-gap-untested"},
                             signals)
        log = self._act_log()                                # the lost entry, recovered
        self.assertIn("recovered from an interrupted run", log)
        self.assertIn("filed tracker issue #901", log)       # credited from the record
        self.assertIn("candidate gate row / rubric line", log)
        self.assertEqual(len(self._issue_creates()), 1)      # NOT re-filed
        self.assertFalse(any(f.get("pending")                # journal fully cleared
                             for f in self._record()["findings"]))

    # ---- (d) register + recurrence -----------------------------------------
    def test_registers_every_finding_class_keyed(self) -> None:
        self._run()
        ledger = act.load_ledger(self.cfg)
        signals = {e["signal"] for e in ledger}
        # EVERY finding registers on first sight — singletons included.
        self.assertLessEqual({"codex-pr:bug-crashes", "codex-pr:convention-style",
                              "codex-pr:noise-nit", "codex-pr:test-gap-untested"},
                             signals)
        self.assertTrue(all(e["status"] == "open" for e in ledger))

    def test_recurrence_flagged_when_class_keyed_signal_reappears(self) -> None:
        self._run(date="2026-06-20")                         # PR 7 registers the signal
        raw = act.resolve(self.cfg, "codex-pr:bug-crashes", "process/act-log.md",
                          "2026-07-01")                      # the human applies a delta
        self.assertEqual(raw, "codex-pr:bug-crashes")
        # The same class-keyed signal reappears on ANOTHER PR after the applied date.
        pr8 = _api_map(number=8, reviews=[], comments=[
            {"user": {"login": "codex"}, "path": "src/marker.py",
             "body": "This crashes again when the marker file is empty.",
             "html_url": f"https://github.com/{_REPO}/pull/8#discussion_r9"}])
        rc, out, _ = self._run(f"https://github.com/{_REPO}/pull/8",
                               date="2026-07-10", api_map=pr8,
                               issue_numbers=("902",))
        self.assertEqual(rc, 0)
        self.assertIn("recurred", out)                       # surfaced to the operator
        recs = act.recurrences(self.cfg, triage._entries(self.cfg))
        hit = [r for r in recs if r["signal"] == "codex-pr:bug-crashes"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["applied"], "2026-07-01")
        self.assertEqual(hit[0]["recurred_in"], ["pr_8"])

    def test_parse_pr_refuses_a_bare_number_without_repo(self) -> None:
        # gh's checkout-default repo could hold an unrelated same-numbered PR.
        self.assertEqual(triage.parse_pr("7"), ("", ""))
        self.assertEqual(triage.parse_pr("7", _REPO), (_REPO, "7"))
        self.assertEqual(triage.parse_pr(_PR_URL), (_REPO, "7"))
        self.assertEqual(triage.parse_pr(f"{_REPO}#7"), (_REPO, "7"))


class TriageCli(unittest.TestCase):
    """The `pdca triage` subcommand exists and routes to the engine (red on a main
    without the subparser: argparse rejects the verb with SystemExit)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "pdca.toml").write_text('[paths]\nbundle_root = "results"\n',
                                            encoding="utf-8")
        self._cwd = Path.cwd()
        os.chdir(self.tmp)

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cli_wires_the_triage_subcommand(self) -> None:
        with mock.patch.object(triage, "run", return_value=0) as m:
            rc = cli.main(["triage", _PR_URL, "--date", "2026-07-25"])
        self.assertEqual(rc, 0)
        _cfg, pr = m.call_args.args
        self.assertEqual(pr, _PR_URL)
        self.assertEqual(m.call_args.kwargs["date"], "2026-07-25")
        self.assertEqual(m.call_args.kwargs["repo"], "")

    def test_cli_usage_error_on_unresolvable_pr(self) -> None:
        with redirect_stderr(io.StringIO()):
            rc = cli.main(["triage", "7"])   # bare number, no --repo → fail closed
        self.assertEqual(rc, 2)

    def test_cli_drives_the_engine_end_to_end(self) -> None:
        # `pdca triage <pr>` all the way through: parse → pull (stubbed gh) →
        # classify → route (issue filed) → register — the success criterion is
        # phrased against the COMMAND, not just the engine function.
        (self.tmp / "pdca.toml").write_text(
            '[paths]\nbundle_root = "results"\n\n[tracker]\nsystem = "github"\n'
            f'url = "https://github.com/{_REPO}/issues"\n', encoding="utf-8")
        calls: list[list[str]] = []

        def fake(cmd, *args, **kwargs):
            calls.append(list(cmd))
            if cmd[:2] == ["gh", "api"]:
                path, amap = cmd[2].split("?")[0], _api_map()
                if path in amap:
                    return SimpleNamespace(returncode=0,
                                           stdout=json.dumps(amap[path]), stderr="")
                return SimpleNamespace(returncode=1, stdout="", stderr="HTTP 404")
            if cmd[:3] == ["gh", "issue", "create"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout=f"https://github.com/{_REPO}/issues/901\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(triage.subprocess, "run", side_effect=fake), \
                mock.patch.object(split.subprocess, "run", side_effect=fake), \
                mock.patch.object(split.shutil, "which", return_value="/usr/bin/gh"), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = cli.main(["triage", _PR_URL, "--date", "2026-06-20"])
        self.assertEqual(rc, 0)
        ledger = json.loads(Path("process/act-ledger.json").read_text(encoding="utf-8"))
        self.assertIn("codex-pr:bug-crashes", {e["signal"] for e in ledger})
        self.assertIn("PR-review triage",
                      Path("process/act-log.md").read_text(encoding="utf-8"))
        self.assertTrue(any(c[:3] == ["gh", "issue", "create"] for c in calls))


if __name__ == "__main__":
    raise SystemExit(unittest.main())
