#!/usr/bin/env bash
# Per-fix correctness gate (C4) — SKELETON. Fill this in for your project.
#
# Wired from pdca.toml as a bundle-scoped GATING check:
#   [[gates.checks]]
#   id = "C4-verify"
#   tier = "C4"
#   cmd = "./engine/scripts/run-verify.sh"
#   gating = true
#   scope = "bundle"
#
# The driver exports $PDCA_BUNDLE = the bundle dir (results/issue_<id>/), which
# holds patch.diff and the brief that names the test. It also exports, when set:
#   - $PDCA_WORKTREE   — the tree Do edited (worktree isolation, #94); run/reset here.
#   - $PDCA_BASE / $PDCA_VERIFY_BASE — the base to reset to before applying patch.diff.
#       The driver sets AT MOST ONE of these: the test base must never diverge from the base
#       publish will commit to. Prefer whichever is set.
#         * $PDCA_BASE (issue #54) — the brief's `Onto branch`. Publish appends the fix as a
#           commit to that existing PR head, so the gate must prove red->green on IT.
#         * $PDCA_VERIFY_BASE (issue #273) — the wave's folded integration branch
#           (`origin/pdca-integration/<base>`) for a wave>0 bundle in a dependency batch, so a
#           dependent verifies against base+prereqs. Resetting to the brief's origin base
#           instead would false-fail "patch does not apply — stale" for a dependent that
#           shares a file with its prereq, or measure red->green against a tree LACKING it.
#       Resolve as: $PDCA_BASE > $PDCA_VERIFY_BASE > your own override > the brief's
#       `Repo + branch target` > origin/<default>. Neither is set for an ordinary wave-0
#       single bundle, where the brief's base is the correct one.
# The contract this script must enforce, exiting 0 iff BOTH hold:
#   - WITHOUT the fix applied, the bundle's test FAILS (red) — proves the repro.
#   - WITH the fix (patch.diff) applied, the bundle's test PASSES (green).
# That validates THIS change, not the whole suite (see engine/README.md).
#
# Typical shape (pseudocode — replace with your project's apply/run/revert):
#   1. read the test path from $PDCA_BUNDLE/brief.md
#   2. revert the production change, run the test  -> expect FAIL (red)
#   3. apply $PDCA_BUNDLE/patch.diff, run the test -> expect PASS (green)
#   4. exit 0 on red-then-green, non-zero otherwise
#
# CLASSIFY THE PATCH FIRST (issue #165). If the patch's only non-test change is a
# NON-BEHAVIORAL file a project must update but that can't move the test — a translation
# manifest / file-registration list / generated asset (e.g. po/POTFILES.{in,skip}) — there
# is nothing to revert that would go red. Emit `PDCA-UNVERIFIABLE: <reason>` and exit 77
# (-> SUMMARY §6 NEEDS-HUMAN, non-gating) instead of a red->green the bundle is guaranteed
# to fail (a false C4 fail for a verify-first test-only fix). Keep the non-production set as
# a config list of path globs. See engine/README.md (§The two gate shapes that matter).
set -euo pipefail

BUNDLE="${PDCA_BUNDLE:?run from the driver — \$PDCA_BUNDLE must be set}"

echo "engine/scripts/run-verify.sh: not yet implemented for this project." >&2
echo "Implement the red->green check against \$PDCA_BUNDLE=$BUNDLE" >&2
echo "(see engine/README.md for the contract), then wire it in pdca.toml." >&2
exit 1
