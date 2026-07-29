# Step 03 — Plan: author the brief

← [02 Rehearse offline](02-rehearse-offline.md) · [Index](README.md) · next: [04 Do →](04-do.md)

**Beat: Plan.** Human touch point #1. The output is one artifact — `brief.md` —
the contribution spec the rest of the cycle is measured against. A bundle with no
`brief.md` is `UNPLANNED`; once the brief exists it's `PLANNED` and ready for Do.

Two parts follow: [**how to use**](#how-to-use) it — the commands, and what a
good brief contains — then [**how it works**](#how-it-works) underneath: the
leaves involved, the guards around the beat's exit, and why the brief has the
shape it does.

## How to use

The Plan leaf is **interactive** — it opens Claude in your terminal so you and the
planner co-author the brief from a tracker issue. gramps configured it to read a
single Mantis CSV row for the issue id:

```bash
pdca flow 11589           # opens the planner; you converge on a brief, then the
                          # driver continues unattended into Do + Check
```

**This command starts one full cycle, not just Plan.** There's no standalone
command that runs Plan alone and stops. `pdca flow` opens the planner's
interactive session, and the moment a brief exists, that same invocation falls
straight through into the unattended Do → Check band — there's no
`--plan-only` flag to stop it there. `pdca run <id>` doesn't help either: it
never advances an `UNPLANNED` bundle at all (there's nothing for the driver to
do without a brief), so it can't trigger Plan in isolation. The nearest thing
to isolating Plan is to author `brief.md` yourself, outside the harness, and
seed it with `pdca init-issue <id> --from-brief path/to/brief.md` — but that
bypasses the planner leaf entirely rather than running it.

Under the hood the interactive session itself is the `planner` leaf from
[step 01](01-render-and-integrate.md), writing `results/issue_11589/brief.md`.

### Planning a specific id list in one session

Pass **several ids** to `pdca flow` to plan + drive exactly that set — e.g. bundles you
seeded from per-bundle triage notes, not a tracker CSV:

```bash
pdca flow 11589 12030 12044 
```

Any UNPLANNED id in the list is briefed in **one shared interactive session** before
the drive; already-briefed ids skip Plan. Ids the planner chooses to skip are left
alone. `--from-csv` seeds the Plan source; with **no ids**, `pdca flow --from-csv PATH`
plans a batch the planner picks from the export.

Each bundle's tracker thread can be fetched automatically: set `[tracker].notes_cmd`
in `pdca.toml` to your scrape tooling (a `{id}` shell template that writes
`issue_<id>/notes.json`; `$PDCA_BUNDLE` is the bundle dir). The harness runs it before
any Plan beat when `notes.json` is absent, so the planner has the comment thread
without you scraping by hand. It's best-effort — a failure just falls back to the
CSV / asking you.

### Composing several sources

A good brief often draws on more than the ticket — a linked design doc, an accepted
proposal, a spec section, a CSV row. Declare a list of `[[plan.source]]` providers in
`pdca.toml` and each contributes context into the bundle's `sources/` dir before Plan,
so the planner briefs from the **full** picture, not one scrape (issue #102). Built-in
types: `github` (`gh`), `gitlab` (`glab`), `csv`, `file` (a path/glob, `{id}`
interpolated), and `command` (the escape hatch — exactly `notes_cmd`, run with
`$PDCA_BUNDLE` / `$PDCA_SOURCES` set). Each is best-effort; the legacy `notes_cmd` still
runs alongside them — **unless** a source sets `role = "tracker"`, which makes that source
the tracker thread (it writes `notes.json` itself) and suppresses `notes_cmd` so the issue
is sourced once, not fetched and stored twice (issue #132). `notes_cmd` and a tracker-role
plan.source are mutually exclusive. For example, "the GitHub issue **and** its linked ADR":

```toml
[[plan.source]]
type = "github"
[[plan.source]]
type = "file"
path = "docs/adr/*{id}*.md"
```

### Ordering and routing the batch

When a batch has **dependencies**, you don't run it by hand in waves — you *declare* the
shape and the driver schedules it. Optional brief fields, set at Plan:

- **`Depends on:` / `Conflicts with:`** — the batch runs as dependency **waves**
  ([09 parallel lanes](../template/PCDA/quality-cycle/09-parallel-lanes.md)): a bundle
  lands in a later wave than its prerequisites and builds on their *accepted* result (the
  wave driver folds each wave onto the base the next builds on — no human merge between
  them). `Conflicts with` puts two file-overlapping bundles in different waves.
  (`Depends on (merged)` / `Stacks on` still parse but are deprecated — in the wave model
  they are just `Depends on`.) **`Ordering note:`** is the free-text sibling of these two —
  *why* the scheduling is set as it is (e.g. "depends-on 12 because both edit cache.py").
  Not machine-parsed; it just keeps the human's reasoning next to the bare-id fields for
  the next person (including future-you) reading the brief.
- **`Difficulty:` / `Do model:`** — route this bundle's Do to the right backend
  ([step 04](04-do.md)): `Difficulty` feeds the `when` routing (and the iterate escalation
  ladder); `Do model` pins a backend by name. So different bundles in one wave can build
  on different models.

Declaring the constraints is a Plan judgment — the planner sets them from the batch's real
dependency/conflict structure; an unschedulable graph (a cycle, or a dep that is neither in
the batch nor already COMPLETE) is rejected up front. `pdca waves <ids…>` prints the
computed wave plan without building.

### What a real brief looks like

This is the **actual** brief the planner produced for gramps issue 11589, a
PluginManager bug (trimmed for length — the real file has fuller prose in a few
fields; nothing quoted below is altered). Read it as a checklist of what a good
spec contains for *this* fix — not every optional field fires on every bug; see
below for which ones sat this one out and why.

```markdown
# Brief — issue 11589 / pluginmanager-uninstall-destroys-shared-dir

- **Slug:** pluginmanager-uninstall-destroys-shared-dir
- **Defect:** In the enhanced Plugin Manager addon, uninstalling a single add-on
  filter rule deletes the **entire** shared `FilterRules` plugin directory — all
  the other rules in the pack *and* any unrelated user content the user placed
  there. Root cause: `__uninstall` calls `shutil.rmtree(pdata.fpath)` on the
  plugin's *directory* (`PluginManager.py:349`), but `pdata.fpath` is the
  directory shared by every plugin in a multi-plugin pack ...
- **Success criterion:** Uninstalling one plugin whose directory is shared by
  other registered plugins removes only the files belonging to the selected
  plugin; the sibling plugins' files and any unrelated files/sub-folders survive,
  and the directory itself is **not** removed while other registered plugins still
  live in it. (Uninstalling a plugin that is the sole occupant of its own
  directory must still remove the directory, preserving today's behaviour.)
- **Repo + branch target:** addons-source @ `maintenance/gramps60` (addons
  production branch per INTEGRATION §2; maintainer cherry-picks forward to gramps61).
- **Scope:** Make `PluginManager.__uninstall` (`...PluginManager.py:339-358`)
  non-destructive for shared directories ... /
  **out of scope:** repackaging the FilterRules pack; the reporter's secondary
  observation; the core plugin manager; changing the install path.
- **Repro instruction:** With the FilterRules pack installed ... select one rule
  and click Uninstall. Observed: the whole directory is deleted. Expected: only
  the selected rule's files are removed.
- **Test file:** `../addons-source/PluginManager/tests/test_uninstall_shared_dir.py`
  (addon convention per INTEGRATION §3 — `tests/` package, `test_*.py` prefix).
- **Citations expected:** Do must cite path:line on the target branch for every change.
- **Prior-art check (triage cycles):** Searched `addons-source` history by file path —
  `git -C ../addons-source log -S "shutil.rmtree" -- PluginManager/PluginManager.py`
  returns only `00794ec32` ("An enhanced addon/plugin manager (#78)"), i.e. the
  `rmtree(pdata.fpath)` uninstall logic is unchanged since the addon was introduced;
  no later fix on any branch. No prior fix or open PR found for this defect.
- **Disposition hint:** likely-fix (Mantis status `confirmed`; root cause confirmed
  in source and corroborated by maintainer prculley in the thread).
```

Fields from the field table in [How it works](#why-each-field-is-there) below
that **don't** appear here: `Falsifiability`, `Invariant to restore`,
`Onto branch`, `Surfaces`, `Difficulty`, `Do model`, `Depends on` /
`Conflicts with` / `Ordering note`, and `External dependencies`. All optional,
and this bug didn't need any of them — it's a single, unbatched fix with no
scheduling constraints, no GUI/E2E surface split, no non-toolchain dependency,
and no ambiguity worth a routing override. You'll see `Depends on` and
`Conflicts with` in the [batch-ordering example](#ordering-and-routing-the-batch)
above, `Difficulty` / `Do model` in [step 04](04-do.md), and
`External dependencies` in [step 01](01-render-and-integrate.md)'s doctor
section — they show up when a brief actually needs them, not by default.

### The Plan can be a pointer

If your project already plans through its own artifacts — an ADR, an enhancement
proposal, a normative spec under change control — you don't restate that here. Use
`templates/plan-pointer.md.tpl`: a thin brief that **references** the host document
(`- **Planning artifact:** docs/adr/0042-thing.md`) and carries only the few fields the
driver parses (slug, success criterion, branch target, test file). Do reads the
referenced artifact as the authoritative plan and cites it; the rest of the cycle is
unchanged. This lets PDCA wrap a host's existing planning process instead of imposing
its own document shape.

---

## How it works

### The leaves Plan uses

Three leaves touch Plan's territory in `pdca.toml`, but only one of them belongs
to the beat outright:

- **`planner`** — Plan's leaf, full stop. Interactive (`interactive = true`): a
  REPL with the human, and the one leaf the model spec itself calls "Plan."
  Everything under [How to use](#how-to-use) above is this leaf running.
- **`splitter`** — also interactive, but the driver never dispatches it —
  **the planner runs it on itself**, inline, mid-session, when the brief it's
  co-authoring turns out to be several slices: `pdca split <id>` drafts
  `split-proposal.md`, then `pdca split <id> --accept` materializes the
  children — both typed by the planner as tool calls in the same REPL the
  human is already sitting in, never a separate command remembered later.
  [Step 07](07-crosscutting.md#size--split) covers the full decomposition
  process; the short version is that splitting is owned by Plan, not bolted on
  after it.
- **`sizer`** — headless, and **not** invoked by the planner or by anything in
  this beat directly. It's called automatically by the driver's pre-dispatch
  policy: once right after Plan exits (a freshly-paid call, before Do), and
  again before Check (reading its stored verdict for free, no second opinion
  bought). It judges `{band, independent_outcomes, proposed_seams, confidence}`
  from whatever `brief.md` says at the time, and its `proposed_seams` is what
  the splitter reads as a starting point when a split does happen. See
  [Entry and exit](#entry-and-exit-the-guards-around-plan) below for exactly
  when it fires.

One thing worth knowing: the vendored model spec
(`../template/PCDA/quality-cycle/03-cycle-automation.md`) still describes "six"
leaves (planner, builder, reviewer, signoff, publisher, act) — `sizer` and
`splitter` are newer than that count and haven't been folded into the spec's own
language yet, even though `pdca.toml`'s `[leaves.*]` tables already ship all
eight.

### Entry and exit: the guards around Plan

**Entry.** A bundle arrives at Plan as `UNPLANNED` — no `brief.md` yet — unless the
tracker already settled the question before anyone wrote one, in which case it's
`RESOLVED` instead and Plan never touches it (the states are covered fully in
[step 00](00-introduction.md)).

**Exit** is `PLANNED` — `brief.md` exists — but reaching `PLANNED` does not, by
itself, buy the bundle a builder. Before the driver dispatches Do, it runs a
**pre-dispatch policy check** against the brief it just got. This is the actual
guard layer, and it's separate from anything in `pdca.toml`'s gates: gates
verify the *built artifact* at Check; this verifies the *brief* before Do is
even allowed to spend a builder on it. (The same check fires again at Do's own
exit, before Check dispatches — that firing belongs to Do, not Plan, and is
covered in [step 04](04-do.md#the-leaf-and-the-guards-around-it).)

#### The two guards

Both are evaluated fresh, every beat — never cached, never pinned by a stale
marker — so editing the brief or registering a missing row un-holds the bundle on
the very next attempt, no re-plan required.

- **The dependency guard** (`[driver].dependency_guard`, default `hold`) —
  **blocking**. Checks every backticked token in the brief's
  `External dependencies` field (above) against the registered
  `[[doctor.checks]]` rows from
  [step 01](01-render-and-integrate.md#1d-install--doctor-verify-the-toolchain).
  A token with no matching row **stops the beat**: the driver raises a
  `PolicyHold`, prints the unregistered token(s) plus the fix (`register a detect
  cmd + install hint in pdca.toml, or annotate it (no-check: …)`), and exits
  non-zero — `pdca run` / `pdca flow` report the bundle as held, not done. This
  isn't a heuristic call: a token either names a registered row or it doesn't, so
  unlike the size guard below, `hold` here is real and is the default. It also
  isn't a *new* block — it moves an existing one earlier. The same unregistered
  dependency already refuses `signoff --accept` through Check's C6 guard
  ([step 05](05-check.md#the-c6-accept-guard)); catching it here spends a human a minute instead of
  spending a full builder + reviewer + adversary pass first. Set
  `dependency_guard = "warn"` to report and proceed anyway, or `"off"` to disable
  it — both go under `[driver]` in `pdca.toml` (not pre-populated there; add the
  key yourself to change the default).
- **The size guard** (`[driver].size_guard`, default `off`) — **advisory only,
  never blocking** — even set to `"hold"`, which the driver accepts but silently
  treats as `"warn"`. That's a deliberate, evidence-based choice, not a gap:
  calibrated over 86 real bundles, the best structural size signal reaches 62%
  precision — nearly one wrong hold per right one — and a blocking gate at that
  rate trains people to override it rather than trust it. With `size_guard =
  "warn"`, an oversized brief prints a note naming which signal fired (structural
  estimate, or the configured `[[leaves.sizer]]` leaf's verdict) and a remedy —
  `pdca split` — and Do dispatches regardless. This is a **backstop**, not the
  normal path — see [The leaves Plan uses](#the-leaves-plan-uses) above and
  [step 07](07-crosscutting.md#size--split) for the default, Plan-owned split
  flow this only catches when it didn't already happen. (The remedy reads
  differently on the guard's second firing, at Do's exit — see
  [step 04](04-do.md#the-leaf-and-the-guards-around-it).)

#### Where it runs, and why

The check lives inside `driver.advance()` itself, not a hook at the literal end
of the Plan beat. That's deliberate: there are four separate code paths that can
walk a bundle from `PLANNED` into Do (`pdca flow <id>`, `pdca flow <ids…>`, the
zero-id batch sweep, and `pdca run <id>` direct), and only one of them is a true
"Plan just finished" hook. Evaluating inside `advance()` covers all four by
construction instead of by enumeration — the same reason it's the right place
for the second firing at Do's exit too, covered in
[step 04](04-do.md#the-leaf-and-the-guards-around-it).

### Why each field is there

A brief is not free-form. Each labelled field is consumed by a later beat, which
is *why* the harness can automate Do and Check at all:

| Field | Consumed by | What would go wrong without it |
|-------|-------------|--------------------------------|
| **Defect** + root cause | Do (where to fix), Check C5 (causal adequacy) | The builder treats symptoms, not the cause |
| **Success criterion** | Check (the validation oracle), §9 sign-off | "Done" is unfalsifiable |
| **Falsifiability** | Plan itself — a self-check, not machine-enforced (unlike the [two guards](#entry-and-exit-the-guards-around-plan) above) | A criterion nothing can ever make go RED; Do burns a cycle "verifying" the unverifiable |
| **Invariant to restore** | Do (the property to guarantee), Check C5 | The fix guards the one reported case instead of the actual defect *category* |
| **Repo + branch target** | Do (branch from), Publish (where the PR goes) | The fix lands on the wrong branch |
| **Onto branch** | Do (branch from), Publish (stacks a commit onto the named PR instead of opening a new one) | A redundant PR opens instead of landing on the one already under review |
| **Surfaces** | Check (routes which runtime gates apply, e.g. an E2E gate only when `gui`) | A GUI-only regression ships because no gate was ever pointed at the surface |
| **Scope / out-of-scope** | Do (stay in lane), reviewer (scope creep is a FAIL) | Scope creep; an un-reviewable diff |
| **Repro instruction** | Check C2 (red pre-fix) | No way to prove the bug existed |
| **External dependencies** | the [dependency guard](#entry-and-exit-the-guards-around-plan) at Plan exit (blocking, by default) + Check §6 as a backstop, reconciled against registered `[[doctor.checks]]` rows ([step 01](01-render-and-integrate.md)) | A missing build tool or service surfaces mid-cycle instead of preflighted — or worse, Do silently works around it |
| **Test file** | Do (ship it here), C4 gate (run it) | The C4 red→green proof has nothing to run |
| **Citations expected** | reviewer + human sign-off (traceability) | An unreviewable "trust me" diff with no `path:line` anchor |
| **Prior-art check (triage cycles)** | Plan itself, sign-off | A duplicate or already-attempted fix reaches Do before anyone checks history |
| **Disposition hint** | sets expectations for sign-off | — |

Notice how **INTEGRATION.md is cited inline** — "addons production branch per
INTEGRATION §2", "addon convention per INTEGRATION §3". That's the payoff of
[step 01](01-render-and-integrate.md): the planner resolves abstract questions
("which branch? where does the test go?") against your repo's concretizations
instead of guessing.

## STOP discipline

Every brief carries the rule that the cycle never ships before sign-off:

> Draft only until Check sign-off. A draft PR MAY be opened for CI; the PR MUST
> NOT be marked ready before sign-off accepts.

The brief is now written. State: `PLANNED`. The driver advances unattended into
the Do beat — [step 04](04-do.md).

← [02 Rehearse offline](02-rehearse-offline.md) · [Index](README.md) · next: [04 Do →](04-do.md)
