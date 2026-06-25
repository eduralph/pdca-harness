# Step 07 — Publish & Act

← [06 Sign-off](06-signoff.md) · [Index](README.md)

Two things remain. **Publish** is the closing work of Check: turn the accepted
bundle into a draft PR. **Act** is the fourth beat — the cross-cycle review that
closes the PDCA loop by improving the *process*, not the contribution.

---

## Publish — contribute the accepted fix

Once a bundle is `COMPLETE`, publish opens it as a **draft** PR on the target
branch from the brief. **Accept publishes by default everywhere** (#97): both
`pdca flow` and a standalone `pdca signoff <id> --accept` run publish on an accept,
so "approved" doesn't silently stay "unpublished"; pass `--no-publish` to opt out
(then it's *deliberately* unpublished, not by accident). A publish that fails is
**loud** — never swallowed. You can also run it standalone:

```bash
pdca publish 11589              # open the draft PR
pdca publish 11589 --dry-run    # print the git/gh plan without pushing
```

`pdca status` (and bare `pdca`) shows each `COMPLETE` bundle's publish state —
`[PR <url>]` when a PR was opened, `[unpublished]` when it wasn't (dry-run /
no-target / failed / not-yet-run), `[close: no PR]` for a close/no-fix bundle — so
an accepted-but-unpublished cycle is visible at a glance.

It writes `publish.json` into the bundle and uses the project's PR conventions
from INTEGRATION §8. For gramps that's a four-section body (Root cause / Fix /
Verified against / Test), and — because 11589 touches an addon — an `## Affected
addon` section that @-mentions the addon's maintainer (INTEGRATION §10).

**STOP discipline holds to the end:** the PR opens as a *draft*. A human marks it
ready — the harness never does. gramps' governance makes this explicit
(INTEGRATION §10): *"Eduard opens fork PRs as draft, marks ready himself; builder
commits and stops — no push/PR-open/ready-mark without explicit instruction."*

This holds for a **batch**, too. A multi-id `pdca flow` runs the batch as dependency
waves ([09 parallel lanes](../template/PCDA/quality-cycle/09-parallel-lanes.md)); in the
default `stack` mode each wave's accepted work is folded onto a run-scoped integration
branch the next wave builds on, and each dependent opens a **stacked** draft PR — the
harness still never merges, so you review and merge the stack bottom-up yourself. Only the
opt-in `[driver].wave_mode = "merge"` (own-repo / CD, where you hold merge rights on the
base) relaxes this, `gh pr merge`-ing each wave before the next builds.

In gramps' git history you can see cycles land this way — each result bundle gets
a branch and a PR:

```
c52c6a4 Merge pull request #105 from eduralph/results/issue-46-graphview-import
f1092ca results(46): record published GraphView import-safety cycle
```

That's one full cycle, [steps 03–07](03-plan.md), from tracker issue to merged
record.

---

## Act — improve the process, not the contribution

Plan/Do/Check operate on **one contribution**. **Act** operates on the **process
itself**, across *many* completed cycles, on its own cadence (every N cycles, or
weekly — *not* per-cycle). It's touch point #3.

```bash
pdca act index                              # read-only: frozen cycles + recurring signals
pdca act log --date 2026-06-12 --append     # scaffold a dated entry into process/act-log.md
```

Act reads the `COMPLETE` bundles' SUMMARYs — especially §6 (what needed a human),
§7 (what stayed unproven), and §10 (Act candidates the cycle flagged) — looking
for **patterns**. A one-off is noise; the same gap across three cycles is a
process defect. What Act explicitly does **not** do: re-decide any disposition,
re-run the suite, or write the next brief.

### A real Act entry

This is from `gramps-testbed-v2/process/act-log.md` (2026-06-12), and it's the
clearest demonstration of why the beat exists. A maintainer reviewing the
`glade-setattr` cycle's published PR was asked to list a new `.py` file in
`po/POTFILES` — a documented rule **no gate owned**:

```markdown
# Act review — 2026-06-12 — cycles considered: glade-setattr (PR #2356 review feedback)

## What the cycles' records exposed
- A documented doc-16 MUST with no owner in the cycle — surfaced by a maintainer
  at review, not by any gate. Nick-Hall asked for the new test file to be listed
  in `po/POTFILES.{in,skip}`. The rule is explicit and sourced ... It is not
  exotic — it simply has no encoding in our T1–T4 ladder.
- The omission fell through every beat. Plan: the brief never stated the
  obligation. Do: the builder has no independent doc-16 checklist. Check/gates:
  T1 is addon-only; T2 checks file *shape*; T4 checks the commit/PR *wrapper* —
  none implement doc 16 §Translation Files. New-file→manifest registration falls
  in the gap between T2 (shape) and T4 (wrapper); neither owns it.

## Process deltas (candidates — routed for engineering, NOT applied here)
- Gate (highest-value): add a bundle-scoped, deterministic check — for a *core*
  bundle whose `patch.diff` adds a `.py` file, assert it appears in
  `po/POTFILES.in` or `po/POTFILES.skip`. Directly implements doc 16:99-100, is
  target-aware (core-only), and needs no Docker. Home: extend `t2_shape.py`.
```

Trace the loop: a human review comment (§ outside the gates) → Act *generalizes*
it ("this will recur on every core bundle that adds a file") → a concrete process
delta (a new gate) → which, once built, means **the next cycle's gates catch it
automatically** instead of a maintainer catching it at review. That is the "→ back
to Plan with a better baseline" arrow made operational.

Another entry shows Act tightening the *spec*, not the gates — the `issue_8653`
cycle shipped a test that exercised a hand-ported copy of production rather than
production itself; only the human's T5 judgment caught it:

```markdown
- Reference (applied): new principle §3.4 — test the production path: the success
  evidence MUST exercise the production path; a parallel re-implementation that
  mirrors production is not acceptable evidence. (docs/principles.md §3.4)
- Spec template (applied): brief Test file field now reminds that the test must
  drive the production path, not a parallel copy. (templates/brief.md.tpl)
```

That second delta is now also a **mechanical** check you can wire in: the reference
`scripts/checks/test_exercises_production.py` (a `[[gates.checks]]` example, issue #154)
asserts each *added* test file imports the production package and declares itself
UNVERIFIABLE → §6 when it can't — promoting "test the production path" from a human-only
T5 judgment to a deterministically-surfaced one.

Act deltas land in five places: the **spec template** (a brief field), the
**ruleset** (a written rule), the **gates** (a new/promoted check), the **agent
files** (`.claude/agents/*.md`), or the **orchestration** (driver/state). Each is
a permanent improvement to the baseline every future cycle starts from.

---

## The loop, closed

```
Plan ─► Do ─► Check ─► (sign-off) ──► COMPLETE ──► publish (draft PR)
  ▲                                      │
  └──────────── Act ◄────────────────────┘
        (every N cycles: turn recurring
         misses into spec/gate/rule deltas)
```

You've now walked one contribution from a tracker issue to a merged, signed-off,
verified fix — and seen how the cycle feeds its own improvement. To do it for
real, go back to [step 01](01-render-and-integrate.md) and render the harness into
your own project; the reference spec in
[`../template/PCDA/quality-cycle/`](../template/PCDA/quality-cycle/) is there when
you want the *why* behind any beat.

← [06 Sign-off](06-signoff.md) · [Index](README.md)
