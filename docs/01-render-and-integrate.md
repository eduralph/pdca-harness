# Step 01 — Render the harness & integrate your repo

← [00 Introduction](00-introduction.md) · [Index](README.md) · next: [02 Rehearse offline →](02-rehearse-offline.md)

This step gets you from "I have the template" to "the driver knows how to build,
gate, and publish for *my* repo." There are four sub-steps: **render**,
**concretize** (`docs/INTEGRATION.md`), **wire** (`pdca.toml`), and **install &
verify** (`make install`, `pdca doctor`).

---

## 1a. Render the template

The harness is a [Copier](https://copier.readthedocs.io/) template. Rendering it
copies the project-agnostic parts into a new directory and asks you a handful of
questions:

```bash
copier copy gh:eduralph/pdca-harness ../wyrd-pdca
cd ../wyrd-pdca
```

> **Rendering from a local checkout instead?** Give copier a `gh:`/`https:`
> URL (as above) or an **absolute** path. Copier records the source argument
> verbatim as `_src_path` in `.copier-answers.yml`, and `copier update`
> resolves it from the *project* directory — a path relative to your render
> cwd never resolves there, so the first `copier update` fails
> ([copier#335](https://github.com/copier-org/copier/issues/335); fix pending
> in [copier#2717](https://github.com/copier-org/copier/pull/2717)). Already
> rendered that way? Hand-edit `_src_path` to the template URL or an absolute
> path — that's the whole repair.

The prompts (from [`copier.yml`](../copier.yml)) and what Wyrd answered — its
`.copier-answers.yml`, which Copier writes so that `copier update` can re-apply
later template releases (wyrd-pdca is at `v0.59.0`):

| Prompt | What it sets | wyrd-pdca |
|--------|--------------|-----------|
| `project_name` | Human-readable name | `Wyrd PDCA` |
| `project_slug` | Slug for paths/package | `wyrd-pdca` |
| `project_description` | One line, lands in the rendered README | `PDCA quality-cycle wrapping Wyrd's cargo xtask gates` |
| `cli_name` | The console script the project installs — namespace it if you run several instances on one machine | `pdca` |
| `author_name` / `author_email` | §9 attribution default | `Eduard Ralph` |
| `project_license` | SPDX license the rendered project declares | `Apache-2.0` |
| `tracker_system` | github / mantis / jira / other | `github` |
| `tracker_url` | Base tracker URL | `https://github.com/getwyrd/wyrd` |
| `issue_id_example` | ID shape in briefs/commits | `#123` |
| `default_branch` | Where fixes land unless overridden | `main` |
| `contribution_model` | `fork` (contribute upstream through a fork: branch from `upstream`, push to `origin`) or `own-repo` (this repo is the target); see the [configuration reference](09-configuration-reference.md#publisher-and-the-contribution-model) | `own-repo` |
| `bundle_root` / `process_dir` | Per-cycle bundle dir / cross-cycle process artifacts | `results` / `process` |
| `builder_family` | Model family for the Do leaf | `claude` |
| `reviewer_family` | Model family for the Check reviewer — **should differ from builder** | `codex` |
| `interactive_family` | Model family for the human-in-the-loop leaves (Plan, sign-off, publish, Act) | `claude` |
| `leaves_mode` | `stub` (offline) or real commands | `stub` to start |
| `gates_runner` | An external gate runner to delegate to; blank = inline gate commands | `cargo xtask` |
| `ship_ci_workflow` | Ship the GitHub Actions merge re-gate? Off if the host runs its own CI | `false` — Wyrd's CI already runs `cargo xtask ci` |
| `ship_merge_guard` | Ship the builder/publisher STOP-discipline hook? | `true` |

> **Start in `stub` mode.** You want the offline rehearsal in
> [step 02](02-rehearse-offline.md) working before you point any beat at a real
> model. The render message says the same thing: *"Replace the stub leaves once
> the deterministic gates exist (build order: gates → driver → batch → Act)."*
> See *The build ladder* below (1c) for why that order matters.

After rendering you have:

```
wyrd-pdca/
  pdca.toml                 # driver config — you edit this (1c below)
  PCDA/quality-cycle/       # the vendored model spec (reference; don't edit)
  docs/INTEGRATION.md       # your repo's concretizations — you fill this (1b below)
  src/pdca_harness/         # the driver (don't edit; updated via `copier update`)
  agents/                   # the leaves' role prompts (vendor-neutral bodies)
  engine/                   # YOUR verification engine — the runners the gates call
  templates/                # brief / SUMMARY / PR-description templates
  examples/toy/             # the offline toy brief (step 02)
  results/                  # bundles land here
  process/act-log.md        # Act log (step 06)
  Makefile                  # the front door
```

---

## 1b. Concretize: fill `docs/INTEGRATION.md`

The model spec is deliberately project-agnostic. `docs/INTEGRATION.md` is where
you bind it to *your* repo — it has **11 items**. You don't need all 11 before a
first cycle, but items 1–4 (tracker, branches, fixtures/runners, conformance
ruleset) are what the Plan and Check beats lean on.

The 11 items:

1. **Tracker integration** — system/URL, issue-ID format, cross-link form, status → disposition mapping
2. **Branch-target rules** — per-area branch map, override convention, cross-version cherry-pick rules
3. **Reproduction fixtures and runners** — canonical fixture, repro/verification runner commands, what counts as a successful repro
4. **Conformance ruleset** — the T1–T5 matrix: written ruleset + home + the single command per tier
5. **Upstream-isn't-ahead routine** — what "upstream" is and how to confirm it isn't already fixed
6. **Brief and design-proposal templates** — required project-specific fields
7. **Bundle and act-log paths** — bundle root + ID format, iterate-archive convention
8. **Committing and PR conventions** — commit-message + PR-body format
9. **Repo-specific scripts and tooling** — the role → path table
10. **Maintainer and governance** — who reviews, the ready-mark gate
11. **Per-repo P-/D-/C-/A- extensions** — rules that tighten the generic ones

Here is how Wyrd filled a few, verbatim from `wyrd-pdca/docs/INTEGRATION.md`.
Wyrd is an **out-of-tree** instance: the harness lives in its own repo beside a
checkout of the target (`../wyrd`), wraps Wyrd's existing Plan/Do/Check
machinery, adds the Act beat Wyrd lacked, and never modifies Wyrd's tree.

**Item 1 — Tracker integration** (a host gate already demands a linked issue, so
the harness's trailer satisfies it instead of inventing a second rule):

```markdown
- **System / URL:** GitHub Issues — https://github.com/getwyrd/wyrd/issues. Already
  gated host-side: Wyrd's `require-issue` xtask/CI check rejects a PR with no linked issue,
  so PDCA's "init from brief" maps onto an issue that already satisfies that rule.
- **Cross-link form (commit/PR → tracker):** `Fixes #nnnn` (GitHub auto-close) — this is
  `[tracker].issue_trailer = "Fixes #{id}"` in `pdca.toml`.
```

**Item 2 — Branch-target rules** (one trunk, and an explicit rule for what is
*not* a branch target — this is what the planner cites when it writes a brief's
*Repo + branch target*):

```markdown
- **Per-area branch map:** single-slice fixes and standalone features target **`main`**
  directly. Wyrd has **no maintenance branches** (no `maintenance/*`, no
  master-vs-maintenance split) — don't invent one. **A multi-slice milestone stacks on a
  shared integration branch** instead (the generic rule is fork-discipline.md §3).
- **Release branches — every release gets one, and it is how the release is maintained**
  (maintainer decision, 2026-08-16). At each release point a branch is cut from `main` and
  named `release/<version>` (e.g. `release/0.1-alpha`).
  - **Direction of flow: `main` first, then cherry-pick.** A fix lands on `main` and is
    cherry-picked onto the release branch. Never the reverse — a fix that exists only on a
    release branch is a regression waiting for the next release.
```

**Item 3 — Reproduction fixtures and runners** (what "reproduced" means in a
system whose test suite is a deterministic simulation):

```markdown
- **Verification runner (the whole gate):** **`cargo xtask ci`**, delegated via
  `./engine/xtask.sh` (§9). It runs the same checks on a laptop and in CI (ADR-0016): the
  prose gates (`typos`, docs lint/render — wyrd#599) first, then fmt (`--check`), clippy
  (`-D warnings`), build, test (incl. DST property tests), `cargo deny check`, and the
  conformance run. Exit 0 = pass. This is Wyrd's single source of gate truth.
- **Reproduction runner(s):** Wyrd's DST is the repro substrate (ADR-0009) — a failing
  **seed** under madsim is the deterministic reproduction; `cargo xtask dst` sweeps seeds,
  and a bug-finding seed becomes a permanent regression test.
- **What counts as a successful repro:** the regression test (DST seed or conformance
  vector) is **red before** the fix and **green after**; `cargo xtask ci` exits 0.
```

**Item 4 — the conformance ladder.** Wyrd has no T1–T4 ruleset of its own; its
checks are single-sourced in `cargo xtask` and rolled into `ci`. So the table maps
PDCA's tier slots onto what Wyrd *already* enforces, and nothing is re-declared:

```markdown
| Tier | Written ruleset (normative source) | Home | Single-sourced command | Status |
|---|---|---|---|---|
| C4 correctness | the change + Wyrd's whole gate | `cargo xtask` (`../wyrd/xtask/`) | `./engine/xtask.sh ci` (delegates `cargo xtask ci`) | [built — **gating**, scope=repo] |
| T1 format-conformance | chunk-format spec v1, RFC-2119; conformance spec (ADR-0002) | `cargo xtask conformance` | `./engine/xtask.sh conformance` | [built — runs inside `ci`] |
| T2 shape | `rustfmt` + `clippy -D warnings` (no project style doc; the linters are the rule) | inside `cargo xtask ci` | (subsumed by `ci`) | [built — part of `ci`] |
| T3 runtime / DST | testing strategy, ADR-0009 (madsim DST is the spine) | `cargo xtask dst` + `test` | `./engine/xtask.sh dst` | [built — runs inside `ci`] |
| T4 contribution | ADR-0003 §1 (DCO), `require-issue`, `adr-immutability`; commit/PR conventions (§8) | `cargo xtask` gates + GitHub CI | `./engine/xtask.sh ci` (re-gate) + host CI | [built — host-enforced] |
| T5 judgment | reviewer contract below | Check reviewer + sign-off | (model) | [planned] |
```

INTEGRATION is one of six documents the render puts in your tree. The other
five are template-owned: `docs/principles.md` (how a brief states an invariant
rather than a mechanism), `docs/fork-discipline.md` (the contribution rules
under `contribution_model = fork`; Wyrd is own-repo, so its copy says to ignore
it), and the `CLAUDE.md`, `AGENTS.md` and `CONTRIBUTING.md` that brief the
agents and the humans working inside the instance. [Step 10](10-upgrading-an-instance.md#the-documents-a-render-ships)
says who reads which.

The lesson: **the harness doesn't ship gate logic — it ships the *contract* for
gates.** You write the checkers — or, as here, point at the ones the project
already has — and wire `pdca.toml` to them (1c). Wyrd's whole `engine/` is two
wrappers around what the target repo owns: `engine/xtask.sh`, which changes into
the per-cycle worktree and runs `cargo xtask "$@"` there, and
`engine/scripts/run-verify.sh`, the per-fix red→green check that applies one
bundle's patch to a clean worktree and runs only the test it ships.

---

## 1c. Wire: `pdca.toml`

`pdca.toml` is the driver's only config. Two things matter most: the **leaves**
(the commands each beat runs) and the **gates** (the deterministic Check
oracles). Here is Wyrd's real config, trimmed to the shape.

### The leaves — one command per beat

```toml
[leaves.planner]
mode = "command"
family = "claude"
interactive = true                                  # opens in your terminal
agent = "planner"
model = "opus"
effort = "xhigh"
argv = ["claude", "--agent", "planner", "--permission-mode", "acceptEdits",
        "--remote-control", "pdca planner"]

[leaves.builder]
mode = "command"
family = "claude"
interactive = false                                 # headless — runs unattended
agent = "builder"
effort = "medium"
argv = ["claude", "-p", "--agent", "builder", "--permission-mode", "acceptEdits",
        "--allowedTools", "Read,Edit,Write,Bash,Grep,Glob", "--model", "opus"]

[leaves.reviewer]
mode = "command"
family = "codex"                                    # a DIFFERENT vendor from the builder
interactive = false
agent = "reviewer"
model = "gpt-6-astra"
effort = "xhigh"
argv = ["codex", "exec", "--sandbox", "workspace-write", "--skip-git-repo-check"]

[leaves.signoff]
mode = "command"
family = "claude"
interactive = true                                  # YOU, at the §9 step
agent = "signoff"
argv = ["claude", "--agent", "signoff", "--permission-mode", "acceptEdits",
        "--remote-control", "pdca signoff"]
# ...and publisher, act — same shape
```

The interactive leaves (planner, signoff, publisher, act) open Claude in your
terminal because they're the human touch points. The headless ones (builder,
reviewer) run unattended.

Two details of Wyrd's shape are worth copying. `model` and `effort` are harness
keys, not decoration: the driver records them per attempt in the bundle's
`loop-telemetry.json`, so an escalation ladder *inside* one vendor stays
readable ([step 04](04-do.md)). And `--remote-control` on the interactive leaves
lets the human answer a planner or sign-off session from another enrolled device
instead of sitting at the machine the flow runs on — a batch parked on one
sign-off for hours, because nobody was at that terminal, is the cost it removes.
Put it anywhere but *last* in `argv`: the driver appends the seed prompt after
the argv, and `--remote-control` takes an optional value, so as the final token
it would swallow the prompt as a session name.

Every interactive session also has a **checked exit** (issue #331): before you
end it, run `/handoff <issue-id>` — it verifies the session's expected artifact
exists and is well-formed, and reports PASS or FAIL with the items to fix. When
the session ends, the driver checks again and prints what it finds to your
terminal. It re-reads the artifacts of every bundle it registered for the
session: the bundle of a Plan, sign-off or publish session, and each bundle of
an id-seeded batch Plan or a batch sign-off. A CSV or default batch Plan picks
its issues mid-session, so the driver has no bundle set to register for it;
instead it re-reads every brief the session created or changed, found by
comparing each brief with how it was when the session started. A brief that
predates the session and is unchanged is not re-read. Where there is nothing
to re-read — Act, or a batch Plan that changed no brief — it checks that the
session named its work through a passing `/handoff` instead. The
end-of-session check only reports: it never keeps the session open and never
changes the bundle. Nothing checks the contract at the end of each turn, so the
leaf can always hand a question back to you (issue #534). A deliberate
walk-away is recorded with
`python3 .claude/hooks/handoff_guard.py --abandon "<why>"`; the driver then
prints your reason first and still lists everything its check found, so an
abandon never hides what is missing. Each agent's role prompt lives in a canonical,
vendor-neutral body at `agents/<name>.md`; Claude leaves additionally get
`.claude/agents/<name>.md` (a frontmatter wrapper that includes that body, so
`--agent` resolves), materialized only when the leaf's family is `claude`.
Non-Claude leaves read the `agents/<name>.md` body directly.

> **Decorrelate the reviewer.** The model recommends the Check reviewer be a
> *different* family from the builder (the template default is `codex`), so the
> reviewer doesn't share the builder's blind spots. Wyrd does exactly that — a
> Claude builder, a Codex reviewer — and goes further in [step 05](05-check.md):
> a Claude-family adversary and a Codex code-review lens run on every bundle, so
> each patch is read by both vendors whichever one built it.

### The gates — your deterministic Check oracles

A gate is a command plus metadata. `gating = true` means a failure **blocks** the
sign-off; `gating = false` is advisory (it still shows in the SUMMARY, but a human
adjudicates). `scope` is `bundle` (this fix) or `repo` (whole tree).

Wyrd's gates are **delegated** rather than declared: `[gates].runner` names the
host's own runner, and a row with a bare `subcmd` is run as `<runner> <subcmd>`.
A row with its own `cmd` runs as written instead — that is how the per-fix rows
reach the engine scripts:

```toml
[gates]
runner = "./engine/xtask.sh"          # cd's to $PDCA_WORKTREE, execs `cargo xtask "$@"`
confirm_gating_fail = true            # a gating red is re-run once before it counts
default_timeout_secs = 7200           # no row may hold a Check beat for a day
checks = [
  # The whole Wyrd gate, wholesale — the ONE gating repo-scoped row
  { id = "C4-ci", tier = "C4", label = "Wyrd gate: cargo xtask ci (fmt/clippy/build/test/deny/conformance)",
    subcmd = "ci", gating = true, scope = "repo" },
  # Per-fix red→green: THIS patch's test red pre-fix, green post-fix. Advisory until it
  # has passed 3 consecutive signed cycles, which `pdca gates --promotions` then surfaces.
  { id = "C4-verify", tier = "C4", label = "per-fix red->green: this patch's test red pre-fix, green post-fix",
    cmd = "./engine/scripts/run-verify.sh", gating = false, scope = "bundle", promote_after = 3, verifies_base = true },
  # Two more probes of the bundle diff: how much of it the shipped test executes, and
  # whether any changed line goes unnoticed when broken. Advisory.
  { id = "C4-diff-cov", tier = "C4", label = "diff coverage: changed lines executed by the patch's tests",
    cmd = "./engine/scripts/run-diff-cov.sh", gating = false, scope = "bundle" },
  { id = "C5-mutants", tier = "C5", label = "surviving mutants on the bundle diff (cargo mutants --in-diff)",
    cmd = "scripts/mutants-in-diff", gating = false, scope = "bundle" },
  # The contribution itself: a batched three-pass rubric review, and the PR/commit text.
  { id = "T4-batch-review", tier = "T4", label = "batched multi-pass rubric review (3x codex, union, triaged)",
    cmd = "scripts/review-branch --bundle", gating = true, scope = "bundle", at_publish = false, confirm_fail = false },
  { id = "T4-contribution", tier = "T4", label = "contribution artifacts complete (user-impact opener + tracker id in both)",
    cmd = "scripts/pdca contribcheck", gating = true, scope = "bundle" },
]
```

A row may also carry `target`, so that it runs only for bundles of a given
kind: the brief's `Repo + branch target` and `Surfaces` fields classify the
bundle through `[gates.target_match]` and `[gates.target_flags]`, and a GUI
smoke test tagged `target = "frontend"` runs only when a brief says
`Surfaces: gui` ([configuration reference](09-configuration-reference.md#gates)).
Wyrd, a server with one surface, uses none of this.

The key design choice you're making here: **what blocks, and what merely
informs.** Wyrd made the whole-tree `cargo xtask ci` gating — it is the very
command Wyrd's CI runs, so a bundle that passes it here cannot open a PR red on
it — and the two contribution rows with it. The per-fix red→green proof is
*advisory* with `promote_after = 3`: it earns the right to block by passing
three signed cycles in a row, and `pdca gates --promotions` tells you when it
has ([step 05](05-check.md)). Diff coverage stays advisory for good: a line
compiled out behind a `#[cfg]` is indistinguishable from a comment in the
coverage report, so that row can read 100% green over a fraction of a patch — it
shows the gap rather than gating on it. And a check that could not adjudicate at
all — a coverage run that measured nothing, a review that came back empty —
surfaces to a human (in §6 NEEDS-HUMAN) rather than silently blocking a correct
fix, or silently passing it. You'll see exactly that play out in
[step 05](05-check.md).

One more wiring question the gate ladder can't answer for you: **does the host
repo's CI run anything on every PR that your gates don't?** A spell-checker, a
docs linter, a feature build — jobs like that let a bundle pass Check green and
open a PR that immediately fails a required status. Declare them in
`[gates].host_ci` (issue #311) and the harness runs each one against the patched
tree at Check *and* re-runs it at publish, immediately before anything is pushed
— a failure there refuses the push. Empty or absent changes nothing. Wyrd has one
such row — the `tikv` feature, which `cargo xtask ci` leaves out but the host's
CI compiles on every PR:

```toml
host_ci = [
  { id = "host-tikv", label = "tikv feature compiles (crate + server selection arms)",
    cmd = "WYRD_TIKV_TOOLCHAIN=1 sh -c 'cargo clippy -p wyrd-metadata-tikv --features tikv --tests && cargo clippy -p wyrd-server --features tikv,etcd --tests'" },
]
```

### The build ladder — what order to wire `pdca.toml` in

The model defines a four-rung **maturity ladder** (the vendored spec's
[`03-cycle-automation.md`](../template/PCDA/quality-cycle/03-cycle-automation.md)
§Maturity ladder) for how much of the cycle runs unattended. Every mechanism is
tagged **[built]** (ships and runs today), **[partial]** (ships, needs
per-project wiring), or **[project-provided]** (tracker-specific — no template
default makes sense):

- **L1 — scripted handoff.** `[project-provided]` A tracker scraper + handoff
  generator that drafts briefs from a candidate pool. This is `docs/INTEGRATION.md`
  item 9 (1b above) — the harness ships no default because it's inherently
  tracker-specific. (Wyrd's is one line: `[tracker].notes_cmd` points at a
  script that runs `gh issue view` and writes the thread into the bundle.)
- **L2 — unattended per-issue body.** `[built]` `pdca run <id>` — Do → Check →
  assemble → STOP for one issue: the state machine, the two headless leaf calls,
  the gate runner.
- **L3 — unattended contribution-batch + sign-off queue.** `[built]`
  `pdca flow <ids…>` fans the driver over N issues; `pdca queue` is the
  cheap-first sign-off burn-down.
- **L4 — Act review tooling.** `[built]` `pdca act index` / `pdca act log` —
  independent of L1–L3, so you can run Act manually against any frozen bundles
  regardless of what else is wired.

**The punchline: you're not building L2–L4 — the harness already ships them.**
What's actually yours to build, in order, is what makes L2 *gate something real*
instead of running on stubs — the **per-project build order**:

1. **Gates, single-sourced** (`[[gates.checks]]`, above) — fill the real Tier
   1–4 rows so the driver and CI call the *same* implementation. This is the
   long pole: until it's done, Check runs on the all-PASS stub fallback and
   blocks nothing, no matter how `gating` is set. (It was a short pole for Wyrd,
   whose `cargo xtask ci` already existed — which is the point of delegating.)
2. **The leaf commands** (`[leaves.*]`, above) — flip `leaves_mode` from `stub`
   to `command` once you trust the gates to catch a bad build. Doing this
   *before* the gates exist means an ungated fix can reach sign-off looking
   green for the wrong reason.
3. **The batch queue and Act tooling** — nothing to build; `pdca flow` and
   `pdca act` are ready the moment 1 and 2 are.

This is exactly why [step 02](02-rehearse-offline.md) has you rehearse the whole
flow on stubs *before* either gates or leaves are real — it proves the L2–L4
plumbing the harness already ships, so the only genuinely new work left is
step 1: your gates.

---

## 1d. Install & doctor: verify the toolchain

Wiring `pdca.toml` says *what* each beat runs; it doesn't install anything or
confirm it's usable. Two commands close that gap — one provisions, the other only
reports.

### `make install` — provision every tool `flow` needs

```bash
make install         # idempotent — re-running is a no-op once everything's present
make install-check   # report-only: same checks, installs nothing; non-zero if a
                      # REQUIRED tool is missing (the CI preflight)
```

`make install` delegates to `scripts/bootstrap-tools.sh` (ships verbatim from the
template — it hardcodes no project gate toolchain of its own). On a machine of
unknown state it works through **three tiers, in order**, printing one
`OK|MISSING|INSTALLED|FAILED|WARN` row per tool as it goes:

**Tier 1 — harness-universal.** Needed regardless of what your leaves or gates
are:
- `python3` (≥3.11) — REQUIRED, not auto-installed (you're already running it).
- A pip-capable venv: probes `ensurepip` in the interpreter first; a clean
  Debian/Ubuntu lacks it, so it installs `python3-venv` via `apt` where possible,
  and falls back to bootstrapping `pip` from `get-pip.py` if even that's
  unavailable — the console-script install below never hard-fails on a pip-less
  stdlib.
- `git` and `gh`, installed via `sudo apt-get` when apt + sudo are available; if
  not, the exact command is printed and — since both are REQUIRED — the run exits
  non-zero rather than silently continuing without them. If `gh` is present but
  not authenticated, it warns (`gh auth login`) since publish/merge need it.
- Then it creates `.venv/` (if absent) and runs `pip install -e .` into it — that's
  what makes the project's console script (named per `pyproject.toml`
  `[project.scripts]`) available on `.venv/bin/`.

**Tier 2 — configured leaf backends.** It parses your rendered `pdca.toml` with
`tomllib` (not a grep — so TOML structure, comments, and each leaf's `mode` are
honoured) and collects the *distinct* `family` values across every `command`-mode
leaf: `builder`, `reviewer`, `planner`, `signoff`, `publisher`, `act`, plus any
`[[leaves.advisory]]`, `builder_variant`, and `builder_escalation` entries. A
`stub`-mode leaf contributes nothing — an all-stub render (step 02's territory)
needs no model CLI installed at all. The **builder's family is REQUIRED** (Do
can't run without it); every other family is optional, the same "advisory never
blocks" contract Check uses for gates. Each family maps to a binary (`claude`,
`codex`, `gemini`, or itself) and, where the harness knows one, an official
user-space installer — today that's `claude` via
`curl -fsSL https://claude.ai/install.sh | bash`; other families print a hint to
install their CLI yourself, since there's no known auto-installer. For Wyrd that
means `claude` is installed for you and `codex` is reported with a hint.

**Tier 3 — your project's gate toolchain (the instance-owned hook).** The script
itself hardcodes nothing here; it just runs what you've declared, in both
`install` and `install-check` mode:
- Every `scripts/bootstrap-tools.d/*.sh` script, in filename order — the
  drop-in extension point for anything not expressible as one command (a hook
  reads `$CHECK_ONLY` itself and must honour it).
- `[install].extra_bootstrap` from `pdca.toml` — **one** idempotent command run
  *last*, only on a real `install` (never under `--check`, which must install
  nothing — it's only echoed there for visibility):

```toml
[install]
extra_bootstrap = "rustup component add llvm-tools-preview"   # one idempotent command,
                                                              # e.g. a cargo-xtask gate's
                                                              # toolchain extras
```

  Both the drop-in scripts and `extra_bootstrap` survive `copier update` — they're
  your config/data, not template files the render owns. Neither declared ⇒ the
  script just reports "no project hook" and moves on. Wyrd declares neither: a
  Rust toolchain is assumed on the host, and everything beyond it is a `doctor`
  row with an install hint (below), so a missing tool is *reported* with its fix
  rather than installed behind the operator's back.

At the end it prints one summary line and sets the exit code from that alone: any
REQUIRED tool still missing ⇒ exit 1 (the failing rows are above); otherwise 0,
with a note if only optional pieces need attention. Run `make install` on a fresh
clone or in CI before the first `pdca flow`; run `make install-check` as the fast
preflight that reports the same rows without installing or mutating anything.

### `pdca doctor` — verify prerequisites, change nothing

Where `install` provisions, `doctor` only reports — one row per prerequisite,
`OK | MISSING | UNAUTH | WARN` plus a fix hint:

```bash
pdca doctor            # every prerequisite; exit 0 iff all REQUIRED rows pass
pdca doctor --strict   # escalate EVERY non-OK row (including WARN) — for CI
```

Most rows are **derived from `pdca.toml` automatically**, so they track your edits
with no extra config: every leaf's `argv[0]` on `PATH` (with a per-family auth
probe), `gh` present and authenticated (needed for publish), the bundle root
writable, the tracker's `notes_cmd` tool resolvable, and — when a leaf sandbox is
configured — its dependencies.

Instance-specific prerequisites (a Docker engine, a client library, a coverage
tool…) are declared as data, the same pattern as `[[gates.checks]]`. Two of
Wyrd's twenty-odd rows:

```toml
[[doctor.checks]]
group = "engine"
id = "docker"
cmd = "docker info"
hint = "https://docs.docker.com/engine/install/ — the TiKV and FoundationDB conformance legs each bring up a Docker cluster"
level = "WARN"          # status when it FAILS (default MISSING); WARN = optional

[[doctor.checks]]
group = "engine"
id = "cargo-deny"
cmd = "cargo deny --config /nonexistent/deny.toml check --help >/dev/null"
hint = "cargo install cargo-deny --locked (needs >= 0.20.0) — `cargo xtask ci`'s ADR-0003 dependency wall passes `--config` at the ROOT (xtask/src/lib.rs), which 0.19.x rejects outright; the gating C4-ci row then fails on every bundle with a message that names a flag rather than the stale tool"
level = "WARN"
```

The second row carries a lesson an Act review paid for ([step 06](06-act.md)):
it probes the tool's *invocation*, not its presence. A `cargo deny --version`
check passed on a version too old for the gate's own command line, and two
bundles went red with a message about a flag. Probing the shape the gate uses,
against a throwaway config, fails exactly when the gate would.

An instance whose gates use sibling per-lane checkouts can add a `per_lane` row,
so `doctor` checks every lane's resource exists before a parallel batch run:

```toml
[[doctor.checks]]
group = "workspace"
id = "lane worktrees lane{lane}"
cmd = "test -e ../repo-lane{lane}/.git"
hint = "make worktrees LANES={lanes}"
per_lane = true
```

Wyrd needs none: the driver's own worktree isolation ([step 04](04-do.md)) gives
each lane a private checkout of the target, and its engine scripts scope the
worktrees they cut by `$PDCA_LANE` themselves.

**This is also where a brief's `External dependencies` field cashes out.** A
brief may name a per-bundle requirement — `protoc`, a live etcd cluster, whatever
that fix's slice needs to build and go red→green — as a backticked token, and
that token MUST equal a registered `[[doctor.checks]]` `id`. The driver
reconciles the two at Plan exit (before Do dispatches) and again at Check as a
backstop: an unregistered token routes into SUMMARY §6 and blocks accept until
you add the matching row here. So a doctor row isn't only a one-time
machine-readiness check — it's the registry a brief's dependency claims are
checked against for the life of the bundle. See [step 03](03-plan.md)'s field
table for the brief side of this contract.

Run `pdca doctor` right after `make install`, and again any time you add a gate,
an `extra_bootstrap` step, or change a leaf's family — it's the fast way to tell
whether a red gate is a real fix regression or just a missing tool.

### `make setup` — grant the interactive Claude leaves workspace read

`install` gets tools onto the machine and `doctor` confirms they're usable;
`setup` is a third, narrower thing — a one-time **permission** grant, and only
relevant if a Claude CLI is one of your leaf backends. Without it, the
interactive leaves (planner, signoff, publisher, act) prompt you file-by-file the
first time they need to read something outside the project.

The **target checkouts** are no longer part of that: the driver admits them
itself, on the spawn's own argv (`--add-dir` and the other families' equivalents),
exactly as it already did for the headless builder and reviewer — each interactive
session gets the primary checkouts its bundles' briefs resolve to, and Plan, which
runs before a brief exists, gets the instance's known targets
(`[publisher.checkouts]` ∪ what the existing briefs resolve to). Never a lane
worktree, never a directory neither the config nor a brief names (issue #494). So
`make setup` is for what remains outside: a build output dir, a docs toolchain,
a second checkout no brief mentions.

```bash
make setup
```

It computes `ws` — the parent directory of your project (the sibling-checkouts
workspace, e.g. where Wyrd keeps `../wyrd` alongside `wyrd-pdca`) — and writes
the machine-local, gitignored `.claude/settings.local.json`:

```json
{
  "permissions": {"allow": ["Read(/abs/path/to/workspace/**)"]},
  "additionalDirectories": ["/abs/path/to/workspace", "/tmp"]
}
```

That's it: read access to the whole sibling workspace plus `/tmp`, nothing more —
it's a **permissions** file, not project trust. If your gates or leaves reach into
other directories (a build output dir, a second sibling checkout), add them to
this file by hand; `make setup` only seeds the common case.

**Trust is a separate, global gate** the harness deliberately doesn't touch:
folder trust lives in `~/.claude.json`, not in this repo, and the *first*
interactive `pdca flow` (or `<cli> flow`) prompts you to trust the project once —
accept it there. `make setup` runs before that and only prevents the
*permission* prompts that would otherwise fire per file after you've trusted it.

---

## Before you go live

With `make install` done, `pdca doctor` green, and (if you're on Claude leaves)
`make setup` run once, you're ready to **prove the control flow offline before
spending a single model token** — that's [step 02](02-rehearse-offline.md).

← [00 Introduction](00-introduction.md) · [Index](README.md) · next: [02 Rehearse offline →](02-rehearse-offline.md)
