# Result — issue <id> / <slug>

> The result document (docs 02 §SUMMARY.md). Assembled by the driver from
> brief.md + check-gates.json + check-review.md (§1–8); the human completes
> Check by clearing §6 and recording §9. §9 closes the contribution; §10 is a
> separate feeder to the next Act review. This file is the canonical shape the
> driver's `assemble_summary` mirrors — keep the two in step.

## 1. Spec (from brief.md)              ← Check verifies against THIS
- Defect:
- Success criterion:
- Repo + branch target:
- Scope (one logical fix) / out of scope:

## 2. Disposition claimed               ← sign-off confirms or overrides
- Outcome: Fixed | Already-fixed | Can't-repro | Wontfix | By-design | External
- Confidence: high | medium | low
- Recommendation: merge-wider | close-<reason> | iterate-to-<beat>

## 3. Correctness (Check — chain)
- reproduction / verification / regression / causal-adequacy: result + oracle + evidence.

## 4. Conformance (Check — stack)
- T1–T4 deterministic: pass/fail + rule IDs + path:line (from check-gates).
- T5 judgment: → see §5.

## 5. Advisory review (artifact-only, decorrelated)
- Reviewer summary, FAILs, and independent RE-RUN results. Produced without build-notes.

## 6. NEEDS-HUMAN — items the human must clear before sign-off
- [ ] <decision only the human can make, and why a gate/reviewer couldn't>

## 7. Proven / not proven
- Proven by which oracle:
- Unproven / needs manual run:

## 8. Ready-to-ship attachments
- patch.diff
- pr-description.md      (only if a PR is warranted)
- tracker-comment.md     (ALWAYS, every tracker item)
- MANUAL-VERIFICATION.md (ANY manual-work outcome)
- build-notes.md         (builder rationale — for the human, not the reviewer)

## 9. Check sign-off                     ← human completes Check here
- Disposition confirmed / overridden:
- Outcome:
- Iteration delta (if iterating):
- By / date:

## 10. Act candidates (hints for the next Act review)
- (empty is the common case)
