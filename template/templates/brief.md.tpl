# Brief — issue <id> / <slug>

> The Plan artifact (docs 02 §PLAN). Human-authored. Do reads ONLY this file.
> The field labels below are parsed by the driver — keep the `- **Label:** value`
> shape. The success criterion is the load-bearing field: it is the sentence
> Check tests "did this work" against.

- **Slug:** <short-kebab-slug>
- **Defect:** <what is wrong — the observable problem>
- **Success criterion:** <the observable condition that means it is fixed>
- **Repo + branch target:** <repo @ branch — resolved here, not left to Do>
- **Scope:** <the one logical fix> / out of scope: <what is explicitly excluded>
- **Repro instruction:** <fixture + exact steps on the target branch>
- **Test file:** <path where the regression test ships — must fail pre-fix, pass post-fix>
- **Citations expected:** Do must cite path:line on the target branch for every change.
- **Disposition hint:** <POSSIBLY-FIXED → verify first | likely-fix | likely-close>

## STOP discipline

Draft only until Check sign-off. Pushing to a feature/draft branch and opening a
draft PR MAY happen during the cycle (useful for CI feedback). The PR MUST NOT be
marked ready before sign-off accepts.
