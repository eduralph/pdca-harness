"""A builder-declared external dependency routes into SUMMARY §6 (#250).

`build-notes.md` is withheld from the reviewer and not otherwise read into `SUMMARY.md`, so
a dependency Do hit that Plan didn't list — and that no gate covers — would be lost. The
builder marks it `NEEDS-HUMAN external dependency: …`; `assemble_summary` scans build-notes
and lifts it into §6, where the C6 accept-guard then blocks accept until the human clears it.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pdca_harness import assemble, brief, cli, doctor, gates, signoff, state
from pdca_harness.config import Config, LeafConfig

_PASS_GATE = {"id": "C4", "tier": "C4", "label": "verify", "scope": "bundle",
              "gating": True, "cmd": "true"}


def _stub_config(root: Path) -> Config:
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


class ExternalDependencySection6(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundle(self, iid: str, build_notes: str) -> Path:
        """A bundle that PASSES every gate and has a clean review — so §6 is fed ONLY by
        whatever build-notes.md declares."""
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** ext\n", encoding="utf-8")
        (d / "patch.diff").write_text("--- a\n+++ b\n", encoding="utf-8")
        (d / "build-notes.md").write_text(build_notes, encoding="utf-8")
        (d / "check-review.md").write_text("All advisory items PASS.\n", encoding="utf-8")
        self.cfg.gates_checks = [_PASS_GATE]
        gates.run_gates(d, self.cfg)
        return d

    def test_marker_routes_to_section6_and_c6_blocks_accept(self) -> None:
        notes = ("Chose approach X.\n\n"
                 "NEEDS-HUMAN external dependency: protoc not installed — split-brain "
                 "freedom verified by code-read, not a compile.\n")
        d = self._bundle("EXT", notes)
        assemble.assemble_summary(d, self.cfg)
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)

        summary = d / "SUMMARY.md"
        open_items = signoff.open_needs_human(summary)
        self.assertTrue(any("protoc" in it for it in open_items),
                        f"declared external dependency not routed to §6: {open_items}")

        # C6: accept is refused while the §6 item is open …
        accept = SimpleNamespace(issue_id="EXT", accept=True, iterate_do=False,
                                 iterate_plan=False, discontinue=False, by="t", delta="")
        self.assertEqual(cli._signoff(self.cfg, accept), 1)
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)

        # … and allowed once the human clears it (an explicit override).
        summary.write_text(summary.read_text().replace("- [ ]", "- [x]"), encoding="utf-8")
        self.assertEqual(cli._signoff(self.cfg, accept), 0)
        self.assertEqual(state.state(d), state.COMPLETE)

    def test_bullet_and_case_insensitive(self) -> None:
        notes = "- needs-human External Dependency: live etcd cluster unavailable\n"
        items = assemble._declared_external_deps(notes)
        self.assertEqual(len(items), 1)
        self.assertIn("etcd", items[0])
        # the leading marker is stripped; the human-readable remainder is kept
        self.assertTrue(items[0].lower().startswith("external dependency"))

    def test_ordinary_build_notes_do_not_trip_section6(self) -> None:
        # Prose rationale — including an unrelated NEEDS-HUMAN mention — must not match.
        notes = ("Ruled out approach Y (needs a wider refactor).\n"
                 "This is not a NEEDS-HUMAN about any dependency; just notes.\n")
        self.assertEqual(assemble._declared_external_deps(notes), [])

    def test_no_build_notes_file_is_fine(self) -> None:
        d = self._bundle("NONE", "plain rationale, nothing to flag\n")
        (d / "build-notes.md").unlink()  # assemble must tolerate its absence
        assemble.assemble_summary(d, self.cfg)
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)
        self.assertEqual(signoff.open_needs_human(d / "SUMMARY.md"), [])


class UnregisteredDependencyForcingFunction(unittest.TestCase):
    """#263: a brief-declared dependency with no `[[doctor.checks]]` row blocks accept.

    The reviewer cannot do this — its sandbox has no pdca.toml, so it cannot know which rows
    are registered — and it is set membership, not judgment. The driver reconciles the two
    deterministically and routes any unregistered token into §6, where C6 holds accept until
    a detect cmd + install hint exists. That turns "seed the doctor rows where you can" into
    a forcing function, instead of letting the dependency surface as a cryptic build failure.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundle(self, iid: str, deps_field: str) -> Path:
        """Passing gates + clean review + no build-notes ⇒ §6 is fed ONLY by the
        brief-vs-doctor reconciliation."""
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text(
            f"- **Slug:** ext\n- **External dependencies:** {deps_field}\n", encoding="utf-8")
        (d / "patch.diff").write_text("--- a\n+++ b\n", encoding="utf-8")
        (d / "check-review.md").write_text("All advisory items PASS.\n", encoding="utf-8")
        self.cfg.gates_checks = [_PASS_GATE]
        gates.run_gates(d, self.cfg)
        assemble.assemble_summary(d, self.cfg)
        return d

    def test_unregistered_dep_blocks_accept_until_registered(self) -> None:
        self.cfg.doctor_checks = []                          # nothing registered
        d = self._bundle("UNREG", "`protoc` (build)")
        summary = d / "SUMMARY.md"
        open_items = signoff.open_needs_human(summary)
        self.assertTrue(any("protoc" in it for it in open_items), open_items)
        self.assertTrue(any("[[doctor.checks]]" in it for it in open_items), open_items)

        accept = SimpleNamespace(issue_id="UNREG", accept=True, iterate_do=False,
                                 iterate_plan=False, discontinue=False, by="t", delta="")
        self.assertEqual(cli._signoff(self.cfg, accept), 1)   # C6 refuses
        self.assertEqual(state.state(d), state.AWAITING_SIGNOFF)

        summary.write_text(summary.read_text().replace("- [ ]", "- [x]"), encoding="utf-8")
        self.assertEqual(cli._signoff(self.cfg, accept), 0)
        self.assertEqual(state.state(d), state.COMPLETE)

    def test_registered_dep_raises_nothing(self) -> None:
        self.cfg.doctor_checks = [
            {"id": "protoc", "cmd": "protoc --version", "hint": "apt install protobuf-compiler"}]
        d = self._bundle("REG", "`protoc` (build)")
        self.assertEqual(signoff.open_needs_human(d / "SUMMARY.md"), [])

    def test_row_without_id_falls_back_to_its_cmd(self) -> None:
        # doctor._expand_checks defaults a row's id to its cmd; the reconciliation matches.
        self.cfg.doctor_checks = [{"cmd": "protoc --version"}]
        self.assertEqual(
            assemble._unregistered_dependency_items(
                self._brief("`protoc --version`"), self.cfg), [])

    def test_row_without_a_cmd_does_not_register(self) -> None:
        # PR #269 review (codex): `doctor._expand_checks` SKIPS a row with no `cmd`, so
        # `[[doctor.checks]] id = "protoc"` runs no preflight at all. Treating it as
        # registered would silence the §6 blocker while nothing ever detects protoc —
        # defeating the detect-cmd forcing function this issue exists to create.
        self.cfg.doctor_checks = [{"id": "protoc"}]                             # no cmd
        self.assertEqual(doctor._expand_checks(self.cfg.doctor_checks, 1), [])  # never runs
        items = assemble._unregistered_dependency_items(self._brief("`protoc` (build)"), self.cfg)
        self.assertEqual(len(items), 1, "a cmd-less row must not count as registration")
        self.assertIn("protoc", items[0])

    def test_blank_cmd_does_not_register(self) -> None:
        self.cfg.doctor_checks = [{"id": "protoc", "cmd": "   "}]
        self.assertEqual(len(assemble._unregistered_dependency_items(
            self._brief("`protoc` (build)"), self.cfg)), 1)

    def test_case_insensitive_match(self) -> None:
        self.cfg.doctor_checks = [{"id": "docker", "cmd": "docker info"}]
        self.assertEqual(
            assemble._unregistered_dependency_items(self._brief("`Docker` (runtime)"), self.cfg), [])

    def _brief(self, deps_field: str) -> Path:
        p = self.tmp / f"brief-{abs(hash(deps_field))}.md"
        p.write_text(f"- **External dependencies:** {deps_field}\n", encoding="utf-8")
        return p

    def test_prose_topology_is_not_flagged(self) -> None:
        # The false-positive hazard: prose must never manufacture a dependency token.
        self.cfg.doctor_checks = []
        d = self._bundle("TOPO", "a ≥3-replica cluster that can exhibit split-brain")
        self.assertEqual(signoff.open_needs_human(d / "SUMMARY.md"), [])

    def test_no_check_annotation_is_exempt(self) -> None:
        self.cfg.doctor_checks = []
        d = self._bundle("NOCHK", "`partition-cluster` (no-check: a topology can't self-detect)")
        self.assertEqual(signoff.open_needs_human(d / "SUMMARY.md"), [])

    def test_none_is_exempt(self) -> None:
        self.cfg.doctor_checks = []
        self.assertEqual(signoff.open_needs_human(self._bundle("NONE", "none") / "SUMMARY.md"), [])

    def test_unfilled_template_placeholder_is_exempt(self) -> None:
        # brief.md.tpl's own placeholder line contains a backticked `protoc` example; an
        # unedited brief must not raise a dependency item. `field()` reads `<…>` as absent.
        self.cfg.doctor_checks = []
        d = self._bundle("TPL", "<the build tools (e.g. `protoc`), runtime services (Docker, a")
        self.assertEqual(signoff.open_needs_human(d / "SUMMARY.md"), [])

    def test_shipped_brief_template_raises_nothing(self) -> None:
        # Guard the template itself: whatever prose it carries, an unfilled brief is silent.
        tpl = Path(__file__).resolve().parents[1] / "templates" / "brief.md.tpl"
        self.assertEqual(assemble._unregistered_dependency_items(tpl, self.cfg), [])

    def test_missing_field_is_exempt(self) -> None:
        # Briefs written before #263 have no External dependencies field at all.
        p = self.tmp / "old-brief.md"
        p.write_text("- **Slug:** legacy\n", encoding="utf-8")
        self.assertEqual(assemble._unregistered_dependency_items(p, self.cfg), [])


class MidCycleRegistration(unittest.TestCase):
    """PR #269 review (codex): `Config` is loaded ONCE per invocation, but `pdca.toml` is
    edited *during* a long `pdca flow` — the Plan beat registers a row for the dependency it
    just enumerated (the planner is now instructed to), and the human pastes in the row the
    builder proposed at Do. Reconciling against the opening snapshot reports those correctly
    registered dependencies as unregistered, blocking §6 on work already done."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)
        self.brief = self.tmp / "brief.md"
        self.brief.write_text("- **External dependencies:** `protoc` (build)\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_toml(self, doctor_block: str = "") -> None:
        (self.tmp / "pdca.toml").write_text(
            '[project]\ndefault_branch = "main"\n'
            '[leaves.builder]\nmode = "stub"\n[leaves.reviewer]\nmode = "stub"\n' + doctor_block,
            encoding="utf-8")

    def test_row_registered_after_config_load_is_seen_at_check(self) -> None:
        self.cfg.doctor_checks = []          # the snapshot `Config.load()` took before Plan
        self._write_toml('[[doctor.checks]]\nid = "protoc"\ncmd = "protoc --version"\n'
                         'hint = "apt install protobuf-compiler"\n')   # Plan registered it
        self.assertEqual(assemble._unregistered_dependency_items(self.brief, self.cfg), [])

    def test_still_blocks_when_the_row_was_never_written(self) -> None:
        self.cfg.doctor_checks = [{"id": "protoc", "cmd": "protoc --version"}]  # stale snapshot
        self._write_toml()                                          # …but pdca.toml has no row
        items = assemble._unregistered_dependency_items(self.brief, self.cfg)
        self.assertEqual(len(items), 1, "disk is the truth; a stale snapshot must not excuse it")

    def test_absent_pdca_toml_falls_back_to_the_snapshot(self) -> None:
        # A synthetic Config (every test above, and `pdca` run outside a project) has no file.
        self.cfg.doctor_checks = [{"id": "protoc", "cmd": "protoc --version"}]
        self.assertFalse((self.tmp / "pdca.toml").exists())
        self.assertEqual(assemble._unregistered_dependency_items(self.brief, self.cfg), [])

    def test_malformed_pdca_toml_falls_back_to_the_snapshot(self) -> None:
        # A half-written file mid-edit must not crash an assemble.
        self.cfg.doctor_checks = [{"id": "protoc", "cmd": "protoc --version"}]
        (self.tmp / "pdca.toml").write_text("[project\nbroken", encoding="utf-8")
        self.assertEqual(assemble._unregistered_dependency_items(self.brief, self.cfg), [])


class ExternalDependencyTokens(unittest.TestCase):
    """`brief.external_dependency_tokens` — what counts as a checkable dependency (#263)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _tokens(self, value: str) -> list[str]:
        p = self.tmp / "brief.md"
        p.write_text(f"- **External dependencies:** {value}\n", encoding="utf-8")
        return brief.external_dependency_tokens(p)

    def test_backticked_tokens_extracted_in_order(self) -> None:
        self.assertEqual(self._tokens("`protoc` (build), `docker` (runtime)"),
                         ["protoc", "docker"])

    def test_duplicates_collapse(self) -> None:
        self.assertEqual(self._tokens("`protoc`, `protoc` again"), ["protoc"])

    def test_none_and_trailing_period(self) -> None:
        self.assertEqual(self._tokens("none"), [])
        self.assertEqual(self._tokens("None."), [])

    def test_prose_yields_no_tokens(self) -> None:
        self.assertEqual(self._tokens("a ≥3-replica cluster, and a partition-capable stack"), [])

    def test_no_check_and_topology_annotations_exempt(self) -> None:
        self.assertEqual(self._tokens("`cluster` (no-check: can't self-detect)"), [])
        self.assertEqual(self._tokens("`mesh` (topology — 3 replicas)"), [])

    def test_annotation_only_exempts_the_token_it_follows(self) -> None:
        self.assertEqual(
            self._tokens("`cluster` (no-check: topology), `protoc` (build)"), ["protoc"])

    def test_placeholder_reads_as_absent(self) -> None:
        self.assertEqual(self._tokens("<the build tools (e.g. `protoc`), runtime services"), [])


if __name__ == "__main__":
    unittest.main()
