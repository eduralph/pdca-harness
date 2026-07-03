# PR description (only if a PR is warranted)

> Attachment referenced by SUMMARY.md §8. One logical fix per PR.

## Summary
**User impact:** <in plain language, what the user experiences / sees go wrong — the
symptom and who it hits. This leads the PR and MUST come before Root cause; a reader
who does not live in this file should grasp WHY from this line alone. No internal
jargon.>

<then the one-line change: WHAT this PR does about it.>

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
     [tracker].issue_trailer form (e.g. `Fixes #<id>`); `pdca publish` auto-links the id
     to [tracker].issue_url_pattern (e.g. `Fixes [#<id>](…/view.php?id=<id>)`) so the
     reader can click through to the report — you may write the bare form. For a
     declared-ticketless fix (no tracker id yet / non-core), OMIT it and state the origin
     in-body instead, e.g. "Reported in <upstream>#<n>; no tracker ticket". -->
Fixes #<id>
