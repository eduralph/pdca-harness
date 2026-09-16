"""Plan-advisory review after a Plan session that split (issue #480; stdlib unittest,
offline). The #301 advisory pass, invoked from ``do_plan``/``do_plan_batch`` right after
the planner session, must cover exactly the briefs THAT SESSION authored or rewrote —
including CHILD bundles a ``pdca split <id> --accept`` run INSIDE a single-bundle session
creates, which the bundle the driver handed the session (``d``) never names — and must
never review a bundle already terminal (``close-disposition``), on either Plan path.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pdca_harness import leaves, state
from pdca_harness.config import Config, LeafConfig

_REVIEWER = {
    "id": "plan-reviewer",
    "mode": "stub",
    "role": "refute the brief: wrong root cause, untestable criterion, hidden scope",
}


def _cfg(root: Path, *, plan_advisory=None) -> Config:
    return Config(
        root=root, bundle_root=root / "results", process_dir=root / "process",
        templates_dir=root / "templates", default_branch="main", tracker_system="github",
        tracker_url="", issue_id_example="#1",
        builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
        planner=LeafConfig(mode="stub", interactive=True),
        plan_advisory_leaves=list(plan_advisory or []))


def _brief(cfg: Config, iid: str, *, body: str | None = None,
           placeholder: bool = False) -> Path:
    d = cfg.bundle(iid)
    d.mkdir(parents=True, exist_ok=True)
    if body is None:
        body = "" if placeholder else f"- **Slug:** {iid.lower()}\n- **Defect:** x.\n"
    (d / "brief.md").write_text(body or "# template\n", encoding="utf-8")
    return d


def _split_side_effect(cfg: Config, parent: Path, child_ids: list[str]):
    """Stands in for `pdca split <id> --accept` run INSIDE the stub-planner session
    (the brief's `Repro instruction`): authors briefs for children `do_plan` was
    never handed, and marks the parent terminal — the parent's OWN brief is left
    exactly as the caller set it up (untouched in shape 1, absent in shape 2)."""
    def fake(_d: Path, cfg_: Config) -> None:
        for cid in child_ids:
            _brief(cfg_, cid, body=f"- **Slug:** {cid.lower()}\n- **Defect:** child.\n")
        (parent / state.CLOSE_MARKER).write_text("split\n", encoding="utf-8")
    return fake


class SingleBundleSplitChildren(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_shape1_parent_keeps_brief_children_reviewed_parent_is_not(self) -> None:
        # brief.md `Repro instruction`, shape 1: split after a rejected attempt keeps
        # the parent's authored brief (`split.accept`, `include_brief=False`).
        cfg = _cfg(self.tmp, plan_advisory=[_REVIEWER])
        parent = _brief(cfg, "P1")
        with mock.patch.object(leaves, "_stub_plan",
                               side_effect=_split_side_effect(cfg, parent, ["C1", "C2"])):
            leaves.do_plan(parent, cfg)
        for cid in ("C1", "C2"):
            d = cfg.bundle(cid)
            self.assertTrue(leaves.plan_advisory_artifact(d, "plan-reviewer").exists(), cid)
            self.assertTrue((d / "plan-advisory-benefit.json").exists(), cid)
        self.assertEqual(list(parent.glob("plan-advisory-*")), [])
        self.assertFalse((parent / "plan-advisory-benefit.json").exists())

    def test_shape2_parent_has_no_brief_children_still_reviewed(self) -> None:
        # brief.md `Repro instruction`, shape 2: split after `iterate-plan` cleared the
        # parent's brief — pre-fix, the `leaves.py:3364-3365` filter drops the parent
        # and the pass silently does nothing for anyone.
        cfg = _cfg(self.tmp, plan_advisory=[_REVIEWER])
        parent = _brief(cfg, "P2")
        (parent / "brief.md").unlink()
        with mock.patch.object(leaves, "_stub_plan",
                               side_effect=_split_side_effect(cfg, parent, ["C3", "C4"])):
            leaves.do_plan(parent, cfg)
        for cid in ("C3", "C4"):
            d = cfg.bundle(cid)
            self.assertTrue(leaves.plan_advisory_artifact(d, "plan-reviewer").exists(), cid)
            self.assertTrue((d / "plan-advisory-benefit.json").exists(), cid)
        self.assertEqual(list(parent.glob("plan-advisory-*")), [])

    def test_terminal_bundle_is_never_reviewed_even_if_its_brief_changed(self) -> None:
        # Success criterion (b): close-disposition must gate REGARDLESS of freshness —
        # a rewritten brief on an already-terminal bundle must not trigger the
        # revision session over a bundle nothing will build. Exercised directly
        # against `run_plan_advisory_batch`, the one choke point both Plan paths
        # funnel through, so the guard is proven on both without duplicating the
        # planner-session plumbing.
        cfg = _cfg(self.tmp, plan_advisory=[_REVIEWER])
        d = _brief(cfg, "TERM")
        (d / state.CLOSE_MARKER).write_text("split\n", encoding="utf-8")
        leaves.run_plan_advisory_batch(cfg, [d])
        self.assertEqual(list(d.glob("plan-advisory-*")), [])
        self.assertFalse((d / "plan-advisory-benefit.json").exists())

    def test_single_bundle_path_selects_like_the_batch_path(self) -> None:
        # Success criterion (c), mirroring `test_rewritten_brief_gets_a_fresh_plan_review`
        # (test_plan_advisory.py:357-376) for the SINGLE-bundle path: a brief elsewhere
        # in the root that the session never touched is skipped, the session's own
        # rewrite of `d` is reviewed, and a placeholder brief is still never reviewed.
        cfg = _cfg(self.tmp, plan_advisory=[_REVIEWER])
        untouched = _brief(cfg, "U1")
        d = _brief(cfg, "RW1")

        def rewrite(dd: Path, _cfg: Config) -> None:
            (dd / "brief.md").write_text(
                "- **Slug:** rw1-v2\n- **Defect:** reframed.\n", encoding="utf-8")

        with mock.patch.object(leaves, "_stub_plan", side_effect=rewrite):
            leaves.do_plan(d, cfg)
        self.assertTrue(leaves.plan_advisory_artifact(d, "plan-reviewer").exists())
        self.assertEqual(list(untouched.glob("plan-advisory-*")), [])

        ph = _brief(cfg, "PH1", placeholder=True)
        with mock.patch.object(leaves, "_stub_plan"):        # session leaves the template as-is
            leaves.do_plan(ph, cfg)
        self.assertEqual(list(ph.glob("plan-advisory-*")), [])


if __name__ == "__main__":
    unittest.main()
