# PR description (only if a PR is warranted)

> Attachment referenced by SUMMARY.md §8. One logical fix per PR.

<!-- TWO TIERS. Summary + What to look at are for ANY reader (a triager, a user, a
     reviewer of another area) — plain, everyday language. Root cause / Fix / Verification
     below are for the maintainer — internals and `path:line` belong there, not above. -->

## Summary
**User impact:** <in plain, everyday language, what the user experiences / sees go wrong —
the symptom and who it hits. This leads the PR and MUST come before Root cause; a reader
who has never opened this file must grasp WHY from this line alone. NO code /
implementation jargon (no type/tag/attribute names, no `path:line`) — defer the mechanism
to Root cause below.>

<then the one-line change: WHAT this PR does about it, still in plain terms.>

<close the Summary with a link to the tracker issue, in the [tracker].issue_url_pattern
form — e.g. `Reported in [#<id>](<tracker-url>).` — so the report is one click away from
the top of the PR, not only via the closing trailer below.>

## What to look at
<the crux of the change in plain terms, plus a concrete way to try / reproduce it — enough
for a non-implementor to decide whether to engage. Keep `path:line` and internals OUT of
here; they live in Root cause / Fix / Verification.>

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
     the id in BOTH. Keep the line below a STRICTLY BARE `Fixes #<id>` (the project's
     [tracker].issue_trailer form) — never a Markdown link on the id: GitHub auto-closes
     only on a bare `#<id>` after the keyword, so `Fixes [#<id>](…)` silently fails to
     close the issue on merge. The clickable reference is the `Reported in [#<id>](url)`
     line in the Summary above, not this trailer. For a declared-ticketless fix (no
     tracker id yet / non-core), OMIT it and state the origin in-body instead, e.g.
     "Reported in <upstream>#<n>; no tracker ticket". -->
Fixes #<id>
