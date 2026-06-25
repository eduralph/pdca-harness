"""Builder escalation ladder + loop telemetry (issue #135, stdlib unittest).

Cost is loop-level, not per-token: an iterate re-runs the builder AND the frontier
reviewer, so a free local builder that needs 3 passes can cost more than one frontier
pass. So the harness (a) records iterations-to-pass as telemetry — the go/no-go metric
for adopting a local executor — and (b) escalates the builder backend on iterate
(min_iteration ladder) so a hard bundle can't loop forever on an underpowered model.
No Claude, no network — the builder argv is a python no-op.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from pdca_harness import leaves
from pdca_harness.config import Config, LeafConfig

NOOP = [sys.executable, "-c", "pass"]


def _cfg(root: Path, **kw) -> Config:
    return Config(
        root=root,
        bundle_root=root / "results",
        process_dir=root / "process",
        templates_dir=root / "templates",
        default_branch="main",
        tracker_system="github",
        tracker_url="",
        issue_id_example="1",
        builder=kw.pop("builder", LeafConfig(mode="command", family="local", argv=["local-build"])),
        reviewer=LeafConfig(mode="stub"),
        **kw,
    )


class SelectBuilder(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_escalates_by_attempt_number(self) -> None:
        cfg = _cfg(self.tmp, builder_escalation=[
            {"min_iteration": 2, "family": "mid", "argv": ["mid-build"]},
            {"min_iteration": 3, "family": "frontier", "argv": ["frontier-build"]},
        ])
        d = self.tmp / "issue_1"
        self.assertEqual(leaves.select_builder(d, cfg, 1).family, "local")     # default
        self.assertEqual(leaves.select_builder(d, cfg, 2).family, "mid")       # ≥2
        self.assertEqual(leaves.select_builder(d, cfg, 3).family, "frontier")  # ≥3
        self.assertEqual(leaves.select_builder(d, cfg, 9).family, "frontier")  # highest wins

    def test_no_ladder_always_uses_default(self) -> None:
        cfg = _cfg(self.tmp)
        d = self.tmp / "issue_1"
        self.assertEqual(leaves.select_builder(d, cfg, 5).family, "local")

    def test_spec_inherits_unset_fields_from_default(self) -> None:
        cfg = _cfg(self.tmp, builder_escalation=[{"min_iteration": 2, "argv": ["mid"]}])
        b = leaves.select_builder(self.tmp / "issue_1", cfg, 2)
        self.assertEqual(b.argv, ["mid"])
        self.assertEqual(b.family, "local")   # inherited
        self.assertEqual(b.mode, "command")   # inherited


class LoopTelemetry(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundle(self, cfg: Config) -> Path:
        d = cfg.bundle("1")
        d.mkdir(parents=True)
        # No "Repo + branch target" → worktree.ensure returns None → edit-in-place, so the
        # no-op builder runs without needing a git worktree fixture.
        (d / "brief.md").write_text("- **Slug:** s\n", encoding="utf-8")
        return d

    def test_telemetry_accumulates_across_iterations_and_records_backend(self) -> None:
        cfg = _cfg(self.tmp, builder=LeafConfig(mode="command", family="local", argv=NOOP),
                   builder_escalation=[{"min_iteration": 2, "family": "frontier", "argv": NOOP}])
        d = self._bundle(cfg)

        leaves.do_build(d, cfg)  # attempt 1
        tel = json.loads((d / "loop-telemetry.json").read_text())
        self.assertEqual(tel["iterations_to_pass"], 1)
        self.assertEqual(tel["attempts"][0]["family"], "local")

        (d / "iteration-v1").mkdir()  # simulate an iterate-to-Do archive
        leaves.do_build(d, cfg)  # attempt 2 → escalated
        tel = json.loads((d / "loop-telemetry.json").read_text())
        self.assertEqual(tel["iterations_to_pass"], 2)
        self.assertEqual(tel["attempts"][1]["n"], 2)
        self.assertEqual(tel["attempts"][1]["family"], "frontier")  # escalated on iterate

    def test_malformed_telemetry_file_does_not_break_do(self) -> None:
        # A best-effort sidecar: a hand edit / older writer that left valid-but-wrong-shape
        # JSON (a top-level array) must not abort Do — it is replaced, not appended to
        # (Codex review, PR #144).
        cfg = _cfg(self.tmp, builder=LeafConfig(mode="command", family="local", argv=NOOP))
        d = self._bundle(cfg)
        (d / "loop-telemetry.json").write_text("[1, 2, 3]", encoding="utf-8")  # wrong shape
        leaves.do_build(d, cfg)  # must not raise
        tel = json.loads((d / "loop-telemetry.json").read_text())
        self.assertEqual(tel["iterations_to_pass"], 1)
        self.assertEqual(tel["attempts"][0]["family"], "local")


if __name__ == "__main__":
    unittest.main()
