# Brief — issue <id> / <slug>

> The Plan artifact (docs 02 §PLAN). Human-authored. Do reads ONLY this file.
> The field labels below are parsed by the driver — keep the `- **Label:** value`
> shape. The success criterion is the load-bearing field: it is the sentence
> Check tests "did this work" against.

- **Slug:** <short-kebab-slug>
- **Defect:** <what is wrong — the observable problem>
- **Success criterion:** <the observable condition that means it is fixed — must be
  demonstrable by C4-verify (the patch applied in isolation at Check). Do NOT scope this
  to a T3 whole-suite pass or a fork-CI green: those only clear after the fix is merged,
  not at Check. Use them as supplementary evidence only.>
- **Falsifiability:** <WHERE the binding success criterion can be made to go RED, and on
  WHICH harness/topology Do is pointed at. Name the environment that can actually exhibit
  the forbidden failure. This is orthogonal to `Verification posture` (green may be deferred
  off-Check) and to `Production reach` — here the question is whether RED is even *possible*
  on the environment Do gets. If no available environment can currently produce the red — a
  topology that cannot exhibit the forbidden failure (e.g. "exactly-one-winner under real
  partition" on a single-TiKV-replica stack, which can only go unavailable, never
  split-brain — the #257/#365 case), or code no gate compiles so RED is only ever asserted
  by code-reading — that is a **Plan-blocking gap**: provision the environment or narrow the
  criterion *before* Do runs; don't burn Do cycles on a criterion that cannot fail.>
- **Invariant to restore:** <the property the fix must make true, stated over the
  defect CATEGORY, not the repro file. NOT a mechanism. Cite its source (language spec /
  framework docs / internal rule) per `docs/principles.md` §3–§6. SELF-TEST: could Do
  satisfy this by guarding a single module? If yes, it's the narrow symptom-sentence —
  widen it. Omit only for non-structural behavioural bug fixes (principles.md §1.1).>
- **Repo + branch target:** <owner/repo> @ <branch>   (resolve here at Plan — do not leave to Do)
- **Onto branch:** <remote>/<existing-pr-branch>   (optional — stack the fix as a commit onto an existing open PR's branch instead of opening a new PR; the fix is tested, committed, and pushed against THIS branch; docs 03)
- **Depends on:** <id>[, <id>…]   (optional — ids only on the value line, any trailing note is ignored; the PRIMARY ordering field. A batch runs as dependency WAVES: this bundle lands in a later wave than its prereqs and builds on their accepted result — the wave driver folds each wave's accepted work onto the base the next builds on, so no human merge is needed between them; docs 09)
- **Depends on (merged):** <id>[, <id>…]   (optional, DEPRECATED — in the wave model this is just `Depends on`: the wave fold already gives the dependent the prereq's accepted diff without waiting for a merge. Still parsed for back-compat; prefer `Depends on`; docs 09)
- **Conflicts with:** <id>[, <id>…]   (optional — ids only on the value line, any trailing note is ignored; these edit a shared resource, so they are scheduled into DIFFERENT waves — never built blind on the same base; docs 09)
- **Stacks on:** <id>[, <id>…]   (optional, DEPRECATED — in the wave model this is just `Depends on`: whole-wave stacking generalises the single-chain Stacks on (and fixes multi-parent). Still parsed for back-compat; prefer `Depends on`; docs 09)
- **Ordering note:** <optional free text — WHY the scheduling fields above are set as they are (e.g. "depends-on-merged 12 because both edit cache.py"). Not machine-parsed; it documents the human's sequencing decision next to the bare-id fields.>
- **Surfaces:** <where the change is observable — `gui` (touches the frontend / an E2E
  through the app is needed), `data` (backend/logic only), or `both`. Drives which
  runtime gates apply (e.g. an E2E gate runs only when this is `gui`). Optional.>
- **Difficulty:** <`low` | `medium` | `high` — the fix's **blast-radius / cross-file
  reach**: how many files/call-sites it touches and how far its effects propagate (what a
  diff-reviewer must hold in view), NOT edge-case density (the deterministic gates own
  that). low = a localized one-site change; high = a wide, cross-cutting change. Routes
  the Do backend and review depth (issues #133/#134). Optional; absent/unknown is the safe
  default — no review or capability is skipped on a missing tag.>
- **Do model:** <optional — pin the Do backend explicitly to a `[[leaves.builder_variant]]`
  `model` name (e.g. `frontier`), OVERRIDING the difficulty `when` routing. Use when a bundle
  must run on a specific backend regardless of difficulty (e.g. keep a privacy-sensitive fix on
  a local model). Absent ⇒ the difficulty routing / default builder; issue #167.>
- **Scope:** <the defect to remove — one logical fix. MUST NOT name a probe/guard/helper
  (a capability check, `hasattr`, `try/except import`): naming a mechanism seats the fix
  shape for Do. Leave mechanism to Do; Do prefers removing the cause over guarding it
  (principles.md §3.1, §3.3).> / out of scope: <what is explicitly excluded>
- **Repro instruction:** <fixture + exact steps on the target branch>
- **External dependencies:** <the build tools (e.g. `protoc`), runtime services (Docker, a
  live etcd/TiKV), and required topology/environment shape (a ≥3-replica cluster) the slice
  needs both to BUILD and to make the success criterion go red→green — enumerated at Plan so
  they preflight rather than surface mid-cycle. **Registration is mandatory, not
  best-effort:** every dependency a human must install or provide MUST have a matching
  `[[doctor.checks]]` row in the render (a `cmd` that detects it + an install `hint`) and be
  named here as a **backticked token equal to that row's `id`** (`protoc` ↔ `id = "protoc"`).
  A declared, human-installable dependency with no matching row is a Plan-exit gap: the
  driver reconciles this field against the registered rows at PLAN EXIT — before Do
  dispatches — and again at Check as a backstop, routing any
  unregistered token into SUMMARY §6, where it blocks accept until it is registered. A
  dependency that legitimately has NO detecting command — a topology / environment shape (a
  ≥3-replica cluster, a partition-capable stack) — goes in plain prose (not backticked), or
  as a backticked token annotated `(no-check: <why>)`; either is exempt. Keep every token on
  this line — a wrapped continuation line is not parsed. `none` if nothing beyond the base
  toolchain is needed (do not list the base toolchain here). Do MUST declare any it
  discovers that is not listed here (see builder) rather than silently work around it with a
  code-read, an alias, or a curated fixture — an unmet/worked-around dependency is a Check
  §6 item, not a substitution.>
- **Test file:** <path where the regression test ships — must fail pre-fix, pass post-fix.
  Match the file to the C4 gate you actually have (engine/README.md). The shipped contract
  reverts the *production* change and keeps the briefed test, so a test appended to an
  existing suite — or co-located with the code — earns its red fine. But a gate that instead
  classifies on an **added test file** can only earn a red from a NEW file; under that
  variant an appended or inline test silently degrades to a green-only check that proves
  nothing. Check which yours does before naming the path.>
- **Citations expected:** Do must cite path:line on the target branch for every change.
  For a **composition slice** — the fix wires into an existing pattern the codebase already
  applies — MAY name the **peer callsite** Do should mirror, e.g. "resolve the backend as
  `cmd_put` does, `cli.rs:865`". Do MAY open that one cited callsite (a narrow, deliberate
  exception to reading `brief.md` only) to copy the composition, so a locally-reasonable but
  globally-wrong call — an empty local redb where the peer resolves TiKV, a positional id
  where the peer uses the registered domain — is avoided. Cite it precisely; anything not
  cited stays out of Do's input.
- **Prior-art check (triage cycles):** <searched by file path — merged history / open PRs / closed PRs — result>
- **Disposition hint:** <one triage flag — drives the driver's Do path. FIX (full
  Do+Check band): `likely-fix`, `POSSIBLY-FIXED → verify first` (needs verification, so
  NOT close). CLOSE / no-fix (FAST-PATHED — builder + reviewer leaves skipped, routed
  straight to sign-off; docs 04 §close-disposition fast path): `likely-close`, `wontfix`,
  `by-design`, `duplicate`, `not-reproducible`, `manual-verification`, `upstream` (not this
  repo's defect), `external` (not a defect in scope). `NO-NOTES` is a low-triage-signal
  flag, not an outcome. The close set is configurable per instance in `pdca.toml`
  `[driver].close_dispositions` — keep this list in step with it.>

## STOP discipline

Draft only until Check sign-off. Pushing to a feature/draft branch and opening a
draft PR MAY happen during the cycle (useful for CI feedback). The PR MUST NOT be
marked ready before sign-off accepts.
