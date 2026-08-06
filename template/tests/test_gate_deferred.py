"""Offline slice for the `deferred` gate-result class (issue #401, stdlib unittest).

A gate that RAN and found its subject **absent by design** — the artifacts it audits are
drafted later in the cycle — declares `deferred` (a line it STARTS with the
`PDCA-DEFERRED:` marker while exiting 0) instead of a green nobody can reproduce. The case
that motivates it: the bundle-scoped T4 contribution row runs at Check, where
`pr-description.md` / `commit-msg.txt` do not exist yet (publish drafts them), so
`pdca contribcheck` was default-open and the matrix recorded a plain PASS — with an empty
evidence line. The reviewer, contractually required to reproduce every recorded green,
structurally cannot (those artifacts are not among its inputs), so it marked the row
provisional and T4 landed in SUMMARY §6 NEEDS-HUMAN on 9 of 9 frozen bundles, cleared
unread every time — which degrades C6, the guard §6 exists for.

Proves, with real gate commands and real bundle directories (no Claude / Docker / network):

* the gate runner classifies the declaration as `deferred`, which does NOT count toward
  `overall` — neither a green nor a gating red (`gates._classify` / `_finalize`);
* it is honoured ONLY for a row something re-runs later (`gates._deferrable` →
  `publish.publish_gates`): a deferral is a hand-off, never a waiver;
* the declaration rules are the marker family's (#329/#428): a non-zero exit fails whatever
  it printed, a mid-line quotation is relayed text, `unverifiable` wins when both declare;
* `assemble` does NOT lift it into §6 (the one deliberate difference from `unverifiable`)
  while §5 still shows it with its reason — so sign-off can accept without a checkbox
  nobody can act on;
* end to end: the REAL `cli._contribcheck`, run as the gate command over a bundle that has
  `patch.diff` and no `pr-description.md`, produces a `deferred` row whose evidence names
  the publish re-gate — while a bundle whose artifacts ARE drafted still records the
  substantive `pass`/`fail` exactly as before, and publish still selects the row to
  hard-gate the push.

Run from the project root:  PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import io
import shlex
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from pdca_harness import assemble, cli, gates, publish, signoff, state
from pdca_harness.config import Config, LeafConfig

_U = gates.UNVERIFIABLE_MARKER
# Composed from the production constant, so this module never emits the marker at a
# *declaring* position in its own captured output (#428's lesson) and cannot flip the row of
# the gate that runs it. The literal fallback keeps the module IMPORTABLE against an engine
# without the constant, so a red→green verifier that reverts the production hunks measures
# the BEHAVIOUR (a `pass` recorded where `deferred` is owed) rather than collecting an
# ImportError — a crash proves the constant is absent, not that the record lies.
_D = getattr(gates, "DEFERRED_MARKER", "PDCA-DEFERRED:")

# The shipped registration's shape in the fields that decide selection: a BUNDLE-scoped T4
# row, which `publish.publish_gates` re-runs before the push — so it may defer.
_T4 = {"id": "T4-contribution", "tier": "T4", "label": "contribution artifacts",
       "scope": "bundle", "gating": True}
_T4_DEFERS = {**_T4, "cmd": f"echo '{_D} pr-description.md not drafted yet — audited at publish'"}
# Rows NOTHING re-gates: a repo-scoped T4 (at_publish defaults off) and a C4 verifier.
_T4_REPO_DEFERS = {**_T4_DEFERS, "scope": "repo"}
_C4_DEFERS = {"id": "C4-verify", "tier": "C4", "label": "verify", "scope": "bundle",
              "gating": True, "cmd": _T4_DEFERS["cmd"]}
# The marker family's rules, on the row that IS allowed to defer.
_T4_DEFERS_AND_FAILS = {**_T4, "cmd": f"echo '{_D} nothing to lint'; echo 'boom'; exit 1"}
_T4_RELAYS = {**_T4, "cmd": f"echo 'the contract reads: {_D} <reason> while exiting 0'; "
                            "echo 'lint OK'"}
_T4_DEFERS_AND_UNVERIFIABLE = {**_T4, "cmd": f"echo '{_U} no linter on PATH'; "
                                             f"echo '{_D} nothing to lint'"}

# The REAL T4 checker, driven the way the gate runner drives it: a subprocess that reads
# $PDCA_BUNDLE (exported by `_run_one`) and returns `cli._contribcheck`'s own exit code.
# `cfg` is unused on that path — the bundle comes from the environment, as under `pdca
# contribcheck` — so this is the production function, not a re-implementation of it.
_SRC = str(Path(cli.__file__).resolve().parents[1])  # the src/ this suite imported cli from
_CONTRIBCHECK = (
    f"{shlex.quote(sys.executable)} -c "
    + shlex.quote(f"import sys; sys.path.insert(0, {_SRC!r}); "
                  "from types import SimpleNamespace as N; from pdca_harness import cli; "
                  "raise SystemExit(cli._contribcheck(None, N(issue_id=None, no_issue=False)))")
)
_T4_CONTRIBCHECK = {**_T4, "cmd": _CONTRIBCHECK}

_GOOD_PR = ("## Summary\n**User impact:** users saw a crash on save.\n\n"
            "## Root cause\nx.\n\nFixes #266\n")
_GOOD_COMMIT = "Fix the save crash\n\nBody under eighty.\n\nFixes #266\n"


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


class _GateCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = _stub_config(self.tmp)

    def _bundle(self, iid: str) -> Path:
        """A Check-time bundle: a patch, no publish artifacts (they are drafted later)."""
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** deferred\n", encoding="utf-8")
        (d / "patch.diff").write_text("--- a\n+++ b\n", encoding="utf-8")
        return d

    def _gated(self, iid: str, gate: dict) -> tuple[Path, dict]:
        d = self._bundle(iid)
        self.cfg.gates_checks = [gate]
        return d, gates.run_gates(d, self.cfg)

    def _row(self, result: dict, element: str = "T4") -> dict:
        return next(r for r in result["rows"] if r["element"] == element)


class DeferredIsAGateDeclaredNonGatingResult(_GateCase):
    """`_classify` / `_finalize`: the vocabulary member itself."""

    def test_declared_deferral_is_recorded_with_its_reason(self) -> None:
        _d, result = self._gated("DEF", _T4_DEFERS)
        row = self._row(result)
        self.assertEqual(row["result"], "deferred")
        self.assertNotIn(row["result"], ("pass", "unverifiable"))
        self.assertIn("audited at publish", row["path_line"])  # the reason, not an empty cell

    def test_deferred_does_not_count_toward_overall(self) -> None:
        # A gating row, yet neither a green nor a gating red: the gate reached no verdict.
        _d, result = self._gated("OVR", _T4_DEFERS)
        self.assertTrue(self._row(result)["gating"])
        self.assertEqual(result["overall"], "pass")

    def test_the_matrix_shows_deferred_with_its_reason(self) -> None:
        d, result = self._gated("MD", _T4_DEFERS)
        md = (d / "check-gates.md").read_text(encoding="utf-8")
        self.assertIn("| deferred |", md)
        self.assertIn("audited at publish", md)

    # -- the guard: a deferral is a hand-off, never a waiver -------------------------

    def test_a_row_nothing_re_gates_keeps_its_pass(self) -> None:
        """Design point 3: `deferred` is legitimate only where the substantive audit
        actually runs later. A repo-scoped T4 row is not selected by `publish_gates`, so it
        has no later verdict to defer to and keeps today's behaviour."""
        self.assertEqual([c["id"] for c in publish.publish_gates(
            _stub_config(self.tmp))], [])  # sanity: selection is by the row, not the tier
        _d, result = self._gated("NOREGATE", _T4_REPO_DEFERS)
        self.assertEqual(self._row(result)["result"], "pass")

    def test_a_c4_row_cannot_defer_itself_out_of_scrutiny(self) -> None:
        """The verifier nothing re-runs: were the marker honoured here, any gate could
        opt out of its own audit by printing one line."""
        _d, result = self._gated("C4DEF", _C4_DEFERS)
        self.assertEqual(self._row(result, "C4")["result"], "pass")

    # -- the marker family's rules apply unchanged (#329 / #428) ---------------------

    def test_a_non_zero_exit_fails_whatever_it_declared(self) -> None:
        _d, result = self._gated("FAILDEF", _T4_DEFERS_AND_FAILS)
        self.assertEqual(self._row(result)["result"], "fail")
        self.assertEqual(result["overall"], "fail")

    def test_a_relayed_marker_is_not_a_declaration(self) -> None:
        _d, result = self._gated("RELAY", _T4_RELAYS)
        row = self._row(result)
        self.assertEqual(row["result"], "pass")
        self.assertEqual(row["path_line"], "lint OK")

    def test_unverifiable_wins_when_a_gate_declares_both(self) -> None:
        """`unverifiable` stops for a human; `deferred` does not. When a gate says both,
        the channel that escalates must win — never the one that quietly stands down."""
        _d, result = self._gated("BOTH", _T4_DEFERS_AND_UNVERIFIABLE)
        self.assertEqual(self._row(result)["result"], "unverifiable")


class DeferredIsNotLiftedIntoSection6(_GateCase):
    """The one deliberate difference from `unverifiable` — and the point of the change."""

    def _assembled(self, iid: str, gate: dict) -> Path:
        d, _result = self._gated(iid, gate)
        # A clean advisory review, so §6 is fed only by the gates (as in the #46 slice).
        (d / "check-review.md").write_text("All advisory items PASS.\n", encoding="utf-8")
        assemble.assemble_summary(d, self.cfg)
        return d

    def test_no_needs_human_item_and_accept_is_not_blocked(self) -> None:
        d = self._assembled("S6", _T4_DEFERS)
        open_items = signoff.open_needs_human(d / "SUMMARY.md")
        self.assertFalse([it for it in open_items if "deferred" in it or "T4" in it],
                         f"a deferred row must not reach §6: {open_items}")
        # C6 therefore has nothing to block on: accept succeeds with no box to tick.
        accept = SimpleNamespace(issue_id="S6", accept=True, iterate_do=False,
                                 iterate_plan=False, discontinue=False, by="t", delta="")
        self.assertEqual(cli._signoff(self.cfg, accept), 0)
        self.assertEqual(state.state(d), state.COMPLETE)

    def test_the_row_is_still_visible_in_section_5_with_its_reason(self) -> None:
        """Not lifted is not hidden: the human still reads WHAT is owed at publish."""
        d = self._assembled("S5", _T4_DEFERS)
        summary = (d / "SUMMARY.md").read_text(encoding="utf-8")
        self.assertIn("deferred", summary)
        self.assertIn("audited at publish", summary)

    def test_an_unverifiable_row_still_reaches_section_6(self) -> None:
        """The #46 route is untouched for every other class — only the by-design condition
        stops producing a checkbox."""
        d = self._assembled("UV", {**_T4, "cmd": f"echo '{_U} no linter on PATH'"})
        self.assertTrue([it for it in signoff.open_needs_human(d / "SUMMARY.md")
                         if "unverifiable" in it])


class ContribcheckDeclaresTheDeferral(_GateCase):
    """End to end through the REAL T4 checker (`cli._contribcheck`) — the success criterion."""

    def _artifacts(self, d: Path, *, pr: str = _GOOD_PR, commit: str = _GOOD_COMMIT) -> None:
        (d / "pr-description.md").write_text(pr, encoding="utf-8")
        (d / "commit-msg.txt").write_text(commit, encoding="utf-8")

    def test_check_time_row_with_a_patch_and_no_pr_body_is_deferred(self) -> None:
        d, result = self._gated("266", _T4_CONTRIBCHECK)
        self.assertFalse((d / "pr-description.md").exists())  # the fault IS the fixture
        row = self._row(result)
        self.assertEqual(row["result"], "deferred")
        self.assertIn("publish", row["path_line"])  # names where the audit actually runs
        self.assertEqual(result["overall"], "pass")

    def test_drafted_artifacts_still_record_the_substantive_pass(self) -> None:
        d = self._bundle("266p")
        self._artifacts(d)
        self.cfg.gates_checks = [_T4_CONTRIBCHECK]
        self.assertEqual(self._row(gates.run_gates(d, self.cfg))["result"], "pass")

    def test_drafted_artifacts_still_record_the_substantive_fail(self) -> None:
        d = self._bundle("266f")
        self._artifacts(d, pr="## Summary\nno user-impact opener.\n\nFixes #266f\n")
        self.cfg.gates_checks = [_T4_CONTRIBCHECK]
        result = gates.run_gates(d, self.cfg)
        self.assertEqual(self._row(result)["result"], "fail")
        self.assertEqual(result["overall"], "fail")

    def test_the_checker_declares_the_marker_at_the_start_of_a_line(self) -> None:
        """The declaration form itself (a mid-line mention would be relayed text, #428)."""
        d = self._bundle("266d")
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli._contribcheck(self.cfg, SimpleNamespace(issue_id=d.name.removeprefix("issue_"),
                                                             no_issue=False))
        self.assertEqual(rc, 0)  # exit code unchanged: nothing failed
        declared = [ln for ln in out.getvalue().splitlines() if ln.startswith(_D)]
        self.assertEqual(len(declared), 1, out.getvalue())
        self.assertIn("publish", declared[0])

    def test_a_close_bundle_with_no_patch_still_just_passes(self) -> None:
        """Nothing was contributed, so nothing is owed later — not a deferral."""
        d = self._bundle("266c")
        (d / "patch.diff").write_text("", encoding="utf-8")
        self.cfg.gates_checks = [_T4_CONTRIBCHECK]
        self.assertEqual(self._row(gates.run_gates(d, self.cfg))["result"], "pass")

    def test_publish_still_hard_gates_the_row_before_any_push(self) -> None:
        """The substantive verdict is unchanged and still blocks the push: the same row the
        Check matrix deferred is the one publish re-runs (`publish._t4_passes`)."""
        self.cfg.gates_checks = [_T4_CONTRIBCHECK]
        self.assertEqual([c["id"] for c in publish.publish_gates(self.cfg)],
                         ["T4-contribution"])


class ModuleDocNamesEveryResultChangingMarker(unittest.TestCase):
    """The module doc's evidence-marker paragraph is normative (issue #442): #401 made
    `deferred` a second result-changing declaration, so the paragraph's exclusivity claim
    for ``PDCA-UNVERIFIABLE`` ("the one marker that can change a ``result``") became false
    the moment :data:`gates.DEFERRED_MARKER` landed twenty lines below it."""

    # The docstring hard-wraps its sentences; collapse whitespace so the assertions span
    # the wrap points instead of pinning the current line layout.
    _DOC = " ".join((gates.__doc__ or "").split())
    # Marker NAMES as the prose cites them (no trailing colon), composed from the
    # production constants like `_D` above — never a second spelling that can drift.
    _CLAIM = (f"only the ``{_U.removesuffix(':')}``/``{_D.removesuffix(':')}`` "
              "declarations can change a ``result``")

    def test_the_exclusivity_claim_is_gone(self) -> None:
        self.assertNotIn(
            "the one marker", self._DOC,
            "gates.__doc__ still claims a SINGLE result-changing marker — false since "
            "#401 added the deferred declaration")

    def test_the_result_changing_claim_names_both_declarations(self) -> None:
        self.assertIn(
            self._CLAIM, self._DOC,
            "gates.__doc__ must name BOTH result-changing declarations in the "
            "evidence-marker paragraph's claim")


if __name__ == "__main__":
    unittest.main()
