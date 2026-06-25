"""Offline slice for the composable Plan-seeding sources (issue #102).

Proves the [[plan.source]] providers the harness can ship offline-testably — command,
file/glob, csv — seed a bundle's sources/ dir; that several compose; that a bad source is
non-fatal; and that the legacy notes_cmd path still runs. github/gitlab are thin gh/glab
command recipes (one is covered with a mocked subprocess). No network.
"""

from __future__ import annotations

import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pdca_harness import sources
from pdca_harness.config import Config, LeafConfig


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
        builder=LeafConfig(mode="stub"),
        reviewer=LeafConfig(mode="stub"),
        **kw,
    )


class PlanSources(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundle(self, cfg: Config, iid: str = "42") -> Path:
        d = cfg.bundle(iid)
        d.mkdir(parents=True)
        return d

    def test_command_provider_writes_into_sources(self) -> None:
        cfg = _cfg(self.tmp, plan_sources=[
            {"type": "command", "cmd": "printf ctx > \"$PDCA_SOURCES/cmd.txt\""}])
        d = self._bundle(cfg)
        sources.seed(cfg, d)
        self.assertEqual((d / "sources" / "cmd.txt").read_text(encoding="utf-8"), "ctx")

    def test_command_provider_captures_stdout_when_out_given(self) -> None:
        cfg = _cfg(self.tmp, plan_sources=[
            {"type": "command", "cmd": "echo captured", "out": "cap.txt"}])
        d = self._bundle(cfg)
        sources.seed(cfg, d)
        self.assertEqual((d / "sources" / "cap.txt").read_text(encoding="utf-8"), "captured\n")

    def test_file_provider_copies_linked_artifact(self) -> None:
        adr = self.tmp / "docs" / "adr"
        adr.mkdir(parents=True)
        (adr / "adr-42.md").write_text("# ADR 42\n", encoding="utf-8")
        cfg = _cfg(self.tmp, plan_sources=[{"type": "file", "path": "docs/adr/adr-{id}.md"}])
        d = self._bundle(cfg)
        sources.seed(cfg, d)
        self.assertEqual((d / "sources" / "adr-42.md").read_text(encoding="utf-8"), "# ADR 42\n")

    def test_csv_provider_extracts_the_issue_row(self) -> None:
        (self.tmp / "export.csv").write_text(
            "id,summary\n41,other\n42,the target row\n", encoding="utf-8")
        cfg = _cfg(self.tmp, plan_sources=[{"type": "csv", "path": "export.csv", "key": "id"}])
        d = self._bundle(cfg)
        sources.seed(cfg, d)
        row = (d / "sources" / "export.csv").read_text(encoding="utf-8")
        self.assertIn("42,the target row", row)
        self.assertNotIn("41,other", row)        # only the matching row is kept

    def test_multiple_sources_compose(self) -> None:
        (self.tmp / "spec.md").write_text("spec\n", encoding="utf-8")
        cfg = _cfg(self.tmp, plan_sources=[
            {"type": "command", "cmd": "echo c", "out": "cmd.txt"},
            {"type": "file", "path": "spec.md"}])
        d = self._bundle(cfg)
        sources.seed(cfg, d)
        names = {p.name for p in (d / "sources").iterdir()}
        self.assertEqual(names, {"cmd.txt", "spec.md"})   # the ticket AND the linked spec

    def test_github_provider_uses_gh_view(self) -> None:
        cfg = _cfg(self.tmp, plan_sources=[{"type": "github"}])
        d = self._bundle(cfg)
        fake = SimpleNamespace(returncode=0, stdout='{"title":"t"}', stderr="")
        with mock.patch("pdca_harness.sources.subprocess.run", return_value=fake) as run:
            sources.seed(cfg, d)
        self.assertIn("issue", run.call_args[0][0])       # `gh issue view 42 --json …`
        self.assertEqual((d / "sources" / "github-42.json").read_text(encoding="utf-8"),
                         '{"title":"t"}')

    def test_bad_source_is_non_fatal(self) -> None:
        cfg = _cfg(self.tmp, plan_sources=[
            {"type": "command", "cmd": "exit 7"},                 # fails
            {"type": "nonsense"},                                 # unknown type
            {"type": "command", "cmd": "echo ok", "out": "ok.txt"}])  # still runs
        d = self._bundle(cfg)
        buf = io.StringIO()
        with redirect_stderr(buf):
            sources.seed(cfg, d)                                  # must not raise
        self.assertTrue((d / "sources" / "ok.txt").exists())      # later source still ran
        self.assertIn("nonsense", buf.getvalue())                 # unknown type noted

    def test_legacy_notes_cmd_still_runs(self) -> None:
        cfg = _cfg(self.tmp, notes_cmd="printf thread > \"$PDCA_BUNDLE/notes.json\"")
        d = self._bundle(cfg)
        sources.seed(cfg, d)
        self.assertTrue((d / "notes.json").exists())   # #65 back-compat preserved

    def test_tracker_role_github_sources_once_and_skips_notes_cmd(self) -> None:
        # #132: a github plan.source declared role="tracker" writes the canonical
        # notes.json and the legacy notes_cmd is skipped — the issue is fetched once,
        # not stored in both notes.json and sources/github-<id>.json.
        cfg = _cfg(
            self.tmp,
            notes_cmd="printf NOTES_CMD_RAN > \"$PDCA_BUNDLE/notes.json\"",
            plan_sources=[{"type": "github", "role": "tracker"}])
        d = self._bundle(cfg)
        fake = SimpleNamespace(returncode=0, stdout='{"title":"t"}', stderr="")
        with mock.patch("pdca_harness.sources.subprocess.run", return_value=fake):
            sources.seed(cfg, d)
        self.assertEqual((d / "notes.json").read_text(encoding="utf-8"), '{"title":"t"}')
        self.assertNotIn("NOTES_CMD_RAN", (d / "notes.json").read_text(encoding="utf-8"))
        self.assertFalse((d / "sources" / "github-42.json").exists())  # not stored twice

    def test_tracker_role_command_writes_notes_json_and_skips_notes_cmd(self) -> None:
        # The `command` escape hatch as the tracker source: its stdout becomes notes.json
        # and notes_cmd does not also run (moving a notes_cmd into a tracker plan.source
        # must not run it twice).
        cfg = _cfg(
            self.tmp,
            notes_cmd="printf NOTES_CMD_RAN > \"$PDCA_BUNDLE/notes.json\"",
            plan_sources=[{"type": "command", "role": "tracker", "cmd": "echo thread"}])
        d = self._bundle(cfg)
        sources.seed(cfg, d)
        self.assertEqual((d / "notes.json").read_text(encoding="utf-8"), "thread\n")

    def test_tracker_command_that_writes_notes_json_is_not_clobbered_by_stdout(self) -> None:
        # A migrated notes_cmd writes notes.json itself and may also log to stdout; the
        # real thread must survive, not be replaced by the log text (Codex review, PR #141).
        cfg = _cfg(self.tmp, plan_sources=[{
            "type": "command", "role": "tracker",
            "cmd": "printf REAL_THREAD > \"$PDCA_BUNDLE/notes.json\"; echo progress-log"}])
        d = self._bundle(cfg)
        sources.seed(cfg, d)
        self.assertEqual((d / "notes.json").read_text(encoding="utf-8"), "REAL_THREAD")

    def test_non_tracker_source_leaves_notes_cmd_running(self) -> None:
        # Back-compat: a plain (non-tracker) plan.source does NOT suppress notes_cmd.
        cfg = _cfg(
            self.tmp,
            notes_cmd="printf thread > \"$PDCA_BUNDLE/notes.json\"",
            plan_sources=[{"type": "file", "path": "missing-{id}.md"}])
        d = self._bundle(cfg)
        sources.seed(cfg, d)
        self.assertEqual((d / "notes.json").read_text(encoding="utf-8"), "thread")


if __name__ == "__main__":
    unittest.main()
