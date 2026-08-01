"""The interactive leaves' checked exit contract — /handoff + Stop hook + session
carry-forward capture (issue #331; stdlib unittest, offline, no Claude/TTY).

Covers, against the #331 success criterion:
  (a) the rendered `/handoff` command exists and the per-leaf contract checks hold —
      planner (brief structure via whole_field + the #333/#340 dependency clause),
      signoff (VALID_DECISIONS token + rationale for iterate-*/discontinue),
      publisher (both artifacts + the instance's deterministic T4 lint, reused),
      act (the session NAMES the entry it wrote, against the driver's baseline);
  (b) the Stop hook ships, is registered, blocks a malformed/missing contract
      artifact and honours the deliberate-abandon escape hatch;
  (c) ids are REQUIRED — no scan mode, and no argument-hint advertises one;
  (d) the gate's verdict is exit status + report — nothing is written into the bundle;
  (e) the session carry-forward channel: flow captures the FULL sign-off rationale
      before the decision file is unlinked, and driver._carry_forward_into_brief
      MERGES it with the §9 delta it already extracts (registered + consumed together);
  (f) which contract applies derives from the render (interactive = true + agent),
      not a hardcoded leaf list.

RED on a tree without the fix: `pdca_harness.handoff`, the hook and the command do
not exist (the import below fails), and the carry-forward-merge assertion fails
against a `_carry_forward_into_brief` that reads recorded artifacts only.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from pdca_harness import driver, flow, handoff, leaves, state
from pdca_harness.config import Config, LeafConfig

TEMPLATE_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = TEMPLATE_ROOT / "templates"
HOOK = TEMPLATE_ROOT / ".claude" / "hooks" / "handoff_guard.py"
SETTINGS = TEMPLATE_ROOT / ".claude" / "settings.json"


def _first(*names: str) -> Path:
    """`<name>.jinja` in the template checkout, `<name>` in a RENDERED instance — this
    suite runs in both (tests/test_render_and_run drives the generated project's own
    tests, the same dual-home convention as test_seed_spill's .gitignore check)."""
    paths = [TEMPLATE_ROOT / n for n in names]
    return next((p for p in paths if p.is_file()), paths[0])


COMMAND = _first(".claude/commands/handoff.md.jinja", ".claude/commands/handoff.md")
GITIGNORE = _first(".gitignore.jinja", ".gitignore")

_AUTHORED_BRIEF = (
    "# Brief — issue 7 / real\n\n"
    "- **Slug:** real-slug\n"
    "- **Defect:** something observable is wrong.\n"
    # Deliberately MULTI-LINE: the corpus trap — line-based parse_fields reads a
    # wrapped value as empty; the contract must read it via whole_field (#336).
    "- **Success criterion:** the observable condition that\n"
    "  means it is fixed, wrapped onto a continuation line.\n"
    "- **Repo + branch target:** example-org/example-repo @ main\n"
    "- **External dependencies:** none\n"
    # NO `Falsifiability`, NO `Test file` value: both are legitimately absent in the
    # measured corpus (52/85 and 7 bundles respectively) and must NOT be required.
)

# A recordable SUMMARY: §9 with an Outcome field and a non-empty §6 (signoff.unrecordable).
_SUMMARY = (
    "# SUMMARY\n\n"
    "## 6. NEEDS-HUMAN\n"
    "- [x] cleared by the human\n\n"
    "## 9. Check sign-off\n"
    "- Outcome:\n"
    "- By / date:\n"
    "- Iteration delta (if iterating):\n"
)


def _cfg(root: Path) -> Config:
    return Config(
        root=root,
        bundle_root=root / "results",
        process_dir=root / "process",
        templates_dir=TEMPLATES,
        default_branch="main",
        tracker_system="github",
        tracker_url="",
        issue_id_example="#1",
        builder=LeafConfig(mode="command", family="claude"),
        reviewer=LeafConfig(mode="command", family="codex"),
        planner=LeafConfig(mode="command", family="claude", interactive=True,
                           agent="planner"),
        signoff=LeafConfig(mode="command", family="claude", interactive=True,
                           agent="signoff"),
        publisher=LeafConfig(mode="command", family="claude", interactive=True,
                             agent="publisher"),
        act=LeafConfig(mode="command", family="claude", interactive=True, agent="act"),
        splitter=LeafConfig(mode="command", family="claude", interactive=True,
                            agent="splitter"),
    )


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = _cfg(self.tmp)

    def bundle(self, iid: str = "7") -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True, exist_ok=True)
        return d


class RenderedArtifacts(unittest.TestCase):
    def test_handoff_command_ships_and_requires_an_id(self) -> None:
        # (a)+(c): the rendered command exists; its one argument is a REQUIRED id and
        # no argument-hint advertises an optional/scan form.
        self.assertTrue(COMMAND.is_file(), f"missing {COMMAND}")
        text = COMMAND.read_text(encoding="utf-8")
        hint = re.search(r"(?m)^argument-hint:\s*(.+)$", text)
        self.assertIsNotNone(hint, "the command must declare an argument-hint")
        self.assertEqual(hint.group(1).strip(), "<issue_id>")
        self.assertNotIn("[", hint.group(1), "no optional-argument (scan-mode) hint")
        self.assertIn("no scan mode", text)
        self.assertIn("$1", text)  # the id is actually passed through
        self.assertIn("handoff_guard.py", text)  # single-sourced with the hook

    def test_stop_hook_ships_and_is_registered(self) -> None:
        # (b): the hook file exists and settings.json registers it on Stop, so the
        # check is non-optional — a slash command cannot terminate its own session.
        self.assertTrue(HOOK.is_file(), f"missing {HOOK}")
        settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
        stops = settings.get("hooks", {}).get("Stop", [])
        cmds = [h.get("command", "") for entry in stops for h in entry.get("hooks", [])]
        self.assertTrue(any("handoff_guard.py" in c for c in cmds),
                        "settings.json must register handoff_guard.py as a Stop hook")

    def test_hook_module_imports_offline(self) -> None:
        spec = importlib.util.spec_from_file_location("handoff_guard", HOOK)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # must be import-side-effect free
        self.assertTrue(callable(mod.main))

    def test_state_prefix_is_gitignored(self) -> None:
        gitignore = GITIGNORE.read_text(encoding="utf-8")
        self.assertIn(f"{handoff.STATE_PREFIX}*.json", gitignore)


class ContractFromRender(Base):
    def test_contracts_derive_from_interactive_leaves(self) -> None:
        # (f): the four interactive leaves carry contracts; headless ones never do.
        self.assertEqual(set(handoff.contracts(self.cfg)),
                         {"planner", "signoff", "publisher", "act"})
        self.cfg.planner = LeafConfig(mode="command", interactive=False)
        self.assertNotIn("planner", handoff.contracts(self.cfg))

    def test_interactive_leaf_without_a_contract_is_unchecked(self) -> None:
        # The splitter is interactive in the render but has no exit contract — it must
        # be neither blocked nor checked.
        self.assertIn("splitter", handoff.interactive_roles(self.cfg))
        self.assertNotIn("splitter", handoff.contracts(self.cfg))
        self.assertEqual(handoff.stop_problems(self.cfg, "splitter", {}), [])

    def test_agent_names_come_from_the_render(self) -> None:
        self.cfg.signoff = LeafConfig(mode="command", interactive=True, agent="custom")
        self.assertEqual(handoff.interactive_roles(self.cfg)["signoff"], "custom")


class PlannerContract(Base):
    def test_missing_and_placeholder_briefs_fail(self) -> None:
        d = self.bundle()
        self.assertTrue(handoff.check_planner(d, self.cfg))
        shutil.copyfile(TEMPLATES / "brief.md.tpl", d / "brief.md")
        problems = handoff.check_planner(d, self.cfg)
        self.assertTrue(any("template" in p for p in problems))

    def test_authored_brief_with_multiline_criterion_passes(self) -> None:
        # Corpus traps (#166): a wrapped Success criterion must be read whole, and
        # absent Falsifiability / Test file must NOT fail the contract.
        d = self.bundle()
        (d / "brief.md").write_text(_AUTHORED_BRIEF, encoding="utf-8")
        self.assertEqual(handoff.check_planner(d, self.cfg), [])

    def test_absent_brief_allowed_only_for_the_batch_wrinkle(self) -> None:
        d = self.bundle()
        self.assertEqual(handoff.check_planner(d, self.cfg, allow_absent=True), [])
        self.assertTrue(handoff.check_planner(d, self.cfg, allow_absent=False))

    def test_dependency_clause_registration_and_probe(self) -> None:
        # (a) planner: every backticked token must name a [[doctor.checks]] row whose
        # detect cmd exits 0 — the #340 probe, reused, not re-declared.
        d = self.bundle()
        brief = _AUTHORED_BRIEF.replace(
            "- **External dependencies:** none",
            "- **External dependencies:** `frobnicator` (build)")
        (d / "brief.md").write_text(brief, encoding="utf-8")
        # Unregistered: no pdca.toml row at all.
        problems = handoff.check_planner(d, self.cfg)
        self.assertTrue(any("frobnicator" in p and "doctor.checks" in p
                            for p in problems))
        # Registered but ABSENT on this host: the detect cmd exits non-zero.
        (self.tmp / "pdca.toml").write_text(
            '[[doctor.checks]]\nid = "frobnicator"\ncmd = "exit 3"\n'
            'hint = "install frobnicator"\n', encoding="utf-8")
        problems = handoff.check_planner(d, self.cfg)
        self.assertTrue(any("exited 3" in p for p in problems))
        # Registered and present: exits 0 → the contract is discharged.
        (self.tmp / "pdca.toml").write_text(
            '[[doctor.checks]]\nid = "frobnicator"\ncmd = "true"\n'
            'hint = "install frobnicator"\n', encoding="utf-8")
        self.assertEqual(handoff.check_planner(d, self.cfg), [])

    def test_no_check_annotation_is_exempt(self) -> None:
        d = self.bundle()
        brief = _AUTHORED_BRIEF.replace(
            "- **External dependencies:** none",
            "- **External dependencies:** `partition-cluster` (no-check: topology)")
        (d / "brief.md").write_text(brief, encoding="utf-8")
        self.assertEqual(handoff.check_planner(d, self.cfg), [])


class SignoffContract(Base):
    def test_missing_invalid_and_bare_iterate_fail(self) -> None:
        d = self.bundle()
        self.assertTrue(handoff.check_signoff(d, self.cfg))  # missing
        (d / leaves.SIGNOFF_DECISION).write_text("maybe\n", encoding="utf-8")
        problems = handoff.check_signoff(d, self.cfg)
        self.assertTrue(any("'maybe'" in p for p in problems))  # invalid token
        (d / leaves.SIGNOFF_DECISION).write_text("iterate-do\n", encoding="utf-8")
        problems = handoff.check_signoff(d, self.cfg)
        self.assertTrue(any("rationale" in p for p in problems))  # no rationale

    def test_accept_alone_and_iterate_with_rationale_pass(self) -> None:
        d = self.bundle()
        (d / leaves.SIGNOFF_DECISION).write_text("accept\n", encoding="utf-8")
        self.assertEqual(handoff.check_signoff(d, self.cfg), [])
        (d / leaves.SIGNOFF_DECISION).write_text(
            "iterate-do\nthe probe hid the cause; remove it\n", encoding="utf-8")
        self.assertEqual(handoff.check_signoff(d, self.cfg), [])
        (d / leaves.SIGNOFF_DECISION).write_text("discontinue\n", encoding="utf-8")
        self.assertTrue(handoff.check_signoff(d, self.cfg))  # discontinue needs why


class PublisherContract(Base):
    def test_missing_artifacts_fail(self) -> None:
        d = self.bundle("X")  # non-numeric id: no tracker-id lint leg
        problems = handoff.check_publisher(d, self.cfg)
        self.assertEqual(len(problems), 2)  # both artifacts named missing

    def test_deterministic_lint_is_reused(self) -> None:
        # The instance's own T4 rules (cli.contribution_problems) judge the PR body —
        # a missing `**User impact:**` opener fails the contract.
        d = self.bundle("X")
        (d / "commit-msg.txt").write_text("summary\n\nbody\n", encoding="utf-8")
        (d / "pr-description.md").write_text("## Summary\nno impact line\n",
                                             encoding="utf-8")
        problems = handoff.check_publisher(d, self.cfg)
        self.assertTrue(any("User impact" in p for p in problems))
        (d / "pr-description.md").write_text(
            "## Summary\n**User impact:** users see X.\n\n## Root cause\ny\n",
            encoding="utf-8")
        self.assertEqual(handoff.check_publisher(d, self.cfg), [])


class ActContract(Base):
    def _log(self, text: str) -> None:
        self.cfg.process_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.process_dir / "act-log.md").write_text(text, encoding="utf-8")

    def test_id_is_required(self) -> None:
        self.assertTrue(handoff.check_act(self.cfg, "", {}))

    def test_named_entry_must_postdate_the_baseline(self) -> None:
        # The driver supplies the session-start baseline — an end-of-session command
        # structurally cannot take one — so authorship is distinguishable.
        old = "# Act log\n\n# Act review — 2026-07-01 — cycles considered: 1\n"
        self._log(old)
        baseline = {"act_log_len": len(old),
                    "act_log_sha": handoff._sha(old)}
        # Unchanged log: the named entry predates the session.
        self.assertTrue(handoff.check_act(self.cfg, "2026-07-01", baseline))
        # Session appended a NEW entry but names the OLD one: still a problem.
        self._log(old + "\n# Act review — 2026-08-01 — cycles considered: 2\n")
        self.assertTrue(handoff.check_act(self.cfg, "2026-07-01", baseline))
        # Naming the entry THIS session wrote passes.
        self.assertEqual(handoff.check_act(self.cfg, "2026-08-01", baseline), [])

    def test_entry_absent_from_log_fails(self) -> None:
        self._log("# Act log\n")
        self.assertTrue(handoff.check_act(self.cfg, "2026-08-01", {}))


class SessionRegistration(Base):
    def test_session_registers_env_and_cleans_up(self) -> None:
        d = self.bundle()
        with handoff.session(self.cfg, "signoff", [d]) as env:
            self.assertEqual(env[handoff.ENV_ROLE], "signoff")
            spath = Path(env[handoff.ENV_STATE])
            self.assertTrue(spath.name.startswith(handoff.STATE_PREFIX))
            self.assertEqual(spath.parent, self.tmp)  # project root, NOT the bundle
            data = json.loads(spath.read_text(encoding="utf-8"))
            self.assertEqual(data["bundles"], [str(d)])
        self.assertFalse(spath.exists())  # reaped by the driver

    def test_non_interactive_leaf_gets_no_contract_env(self) -> None:
        self.cfg.signoff = LeafConfig(mode="command", interactive=False)
        with handoff.session(self.cfg, "signoff", []) as env:
            self.assertEqual(env, {})

    def test_act_session_carries_the_baseline(self) -> None:
        self.cfg.process_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.process_dir / "act-log.md").write_text("# Act log\n", encoding="utf-8")
        with handoff.session(self.cfg, "act") as env:
            data = json.loads(Path(env[handoff.ENV_STATE]).read_text(encoding="utf-8"))
            self.assertEqual(data["baseline"]["act_log_len"], len("# Act log\n"))
            self.assertIn("act_log_sha", data["baseline"])

    def test_abandon_reason_is_reported_when_the_driver_reaps(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            with handoff.session(self.cfg, "signoff", []) as env:
                handoff.record_abandon(Path(env[handoff.ENV_STATE]), "human said stop")
        self.assertIn("human said stop", err.getvalue())


class StopVerdict(Base):
    def test_blocks_missing_decision_and_allows_after_discharge(self) -> None:
        d = self.bundle()
        st = {"role": "signoff", "bundles": [str(d)]}
        problems = handoff.stop_problems(self.cfg, "signoff", st)
        self.assertTrue(any(d.name in p for p in problems))
        (d / leaves.SIGNOFF_DECISION).write_text(
            "iterate-do\nwhy: symptom-guard\n", encoding="utf-8")
        self.assertEqual(handoff.stop_problems(self.cfg, "signoff", st), [])

    def test_abandon_is_the_escape_hatch(self) -> None:
        d = self.bundle()
        st = {"role": "signoff", "bundles": [str(d)], "abandoned": "deliberate"}
        self.assertEqual(handoff.stop_problems(self.cfg, "signoff", st), [])

    def test_unknown_work_set_requires_a_named_pass(self) -> None:
        # CSV-batch planner / act: the driver could not register bundles at spawn, so
        # the session must have NAMED its work via a passing /handoff.
        self.assertTrue(handoff.stop_problems(self.cfg, "planner", {"bundles": []}))
        self.assertEqual(handoff.stop_problems(
            self.cfg, "planner", {"bundles": [], "passed": ["issue_9"]}), [])
        self.assertTrue(handoff.stop_problems(self.cfg, "act", {}))
        self.assertEqual(handoff.stop_problems(
            self.cfg, "act", {"passed": ["2026-08-01"]}), [])

    def test_batch_planner_may_leave_a_bundle_unbriefed_not_malformed(self) -> None:
        d = self.bundle()
        st = {"role": "planner", "bundles": [str(d)], "require_artifact": False}
        self.assertEqual(handoff.stop_problems(self.cfg, "planner", st), [])  # absent ok
        shutil.copyfile(TEMPLATES / "brief.md.tpl", d / "brief.md")
        self.assertTrue(handoff.stop_problems(self.cfg, "planner", st))  # malformed not


class RunCheckVerdict(Base):
    def test_ids_required_and_verdict_is_exit_status_plus_report(self) -> None:
        d = self.bundle()
        (d / leaves.SIGNOFF_DECISION).write_text("accept\n", encoding="utf-8")
        before = sorted(p.name for p in d.iterdir())
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            rc = handoff.run_check(self.cfg, "issue_7",
                                   environ={handoff.ENV_ROLE: "signoff"})
        self.assertEqual(rc, 0)
        self.assertIn("PASS", out.getvalue())
        # (d): the gate writes NOTHING into the bundle — no handoff.json, no marker.
        self.assertEqual(sorted(p.name for p in d.iterdir()), before)

    def test_fail_reports_the_problems(self) -> None:
        self.bundle()  # no decision file
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            rc = handoff.run_check(self.cfg, "7",
                                   environ={handoff.ENV_ROLE: "signoff"})
        self.assertEqual(rc, 1)
        self.assertIn("FAIL", out.getvalue())
        self.assertIn(leaves.SIGNOFF_DECISION, out.getvalue())

    def test_no_active_contract_is_a_distinct_verdict(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            rc = handoff.run_check(self.cfg, "7", environ={})
        self.assertEqual(rc, 2)

    def test_a_pass_is_recorded_in_the_session_state(self) -> None:
        d = self.bundle()
        (d / leaves.SIGNOFF_DECISION).write_text("accept\n", encoding="utf-8")
        spath = self.tmp / f"{handoff.STATE_PREFIX}t.json"
        spath.write_text('{"role": "signoff", "passed": []}', encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            handoff.run_check(self.cfg, "issue_7",
                              environ={handoff.ENV_ROLE: "signoff",
                                       handoff.ENV_STATE: str(spath)})
        self.assertIn("issue_7",
                      json.loads(spath.read_text(encoding="utf-8"))["passed"])


class SessionCarryForwardChannel(Base):
    """Criterion (e): registered by flow at decision-consumption, consumed by
    driver._carry_forward_into_brief — shipped together."""

    def test_flow_captures_the_full_rationale_before_the_unlink(self) -> None:
        d = self.bundle()
        (d / "SUMMARY.md").write_text(_SUMMARY, encoding="utf-8")
        (d / leaves.SIGNOFF_DECISION).write_text(
            "iterate-do\nthe probe hid the real cause; remove it\n"
            "and compute the value lazily\n", encoding="utf-8")
        with redirect_stderr(io.StringIO()):
            action = flow._apply_decision(self.cfg, d, by="T", today="2026-08-01",
                                          apply_now=False)
        self.assertEqual(action, "iterate-do")
        self.assertFalse((d / leaves.SIGNOFF_DECISION).exists())  # consumed, as before
        captured = (d / state.SESSION_CARRY).read_text(encoding="utf-8")
        self.assertIn("the probe hid the real cause; remove it", captured)
        self.assertIn("and compute the value lazily", captured)  # structure kept

    def test_carry_forward_merges_the_session_capture_with_section9(self) -> None:
        # RED on the pre-fix tree: _carry_forward_into_brief reads recorded artifacts
        # only, so the session capture never reaches the brief.
        d = self.bundle()
        (d / "brief.md").write_text("# Brief\n- **Slug:** x\n", encoding="utf-8")
        (d / "SUMMARY.md").write_text(
            "## 9. Check sign-off\n- Outcome: iterated-to-Do\n"
            "- Iteration delta (if iterating): the probe hid the real cause; remove it "
            "and compute the value lazily\n", encoding="utf-8")
        (d / state.SESSION_CARRY).write_text(
            "the probe hid the real cause; remove it\nand compute the value lazily\n",
            encoding="utf-8")
        driver._carry_forward_into_brief(d, 1)
        brief = (d / "brief.md").read_text(encoding="utf-8")
        self.assertIn("## Iteration 1 — carry-forward", brief)
        self.assertIn("Sign-off session carry-forward", brief)
        self.assertIn("and compute the value lazily", brief)

    def test_single_line_capture_identical_to_delta_is_not_duplicated(self) -> None:
        d = self.bundle()
        (d / "brief.md").write_text("# Brief\n- **Slug:** x\n", encoding="utf-8")
        (d / "SUMMARY.md").write_text(
            "## 9. Check sign-off\n- Outcome: iterated-to-Do\n"
            "- Iteration delta (if iterating): one line only\n", encoding="utf-8")
        (d / state.SESSION_CARRY).write_text("one line only\n", encoding="utf-8")
        driver._carry_forward_into_brief(d, 1)
        brief = (d / "brief.md").read_text(encoding="utf-8")
        self.assertIn("Sign-off rationale: one line only", brief)
        self.assertNotIn("Sign-off session carry-forward", brief)

    def test_capture_is_archived_with_its_attempt(self) -> None:
        # In DOWNSTREAM_OF_BRIEF ⇒ _archive_iteration moves it; it never leaks into
        # the next attempt's inputs.
        self.assertIn(state.SESSION_CARRY, state.DOWNSTREAM_OF_BRIEF)


if __name__ == "__main__":
    unittest.main()
