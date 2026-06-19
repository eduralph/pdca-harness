# Brief — issue <id> / <slug>

> The Plan artifact (docs 02 §PLAN). Human-authored. Do reads ONLY this file.
> The field labels below are parsed by the driver — keep the `- **Label:** value`
> shape. The success criterion is the load-bearing field: it is the sentence
> Check tests "did this work" against.

- **Slug:** <short-kebab-slug>
- **Defect:** <what is wrong — the observable problem>
- **Success criterion:** <the observable condition that means it is fixed>
- **Invariant to restore:** <the property the fix must make true, stated over the
  defect CATEGORY, not the repro file. NOT a mechanism. Cite its source (language spec /
  framework docs / internal rule) per `docs/principles.md` §3–§6. SELF-TEST: could Do
  satisfy this by guarding a single module? If yes, it's the narrow symptom-sentence —
  widen it. Omit only for non-structural behavioural bug fixes (principles.md §1.1).>
- **Repo + branch target:** <owner/repo> @ <branch>   (resolve here at Plan — do not leave to Do)
- **Onto branch:** <remote>/<existing-pr-branch>   (optional — stack the fix as a commit onto an existing open PR's branch instead of opening a new PR; the fix is tested, committed, and pushed against THIS branch; docs 03)
- **Depends on:** <id>[, <id>…]   (optional — batch/lane scheduling waits until these bundles are COMPLETE before this one runs; docs 09)
- **Conflicts with:** <id>[, <id>…]   (optional — never co-schedule these in the same concurrent wave, e.g. they edit a shared file; docs 09)
- **Surfaces:** <where the change is observable — `gui` (touches the frontend / an E2E
  through the app is needed), `data` (backend/logic only), or `both`. Drives which
  runtime gates apply (e.g. an E2E gate runs only when this is `gui`). Optional.>
- **Scope:** <the defect to remove — one logical fix. MUST NOT name a probe/guard/helper
  (a capability check, `hasattr`, `try/except import`): naming a mechanism seats the fix
  shape for Do. Leave mechanism to Do; Do prefers removing the cause over guarding it
  (principles.md §3.1, §3.3).> / out of scope: <what is explicitly excluded>
- **Repro instruction:** <fixture + exact steps on the target branch>
- **Test file:** <path where the regression test ships — must fail pre-fix, pass post-fix>
- **Citations expected:** Do must cite path:line on the target branch for every change.
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
