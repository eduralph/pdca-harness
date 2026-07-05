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

from pdca_harness import assemble, cli, gates, signoff, state
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


if __name__ == "__main__":
    unittest.main()
