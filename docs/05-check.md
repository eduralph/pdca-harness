# Step 05 — Check: gates, reviewer, and the assembled SUMMARY

← [04 Do](04-do.md) · [Index](README.md) · next: [06 Sign-off →](06-signoff.md)

**Beat: Check.** This is the substance of the harness. Check verifies the built
artifact against the spec along **three axes — the "5/5/1"** — using three
mechanisms that run in order, then assembles everything into one document for the
human: `SUMMARY.md`.

```
BUILT ─► gates (deterministic) ─► reviewer (advisory) ─► assemble SUMMARY ─► AWAITING_SIGNOFF
         check-gates.{md,json}     check-review.md        (driver stops here)
```

## The 5/5/1

Check asks three different kinds of question:

- **5 correctness** — does it work? `C1` spec · `C2` reproduction (red pre-fix) ·
  `C3` change · `C4` verification (green post-fix) · `C5` causal adequacy.
- **5 conformance** — does it fit the project? `T1` structure · `T2` shape · `T3`
  runtime · `T4` contribution · `T5` judgment.
- **1 validation** — is it the *right* thing? (scope, success criterion, root
  cause vs symptom) — a human call.

Of these, the deterministic ones run as **gates**; `C5`, `T5`, and validation are
inherently judgment and route to the reviewer and to you.

## 1. Gates — the deterministic oracles

The gates you wired in [step 01](01-render-and-integrate.md) run automatically.
Each emits a row: check, result, oracle, and whether it's gating. Here is the
real `results/issue_11589/check-gates.md`:

```markdown
# Check gates — issue_11589

**Overall (gating): pass**

## Correctness (5)
| Check | Result | Oracle | Gating |
|---|---|---|---|
| C4 fix verified: test red pre-fix, green post-fix | pass | run-verify.sh | yes |
| (C1/C2/C3/C5 — none configured / judgment) | none | — | no |

## Conformance (5)
| Check | Result | Oracle | Rule | Gating |
|---|---|---|---|---|
| T1 structure | pass | gate.py T1 | 1 addon(s) conform | no |
| T2 shape | fail | gate.py T2 | __init__.py: no GPL header (doc16:99) | no |
| T3 runtime: core unit suite | fail | run-unit.sh | Trace/breakpoint trap (core dumped) [baseline] | no |
| T3 runtime: addon unit suites | fail | run-addon-unit.sh | pip install logs (3 failures) [baseline] | no |
| T4 contribution | pass | gate.py T4 | N/A: no commit-msg.txt | no |
```

**`Overall (gating): pass` even though three rows say `fail`.** This is the whole
point of the gating/advisory split. The only gating check — `C4-verify`, the
red→green proof — passed, so the contribution is *correct*. The failing rows are
all **advisory**: a GPL-header gap in a file the patch never touched, and two
runtime suites failing with `[baseline]` signatures (a pre-existing core segfault
and an environmental pip issue). They don't block — but they don't vanish either.
They become NEEDS-HUMAN items.

> **`[baseline]` vs `[delta]`.** gramps' T3 gate baseline-diffs: a known
> pre-existing failure is tagged `[baseline]` (ignore — not your fix's fault); a
> *new* failure is `[delta]` (your fix may have caused it). You'll see a `[delta]`
> bite in [step 06](06-signoff.md).

### Delegating to a host runner

If your project already single-sources its gates in its own runner (`cargo xtask`,
`make`, `just`, …), don't re-declare them in `pdca.toml` — **delegate**. Set a runner
and give each check a bare `subcmd`:

```toml
[gates]
runner = "cargo xtask"
checks = [
  { id = "C4-verify", tier = "C4", label = "fix verified red->green", subcmd = "verify", gating = true,  scope = "bundle" },
  { id = "T3-suite",  tier = "T3", label = "runtime suite",           subcmd = "test",   gating = false, scope = "repo" },
]
```

PDCA runs `cargo xtask verify` / `cargo xtask test` and maps the results onto the
5/5/1 — the host runner stays the single source of truth; PDCA only orchestrates it.
A full `cmd` (e.g. `cmd = "cargo xtask ci"`) still works for wholesale delegation. A
missing runner surfaces as a clear failing row (`runner '…' not found on PATH`), never
a crash. Set it at render time with the `gates_runner` copier question, or later in
`pdca.toml`.

## 2. Reviewer — the decorrelated second opinion

Next the `reviewer` leaf runs against `{patch.diff, test, brief.md,
check-gates.json}` — **not** `build-notes.md` ([step 04](04-do.md) explained why).
It re-runs the asserted evidence (stash → confirm red, unstash → confirm green),
re-checks that cited `path:line`s exist on the target branch, and flags scope
creep. Its output, `check-review.md`, is **advisory** — it annotates, it never
gates. The blocking path contains no LLM at all.

## 3. Assembly — the SUMMARY the human signs

The driver folds brief + gates + review into `SUMMARY.md`, a 10-section document.
The two sections that drive the human decision are **§6 NEEDS-HUMAN** (what you
must clear) and **§9 Check sign-off** (where you record the verdict — [step
06](06-signoff.md)). When `SUMMARY.md` exists with an empty §9, the bundle is
`AWAITING_SIGNOFF` and the driver **stops**.

Here is the real §6 from issue 11589 — note these are the advisory gate failures
turned into explicit, adjudicable questions (shown already cleared, `- [x]`):

```markdown
## 6. NEEDS-HUMAN — items the human must clear before sign-off
- [x] T2 — Gate failed on `__init__.py: no GPL licence header` — but no
  `__init__.py` appears in `patch.diff`. Both files the patch *does* touch are
  shape-clean. The violation sits in an untouched bundle file → human must decide
  whether the pre-existing gap blocks this contribution.
- [x] T3 — All three runtime suites are `fail` but the failure modes are not
  plausibly caused by a 2-file addon change: core unit `Trace/breakpoint trap
  (core dumped)` (segfault in gramps core, which the diff never touches),
  addon-unit `pip install logs` (env), interface `_ErrorHolder`. Marked
  "whole-suite baseline" — human must diff against baseline to confirm these are
  pre-existing, not regressions.
- [x] T5 — Always-human element. Overall contribution judgment — code quality,
  idiom fit, whether the sibling-detection approach is the right shape.
- [x] V — Validation — Whether the change solves the user's problem against the
  *real* FilterRules pack layout (13 rule pairs in one dir), not just the temp-dir
  stand-in, is a fitness-to-purpose call only the human can make.
```

This is the harness working as designed: the deterministic gate proved
correctness (`C4` green) and *surfaced* everything it couldn't adjudicate — a
header gap of ambiguous scope, environmental test noise, and the two irreducibly
human checks (`T5` judgment, `V` validation) — as a short, specific checklist.
The human isn't re-reading the whole diff; they're answering four pointed
questions.

## The C6 accept-guard

One rule connects §6 to the next step: **you cannot `--accept` while any §6 item
is unchecked.** That's the `C6` guard. It's why §6 items above are `- [x]` — the
human worked them before accepting. (`--iterate-*` and `--discontinue` are *not*
guarded — you can redirect or abandon a bundle with §6 still open.)

State: `AWAITING_SIGNOFF`. Your move — [step 06](06-signoff.md).

← [04 Do](04-do.md) · [Index](README.md) · next: [06 Sign-off →](06-signoff.md)
