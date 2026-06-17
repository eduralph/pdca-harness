# Step 06 — Sign-off: the §9 verdict

← [05 Check](05-check.md) · [Index](README.md) · next: [07 Publish & Act →](07-publish-and-act.md)

**Beat: Check, completed by the human.** Touch point #2. The bundle is
`AWAITING_SIGNOFF`; you've cleared §6 ([step 05](05-check.md)); now you record the
verdict in §9. This single decision is the only thing standing between a built fix
and a shipped one — by design, no model can make it.

## The four dispositions

```bash
pdca signoff <id> --accept        # the fix is good → COMPLETE, ready to publish
pdca signoff <id> --iterate-do    # right spec, wrong build → rebuild against same brief
pdca signoff <id> --iterate-plan  # the spec itself was wrong → re-author the brief
pdca signoff <id> --discontinue   # this doesn't belong in the cycle → abandon
```

Each maps to a state transition the driver applies the instant you record it:

| Disposition | §9 outcome | New state | What the driver does |
|-------------|-----------|-----------|----------------------|
| `--accept` | `merged-wider` | `COMPLETE` | Freezes the bundle; publish can run |
| `--iterate-do` | `iterated-to-Do` | → `PLANNED` | Archives Do+Check into `iteration-v<N>/`, keeps the brief, re-runs the builder |
| `--iterate-plan` | `iterated-to-Plan` | → `UNPLANNED` | Archives the **whole attempt incl. brief**, you re-spec |
| `--discontinue` | `discontinued` | `DISCONTINUED` | Records §9, no transition, drops from the pending set |

`--accept` is **guarded** (the C6 rule from [step 05](05-check.md)): it's refused
while any §6 item is unchecked. The other three are not — you can redirect or
abandon a bundle with §6 still open. Add `--by "Name"` to set attribution and
`--delta "…"` to record the rationale on an iterate.

> `--discontinue` is the disposition this harness version
> ([v0.18.0](https://github.com/eduralph/pdca-harness/releases)) unified — the
> action, the `discontinued` §9 outcome, and the terminal `DISCONTINUED` state now
> all share one word. Use it for work that, on inspection, doesn't fit the cycle
> (handled out-of-band, duplicate, won't-fix) — a deliberate abandon, independent
> of §6.

## The simple case — accept

For issue 11589 ([steps 03–05](03-plan.md)), the gates proved correctness and the
four §6 items were all cleared, so the maintainer accepted. The real
`results/issue_11589/SUMMARY.md` §9:

```markdown
## 9. Check sign-off                     ← human completes Check here
- Disposition confirmed / overridden:
- Outcome: merged-wider
- Iteration delta (if iterating):
- By / date: Eduard Ralph / 2026-06-06
```

`Outcome: merged-wider` ⇒ state `COMPLETE`. The bundle is frozen and ready for
publish ([step 07](07-publish-and-act.md)).

## The instructive case — iterate, with a real reason

Acceptance is the boring path. The cycle earns its keep on **iteration** — when
the human catches something the gates can't. gramps issue 46 (a GraphView
import-safety fix) took **two** iterations before it was accepted. The brief's
own carry-forward records *why* the first attempt was rejected — this is a real
`--iterate-do` rationale, verbatim:

```markdown
## Iteration 1 — carry-forward (from the previous attempt)
- Sign-off rationale: Reject: the regression test reimplements GTK load/version-
  pinning (test_graphview_import.py:80-82, top-level `import gi` +
  gi.require_version("Gtk","3.0")). GTK loading/pinning is already owned by gramps
  at runtime and by PR 950 in unit testing, so this machinery must not be in the
  addon test — it is redundant and is the likely cause of the two T3 addon-unit
  exit-2 collection aborts. Rebuild the test without any manual gi/Gtk pinning;
  keep the production change (removal of the two import-time raises) as-is.
  Re-run T3 addon-unit and E2E ...
```

What happened mechanically:

1. **Iteration 1** built a fix whose *production change was correct* but whose
   *test* carried redundant GTK-pinning machinery. The C4 gate went green (the fix
   worked), but the human noticed the test introduced two new `[delta]` failures in
   the T3 addon-unit matrix — a regression the test itself caused.
2. The maintainer ran `pdca signoff 46 --iterate-do --delta "rebuild the test
   without the gi pin; keep the production change"`. The driver archived the
   attempt into `iteration-v1/`, reverted to `PLANNED`, and re-ran the builder —
   which got the rejection rationale folded into the brief (above), so it didn't
   repeat the mistake.
3. **Iteration 2** rebuilt the test without the pin. Accepted:

```markdown
## 9. Check sign-off
- Outcome: merged-wider
- Iteration delta (if iterating): [iteration 2 — rebuilt test without gi pin]
- By / date: Eduard Ralph / 2026-06-13
```

The lesson this case teaches about the harness: **the deterministic C4 gate said
"correct," and it was right — but "correct fix" is not "good contribution."** A
test that pins GTK is correct *and* redundant *and* regression-inducing; only the
human reading the §6/§7/review material caught it. The harness didn't prevent the
mistake — it **surfaced it cheaply** (the `[delta]` rows), gave the human a clean
"reject with reason" lever (`--iterate-do --delta`), and **carried the reason
forward** so the rebuild started smarter. That carry-forward loop is the "Plan
with a better baseline" arrow of PDCA, operating inside a single contribution.

State after accept: `COMPLETE`. On to publish and the cross-cycle Act review —
[step 07](07-publish-and-act.md).

← [05 Check](05-check.md) · [Index](README.md) · next: [07 Publish & Act →](07-publish-and-act.md)
