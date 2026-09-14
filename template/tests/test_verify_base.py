"""A bundle-scoped gate is told exactly one base — $PDCA_BASE / $PDCA_VERIFY_BASE /
$PDCA_BRIEF_BASE (#273, #387).

Under the wave model, a dependent bundle's Do worktree is cut off the run-scoped integration
branch (prior waves' folded patches). A per-fix verifier that resets to a base must reset to
THAT branch, not the brief's origin base — else the dependent false-fails "patch does not
apply" or measures red→green against a tree lacking its prereq. The driver exports the folded
base as `PDCA_VERIFY_BASE=origin/<integration-branch>` to bundle-scoped gate commands, read
from the per-bundle `stack-base` marker the wave driver stamped before Check. A wave-0 bundle
has no marker, so the var is absent and behaviour is unchanged.

The ordinary wave-0 bundle then gets the LAST rung of the published ladder (#387):
`PDCA_BRIEF_BASE=<base_remote>/<brief base branch>`, resolved by the driver with the same
anchored parser publish uses — so a gate script never parses `brief.md` itself and the two
implementations of that parse (#235, #262) cannot disagree.

Real gate commands, no model/network. Run from the project root:
    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pdca_harness import gates, publish
from pdca_harness.config import Config, LeafConfig

#: The three exports this module is about. It reads them back out of a real gate
#: SUBPROCESS, whose environment is `{**os.environ, **exports}` (`gates.py:782`) — so one
#: of them already present in the ambient environment is read back as though the driver
#: had set it, and every assertion here about which base is set (and about only ONE being
#: set) fails with nothing wrong in the code under test. Not hypothetical: this harness
#: drives itself, and the outer run exports `PDCA_VERIFY_BASE` (the folded base of a
#: wave-dependent bundle) to the gate command that runs this very suite, which turned 11
#: of these tests red in a frozen gate record. A module that asserts what the driver
#: exports has to own the baseline it measures against.
_BASE_VARS = ("PDCA_BASE", "PDCA_VERIFY_BASE", "PDCA_BRIEF_BASE")

# A bundle-scoped gate whose cmd records ALL THREE exported bases into the bundle dir, so the
# test reads back exactly what the driver set (`UNSET` when a var is absent). All three,
# because the load-bearing property is that exactly ONE of them is ever set (PR #282 review).
_ECHO_BASES = {
    "id": "C4", "tier": "C4", "label": "record bases", "scope": "bundle", "gating": True,
    "cmd": ('printf "%s\\n%s\\n%s\\n" "${PDCA_BASE-UNSET}" "${PDCA_VERIFY_BASE-UNSET}" '
            '"${PDCA_BRIEF_BASE-UNSET}" > "$PDCA_BUNDLE/bases.txt"'),
}


def _echo_row(id_: str, *, tier: str, scope: str, filename: str,
              verifies_base: bool | None = None) -> dict:
    """A row shaped like ``_ECHO_BASES`` but writing to its OWN file (``filename``) and, by
    default, tagged a tier OTHER than ``"C4"`` — a non-verifier row (issue #474). Pass
    ``verifies_base`` to declare the key explicitly in either direction."""
    row = {
        "id": id_, "tier": tier, "label": f"record bases ({id_})", "scope": scope,
        "gating": False,
        "cmd": ('printf "%s\\n%s\\n%s\\n" "${PDCA_BASE-UNSET}" "${PDCA_VERIFY_BASE-UNSET}" '
                f'"${{PDCA_BRIEF_BASE-UNSET}}" > "$PDCA_BUNDLE/{filename}"'),
    }
    if verifies_base is not None:
        row["verifies_base"] = verifies_base
    return row


# The C4 skeleton every rendered instance fills in: it publishes the ladder gate scripts
# follow, so it is where an instance learns whether to read the export or parse the brief.
_TEMPLATE_ROOT = Path(__file__).resolve().parents[1]
_SKELETON = _TEMPLATE_ROOT / "engine" / "scripts" / "run-verify.sh"
# `run-verify.sh` carries no `.jinja` suffix, so its own name never signals which posture
# it is in — every instance is instructed to overwrite it with its own gate. Read the
# posture off the project root's `pdca.toml(.jinja)` instead, same signal `test_families.py`
# and `test_remote_control_docs.py` use (issue #507).
_TOML = next((_TEMPLATE_ROOT / n for n in ("pdca.toml.jinja", "pdca.toml")
             if (_TEMPLATE_ROOT / n).is_file()), None)
RENDERED = _TOML is not None and _TOML.name == "pdca.toml"


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
        base_remote="origin",
    )


class VerifyBaseExport(unittest.TestCase):
    #: Overridable by `C4BaseLadderPostures` below to drive
    #: `test_the_c4_skeleton_names_the_export_as_the_last_rung` against synthetic
    #: text/posture in a temp dir, without touching the real checkout and without a
    #: subprocess (issue #507's fork-storm constraint). Every other test method in this
    #: class ignores these — they bind every instance and are untouched by issue #507.
    SKELETON_TEXT: str | None = None
    RENDERED: bool = RENDERED

    def setUp(self) -> None:
        # Hermetic baseline (see _BASE_VARS): snapshot the environment, drop the three vars
        # under test for the duration of the test, restore it afterwards. Only these three
        # — the gate subprocess still needs the rest of the environment (PATH, HOME) to run
        # at all, so this narrows the ambient env, it never replaces it.
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for var in _BASE_VARS:
            os.environ.pop(var, None)
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)
        self.cfg.gates_checks = [_ECHO_BASES]

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundle(self, iid: str) -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** v\n", encoding="utf-8")
        (d / "patch.diff").write_text("--- a\n+++ b\n", encoding="utf-8")
        return d

    def _recorded_bases(self, d: Path) -> dict[str, str]:
        """All three bases as the gate command actually saw them."""
        gates.run_gates(d, self.cfg)
        base, verify_base, brief_base = (
            d / "bases.txt").read_text(encoding="utf-8").splitlines()[:3]
        return {"PDCA_BASE": base, "PDCA_VERIFY_BASE": verify_base,
                "PDCA_BRIEF_BASE": brief_base}

    def _recorded_base(self, d: Path) -> str:
        return self._recorded_bases(d)["PDCA_VERIFY_BASE"]

    def test_wave_dependent_gets_the_folded_base(self) -> None:
        d = self._bundle("DEP")
        publish.write_stack_base(d, "pdca-integration/main")   # the wave driver stamps this
        self.assertEqual(self._recorded_base(d), "origin/pdca-integration/main")

    def test_flattened_base_is_carried_verbatim(self) -> None:
        # The marker already holds the flattened branch name; the gate export just prefixes it.
        d = self._bundle("DEP2")
        publish.write_stack_base(d, "pdca-integration/maintenance-sgramps60")
        self.assertEqual(self._recorded_base(d),
                         "origin/pdca-integration/maintenance-sgramps60")

    def test_wave0_bundle_has_no_verify_base(self) -> None:
        # No stack-base marker → the var is unset → today's behaviour, unchanged.
        d = self._bundle("W0")
        self.assertFalse((d / publish.STACK_BASE_FILE).exists())
        self.assertEqual(self._recorded_base(d), "UNSET")

    def test_cleared_marker_reverts_to_no_verify_base(self) -> None:
        # A stale marker cleared by the driver (#187) → back to unset.
        d = self._bundle("CLR")
        publish.write_stack_base(d, "pdca-integration/main")
        publish.clear_stack_base(d)
        self.assertEqual(self._recorded_base(d), "UNSET")

    def test_onto_branch_wins_over_the_wave_base(self) -> None:
        """PR #282 review (codex). A bundle can carry BOTH an `Onto branch` and a wave
        stack-base marker. `publish.publish` takes the Onto path and returns BEFORE it ever
        reads the stack-base, so the fix is committed to the Onto branch. Exporting the wave
        base too would send the verifier to the integration branch while publish commits
        elsewhere — the test base diverging from the deploy base, which is exactly what #54's
        PDCA_BASE exists to prevent. The two exports are mutually exclusive; Onto wins."""
        d = self._bundle("ONTO")
        (d / "brief.md").write_text(
            "- **Slug:** v\n- **Onto branch:** origin/feature/x\n", encoding="utf-8")
        publish.write_stack_base(d, "pdca-integration/main")   # the wave driver stamps it too
        bases = self._recorded_bases(d)
        self.assertEqual(bases["PDCA_BASE"], "origin/feature/x")   # where publish commits
        self.assertEqual(bases["PDCA_VERIFY_BASE"], "UNSET")       # …and where the gate tests

    def test_wave_base_still_exported_without_an_onto_branch(self) -> None:
        # The ordinary wave dependent — no Onto — is unaffected by the precedence rule.
        d = self._bundle("NOONTO")
        publish.write_stack_base(d, "pdca-integration/main")
        bases = self._recorded_bases(d)
        self.assertEqual(bases["PDCA_BASE"], "UNSET")
        self.assertEqual(bases["PDCA_VERIFY_BASE"], "origin/pdca-integration/main")

    def test_onto_alone_is_unchanged(self) -> None:
        # Stack mode (#54) with no wave marker — behaviour predating #273.
        d = self._bundle("ONTOONLY")
        (d / "brief.md").write_text(
            "- **Slug:** v\n- **Onto branch:** origin/feature/x\n", encoding="utf-8")
        bases = self._recorded_bases(d)
        self.assertEqual(bases["PDCA_BASE"], "origin/feature/x")
        self.assertEqual(bases["PDCA_VERIFY_BASE"], "UNSET")

    def test_the_two_bases_are_never_both_set(self) -> None:
        # The invariant, stated directly: a gate is told exactly one base, or none.
        for name, onto, marker in (("A", True, True), ("B", True, False),
                                   ("C", False, True), ("D", False, False)):
            with self.subTest(onto=onto, marker=marker):
                d = self._bundle(f"INV{name}")
                if onto:
                    (d / "brief.md").write_text(
                        "- **Slug:** v\n- **Onto branch:** origin/feature/x\n",
                        encoding="utf-8")
                if marker:
                    publish.write_stack_base(d, "pdca-integration/main")
                bases = self._recorded_bases(d)
                set_count = sum(1 for v in bases.values() if v != "UNSET")
                self.assertLessEqual(set_count, 1,
                                     f"the test base and the deploy base can diverge: {bases}")

    def test_public_accessor_matches_the_marker(self) -> None:
        d = self._bundle("ACC")
        self.assertEqual(publish.read_stack_base(d), "")
        publish.write_stack_base(d, "pdca-integration/main")
        self.assertEqual(publish.read_stack_base(d), "pdca-integration/main")

    # ------------------------------------------------------------------ issue #387
    # The ladder's LAST rung: the brief's own base, resolved by the driver and exported
    # as $PDCA_BRIEF_BASE, so no gate script ever parses `brief.md` for it.

    def _brief(self, d: Path, *lines: str) -> None:
        (d / "brief.md").write_text("\n".join(("- **Slug:** v", *lines)) + "\n",
                                    encoding="utf-8")

    def test_brief_base_is_exported_for_an_ordinary_bundle(self) -> None:
        # No Onto, no wave marker — the case the driver used to leave with NO base at all,
        # forcing the gate script to parse the brief itself.
        d = self._bundle("BB")
        self._brief(d, "- **Repo + branch target:** owner/repo @ main")
        bases = self._recorded_bases(d)
        self.assertEqual(bases["PDCA_BRIEF_BASE"], "origin/main")
        self.assertEqual(bases["PDCA_BASE"], "UNSET")
        self.assertEqual(bases["PDCA_VERIFY_BASE"], "UNSET")

    def test_brief_base_ignores_a_parenthetical_backticked_aside(self) -> None:
        """The anchored rule (#235/#262): a backtick span counts only when it STARTS the
        field, so this brief's base is `main` — the aside names a DIFFERENT branch. The
        pre-#235 unanchored rule (which every bash re-derivation carries) yields
        `feat/x-slice`, and publish would then open the PR against a base the gate never
        tested."""
        d = self._bundle("PAREN")
        self._brief(d, "- **Repo + branch target:** getwyrd/wyrd @ main "
                       "(feature branch `feat/x-slice`)")
        self.assertEqual(self._recorded_bases(d)["PDCA_BRIEF_BASE"], "origin/main")

    def test_brief_base_takes_a_fully_backticked_ref(self) -> None:
        d = self._bundle("TICK")
        self._brief(d, "- **Repo + branch target:** owner/repo @ `feat/x`")
        self.assertEqual(self._recorded_bases(d)["PDCA_BRIEF_BASE"], "origin/feat/x")

    def test_brief_without_a_target_field_falls_back_to_the_default_branch(self) -> None:
        d = self._bundle("NOTGT")                 # brief.md carries only a Slug
        self.cfg.default_branch = "trunk"         # …so the project default decides
        self.assertEqual(self._recorded_bases(d)["PDCA_BRIEF_BASE"], "origin/trunk")

    def test_brief_base_is_a_remote_tracking_ref_on_the_configured_remote(self) -> None:
        """Same shape as the other two rungs, on the remote publish checks out from (the
        fork model, `[publisher] base_remote = "upstream"`). Fully qualified is the point:
        a script composing `origin/$PDCA_BRIEF_BASE` over a bare branch is how the doubled
        `origin/origin/main` in the report arose."""
        d = self._bundle("FORK")
        self.cfg.base_remote = "upstream"
        self._brief(d, "- **Repo + branch target:** owner/repo @ main")
        exported = self._recorded_bases(d)["PDCA_BRIEF_BASE"]
        self.assertEqual(exported, "upstream/main")
        self.assertEqual(exported.count("/"), 1)

    def test_brief_base_agrees_with_the_base_publish_resolves(self) -> None:
        """The invariant, stated directly: ONE parse. Whatever the field's style, the base
        the gate is told and the base publish commits to are the same string — they come
        from the same accessor, not from two implementations that drifted (#235 → #262)."""
        for name, target in (("A", "getwyrd/wyrd @ main (feature branch `feat/x-slice`)"),
                             ("B", "owner/repo @ `feat/x`"),
                             ("C", "owner/repo @ release/2.1 — the maintenance line"),
                             ("D", "owner/repo @ main.")):
            with self.subTest(target=target):
                d = self._bundle(f"AGREE{name}")
                self._brief(d, f"- **Repo + branch target:** {target}")
                _repo, base, _slug = publish._resolve_target(d)   # what publish will use
                self.assertEqual(self._recorded_bases(d)["PDCA_BRIEF_BASE"],
                                 f"{self.cfg.base_remote}/{base}")

    def test_an_onto_branch_suppresses_the_brief_base(self) -> None:
        # Rung 1 wins over rung 3: publish commits to the Onto branch, so that is the base
        # the gate must test against — exporting the brief's base too would diverge them.
        d = self._bundle("ONTOBB")
        self._brief(d, "- **Onto branch:** origin/feature/x",
                    "- **Repo + branch target:** owner/repo @ main")
        bases = self._recorded_bases(d)
        self.assertEqual(bases["PDCA_BASE"], "origin/feature/x")
        self.assertEqual(bases["PDCA_BRIEF_BASE"], "UNSET")

    def test_the_wave_base_suppresses_the_brief_base(self) -> None:
        # Rung 2 wins over rung 3: a wave>0 dependent verifies against the folded branch.
        d = self._bundle("WAVEBB")
        self._brief(d, "- **Repo + branch target:** owner/repo @ main")
        publish.write_stack_base(d, "pdca-integration/main")
        bases = self._recorded_bases(d)
        self.assertEqual(bases["PDCA_VERIFY_BASE"], "origin/pdca-integration/main")
        self.assertEqual(bases["PDCA_BRIEF_BASE"], "UNSET")

    def test_exactly_one_base_is_exported_for_every_bundle(self) -> None:
        """The ladder is total AND mutually exclusive: every bundle-scoped gate invocation
        gets one base — never two (the test base would diverge from the deploy base) and
        never none (the gate would have to parse `brief.md` to find one)."""
        for name, onto, marker in (("A", True, True), ("B", True, False),
                                   ("C", False, True), ("D", False, False)):
            with self.subTest(onto=onto, marker=marker):
                d = self._bundle(f"ONE{name}")
                lines = ["- **Repo + branch target:** owner/repo @ main"]
                if onto:
                    lines.insert(0, "- **Onto branch:** origin/feature/x")
                self._brief(d, *lines)
                if marker:
                    publish.write_stack_base(d, "pdca-integration/main")
                bases = self._recorded_bases(d)
                set_bases = {k: v for k, v in bases.items() if v != "UNSET"}
                self.assertEqual(len(set_bases), 1, f"not exactly one base: {bases}")

    # ------------------------------------------------------------------ issue #474
    # The ladder is exported to the per-fix VERIFIER row only — no other configured row,
    # bundle-scoped or repo-scoped, may observe it (the invariant this slice restores).

    def test_only_the_verifier_row_receives_the_ladder(self) -> None:
        """Falsifiability case (issue #474): add a second row to the config — one
        repo-scoped, one bundle-scoped — neither tagged the verifier tier ``"C4"`` nor
        opted in with ``verifies_base``. Both must see NONE of the three vars, even
        though the ACTUAL verifier row (``_ECHO_BASES``, tier ``"C4"``) in the very same
        run receives the ladder's resolved value. Before the fix every row here recorded
        the bundle's resolved base — this is the red the brief's Falsifiability names."""
        d = self._bundle("MULTI")
        self._brief(d, "- **Repo + branch target:** owner/repo @ main")
        repo_row = _echo_row("T3-repo", tier="T3", scope="repo", filename="repo_bases.txt")
        bundle_row = _echo_row("T4-other", tier="T4", scope="bundle",
                               filename="bundle_bases.txt")
        self.cfg.gates_checks = [_ECHO_BASES, repo_row, bundle_row]
        gates.run_gates(d, self.cfg)
        verifier = (d / "bases.txt").read_text(encoding="utf-8").splitlines()[:3]
        repo_bases = (d / "repo_bases.txt").read_text(encoding="utf-8").splitlines()[:3]
        bundle_bases = (d / "bundle_bases.txt").read_text(encoding="utf-8").splitlines()[:3]
        self.assertEqual(verifier, ["UNSET", "UNSET", "origin/main"])
        self.assertEqual(repo_bases, ["UNSET", "UNSET", "UNSET"], repo_bases)
        self.assertEqual(bundle_bases, ["UNSET", "UNSET", "UNSET"], bundle_bases)

    def test_the_unconditional_brief_base_rung_also_stays_off_non_verifier_rows(self) -> None:
        """The brief calls out rung 3 (``PDCA_BRIEF_BASE``) by name: it is exported on
        EVERY ordinary wave-0 cycle (no ``Onto branch``, no stack-base marker), so a fix
        that only suppressed the stacked-bundle export would guard the one symptom that
        happened to be observed and leave this rung leaking on every ordinary cycle."""
        d = self._bundle("ORD")
        self._brief(d, "- **Repo + branch target:** owner/repo @ main")
        self.assertFalse((d / publish.STACK_BASE_FILE).exists())  # wave-0, no Onto
        bundle_row = _echo_row("T5-other", tier="T5", scope="bundle", filename="ord.txt")
        self.cfg.gates_checks = [_ECHO_BASES, bundle_row]
        gates.run_gates(d, self.cfg)
        non_verifier = (d / "ord.txt").read_text(encoding="utf-8").splitlines()[:3]
        self.assertEqual(non_verifier, ["UNSET", "UNSET", "UNSET"], non_verifier)

    def test_a_predating_c4_row_keeps_its_base_with_no_config_edit(self) -> None:
        """Compatibility rule (issue #474 iii): a rendered instance's C4 row that predates
        this change carries no ``verifies_base`` key — exactly ``_ECHO_BASES`` above,
        which never sets one. It must not silently lose the base: the default falls
        through to ``tier == "C4"`` and keeps exporting to it, so an un-migrated instance
        needs zero config changes. A silent loss here would be worse than the leak this
        slice fixes — the test base and the deploy base diverging (#54/#273/#387)."""
        d = self._bundle("PREDATE")
        self._brief(d, "- **Repo + branch target:** owner/repo @ main")
        self.assertNotIn("verifies_base", _ECHO_BASES)
        self.assertEqual(self._recorded_bases(d)["PDCA_BRIEF_BASE"], "origin/main")

    def test_an_explicitly_declared_non_c4_verifier_still_receives_the_base(self) -> None:
        """The declaration is explicit and overrides tier in EITHER direction: a row NOT
        tagged ``tier = "C4"`` but marked ``verifies_base = true`` is still the verifier
        (issue #474) — the shape mirrors ``at_publish``'s own explicit override
        (`src/pdca_harness/publish.py:767`)."""
        d = self._bundle("EXPLICIT")
        self._brief(d, "- **Repo + branch target:** owner/repo @ main")
        row = _echo_row("custom-verifier", tier="T9", scope="bundle",
                        filename="explicit.txt", verifies_base=True)
        self.cfg.gates_checks = [row]
        gates.run_gates(d, self.cfg)
        recorded = (d / "explicit.txt").read_text(encoding="utf-8").splitlines()[:3]
        self.assertEqual(recorded, ["UNSET", "UNSET", "origin/main"], recorded)

    def test_a_c4_row_can_opt_out_explicitly(self) -> None:
        """The override also runs the other way: ``verifies_base = false`` on a
        ``tier = "C4"`` row suppresses the export even though the tier default would
        otherwise grant it — the same both-directions shape as ``at_publish``."""
        d = self._bundle("OPTOUT")
        self._brief(d, "- **Repo + branch target:** owner/repo @ main")
        row = dict(_ECHO_BASES, verifies_base=False)
        self.cfg.gates_checks = [row]
        gates.run_gates(d, self.cfg)
        recorded = (d / "bases.txt").read_text(encoding="utf-8").splitlines()[:3]
        self.assertEqual(recorded, ["UNSET", "UNSET", "UNSET"], recorded)

    def test_the_c4_skeleton_names_the_export_as_the_last_rung(self) -> None:
        """The guidance every rendered instance fills in must terminate the ladder in the
        export, not in "parse the brief yourself" — that instruction is what made each
        instance re-derive the anchored parse in bash.

        Binds the TEMPLATE CHECKOUT ONLY (issue #507): `run-verify.sh` is the one file
        every instance is *told* to replace (`engine/scripts/run-verify.sh:2`,
        `engine/README.md.jinja:31,84`) — what the harness *publishes* in its skeleton,
        not what an instance's own filled-in gate must go on quoting. (The base ladder
        itself is unaffected — `$PDCA_BASE`/`$PDCA_VERIFY_BASE`/`$PDCA_BRIEF_BASE` are
        still exported to the per-fix verifier row, tier ``"C4"``, by every other test in
        this class — since issue #474, to that row alone.)"""
        if self.RENDERED:
            self.skipTest("run-verify.sh is instructed to become the instance's own "
                          "filled-in gate once rendered — the base-ladder wording binds "
                          "the template checkout only (issue #507)")
        text = self.SKELETON_TEXT if self.SKELETON_TEXT is not None \
            else _SKELETON.read_text(encoding="utf-8")
        self.assertIn("$PDCA_BRIEF_BASE", text)
        self.assertIn("Resolve as: $PDCA_BASE > $PDCA_VERIFY_BASE > your own override "
                      "> $PDCA_BRIEF_BASE", text)
        self.assertNotIn("origin/<default>", text)   # the old, un-supplied last rungs


class C4BaseLadderPostures(unittest.TestCase):
    """Posture regressions (issue #507). Drives the REAL
    `VerifyBaseExport.test_the_c4_skeleton_names_the_export_as_the_last_rung` method
    in-process against synthetic text/posture — no subprocess (the brief's fork-storm
    constraint; the Success criterion already mandates this shape) — by pointing its
    overridable `SKELETON_TEXT`/`RENDERED` at synthetic values instead of the checkout's
    own files."""

    _METHOD = "test_the_c4_skeleton_names_the_export_as_the_last_rung"

    #: A filled-in project gate that names none of the base-ladder vocabulary — the
    #: shape `engine/README.md.jinja:31,84` instructs every instance to replace the
    #: skeleton with.
    _FILLED_IN = "#!/usr/bin/env bash\nset -euo pipefail\npytest -q\n"

    def _run(self, text: str, rendered: bool) -> unittest.TestResult:
        case = VerifyBaseExport(self._METHOD)
        case.SKELETON_TEXT = text
        case.RENDERED = rendered
        result = unittest.TestResult()
        case.run(result)
        return result

    def test_the_unrendered_posture_still_requires_the_base_ladder(self) -> None:
        """Posture (i): today's green, unchanged — the real skeleton text against the
        template checkout."""
        result = self._run(_SKELETON.read_text(encoding="utf-8"), rendered=False)
        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)

    def test_a_filled_in_gate_missing_the_ladder_is_not_flagged_once_rendered(self) -> None:
        """Posture (iv): a rendered instance replaced the skeleton with its own real gate
        that names none of the base-ladder vocabulary — one of today's 8 failures (the
        other 7 are `C4RedLegVerdictRule` in `test_verify_red_leg.py`). Must be green:
        the property is scoped to what the harness PUBLISHES, not to what every instance
        is instructed to overwrite (issue #507)."""
        result = self._run(self._FILLED_IN, rendered=True)
        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
        self.assertEqual(len(result.skipped), 1)

    def test_the_same_missing_ladder_would_fail_if_it_were_still_bound(self) -> None:
        """Negative control: without the posture scope this fixture fails (it names none
        of the base-ladder vocabulary), so the previous test is exercising the skip, not
        a fixture that happens to pass anyway."""
        result = self._run(self._FILLED_IN, rendered=False)
        self.assertFalse(result.wasSuccessful())


if __name__ == "__main__":
    unittest.main()
