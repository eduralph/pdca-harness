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
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

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
        self.assertEqual(list(self.cfg.process_dir.glob(".act-reviewed.tmp*")), [])
        self.assertEqual(act.cycles_since_review(self.cfg), 0)

    def test_legacy_bare_int_marker_keeps_cadence_but_infers_no_names(self) -> None:
        # #299 review round 3: a count carries NO name information. Inferring a sorted
        # prefix misassigns when a new bundle sorts before reviewed ones (issue_10
        # freezing after 20/30; 99→100 digit boundaries) — and a misassignment is a
        # PERMANENT silent skip once the first --append converts it to a frontier. So
        # the cadence keeps the legacy arithmetic, but the scope treats EVERYTHING as
        # unreviewed until a real frontier is recorded (fail toward re-review).
        for iid in ("10", "20", "30"):
            _freeze(self.cfg, iid)
        self.cfg.process_dir.mkdir(parents=True, exist_ok=True)
        self.marker.write_text("2\n", encoding="utf-8")     # pre-#299 count marker
        self.assertEqual(act.cycles_since_review(self.cfg), 1)   # cadence: old arithmetic
        self.assertFalse(act.has_frontier(self.cfg))
        self.assertEqual([d.name for d in act.unreviewed_bundles(self.cfg)],
                         ["issue_10", "issue_20", "issue_30"])   # scope: no inference
        # A pre-existing-but-newly-sorting-first bundle can therefore never be skipped:
        # the first frontier write records exactly what THAT review covered.
        act.mark_reviewed(self.cfg, reviewed=[self.cfg.bundle("issue_30".removeprefix("issue_"))],
                          date="2026-07-19")
        data = json.loads(self.marker.read_text(encoding="utf-8"))
        self.assertEqual(data["reviewed"], ["issue_30"])    # legacy count contributed nothing

    def test_act_scope_uses_one_frozen_snapshot(self) -> None:
        # #299 review round 3: both scopes must come from ONE glob — a bundle freezing
        # between two would be marked reviewed while missing from the signal history.
        _freeze(self.cfg, "1")
        import argparse
        args = argparse.Namespace(all=False, since=None)
        with mock.patch.object(cli.act, "frozen_bundles",
                               wraps=cli.act.frozen_bundles) as fb:
            cli._act_scope(self.cfg, args)
        self.assertEqual(fb.call_count, 1)

    def test_garbage_marker_means_nothing_reviewed_never_a_crash(self) -> None:
        _freeze(self.cfg, "10")
        self.cfg.process_dir.mkdir(parents=True, exist_ok=True)
        # `{"count": true}` is the #299-review case: bool is an int subclass, and
        # accepting it would silently treat one frozen bundle as reviewed.
        for garbage in ("{not json", '"a string"', "true", '{"reviewed": "nope"}',
                        '{"count": true}'):
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

    def test_concurrent_marks_union_not_overwrite(self) -> None:
        # #299 review: two overlapping Act sessions must serialize on the marker —
        # the read-union-write is flock-guarded and temp files are per-writer, so
        # neither invocation's reviewed set is lost and no stray tmp survives.
        a = _freeze(self.cfg, "10")
        b = _freeze(self.cfg, "20")
        import threading
        ts = [threading.Thread(target=act.mark_reviewed, args=(self.cfg,),
                               kwargs={"reviewed": [d], "date": "2026-07-18"})
              for d in (a, b)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        data = json.loads(self.marker.read_text(encoding="utf-8"))
        self.assertEqual(data["reviewed"], ["issue_10", "issue_20"])
        self.assertEqual(list(self.cfg.process_dir.glob(".act-reviewed.tmp*")), [])

    def test_auto_act_frontier_uses_the_pre_session_snapshot(self) -> None:
        # #299 review round 5: the review can only have covered what existed when it
        # started — a bundle freezing mid-session, and a bundle whose revalidation
        # delta landed mid-session, must both stay OUT of the frontier advance.
        from pdca_harness import leaves
        from pdca_harness.config import LeafConfig
        reviewed_early = _freeze(self.cfg, "50")
        self.cfg.act = LeafConfig(mode="stub", interactive=True)
        self.cfg.act_cadence = 1                           # due: run_act re-checks (#299 r11)
        self.cfg.templates_dir = self.cfg.root / "no-templates"

        real_stub = leaves._stub_act

        def stub_with_midrun_events(cfg_, date_, bundles=None):
            real_stub(cfg_, date_, bundles=bundles)
            _freeze(self.cfg, "60")                     # froze while the session ran
            (reviewed_early / "revalidation-2026-07-19.json").write_text(
                json.dumps({"date": "2026-07-19", "rows": [
                    {"check": "C4", "old": "pass", "new": "fail", "changed": True,
                     "gating": True}], "changed": True}), encoding="utf-8")

        with mock.patch.object(leaves, "_stub_act", side_effect=stub_with_midrun_events):
            leaves.run_act(self.cfg, "2026-07-19")
        names = [d.name for d in act.unreviewed_bundles(self.cfg)]
        self.assertIn("issue_60", names)                # mid-session freeze not marked
        self.assertIn("issue_50", names)                # mid-session delta not re-hidden

    def test_unmark_serializes_with_the_first_frontier_write(self) -> None:
        # #299 review round 9: with NO marker yet (the first-ever Act), unmark must
        # still enter the marker's critical section — an unlocked absent-check could
        # no-op while mark_reviewed (whose in-lock delta scan predates the stamp)
        # publishes a frontier containing the changed bundle. Simulate the writer
        # holding the lock: unmark must not return until it is released.
        import threading
        d = _freeze(self.cfg, "90")
        self.cfg.process_dir.mkdir(parents=True, exist_ok=True)
        self.assertFalse(self.marker.exists())             # first review: no marker
        lock = self.marker.with_name(self.marker.name + ".lock").open("w")
        self.addCleanup(lock.close)
        act._lock_exclusive(lock)
        done = threading.Event()

        def run_unmark():
            act.unmark_reviewed(self.cfg, d)
            done.set()

        t = threading.Thread(target=run_unmark)
        t.start()
        self.assertFalse(done.wait(0.3))                   # blocked behind the writer
        act._unlock(lock)
        t.join(timeout=5)
        self.assertTrue(done.is_set())

    def test_delta_since_tolerates_filesystem_clock_skew(self) -> None:
        # #299 review round 7: fs mtimes and time.time() are different clocks — a
        # stamp written moments AFTER `started` can carry an mtime just before it.
        # The slack errs toward re-review; a genuinely old stamp still doesn't count.
        d = _freeze(self.cfg, "80")
        stamp = d / "revalidation-2026-07-19.json"
        stamp.write_text(json.dumps({"changed": True, "rows": []}), encoding="utf-8")
        st = stamp.stat().st_mtime
        self.assertTrue(act.delta_since(d, st + 1.0))       # within slack: a delta
        self.assertFalse(act.delta_since(d, st + act._MTIME_SLACK + 5))  # well past

    def test_mark_reviewed_delta_guard_withholds_inside_the_critical_section(self) -> None:
        # #299 review round 7: the delta scan runs INSIDE mark_reviewed's flock'd
        # section (a scan outside it races revalidate's unmark_reviewed); the
        # withheld names come back so callers can report them.
        a = _freeze(self.cfg, "10")
        b = _freeze(self.cfg, "20")
        started = time.time()
        (a / "revalidation-2026-07-19.json").write_text(
            json.dumps({"changed": True, "rows": []}), encoding="utf-8")
        withheld = act.mark_reviewed(self.cfg, reviewed=[a, b], date="2026-07-19",
                                     delta_guard=started)
        self.assertEqual(withheld, ["issue_10"])
        self.assertEqual([d.name for d in act.unreviewed_bundles(self.cfg)],
                         ["issue_10"])
        del b  # (fixture bookkeeping)

    def test_review_content_matches_the_frontier_snapshot(self) -> None:
        # #299 review round 13: a bundle freezing between run_act's snapshot and the
        # leaf's indexing must not enter the LOGGED review either — the reviewed
        # content and the advanced frontier must describe the SAME snapshot, or the
        # next cadence reviews and logs the bundle a second time.
        from pdca_harness import leaves
        from pdca_harness.config import LeafConfig
        _freeze(self.cfg, "40")
        self.cfg.act = LeafConfig(mode="stub", interactive=True)
        self.cfg.act_cadence = 1
        self.cfg.templates_dir = self.cfg.root / "no-templates"
        calls = {"n": 0}
        real_frozen = act.frozen_bundles

        def snap_then_freeze(cfg_):
            # Call 1 = run_act's act_due check; call 2 = the covered snapshot —
            # freeze issue_41 immediately AFTER that snapshot was taken.
            calls["n"] += 1
            out = real_frozen(cfg_)
            if calls["n"] == 2:
                _freeze(self.cfg, "41")
            return out

        with mock.patch.object(act, "frozen_bundles", side_effect=snap_then_freeze):
            leaves.run_act(self.cfg, "2026-07-19")
        log = (self.cfg.process_dir / "act-log.md").read_text(encoding="utf-8")
        self.assertIn("cycles considered: 40\n", log)      # exactly the snapshot
        self.assertNotIn("41", log.split("##")[0])         # 41 absent from the header
        self.assertEqual([b.name for b in act.unreviewed_bundles(self.cfg)],
                         ["issue_41"])                     # covered next cadence

    def test_concurrent_auto_act_waits_then_reviews_what_is_left(self) -> None:
        # #299 review rounds 11/14: two flows completing at once both pass act_due
        # before either advances the marker — the loser WAITS for the active session
        # (a skip would leave its newly frozen bundles without their promised
        # automatic review until some unrelated later flow completed), then the
        # cadence re-check decides whether anything is left.
        import threading
        from pdca_harness import leaves, worktree
        from pdca_harness.config import LeafConfig
        _freeze(self.cfg, "95")
        self.cfg.act = LeafConfig(mode="stub", interactive=True)
        self.cfg.act_cadence = 1
        self.cfg.templates_dir = self.cfg.root / "no-templates"
        self.cfg.process_dir.mkdir(parents=True, exist_ok=True)
        session = (self.cfg.process_dir / ".act-session.lock").open("w")
        self.addCleanup(session.close)
        worktree._lock_file(session, wait=False)           # the "other" running Act
        done = threading.Event()
        err = io.StringIO()

        def run():
            with redirect_stderr(err):
                leaves.run_act(self.cfg, "2026-07-19")
            done.set()

        t = threading.Thread(target=run)
        t.start()
        self.assertFalse(done.wait(0.3))                   # waiting, not skipping
        worktree._unlock_file(session)
        t.join(timeout=10)
        self.assertTrue(done.is_set())
        # The winner had reviewed nothing here, so the waiter's re-check found the
        # cadence still due and it ran ITS review over the still-unreviewed cycle.
        self.assertTrue(self.marker.exists())
        self.assertTrue((self.cfg.process_dir / "act-log.md").exists())
        self.assertEqual(act.unreviewed_bundles(self.cfg), [])

    def test_auto_act_no_longer_due_after_a_concurrent_session_skips(self) -> None:
        # The loser that acquires AFTER the winner finished re-checks the cadence
        # under the session lock and skips instead of duplicating the review.
        from pdca_harness import leaves
        from pdca_harness.config import LeafConfig
        _freeze(self.cfg, "96")
        self.cfg.act = LeafConfig(mode="stub", interactive=True)
        self.cfg.act_cadence = 1
        self.cfg.templates_dir = self.cfg.root / "no-templates"
        act.mark_reviewed(self.cfg, date="2026-07-19")     # the winner covered it all
        err = io.StringIO()
        with redirect_stderr(err):
            leaves.run_act(self.cfg, "2026-07-19")
        self.assertIn("no longer due", err.getvalue())
        self.assertFalse((self.cfg.process_dir / "act-log.md").exists())

    def test_confirming_midsession_revalidation_still_advances_the_frontier(self) -> None:
        # #299 review round 6: the stamp's `changed` VERDICT decides, never its mtime
        # alone — a concurrent revalidation that CONFIRMED the frozen record is not
        # new Act signal, and withholding its bundle would inflate
        # cycles_since_review into a redundant extra Act run.
        from pdca_harness import leaves
        from pdca_harness.config import LeafConfig
        confirmed = _freeze(self.cfg, "70")
        self.cfg.act = LeafConfig(mode="stub", interactive=True)
        self.cfg.act_cadence = 1                           # due: run_act re-checks (#299 r11)
        self.cfg.templates_dir = self.cfg.root / "no-templates"
        real_stub = leaves._stub_act

        def stub_with_confirming_reval(cfg_, date_, bundles=None):
            real_stub(cfg_, date_, bundles=bundles)
            (confirmed / "revalidation-2026-07-19.json").write_text(
                json.dumps({"date": "2026-07-19", "changed": False, "rows": []}),
                encoding="utf-8")

        with mock.patch.object(leaves, "_stub_act",
                               side_effect=stub_with_confirming_reval):
            leaves.run_act(self.cfg, "2026-07-19")
        self.assertEqual(act.unreviewed_bundles(self.cfg), [])  # frontier advanced

    def test_revalidation_delta_reopens_the_reviewed_bundle(self) -> None:
        # #299 review round 4: a revalidation DELTA on a frozen cycle is new Act signal
        # — the bundle must re-enter the default scope instead of hiding behind the
        # frontier until an unrelated bundle happens to freeze.
        from pdca_harness import revalidate
        d = _freeze(self.cfg, "40")
        act.mark_reviewed(self.cfg, date="2026-07-19")
        self.assertEqual(act.unreviewed_bundles(self.cfg), [])
        res = revalidate.revalidate(self.cfg, d, "2026-07-20")   # stub re-gate ⇒ deltas
        self.assertTrue(res["changed"])
        self.assertEqual([b.name for b in act.unreviewed_bundles(self.cfg)],
                         ["issue_40"])
        self.assertEqual(act.cycles_since_review(self.cfg), 1)   # cadence sees it too

    def test_withheld_delta_also_leaves_the_prior_frontier(self) -> None:
        # #299 review round 20: a re-review (auto-Act / --all) of an already-covered
        # bundle whose revalidation delta landed mid-session must not have the union
        # re-add it from the PRIOR frontier — even when revalidate's unmark is
        # interrupted after durably writing the stamp, the delta (a PASS→FAIL
        # regression included) stays in the default scope.
        d = _freeze(self.cfg, "70")
        act.mark_reviewed(self.cfg, date="2026-07-01")     # covered in prior
        started = time.time()
        (d / "revalidation-2026-07-19.json").write_text(
            json.dumps({"changed": True, "rows": []}), encoding="utf-8")
        withheld = act.mark_reviewed(self.cfg, date="2026-07-19",
                                     delta_guard=started)  # the re-review
        self.assertEqual(withheld, ["issue_70"])
        self.assertEqual([b.name for b in act.unreviewed_bundles(self.cfg)],
                         ["issue_70"])                     # dropped from prior too

    def test_fingerprint_is_line_ending_invariant(self) -> None:
        # #299 review round 19: a frontier shared between checkouts with different
        # core.autocrlf settings must not read unchanged content as a new
        # generation — CRLF and LF forms of the same summary hash identically,
        # while a real content change still differs.
        d = _freeze(self.cfg, "60")
        lf = (d / "SUMMARY.md").read_text(encoding="utf-8")
        base = act._fingerprint(d)
        (d / "SUMMARY.md").write_bytes(lf.replace("\n", "\r\n").encode("utf-8"))
        self.assertEqual(act._fingerprint(d), base)        # CRLF form: same identity
        (d / "SUMMARY.md").write_text(lf + "changed\n", encoding="utf-8")
        self.assertNotEqual(act._fingerprint(d), base)     # real change: differs

    def test_midsession_recreation_is_never_attested(self) -> None:
        # #299 review round 17: the fingerprint rides the pre-session snapshot — a
        # bundle recreated WHILE the review runs must not be attested by a hash
        # computed from the new generation's file after the session.
        from pdca_harness import leaves
        from pdca_harness.config import LeafConfig
        d = _freeze(self.cfg, "55", date="2026-07-01")
        self.cfg.act = LeafConfig(mode="stub", interactive=True)
        self.cfg.act_cadence = 1
        self.cfg.templates_dir = self.cfg.root / "no-templates"
        real_stub = leaves._stub_act

        def stub_recreates(cfg_, date_, bundles=None):
            real_stub(cfg_, date_, bundles=bundles)
            shutil.rmtree(d)
            _freeze(self.cfg, "55", date="2026-07-10")     # generation B, mid-session

        with mock.patch.object(leaves, "_stub_act", side_effect=stub_recreates):
            leaves.run_act(self.cfg, "2026-07-02")
        self.assertEqual([b.name for b in act.unreviewed_bundles(self.cfg)],
                         ["issue_55"])                     # gen B was never attested

    def test_recreated_bundle_is_unreviewed_again(self) -> None:
        # #299 review round 16: the documented redo (rm -rf + rerun) recreates
        # issue_X under the SAME name — the frontier keys coverage on the frozen
        # record's fingerprint, so the new generation re-enters the default scope
        # and the cadence instead of being silently treated as reviewed.
        d = _freeze(self.cfg, "50", date="2026-07-01")
        act.mark_reviewed(self.cfg, date="2026-07-02")
        self.assertEqual(act.unreviewed_bundles(self.cfg), [])
        shutil.rmtree(d)
        _freeze(self.cfg, "50", date="2026-07-10")         # the redo generation
        self.assertEqual([b.name for b in act.unreviewed_bundles(self.cfg)],
                         ["issue_50"])
        self.assertEqual(act.cycles_since_review(self.cfg), 1)  # auto-Act due again
        # Reviewing the new generation records ITS fingerprint and covers it.
        act.mark_reviewed(self.cfg, date="2026-07-11")
        self.assertEqual(act.unreviewed_bundles(self.cfg), [])

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

    def test_append_leaves_a_midwrite_delta_unreviewed(self) -> None:
        # #299 review rounds 6/10: a `pdca revalidate` recording a REAL delta while
        # the append transaction runs was not in the entry just logged — the in-lock
        # delta_guard scan must withhold its bundle from the frontier. (Revalidate's
        # own unmark_reviewed blocks on the same lock until the transaction ends —
        # the stamp, written before it calls unmark, is what the scan sees.)
        d1 = _freeze(self.cfg, "1")
        _freeze(self.cfg, "2")
        real_append = act.append_entry

        def append_and_race(cfg_, text_):
            out = real_append(cfg_, text_)
            (d1 / "revalidation-2026-07-19.json").write_text(
                json.dumps({"date": "2026-07-19", "changed": True,
                            "rows": [{"check": "C4", "old": "pass", "new": "fail",
                                      "changed": True, "gating": True}]}),
                encoding="utf-8")
            return out

        with mock.patch.object(act, "append_entry", side_effect=append_and_race):
            rc, _out, err = self._main(["act", "log", "--date", "2026-07-19", "--append"])
        self.assertEqual(rc, 0)
        self.assertIn("left unreviewed", err)
        self.assertEqual([b.name for b in act.unreviewed_bundles(self.cfg)],
                         ["issue_1"])          # the delta'd cycle stays in scope

    def test_manual_append_respects_the_act_session_lock(self) -> None:
        # #299 review round 12: `act log --append` overlapping a flow's auto-Act
        # would log-and-mark the very snapshot the leaf is still reviewing — the
        # manual writing path holds the SAME session lock and refuses while it is
        # taken, instead of relying on the marker union to undo a duplicate entry.
        _freeze(self.cfg, "1")
        self.cfg.process_dir.mkdir(parents=True, exist_ok=True)
        with act.act_session(self.cfg) as held:            # the "running" auto-Act
            self.assertTrue(held)
            rc, _out, err = self._main(["act", "log", "--date", "2026-07-19",
                                        "--append"])
        self.assertEqual(rc, 1)
        self.assertIn("another Act session is running", err)
        self.assertFalse((self.cfg.process_dir / "act-log.md").exists())
        # Released → the append proceeds normally.
        rc, _out, _err = self._main(["act", "log", "--date", "2026-07-19", "--append"])
        self.assertEqual(rc, 0)
        self.assertTrue((self.cfg.process_dir / "act-log.md").exists())

    def test_full_scope_append_attests_the_extracted_generation(self) -> None:
        # #299 review round 18: `act log --all --append` must attest the entries'
        # EXTRACTION-time fingerprints — a bundle recreated between the scaffold and
        # the frontier write is a new generation the logged review never read, so it
        # must remain unreviewed afterwards.
        d = _freeze(self.cfg, "1", date="2026-07-01")
        real_append = act.append_entry

        def append_and_recreate(cfg_, text_):
            out = real_append(cfg_, text_)
            shutil.rmtree(d)
            _freeze(self.cfg, "1", date="2026-07-12")      # generation B, mid-append
            return out

        with mock.patch.object(act, "append_entry", side_effect=append_and_recreate):
            rc, _out, _err = self._main(["act", "log", "--date", "2026-07-19",
                                         "--all", "--append"])
        self.assertEqual(rc, 0)
        self.assertEqual([b.name for b in act.unreviewed_bundles(self.cfg)],
                         ["issue_1"])                      # gen B was never attested

    def test_append_transaction_rescopes_under_the_lock(self) -> None:
        # #299 review round 10: two overlapping default appends must not both log
        # the same cycles — the loser re-scopes INSIDE the marker critical section,
        # re-renders for the surviving cycles only, and appends nothing when none
        # survive.
        a = _freeze(self.cfg, "1")
        _freeze(self.cfg, "2")
        entries = act.index(self.cfg)
        act.mark_reviewed(self.cfg, reviewed=[a], date="2026-07-19")  # winner landed
        log, kept, _withheld = act.append_reviewed(
            self.cfg, entries,
            lambda kept: "entry for " + ", ".join(e.bundle.name for e in kept),
            date="2026-07-19")
        self.assertEqual([e.bundle.name for e in kept], ["issue_2"])
        text = log.read_text(encoding="utf-8")
        self.assertIn("issue_2", text)
        self.assertNotIn("entry for issue_1", text)        # not double-logged
        self.assertEqual(act.unreviewed_bundles(self.cfg), [])
        # Everything now covered → nothing at all is appended.
        log2, kept2, _ = act.append_reviewed(
            self.cfg, entries, lambda kept: "duplicate entry", date="2026-07-19")
        self.assertIsNone(log2)
        self.assertEqual(kept2, [])
        self.assertNotIn("duplicate entry", log.read_text(encoding="utf-8"))

    def test_append_still_covers_a_confirming_midwrite_revalidation(self) -> None:
        # The mirror case (#299 review round 6): a confirming stamp (changed: false)
        # landing mid-append is not new signal and must not withhold the frontier.
        d1 = _freeze(self.cfg, "1")
        real_append = act.append_entry

        def append_and_confirm(cfg_, text_):
            out = real_append(cfg_, text_)
            (d1 / "revalidation-2026-07-19.json").write_text(
                json.dumps({"date": "2026-07-19", "changed": False, "rows": []}),
                encoding="utf-8")
            return out

        with mock.patch.object(act, "append_entry", side_effect=append_and_confirm):
            rc, _out, err = self._main(["act", "log", "--date", "2026-07-19", "--append"])
        self.assertEqual(rc, 0)
        self.assertNotIn("left unreviewed", err)
        self.assertEqual(act.unreviewed_bundles(self.cfg), [])

    def test_fully_reviewed_index_still_renders_the_signal_history(self) -> None:
        # #299 review: an empty SCOPED set ("everything reviewed") must not discard the
        # full-history sections — the recurring signals and the process-delta ledger are
        # exactly what an operator checks between reviews.
        _freeze(self.cfg, "1", candidate="tighten the repro gate for flaky suites")
        _freeze(self.cfg, "2", candidate="tighten the repro gate for flaky suites")
        self._main(["act", "log", "--date", "2026-07-18", "--append"])   # registers + covers all
        rc, out, err = self._main(["act", "index"])
        self.assertEqual(rc, 0)
        self.assertIn("0 unreviewed of 2 frozen", err)
        self.assertIn("(no cycles in scope)", out)
        self.assertIn("2× tighten the repro gate", out)   # signal history preserved
        self.assertIn("Process-delta ledger", out)
        self.assertIn("tighten the repro gate", out.split("Process-delta ledger")[1])

    def test_since_append_still_registers_cross_date_recurrences(self) -> None:
        # #299 review round 7: --since narrows only the NARRATIVE scope — a signal
        # seen once before the requested date and once after must still register as
        # recurring when --since rides --append.
        _freeze(self.cfg, "1", date="2026-07-01",
                candidate="tighten the repro gate for flaky suites")
        _freeze(self.cfg, "2", date="2026-07-15",
                candidate="tighten the repro gate for flaky suites")
        rc, _out, _err = self._main(["act", "log", "--date", "2026-07-19",
                                     "--since", "2026-07-10", "--append"])
        self.assertEqual(rc, 0)
        log = (self.cfg.process_dir / "act-log.md").read_text(encoding="utf-8")
        self.assertIn("cycles considered: 2\n", log)        # narrative: post-date only
        ledger = act.load_ledger(self.cfg)                  # history: full, recurring
        self.assertTrue(any("tighten the repro gate" in e.get("raw", "")
                            for e in ledger))

    def test_pattern_history_spans_the_frontier(self) -> None:
        # A signal seen once BEFORE the frontier and once after must still register as
        # recurring — narrowing the narrative scope must never narrow signal history.
        _freeze(self.cfg, "1", candidate="tighten the repro gate for flaky suites")
        self._main(["act", "log", "--date", "2026-07-18", "--append"])
        _freeze(self.cfg, "2", candidate="tighten the repro gate for flaky suites")
        rc, out, _err = self._main(["act", "log", "--date", "2026-07-19"])
        self.assertEqual(rc, 0)
        self.assertIn("2× tighten the repro gate", out)     # counted across the frontier
        self.assertEqual(act.load_ledger(self.cfg), [])     # preview registers nothing (#298 review)
        rc, _out, _err = self._main(["act", "log", "--date", "2026-07-19", "--append"])
        self.assertEqual(rc, 0)
        ledger = act.load_ledger(self.cfg)                  # recording registers, over ALL history
        self.assertTrue(any("tighten the repro gate" in e.get("raw", "") for e in ledger))


if __name__ == "__main__":
    unittest.main()
