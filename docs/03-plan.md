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
planner co-author the brief from a tracker issue. Wyrd configures it to fetch the
GitHub issue thread for you (`[tracker].notes_cmd` runs a one-line script around
`gh issue view` and writes `notes.json` into the bundle) and to read the target's
design corpus — its ADRs, proposals and specs under `../wyrd/docs/design/` — in
place rather than copied (INTEGRATION §2):

```bash
pdca flow 779             # opens the planner; you converge on a brief, then the
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
[step 01](01-render-and-integrate.md), writing `results/issue_779/brief.md`.

### Planning a specific id list in one session

Pass **several ids** to `pdca flow` to plan + drive exactly that set — e.g. bundles you
seeded from per-bundle triage notes, not a tracker CSV:

```bash
pdca flow 778 779
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
CSV / asking you. Wyrd's is `./engine/scripts/scrape-notes.sh {id}`.

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

Wyrd deliberately does *not* wire its design corpus this way: INTEGRATION §2
calls the ADRs, proposals and specs "reference foundation, not bundle
artifacts" — the planner reads them live from the `../wyrd` checkout, so nothing
is duplicated into `results/`.

### A second opinion on the brief — plan-advisory leaves (issue #301)

The reviewer in [step 05](05-check.md) reads the *patch*. A wrong brief is
cheaper to catch before a builder, a reviewer and an adversary have each spent a
pass on it, so the same open-list mechanism exists at Plan: each
`[[leaves.plan_advisory]]` leaf reviews the **brief** (brief + tracker notes +
sources — no patch exists yet) as an antagonist of the plan — wrong root-cause
framing, an unverifiable success criterion, hidden scope, a prerequisite that
cannot resolve, ignored tracker context. Its findings are `- NEEDS-HUMAN —`
bullets in `plan-advisory-<id>.md`; the planner then gets **one** bounded
revision pass over them, and the bundle records `plan-advisory-benefit.json` —
the brief's hash before and after, whether it was revised, the finding count —
which surfaces in SUMMARY §10 so Act can judge over cycles whether plan reviews
pay for themselves. A finding left unrevised lands in §6 for you. Always
advisory, never a gate; off by default. Wyrd's:

```toml
[[leaves.plan_advisory]]
id = "plan-reviewer"
role = "refute the brief: wrong root cause, untestable success criterion, hidden scope"
mode = "command"
family = "codex"
agent = "plan-reviewer"
model = "gpt-6-astra"
effort = "xhigh"
argv = ["codex", "exec", "--sandbox", "workspace-write", "--skip-git-repo-check"]

[leaves.plan_advisory_selection]
mode = "vendor-complement"   # anchor: the PLANNER family (the brief's author)
```

`vendor-complement` makes the pairing automatic: the driver runs the single leaf
whose `family` differs from the *planner's* (pre-Do there is no builder to anchor
on), so plan validation is cross-vendor without per-brief edits. If no leaf
differs it falls back to the first applicable one and files the lapse as a §6
note.

It earns its keep. The brief below carries a "Plan-review response" section
that records what the review changed — two of four findings were about the
success criterion itself:

```markdown
* **The criterion did not falsify the server-error category** it claims. The invariant names
  four response categories; legs 1-3 exercised three. **Leg 4 above is new** — a signed
  `GET /wyrd-bucket?acl` refused `501` — and the old leg 4 (byte equality) is now leg 5.
  Nothing else changed: no leg was weakened, and the honest limit about a real `500` is
  stated rather than papered over.
* **Equality with the tarball's `VERSION` has no PR-time gate.** True, and already declared
  under Verification posture; what was missing is that it need not wait for a tag. The
  `workflow_dispatch` route and the local `cargo xtask dist` comparison are now named there
  as sign-off options. The binding criterion was NOT widened to include them — a criterion
  no gate can evaluate is worse than a declared deferral.
```

A success criterion that names four response categories and tests three is the
kind of hole a builder would have happily built to — and a Check would have
passed. One revision pass, before Do, closed it.

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
- **`Merges with:`** — an *atomic merge group*: bundles that must not land on the base
  separately (a caller-first pair, or issues planned to merge as one unit). Ids only, like
  `Conflicts with`. Declare it on **every** member: the driver only reads the briefs in
  the run, so a group named only by a bundle outside the run is not seen. It changes no
  wave. Under the default `wave_mode = "stack"` you merge the PRs yourself and can take a
  group together, so nothing more happens. Under `wave_mode = "merge"` the driver merges
  each PR on its own, so for now it builds no member it can see: `pdca flow <ids>` refuses
  the run, and the `--from-csv` sweep holds the group and its dependents
  ([07 · Waves in execution](07-crosscutting.md#waves-in-execution); real merge-mode
  support is issue #778).
- **`Difficulty:` / `Do model:`** — route this bundle's Do to the right backend
  ([step 04](04-do.md)): `Difficulty` feeds the `when` routing (and the iterate escalation
  ladder); `Do model` pins a backend by name. So different bundles in one wave can build
  on different models.

Declaring the constraints is a Plan judgment — the planner sets them from the batch's real
dependency/conflict structure; an unschedulable graph (a cycle, or a dep that is neither in
the batch nor already COMPLETE) is rejected up front — `pdca flow` exits 2 before building
anything, naming the bundle and the dependency (or the cycle) and the ways out.
`pdca waves <ids…>` prints the computed wave plan without building.

### What a real brief looks like

This is the **actual** brief the planner produced for Wyrd issue 779 — the S3
gateway sent no `Server` header, so nothing on the wire said which build a
client was talking to. It is the second child of a split ([step 07](07-crosscutting.md#size--split)
tells that story): its sibling, 778, bakes a build identity into the binary;
this slice puts that identity on the wire. Trimmed for length — the real file
is about 320 lines and far more specific in several fields; nothing quoted
below is altered. Read it as a checklist of what a good spec contains for
*this* change.

```markdown
# Brief — issue 779 / s3-server-version-header-stamp

> Plan artifact (docs 02 §PLAN). Do reads ONLY this file (plus the peer callsites cited
> under **Citations expected**). The `- **Label:** value` lines are parsed by the driver.
>
> **Child 2 of 2** from the split of #736 (`results/issue_736/split-proposal.md`). #778 is
> already on your base: it bakes the build identity into the binary, exposes it as
> `wyrd_server::version::BUILD_IDENTITY`, and records it as the `version` field of the `s3`
> role's `role started` log event. This slice puts that identity on the wire. It derives
> nothing.
>
> Plan of record: `docs/design/proposals/draft/0017-blackbox-validation-tool.md`
> §Dependencies ("The gateway advertises no version") and §3 ("The matrix must be keyed to
> the server version"). Read in place in the target checkout — never copied here.

- **Slug:** s3-server-version-header-stamp
- **Kind:** enhancement
- **Defect:** The S3 gateway advertises no version on the wire. Verified on `origin/main`
  at `a801997`: `grep -rn "header::SERVER" crates/gateway-s3/src/` returns nothing, and the
  only header `handle` stamps on every response is `x-amz-request-id`
  (`crates/gateway-s3/src/lib.rs:1552-1556`). Real S3 sends `Server: AmazonS3`; sending
  nothing is the unusual choice. A client cannot tell last month's deployment from today's,
  a captured HTTP exchange does not identify the build that produced it ...
- **Goal:** Every response the S3 front door emits carries `Server: wyrd/<version>`, where
  `<version>` is the identity #778 baked in — success, client error, server error and the
  streaming-GET head alike.
- **Success criterion:** BINDING (demonstrable by C4-verify at Check — no container, no
  cluster, no feature flag): a new integration test spawns the **built `wyrd` binary** as
  an `s3` role (... killed on every exit path) and asserts, against that one child
  process:
  1. **the success plane carries it** — a signed `PUT` that succeeds returns a `Server`
     header;
  2. **the error plane carries it** — an UNSIGNED request refused `403` returns a `Server`
     header. This is the load-bearing leg: an error response is the one most likely to end
     up in a bug report, and it is the leg a per-handler implementation would miss;
  3. **the streaming-GET head carries it** — a signed `GET` of the object just written
     returns a `Server` header. ...
  4. **the SERVER-ERROR class carries it** — a **signed** `GET /wyrd-bucket?acl` is refused
     `501 NotImplemented` by the subresource denylist ... and that response returns a
     `Server` header. ...
  5. **the value is the real build identity, not a placeholder** — each of those four
     header values is EXACTLY `wyrd/` + V, where V is the `version` field of the same child
     process's `role started` JSON event (#778's observable, read from the same stderr
     stream). Byte equality against an independently-observed value, not a shape test: this
     is what makes the criterion unfakeable, and it is why this slice depends on #778.
- **Falsifiability:** RED is producible on the ordinary developer harness Do is pointed at
  — `cargo test -p wyrd-server --test s3_server_version_header`, no Docker, no cluster, no
  feature flag. On the reverted tree there is no `Server` header on any response at all
  ... This instance's `C4-verify` **reverts the production change and keeps the added test**
  (`engine/scripts/run-verify.sh:508-517`), so the test must still COMPILE against the
  reverted tree: drive `env!("CARGO_BIN_EXE_wyrd")` ... Do MUST NOT reference any symbol
  this patch introduces ... from the test file; a test that calls net-new API fails to
  compile on the red leg, which the gate correctly scores `UNVERIFIABLE` (exit 77 →
  SUMMARY §6) rather than as a proven red. ...
- **Invariant to restore:** *Every response the S3 front door emits identifies the build
  that produced it.* Stated over the response CATEGORY — success, client error, server
  error and the streaming-GET head alike — not over one handler and not over one status
  code. SELF-TEST: this cannot be satisfied by touching a single handler, which is the
  point; the gateway already has exactly one place where the property is expressible for
  all of them (`handle`'s single stamp point, `crates/gateway-s3/src/lib.rs:1552-1556`,
  reached by every request because the router is `Router::new().fallback(handle::<G>)`,
  `:205`). ...
- **Repo + branch target:** getwyrd/wyrd @ main
- **Depends on:** 778
- **Conflicts with:** 738, 742
- **Ordering note:** `Depends on: 778` is a genuine build-on dependency, not just
  sequencing: leg 5 asserts byte-equality against the `version` field #778 adds to the
  `role started` event ... Built on a base without #778 this slice cannot go green. ...
  `Conflicts with: 738` — #738 threads `--chunk-size` through `cmd_s3` → `serve_s3_role` →
  `serve_s3_dispatch` → `serve_s3`, and this slice edits `serve_s3`'s body (`cli.rs:2377`).
  `Conflicts with: 742` — #742's scope (f) rewrites the `release.yml` smoke step this slice
  extends, and it is last in the batch, so the overlap is already scheduled apart.
- **Surfaces:** data
- **Difficulty:** medium
- **Scope:** (a) `S3Config` gains a server-identity field, defaulted in `S3Config::new` so
  every existing caller keeps compiling ...; (b) `handle` stamps `Server: wyrd/<v>` at its
  single stamp point, beside the request id; (c) the composition root supplies the real
  value — `serve_s3` sets the field from `wyrd_server::version::BUILD_IDENTITY` ...;
  (d) `docs/design/architecture/14-threat-model.md` records the disclosure decision (see
  Decision 2); (e) `.github/workflows/release.yml`'s existing installer smoke step gains
  the ONE end-to-end check that closes the tracker's "matches `VERSION`" clause ...
  **/ out of scope:** any derivation, build script, normalizer or packaging change — #778
  owns the identity and it is already on your base ...; a configuration knob to suppress or
  coarsen the header (Decision 2 — a follow-up issue if an operator ever asks); ...
- **Repro instruction:** On `origin/main` at `a801997`, in the target checkout:
  `grep -rn "header::SERVER\|\"server\"" crates/gateway-s3/src/` → nothing. ...
- **External dependencies:** none.
- **Test file:** `crates/server/tests/s3_server_version_header.rs` — a NEW file, and the
  patch's **ONLY** added `*/tests/*.rs`. ... Appending to the existing `s3_http_wire.rs`
  would silently degrade the gate to green-only and prove nothing; adding a SECOND
  `*/tests/*.rs` would make it a second discriminator that must also go red with production
  reverted, so do not.
- **Verification posture:** The BINDING criterion above is the default posture — a flippable
  test, red pre-fix and green post-fix at Check. One half IS deferred and is declared here
  so it lands as a pre-declared sign-off item rather than a surprise NEEDS-HUMAN: the
  tracker's "the wire version matches what `cargo xtask dist` stamps into the tarball's
  `VERSION`" clause cannot be observed inside `cargo xtask ci`, because `dist` needs Docker
  and a network ...
- **Citations expected:** Do must cite `path:line` on the base for every change. Peer
  callsites Do MAY open and should mirror: ... the `x-amz-request-id` stamp at the end of
  `handle` ... Stamp the `Server` header there, the same way, for the same reason. ...
- **Prior-art check (triage cycles):** searched by affected path on `origin/main` at
  `a801997`. ... `gh pr list --state all --search "Server header"` → no PR, open or closed,
  has attempted one. The only prior attempt is #736's own four rounds
  (`results/issue_736/iteration-v{1,2,3,4}/`), whose header half was never refuted; this
  brief is that half, re-scoped.
- **Disposition hint:** new-feature
```

Four of those labels — `Kind`, `Goal`, `Verification posture` and
`Production reach` (not shown) — are not in the template's brief. Wyrd's own
templates add them (`Kind` and `Goal` come from its design-proposal form, the
other two sit in its brief template beside a `Track` field); the driver parses
none of them, the planner, builder and reviewers read them. That is the designed
seam: the template fixes the fields the *machine* consumes, and a project adds
what its *reviewers* need. How to add a field without fighting the next
`copier update` is in [step 10](10-upgrading-an-instance.md#the-templates-folder).

Fields from the field table in [How it works](#why-each-field-is-there) below
that **don't** appear here: `Onto branch`, `Do model`, `Merges with`,
`Depends on (merged)` and `Stacks on`. All optional, and this change didn't need
any of them — it opens its own PR rather than landing on an existing one, its
default builder is the right one, and it is scheduled by a plain dependency
rather than an atomic merge group. `External dependencies` is declared `none`
explicitly: the brief even explains why the cargo dependency the build fetches is
not one (a human never installs it). You'll see `Do model` in
[step 04](04-do.md), and `External dependencies` in
[step 01](01-render-and-integrate.md)'s doctor section — they show up when a
brief actually needs them, not by default.

### The Plan can be a pointer

If your project already plans through its own artifacts — an ADR, an enhancement
proposal, a normative spec under change control — you don't restate that here. Use
`templates/plan-pointer.md.tpl`: a thin brief that **references** the host document
(`- **Planning artifact:** docs/adr/0042-thing.md`) and carries only the few fields the
driver parses (slug, success criterion, branch target, test file). Do reads the
referenced artifact as the authoritative plan and cites it; the rest of the cycle is
unchanged. This lets PDCA wrap a host's existing planning process instead of imposing
its own document shape. Wyrd's INTEGRATION §6 says its Plan *is* "a set of existing
artifacts" — the linked ADR, proposal or spec — and the brief above shows the
hybrid in practice: a full brief whose "Plan of record" is proposal 0017, read in
place, with the brief adding only what that proposal leaves to one Do cycle.

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
  process, with the split that produced the brief above; the short version is
  that splitting is owned by Plan, not bolted on after it.
- **`sizer`** — headless, and **not** invoked by the planner or by anything in
  this beat directly. It's called automatically by the driver's pre-dispatch
  policy: once right after Plan exits (a freshly-paid call, before Do), and
  again before Check (reading its stored verdict for free, no second opinion
  bought). It judges `{band, independent_outcomes, proposed_seams, confidence}`
  from whatever `brief.md` says at the time, and its `proposed_seams` is what
  the splitter reads as a starting point when a split does happen. See
  [Entry and exit](#entry-and-exit-the-guards-around-plan) below for exactly
  when it fires.

A fourth, the `[[leaves.plan_advisory]]` pool above, is dispatched by the driver
right after the planner exits and before Do — an antagonist of the brief, not a
co-author of it.

One thing worth knowing: the vendored model spec
(`../template/PCDA/quality-cycle/03-cycle-automation.md`) still describes "six"
leaves (planner, builder, reviewer, signoff, publisher, act) — `sizer`,
`splitter` and the advisory pools are newer than that count and haven't been
folded into the spec's own language yet, even though `pdca.toml`'s `[leaves.*]`
tables already ship all of them.

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
  non-zero — `pdca run` / `pdca flow` report the bundle as held, not done.
  Registration is only half the contract: the guard then **runs** the named
  rows' detect `cmd`s — and only those, never the instance's wider doctor
  inventory — so a registered row whose cmd exits non-zero holds the same way,
  quoting that row's own `hint`. A dependency can't be discharged by
  registration alone on a machine that doesn't have it; keep detect cmds cheap
  and side-effect-free, because they run every beat the policy is consulted,
  not just on `pdca doctor`. This
  isn't a heuristic call: a token either names a registered row or it doesn't,
  and a detect cmd either exits 0 or it doesn't, so
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

Notice how the brief **grounds itself in the project's own rules**: the target
`getwyrd/wyrd @ main` is INTEGRATION §2's rule for single-slice work (one trunk,
no maintenance branches to guess at); the plan of record is a proposal in the
target's design corpus, read in place as INTEGRATION §2 asks; and the `Test
file` reasoning quotes the instance's own verifier
(`engine/scripts/run-verify.sh`) to explain why the test must be a *new* file.
That's the payoff of [step 01](01-render-and-integrate.md): the planner resolves
abstract questions ("which branch? where does the test go? what counts as red?")
against your repo's concretizations instead of guessing.

## STOP discipline

Every brief carries the rule that the cycle never ships before sign-off:

> Draft only until Check sign-off. Pushing to a feature/draft branch and opening a
> draft PR MAY happen during the cycle. The PR MUST NOT be marked ready before
> sign-off accepts.

The brief is now written. State: `PLANNED`. The driver advances unattended into
the Do beat — [step 04](04-do.md).

← [02 Rehearse offline](02-rehearse-offline.md) · [Index](README.md) · next: [04 Do →](04-do.md)
