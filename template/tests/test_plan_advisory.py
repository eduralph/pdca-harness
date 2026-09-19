"""Plan-beat advisory reviewers (issue #301; stdlib unittest, offline).

Mirrors the Check advisory slice (test_adversary / vendor-complement) at Plan: opt-in
[[leaves.plan_advisory]] leaves review the BRIEF right after Plan, write
plan-advisory-<id>.md, the planner gets one bounded revision pass, and
plan-advisory-benefit.json records whether the review changed anything — surfaced in
SUMMARY §10 (always) and §6 (only when findings were left unrevised).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pdca_harness import assemble, leaves
from pdca_harness.config import Config, LeafConfig

_REVIEWER = {
    "id": "plan-reviewer",
    "mode": "stub",
    "role": "refute the brief: wrong root cause, untestable criterion, hidden scope",
}

_FIXTURES = Path(__file__).resolve().parent / "fixtures"

# A stand-in `claude` for issue #526: a Python interpreter in the leaf's own argv that
# replays PINNED vendor bytes — what the real CLI printed on a host whose sandbox could not
# start (tests/fixtures/README.md) — so each run goes through the production spawn, stream
# drain and classification, and nothing the fix adds is mocked. `_invoke` appends the
# stream and confinement flags (ignored here) and feeds the prompt on stdin. $FAKE_STDOUT /
# $FAKE_STDERR name the files to replay, $FAKE_RC is the exit code, and $FAKE_WRITES names
# a review the stand-in delivers into its cwd, as the Write tool would.
_FAKE_CLAUDE = (
    "import os,pathlib,sys\n"
    "sys.stdin.read()\n"
    "e=os.environ\n"
    "if e.get('FAKE_WRITES'):\n"
    "    pathlib.Path('plan-advisory-plan-reviewer.md').write_bytes(\n"
    "        pathlib.Path(e['FAKE_WRITES']).read_bytes())\n"
    "sys.stdout.buffer.write(pathlib.Path(e['FAKE_STDOUT']).read_bytes())\n"
    "sys.stderr.buffer.write(pathlib.Path(e['FAKE_STDERR']).read_bytes())\n"
    "sys.exit(int(e['FAKE_RC']))\n"
)


def _fixture_lines(name: str) -> list[str]:
    """A pinned stream, one vendor event per line (tests/fixtures/README.md)."""
    return (_FIXTURES / name).read_text(encoding="utf-8").splitlines(keepends=True)


def _cfg(root: Path, *, plan_advisory=None, selection=None, planner=None) -> Config:
    return Config(
        root=root, bundle_root=root / "results", process_dir=root / "process",
        templates_dir=root / "templates", default_branch="main", tracker_system="github",
        tracker_url="", issue_id_example="#1",
        builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
        planner=planner or LeafConfig(mode="stub", interactive=True),
        plan_advisory_leaves=list(plan_advisory or []),
        plan_advisory_selection=dict(selection or {}))


def _brief(cfg: Config, iid: str, *, difficulty: str | None = None,
           placeholder: bool = False) -> Path:
    d = cfg.bundle(iid)
    d.mkdir(parents=True, exist_ok=True)
    body = "" if placeholder else f"- **Slug:** {iid.lower()}\n- **Defect:** x.\n"
    if difficulty:
        body += f"- **Difficulty:** {difficulty}\n"
    (d / "brief.md").write_text(body or "# template\n", encoding="utf-8")
    return d


class PlanAdvisory(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_off_leaves_the_plan_beat_untouched(self) -> None:
        cfg = _cfg(self.tmp)                              # no plan_advisory config
        d = self.tmp / "results" / "issue_OFF"
        leaves.do_plan(d, cfg)                            # stub planner briefs it
        self.assertTrue((d / "brief.md").exists())
        self.assertEqual(list(d.glob("plan-advisory-*")), [])

    def test_stub_leaf_writes_artifact_and_benefit_record(self) -> None:
        cfg = _cfg(self.tmp, plan_advisory=[_REVIEWER])
        d = _brief(cfg, "R1")
        leaves.run_plan_advisory(d, cfg)
        art = leaves.plan_advisory_artifact(d, "plan-reviewer")
        self.assertTrue(art.exists())
        self.assertIn("NEEDS-HUMAN", art.read_text(encoding="utf-8"))
        benefit = json.loads((d / "plan-advisory-benefit.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(benefit["findings"], 1)
        self.assertFalse(benefit["revised"])              # stub planner: no revision pass
        self.assertEqual(benefit["leaves"], ["plan-reviewer"])
        self.assertEqual(benefit["before_sha"], benefit["after_sha"])

    def test_placeholder_brief_is_never_reviewed(self) -> None:
        # A template copy is boilerplate, not a plan — reviewing it would grade the
        # template. state() reads it UNPLANNED for the same reason (#113).
        cfg = _cfg(self.tmp, plan_advisory=[_REVIEWER])
        d = _brief(cfg, "PH", placeholder=True)
        leaves.run_plan_advisory(d, cfg)
        self.assertEqual(list(d.glob("plan-advisory-*")), [])

    def test_check_only_sandbox_grants_are_withheld_from_the_plan_review(self) -> None:
        # #301 review round 6: [leaves.sandbox] network_access / unsandboxed_commands
        # are CHECK-leaf opt-ins (Docker-backed gates, the reviewer's prior-art fetch).
        # A plan review only reads the brief + pinned target, so neither grant may
        # reach the reviewer leaf — it runs under the vendor's default sandbox.
        reviewer = {"id": "codex-lens", "mode": "command", "family": "codex",
                    "argv": ["codex"]}
        cfg = _cfg(self.tmp, plan_advisory=[reviewer])
        cfg.leaf_network_access = True
        cfg.leaf_unsandboxed_commands = ["cargo xtask conformance"]
        d = _brief(cfg, "GRANTS")
        seen: dict[str, list[str]] = {}

        def fake(leaf, cwd, prompt, **kw):
            seen["extra"] = list(kw.get("extra_argv") or [])
            return None

        with mock.patch.object(leaves, "_invoke_leaf_resilient", side_effect=fake), \
                mock.patch.object(leaves, "_seed_sandbox_settings") as seed:
            leaves.run_plan_advisory(d, cfg)
        seed.assert_not_called()                          # no seeded Check exemptions
        self.assertNotIn("sandbox_workspace_write.network_access=true",
                         seen["extra"])                   # no codex network grant

    def test_claude_plan_reviewer_gets_a_minimal_failclosed_sandbox(self) -> None:
        # #301 review round 8: withholding the Check seed must not leave the temp
        # cwd with NO sandbox policy — claude's sandbox.enabled defaults FALSE, so a
        # Bash-capable plan reviewer would run unconfined. The runner seeds a
        # MINIMAL policy (enabled, fail-closed, none of the Check grants) and passes
        # the confinement flag exactly because the seeded file exists.
        reviewer = {"id": "claude-lens", "mode": "command", "family": "claude",
                    "argv": ["claude"]}
        cfg = _cfg(self.tmp, plan_advisory=[reviewer])
        cfg.leaf_unsandboxed_commands = ["cargo xtask conformance"]  # must NOT leak in
        d = _brief(cfg, "MINSBX")
        seen: dict = {}

        def fake(leaf, cwd, prompt, **kw):
            seen["extra"] = list(kw.get("extra_argv") or [])
            seen["settings"] = json.loads(
                (Path(cwd) / ".claude" / "settings.json").read_text(encoding="utf-8"))
            return None

        with mock.patch.object(leaves, "_invoke_leaf_resilient", side_effect=fake):
            leaves.run_plan_advisory(d, cfg)
        sbx = seen["settings"]["sandbox"]
        self.assertIs(sbx["enabled"], True)
        self.assertIs(sbx["allowUnsandboxedCommands"], False)
        self.assertIs(sbx["failIfUnavailable"], True)     # refuse > run unconfined
        self.assertNotIn("excludedCommands", sbx)         # Check exemptions withheld
        self.assertNotIn("network", sbx)                  # no network grant either
        for flag in ("--setting-sources", "project"):     # confinement flag rides
            self.assertIn(flag, seen["extra"])

    def test_when_gates_on_a_brief_field(self) -> None:
        gated = {**_REVIEWER, "when": {"field": "difficulty", "substring": "high"}}
        cfg = _cfg(self.tmp, plan_advisory=[gated])
        high = _brief(cfg, "H", difficulty="high")
        low = _brief(cfg, "L", difficulty="low")
        leaves.run_plan_advisory(high, cfg)
        leaves.run_plan_advisory(low, cfg)
        self.assertTrue(leaves.plan_advisory_artifact(high, "plan-reviewer").exists())
        self.assertFalse(leaves.plan_advisory_artifact(low, "plan-reviewer").exists())
        self.assertFalse((low / "plan-advisory-benefit.json").exists())

    def test_unavailable_review_never_triggers_the_revision_pass(self) -> None:
        # #301 review round 3: a NOT-COMPLETED placeholder's NEEDS-HUMAN line reports
        # infrastructure, not the brief — it must not count as a finding, trigger the
        # planner revision, or pollute the benefit telemetry. It still folds into §6.
        cfg = _cfg(self.tmp, plan_advisory=[_REVIEWER],
                   planner=LeafConfig(mode="command", family="claude", interactive=True,
                                      argv=["claude"]))
        d = _brief(cfg, "UNAV")
        with mock.patch.object(leaves, "_stub_plan_advisory",
                               side_effect=lambda dd, spec, lid:
                               leaves._plan_advisory_unavailable(dd, lid, "leaf failed")), \
                mock.patch.object(leaves, "_invoke") as inv:
            leaves.run_plan_advisory(d, cfg)
        inv.assert_not_called()                           # no revision over an outage
        benefit = json.loads((d / "plan-advisory-benefit.json").read_text(encoding="utf-8"))
        self.assertEqual(benefit["findings"], 0)
        self.assertFalse(benefit["revised"])

    def test_failed_revision_pass_never_crashes_the_plan_beat(self) -> None:
        # #301 review round 3: the revision is an opt-in advisory step — a planner that
        # exits non-zero there must not fail the Plan beat or skip the benefit records.
        cfg = _cfg(self.tmp, plan_advisory=[_REVIEWER],
                   planner=LeafConfig(mode="command", family="claude", interactive=True,
                                      argv=["claude"]))
        d = _brief(cfg, "RFAIL")
        with mock.patch.object(leaves, "_invoke",
                               side_effect=RuntimeError("planner died")):
            leaves.run_plan_advisory(d, cfg)              # no raise
        benefit = json.loads((d / "plan-advisory-benefit.json").read_text(encoding="utf-8"))
        self.assertFalse(benefit["revised"])              # briefs left as authored
        self.assertGreaterEqual(benefit["findings"], 1)

    def test_revision_pass_records_revised_true(self) -> None:
        # Command-mode planner + findings → ONE revision invocation; a changed brief
        # hashes different → revised: true.
        cfg = _cfg(self.tmp, plan_advisory=[_REVIEWER],
                   planner=LeafConfig(mode="command", family="claude", interactive=True,
                                      argv=["claude"]))
        d = _brief(cfg, "REV")

        def fake_invoke(leaf, cwd, prompt, **kw):
            self.assertIn("REVISION pass", prompt)
            self.assertIn(str(d), prompt)
            (d / "brief.md").write_text(
                (d / "brief.md").read_text(encoding="utf-8")
                + "\nPlan-review response: criterion tightened.\n", encoding="utf-8")

        with mock.patch.object(leaves, "_invoke", side_effect=fake_invoke) as inv:
            leaves.run_plan_advisory(d, cfg)
        self.assertEqual(inv.call_count, 1)               # bounded: exactly one pass
        benefit = json.loads((d / "plan-advisory-benefit.json").read_text(encoding="utf-8"))
        self.assertTrue(benefit["revised"])
        self.assertNotEqual(benefit["before_sha"], benefit["after_sha"])

    # -- #526: a host where the vendor sandbox cannot start ---------------------------
    def _run_stand_in(self, iid: str, *, stdout: list[str], stderr: str = "",
                      rc: int = 0, writes: str = "") -> tuple[Path, str, dict]:
        """The plan-advisory pass over a fresh bundle, its one claude leaf played by the
        `_FAKE_CLAUDE` stand-in. Returns the bundle, its artifact and benefit record."""
        files = {}
        for name, body in (("stdout", "".join(stdout)), ("stderr", stderr),
                           ("writes", writes)):
            files[name] = self.tmp / f"{iid}.{name}"
            files[name].write_text(body, encoding="utf-8")
        reviewer = {"id": "plan-reviewer", "mode": "command", "family": "claude",
                    "argv": [sys.executable, "-c", _FAKE_CLAUDE]}
        cfg = _cfg(self.tmp, plan_advisory=[reviewer])
        d = _brief(cfg, iid)
        env = {"FAKE_STDOUT": str(files["stdout"]), "FAKE_STDERR": str(files["stderr"]),
               "FAKE_RC": str(rc), "FAKE_WRITES": str(files["writes"]) if writes else ""}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stderr(io.StringIO()):
            leaves.run_plan_advisory(d, cfg)
        text = leaves.plan_advisory_artifact(d, "plan-reviewer").read_text(encoding="utf-8")
        benefit = json.loads((d / "plan-advisory-benefit.json").read_text(encoding="utf-8"))
        return d, text, benefit

    def test_a_sandbox_that_cannot_start_is_filed_as_sandbox_infra(self) -> None:
        # #526 (a)+(b). The seeded fail-closed sandbox cannot start on a host that denies
        # unprivileged user namespaces: the CLI exits 0, writes nothing, and every Bash
        # call dies in the sandbox's own startup (pinned vendor bytes). That was filed
        # `human-empty` ("do not assume an infra blip") and recorded as `findings: 0,
        # revised: false` — the record of a review that ran and found nothing.
        d, text, benefit = self._run_stand_in(
            "SBX", stdout=_fixture_lines("claude_sandbox_cannot_start.stream.jsonl"))
        self.assertIn("<!-- pdca:leaf-status sandbox-empty -->", text)  # (a) infra marker
        self.assertEqual(assemble.leaf_status(text), "sandbox-empty")
        self.assertIn("the vendor sandbox could not start", text)        # (a) the cause,
        self.assertIn("apply-seccomp: write /proc/self/setgroups", text)  # its evidence
        item, = assemble._items_from_artifact(text)                      # what §6 shows
        self.assertIn("could not start on this host — fix the host", item.text)
        self.assertNotIn("needs a human", item.text)
        self.assertIs(benefit.get("completed"), False)                   # (b) not a clean 0
        self.assertEqual(benefit.get("not_completed"), {"plan-reviewer": "sandbox-empty"})
        self.assertEqual((benefit["findings"], benefit["revised"]), (0, False))
        line, = assemble._plan_advisory_act_lines(d)                     # (b) what act reads
        self.assertTrue(line.startswith("- Plan advisory NOT completed (sandbox-empty)"),
                        line)

    def test_a_cli_that_refuses_to_start_without_its_sandbox_is_sandbox_infra(self) -> None:
        # #526: `failIfUnavailable` doing its job on a host missing bubblewrap — the CLI
        # refuses to start at all (exit 1, pinned vendor bytes). The same environment
        # fault, said by the CLI itself; it was filed `human-empty` as well.
        stderr = (_FIXTURES / "claude_sandbox_refused.stderr.txt").read_text(encoding="utf-8")
        d, text, benefit = self._run_stand_in(
            "REFUSED", stdout=_fixture_lines("claude_sandbox_refused.stream.jsonl"),
            stderr=stderr, rc=1)
        self.assertIn("<!-- pdca:leaf-status sandbox-empty -->", text)
        self.assertIn("sandbox required but unavailable", text)
        self.assertEqual(benefit.get("not_completed"), {"plan-reviewer": "sandbox-empty"})

    def test_a_review_delivered_without_bash_is_kept_and_says_so(self) -> None:
        # #526 (c): the findings still reach the bundle while Bash is dead, by the Write
        # route (the stand-in writes its review into its cwd, as Write does, while
        # replaying the dead-Bash stream). The artifact is the leaf's real review, and it
        # states that Bash was unavailable although this review never says so — the
        # harness does, on the vendor's evidence, rather than trusting the leaf to.
        review = ("# Plan advisory - plan-reviewer\n\n"
                  "- NEEDS-HUMAN - the success criterion is unverifiable (brief.md:3)\n")
        d, text, benefit = self._run_stand_in(
            "WRITE", stdout=_fixture_lines("claude_sandbox_cannot_start.stream.jsonl"),
            writes=review)
        self.assertTrue(text.startswith(review), text)                 # the leaf's review
        self.assertEqual(assemble.leaf_status(text), "")               # not a placeholder
        self.assertIn("Bash was unavailable to this reviewer", text)   # …and says so
        self.assertIs(benefit.get("completed"), True)
        self.assertEqual(benefit.get("not_completed"), {})
        self.assertEqual(benefit["findings"], 1)                       # the note is none

    def test_a_working_sandbox_that_delivers_nothing_keeps_todays_class(self) -> None:
        # #526 (e): the fix narrows the substantive class; it does not relabel every
        # empty result. The leaf's own words are never the evidence — so a run whose Bash
        # WORKED and whose closing text says "The sandbox is working normally" (pinned
        # bytes) is classified exactly as before, and so is a run whose Bash failures
        # carry no sandbox-startup error, or where one call failed that way but one ran.
        ran = _fixture_lines("claude_bash_ran.stream.jsonl")        # echo ok, false, text
        dead = _fixture_lines("claude_sandbox_cannot_start.stream.jsonl")
        cases = {
            "bash ran; 'the sandbox is working normally'": ran,
            "every failure is the command's own": ran[2:],
            "one call hit the sandbox helper, another ran": dead[:2] + ran[:2] + ran[4:],
        }
        for n, (case, stdout) in enumerate(cases.items()):
            with self.subTest(case):
                self.assertIn("The sandbox is working normally", stdout[-1])
                _d, text, benefit = self._run_stand_in(f"OK{n}", stdout=stdout)
                self.assertEqual(assemble.leaf_status(text), "human-empty")
                self.assertNotIn("could not start", text)
                self.assertEqual(benefit.get("not_completed"),
                                 {"plan-reviewer": "human-empty"})

    def test_the_seeded_policy_keeps_the_target_and_the_bundle_read_only(self) -> None:
        # #526 (d): the fail-closed block is unchanged, and the leaf's file tools — Write
        # is how it delivers when Bash is dead — are denied on the pinned target and on
        # the bundle. Without the rules, acceptEdits let Write overwrite a file in the
        # `--add-dir` target (observed live, see build notes); the target stays readable.
        reviewer = {"id": "claude-lens", "mode": "command", "family": "claude",
                    "argv": ["claude"]}
        cfg = _cfg(self.tmp, plan_advisory=[reviewer])
        d = _brief(cfg, "RO")
        target = self.tmp / "pinned-target"
        target.mkdir()
        seen: dict = {}

        @contextlib.contextmanager
        def pinned(_d, _cfg):
            yield target

        def fake(leaf, cwd, prompt, **kw):
            seen["extra"] = list(kw.get("extra_argv") or [])
            seen["settings"] = json.loads(
                (Path(cwd) / ".claude" / "settings.json").read_text(encoding="utf-8"))
            return None

        with mock.patch.object(leaves, "_pinned_plan_target", pinned), \
                mock.patch.object(leaves, "_invoke_leaf_resilient", side_effect=fake), \
                contextlib.redirect_stderr(io.StringIO()):
            leaves.run_plan_advisory(d, cfg)
        self.assertEqual(seen["settings"]["sandbox"],
                         {"enabled": True, "allowUnsandboxedCommands": False,
                          "failIfUnavailable": True})             # the boundary, unchanged
        permissions = seen["settings"].get("permissions", {})
        for read_only in (target, d):
            self.assertIn(f"Edit(/{read_only.resolve()}/**)", permissions.get("deny", []))
        self.assertEqual(set(permissions), {"deny"})              # a deny grants nothing
        self.assertIn(str(target), seen["extra"])                 # still granted to read

    def test_the_bash_fallback_is_in_the_prompt_the_model_reads(self) -> None:
        # #526: the fallback, its trigger and the disclosure are prompt TEXT — a
        # condition kept in a Python comment is invisible to the model — and a healthy
        # run is not told to claim that Bash was down.
        prompt = leaves._plan_advisory_prompt(_REVIEWER, "plan-reviewer")
        self.assertIn("If Bash does not work in this run", prompt)
        self.assertIn("create plan-advisory-plan-reviewer.md with the Write tool", prompt)
        self.assertIn("Do not add that line when Bash works.", prompt)

    def test_the_reviewer_agent_has_write_but_the_example_argv_does_not_preapprove_it(
            self) -> None:
        # #526 (c)+(d): Write reaches the leaf through its agent's `tools:` line, and
        # `acceptEdits` approves it only inside the leaf's working directories. Listing
        # it in --allowedTools would pre-approve it on EVERY path — observed: it then
        # overwrote a file outside every directory the leaf was granted.
        root = Path(__file__).resolve().parents[1]

        def shipped(name: str) -> str:
            # `.jinja` in the template repo; rendered in an instance, where this suite
            # also runs (the render test re-runs it there).
            for path in (root / f"{name}.jinja", root / name):
                if path.is_file():
                    return path.read_text(encoding="utf-8")
            self.skipTest(f"no {name}(.jinja) beside the tests")
            raise AssertionError("unreachable")  # skipTest raises

        agent = shipped(".claude/agents/plan-reviewer.md")
        tools = next(ln for ln in agent.splitlines() if ln.startswith("tools:"))
        self.assertEqual([t.strip() for t in tools.removeprefix("tools:").split(",")],
                         ["Read", "Bash", "Grep", "Glob", "Write"])
        example = next(ln for ln in shipped("pdca.toml").splitlines()
                       if '"--agent", "plan-reviewer"' in ln)
        self.assertTrue(example.endswith('"--allowedTools", "Read,Bash,Grep,Glob"]'),
                        example)


class VendorComplement(unittest.TestCase):
    """#301: the complement anchor is the PLANNER family (the brief's author)."""

    _POOL = [{"id": "claude-lens", "mode": "stub", "family": "claude"},
             {"id": "codex-lens", "mode": "stub", "family": "codex"}]

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *, pool, planner_family: str) -> Path:
        cfg = _cfg(self.tmp, plan_advisory=pool, selection={"mode": "vendor-complement"},
                   planner=LeafConfig(mode="stub", family=planner_family, interactive=True))
        d = _brief(cfg, "VC")
        leaves.run_plan_advisory(d, cfg)
        return d

    def test_complement_of_the_planner_family_runs(self) -> None:
        d = self._run(pool=self._POOL, planner_family="codex")
        self.assertTrue(leaves.plan_advisory_artifact(d, "claude-lens").exists())
        self.assertFalse(leaves.plan_advisory_artifact(d, "codex-lens").exists())
        self.assertFalse(leaves.plan_advisory_artifact(d, "decorrelation").exists())

    def test_same_family_pool_falls_back_with_decorrelation_note(self) -> None:
        d = self._run(pool=[self._POOL[0]], planner_family="claude")
        self.assertTrue(leaves.plan_advisory_artifact(d, "claude-lens").exists())
        note = leaves.plan_advisory_artifact(d, "decorrelation")
        self.assertTrue(note.exists())
        self.assertIn("NEEDS-HUMAN", note.read_text(encoding="utf-8"))

    def test_unknown_planner_family_falls_back_with_note(self) -> None:
        d = self._run(pool=self._POOL, planner_family="")
        self.assertTrue(leaves.plan_advisory_artifact(d, "claude-lens").exists())
        self.assertIn("not declared",
                      leaves.plan_advisory_artifact(d, "decorrelation")
                      .read_text(encoding="utf-8"))


class PinnedTarget(unittest.TestCase):
    """#301 review round 2: the plan review grounds on a checkout PINNED to the brief's
    resolved base — never the human's sibling checkout, which may sit on another branch
    or carry local edits the antagonist would mistake for the plan's target."""

    def setUp(self) -> None:
        import subprocess as sp
        self.sp = sp
        self.tmp = Path(tempfile.mkdtemp())
        self.primary = self.tmp / "checkout"
        origin = self.tmp / "origin.git"
        sp.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        sp.run(["git", "clone", "-q", str(origin), str(self.primary)], check=True)
        g = lambda *a: sp.run(["git", "-C", str(self.primary), *a], check=True,
                              capture_output=True)
        g("config", "user.email", "t@example.com")
        g("config", "user.name", "T")
        (self.primary / "file.txt").write_text("base\n", encoding="utf-8")
        g("add", "-A"); g("commit", "-q", "-m", "base")
        g("branch", "-M", "main"); g("push", "-q", "-u", "origin", "main")
        # The human's checkout drifts: another branch + a local edit.
        g("checkout", "-q", "-b", "wip")
        (self.primary / "file.txt").write_text("LOCAL WIP EDIT\n", encoding="utf-8")
        self.cfg = _cfg(self.tmp)
        self.cfg.base_remote = "origin"
        self.cfg.repo_checkouts = {"org/repo": str(self.primary)}

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_target_is_pinned_to_the_resolved_base_and_cleaned_up(self) -> None:
        d = self.cfg.bundle("PT")
        d.mkdir(parents=True)
        (d / "brief.md").write_text(
            "- **Slug:** s\n- **Repo + branch target:** org/repo @ main\n", encoding="utf-8")
        with leaves._pinned_plan_target(d, self.cfg) as target:
            self.assertIsNotNone(target)
            self.assertNotEqual(target, self.primary)     # never the drifted checkout
            self.assertEqual((target / "file.txt").read_text(encoding="utf-8"),
                             "base\n")                    # the resolved base, not WIP
            kept = target
        self.assertFalse(kept.exists())                   # removed after the review
        # The human's checkout is untouched.
        self.assertEqual((self.primary / "file.txt").read_text(encoding="utf-8"),
                         "LOCAL WIP EDIT\n")

    def test_target_less_brief_yields_no_grounding(self) -> None:
        d = self.cfg.bundle("NOTGT")
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** s\n", encoding="utf-8")  # no target
        with leaves._pinned_plan_target(d, self.cfg) as target:
            self.assertIsNone(target)                     # no target ⇒ no grounding

    def test_fallback_is_a_disposable_tree_never_a_lane_or_the_primary(self) -> None:
        # #301 review rounds 7/8: when the pinned add fails (missing base ref), the
        # fallback is a DISPOSABLE detached worktree at the primary's HEAD — never
        # _reviewer_target's lane worktree (its last user's patched content), and
        # never the primary itself (the grounding flag is read/WRITE for codex, so
        # exposing it would let a reviewer command mutate the operator's WIP).
        d = self.cfg.bundle("LANE")
        d.mkdir(parents=True)
        (d / "brief.md").write_text(
            "- **Slug:** s\n- **Repo + branch target:** org/repo @ no-such-branch\n",
            encoding="utf-8")
        lane = self.tmp / "stale-lane-worktree"
        lane.mkdir()
        with mock.patch.object(leaves.worktree, "path", return_value=lane):
            with leaves._pinned_plan_target(d, self.cfg) as target:
                self.assertNotEqual(target, lane)         # never the shared lane
                self.assertNotEqual(target, self.primary)  # never the writable primary
                self.assertEqual((target / "file.txt").read_text(encoding="utf-8"),
                                 "base\n")                # committed HEAD, not the WIP
                kept = target
        self.assertFalse(kept.exists())                   # disposable: removed after
        self.assertEqual((self.primary / "file.txt").read_text(encoding="utf-8"),
                         "LOCAL WIP EDIT\n")              # operator's tree untouched


class BatchAndAssemble(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_batch_reviews_only_freshly_briefed_bundles(self) -> None:
        cfg = _cfg(self.tmp, plan_advisory=[_REVIEWER])
        pre = _brief(cfg, "OLD")                          # briefed before this session
        leaves.do_plan_batch(cfg, ids=["N1", "N2"])       # stub batch briefs the new ids
        for iid in ("N1", "N2"):
            d = cfg.bundle(iid)
            self.assertTrue(leaves.plan_advisory_artifact(d, "plan-reviewer").exists(), iid)
            self.assertTrue((d / "plan-advisory-benefit.json").exists(), iid)
        self.assertEqual(list(pre.glob("plan-advisory-*")), [])  # pre-existing untouched

    def test_stale_artifacts_from_a_prior_review_are_cleared(self) -> None:
        # #301 review round 6: a rewritten brief (or a changed `when` selection) must
        # not inherit the previous review's artifacts — stale findings would re-enter
        # §6/_plan_findings and could gate a brief they never reviewed. Cleared even
        # when the new brief matches NO leaf.
        gated = {**_REVIEWER, "when": {"field": "difficulty", "substring": "high"}}
        cfg = _cfg(self.tmp, plan_advisory=[gated])
        d = _brief(cfg, "STALE", difficulty="high")
        leaves.run_plan_advisory(d, cfg)
        self.assertTrue(leaves.plan_advisory_artifact(d, "plan-reviewer").exists())
        (d / "brief.md").write_text(                       # rewritten: now low difficulty
            "- **Slug:** stale-v2\n- **Defect:** y.\n- **Difficulty:** low\n",
            encoding="utf-8")
        leaves.run_plan_advisory(d, cfg)
        self.assertEqual(list(d.glob("plan-advisory-*")), [])  # nothing stale survives

    def test_rewritten_brief_gets_a_fresh_plan_review(self) -> None:
        # #301 review round 5: the snapshot is by CONTENT HASH — a rerun session that
        # rewrites an existing authored brief must get a fresh review, while an
        # unchanged resumption still skips.
        cfg = _cfg(self.tmp, plan_advisory=[_REVIEWER])
        untouched = _brief(cfg, "U1")
        rewritten = _brief(cfg, "RW1")
        real_stub = leaves._stub_plan_batch

        def stub_rewrites_rw1(cfg_, ids_=None):
            real_stub(cfg_, ids_)
            (rewritten / "brief.md").write_text(
                "- **Slug:** rw1-v2\n- **Defect:** reframed.\n", encoding="utf-8")

        with mock.patch.object(leaves, "_stub_plan_batch", side_effect=stub_rewrites_rw1):
            leaves.do_plan_batch(cfg, ids=["N9"])          # session also briefs N9
        self.assertTrue(leaves.plan_advisory_artifact(rewritten, "plan-reviewer").exists())
        self.assertTrue(
            leaves.plan_advisory_artifact(cfg.bundle("N9"), "plan-reviewer").exists())
        self.assertEqual(list(untouched.glob("plan-advisory-*")), [])  # unchanged: skipped

    def test_dependency_manifest_resolves_declared_prereqs(self) -> None:
        # #301 review round 5: the sandbox holds only plan inputs, so the reviewer gets
        # a read-only manifest of each declared prerequisite's existence and state.
        cfg = _cfg(self.tmp)
        dep = _brief(cfg, "77")                            # an existing PLANNED prereq
        d = _brief(cfg, "M1")
        (d / "brief.md").write_text(
            "- **Slug:** m1\n- **Defect:** x.\n- **Depends on:** 77, 940\n",
            encoding="utf-8")
        manifest = leaves._dependency_manifest(d, cfg)
        self.assertEqual(manifest["77"]["exists"], True)
        self.assertEqual(manifest["77"]["state"], "PLANNED")
        self.assertEqual(manifest["940"], {"declared": "Depends on", "exists": False,
                                           "state": None})
        self.assertIn("dependency-state.json", leaves._plan_advisory_prompt(_REVIEWER,
                                                                            "plan-reviewer"))
        del dep  # (fixture bookkeeping)

    def test_placeholder_brief_counts_as_unbriefed_in_the_snapshot(self) -> None:
        # #301 review round 2: an unfilled template copy is not "briefed" (#113) — the
        # session replaces it with a real brief, and that fresh brief must get its plan
        # review instead of being snapshot-excluded.
        cfg = _cfg(self.tmp, plan_advisory=[_REVIEWER])
        d = cfg.bundle("PH1")
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** <fill-me>\n", encoding="utf-8")
        leaves.do_plan_batch(cfg, ids=["PH1"])            # stub batch authors the brief
        self.assertTrue(leaves.plan_advisory_artifact(d, "plan-reviewer").exists())
        self.assertTrue((d / "plan-advisory-benefit.json").exists())

    def _summary_bundle(self, cfg: Config, benefit: dict | str,
                        findings: list[str] = ()) -> Path:
        d = _brief(cfg, "S1")
        (d / "patch.diff").write_text("diff --git a/x b/x\n", encoding="utf-8")
        (d / "check-gates.json").write_text(json.dumps({"overall": "pass", "rows": []}),
                                            encoding="utf-8")
        (d / "check-review.md").write_text("looks fine\n", encoding="utf-8")
        text = benefit if isinstance(benefit, str) else json.dumps(benefit)
        (d / "plan-advisory-benefit.json").write_text(text, encoding="utf-8")
        if findings:
            (d / "plan-advisory-plan-reviewer.md").write_text(
                "# Plan advisory — plan-reviewer\n\n"
                + "".join(f"- NEEDS-HUMAN — {f}\n" for f in findings), encoding="utf-8")
        return d

    def test_findings_fold_into_section6_individually(self) -> None:
        # #301 review: every plan-advisory NEEDS-HUMAN finding folds into §6 like the
        # Check advisories', and the benefit line rides §10 — telemetry, never a gate.
        cfg = _cfg(self.tmp)
        d = self._summary_bundle(cfg, {"findings": 2, "revised": False},
                                 findings=["success criterion is unverifiable",
                                           "hidden scope: touches the exporter too"])
        assemble.assemble_summary(d, cfg)
        summary = (d / "SUMMARY.md").read_text(encoding="utf-8")
        self.assertIn("- [ ] success criterion is unverifiable", summary)
        self.assertIn("- [ ] hidden scope: touches the exporter too", summary)
        self.assertIn("- Plan advisory: 2 finding(s); brief revised: no", summary)

    def test_findings_stay_visible_even_after_a_revision(self) -> None:
        # #301 review: a bundle-wide "brief revised" bit cannot say WHICH findings the
        # revision addressed — it must never suppress them. Each stays a §6 item the
        # human dispositions at sign-off; §10 still records the benefit telemetry.
        cfg = _cfg(self.tmp)
        d = self._summary_bundle(cfg, {"findings": 1, "revised": True},
                                 findings=["root cause framing contradicts the thread"])
        assemble.assemble_summary(d, cfg)
        summary = (d / "SUMMARY.md").read_text(encoding="utf-8")
        self.assertIn("- [ ] root cause framing contradicts the thread", summary)
        self.assertIn("- Plan advisory: 1 finding(s); brief revised: yes", summary)

    def test_decorrelation_note_surfaces_in_section6(self) -> None:
        # #301 review: the same-vendor fallback note must reach §6 — no other summary
        # path reads plan-advisory-*.md, so without the fold the independence lapse
        # could be accepted without human confirmation.
        cfg = _cfg(self.tmp, plan_advisory=[{"id": "claude-lens", "mode": "stub",
                                             "family": "claude"}],
                   selection={"mode": "vendor-complement"},
                   planner=LeafConfig(mode="stub", family="claude", interactive=True))
        d = self._summary_bundle(cfg, {"findings": 1, "revised": False})
        leaves.run_plan_advisory(d, cfg)                  # same-family pool → note
        assemble.assemble_summary(d, cfg)
        summary = (d / "SUMMARY.md").read_text(encoding="utf-8")
        self.assertIn("could not be decorrelated from the planner", summary)

    def test_malformed_benefit_record_never_crashes_assemble(self) -> None:
        cfg = _cfg(self.tmp)
        d = self._summary_bundle(cfg, "{not json")
        assemble.assemble_summary(d, cfg)                 # no raise
        self.assertNotIn("Plan advisory:",
                         (d / "SUMMARY.md").read_text(encoding="utf-8"))

    def test_doctor_enumerates_plan_advisory_command_leaves(self) -> None:
        # #301 review: a command-mode plan advisory is a CLI a real run spawns —
        # doctor's command-leaf enumeration (and thus --strict + the sandbox-dep
        # gate) must include it.
        from pdca_harness import doctor
        cfg = _cfg(self.tmp, plan_advisory=[{"id": "pr", "mode": "command",
                                             "family": "codex",
                                             "argv": ["no-such-plan-cli-xyz"]}])
        leaves_map = doctor._command_leaves(cfg)
        self.assertIn("plan-advisory:pr", leaves_map)
        self.assertEqual(leaves_map["plan-advisory:pr"].argv, ["no-such-plan-cli-xyz"])


if __name__ == "__main__":
    unittest.main()
