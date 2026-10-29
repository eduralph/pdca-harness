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
