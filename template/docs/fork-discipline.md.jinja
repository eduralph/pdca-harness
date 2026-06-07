# Fork & contribution discipline

> The generic rules for contributing a fix upstream through a fork — the **Check
> closing / contribution** discipline (the counterpart to [`principles.md`](principles.md),
> which governs Plan-time solution design). These rules are project-agnostic; **your
> project's concrete answers** — which branches, which remotes, which validation
> runners — live in [`INTEGRATION.md`](INTEGRATION.md) (§2 branch targets, §5
> upstream-isn't-ahead, §8 committing/PR, §10 maintainer). The cycle consults this
> file: the planner, builder, reviewer, and publisher are each pointed here for the
> rules they act on. Maintained as a generic reference — record a project specific in
> `INTEGRATION.md`, not here.

The split this file assumes: **the rules are generic and live here; the answers are
per-project and live in `INTEGRATION.md`.** Where a rule needs a concrete value
(a branch name, a remote, a runner), it says *"(instance: INTEGRATION.md §N)"* — fill
it there, not by editing this file (so `copier update` keeps the rules current).

## 1. Fork mechanics

- **Remote layout.** `upstream` = the canonical project (read-only, the contribution
  target); `origin` = **your fork** (where branches are pushed). The fork owner is
  distinct from the upstream owner. *(instance: the actual repos/owner — INTEGRATION.md §5.)*
- **Branch from `upstream/<base>`, never the fork's tracking branch.** A fork's
  `maintenance/*` / default branches **drift** — they carry local CI/tooling commits
  that must not ride into the contribution. Always `fetch upstream` and branch off
  `upstream/<base>` so the diff is exactly the fix. *(A stray tooling file riding in on
  a fork-based branch is the classic symptom.)*
- **A fork-based PR's `--head` is `OWNER:BRANCH`.** The branch lives on the fork
  (origin); `gh pr create --head <bare-branch>` resolves it against the *base* repo,
  where it doesn't exist, and fails. The publish mechanics handle this deterministically.
- **Keep the fork checkout clean.** Publish refuses on a dirty checkout. A
  patch-apply-then-commit op must stage **added** files (a new test), not only modified
  ones — `git apply` + `git add --all` + `git commit`, never `commit -a` (which silently
  drops the new file). The same applies to a patch-and-revert validation gate: revert
  *modified* files and **remove** *added* ones, or run in a throwaway worktree.

## 2. Draft-only, and STOP

- The automation **commits, pushes a branch, and opens a *draft* PR — then stops.** It
  never marks a PR ready and never merges. Marking ready is the human's disposition
  after a fresh-eyes re-read.
- No push / PR-open / ready-mark happens without explicit instruction. This is
  **mechanically enforced** by the builder/publisher PreToolUse hook (`builder_guard.py`),
  not left to prompt discipline.
- If a brief seems to require marking a PR ready, that is a brief defect — surface it
  and stop; do not work around the block.

## 3. Contribution targeting

For a project with a single line of development this is one rule (target the default
branch). For a project with **parallel maintenance lines**, the targeting matters:

- **Fixes ride the current maintenance line and forward-merge** to the development
  line; only genuinely new features target the development/`main` line. A fix sent to
  the dev line alone doesn't reach released users. *(instance: the per-area branch map
  — INTEGRATION.md §2.)*
- **A maintainer's explicit base-branch request on the PR overrides the default.**
- **Cross-version cherry-pick is a *correctness* check, not a conflict check.**
  "Applies cleanly" is **not** "remains correct": verify a cherry-picked fix against the
  *target branch's* related code — **including files the patch doesn't touch** — not just
  that `git cherry-pick`/`git apply` succeeds.
- **Where the test/artifact ships can differ by target.** Different branches or target
  kinds (e.g. a core vs a plugin/addon contribution) may use different test locations or
  naming conventions; the brief's **Test file** field must name the right one *for the
  target branch*. Getting it wrong is a recurring Do error. *(instance: the per-target
  conventions — INTEGRATION.md §3.)*

## 4. Validation against upstream

- **Validate against the *clean upstream* contribution target**, not the fork's
  tooling-polluted branches and not a developer's working clone. Use a pinned checkout
  of `upstream/<base>` (a per-version `git worktree` when the project has several target
  versions) so "verified" means "verified against what reviewers will see." *(instance:
  the runners + how the pinned targets are built — INTEGRATION.md §3.)*
  - *Docker note:* a `git worktree`'s `.git` is a **file** pointing at the primary
    gitdir; if you bind-mount a worktree into a container, also mount that gitdir at its
    own absolute path or in-container git breaks.
- **A fix may legitimately depend on an unmerged upstream fix.** When the contribution
  target lacks a not-yet-merged prerequisite, **record the dependency** (e.g. retry
  against an "essential" line = upstream + a minimal, evidence-based set of enabling
  fixes, and write a dependency note into the bundle) rather than silently failing the
  gate. Keep that enabling set minimal — add a fix only once a bundle demonstrably needs
  it ([`principles.md`](principles.md) §8). *(instance: the essential set + manifest —
  INTEGRATION.md §2.)*
- **The patch must be commit-ready for the *target* repo.** The publish commit runs the
  *target's own* pre-commit hooks (formatter/linters), which no PDCA gate models — so
  "all gates green" ≠ committable. Run the project's configured formatter / commit hooks
  before declaring done.

## 5. Upstream-isn't-ahead (prior art)

Before contributing, confirm the fix isn't already upstream or already rejected:

- **Search by affected file *path*, not bug-id or keyword.** Anchoring on the path
  catches side-effect fixes that an id/keyword search misses. (Mind host tokenization
  quirks — a substring search may not match a compound word.)
- **Check merged history *and* closed/rejected PRs** on the upstream repo(s) for that
  path before opening a contribution. *(instance: the exact branches + search commands —
  INTEGRATION.md §5.)*

---

**Who consults this.** Plan resolves the branch target and runs the prior-art check
(§3, §5); Do writes the patch against the target branch, honoring cherry-pick
correctness and test placement (§1, §3, §4); Check (reviewer) judges cherry-pick
correctness, validation-against-upstream, and prior-art (§3–§5) and routes the
unmechanizable ones to NEEDS-HUMAN; Publish performs the fork push under §1–§2. The
deterministic git/PR mechanics are in `pdca_harness.publish`; this file is the *why*.
