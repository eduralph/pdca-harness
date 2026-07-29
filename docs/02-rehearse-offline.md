# Step 02 — Rehearse offline

← [01 Render & integrate](01-render-and-integrate.md) · [Index](README.md) · next: [03 Plan →](03-plan.md)

Before any beat touches a real model or runs a live gate, drive the **whole
control flow offline** with stub leaves and stub gates. This is the single most
valuable habit when adopting the harness: it proves the *plumbing* (state
transitions, artifact assembly, archiving on iterate) independent of any model
behaviour, in well under a second, for free.

## The toy bundle

The template ships a worked brief at `examples/toy/brief.md` whose only purpose is
to let the driver run end-to-end with nothing wired up:

```markdown
# Brief — issue TOY / off-by-one-in-pager

> A worked toy brief so the driver runs end-to-end offline (stub leaves/gates).
> Replace with real briefs from your Plan beat. The field labels are what the
> driver parses — keep the `- **Label:** value` shape.

- **Slug:** off-by-one-in-pager
- **Defect:** The result pager shows N-1 of N items; the last item is dropped.
- **Success criterion:** Pager shows all N items; a test over a 3-item list asserts 3 are rendered.
- **Repo + branch target:** example-repo @ main
- **Scope:** Fix the pager's range bound. / out of scope: pager styling, pagination size.
- **Repro instruction:** Load a 3-item fixture, open the pager, observe 2 items shown.
- **Test file:** test_toy.py
- **Citations expected:** Do must cite path:line on main for the changed bound.
- **Disposition hint:** likely-fix
```

Note the `- **Label:** value` shape — those labels are exactly what the driver
parses out of every brief, including your real ones.

## Drive it, beat by beat

```bash
# seed a bundle from the toy brief  →  state: PLANNED
PYTHONPATH=src python -m pdca_harness.cli init-issue TOY --from-brief examples/toy/brief.md

# advance until the driver halts     →  Do (stub) → gates (stub) → reviewer (stub)
#                                        → assemble SUMMARY → state: AWAITING_SIGNOFF
PYTHONPATH=src python -m pdca_harness.cli run TOY

# see where it stopped
PYTHONPATH=src python -m pdca_harness.cli status
```

`run` advances the bundle until it reaches a **halted** state. With stubs, the
builder/reviewer leaves emit canned artifacts and the gates report canned passes,
so the bundle sails to `AWAITING_SIGNOFF` — the driver's "your turn, human" stop.
From there you record a verdict exactly as you would on a real cycle (this is
[step 05](05-check.md#signing-off--the-four-dispositions)):

```bash
PYTHONPATH=src python -m pdca_harness.cli signoff TOY --accept     # → COMPLETE
# or try the others to watch the state machine archive + loop:
PYTHONPATH=src python -m pdca_harness.cli signoff TOY --iterate-do    # → archives, back to PLANNED
PYTHONPATH=src python -m pdca_harness.cli signoff TOY --iterate-plan  # → archives brief too, back to UNPLANNED
PYTHONPATH=src python -m pdca_harness.cli signoff TOY --discontinue   # → DISCONTINUED (terminal)
```

## The one-shot rehearsal

Typing the beats individually is good for *seeing* the state machine. To rehearse
the **same orchestration `pdca flow` uses** — Plan → Do → Check → sign-off →
publish — but entirely stubbed, use the `--rehearse` flag:

```bash
pdca flow TOY --rehearse
```

This forces `PDCA_LEAVES_MODE=stub`, stubs the gates, dry-runs the publish, and
writes to a throwaway bundle root (`.rehearse/`, not your real `results/`). No
Claude, no Docker, no network — just the control flow, instantly. gramps used
this constantly while building out its `engine/` gates, because it answers "did I
break the driver?" without "is the model having a bad day?" muddying the signal.

## What you've proven

After a green rehearsal you know the **plumbing** is sound: states derive
correctly from files, the SUMMARY assembles, iterate archives into
`iteration-v<N>/`, and the sign-off transitions fire. Now — and only now — is it
worth pointing the beats at real models and real gates. The next five steps walk
one **live** gramps cycle through each beat.

← [01 Render & integrate](01-render-and-integrate.md) · [Index](README.md) · next: [03 Plan →](03-plan.md)
