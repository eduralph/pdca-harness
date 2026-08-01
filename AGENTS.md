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
- Your work on a change **ends at the open PR with green checks**. Report the PR
  URL and stop. The ready-mark and the merge are the maintainer's sign-off
  touchpoints — the human decision this project's quality cycle exists to
  protect, and the same STOP discipline the harness enforces on its own leaves.

## Change discipline

- Every change starts from an issue on the open milestone, and the PR body
  carries a closing reference (`Closes #NNN`) — the `require-linked-issue` check
  enforces it.
- One logical change per issue / branch / PR; split unrelated changes.
- Commits are signed off (`git commit -s`, [DCO](DCO)) with a
  conventional-prefix subject (`fix:`, `feat:`, `docs:`, `ci:`, `chore:`) and a
  body that explains the why.
- Keep the offline suite green:
  `cd template && PYTHONPATH=src python3 -m unittest discover -s tests`.
