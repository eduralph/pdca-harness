# PR description (only if a PR is warranted)

> Attachment referenced by SUMMARY.md §8. One logical fix per PR.

## Summary
<1–2 sentences in plain language: the user-facing symptom and its impact, then the
one-line change. A reader who does not live in this file should grasp WHAT and WHY.
No internal/process jargon.>

## What to look at
<orient the reviewer: the key file(s)/function(s) and the crux of the change, and how
to exercise or reproduce it. Lower the barrier to a first pass.>

## Root cause
<two sentences — for the reviewer who wants the internals>

## Fix
<what the diff does>

## Verification
<a skimmable claim→evidence trail — what was checked and where, so the review is
visible, not implied:>
- **Claim:** <the condition this fix establishes (the brief's success criterion)>
- **Checked:** <path>:<lines> on the branch the PR targets — <what was verified there>
- **Test:** <regression test path> — fails pre-fix, passes post-fix. <Or: why no test
  applies + the manual repro steps.>

<!-- Tracker reference (optional, mirrors the commit-msg trailer). The contribution
     gate lints commit-msg.txt and this PR body INDEPENDENTLY, so a ticketed fix needs
     the id in BOTH. For a ticketed fix, keep the line below in the project's
     [tracker].issue_trailer form (e.g. `Fixes #<id>`). For a declared-ticketless fix
     (no tracker id yet / non-core), OMIT it and state the origin in-body instead, e.g.
     "Reported in <upstream>#<n>; no tracker ticket". -->
Fixes #<id>
