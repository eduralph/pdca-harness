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


class TelemetryRecordsTheTier(unittest.TestCase):
    """The recorded attempt names the tier that ACTUALLY ran it (issue #356).

    `builder`/`family` alone cannot tell tier 1 from tier 2 on a ladder that climbs
    within ONE vendor (sonnet/high → opus/xhigh) — every entry writes the identical
    claude/claude pair, so `loop-telemetry.json` cannot answer the one question it
    exists for. The effective model/effort are resolved with `_mapped_argv`'s own
    precedence (leaves.py:150-165): explicit argv wins over the leaf's keys.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _attempt(self, builder: LeafConfig, **kw) -> dict:
        """The dict `_record_loop_attempt` (the production writer) appends for `builder`."""
        cfg = _cfg(self.tmp, builder=builder, **kw)
        self._seq = getattr(self, "_seq", 0) + 1   # one fresh bundle per call
        d = cfg.bundle(str(self._seq))
        d.mkdir(parents=True)
        leaves._record_loop_attempt(d, 1, builder, cfg)
        return json.loads((d / "loop-telemetry.json").read_text())["attempts"][0]

    def test_argv_wins_over_the_leaf_keys(self) -> None:
        # (a) argv already carries the family's model flag / effort mapping ⇒ the recorded
        # values are the ARGV ones. `_mapped_argv` would add nothing here, so opus/xhigh
        # is what was *asked for* and sonnet/low is what RUNS.
        a = self._attempt(LeafConfig(
            mode="command", family="claude", model="opus", effort="xhigh",
            argv=["claude", "-p", "--model", "sonnet", "--effort", "low"]))
        self.assertEqual((a["model"], a["effort"]), ("sonnet", "low"))
        # …the `=`-joined spelling of the same flag…
        b = self._attempt(LeafConfig(
            mode="command", family="claude", model="opus", effort="xhigh",
            argv=["claude", "-p", "--model=haiku", "--effort=minimal"]))
        self.assertEqual((b["model"], b["effort"]), ("haiku", "minimal"))
        # …and codex's `-c key=value` effort mapping, whose flag token is the KEY.
        c = self._attempt(LeafConfig(
            mode="command", family="codex", model="gpt-5", effort="high",
            argv=["codex", "exec", "-m", "gpt-5-codex",
                  "-c", "model_reasoning_effort=minimal"]))
        self.assertEqual((c["model"], c["effort"]), ("gpt-5-codex", "minimal"))

    def test_falls_back_to_the_leaf_keys_when_argv_is_silent(self) -> None:
        # (b) argv says nothing ⇒ the keys are what `_mapped_argv` will map onto the CLI.
        a = self._attempt(LeafConfig(mode="command", family="claude", model="opus",
                                     effort="xhigh", argv=["claude", "-p"]))
        self.assertEqual((a["model"], a["effort"]), ("opus", "xhigh"))

    def test_unset_everywhere_records_empty_never_a_guessed_default(self) -> None:
        # (c) neither set ⇒ "" — the CLI picks its own default and the harness must not
        # invent one, which would be a false calibration record.
        a = self._attempt(LeafConfig(mode="command", family="claude", argv=["claude", "-p"]))
        self.assertEqual((a["model"], a["effort"]), ("", ""))
        # A family with no model/effort mapping at all (generic) behaves the same way.
        b = self._attempt(LeafConfig(mode="command", family="local", model="q4",
                                     argv=["local-build"]))
        self.assertEqual((b["model"], b["effort"]), ("q4", ""))

    def test_the_argv_probe_matches_the_flag_token_exactly(self) -> None:
        # (d) codex's model flag is `-m`, a substring of an unrelated `--model-info`
        # argument: a loose `in`-style probe would read "x" (or "") as the model. The
        # exact match falls back to the leaf's key, which is what actually runs.
        a = self._attempt(LeafConfig(
            mode="command", family="codex", model="gpt-5", effort="high",
            argv=["codex", "exec", "--model-info", "x",
                  "-c", "model_reasoning_effort_probe=zzz"]))
        self.assertEqual((a["model"], a["effort"]), ("gpt-5", "high"))

    def test_same_vendor_ladder_is_distinguishable_end_to_end(self) -> None:
        # The reported defect, through the real `do_build` → `select_builder` → sidecar
        # path: two escalation tiers of the SAME family, differing only in the tier they
        # run. `_invoke` is stubbed (as in test_do_confine.py:90) so no CLI is spawned;
        # everything up to and including the telemetry write is production code.
        cfg = _cfg(self.tmp,
                   builder=LeafConfig(mode="command", family="claude", model="sonnet",
                                      effort="high", argv=["claude", "-p"]),
                   builder_escalation=[{"min_iteration": 2,
                                        "argv": ["claude", "-p", "--model", "opus",
                                                 "--effort", "max"]}])
        d = cfg.bundle("1")
        d.mkdir(parents=True)
        # No "Repo + branch target" → worktree.ensure returns None → edit in place.
        (d / "brief.md").write_text("- **Slug:** s\n", encoding="utf-8")
        orig = leaves._invoke
        leaves._invoke = lambda *a, **kw: None
        try:
            leaves.do_build(d, cfg)          # attempt 1 — the leaf's own keys
            (d / "iteration-v1").mkdir()     # simulate an iterate-to-Do archive
            leaves.do_build(d, cfg)          # attempt 2 — escalated, argv-pinned
        finally:
            leaves._invoke = orig
        first, second = json.loads((d / "loop-telemetry.json").read_text())["attempts"]
        self.assertEqual(first["family"], second["family"])      # same vendor…
        self.assertEqual(first["builder"], second["builder"])    # …same argv[0]
        self.assertEqual((first["model"], first["effort"]), ("sonnet", "high"))
        self.assertEqual((second["model"], second["effort"]), ("opus", "max"))
        self.assertNotEqual(first, second)  # the tiers are finally tellable apart

    def test_a_malformed_family_mapping_does_not_break_do(self) -> None:
        # Best-effort sidecar: a [families.*] override whose effort_argv carries an
        # unknown placeholder raises inside str.format — it must not abort Do.
        a = self._attempt(
            LeafConfig(mode="command", family="local", model="m", effort="high",
                       argv=["local-build"]),
            families={"local": {"effort_argv": ["-c", "effort={bogus}"]}})
        self.assertEqual((a["model"], a["effort"]), ("", ""))


if __name__ == "__main__":
    unittest.main()
