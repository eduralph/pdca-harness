# Agent context — pdca-harness

Rules for any coding agent (Claude, Codex, or other) working **on this template
repository**. [CONTRIBUTING.md](CONTRIBUTING.md) governs humans and agents alike;
this file adds the agent-specific discipline. (The AGENTS.md a rendered instance
ships — `template/AGENTS.md.jinja` — governs the model leaves *inside* an
instance; this one governs work on the template itself.)

## STOP discipline — the merge is never yours

- **Never merge a pull request.** Not `gh pr merge`, not the web UI, not
  auto-merge — no path, regardless of green checks or how trivial the change is.
- **Never mark a draft pull request ready** (`gh pr ready`).
- These rules cover this project's own pull requests. The one exception is the ready-marks
  and merges that `tools/live/stack_mode_live.py` makes itself, started by a human, on a
  repository that passes the tool's repo guard (the private sandbox; see
  [CONTRIBUTING.md](CONTRIBUTING.md#checking-stack-mode-on-real-github)). It covers no other
  merge on the sandbox repository, by hand or by an agent.
- Your work on a change **ends at the open PR with green checks**. Report the PR
  URL and stop. The ready-mark and the merge are the maintainer's sign-off
  touchpoints — the human decision this project's quality cycle exists to
  protect, and the same STOP discipline the harness enforces on its own leaves.

## Change discipline

- Every change starts from an issue on the open milestone, and the PR body
  carries a closing reference (`Closes #NNN`) — the `require-linked-issue` check
  enforces it. A PR that delivers only part of an issue uses a non-closing
  `Refs #NNN` instead, so the issue stays open for the rest; the check accepts
  that too, but only for a number that is an existing issue. Say in the PR body
  what is left and where it is tracked.
- One logical change per issue / branch / PR; split unrelated changes.
- Commits are signed off (`git commit -s`, [DCO](DCO)) with a
  conventional-prefix subject (`fix:`, `feat:`, `docs:`, `ci:`, `chore:`) and a
  body that explains the why.
- Keep the offline suite green:
  `cd template && PYTHONPATH=src python3 -m unittest discover -s tests`.

## Workflows

- Pin every action to a full commit hash with its release as a comment
  (`uses: actions/checkout@<40-char sha> # v7.0.1`), never to a tag or branch. Pick a
  release at least 7 days old (the Dependabot cooldown); Dependabot keeps the pins in
  `.github/workflows/` current.
- The workflow the template renders into every instance
  (`template/.github/workflows/*.jinja`) uses the same pins: Dependabot doesn't read it, so
  update it in the same PR.
- Give every `actions/checkout` `persist-credentials: false`, and each job only the
  `permissions:` it needs.
- `tests/test_workflows.py` checks all three; `render-check` runs it.
- A job that branch protection requires is listed in `.github/required-checks.txt`. When
  you rename, add or drop one, change the file in the same PR and tell the maintainer to
  re-apply the settings after the merge; it needs admin rights.

## Review rubric & protocol

This section binds both sides of a review: authors self-review against it before
requesting review, and reviewers judge against it. The repo's written conventions are the
arbiter, not reviewer taste. Each defect class below recurred in review findings on this
repository; when a class gets a deterministic gate, it leaves review scope. Instances
consume this section through `rubric_file` / `rubric_section`, so keep the heading.

### Hard conventions (MUST)

- **Deterministic driver**: no model decides control flow. State transitions, gates and
  the accept-guard stay deterministic code; a model only fills an artifact at a leaf.
- **STOP discipline**: no code path marks a PR ready or merges it, except the live tool
  named under STOP discipline above.
- **Fail open only where the design says so**: an optional input such as the rubric
  degrades to "absent" with a warning. A gate, a guard or a state check fails closed.
- **Template and rendered output stay in step**: a change to a shipped workflow pin, a
  `pdca.toml` key or a rendered doc changes `template/` in the same PR, and a new config
  key appears in `template/pdca.toml.jinja`.
- **Workflows** follow the rules under Workflows above.

### Recurring defect classes (MUST check when the diff touches the surface)

- **Partial state on failure**: a multi-step change to bundles, branches, worktrees or
  tracker state either completes or rolls back. Validate everything (missing briefs,
  cycles, frozen parents, existing destinations) before the first write, not between
  writes.
- **Persistence order**: persist a result before acting on the next item, and persist a
  verified value (a fetched base, a retargeted branch) rather than the assumed one.
- **Stale state**: re-read tracker, label and branch state before deciding on it. A cached
  resolution, an unfetched base or a reopened issue must not be skipped as done.
- **Locks and shared worktrees**: take the lane lock before sweeping or mutating, and never
  sweep a worktree another run or a human still owns.
- **Hand-written Markdown parsing**: closing fences match their opener, headings inside
  fences and HTML comments are not structure, and section names match exactly or as a
  documented prefix, never as any substring.
- **Workflow path handling**: parse changed paths without Git's filename quoting
  (`-z`), and cover every diff-filter letter the check needs (`A`, `M`, `R`, `T`).
- **Tests CI does not run**: a test change runs every suite that imports or mocks the
  changed code, including suites outside `render-check`. A test that needs real Git
  history creates real commits; a test must be able to fail.

### Reviewer protocol

- **DCO**: the `dco` status check is the sole authority. Do not report `Signed-off-by`
  findings from your own commit inspection; the commits a review sees are often GitHub's
  synthesized merge previews, and every such finding here was a false positive.
- **Commit and PR metadata**: the linked issue and the sign-off are enforced by required
  checks. Do not raise them as code findings.
- **Severity**: P1 and P2 findings are fixed in the PR. A P3 is answered with a follow-up
  issue number; that settles it, so do not raise it again in later rounds.
- **Out of scope**: a real finding outside the PR's stated scope gets an issue reference,
  not an in-PR fix.
- **Verify before reporting**: cite only commits, lines and rules that exist on the
  reviewed head. Quote this file as it stands on that head.
