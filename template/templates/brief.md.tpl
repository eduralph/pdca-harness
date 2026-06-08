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
- **Repo + branch target:** <repo @ branch — resolved here, not left to Do>
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
- **Disposition hint:** <likely-fix | likely-close | POSSIBLY-FIXED → verify first | UPSTREAM | EXTERNAL | NO-NOTES>

## STOP discipline

Draft only until Check sign-off. Pushing to a feature/draft branch and opening a
draft PR MAY happen during the cycle (useful for CI feedback). The PR MUST NOT be
marked ready before sign-off accepts.
