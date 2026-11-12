---
created: 12.11.2026 13:53
type: proposal
status: draft
author: Eduard Ralph
tracking-issue: https://github.com/eduralph/pdca-harness/issues/904
tags:
  - proposal
  - act
---
# Proposal: Act architecture review — a second Act leaf fed by a deterministic collector

> Enhancement proposals (KEP/RFC-style) are how larger changes and milestone plans are
> proposed. Move this file from `draft/` to `accepted/` when accepted; an accepted proposal
> is never edited — a later proposal supersedes it, recorded in `../proposals/README.md`.
> If a decision is foundational rather than a change, it may instead warrant an ADR.

## Motivation

Act improves the **process**. Its leaf reads frozen bundles' §6, §7 and §10 and produces
process deltas: a spec field, a rule, a gate or an agent skill
([`template/agents/act.md.jinja`](../../../../template/agents/act.md.jinja)). It never reads
the code. A design issue reaches Act only if a cycle happened to write it into its record,
and then Act only names it and hands it to the human to schedule. Nothing in the cycle
looks at the system as a whole, so two kinds of architectural signal go unseen.

**Drift between the documented design and the code.** Every bundle's Check sees only its
own patch. #904 records the evidence from wyrd-pdca. A manual run on 2026-11-09 over 633
commits found five design/code mismatches in Wyrd's arc42 docs that every Check had passed
(getwyrd/wyrd#1052 to #1056). Among them:
- a building-block view naming crates that do not exist;
- a container diagram still showing a metadata store that ADR-0042 replaced;
- a composition-root crate doing far more than the design says. Only the change history
  showed this; the dependency graph looked clean.

**Clusters of follow-up issues on one subsystem.** Each cycle files its own follow-ups, and
each is right where it was found. A run of them against one part of the code is a design
signal, but no single cycle can see it. Example: what a flow does when `main` moves mid-run
was handled one cycle at a time, in #765, #893, #756, #757 and #626. The shared cause, that
a run never pins its base commit, was named only on 2026-11-12 and filed as #946–#949, after
a maintainer merged six Dependabot PRs into a target repository during a running flow and
broke it.

The maintainer decided the shape on #904: **two Act leaves**, one for process (today's) and
one for architecture, rather than one widened Act.

## Design

The principles are the harness's own. Deterministic code gathers the facts, a model
interprets them, and the human decides
([ADR-0004](../../adr/0004-no-model-writes-the-decision.md)). Every fact that must be right
every time, including the four traps from the #904 test run, is computed by tested code.
The model only gets an evidence file it cannot fabricate.

### 1. Collector: `pdca act arch-index` (deterministic, read-only)

The counterpart of `pdca act index`. It writes a reproducible facts file plus a rendered
`.md` for a review window. It is pure stdlib
([ADR-0008](../../adr/0008-stdlib-only-python.md)) and reaches git and the tracker through
subprocesses, as the driver already does.

**Snapshots are immutable and published atomically.** Each run builds the complete snapshot
(JSON, rendered `.md`, copied design docs) in a temporary sibling directory and validates it.
It fsyncs the files, then renames the directory to
`process/arch-index/<UTC timestamp>-<content sha256[:12]>/` in one step. A crash leaves only
a temporary directory, which the next run removes, never a partial snapshot. A published
snapshot is never overwritten. The JSON records:
- the target commit it read;
- the review window;
- the time of the tracker query;
- the `[act.architecture]` config it ran with.

A review cites its snapshot by that name, so evidence a past review cited is never
replaced.

The `design_docs` are **pinned to the same commit**. The collector expands each configured
path at that commit with `git ls-tree -r -z <commit> -- <path>`, so a directory such as the
arc42 or ADR folder yields its files. It reads each blob with `git show <commit>:<file>` and
copies it into the snapshot directory with its sha256,
and the leaf reads those copies, never the live files. Structure, history and design
therefore always come from one commit, and a review can be reproduced from its snapshot
alone.

**One collector at a time, with recoverable leftovers.** A collector run holds Act's
session lock (the cross-platform helper in `act.py`, see Routing below) from start to
publication.
- Its staging directory and throwaway worktree carry a sibling owner file holding the pid
  and start time. This is modelled on the lane worktrees' owner files (`worktree.py`),
  which record the bundle instead.
- Under the lock, a run removes a leftover staging directory or worktree, including its
  `.git/worktrees` registration, only when the stamped owner is provably dead.
- A killed run therefore leaves debris that the next run clears, and a concurrent run never
  removes another run's live work.

**Read-only by construction.** The collector never runs anything in the instance's or the
target's working checkout:
- Git history is read with plumbing commands only.
- `structure_command` runs in a throwaway detached worktree of the recorded commit, removed
  afterwards. Its documented defaults use locked and offline modes, e.g.
  `cargo metadata --format-version 1 --locked --offline`.
- A worktree alone confines nothing, because the command could write outside its directory
  or into the shared `.git`. So the command also runs inside the OS sandbox the harness
  already uses for read-only leaves, with only the throwaway worktree and a scratch
  directory writable.
- As detection on top of the sandbox, the collector compares the primary checkout's
  `git status --porcelain` and `git for-each-ref` output before and after the command.
- A command that fails in that mode, or leaves its own worktree dirty, is recorded as a
  failed-structure row. It is not retried without the lock or the sandbox.
- A command that **changed the primary state** despite the sandbox **aborts the run**:
  nothing is published, and the exact `git status` and ref differences are printed. The
  collector does not try to undo them; reverting changes in an operator's checkout
  automatically is a larger risk than stopping. See open question 6.
- Where the sandbox is unavailable, the structure part is skipped and reported, and the
  rest of the index still runs.

**Instance-supplied inputs** (`pdca.toml`, new `[act.architecture]` table):

- `components`: a path → component mapping, e.g. `{ "server/**" = "server", ... }`. This is
  the only real input a human writes. It was about 25 lines for Wyrd in the #904 run.
- `structure_command`: the argv that prints the as-built dependency graph, plus its format.
  For Rust that is `cargo metadata --format-version 1`, `edges` from the workspace graph;
  for Python, an import graph.

  The harness defines the format it reads and the instance supplies the command (template
  vs instance boundary).
- `design_docs`: paths to the documented design (arc42 directory, ADR directory, C4
  diagrams).
- `bot_authors`: authors excluded from change history. Default: `dependabot[bot]`,
  `github-actions[bot]`.
- `test_paths`: globs that mark test code. Defaults per ecosystem, e.g. `**/tests/**`.
- `labels`: an `area:` label → component table, for placing issues (see 3 below).
- `trusted_authors`: accounts whose provenance markers and cross-references count. Default:
  the instance's maintainers and its bot accounts.

**Facts it computes**, each row with a stable id so the model can cite it:

1. **Structure.** Component → component edges from `structure_command`. The edges the
   documented design names (where the docs make them machine-readable) are compared with
   these.
2. **Change history, with the four #904 traps handled in code:**
   - commits per component counted with `git rev-list --count -- <paths>`, never by summing
     file revisions (trap 1: a 3× overcount);
   - `bot_authors` filtered out before any coupling is computed (trap 2: Dependabot pin
     bumps showed as 60–75 % workflow coupling);
   - source and `test_paths` counted separately (trap 3: a crate looked coupled to the crate
     holding its end-to-end tests);
   - coupling reported per time window, at least the review window next to all history,
     with the dates of joint commits (trap 4: coupling from a finished build-out phase).
3. **Tracker clusters.** Issues opened or updated in the window, mapped to components. Free
   issue text cannot be the source:
   - Harness producers do not write `path:line` citations or a fixed follow-up phrase
     reliably: the Act leaf's routing (`template/agents/act.md.jinja:24-44`) requires only a
     link, and the triage bot's issue body can omit a path.
   - Any issue author can type either pattern into a public issue.

   So the collector uses only **authenticated or harness-written provenance**:
   - **Labels.** An `area:` → component table (`[act.architecture].labels`). A label counts
     only if the issue's author is in `trusted_authors`, or the issue timeline's `labeled`
     event for it was made by a maintainer.
     - ADR-0017's triage bot picks `area:` with a model reading the untrusted issue text, so
       a label only the bot set does not count. Otherwise a stranger could steer several
       reports into one cluster.
     - Once a maintainer sets or re-sets the label, it counts.
   - **A provenance marker.** Every harness producer that files a follow-up writes a
     marker: the Act leaf, Check carry-forwards, `pdca triage`, and the routed items of this
     review. The marker is `<!-- pdca:follow-up-of #N paths=<p1>,<p2> -->`, and phase 1
     updates each producer to write it. The marker is counted only on issues whose author is
     in `[act.architecture].trusted_authors` (default: the instance's maintainers and its
     bot accounts), so a stranger cannot fake it.
   - **Tracker cross-references.** GitHub's issue timeline cross-reference events, filtered
     to the same `trusted_authors`, link a follow-up to its origin when no marker exists.

   Output: follow-ups per component per window, the chains themselves, and a count of
   in-window issues no rule could place. That last count is reported, not hidden.
4. **Previous findings.** The findings and routing of the last architecture review, so a
   recurrence is visible. The frontier works like Act's (#299).
5. **Unmapped paths.** Every changed path that matches no component is a row of its own,
   so a stale `components` mapping shows up instead of shrinking the review silently.

### 2. Leaf: `act-architecture`

A role in the shape of ADR-0011 (planned, #716): a prompt, an input manifest and an output
schema. The **input manifest** is the facts file and the `design_docs`, nothing else; not
the bundles, which are the process leaf's input.

**Output schema.** A list of findings, each with:
- `kind`: `drift` | `hotspot` | `follow-up-cluster` | `boundary-question` | `stale-mapping`;
- `component`;
- `subject`: a structured, stable locator of what the finding is about, chosen from
  locators **the collector generated**. The leaf picks one; it never writes one:
  - a dependency edge `a → b`;
  - the root issue of a follow-up chain;
  - a design-doc element, given as file plus heading path (e.g.
    `05-building-block-view.md#Level 2/client`), which the collector lists for every
    pinned design doc;
  - an unmapped path prefix;
  - a history metric: component, metric (`commits` | `coupling:<other component>`), source
    or test, and window, for hotspot and coupling findings found from history alone.
- `evidence`: one or more facts-file row ids;
- `claim`: one sentence;
- `route`: `tracker` | `proposal` | `adr` | `design-doc-fix`.

The runner validates this at the boundary. **A finding with no evidence row, or citing a row
that does not exist, is rejected**, the per-finding evidence contract of proposal 0001
applied to this leaf.

**Session.** Interactive, like `[leaves.act]`: the leaf presents the validated findings and
the human routes each one. The result is a dated entry in `process/architecture-log.md` and
the tracker issues the human agreed to file, opened by the human or by the leaf
with the human's agreement, exactly as the process leaf does today.

**Ledger.** `process/architecture-ledger.json` mirrors `process/act-ledger.json`. It is
keyed by a **signal id per finding**, not by component and kind; two unrelated `drift`
findings on one component must not collide. Code derives the id as `kind` plus `subject`,
never from the free-text `claim`, so a paraphrase of the same finding keeps its id. Two
different findings on one design-doc element are told apart by the element's heading path.
If two findings in one review still share an id, the runner rejects the second and asks
the leaf to merge them.

`component` and `kind` are attributes on the entry. A signal that returns after its routed
fix is flagged as an ineffective route, as the process ledger flags an ineffective delta.

**The log is the source of truth; the ledger is derived.** Every route's signal id, issue
number and state are in `architecture-log.md` (below). The ledger is rebuilt from the log
at the start of every session and after every recorded route. A crash between the two
therefore never loses a route: the next session rebuilds the ledger before it decides
whether to reuse an issue.

**Routing is a transaction**, following the `act log --append` transaction (#299):
1. One session at a time holds Act's existing session lock, through Act's cross-platform
   lock helper (`act.py`: `flock` on POSIX, `msvcrt.locking` on Windows). The process and
   architecture sessions therefore never run concurrently.
2. Each route gets a deterministic id: the snapshot name plus the finding's signal id.
3. **An open route is reused.** Before filing, the runner looks the signal id up in the
   ledger and re-reads the current state of the issue last filed for it.
   - If that issue is still open, the route links the new evidence to it, as a comment
     carrying the same marker, instead of filing again.
   - If it is closed, the signal is a recurrence. A new issue is filed and the ledger flags
     an ineffective route.
4. The route is written to the log as `pending` **before** anything is filed.
5. **Every route is filed as a tracker issue**, whatever its destination: `tracker`,
   `proposal`, `adr` or `design-doc-fix`. The issue states the destination, and writing a
   proposal, ADR or doc fix is later work that starts from that issue, as every change in
   this repository does. The session itself writes no repository document, so the tracker
   is the only side effect to recover. The issue carries `<!-- pdca:arch-route <id> -->`,
   and the returned number is written to the log right after.
6. On a retry, a route with a recorded number is skipped. A `pending` route is first looked
   up on the tracker by its marker, so an interrupted session never files twice.
7. The frontier (the snapshot last reviewed) advances only after every route of the session
   is recorded.

The process leaf is unchanged.

### 3. Cadence and trigger

- **Cadence:** `[act.architecture].cadence`, counted in Act reviews (#904 suggests roughly
  every third), independent of `[driver].act_cadence`. Like the rest of the harness's state
  ([ADR-0003](../../adr/0003-file-derived-bundle-state.md)), whether a review is due is
  **computed from files, not kept in a counter**:
  - Each completed entry in `process/architecture-log.md` records the **process-review
    frontier** it ran after: the number of process review entries in `process/act-log.md`
    at that moment. Completed means every route is recorded.
  - The due count is the number of entries in `act-log.md` now, minus that recorded
    number. Dates are never compared: `act-log.md` headings carry only a date
    (`act.py:634`), and several reviews can share one.
  - If that count is at least `cadence`, the review is **due**.

  This counts every way a process review completes: `act log --append`, and `pdca flow`'s
  `leaves.run_act`, which appends the entry itself. No counter can be lost to a crash or a
  concurrent session, and an aborted architecture review stays due.
  - **In `pdca flow`.** The due check runs at **every** post-COMPLETE Act slot, on its own,
    not only when `[driver].act_cadence` makes the process leaf run. When both are due, the
    process leaf runs first; when only the architecture review is due, it runs alone.
    `--no-act` skips both.
  - `pdca act index` prints "architecture review due" with the count.
- **Trigger:** every `pdca act index` runs only the tracker-cluster part of the collector.
  That part is cheap: no model, no worktree, no `structure_command`. It writes no snapshot
  and checks one threshold: `follow_up_threshold` follow-ups against one component within
  `follow_up_window` (defaults to be set in phase 3, from the dry runs). Crossing it adds a
  line to the Act index recommending an architecture review. It never starts the leaf
  itself.
- `pdca act arch-review` runs it on demand. `enabled = false` turns the whole thing off, for
  an instance whose target has no documented design.

## Alternatives considered

- **Widen the process leaf.** Rejected by the maintainer on #904. One prompt would mix two
  scopes, could not run on two cadences, and Act's narrow scope is deliberate.
- **Let the model gather the facts itself** (give the leaf git and `gh`). The #904 run
  showed that each of four traps produced a wrong result before a manual cross-check caught
  it. A model would make them again, differently each run, and its evidence would not be
  reproducible.
- **Off-the-shelf tools only** (code-maat, `cargo depgraph`, dependency-cruiser). They do
  not handle the traps, are per-ecosystem, and code-maat is not stdlib Python. The collector
  borrows their method, not their code.
- **Architecture rules as gates only.** That is #905, which is complementary. Gates stop
  known drift from returning; this review finds drift nobody has written a rule for yet.

## Graduation criteria

Three phases, each usable on its own.

1. **Collector.** Unit tests for each trap on a fixture repository (bot commits, a tests
   directory, an old joint-commit phase, grouped revisions). An acceptance test replays two
   known answers:
   - On the 2026-11-09 Wyrd snapshot, the index must show the composition-root crate as the
     top hotspot by real commit count (getwyrd/wyrd#1054), and must not show the workflow
     coupling once `bot_authors` is applied.
   - On this repository's tracker, it must show the moving-`main` follow-ups (#765, #893,
     #756, #757 and their chains) as one cluster on the integrate/flow component, through
     their `area:orchestration` label and cross-references by trusted authors, since they
     predate the marker.

   Tests also cover:
   - an issue by an untrusted author carrying a forged marker, which is not counted;
   - a second run on the same day, which leaves the first snapshot byte-identical;
   - a `structure_command` that dirties the worktree, which yields a failed-structure row
     and leaves the target checkout untouched.

   Both are hand-checked answers, so they serve as regression tests.
2. **Leaf.** Schema validation rejects an evidence-free finding (test with a stubbed leaf
   output). One live run each on wyrd-pdca and pdca-pdca, reviewed by the maintainer, is
   recorded in `architecture-log.md`.
3. **Cadence and trigger.** Tests for the cadence count and the threshold line in `act
   index`. Threshold defaults set from the phase-1 replays.

Done when an instance can turn it on with only `[act.architecture]` filled in, and the docs
(`docs/06-act.md`, `docs/09-configuration-reference.md`, architecture §5/§6) describe it.

## Security impact

The collector reads the tracker, and **issue text is untrusted input**
([ADR-0017](../../adr/0017-issue-priority-and-automated-triage.md) §4). It trusts nothing
an issue author can write:
- Components come from labels, which only people with triage rights set.
- Provenance markers and cross-references count only on issues by `trusted_authors`.
- The markers are parsed with one fixed pattern, and only `#N` numbers and paths that match
  a `components` glob are kept.

The facts file holds paths, numbers, issue ids and counts, never prose. The leaf therefore
sees no issue body, and an issue author can neither address it nor add an issue to a
cluster. A trusted author's account being compromised is out of scope here, as it is for
the rest of the harness.

`structure_command` is instance configuration, trusted like every other `pdca.toml` argv.
The collector still runs it only in a throwaway worktree with locked defaults and a
cleanliness check, so a misbehaving command cannot change the target's checkout.

The leaf runs interactively with the human, like the process leaf, with no credential the
process leaf does not already have. Anything it would file goes through the human's
agreement. Commit messages and paths from the target repository are already read by other
leaves; nothing new becomes trusted.

## Backward compatibility

Additive. With no `[act.architecture]` table the feature is off, and `act index`, `act log`
and the process leaf behave exactly as today. New files appear under `process/` only when it
runs. Instances pick it up through the normal template update (ADR-0016: instances follow
`main`).

## Open questions

1. **Ordering against migration step 3 (#716).** Phase 2 needs a role with an output schema.
   Either wait for #716 or make this the first new role built in that shape.
2. **Machine-readable design edges.** Phase 1 can compare structure with the documented
   design only where the docs state edges in a form code can read (e.g. a Mermaid C4
   diagram). Should instances be asked to keep one such diagram, or is drift against prose
   docs left to the leaf?
3. **Producers to update in phase 1.** Which producers file follow-ups today needs a
   complete list before the marker is added: the Act leaf, Check carry-forwards and
   `pdca triage` at least. Whether to backfill markers on existing open issues, or only
   count from the change onward, is also open.
4. **Where the trigger threshold lives.** As a recommendation line in `act index` (proposed),
   or also as a §10-style candidate in the next Plan?
5. **Template or instance for `structure_command` defaults.** Ship per-ecosystem defaults in
   the template, or require every instance to set it?
6. **Undoing a detected change to the primary checkout.** This proposal stops and reports
   instead of reverting, because an automatic revert could destroy an operator's own
   concurrent edits. The Codex review of this draft argued for restoring "verified
   command-owned changes" under a lock. Decide whether such ownership can be proven
   reliably enough to revert automatically.
