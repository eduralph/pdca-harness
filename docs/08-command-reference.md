# Command reference — every `pdca` subcommand

← [07 Cross-cutting](07-crosscutting.md) · [Index](README.md) · next: [09 Configuration reference →](09-configuration-reference.md)

The walkthrough introduces each command where the cycle first needs it. This
page lists all of them in one place — first the commands that get an instance
rendered, installed and checked, then every `pdca` subcommand: what each does,
its options, what its exit code means, and where the walkthrough explains it.
The usage lines are the tool's own `--help` output at template `v0.59.0`;
`pdca <command> --help` is always the authority for the version you have
installed.

## Conventions

- **The command is named per project.** `cli_name` at render time sets the
  console script (`pdca` by default; `pdca-<slug>` if you run several instances
  on one machine). Before `make install`, or in CI, every command also runs as
  `PYTHONPATH=src python3 -m pdca_harness.cli <command> …`.
- **Every command runs inside a rendered project.** Each one loads `pdca.toml`
  from the current directory first. No `pdca.toml`, or an invalid one, is a
  one-line error and **exit 2**, never a traceback.
- **Bare `pdca` is `pdca status`.** The dashboard is the default command.
- **Exit codes** are a contract: **0** did what it says; **1** a bundle or a
  check did not pass, or the command refused to act; **2** bad input (config, an
  unknown id, an unschedulable graph). `pdca flow` adds **3** — see below.
- **`<id>`** is a tracker issue id as it appears in `results/issue_<id>/`.
  Several commands take none and then act on every bundle that fits.

## Getting set up

These run once per instance, or once per machine, before any `pdca` command.
[Step 01](01-render-and-integrate.md) walks through them in order with Wyrd's
real answers.

| Command | What it does | Covered in |
|---|---|---|
| `copier copy gh:eduralph/pdca-harness <dir>` | Renders the template into a new project, asking the `copier.yml` questions. Use a `gh:`/`https:` URL or an **absolute** path, never a relative one. | [01](01-render-and-integrate.md#1a-render-the-template) |
| `copier update` | From inside the rendered project: re-applies a newer template release over your render, keeping your answers and your instance files. Do it in a separate worktree, never under a live flow. | [01](01-render-and-integrate.md#1a-render-the-template), [04](04-do.md#the-harness-checkout-is-not-isolated--dont-do-git-maintenance-under-a-live-flow-issue-130) |
| `make install` | Bootstraps every tool `flow` needs, idempotently, then creates `.venv/` and installs the console script with `pip install -e .`. Three tiers: harness-universal tools (`git`, `gh`, a pip-capable venv), the leaf CLIs your `pdca.toml` configures, and your own gate toolchain hook. Re-runs when `pdca.toml` or the bootstrap scripts change. | [01](01-render-and-integrate.md#make-install--provision-every-tool-flow-needs) |
| `make install-check` | The same checks, installing nothing: one `OK\|MISSING\|INSTALLED\|FAILED\|WARN` row per tool. Exit non-zero only if a REQUIRED tool is missing — the CI preflight. | [01](01-render-and-integrate.md#make-install--provision-every-tool-flow-needs) |
| `scripts/bootstrap-tools.sh [--check]` | What `make install` and `make install-check` call. Ships verbatim from the template; your toolchain goes in `scripts/bootstrap-tools.d/*.sh` and `[install].extra_bootstrap`. | [01](01-render-and-integrate.md#make-install--provision-every-tool-flow-needs) |
| `pwsh -File scripts/install.ps1` | Windows: the equivalent of `make install` plus `make setup`, where GNU make is not standard. | [01](01-render-and-integrate.md#1d-install--doctor-verify-the-toolchain) |
| `make setup` | One-time, Claude leaves only: writes the gitignored `.claude/settings.local.json` granting the interactive leaves read of the sibling workspace and `/tmp`, so they don't prompt per file. Permissions, not trust — the first interactive `pdca flow` still asks once to trust the project. | [01](01-render-and-integrate.md#make-setup--grant-the-interactive-claude-leaves-workspace-read) |
| `pdca doctor [--strict]` | Reports every prerequisite — derived from `pdca.toml` plus your `[[doctor.checks]]` rows — and changes nothing. Run it after `make install` and whenever a red gate might be a missing tool. Full contract [below](#pdca-doctor--report-every-prerequisite-change-nothing). | [01](01-render-and-integrate.md#pdca-doctor--verify-prerequisites-change-nothing) |
| `make check` | The fast offline self-test: the driver's unit tests (`python3 -m unittest discover -s tests`), about a second. | [02](02-rehearse-offline.md) |
| `make` / `make test` | `check`, then a full driver cycle on a throwaway bundle with stub leaves — `init-issue selftest` and `run selftest` under `PDCA_LEAVES_MODE=stub`. Inspect `results/issue_selftest/`. | [02](02-rehearse-offline.md) |
| `pdca flow TOY --rehearse` | The same orchestration `flow` uses, entirely stubbed, in `.rehearse/`: the first command to run once the install is green. | [02](02-rehearse-offline.md#the-one-shot-rehearsal) |

The order matters: render, then `make install`, then `pdca doctor`, then the
offline rehearsal — and only then point a leaf at a real model. If
`ship_ci_workflow` was left on at render, the shipped `check-gates.yml` runs
`pdca gates --working-tree` on every PR of the instance, the same gate set the
driver runs.

## Drive the cycle

### `pdca flow` — the front door

```
usage: pdca flow [-h] [--from-csv FROM_CSV] [--from-briefs FROM_BRIEFS]
                 [--rehearse] [--no-publish] [--no-act] [--by BY]
                 [--lanes LANES] [--max-passes MAX_PASSES] [--auto-iterate]
                 [--no-inhibit]
                 [issue_ids ...]
```

Runs the whole cycle — Plan → Do → Check (gates → review → sign-off → publish)
→ Act — for one issue, a batch, or a batch planned from a tracker export. One id
is a single cycle; several ids run as [dependency waves](07-crosscutting.md#waves-in-execution),
briefing any unbriefed id in one shared Plan session first; no ids plus
`--from-csv` lets the planner pick the batch from the export. A split parent in
the set hands its children to the run ([07](07-crosscutting.md#size--split)).
Re-issuing the same command resumes a run that stopped.

| Option | What it does |
|---|---|
| `--from-csv FROM_CSV` | Tracker export that seeds the interactive Plan of unbriefed ids. |
| `--from-briefs FROM_BRIEFS` | Initialize any missing bundle from `DIR/<id>.md` before driving. |
| `--rehearse` | Dry run: stub leaves and stub gates in an isolated bundle root (`.rehearse/`), publish dry-run. No model, no `cargo`, no network ([02](02-rehearse-offline.md)). |
| `--no-publish` | Don't open the draft PR after an accept. |
| `--no-act` | Skip the Act leaf (Act runs by default once `act_cadence` cycles have frozen). |
| `--by BY` | Who signed off; recorded in §9. |
| `--lanes LANES` | Worker-pool size for the unattended Do + Check band; overrides `[driver].lanes` and `PDCA_LANES` ([07](07-crosscutting.md#lanes)). |
| `--max-passes MAX_PASSES` | Sign-off pass budget per wave before the driver stops driving a bundle; overrides `[driver].max_passes` and `PDCA_MAX_PASSES` ([07](07-crosscutting.md#the-iteration-budget)). |
| `--auto-iterate` | Rebuild without stopping while Check finds implementation-level work; human-judgment findings are deferred to the handover §6; overrides `[driver].auto_iterate` and `PDCA_AUTO_ITERATE` ([07](07-crosscutting.md#auto-iterate-the-driver-deciding-for-itself)). |
| `--no-inhibit` | Don't hold a suspend inhibitor for the run (also `PDCA_NO_INHIBIT=1`); for CI and containers. |

**Exit status**, from the tool's own help: **0** every bundle finished (with
one id, a stop for sign-off counts too); **1** a bundle did not finish, or the
run could not start; **2** bad input (a bad `pdca.toml`, no ids, an
unschedulable dependency graph); **3** every bundle finished, but a fold held an
accepted bundle out of the run's integration line (it pushed no branch, or it
conflicts with a same-wave bundle), so that work is on no line of the run.

Covered in: [index](README.md#the-whole-cycle-in-one-command), [03](03-plan.md#how-to-use),
[07](07-crosscutting.md#waves-in-execution).

### `pdca run` — one issue, one halted state

```
usage: pdca run [-h] issue_id
```

Advances a single bundle until the driver reaches a halted state
(`AWAITING_SIGNOFF`, `COMPLETE`, …): Do, gates, reviewer, assembly, STOP. It
never advances an `UNPLANNED` bundle — there is nothing to do without a brief —
and it runs no Plan, sign-off, publish or Act. This is the L2 rung of the
maturity ladder; `flow` wraps it. Covered in: [02](02-rehearse-offline.md).

### `pdca init-issue` — seed a bundle from a brief you already wrote

```
usage: pdca init-issue [-h] [--from-brief FROM_BRIEF] issue_id
```

Creates `results/issue_<id>/` and copies the file given by `--from-brief` in as
its `brief.md`, so the bundle starts `PLANNED`. The flag is required: to start
from a ticket, use `flow <id>`, which plans. This bypasses the planner leaf and
its advisory review, so it is for the offline toy brief, for hand-authored
briefs, and for tests. Covered in: [02](02-rehearse-offline.md), [03](03-plan.md#how-to-use).

### `pdca signoff` — record the human verdict (§9)

```
usage: pdca signoff [-h] (--accept | --iterate-do | --iterate-plan |
                    --discontinue) [--by BY] [--delta DELTA] [--no-publish]
                    issue_id
```

Exactly one disposition is required.

| Option | What it does |
|---|---|
| `--accept` | Accept — `merged-wider`, state `COMPLETE`, then publish (unless `--no-publish`). |
| `--iterate-do` | Right spec, wrong build: archive the attempt into `iteration-v<N>/`, keep the brief, back to `PLANNED`. |
| `--iterate-plan` | The spec was wrong: archive the whole attempt including the brief, back to `UNPLANNED`. |
| `--discontinue` | Deliberate abandon: record §9, no transition, drop from the pending set. |
| `--by BY` | Who signed off. |
| `--delta DELTA` | The iteration rationale; folded into the brief's carry-forward on an iterate. |
| `--no-publish` | Record the accept and stop at `COMPLETE`. |

**Refusals, all exit 1:** no `SUMMARY.md` yet; `--accept` while any §6
NEEDS-HUMAN item is unchecked (the C6 guard, which lists the open items); a
SUMMARY with no §9 section. The other three dispositions are not guarded. On an
accept that publishes, the exit code is publish's. Covered in:
[05](05-check.md#signing-off--the-four-dispositions), [05](05-check.md#the-c6-accept-guard).

### `pdca publish` — contribute an accepted bundle as a draft PR

```
usage: pdca publish [-h] [--dry-run] [--no-pr] [--no-issue] [--by BY] issue_id
```

Check's closing step: branch from the bundle's target (or the run's integration
line for a dependent), apply the patch, run the `host_ci` rows and the
contribution check against the drafted commit message and PR body, push, open a
**draft** PR, write `publish.json`. A failing host-CI row or contribution check
refuses the push.

| Option | What it does |
|---|---|
| `--dry-run` | Print the git and gh commands without running them. |
| `--no-pr` | Push the branch but don't open the draft PR. |
| `--no-issue` | No tracker id yet: the T4 check drops only the tracker-trailer rule and records `id_pending` (travels to the gate as `PDCA_PENDING_ID`). |
| `--by BY` | Who published; recorded in `publish.json`. |

Covered in: [05](05-check.md#publishing-the-accepted-change).

### `pdca record` — commit finished bundles to the instance repo

```
usage: pdca record [-h] [--dry-run] [issue_ids ...]
```

Commits every bundle whose cycle is over — `COMPLETE`, `DISCONTINUED`,
`RESOLVED` — as one batch commit; with `[records] mode = "pr"` it pushes a
`records/<date>` branch and opens one draft PR instead. A named id that is not
terminal is excluded, loudly. Needs the `[records]` table in `pdca.toml`; once
enabled, publish triggers it too. `--dry-run` prints the plan. Covered in:
[05](05-check.md#publishing-the-accepted-change).

### `pdca revert` — undo a published contribution

```
usage: pdca revert [-h] [--dry-run] [--by BY] issue_id
```

Reads the bundle's `publish.json`: a **merged** PR gets a draft revert PR that
reverse-applies the bundle's own `patch.diff` onto the base; an **open** PR is
withdrawn (`gh pr close --delete-branch`). The revert PR stays a draft for you.
`--by` is recorded in `revert.json`. Covered in: [05](05-check.md#publishing-the-accepted-change).

## Look at state

### `pdca status` — the dashboard (also bare `pdca`)

```
usage: pdca status [-h] [issue_id]
```

One line per bundle, cheap-first: state, id, and markers — `[N NEEDS-HUMAN]`
or `[cheap: confirm]` on a bundle awaiting sign-off, `[blocked-by: …]` for an
unsatisfied `Depends on`, `[oversized]` from the size signal, and for a
`COMPLETE` bundle `[PR <url>]` (with `↑<base>` when the PR is stacked),
`[unpublished]`, or `[close: no PR]`. With an id, that bundle only. Exit 0.
Covered in: [index](README.md#the-whole-cycle-in-one-command), [05](05-check.md#publishing-the-accepted-change).

### `pdca queue` — the sign-off burn-down

```
usage: pdca queue [-h]
```

The `AWAITING_SIGNOFF` bundles, cheapest first — the order to work a batch's
verdicts in. Exit 0.

### `pdca waves` — preview the schedule

```
usage: pdca waves [-h] [issue_ids ...]
```

Prints the dependency-wave plan `flow` would run for the given ids (none: every
in-flight briefed bundle) without building anything. An unschedulable graph — a
cycle, an unresolved `Depends on`, a `Merges with` group under merge mode — is
reported as `unschedulable:` with exit 1. Covered in:
[03](03-plan.md#ordering-and-routing-the-batch), [07](07-crosscutting.md#waves-in-execution).

### `pdca size` — the a-priori size estimate

```
usage: pdca size [-h] [issue_ids ...]
```

Read-only. For each briefed bundle (or the ids given) prints the band — `ok`,
`watch`, `oversized` — and the signals that fired, split into churn and patch
size. Decides nothing and writes nothing. Covered in: [07](07-crosscutting.md#the-estimate).

## Decompose

### `pdca split` — propose, then materialize, a decomposition

```
usage: pdca split [-h] [--accept] [--ids IDS] [--force] issue_id
```

Without `--accept`, the splitter leaf drafts `split-proposal.md` — one full
draft brief per child. With `--accept`, the deterministic half files one
tracker sub-issue per child (unless `--ids` names existing ones, in proposal
order), materializes the child bundles, rewrites the proposal's sibling labels
to real ids, writes `split-lineage.json` everywhere, and closes the parent with
`close-disposition` = `split`. Acceptance is transactional: everything is
validated before any issue is filed or file written. A bundle already two
splits deep is refused unless `--force`. Normally the planner runs both from
inside the Plan session. Covered in: [07](07-crosscutting.md#the-split).

## Gates and checks

### `pdca gates` — run the deterministic Check gates

```
usage: pdca gates [-h] [--working-tree] [--promotions] [issue_id]
```

The same code the driver and CI call. With an id, that bundle's full gate set
is run and printed, nothing recorded. `--working-tree` runs the repo-scoped
rows only — the CI merge re-gate. `--promotions` lists the advisory rows that
have passed in their `promote_after` most recent frozen cycles and so have
earned promotion to gating (you flip `gating = true` yourself). Exit 1 when the
gating overall is `fail`, else 0. Covered in: [01](01-render-and-integrate.md#the-gates--your-deterministic-check-oracles),
[05](05-check.md#the-gates-run-automatically).

### `pdca registry-check` — a manifest line must point at a touched path

```
usage: pdca registry-check [-h] [issue_id]
```

A pure diff analysis for a bundle-scoped gate row: every line a patch adds to a
declared registry or manifest file (`[gates.registry_consistency] files`, with
an optional extraction `pattern`) must reference a path the same patch touches;
anything else is cross-bundle contamination. The id may also arrive as
`$PDCA_BUNDLE`, which is how a gate row calls it. Exit 0 with no registry files
declared or no patch; 1 on a violation; 2 when no bundle can be resolved.

### `pdca contribcheck` — the T4 contribution check

```
usage: pdca contribcheck [-h] [--no-issue] [issue_id]
```

Fails a contribution whose PR body lacks a user-impact opener or whose commit
message and PR body lack the tracker id. At Check it reports `deferred` (the
texts are drafted at publish); publish runs it for real before the push.
`--no-issue` (or `$PDCA_PENDING_ID`) drops only the tracker-id rule. Exit 0
when nothing is wrong or there is no patch to contribute, 1 on a problem, 2
when no bundle resolves. Covered in: [05](05-check.md#the-gates-run-automatically).

### `pdca revalidate` — re-gate a frozen bundle against today's engine

```
usage: pdca revalidate [-h] [--date DATE] issue_id
```

Re-runs the same gate set over a `COMPLETE` bundle and writes an additive,
dated stamp `revalidation-<date>.json` recording each row's old → new result.
It never touches `check-gates.json`, `check-gates.md` or §9: the original
decision stands, and a changed result in either direction is a signal, not a
verdict.

### `pdca drift` — which published patches no longer apply

```
usage: pdca drift [-h] [--no-fetch]
```

For every `COMPLETE` bundle with an open PR, `git apply --check`s its patch
against the freshly fetched base in a throwaway worktree and reports the ones
that need a rebase. Report-only: it mutates nothing and re-decides nothing.
`--no-fetch` checks against already-fetched refs.

### `pdca try` — run the patched build yourself

```
usage: pdca try [-h] issue_id
```

Materializes the bundle's patched tree from `patch.diff` and runs the
project's `[manual_test].cmd` from it, inheriting your terminal, with no
timeout — you quit the app to return. Advisory: it changes nothing in the
bundle. Needs `[driver].worktree` on and a configured command; otherwise it
prints a hint. Covered in: [05](05-check.md#trying-the-build-by-hand--pdca-try-id).

## Act

### `pdca act` — cross-cycle review tooling

```
usage: pdca act [-h] {index,log,resolve} ...
```

Manual, out-of-turn Act review over frozen (`COMPLETE`) cycles. These carry no
cadence gate — `[driver].act_cadence` throttles only the flow's automatic Act.
`index` and `log` default to the cycles frozen since the last review (the
frontier in `process/.act-reviewed`); `--all` or `--since` widens the scope.

```
usage: pdca act index [-h] [--since SINCE] [--all]
usage: pdca act log [-h] [--since SINCE] [--all] --date DATE [--append]
usage: pdca act resolve [-h] [--location LOCATION] [--date DATE] signal
```

| Subcommand | What it does |
|---|---|
| `index` | Read-only index of frozen cycles, their §6/§7/§10 extracts, recurring signals, the per-cycle `sizing:` line and the "⚠ Ineffective deltas" audit. Writes nothing. |
| `log --date <ISO>` | Prints a scaffolded, dated act-log entry; its Process-deltas section is left `TODO` on purpose. With `--append` it is written to `process/act-log.md` and the review frontier advances, so the flow's next automatic Act won't re-cover these cycles. Exits 1 when no frozen cycle exists. |
| `resolve <signal>` | Marks a tracked recurring signal as a delta you applied, in `process/act-ledger.json`; `--location path:line` says where. A later Act flags it as ineffective if the miss recurs after that date. |

Covered in: [06](06-act.md).

### `pdca triage` — ingest a published PR's review findings

```
usage: pdca triage [-h] [--repo REPO] [--date DATE] pr
```

Pulls the PR's reviews and review comments through `gh api`, classifies each
finding as BUG, CONVENTION, NOISE or TEST-GAP, routes by class (a BUG on a
merged PR files a tracker issue with a carry-forward note; the rest append
candidate gate, rubric and exclusion entries to `process/act-log.md`), and
registers every finding in the Act ledger under `codex-pr:<slug>`. Proposes
only: it never edits `pdca.toml` or the rubric. `pr` is a URL, `OWNER/REPO#N`,
or a bare number with `--repo`. Re-running ingests only new findings. Covered
in: [06](06-act.md#external-review-findings--pdca-triage-issue-316).

## Maintenance

### `pdca doctor` — report every prerequisite, change nothing

```
usage: pdca doctor [-h] [--strict]
```

One row per prerequisite — `OK`, `MISSING`, `UNAUTH` or `WARN` — each with a
fix hint. Read-only and idempotent: it installs nothing and edits nothing.
Run it after `make install`, and again whenever you add a gate, change a leaf's
family, or a red gate might be a missing tool rather than a bad patch.

**Rows derived from `pdca.toml`**, so they follow your edits with no extra
config:

- every distinct command-leaf `argv[0]` on `PATH`, with a per-family auth probe
  (`UNAUTH` for a Codex CLI without a login; `WARN` when no Claude credentials
  are found) — the builder's family is REQUIRED, the others optional;
- `git` with an identity, and `gh` present and authenticated (publish, merge
  and revert need it);
- the bundle root writable, and free space where the harness cuts worktrees;
- the tracker `notes_cmd`'s tool resolvable;
- the leaf sandbox's dependencies (`bubblewrap`, `socat`) when a bounded
  `[leaves.sandbox]` exemption will be seeded — REQUIRED, because a sandbox
  that cannot start does not fail, it silently does not confine.

**Rows you declare**, one `[[doctor.checks]]` table each:

| Key | Meaning |
|---|---|
| `id` | The row's name. A brief's `External dependencies` token must equal one of these. |
| `cmd` | Run with cwd = project root; exit 0 ⇒ `OK`. Keep it cheap and side-effect-free: the dependency guard runs it at every beat it is consulted. |
| `hint` | What a human does to fix it. |
| `group` | Optional section header the row prints under. |
| `level` | The status when `cmd` fails: `MISSING` (default) or `WARN` for an optional tool. |
| `required` | A failing required row makes doctor exit non-zero (default false). |
| `per_lane` | One row per `[driver].lanes` slot, with `{lane}` and `{lanes}` filled in; nothing when lanes ≤ 1. |

**Exit codes:** 0 when every REQUIRED row passes; 1 when a REQUIRED row failed
("REQUIRED checks failed — fix the lines above first"); with `--strict`, 1 on
*any* non-OK row, `WARN` included — the CI setting.

The same rows are the registry a brief's `External dependencies` field is
checked against, at Plan exit, at Do's exit and at Check, so a doctor row is
not only a one-time readiness check. Covered in:
[01](01-render-and-integrate.md#pdca-doctor--verify-prerequisites-change-nothing),
[03](03-plan.md#entry-and-exit-the-guards-around-plan),
[05](05-check.md#an-unregistered-dependency-is-a-6-item-issue-263).

### `pdca cleanup` — reconcile bundles with the tracker

```
usage: pdca cleanup [-h] [--apply] [--repo REPO] [--by BY] [issue_ids ...]
```

Matches each bundle's state against its tracker issue and plans the matching
action: a closed issue resolves a notes-only bundle (`RESOLVED`) or
discontinues one awaiting sign-off; a `COMPLETE` or `DISCONTINUED` bundle whose
issue is still open gets it commented and closed; a merged PR on an unaccepted
bundle is reported, never auto-accepted. Dry run by default; `--apply`
executes. `--repo OWNER/REPO` names the tracker when it is not the configured
GitHub source; `--by` is the §9 attribution for discontinue records. A missing
or unauthenticated `gh` aborts before any write. Covered in:
[07](07-crosscutting.md#housekeeping-sweep--cleanup).

### `pdca sweep` — reclaim the harness's worktree footprint

```
usage: pdca sweep [-h] [--remove] [--dry-run]
```

Cleans the harness's own lane, integration and overflow worktrees — never
bundle artifacts, never the primary checkout. Default: strip build state and
keep the checkouts warm; `--remove` deletes the lane worktrees too (Do and
`try` recreate them on demand); `--dry-run` reports only. Runs automatically at
the publish/freeze boundary per `[driver].sweep_worktrees`; the manual command
works even when that is `"off"`. Exit 0. Covered in:
[07](07-crosscutting.md#housekeeping-sweep--cleanup).

## Environment variables

Two groups. The first you may set to override `pdca.toml` for one run; the
second the driver exports to the gate commands and leaves it spawns, so your
scripts can read them.

| You set | Effect |
|---|---|
| `PDCA_LANES` | Worker-pool size for one run (`[driver].lanes`; `--lanes` wins over both). |
| `PDCA_LANE` | Pin a standalone `pdca flow <id>` process to lane slot `k` instead of auto-claiming one. |
| `PDCA_MAX_PASSES` | The per-wave sign-off pass budget (`[driver].max_passes`). |
| `PDCA_AUTO_ITERATE` | Turn auto-iterate on for one run (`[driver].auto_iterate`). |
| `PDCA_MAX_AUTO_ITERS` | The auto-iterate hard cap for one run (`[driver].max_auto_iters`). |
| `PDCA_LEAVES_MODE` | Force every leaf's mode (`stub` or `command`) regardless of `pdca.toml`; `--rehearse` sets `stub`. |
| `PDCA_GATES_MODE` | `stub` empties the configured checks into the all-PASS stub rows; `--rehearse` sets it. |
| `PDCA_BUNDLE_ROOT` | Redirect the bundle root; `--rehearse` points it at `.rehearse/`. |
| `PDCA_NO_INHIBIT` | Same as `flow --no-inhibit`. |

| The driver exports | To whom, meaning |
|---|---|
| `PDCA_WORKTREE` | Gates and leaves: the per-cycle worktree Do edited — the tree a bundle-scoped gate must test. |
| `PDCA_TARGET` | The reviewer and advisory leaves: the read-only grounding checkout (the same worktree, patch applied). |
| `PDCA_BUNDLE` | Gates, leaves, `notes_cmd` and plan sources: the bundle directory. |
| `PDCA_LANE` | Gates, when lanes > 1: this worker's slot, `0..N-1`, for namespacing shared resources. Unset on the serial path. |
| `PDCA_SOURCES` | A `command` plan source: the bundle's `sources/` directory to write into. |
| `PDCA_BASE` | The per-fix verifier: the brief's `Onto branch`, when set. |
| `PDCA_VERIFY_BASE` | The per-fix verifier: the wave's folded integration branch, for a wave > 0 bundle. |
| `PDCA_BRIEF_BASE` | The per-fix verifier: the brief's `Repo + branch target` base, resolved by the driver. |
| `PDCA_PENDING_ID` | The T4 contribution check: set by `publish --no-issue`, so the gate knows which rule it is excusing. |

The three base variables form a ladder the per-fix red→green row resolves in
that order; Wyrd's `run-verify.sh` in [step 01](01-render-and-integrate.md#the-gates--your-deterministic-check-oracles)
reads them exactly so. Every `pdca.toml` key those commands read is in the
[configuration reference](09-configuration-reference.md).

← [07 Cross-cutting](07-crosscutting.md) · [Index](README.md) · next: [09 Configuration reference →](09-configuration-reference.md)
