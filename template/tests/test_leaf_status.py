"""An infra-empty leaf artifact is distinguishable from "ran, found nothing" (#278).

When a reviewer / advisory leaf can't produce a verdict, `leaves` writes a placeholder. The
placeholder already classified the failure (transient infra vs substantive, #138) — but only
in PROSE, so §6 rendered one generic "did not produce findings; re-run or adjudicate" row for
both, and the operator had to hand-annotate "this empty verdict was infra, not substance".
An empty adversarial artifact then reads exactly like a clean adversarial pass.

The placeholder now carries a machine-readable `<!-- pdca:leaf-status … -->` marker, and
assemble labels the §6 row from it. An empty artifact is never builder-fixable (there is no
finding to fix), so it is forced HUMAN and can never be auto-iterated (#264).

Offline: stub leaves, real gates, no model. Run from the project root:
    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from pdca_harness import assemble, gates, leaves, signoff
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


class LeafStatusMarker(unittest.TestCase):
    """The marker itself — written by leaves, parsed by assemble."""

    def test_transient_failure_is_infra_empty(self) -> None:
        block = leaves._unavailable_classification(leaves._FAIL_TRANSIENT, None)
        self.assertEqual(assemble.leaf_status(block), assemble.LEAF_STATUS_INFRA)

    def test_substantive_failure_is_human_empty(self) -> None:
        block = leaves._unavailable_classification(leaves._FAIL_SUBSTANTIVE, None)
        self.assertEqual(assemble.leaf_status(block), assemble.LEAF_STATUS_HUMAN)

    def test_a_real_artifact_carries_no_status(self) -> None:
        # A leaf that actually reviewed the diff — no marker, so no relabelling.
        self.assertEqual(assemble.leaf_status("# Review\n\n- NEEDS-HUMAN — scope creep\n"), "")

    def test_a_startup_failure_is_infra_not_substance(self) -> None:
        # PR #285 review (codex). A missing binary raises FileNotFoundError from the spawn —
        # never a LeafError — so `getattr(exc, "transient", False)` was False and the canonical
        # `[Errno 2] 'codex'` failure was reported as "the leaf ran but did not yield a usable
        # verdict"… for a leaf that never started. It is the exact case #278 exists to catch.
        exc = FileNotFoundError(2, "No such file or directory", "codex")
        self.assertEqual(leaves._failure_class(exc), leaves._FAIL_STARTUP)
        block = leaves._unavailable_classification(leaves._FAIL_STARTUP, None)
        self.assertEqual(assemble.leaf_status(block), assemble.LEAF_STATUS_STARTUP)

    def test_startup_prose_says_a_plain_re_run_will_not_help(self) -> None:
        # Infra, but NOT the same action as a transient blip: the binary is still absent.
        block = leaves._unavailable_classification(leaves._FAIL_STARTUP, None)
        self.assertIn("never ran", block)
        self.assertIn("re-run will fail the same way", block)
        self.assertNotIn("safe to re-run", block)

    def test_failure_class_maps_every_shape(self) -> None:
        self.assertEqual(
            leaves._failure_class(leaves.LeafError(1, ["x"], output="", produced=False)),
            leaves._FAIL_TRANSIENT)                       # ran, no output → retryable blip
        self.assertEqual(
            leaves._failure_class(leaves.LeafError(1, ["x"], output="verdict?", produced=True)),
            leaves._FAIL_SUBSTANTIVE)                     # ran, produced output → human
        self.assertEqual(leaves._failure_class(PermissionError(13, "denied", "codex")),
                         leaves._FAIL_STARTUP)            # could not launch → infra
        self.assertEqual(leaves._failure_class(None), leaves._FAIL_SUBSTANTIVE)

    def test_prose_classification_is_preserved(self) -> None:
        # The #138 prose stays — the marker is additive, not a replacement.
        self.assertIn("safe to re-run", leaves._unavailable_classification(leaves._FAIL_TRANSIENT, None))
        self.assertIn("needs a human", leaves._unavailable_classification(leaves._FAIL_SUBSTANTIVE, None))


class Section6Labelling(unittest.TestCase):
    """The §6 row now says WHY the artifact is empty."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)
        self.cfg.gates_checks = [_PASS_GATE]

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundle(self, iid: str) -> Path:
        """Passing gates + a clean review ⇒ §6 is fed only by the advisory placeholder."""
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** x\n", encoding="utf-8")
        (d / "patch.diff").write_text("--- a\n+++ b\n", encoding="utf-8")
        (d / "check-review.md").write_text("All advisory items PASS.\n", encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            gates.run_gates(d, self.cfg)
        return d

    def _advisory_items(self, d: Path) -> list[assemble.NeedsHumanItem]:
        with redirect_stdout(io.StringIO()):
            assemble.assemble_summary(d, self.cfg)
        return [i for i in assemble.collect_needs_human(d, self.cfg) if "leaf" in i.text]

    def _fail_advisory(self, d: Path, *, failure: str) -> None:
        with redirect_stderr(io.StringIO()):
            leaves._advisory_unavailable(
                d, "adversary", "leaf failed: [Errno 2] 'codex'", failure=failure)

    def test_infra_failure_is_labelled_and_re_runnable(self) -> None:
        d = self._bundle("INFRA")
        self._fail_advisory(d, failure=leaves._FAIL_TRANSIENT)
        items = self._advisory_items(d)
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].text.startswith("leaf did not run (transient infra — safe to re-run)"),
                        items[0].text)
        # …and it reaches §6, so the empty adversarial pass can't be accepted as clean.
        self.assertTrue(any("infra" in it for it in
                            signoff.open_needs_human(d / "SUMMARY.md")))

    def test_substantive_failure_is_labelled_for_the_human(self) -> None:
        d = self._bundle("SUBST")
        self._fail_advisory(d, failure=leaves._FAIL_SUBSTANTIVE)
        items = self._advisory_items(d)
        self.assertEqual(len(items), 1)
        self.assertTrue(
            items[0].text.startswith("leaf produced no usable verdict (needs a human)"),
            items[0].text)

    def test_the_two_are_distinguishable(self) -> None:
        # The whole point: infra and substance must not render the same §6 row.
        a, b = self._bundle("A"), self._bundle("B")
        self._fail_advisory(a, failure=leaves._FAIL_TRANSIENT)
        self._fail_advisory(b, failure=leaves._FAIL_SUBSTANTIVE)
        self.assertNotEqual(self._advisory_items(a)[0].text,
                            self._advisory_items(b)[0].text)

    def test_an_empty_artifact_is_never_auto_iterated(self) -> None:
        # #264: an infra-empty has no finding a rebuild could fix — it must stay HUMAN, or
        # auto-iterate would spin rebuilding against a leaf that never ran.
        for failure in (leaves._FAIL_TRANSIENT, leaves._FAIL_STARTUP, leaves._FAIL_SUBSTANTIVE):
            with self.subTest(failure=failure):
                d = self._bundle(f"K{failure}")
                self._fail_advisory(d, failure=failure)
                items = self._advisory_items(d)
                self.assertEqual([i.kind for i in items], [assemble.HUMAN])

    def test_a_missing_leaf_binary_lands_in_section6_as_infra(self) -> None:
        """The end-to-end shape of the PR #285 review finding, driven through the real leaf
        invocation rather than a hand-passed class. The spawn raises FileNotFoundError before
        any LeafError exists, so the old `getattr(err, "transient", False)` read False and §6
        told the operator to adjudicate an *empty verdict* — for a leaf that never started.
        This is the `[Errno 2] 'codex'` case the issue itself cites."""
        d = self._bundle("NOBIN")
        leaf = LeafConfig(mode="command", family="codex", argv=["codex"])
        with mock.patch.object(
                leaves.progress, "run_with_heartbeat",
                side_effect=FileNotFoundError(2, "No such file or directory", "codex")), \
                redirect_stderr(io.StringIO()):
            leaves._run_advisory_sandboxed(
                d, self.cfg, leaf, {"id": "adversary", "role": "refute"}, "adversary")

        art = (d / "check-advisory-adversary.md").read_text(encoding="utf-8")
        self.assertEqual(assemble.leaf_status(art), assemble.LEAF_STATUS_STARTUP)
        self.assertIn("never ran", art)

        items = self._advisory_items(d)
        self.assertTrue(items[0].text.startswith("leaf did not run ("), items[0].text)
        # …and §6 must NOT tell the operator a plain re-run is safe — the binary is still gone.
        self.assertIn("could not be launched", items[0].text)
        self.assertNotIn("safe to re-run", items[0].text)
        self.assertEqual(items[0].kind, assemble.HUMAN)   # still the human's, never auto-iterated

    def test_an_impl_tagged_finding_in_a_placeholder_cannot_smuggle_in_impl(self) -> None:
        # Defence in depth: even if a placeholder's bullet carried an `[impl]` tag, an empty
        # artifact stays HUMAN — the status marker wins over the finding tag.
        d = self._bundle("SMUG")
        (d / "check-advisory-adversary.md").write_text(
            f"<!-- pdca:leaf-status {assemble.LEAF_STATUS_INFRA} -->\n\n"
            "- NEEDS-HUMAN [impl] — leaf died before it could review\n", encoding="utf-8")
        items = self._advisory_items(d)
        self.assertEqual([i.kind for i in items], [assemble.HUMAN])

    def test_a_real_advisory_finding_is_untouched(self) -> None:
        # No marker ⇒ no relabelling, and an [impl] tag still classifies IMPL as before.
        d = self._bundle("REAL")
        (d / "check-advisory-adversary.md").write_text(
            "- NEEDS-HUMAN [impl] — off-by-one at src/x.py:12\n", encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            assemble.assemble_summary(d, self.cfg)
        items = [i for i in assemble.collect_needs_human(d, self.cfg)
                 if "off-by-one" in i.text]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, assemble.IMPL)
        self.assertNotIn("leaf did not run", items[0].text)

    def test_a_failed_main_reviewer_is_labelled_too(self) -> None:
        d = self._bundle("REV")
        with redirect_stderr(io.StringIO()):
            leaves._review_unavailable(d, "connection dropped",
                                       failure=leaves._FAIL_TRANSIENT)
        with redirect_stdout(io.StringIO()):
            assemble.assemble_summary(d, self.cfg)
        items = [i for i in assemble.collect_needs_human(d, self.cfg)
                 if "leaf did not run" in i.text]
        self.assertTrue(items, "a transient reviewer failure must be labelled infra in §6")
        self.assertEqual(items[0].kind, assemble.HUMAN)


if __name__ == "__main__":
    unittest.main()
