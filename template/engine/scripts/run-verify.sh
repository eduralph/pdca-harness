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
#   - $PDCA_BASE / $PDCA_VERIFY_BASE / $PDCA_BRIEF_BASE — the base to reset to before
#       applying patch.diff. The driver sets EXACTLY ONE of these for every bundle-scoped
#       gate: the test base must never diverge from the base publish will commit to. Each is
#       already a fully-qualified remote-tracking ref (`<remote>/<branch>`) — use it as-is,
#       never `origin/$VAR` (that doubles the remote).
#         * $PDCA_BASE (issue #54) — the brief's `Onto branch`. Publish appends the fix as a
#           commit to that existing PR head, so the gate must prove red->green on IT.
#         * $PDCA_VERIFY_BASE (issue #273) — the wave's folded integration branch
#           (`origin/pdca-integration/<base>`) for a wave>0 bundle in a dependency batch, so a
#           dependent verifies against base+prereqs. Resetting to the brief's origin base
#           instead would false-fail "patch does not apply — stale" for a dependent that
#           shares a file with its prereq, or measure red->green against a tree LACKING it.
#         * $PDCA_BRIEF_BASE (issue #387) — the ordinary case: the brief's own
#           `Repo + branch target` base (or the project default branch when it names none),
#           resolved by the driver with the SAME parser publish uses. Do NOT re-derive it by
#           parsing brief.md in shell: that parse is subtle (a backticked ref counts only at
#           the START of the field, so `main (feature branch \`feat/x\`)` means `main`) and
#           a re-derivation drifts from the base publish commits to — the divergence #235
#           and #262 fixed in Python and this export removes from shell.
#       Resolve as: $PDCA_BASE > $PDCA_VERIFY_BASE > your own override > $PDCA_BRIEF_BASE.
# The contract this script must enforce, exiting 0 iff BOTH hold:
#   - WITHOUT the fix applied, the bundle's test FAILS (red) — proves the repro.
#   - WITH the fix (patch.diff) applied, the bundle's test PASSES (green).
# That validates THIS change, not the whole suite (see engine/README.md).
#
# Typical shape (pseudocode — replace with your project's apply/run/revert):
#   1. read the test path from $PDCA_BUNDLE/brief.md
#   2. revert the production change, run the test  -> expect a REAL red: a test that RAN
#      and failed (judge it by the two facts below, never by the exit code alone)
#   3. apply $PDCA_BUNDLE/patch.diff, run the test -> expect PASS (green)
#   4. exit 0 on red-then-green, non-zero otherwise
#
# JUDGE EVERY LEG BY TWO FACTS: the runner's exit code AND how many tests actually ran.
# A test runner exits non-zero for two unrelated reasons — the test RAN and failed (the red
# leg's proof), or NO test ran at all (it failed to compile/import/collect, the runner could
# not find it, the runner itself died). An exit code cannot tell those apart, so a leg judged
# on the exit code alone reports PASS for a bundle whose test never executed. That is an
# everyday shape, not a corner case: reverting the fix also removes any symbol the fix
# introduced, so a test that calls one cannot even build on the red leg.
# Capture BOTH per run: the exit code, and a COUNT of executed tests parsed from the runner's
# own machine-readable report (TAP, JUnit XML, `--format json`, `python -m unittest -v`, …).
# Never infer that count from the exit code.
#
#   exit code | tests ran | what it means -> what to report
#   ----------+-----------+---------------------------------------------------------------
#    0        |  0        | nothing ran -> PDCA-UNVERIFIABLE (77): no evidence either way
#    0        | >0        | test PASSED -> green leg: OK; red leg: C4 FAIL (green without
#             |           |                the fix — the test does not capture the defect)
#    non-zero | >0        | test FAILED -> red leg: the red you want; green leg: C4 FAIL
#    non-zero |  0        | nothing ran -> PDCA-UNVERIFIABLE (77), NEVER PASS: the runner
#             |           |                died before/while collecting, so its non-zero
#             |           |                exit proves nothing about the defect
#
# Keep the two "nothing ran" cases distinguishable in the reason you print — the human
# reading §6 needs different things from each: `no test executed (runner exited 0: nothing
# was selected — wrong test path or filter?)` vs `no test executed (runner exited <rc>: the
# test did not build/import — e.g. it calls a symbol the reverted fix added)`.
# THE RULE, for every leg you add here and for every other verification step: a step in
# which no test ran is UNVERIFIABLE — exit 77 / `PDCA-UNVERIFIABLE: <reason>` (-> SUMMARY §6
# NEEDS-HUMAN, non-gating) — never a pass and never a fail. A gate never turns "no evidence"
# into a verdict.
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
