"""Which T4 rows publish runs — `at_publish`, defaulted by scope (issue #339).

`_t4_passes` picked its gates on the tier string alone, so registering ANY T4-tier check
for Check silently made publish re-run it before every push. In one instance that check
was a batched 3x model review of the whole `patch.diff`: ~6 minutes, re-paid on every
publish attempt and every retry — and the push and `gh pr create` sit downstream, so
retries happen.

Duplicated cost is the smaller half. **Publish re-sampled a nondeterministic reviewer
after the human signed off**: a bundle green at Check could be refused at publish over a
finding that did not exist when §9 was recorded. Observed on one real bundle in both
directions — two findings each seen by 1 of 3 passes, and a re-run of the identical
command minutes later reporting none.

The slot exists for checks whose subject is the contribution artifacts publish just
drafted (`commit-msg.txt` / `pr-description.md`), which do not exist at Check time.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from pdca_harness import publish
from pdca_harness.config import Config, LeafConfig

# The row this template ships, verbatim in the fields that decide selection.
_SHIPPED_CONTRIBUTION = {"id": "T4-contribution", "tier": "T4", "gating": True,
                         "scope": "bundle", "cmd": "pdca contribcheck"}
# The shape that motivated the issue: a whole-diff model review registered at T4.
_WHOLE_DIFF_REVIEW = {"id": "T4-review", "tier": "T4", "scope": "repo",
                      "cmd": "scripts/review-branch"}


def _cfg(root: Path, checks: list[dict]) -> Config:
    cfg = Config(
        root=root, bundle_root=root / "results", process_dir=root / "process",
        templates_dir=root / "templates", default_branch="main",
        tracker_system="github", tracker_url="", issue_id_example="#1",
        builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
    )
    cfg.gates_checks = checks
    return cfg


class PublishGateSelection(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _selected(self, checks: list[dict]) -> list[str]:
        return [c["id"] for c in publish.publish_gates(_cfg(self.tmp, checks))]

    # -- the defaults ----------------------------------------------------------------

    def test_shipped_contribution_row_still_runs_at_publish(self) -> None:
        """Back-compat that actually matters: this must not change for any instance, on a
        fresh render or a `copier update`. It is bundle-scoped, so it defaults on."""
        self.assertEqual(self._selected([_SHIPPED_CONTRIBUTION]), ["T4-contribution"])

    def test_repo_scoped_row_no_longer_runs_at_publish(self) -> None:
        """The whole-diff review Check already paid for. A repo-scoped check cannot be
        about artifacts publish just drafted."""
        self.assertEqual(self._selected([_WHOLE_DIFF_REVIEW]), [])

    def test_row_with_no_scope_defaults_off(self) -> None:
        """`_applies` treats a missing scope as "repo" (`gates.py:268`), so selection must
        read it the same way — anything else makes the two disagree about one row."""
        self.assertEqual(self._selected([{"id": "T4-bare", "tier": "T4", "cmd": "true"}]), [])

    # -- the explicit flag wins, both ways -------------------------------------------

    def test_explicit_true_forces_a_repo_scoped_row_on(self) -> None:
        row = {**_WHOLE_DIFF_REVIEW, "at_publish": True}
        self.assertEqual(self._selected([row]), ["T4-review"])

    def test_explicit_false_forces_a_bundle_scoped_row_off(self) -> None:
        row = {**_SHIPPED_CONTRIBUTION, "at_publish": False}
        self.assertEqual(self._selected([row]), [])

    # -- scoping stays tier-bounded ---------------------------------------------------

    def test_non_t4_rows_are_never_selected(self) -> None:
        """A bundle-scoped C4 row is Check's, whatever `at_publish` says."""
        rows = [{"id": "C4", "tier": "C4", "scope": "bundle", "cmd": "true"},
                {"id": "C4-forced", "tier": "C4", "scope": "bundle",
                 "at_publish": True, "cmd": "true"}]
        self.assertEqual(self._selected(rows), [])

    def test_mixed_registry_selects_only_the_contribution_row(self) -> None:
        """The instance configuration from the issue, end to end."""
        rows = [_SHIPPED_CONTRIBUTION, _WHOLE_DIFF_REVIEW,
                {"id": "C4-ci", "tier": "C4", "scope": "repo", "cmd": "true"}]
        self.assertEqual(self._selected(rows), ["T4-contribution"])

    # -- the reason a flat `True` default was rejected --------------------------------

    def test_a_flat_true_default_would_reinstate_the_defect(self) -> None:
        """Guards the DEFAULT, not just the mechanism.

        Publish runs from `cfg.root` with an env of only `PDCA_BUNDLE`; the Check runner
        additionally exports `PDCA_WORKTREE`. So a legacy repo-scoped row referencing
        `$PDCA_WORKTREE` passes at Check and would falsely BLOCK the push if it were still
        selected here. Asserting the negative keeps a later "simplify the default" from
        quietly restoring it.
        """
        legacy = {"id": "T4-legacy", "tier": "T4", "scope": "repo",
                  "cmd": 'test -n "$PDCA_WORKTREE"'}
        self.assertEqual(self._selected([legacy]), [],
                         "a repo-scoped legacy row is selected — the flat-True default")


if __name__ == "__main__":
    unittest.main()
