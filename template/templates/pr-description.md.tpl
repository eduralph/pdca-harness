# PR description (only if a PR is warranted)

> Attachment referenced by SUMMARY.md §8. One logical fix per PR.

## Root cause
<two sentences>

## Fix
<what the diff does>

## Verified against
- <path>:<lines> — <what was checked there, on the branch the PR targets>

## Test
<link to the regression test, or rationale for why none applies + manual repro>

<!-- Tracker reference (optional, mirrors the commit-msg trailer). The contribution
     gate lints commit-msg.txt and this PR body INDEPENDENTLY, so a ticketed fix needs
     the id in BOTH. For a ticketed fix, keep the line below in the project's
     [tracker].issue_trailer form (e.g. `Fixes #<id>`). For a declared-ticketless fix
     (no tracker id yet / non-core), OMIT it and state the origin in-body instead, e.g.
     "Reported in <upstream>#<n>; no tracker ticket". -->
Fixes #<id>
