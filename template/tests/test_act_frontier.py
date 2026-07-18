"""The Act review frontier (issue #299; stdlib unittest, offline).

The `.act-reviewed` marker records WHICH frozen bundles the last review covered (a
JSON object), not just a count — so `act index`/`act log` resume from the frontier by
default, out-of-order freezes surface as unreviewed, and overlapping sessions union
instead of conflicting. Proves the marker round-trip + legacy/garbage compat, the
unreviewed-set arithmetic, the CLI scope defaults, and that pattern history stays full.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from pdca_harness import act, cli, signoff
from pdca_harness.config import Config, LeafConfig

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


def _cfg(root: Path) -> Config:
    return Config(
        root=root,
        bundle_root=root / "results",
        process_dir=root / "process",
        templates_dir=TEMPLATES,
        default_branch="main",
        tracker_system="github",
        tracker_url="",
        issue_id_example="1",
        builder=LeafConfig(mode="stub"),
        reviewer=LeafConfig(mode="stub"),
    )


def _freeze(cfg: Config, iid: str, *, date: str = "2026-07-01",
            candidate: str = "") -> Path:
    """A COMPLETE (frozen) bundle with an accepted §9 dated ``date``."""
    d = cfg.bundle(iid)
    d.mkdir(parents=True)
    (d / "brief.md").write_text("- **Slug:** s\n", encoding="utf-8")
    (d / "patch.diff").write_text("diff --git a/x b/x\n", encoding="utf-8")
    (d / "check-gates.json").write_text("{}", encoding="utf-8")
    shutil.copyfile(TEMPLATES / "SUMMARY.md.tpl", d / "SUMMARY.md")
    if candidate:
        text = (d / "SUMMARY.md").read_text(encoding="utf-8")
        text = text.replace("## 10. Act candidates",
                            f"## 10. Act candidates\n- {candidate}")
        (d / "SUMMARY.md").write_text(text, encoding="utf-8")
    signoff.record(d / "SUMMARY.md", action="accept", by="T", date=date)
    return d


class MarkerFormat(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _cfg(self.tmp)
        self.marker = self.cfg.process_dir / ".act-reviewed"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mark_reviewed_writes_json_object_atomically(self) -> None:
        _freeze(self.cfg, "10")
        _freeze(self.cfg, "20")
        act.mark_reviewed(self.cfg, date="2026-07-18")
        data = json.loads(self.marker.read_text(encoding="utf-8"))
        self.assertEqual(data["reviewed"], ["issue_10", "issue_20"])
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["last_review_date"], "2026-07-18")
        self.assertFalse(self.marker.with_name(self.marker.name + ".tmp").exists())
        self.assertEqual(act.cycles_since_review(self.cfg), 0)

    def test_legacy_bare_int_marker_keeps_old_arithmetic(self) -> None:
        for iid in ("10", "20", "30"):
            _freeze(self.cfg, iid)
        self.cfg.process_dir.mkdir(parents=True, exist_ok=True)
        self.marker.write_text("2\n", encoding="utf-8")     # pre-#299 count marker
        self.assertEqual(act.cycles_since_review(self.cfg), 1)
        # Legacy heuristic: the first n name-sorted frozen bundles read as reviewed.
        self.assertEqual([d.name for d in act.unreviewed_bundles(self.cfg)],
                         ["issue_30"])

    def test_garbage_marker_means_nothing_reviewed_never_a_crash(self) -> None:
        _freeze(self.cfg, "10")
        self.cfg.process_dir.mkdir(parents=True, exist_ok=True)
        for garbage in ("{not json", '"a string"', "true", '{"reviewed": "nope"}'):
            self.marker.write_text(garbage, encoding="utf-8")
            self.assertEqual(act.cycles_since_review(self.cfg), 1, msg=garbage)
            self.assertEqual(len(act.unreviewed_bundles(self.cfg)), 1, msg=garbage)

    def test_union_across_reviews_and_deleted_bundle_intersection(self) -> None:
        a = _freeze(self.cfg, "10")
        act.mark_reviewed(self.cfg, reviewed=[a], date="2026-07-01")
        b = _freeze(self.cfg, "20")
        act.mark_reviewed(self.cfg, reviewed=[b], date="2026-07-02")
        data = json.loads(self.marker.read_text(encoding="utf-8"))
        self.assertEqual(data["reviewed"], ["issue_10", "issue_20"])  # union, not replace
        shutil.rmtree(a)                                    # a deleted bundle…
        act.mark_reviewed(self.cfg, reviewed=[], date="2026-07-03")
        data = json.loads(self.marker.read_text(encoding="utf-8"))
        self.assertEqual(data["reviewed"], ["issue_20"])    # …can't wedge the counts

    def test_out_of_order_freeze_surfaces_as_unreviewed(self) -> None:
        # The observed coverage-gap case: issue_20 froze AROUND a review that covered
        # issue_10 and issue_30 — a count marker hides it; the frontier does not.
        _freeze(self.cfg, "10")
        _freeze(self.cfg, "30")
        act.mark_reviewed(self.cfg, date="2026-07-10")      # covers 10 + 30
        _freeze(self.cfg, "20")                             # freezes out of name order
        self.assertEqual([d.name for d in act.unreviewed_bundles(self.cfg)],
                         ["issue_20"])
        self.assertEqual(act.cycles_since_review(self.cfg), 1)


class CliScope(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "pdca.toml").write_text('[paths]\nbundle_root = "results"\n',
                                            encoding="utf-8")
        self._cwd = Path.cwd()
        os.chdir(self.tmp)
        self.cfg = Config.load(self.tmp)

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _main(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_second_log_reports_all_reviewed_and_all_rereviews(self) -> None:
        _freeze(self.cfg, "1")
        rc, _out, _err = self._main(["act", "log", "--date", "2026-07-18", "--append"])
        self.assertEqual(rc, 0)
        rc, _out, err = self._main(["act", "log", "--date", "2026-07-19"])
        self.assertEqual(rc, 1)                             # frontier covers everything
        self.assertIn("no unreviewed frozen cycles", err)
        rc, out, _err = self._main(["act", "log", "--date", "2026-07-19", "--all"])
        self.assertEqual(rc, 0)                             # explicit full re-review
        self.assertIn("cycles considered: 1", out)
        # Zero frozen cycles keeps its own distinct message.
        shutil.rmtree(self.cfg.bundle("1"))
        rc, _out, err = self._main(["act", "log", "--date", "2026-07-19"])
        self.assertEqual(rc, 1)
        self.assertIn("no frozen cycles to review", err)

    def test_index_defaults_to_unreviewed_with_scope_line(self) -> None:
        _freeze(self.cfg, "1")
        self._main(["act", "log", "--date", "2026-07-18", "--append"])
        _freeze(self.cfg, "2")
        rc, out, err = self._main(["act", "index"])
        self.assertEqual(rc, 0)
        self.assertIn("1 unreviewed of 2 frozen", err)      # the scope line
        self.assertIn("issue_2", out)
        self.assertNotIn("## issue_1", out)                 # reviewed cycle not re-listed
        rc, out, _err = self._main(["act", "index", "--all"])
        self.assertIn("## issue_1", out)                    # explicit full index

    def test_append_advances_frontier_only_over_scoped_cycles(self) -> None:
        _freeze(self.cfg, "1")
        self._main(["act", "log", "--date", "2026-07-18", "--append"])
        _freeze(self.cfg, "2")
        _freeze(self.cfg, "3")
        rc, _out, _err = self._main(["act", "log", "--date", "2026-07-19", "--append"])
        self.assertEqual(rc, 0)
        log = (self.cfg.process_dir / "act-log.md").read_text(encoding="utf-8")
        self.assertIn("2026-07-19 — cycles considered: 2, 3", log)  # scoped entry only
        data = json.loads((self.cfg.process_dir / ".act-reviewed").read_text("utf-8"))
        self.assertEqual(data["reviewed"], ["issue_1", "issue_2", "issue_3"])

    def test_pattern_history_spans_the_frontier(self) -> None:
        # A signal seen once BEFORE the frontier and once after must still register as
        # recurring — narrowing the narrative scope must never narrow signal history.
        _freeze(self.cfg, "1", candidate="tighten the repro gate for flaky suites")
        self._main(["act", "log", "--date", "2026-07-18", "--append"])
        _freeze(self.cfg, "2", candidate="tighten the repro gate for flaky suites")
        rc, out, _err = self._main(["act", "log", "--date", "2026-07-19"])
        self.assertEqual(rc, 0)
        self.assertIn("2× tighten the repro gate", out)     # counted across the frontier
        ledger = act.load_ledger(self.cfg)
        self.assertTrue(any("tighten the repro gate" in e.get("raw", "") for e in ledger))


if __name__ == "__main__":
    unittest.main()
