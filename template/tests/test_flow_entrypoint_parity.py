"""Offline slice for issue #468 — `cli._flow` must route the single-id and the multi-id
CLI shapes through ONE drive path (`flow.flow_ids`) returning ONE TOTAL results map, with
the single-id presentation DERIVED from that map rather than from a second drive path (a
bare state string) or a second authority (a disk read only one shape performs).

Every drive here goes **through `cli._flow`** — never a hand-picked `flow.*` call — because
that is the surface where iterations 4 and 5 of #449 both found parity breaks. Fixture shape
mirrors `tests/test_flow_slice.py:31-56` (all six leaves stubbed, gates empty): no Claude,
no TTY, no Docker, no tracker.

Modules are imported, never new symbols (`from pdca_harness import cli, flow, …`): a
`from pdca_harness.flow import <new helper>` would raise ImportError on the C4 red leg,
which `engine/scripts/run-verify.sh` classifies PDCA-UNVERIFIABLE rather than red.

Run from the project root:
    PYTHONPATH=src python3 -m unittest tests.test_flow_entrypoint_parity
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from pdca_harness import cli, flow, leaves, signoff, split, state
from pdca_harness.config import Config, LeafConfig

#: The id every parity case drives ALONGSIDE the bundle under test, so the multi-id shape
#: really is a multi-id run. It is seeded in-flight and completes, i.e. it is a member of
#: the exit rule's OK set — so adding it must not move the run's exit code. That is the
#: divergence #468's second round still had: the same DISCONTINUED bundle exited 1 alone
#: and 0 beside this filler, because only one of the two shapes could see it.
FILLER = "FILLER468"


def _stub_config(root: Path) -> Config:
    """All six leaves stubbed, gates empty (all-PASS stub rows) — the same fixture shape as
    `tests/test_flow_slice.py:31-56`, the peer callsite the brief names."""
    return Config(
        root=root,
        bundle_root=root / "results",
        process_dir=root / "process",
        templates_dir=root / "templates",  # empty → planner stub uses its fallback brief
        default_branch="main",
        tracker_system="github",
        tracker_url="",
        issue_id_example="#1",
        builder=LeafConfig(mode="stub", family="claude"),
        reviewer=LeafConfig(mode="stub", family="codex"),
        planner=LeafConfig(mode="stub", family="claude", interactive=True),
        signoff=LeafConfig(mode="stub", family="claude", interactive=True),
        publisher=LeafConfig(mode="stub", family="claude", interactive=True),
        act=LeafConfig(mode="stub", family="claude", interactive=True),
        act_cadence=1,
        # Hermetic toy target inside this test's tmp root (same reason as test_flow_slice).
        repo_checkouts={"example-org/example-repo": str(root / "example-repo")},
    )


def _args(ids: list[str]) -> SimpleNamespace:
    return SimpleNamespace(issue_ids=ids, from_csv=None, from_briefs=None,
                           no_publish=True, no_act=True, by="", lanes=None)


def _state_for(iid: str, out: str) -> str | None:
    """The disposition token printed for `iid` on STDOUT, whichever shape produced `out`:
    the single-id shape prints `state<TAB><path ending in issue_<iid>>`, the multi-id one
    (`_report_batch`) prints `state<TAB><iid>`. ONE reader for both, so no comparison
    favours either format.

    Deliberately stdout-only, with no stderr fallback: an id the run skipped as terminal
    still belongs in the results map, so it still reaches the printed table. Reading its
    disposition out of the skip note on stderr instead is what let the batch shape omit
    the id from the map — and its exit-code rule — while the single-id shape reported it.
    """
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and (parts[1] == iid or parts[1].endswith(f"issue_{iid}")):
            return parts[0]
    return None


@contextmanager
def _signoff_leaf(fn):
    """Swap the stub sign-off leaf the SHARED drive path calls.

    `_drive_wave` signs a wave off through `leaves.run_signoff_batch`, which loops
    `leaves._stub_signoff` in stub mode (`leaves.py:3002-3003`) — the per-bundle
    `leaves.run_signoff` is the single-bundle library driver's leaf and is never reached
    from `cli._flow` after #468. Patch what production actually calls.
    """
    orig = leaves._stub_signoff
    leaves._stub_signoff = fn
    try:
        yield
    finally:
        leaves._stub_signoff = orig


def _split_proposal(child_labels: list[str]) -> str:
    body = "<!-- pdca:split-proposal v1 -->\n\n"
    for label in child_labels:
        body += (
            f"<!-- pdca:child {label} -->\n"
            f"- **Slug:** {label}\n"
            f"- **Defect / goal:** stub child body for {label}.\n"
            f"- **Success criterion:** stub.\n"
            f"<!-- pdca:end {label} -->\n\n"
        )
    return body


class EntrypointParity(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    # -- fixture builders --------------------------------------------------------------

    def _run(self, root: Path, ids: list[str]) -> tuple[int, str, str]:
        """Drive `ids` **through `cli._flow`** (never a hand-picked `flow.*` call)."""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli._flow(_stub_config(root), _args(ids))
        return rc, out.getvalue(), err.getvalue()

    def _fork(self, seed_root: Path) -> Path:
        """A byte-identical copy of `seed_root`'s whole disk state, so the two shapes each
        start from the SAME bytes — including the filler bundle, which the single-id run
        leaves untouched."""
        dst = Path(tempfile.mkdtemp()) / "root"
        shutil.copytree(seed_root, dst)
        self.addCleanup(shutil.rmtree, dst.parent, ignore_errors=True)
        return dst

    def _seed(self, name: str) -> Path:
        """A fresh seed root carrying the in-flight filler and nothing else."""
        seed = self.tmp / f"seed-{name}"
        self._inflight(seed, FILLER)
        return seed

    def _inflight(self, root: Path, iid: str) -> None:
        cfg = _stub_config(root)
        leaves.do_plan(cfg.bundle(iid), cfg)  # PLANNED — nothing driven yet
        self.assertEqual(state.state(cfg.bundle(iid)), state.PLANNED)

    def _complete(self, root: Path, iid: str) -> None:
        self._inflight(root, iid)
        rc, _out, _err = self._run(root, [iid])
        self.assertEqual(rc, 0)
        self.assertEqual(state.state(_stub_config(root).bundle(iid)), state.COMPLETE)

    def _discontinued(self, root: Path, iid: str) -> None:
        def discontinue(d: Path, cfg_: Config) -> None:
            (d / leaves.SIGNOFF_DECISION).write_text(
                "discontinue\nsuperseded, handled out-of-band\n", encoding="utf-8")

        self._inflight(root, iid)
        with _signoff_leaf(discontinue):
            self._run(root, [iid])
        self.assertEqual(state.state(_stub_config(root).bundle(iid)), state.DISCONTINUED)

    def _resolved(self, root: Path, iid: str) -> None:
        # Non-digit id: `sources.tracker_issue_reopened` bails at its `isdigit()` guard
        # without touching `gh`, so the shared RESOLVED revalidation stays offline here.
        d = _stub_config(root).bundle(iid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "notes.json").write_text(json.dumps({
            "resolved": {"github_state": "CLOSED", "state_reason": "COMPLETED",
                         "closed_at": "2026-01-01T00:00:00Z",
                         "note": "settled outside a cycle"},
        }), encoding="utf-8")
        self.assertEqual(state.state(d), state.RESOLVED)

    def _split_parent(self, root: Path, iid: str, child_ids: list[str]) -> Path:
        """A REAL terminal split parent: production `split.accept` (`split.py:525`) writes
        the `children` lineage edge and the close marker; the flow then drives it terminal."""
        cfg = _stub_config(root)
        pd = cfg.bundle(iid)
        leaves.do_plan(pd, cfg)  # a normal brief first — a realistic split parent
        labels = [f"child-{i + 1}" for i in range(len(child_ids))]
        (pd / split.PROPOSAL).write_text(_split_proposal(labels), encoding="utf-8")
        split.accept(pd, child_ids, cfg)
        rc, _out, _err = self._run(root, [iid])
        self.assertEqual(rc, 0)
        self.assertEqual(state.state(pd), state.COMPLETE)
        self.assertEqual((split.read_lineage(pd) or {}).get("children"), child_ids)
        return pd

    # -- the parity assertion both shapes are held to ----------------------------------

    def _assert_parity(self, seed: Path, iid: str, expected: str,
                       expected_rc: int) -> tuple[str, str]:
        """Drive the SAME bytes twice — `flow <id>` and `flow <id> <filler>` — and require
        the two shapes to agree by construction. Returns both runs' stderr.

        Three things at once, because each is a way the shapes drifted apart before:
          * the per-bundle DISPOSITION is the same token, and both read it off the printed
            table (i.e. both got it from the one results map);
          * the single-id exit code is the documented one for that state;
          * adding a COMPLETING sibling does not move it — the two shapes apply the same
            rule to the same map, so a state that fails a run alone fails it in company.
        """
        rc1, out1, err1 = self._run(self._fork(seed), [iid])
        rc2, out2, err2 = self._run(self._fork(seed), [iid, FILLER])
        self.assertEqual(_state_for(iid, out1), expected,
                         f"single-id shape must report {expected} for {iid}")
        self.assertEqual(_state_for(iid, out2), expected,
                         f"multi-id shape must report {expected} for {iid} — a skipped id "
                         "still belongs in the results map both shapes present")
        self.assertEqual(_state_for(FILLER, out2), state.COMPLETE)  # the sibling did run
        self.assertEqual(rc1, expected_rc)
        self.assertEqual(rc2, rc1,
                         "same disk state + one COMPLETING sibling must not change the "
                         "exit code — that is the shared results-map rule")
        return err1, err2

    # -- structural proof: ONE drive path, ONE authority --------------------------------

    def test_single_id_routes_through_flow_ids_and_never_flow_flow(self) -> None:
        """`cli._flow` must call the SAME `flow.flow_ids` the multi-id shape uses for
        `len(ids) == 1` too — never `flow.flow`, whose bare state string cannot carry the
        map the other shape reports. Pre-fix, `flow.flow_ids` is never reached for a single
        id and `flow.flow` is."""
        captured: dict = {}
        called_flow: list[str] = []

        def spy(cfg, ids, **kw):
            captured["ids"] = ids
            captured["plan_missing"] = kw.get("plan_missing")
            return {"SOLO468": state.COMPLETE}

        orig_ids, orig_flow = flow.flow_ids, flow.flow
        flow.flow_ids = spy
        flow.flow = lambda *a, **kw: called_flow.append("called") or state.COMPLETE
        try:
            outbuf, errbuf = io.StringIO(), io.StringIO()
            with redirect_stdout(outbuf), redirect_stderr(errbuf):
                rc = cli._flow(_stub_config(self.tmp), _args(["SOLO468"]))
            out = outbuf.getvalue()
        finally:
            flow.flow_ids, flow.flow = orig_ids, orig_flow
        self.assertEqual(captured.get("ids"), ["SOLO468"])
        self.assertTrue(captured.get("plan_missing"))
        self.assertEqual(called_flow, [])          # the second drive path is gone
        self.assertEqual(rc, 0)
        self.assertIn(f"{state.COMPLETE}\t", out)  # presentation DERIVED from that map

    def test_single_id_report_and_rc_come_from_the_map_not_from_disk(self) -> None:
        """No second authority. The single-id shape must present the DRIVE PATH's answer,
        so a map that disagrees with the bytes on disk wins — a disk read here is exactly
        what let one shape report a bundle the other could not see.

        Pre-fix this is doubly red: the COMPLETE bundle never reaches a drive path at all
        (the `cli.py:604-608` short-circuit returns 0 first), and the single-id route reads
        its answer from `flow.flow`/disk rather than from the map.
        """
        root = self.tmp / "authority"
        self._complete(root, "AUTH468")

        orig = flow.flow_ids
        flow.flow_ids = lambda cfg, ids, **kw: {"AUTH468": state.DISCONTINUED}
        try:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = cli._flow(_stub_config(root), _args(["AUTH468"]))
        finally:
            flow.flow_ids = orig
        self.assertEqual(_state_for("AUTH468", out.getvalue()), state.DISCONTINUED)
        self.assertEqual(rc, 1)   # …and the exit code follows the same single value

    def test_preflight_error_same_rc_and_message_both_shapes(self) -> None:
        """An error meant to abort a run produces the SAME rc (and message) on both shapes —
        pre-fix the single-id route had no `try/except` at all, so a `PreflightError` escaped
        `cli._flow` uncaught for `len(ids) == 1` while the batch route returned 1."""

        def boom(*_a, **_kw):
            raise flow.PreflightError("lane preflight failed for this stub")

        orig_ids, orig_flow = flow.flow_ids, flow.flow
        flow.flow_ids = boom
        flow.flow = boom
        try:
            cfg = _stub_config(self.tmp)
            out1, err1 = io.StringIO(), io.StringIO()
            with redirect_stdout(out1), redirect_stderr(err1):
                rc1 = cli._flow(cfg, _args(["A468"]))
            out2, err2 = io.StringIO(), io.StringIO()
            with redirect_stdout(out2), redirect_stderr(err2):
                rc2 = cli._flow(cfg, _args(["A468", "B468"]))
        finally:
            flow.flow_ids, flow.flow = orig_ids, orig_flow
        self.assertEqual(rc1, 1)
        self.assertEqual(rc2, 1)
        self.assertEqual(err1.getvalue(), err2.getvalue())

    # -- behavioural parity across the state matrix ------------------------------------

    def test_in_flight_bundle_agrees_across_shapes(self) -> None:
        seed = self._seed("inflight")
        self._inflight(seed, "IF468")
        self._assert_parity(seed, "IF468", state.COMPLETE, 0)

    def test_complete_bundle_agrees_across_shapes(self) -> None:
        seed = self._seed("complete")
        self._complete(seed, "DONE468")
        err1, err2 = self._assert_parity(seed, "DONE468", state.COMPLETE, 0)
        # A plain COMPLETE bundle (no lineage record) keeps the redo hint — and now BOTH
        # shapes print it, from the one shared terminal filter.
        for err in (err1, err2):
            self.assertIn("already complete — nothing to run", err)

    def test_discontinued_bundle_agrees_across_shapes(self) -> None:
        """The divergence the second round of #468 still had: DISCONTINUED is not a
        successful terminal, so it exits 1 — and must keep exiting 1 when a COMPLETING
        sibling shares the run. It could not, while the batch map dropped skipped ids."""
        seed = self._seed("discontinued")
        self._discontinued(seed, "DISC468")
        err1, err2 = self._assert_parity(seed, "DISC468", state.DISCONTINUED, 1)
        for err in (err1, err2):   # an abandoned bundle is never told to redo itself
            self.assertNotIn("rm -rf", err)

    def test_resolved_bundle_agrees_across_shapes(self) -> None:
        seed = self._seed("resolved")
        self._resolved(seed, "RES468")
        # RESOLVED is a successful no-op (#302) — 0 on both shapes.
        err1, err2 = self._assert_parity(seed, "RES468", state.RESOLVED, 0)
        # The #302 reopen remediation was single-id-only pre-fix; on the shared path both
        # shapes give it (parity in the direction that keeps the guidance, not drops it).
        for err in (err1, err2):
            self.assertIn("resolved outside a cycle", err)
            self.assertIn("notes.superseded-by-reopen.json", err)

    def test_terminal_split_parent_agrees_across_shapes(self) -> None:
        seed = self._seed("split")
        self._split_parent(seed, "PARENT468", ["469", "470"])
        self._assert_parity(seed, "PARENT468", state.COMPLETE, 0)

    def test_terminal_split_parent_names_children_never_rm_rf(self) -> None:
        """The brief's concrete defect: a terminal bundle with a `children` lineage edge (a
        split parent, `split.py:392-395`) must never be told `rm -rf` — deleting it destroys
        the one on-disk record of the split (`split.py:47`) and orphans the children. Both
        shapes must name the recovery instead."""
        seed = self._seed("split-msg")
        self._split_parent(seed, "PARENT468", ["469", "470"])
        _rc1, _out1, err1 = self._run(self._fork(seed), ["PARENT468"])
        _rc2, _out2, err2 = self._run(self._fork(seed), ["PARENT468", FILLER])
        for err in (err1, err2):
            self.assertNotIn("rm -rf", err)
            self.assertIn("pdca flow 469 470", err)

    def test_malformed_lineage_children_degrades_the_hint_not_the_run(self) -> None:
        """A hand-edited lineage record must degrade the HINT, never abort the flow.

        `split.read_lineage` is tolerant by construction about the FILE (`split.py:373-402`)
        and `split._recorded_depth` about a VALUE it cannot compute with (`split.py:405`);
        a consumer that formats `children` without the same tolerance moves the throw one
        line down — `" ".join(7)` raises TypeError straight out of `cli._flow`. And a record
        that CARRIES a `children` key is a split parent whatever its value, so the
        destructive `rm -rf` advice must stay suppressed even when the ids are unreadable.
        """
        cases = {"non-list": 7, "string": "469", "junk-entries": [1, None, {}, ""],
                 "empty-list": []}
        for name, children in cases.items():
            with self.subTest(case=name):
                seed = self._seed(f"bad-{name}")
                iid = f"BAD{name.upper().replace('-', '')}468"
                self._complete(seed, iid)
                (_stub_config(seed).bundle(iid) / split.LINEAGE).write_text(
                    json.dumps({"version": split.LINEAGE_VERSION, "id": iid,
                                "children": children}), encoding="utf-8")
                err1, err2 = self._assert_parity(seed, iid, state.COMPLETE, 0)
                for err in (err1, err2):
                    self.assertIn("already terminal (COMPLETE), skipped", err)
                    self.assertNotIn("rm -rf", err)   # a `children` key ⇒ a split parent
                    self.assertNotIn("Traceback", err)
                    # …and no invented recovery: `" ".join("469")` would offer
                    # `pdca flow 4 6 9`, which is worse advice than none.
                    self.assertNotIn("drive them instead", err)

    # -- preserved single-id presentation ----------------------------------------------

    def test_single_id_awaiting_signoff_presentation_preserved(self) -> None:
        """Single-id keeps its own presentation — the §6 listing and the rc-0
        stop-for-the-human semantics — as a PRESENTATION of the shared map, not as a
        separate drive path.

        This is the ONE documented difference between the shapes, so it is pinned from both
        sides: the same halted bundle is reported with the same disposition by both, and
        only the exit code differs — 0 for the single id (the human who typed the command
        is the intended next actor), 1 for a batch set (an unfinished member of a set).
        """
        def walk_away(d: Path, cfg_: Config) -> None:
            return None   # the human never answered — the bundle halts at AWAITING_SIGNOFF

        seed = self._seed("awaiting")
        self._inflight(seed, "AWSF468")
        with _signoff_leaf(walk_away):
            root1 = self._fork(seed)
            rc1, out1, _err1 = self._run(root1, ["AWSF468"])
            rc2, out2, _err2 = self._run(self._fork(seed), ["AWSF468", FILLER])
        self.assertEqual(_state_for("AWSF468", out1), state.AWAITING_SIGNOFF)
        self.assertEqual(_state_for("AWSF468", out2), state.AWAITING_SIGNOFF)
        self.assertEqual(rc1, 0)   # stop-for-the-human is not a failed single-id run
        self.assertEqual(rc2, 1)   # …but an unfinished member of a batch set still is
        d = _stub_config(root1).bundle("AWSF468")
        # …and the single-id shape still opens with `state<TAB>PATH` (the batch table
        # prints the bare id), so a caller piping `cut -f2` still gets a bundle path.
        self.assertTrue(out1.startswith(f"{state.AWAITING_SIGNOFF}\t{d}\n"), out1)
        open_items = signoff.open_needs_human(d / "SUMMARY.md")
        self.assertTrue(open_items)
        for it in open_items:
            self.assertIn(f"    {it}", out1)   # the §6 listing, single-id only


if __name__ == "__main__":
    unittest.main()
