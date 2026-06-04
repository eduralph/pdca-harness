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
# holds patch.diff and the brief that names the test. The contract this script
# must enforce, exiting 0 iff BOTH hold:
#   - WITHOUT the fix applied, the bundle's test FAILS (red) — proves the repro.
#   - WITH the fix (patch.diff) applied, the bundle's test PASSES (green).
# That validates THIS change, not the whole suite (see engine/README.md).
#
# Typical shape (pseudocode — replace with your project's apply/run/revert):
#   1. read the test path from $PDCA_BUNDLE/brief.md
#   2. revert the production change, run the test  -> expect FAIL (red)
#   3. apply $PDCA_BUNDLE/patch.diff, run the test -> expect PASS (green)
#   4. exit 0 on red-then-green, non-zero otherwise
set -euo pipefail

BUNDLE="${PDCA_BUNDLE:?run from the driver — \$PDCA_BUNDLE must be set}"

echo "engine/scripts/run-verify.sh: not yet implemented for this project." >&2
echo "Implement the red->green check against \$PDCA_BUNDLE=$BUNDLE" >&2
echo "(see engine/README.md for the contract), then wire it in pdca.toml." >&2
exit 1
