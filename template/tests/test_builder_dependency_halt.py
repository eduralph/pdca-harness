"""A builder-declared unmet external dependency halts the Do beat — when CONFIRMED (#341).

Before this, the honest declaration (`NEEDS-HUMAN external dependency:` in
build-notes.md, the builder-contract marker) changed nothing: BUILT unconditionally
bought the full Check beat — gates, cross-vendor reviewer, adversary — to adjudicate a
patch already *stated* to be unverifiable (`driver.advance` ran `gates.run_gates` →
`leaves.run_review` → `run_advisory_leaves` regardless of anything the builder wrote).
The honest and dishonest paths proceeded identically, and the dishonest bundle looked
better at Check.

The halt is earned, never self-reported (the inverse failure #332 documents): the named
dependency must resolve to a `[[doctor.checks]]` row — registered in pdca.toml, else
parsed from the fenced TOML block the builder contract already requires it to propose —
AND that row's detect cmd must exit non-zero (#340's probe, one beat later). Confirmed ⇒
the close fast path to AWAITING_SIGNOFF (N/A gates, no reviewer, resumable via
iterate-do — never DISCONTINUED: sign-off alone owns terminal states). Refuted or
unresolvable ⇒ full Check, byte-identical, with the refutation recorded into §6 where
`pdca act index` reads. Opt-in via `[driver].dependency_halt`; off is byte-identical.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pdca_harness import dependency_halt, driver, signoff, state
from pdca_harness.config import Config, LeafConfig

_BRIEF = ("- **Slug:** dep-halt\n"
          "- **Defect:** the builder hit an unmet external dependency\n"
          "- **Success criterion:** the claim is adjudicated deterministically\n"
          "- **Test file:** test_stub.py\n")

_MARKER_NOTES = ("Chose approach X; could not verify the fix.\n\n"
                 "NEEDS-HUMAN external dependency: protoc — the generated stubs cannot "
                 "be compiled, so the fix is unverifiable on this host.\n")

_PROPOSED_BLOCK = ('\n```toml\n'
                   '[[doctor.checks]]\n'
                   'id    = "protoc"\n'
                   'cmd   = "false"\n'
                   'hint  = "apt install protobuf-compiler"\n'
                   'level = "MISSING"\n'
                   '```\n')

# The stub reviewer's artifact opens with this — its presence proves leaf 2 ran.
_STUB_REVIEW_HEAD = "Cross-vendor reviewer"


def _cfg(root: Path, *, halt: bool = True, rows: list[dict] | None = None,
         gate_cmd: str = "true") -> Config:
    cfg = Config(
        root=root, bundle_root=root / "results", process_dir=root / "process",
        templates_dir=root / "templates", default_branch="main",
        tracker_system="github", tracker_url="", issue_id_example="#1",
        builder=LeafConfig(mode="stub", family="claude"),
        reviewer=LeafConfig(mode="stub", family="codex"),
    )
    cfg.dependency_halt = halt
    cfg.doctor_checks = list(rows or [])
    # An OBSERVABLE gate: its side effect proves whether any gate subprocess ran.
    cfg.gates_checks = [{"id": "C4", "tier": "C4", "label": "verify", "scope": "bundle",
                         "gating": True, "cmd": gate_cmd}]
    return cfg


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "results").mkdir(parents=True)
        self.gate_marker = self.tmp / "gate-ran"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundle(self, notes: str = _MARKER_NOTES) -> Path:
        """A bundle at BUILT: the builder shipped a patch AND declared the dependency."""
        d = self.tmp / "results" / "issue_1"
        d.mkdir(parents=True, exist_ok=True)
        (d / "brief.md").write_text(_BRIEF, encoding="utf-8")
        (d / "patch.diff").write_text("--- a\n+++ b\n", encoding="utf-8")
        (d / "build-notes.md").write_text(notes, encoding="utf-8")
        self.assertEqual(state.state(d), state.BUILT)
        return d

    def _gate_cmd(self) -> str:
        return f"touch {self.gate_marker}"


class ConfirmedClaimHalts(_Base):
    """Criterion (a): marker + confirmed claim ⇒ the close fast path to sign-off."""

    def test_confirmed_claim_skips_reviewer_and_halts_at_signoff(self) -> None:
        """RED on current main: BUILT runs gates + reviewer unconditionally, so the
        reviewer artifact is the stub review and the observable gate cmd runs."""
        d = self._bundle()
        cfg = _cfg(self.tmp, rows=[{"id": "protoc", "cmd": "false",
                                    "hint": "apt install protobuf-compiler"}],
                   gate_cmd=self._gate_cmd())
        final = driver.run_issue(d, cfg)

        # Halts for the human — never a terminal state set by a leaf (criterion e).
        self.assertEqual(final, state.AWAITING_SIGNOFF)
        self.assertNotEqual(state.state(d), state.DISCONTINUED)

        review = (d / "check-review.md").read_text(encoding="utf-8")
        self.assertNotIn(_STUB_REVIEW_HEAD, review, "the reviewer leaf was invoked")
        self.assertIn("SKIPPED", review)
        self.assertIn("protoc", review)
        self.assertFalse(self.gate_marker.exists(),
                         "a gate subprocess ran — the matrix must be the N/A close one")

        # §6 carries the _declared_external_deps item (#250) so C6 blocks accept.
        open_items = signoff.open_needs_human(d / "SUMMARY.md")
        self.assertTrue(any("protoc" in it for it in open_items), open_items)

        rec = json.loads((d / state.DEPENDENCY_ADJUDICATION).read_text(encoding="utf-8"))
        self.assertTrue(rec["halted"])
        self.assertEqual(rec["verdicts"][0]["verdict"], dependency_halt.CONFIRMED)
        self.assertEqual(rec["verdicts"][0]["source"], "registered")

    def test_confirmed_via_the_builders_proposed_row(self) -> None:
        """No registered row: the fenced [[doctor.checks]] block the builder contract
        mandates is the resolution — the builder supplies the detect command, the
        harness runs it, the exit code decides (the proposal-driven reading)."""
        d = self._bundle(_MARKER_NOTES + _PROPOSED_BLOCK)
        cfg = _cfg(self.tmp, rows=[], gate_cmd=self._gate_cmd())
        self.assertEqual(driver.run_issue(d, cfg), state.AWAITING_SIGNOFF)
        review = (d / "check-review.md").read_text(encoding="utf-8")
        self.assertNotIn(_STUB_REVIEW_HEAD, review)
        self.assertFalse(self.gate_marker.exists())
        rec = json.loads((d / state.DEPENDENCY_ADJUDICATION).read_text(encoding="utf-8"))
        self.assertEqual(rec["verdicts"][0]["source"], "proposed")

    def test_blocked_bundle_is_resumable_via_iterate_do(self) -> None:
        """The blocked halt is blocked-resume-when-provided, not deliberately-abandoned:
        iterate-do archives the attempt (adjudication record included) and reruns the
        FULL Do+Check band — the fresh build-notes carry no marker, so the reviewer runs."""
        from pdca_harness import cli
        d = self._bundle()
        cfg = _cfg(self.tmp, rows=[{"id": "protoc", "cmd": "false", "hint": "h"}])
        self.assertEqual(driver.run_issue(d, cfg), state.AWAITING_SIGNOFF)

        args = SimpleNamespace(issue_id="1", accept=False, iterate_do=True,
                               iterate_plan=False, discontinue=False, by="t", delta="",
                               no_publish=True)
        cli._signoff(cfg, args)

        arch = d / "iteration-v1"
        self.assertTrue((arch / state.DEPENDENCY_ADJUDICATION).exists(),
                        "the adjudication record must be archived with its attempt")
        self.assertTrue((arch / "check-review.md").exists())
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)
        self.assertIn(_STUB_REVIEW_HEAD,
                      (d / "check-review.md").read_text(encoding="utf-8"),
                      "the resumed attempt must run the real review band")


class RefutedClaimCannotSkipReview(_Base):
    """Criterion (d): a claim whose detect cmd exits 0 buys the builder nothing."""

    def test_refuted_claim_runs_full_check(self) -> None:
        d = self._bundle()
        cfg = _cfg(self.tmp, rows=[{"id": "protoc", "cmd": "true", "hint": "h"}],
                   gate_cmd=self._gate_cmd())
        self.assertEqual(driver.run_issue(d, cfg), state.AWAITING_SIGNOFF)
        review = (d / "check-review.md").read_text(encoding="utf-8")
        self.assertIn(_STUB_REVIEW_HEAD, review, "the reviewer leaf must run")
        self.assertTrue(self.gate_marker.exists(), "the real gates must run")
        rec = json.loads((d / state.DEPENDENCY_ADJUDICATION).read_text(encoding="utf-8"))
        self.assertFalse(rec["halted"])
        self.assertEqual(rec["verdicts"][0]["verdict"], dependency_halt.REFUTED)

    def test_refutation_is_recorded_in_section6_for_act(self) -> None:
        """Criterion (b): the refutation reaches SUMMARY §6 — which is what `pdca act
        index` extracts — not just a bundle-local json only the driver saw."""
        d = self._bundle()
        cfg = _cfg(self.tmp, rows=[{"id": "protoc", "cmd": "true", "hint": "h"}])
        driver.run_issue(d, cfg)
        open_items = signoff.open_needs_human(d / "SUMMARY.md")
        self.assertTrue(any("REFUTED" in it and "protoc" in it for it in open_items),
                        open_items)

    def test_a_registered_row_beats_the_builders_proposed_row(self) -> None:
        """A builder cannot out-vote the instance's own registration with a bogus
        always-failing proposed row: the human-blessed registered row is probed first."""
        d = self._bundle(_MARKER_NOTES + _PROPOSED_BLOCK)  # proposed cmd = "false"
        cfg = _cfg(self.tmp, rows=[{"id": "protoc", "cmd": "true", "hint": "h"}],
                   gate_cmd=self._gate_cmd())
        self.assertEqual(driver.run_issue(d, cfg), state.AWAITING_SIGNOFF)
        self.assertIn(_STUB_REVIEW_HEAD,
                      (d / "check-review.md").read_text(encoding="utf-8"))
        rec = json.loads((d / state.DEPENDENCY_ADJUDICATION).read_text(encoding="utf-8"))
        self.assertEqual(rec["verdicts"][0]["source"], "registered")
        self.assertEqual(rec["verdicts"][0]["verdict"], dependency_halt.REFUTED)


class UnconfirmedFailsTowardReview(_Base):
    """Criterion (c): unresolvable ⇒ full Check — never toward skipping it."""

    def test_no_row_and_no_proposal_runs_full_check(self) -> None:
        d = self._bundle()
        cfg = _cfg(self.tmp, rows=[], gate_cmd=self._gate_cmd())
        self.assertEqual(driver.run_issue(d, cfg), state.AWAITING_SIGNOFF)
        self.assertIn(_STUB_REVIEW_HEAD,
                      (d / "check-review.md").read_text(encoding="utf-8"))
        self.assertTrue(self.gate_marker.exists())
        rec = json.loads((d / state.DEPENDENCY_ADJUDICATION).read_text(encoding="utf-8"))
        self.assertEqual(rec["verdicts"][0]["verdict"], dependency_halt.UNCONFIRMED)

    def test_malformed_proposed_block_runs_full_check(self) -> None:
        malformed = _MARKER_NOTES + '\n```toml\n[[doctor.checks\nid = "protoc"\n```\n'
        d = self._bundle(malformed)
        cfg = _cfg(self.tmp, rows=[], gate_cmd=self._gate_cmd())
        self.assertEqual(driver.run_issue(d, cfg), state.AWAITING_SIGNOFF)
        self.assertIn(_STUB_REVIEW_HEAD,
                      (d / "check-review.md").read_text(encoding="utf-8"))
        self.assertTrue(self.gate_marker.exists())
        rec = json.loads((d / state.DEPENDENCY_ADJUDICATION).read_text(encoding="utf-8"))
        self.assertEqual(rec["verdicts"][0]["verdict"], dependency_halt.UNCONFIRMED)
        self.assertIn("failed to parse", rec["verdicts"][0]["detail"])


class OffIsByteIdentical(_Base):
    """Criterion (f): opt-in for one release — off, nothing changes and nothing runs."""

    def test_off_never_probes_and_writes_no_record(self) -> None:
        probe_marker = self.tmp / "probed"
        d = self._bundle()
        cfg = _cfg(self.tmp, halt=False,
                   rows=[{"id": "protoc", "cmd": f"touch {probe_marker}; exit 1",
                          "hint": "h"}])
        self.assertEqual(driver.run_issue(d, cfg), state.AWAITING_SIGNOFF)
        self.assertFalse(probe_marker.exists(), "off must not spawn a detect cmd")
        self.assertFalse((d / state.DEPENDENCY_ADJUDICATION).exists())
        self.assertIn(_STUB_REVIEW_HEAD,
                      (d / "check-review.md").read_text(encoding="utf-8"))

    def test_config_defaults_off_and_is_a_strict_boolean(self) -> None:
        """A quoted "true" is a truthy string; this setting can skip the reviewer, so a
        non-boolean fails CLOSED (feature off), the [leaves.sandbox] network_access
        lesson (PR #292)."""
        base = '[paths]\nbundle_root = "results"\n[driver]\n'
        for body, expected in ((base, False),
                               (base + 'dependency_halt = true\n', True),
                               (base + 'dependency_halt = "true"\n', False),
                               (base + 'dependency_halt = "false"\n', False)):
            with self.subTest(body=body.splitlines()[-1]):
                (self.tmp / "pdca.toml").write_text(body, encoding="utf-8")
                self.assertIs(Config.load(self.tmp).dependency_halt, expected)


class DeclarationParsing(unittest.TestCase):
    """The name/row extraction feeding the adjudication."""

    def test_names_from_marker_lines(self) -> None:
        text = ("NEEDS-HUMAN external dependency: protoc — blocks the proto build\n"
                "- needs-human External Dependency: `docker` — the gates need a daemon\n"
                "prose mentioning NEEDS-HUMAN but no dependency marker\n")
        self.assertEqual(dependency_halt.declared_dependencies(text),
                         ["protoc", "docker"])

    def test_duplicate_declarations_collapse(self) -> None:
        text = ("NEEDS-HUMAN external dependency: protoc — one\n"
                "NEEDS-HUMAN external dependency: Protoc — two\n")
        self.assertEqual(dependency_halt.declared_dependencies(text), ["protoc"])

    def test_proposed_rows_and_the_malformed_flag(self) -> None:
        rows, malformed = dependency_halt.proposed_rows(_PROPOSED_BLOCK)
        self.assertFalse(malformed)
        self.assertEqual(rows[0]["id"], "protoc")
        rows, malformed = dependency_halt.proposed_rows("```toml\n[[broken\n```\n")
        self.assertEqual(rows, [])
        self.assertTrue(malformed)


if __name__ == "__main__":
    unittest.main()
