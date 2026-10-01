"""Confirm-once for a failed gating row (issue #371, stdlib unittest).

A gating row is ONE sample. A downstream instance lost a round's verdict to a single
`cargo test` red that six earlier rounds, the C4 run seconds later, the reviewer's own run
and a manual re-run all contradicted. The harness already records "the oracle gave no
answer" as `unverifiable` (#46, #368); a fail contradicted by an immediate pass of the
same command is the same situation one step later. So at Check a failed gating row is
re-run exactly once and both samples are kept:

  (1) fail → pass  ⇒ `pass` + `flaky`, `attempts = ["fail", "pass"]`, two runs;
  (2) fail → fail  ⇒ `fail` with the confirm run's evidence;
  (3) fail → no clean answer (timeout / unverifiable / deferred / exception) ⇒ the first
      `fail` stands with the FIRST run's evidence; `attempts` names the second outcome;
  (4) bounds — one confirm only; non-gating / passing / cmd_error / raised-first rows run
      once; `[gates] confirm_gating_fail = false` and a per-row `confirm_fail = false`
      (on `[[gates.checks]]` AND `[gates] host_ci`) turn it off;
  (4b) Check only — `run_gates` / `run_gates_dry`; the working-tree and integration
      re-gates and the publish host-CI gate run a failing row once (publish is driven
      for real: `publish.publish` against a toy bare origin + clone);
  (5) the gate log holds both runs, each in its own block;
  (6) a `flaky` row becomes one HUMAN §6 item; `overall` counts it as a pass.

Real gate commands through the real `gates._run_one` — a small shell script that counts
its own runs in a marker file. No Claude / Docker / network. Run from the template root:
PYTHONPATH=src python3 -m unittest tests.test_gate_confirm
"""

from __future__ import annotations

import io
import json
import shlex
import shutil
import subprocess as sp
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from pdca_harness import assemble, gates, progress, publish, signoff
from pdca_harness.config import Config, LeafConfig, _normalize_host_ci

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


def _stub_config(root: Path) -> Config:
    # Mirrors tests/test_gate_logs.py:_stub_config.
    return Config(
        root=root,
        bundle_root=root / "results",
        process_dir=root / "process",
        templates_dir=root / "templates",
        default_branch="main",
        tracker_system="github",
        tracker_url="",
        issue_id_example="#1",
        builder=LeafConfig(mode="stub", family="claude"),
        reviewer=LeafConfig(mode="stub", family="codex"),
    )


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = _stub_config(self.tmp)
        self.marker = self.tmp / "runs"

    # A command that appends one line per run to the marker, prints `run-<n>`, then runs
    # the shell `tail` with $n bound to the run number (1-based).
    def _cmd(self, tail: str) -> str:
        m = shlex.quote(str(self.marker))
        return (f"echo x >> {m}; n=$(wc -l < {m} | tr -d ' '); "
                f"echo \"output-of-run-$n\"; {tail}")

    def flaky_cmd(self) -> str:  # fails on run 1, passes from run 2 on
        return self._cmd('echo "evidence-run-$n"; [ "$n" -ge 2 ]')

    def runs(self) -> int:
        if not self.marker.exists():
            return 0
        return len(self.marker.read_text().splitlines())

    def _bundle(self, iid: str = "B") -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** gc\n", encoding="utf-8")
        (d / "patch.diff").write_text("--- a\n+++ b\n", encoding="utf-8")
        return d

    def _chk(self, cmd: str, **kw) -> dict:
        return {"id": "C4-confirm", "tier": "C4", "label": "verify", "scope": "bundle",
                "gating": True, "cmd": cmd, **kw}

    def _row(self, result: dict, rule_id: str = "C4-confirm") -> dict:
        return next(r for r in result["rows"] if r["rule_id"] == rule_id)

    def _check_run(self, chk: dict) -> dict:
        """The row as the Check matrix records it (run_gates — the confirming caller)."""
        self.cfg.gates_checks = [chk]
        self.result = gates.run_gates(self._bundle(), self.cfg)
        return self._row(self.result)


# -- (1) / (2) / (3): the combination rule -------------------------------------------

class CombinationRule(_Base):
    def test_fail_then_pass_records_pass_flaky_after_two_runs(self) -> None:
        row = self._check_run(self._chk(self.flaky_cmd()))
        self.assertEqual(self.runs(), 2, "the failed gating row was not re-run exactly once")
        self.assertEqual(row["result"], "pass")
        self.assertIs(row.get("flaky"), True)
        self.assertEqual(row.get("attempts"), ["fail", "pass"])
        self.assertEqual(self.result["overall"], "pass")

    def test_fail_then_fail_records_fail_with_confirm_run_evidence(self) -> None:
        row = self._check_run(self._chk(self._cmd('echo "evidence-run-$n"; exit 1')))
        self.assertEqual(self.runs(), 2)
        self.assertEqual(row["result"], "fail")
        self.assertFalse(row.get("flaky"))
        self.assertEqual(row.get("attempts"), ["fail", "fail"])
        self.assertEqual(row["path_line"], "evidence-run-2")
        self.assertEqual(self.result["overall"], "fail")

    def _first_fail_stands(self, row: dict, second: str) -> None:
        self.assertEqual(self.runs(), 2)
        self.assertEqual(row["result"], "fail")
        self.assertFalse(row.get("flaky"))
        self.assertEqual(row.get("attempts"), ["fail", second])
        self.assertEqual(row["path_line"], "evidence-run-1")
        self.assertEqual(self.result["overall"], "fail")

    def test_fail_then_timeout_first_fail_stands(self) -> None:
        tail = 'if [ "$n" -ge 2 ]; then sleep 5; fi; echo "evidence-run-$n"; exit 1'
        row = self._check_run(self._chk(self._cmd(tail), timeout_secs=1))
        self._first_fail_stands(row, "unverifiable")

    def test_fail_then_unverifiable_first_fail_stands(self) -> None:
        tail = ('if [ "$n" -ge 2 ]; then echo "PDCA-UNVERIFIABLE: no oracle"; exit 77; fi; '
                'echo "evidence-run-$n"; exit 1')
        row = self._check_run(self._chk(self._cmd(tail)))
        self._first_fail_stands(row, "unverifiable")

    def test_fail_then_deferred_first_fail_stands(self) -> None:
        # A bundle-scoped T4 row is re-gated at publish, so it may declare `deferred`
        # (gates._deferrable → publish.publish_gates).
        tail = ('if [ "$n" -ge 2 ]; then echo "PDCA-DEFERRED: audited at publish"; exit 0; fi; '
                'echo "evidence-run-$n"; exit 1')
        chk = {"id": "T4-confirm", "tier": "T4", "label": "contrib", "scope": "bundle",
               "gating": True, "cmd": self._cmd(tail)}
        self.cfg.gates_checks = [chk]
        self.result = gates.run_gates(self._bundle(), self.cfg)
        self._first_fail_stands(self._row(self.result, "T4-confirm"), "deferred")

    def test_fail_then_exception_first_fail_stands(self) -> None:
        real = progress.run_with_heartbeat
        calls = []

        def second_raises(*a, **kw):
            calls.append(1)
            if len(calls) == 2:
                raise OSError("confirm run could not start")
            return real(*a, **kw)

        with mock.patch.object(progress, "run_with_heartbeat", side_effect=second_raises):
            row = self._check_run(self._chk(self._cmd('echo "evidence-run-$n"; exit 1')))
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.runs(), 1)  # the confirm run never started the command
        self.assertEqual(row["result"], "fail")
        self.assertFalse(row.get("flaky"))
        self.assertEqual(row.get("attempts"), ["fail", "error"])
        self.assertEqual(row["path_line"], "evidence-run-1")

    def test_check_dry_regate_confirms_too(self) -> None:
        self.cfg.gates_checks = [self._chk(self.flaky_cmd())]
        result = gates.run_gates_dry(self._bundle(), self.cfg)
        row = self._row(result)
        self.assertEqual(self.runs(), 2)
        self.assertEqual((row["result"], row.get("flaky")), ("pass", True))
        self.assertEqual(result["overall"], "pass")


# -- (4): bounds ------------------------------------------------------------------------

class Bounds(_Base):
    def test_exactly_one_confirm_run(self) -> None:
        # Fails on runs 1 AND 2, would pass on run 3 — a second confirm must not happen.
        row = self._check_run(self._chk(self._cmd('echo "evidence-run-$n"; [ "$n" -ge 3 ]')))
        self.assertEqual(self.runs(), 2)
        self.assertEqual(row["result"], "fail")
        self.assertEqual(row.get("attempts"), ["fail", "fail"])

    def test_non_gating_failing_row_runs_once(self) -> None:
        row = self._check_run(self._chk(self.flaky_cmd(), gating=False))
        self.assertEqual(self.runs(), 1)
        self.assertEqual(row["result"], "fail")
        self.assertNotIn("attempts", row)

    def test_passing_row_runs_once(self) -> None:
        row = self._check_run(self._chk(self._cmd("exit 0")))
        self.assertEqual(self.runs(), 1)
        self.assertEqual(row["result"], "pass")
        self.assertNotIn("attempts", row)
        self.assertNotIn("flaky", row)

    def test_cmd_error_row_is_not_confirmed(self) -> None:
        calls = []
        real = progress.run_with_heartbeat

        def counting(*a, **kw):
            calls.append(1)
            return real(*a, **kw)

        chk = {"id": "C4-confirm", "tier": "C4", "label": "verify", "scope": "bundle",
               "gating": True, "subcmd": "verify"}  # subcmd but no [gates] runner
        with mock.patch.object(progress, "run_with_heartbeat", side_effect=counting):
            row = self._check_run(chk)
        self.assertEqual(calls, [])
        self.assertEqual(row["result"], "fail")
        self.assertNotIn("attempts", row)

    def test_raised_before_exit_code_is_not_confirmed(self) -> None:
        calls = []

        def raises(*a, **kw):
            calls.append(1)
            raise OSError("cannot spawn")

        with mock.patch.object(progress, "run_with_heartbeat", side_effect=raises):
            row = self._check_run(self._chk(self.flaky_cmd()))
        self.assertEqual(len(calls), 1)
        self.assertEqual(row["result"], "fail")
        self.assertNotIn("attempts", row)

    def test_project_switch_off_runs_once(self) -> None:
        self.cfg.gates_confirm_gating_fail = False
        row = self._check_run(self._chk(self.flaky_cmd()))
        self.assertEqual(self.runs(), 1)
        self.assertEqual(row["result"], "fail")
        self.assertNotIn("flaky", row)

    def test_project_switch_parsed_from_pdca_toml(self) -> None:
        for body, expected in (("", True),
                               ("confirm_gating_fail = true\n", True),
                               ("confirm_gating_fail = false\n", False),
                               ('confirm_gating_fail = "false"\n', False)):
            with self.subTest(body=body):
                (self.tmp / "pdca.toml").write_text(
                    '[paths]\nbundle_root = "results"\n[gates]\n' + body, encoding="utf-8")
                self.assertIs(Config.load(self.tmp).gates_confirm_gating_fail, expected)

    def test_row_switch_off_on_gates_checks(self) -> None:
        row = self._check_run(self._chk(self.flaky_cmd(), confirm_fail=False))
        self.assertEqual(self.runs(), 1)
        self.assertEqual(row["result"], "fail")
        self.assertNotIn("attempts", row)

    def _host_ci_row(self, extra: str) -> dict:
        """A `[gates] host_ci` row as Config.load normalizes it (`_normalize_host_ci`)."""
        cmd = self.flaky_cmd().replace("\\", "\\\\").replace('"', '\\"')
        (self.tmp / "pdca.toml").write_text(
            '[paths]\nbundle_root = "results"\n[gates]\n'
            f'host_ci = [{{ id = "host-ci-x", cmd = "{cmd}"{extra} }}]\n',
            encoding="utf-8")
        rows = Config.load(self.tmp).host_ci_checks
        self.assertEqual(len(rows), 1)
        return rows[0]

    def _run_host_ci_at_check(self, chk: dict) -> dict:
        # The Check matrix's host-CI branch (gates._run_checks), with the patched tree
        # supplied explicitly — the same tree `run_gates` would get from worktree.for_gate.
        self.cfg.host_ci_checks = [chk]
        wt = self.tmp / "wt"
        wt.mkdir()
        rows = gates._run_checks(self.cfg, cwd=self.cfg.root, bundle=self._bundle(),
                                 scopes=("repo", "bundle"), worktree_override=wt,
                                 confirm=True)
        return next(r for r in rows if r["rule_id"] == "host-ci-x")

    def test_host_ci_row_confirms_at_check(self) -> None:
        row = self._run_host_ci_at_check(self._host_ci_row(""))
        self.assertEqual(self.runs(), 2)
        self.assertEqual((row["result"], row.get("flaky")), ("pass", True))

    def test_row_switch_off_on_host_ci_survives_normalization(self) -> None:
        chk = self._host_ci_row(", confirm_fail = false")
        self.assertIs(chk.get("confirm_fail"), False)
        row = self._run_host_ci_at_check(chk)
        self.assertEqual(self.runs(), 1)
        self.assertEqual(row["result"], "fail")
        self.assertNotIn("attempts", row)


# -- (4b): Check-time only ----------------------------------------------------------------

class CheckTimeOnly(_Base):
    def test_run_one_without_the_switch_runs_once(self) -> None:
        # The default every non-Check caller gets: no confirm switch.
        row = gates._run_one(self._chk(self.flaky_cmd()), cfg=self.cfg, cwd=self.tmp,
                             bundle=self._bundle())
        self.assertEqual(self.runs(), 1)
        self.assertEqual(row["result"], "fail")
        self.assertNotIn("flaky", row)

    def test_working_tree_regate_runs_once(self) -> None:
        self.cfg.gates_checks = [self._chk(self.flaky_cmd(), scope="repo")]
        result = gates.run_working_tree(self.cfg)
        self.assertEqual(self.runs(), 1)
        self.assertEqual(self._row(result)["result"], "fail")
        self.assertEqual(result["overall"], "fail")

    def test_integration_regate_runs_once(self) -> None:
        self.cfg.gates_checks = [self._chk(self.flaky_cmd(), scope="repo")]
        wt = self.tmp / "integ"
        wt.mkdir()
        result = gates.run_integration(self.cfg, wt, hold_lock=False)
        self.assertEqual(self.runs(), 1)
        self.assertEqual(self._row(result)["result"], "fail")
        self.assertEqual(result["overall"], "fail")


class PublishHostCiRunsOnce(_Base):
    """(4b) at publish, driven for real: `publish.publish` → `publish._host_ci_passes` →
    `gates._run_one`, against a toy bare origin + clone (the fixture of
    tests/test_host_ci.py `HostCiPatchedTree`), nothing mocked. The #311 contract is
    literal — a declared command that exits non-zero refuses the push — so a lucky second
    sample must never let a push through."""

    _PATCH = ("diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n"
              "@@ -1 +1,2 @@\n base\n+fix\n")

    def setUp(self) -> None:
        super().setUp()
        self.origin = self.tmp / "origin.git"
        self.repo = self.tmp / "checkout"
        sp.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)
        sp.run(["git", "clone", "-q", str(self.origin), str(self.repo)], check=True,
               capture_output=True)

        def git(*a: str) -> None:
            sp.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True)

        git("config", "user.email", "t@example.com")
        git("config", "user.name", "T")
        git("config", "commit.gpgsign", "false")
        (self.repo / "file.txt").write_text("base\n", encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", "base")
        git("branch", "-M", "main")
        git("push", "-q", "-u", "origin", "main")
        # A publish-capable config (mirrors tests/test_host_ci.py:_cfg): stub leaves,
        # own-repo remotes, the toy target pinned inside this test's tmp root.
        self.cfg = Config(
            root=self.tmp,
            bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process",
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
            base_remote="origin",
            repo_checkouts={"example-org/example-repo": str(self.repo)},
        )
        # An accepted (COMPLETE) bundle whose patch applies — publish's precondition.
        self.d = self.cfg.bundle("RG")
        self.d.mkdir(parents=True)
        (self.d / "brief.md").write_text(
            "- **Slug:** my-fix\n"
            "- **Repo + branch target:** example-org/example-repo @ main\n",
            encoding="utf-8")
        (self.d / "patch.diff").write_text(self._PATCH, encoding="utf-8")
        (self.d / "check-gates.json").write_text("{}", encoding="utf-8")
        shutil.copyfile(TEMPLATES / "SUMMARY.md.tpl", self.d / "SUMMARY.md")
        signoff.record(self.d / "SUMMARY.md", action="accept", by="Tester", date="2026-07-31")
        # The fail→pass command as a declared host-CI row, normalized as Config.load does.
        self.cfg.host_ci_checks = _normalize_host_ci([self.flaky_cmd()])

    def _origin_heads(self) -> list[str]:
        return sp.run(["git", "-C", str(self.origin), "for-each-ref",
                       "--format=%(refname)", "refs/heads"],
                      capture_output=True, text=True, check=True).stdout.split()

    def test_fail_then_pass_command_refuses_the_push_after_one_run(self) -> None:
        chk = self.cfg.host_ci_checks[0]
        # Precondition: a row the Check matrix WOULD confirm (see the control below) —
        # gating, the project switch on, no row opt-out. So only the publish caller's own
        # choice not to confirm can keep it at one run.
        self.assertIs(chk["gating"], True)
        self.assertIs(self.cfg.gates_confirm_gating_fail, True)
        self.assertNotIn("confirm_fail", chk)
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            rc = publish.publish(self.cfg, "RG", open_pr=False, by="T", today="2026-07-31")
        self.assertEqual(self.runs(), 1,
                         "publish's host-CI gate re-ran a failing command:\n" + buf.getvalue())
        self.assertEqual(rc, 1, buf.getvalue())                       # the push was refused
        self.assertEqual(self._origin_heads(), ["refs/heads/main"])  # nothing was pushed
        self.assertFalse((self.d / "publish.json").exists())
        record = json.loads((self.d / "host-ci.json").read_text(encoding="utf-8"))
        self.assertEqual(record["overall"], "fail")
        [row] = record["rows"]
        self.assertEqual(row["result"], "fail")
        self.assertEqual(row["path_line"], "evidence-run-1")
        self.assertNotIn("flaky", row)
        self.assertNotIn("attempts", row)

    def test_control_the_same_row_is_confirmed_at_check(self) -> None:
        # Same toy target, same command, through the Check matrix (run_gates → the lane
        # tree): it IS confirmed and records pass + flaky. So the publish test above does
        # not pass for want of a confirmable row or a real fail→pass command.
        with redirect_stderr(io.StringIO()):
            result = gates.run_gates(self.d, self.cfg)
        row = self._row(result, "host-ci-0")
        self.assertEqual(self.runs(), 2)
        self.assertEqual((row["result"], row.get("flaky")), ("pass", True))
        self.assertEqual(row.get("attempts"), ["fail", "pass"])


# -- (5): evidence -------------------------------------------------------------------------

class Evidence(_Base):
    def test_log_holds_both_runs_with_combined_outcome_header(self) -> None:
        self._check_run(self._chk(self.flaky_cmd()))
        text = (self.cfg.bundle("B") / "gate-logs" / "C4-confirm.log").read_text("utf-8")
        head, _, rest = text.partition("# ==== attempt 1")
        self.assertTrue(rest, f"no per-attempt block in the log:\n{text}")
        # The top-level header shows the row's recorded (combined) result.
        self.assertIn("# outcome: pass\n", head)
        one, _, two = rest.partition("# ==== attempt 2")
        self.assertTrue(two, f"no second attempt block in the log:\n{text}")
        self.assertIn("# exit: 1\n", one)
        self.assertIn("# outcome: fail\n", one)
        self.assertIn("output-of-run-1\nevidence-run-1\n", one)
        self.assertIn("# exit: 0\n", two)
        self.assertIn("# outcome: pass\n", two)
        self.assertIn("output-of-run-2\nevidence-run-2\n", two)

    def test_single_run_log_unchanged(self) -> None:
        self._check_run(self._chk(self._cmd("exit 0")))
        text = (self.cfg.bundle("B") / "gate-logs" / "C4-confirm.log").read_text("utf-8")
        self.assertNotIn("attempt", text)
        self.assertIn("# exit: 0\n# outcome: pass\n", text)


# -- (6): routing ---------------------------------------------------------------------------

class Routing(_Base):
    def test_flaky_row_is_one_human_section6_item(self) -> None:
        row = self._check_run(self._chk(self.flaky_cmd()))
        d = self.cfg.bundle("B")
        self.assertEqual(json.loads((d / "check-gates.json").read_text())["overall"], "pass")
        (d / "check-review.md").write_text("# Review\n\nNo findings.\n", encoding="utf-8")
        items = [it for it in assemble.collect_needs_human(d, self.cfg)
                 if row["check"] in it.text]
        self.assertEqual(len(items), 1, items)
        item = items[0]
        # HUMAN even though C4 is a gate element (which would be IMPL for a plain fail).
        self.assertEqual(item.kind, assemble.HUMAN)
        self.assertIn("first run fail, confirm re-run pass", item.text)

    def test_flaky_row_without_attempts_invents_no_history(self) -> None:
        # The recorder always writes `attempts` with `flaky`. If a row ever lacks it, the
        # §6 line must say so, not make up a "first run fail" the record does not hold.
        [text] = assemble._flaky_items({"rows": [
            {"check": "C4 verify", "result": "pass", "flaky": True,
             "path_line": "ok", "oracle": "x"}]})
        self.assertIn("C4 verify FLAKY — attempts not recorded", text)
        self.assertNotIn("first run", text)

    def test_clean_pass_adds_no_item(self) -> None:
        row = self._check_run(self._chk(self._cmd("exit 0")))
        d = self.cfg.bundle("B")
        (d / "check-review.md").write_text("# Review\n\nNo findings.\n", encoding="utf-8")
        self.assertFalse([it for it in assemble.collect_needs_human(d, self.cfg)
                          if row["check"] in it.text])


if __name__ == "__main__":
    unittest.main()
