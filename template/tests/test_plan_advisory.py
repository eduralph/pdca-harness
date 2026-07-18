"""Plan-beat advisory reviewers (issue #301; stdlib unittest, offline).

Mirrors the Check advisory slice (test_adversary / vendor-complement) at Plan: opt-in
[[leaves.plan_advisory]] leaves review the BRIEF right after Plan, write
plan-advisory-<id>.md, the planner gets one bounded revision pass, and
plan-advisory-benefit.json records whether the review changed anything — surfaced in
SUMMARY §10 (always) and §6 (only when findings were left unrevised).
"""

from __future__ import annotations

import json
import shutil
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
