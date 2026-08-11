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
copier copy gh:eduralph/pdca-harness ../gramps-testbed-v2
cd ../gramps-testbed-v2
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

The prompts (from [`copier.yml`](../copier.yml)) and what gramps answered:

| Prompt | What it sets | gramps-testbed-v2 |
|--------|--------------|-------------------|
| `project_name` | Human-readable name | `Gramps Testbed v2` |
| `project_slug` | Slug for paths/package | `gramps-testbed-v2` |
| `author_name` / `author_email` | §9 attribution default | `Eduard Ralph` |
| `tracker_system` | github / mantis / jira / other | `mantis` |
| `tracker_url` | Base tracker URL | `https://gramps-project.org/bugs` |
| `issue_id_example` | ID shape in briefs/commits | `13418` |
| `default_branch` | Where fixes land unless overridden | `main` |
| `bundle_root` | Per-cycle bundle dir | `results` |
| `process_dir` | Cross-cycle process artifacts | `process` |
| `builder_family` | Model for the Do leaf | `claude` |
| `reviewer_family` | Model for the Check reviewer — **should differ from builder** | `codex` (gramps later used a separate `claude` agent) |
| `leaves_mode` | `stub` (offline) or real commands | `stub` to start |

> **Start in `stub` mode.** You want the offline rehearsal in
> [step 02](02-rehearse-offline.md) working before you point any beat at a real
> model. The render message says the same thing: *"Replace the stub leaves once
> the deterministic gates exist (build order: gates → driver → batch → Act)."*
> See *The build ladder* below (1c) for why that order matters.

After rendering you have:

```
gramps-testbed-v2/
  pdca.toml                 # driver config — you edit this (1c below)
  PCDA/quality-cycle/       # the vendored model spec (reference; don't edit)
  docs/INTEGRATION.md       # your repo's concretizations — you fill this (1b below)
  src/pdca_harness/         # the driver (don't edit; updated via `copier update`)
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

Here is how gramps filled a few, verbatim from
`gramps-testbed-v2/docs/INTEGRATION.md`:

**Item 2 — Branch-target rules** (gramps fixes don't all go to one branch):

```markdown
- gramps **core** fixes → `maintenance/gramps61` (v6.1.0), forward-merged to `master`
- **addon** fixes (`addons-source`) → `maintenance/gramps60`; maintainer cherry-picks
  forward to gramps61
- **testbed itself** → `main`
- Branch from `upstream/<base>`, not the fork's tracking copy
- Validation target — UPSTREAM by default. Bundles validated against clean
  `upstream/maintenance/gramps6{0,1}` in pinned worktrees `make worktrees` builds
```

**Item 3 — test placement** (this exact rule is what the Plan leaf cites when it
names a brief's *Test file*):

```markdown
- core = `test/` package (singular) + `<module>_test.py` (suffix)
- addon = `tests/` package (plural) + `test_<thing>.py` (prefix)
```

**Item 4 — the conformance ladder.** Each tier names a *written ruleset*, a
*home* (the file that implements the check), and a *single command*:

```markdown
| Tier | Written ruleset | Command | Status |
|---|---|---|---|
| T1 structure | doc16 §Structure | python3 ./engine/conformance/gate.py T1 | advisory, bundle |
| T2 shape | GPL header MUST + §Logging + POTFILES | python3 ./engine/conformance/gate.py T2 | advisory, bundle |
| T3 runtime | gramps test suite, baseline-diffed | t3_baseline.py run-unit.sh | advisory, matrix 6.0/6.1 |
| T4 contribution | §Commit messages; 4-section PR body | python3 ./engine/conformance/gate.py T4 | advisory, bundle |
```

The lesson: **the harness doesn't ship gate logic — it ships the *contract* for
gates.** You write the checkers (gramps put them under `engine/conformance/`) and
point `pdca.toml` at them (1c).

---

## 1c. Wire: `pdca.toml`

`pdca.toml` is the driver's only config. Two things matter most: the **leaves**
(the commands each beat runs) and the **gates** (the deterministic Check
oracles). Here is gramps' real config, trimmed to the shape.

### The leaves — one command per beat

```toml
[leaves.planner]
mode = "command"
family = "claude"
interactive = true                                  # opens in your terminal
argv = ["claude", "--model", "claude-opus-4-8", "--effort", "xhigh",
        "--agent", "planner", "--permission-mode", "acceptEdits"]

[leaves.builder]
mode = "command"
family = "claude"
interactive = false                                 # headless — runs unattended
argv = ["claude", "-p", "--agent", "builder", "--permission-mode", "acceptEdits",
        "--allowedTools", "Read,Edit,Bash(git *),Bash(python3 *)"]

[leaves.reviewer]
mode = "command"
family = "claude"                                   # a decorrelated reviewer agent
interactive = false
argv = ["claude", "-p", "--agent", "reviewer", "--permission-mode", "acceptEdits",
        "--allowedTools", "Read,Edit"]

[leaves.signoff]
interactive = true                                  # YOU, at the §9 step
argv = ["claude", "--agent", "signoff", "--permission-mode", "acceptEdits"]
# ...and publisher, act — same shape
```

The interactive leaves (planner, signoff, publisher, act) open Claude in your
terminal because they're the human touch points. The headless ones (builder,
reviewer) run unattended.

Every interactive session also has a **checked exit** (issue #331): before you
end it, run `/handoff <issue-id>` — it verifies the session's expected artifact
exists and is well-formed, and reports PASS or FAIL with the items to fix. The
same contract is enforced mechanically at session end by a Stop hook, so a
session can't silently close with its artifact missing or malformed; a
deliberate walk-away is recorded with
`python3 .claude/hooks/handoff_guard.py --abandon "<why>"` rather than by
ignoring the guard. Each agent's role prompt lives in a canonical,
vendor-neutral body at `agents/<name>.md`; Claude leaves additionally get
`.claude/agents/<name>.md` (a frontmatter wrapper that includes that body, so
`--agent` resolves), materialized only when the leaf's family is `claude`.
Non-Claude leaves read the `agents/<name>.md` body directly.

> **Decorrelate the reviewer.** The model recommends the Check reviewer be a
> *different* family from the builder (the template default is `codex`), so the
> reviewer doesn't share the builder's blind spots. gramps used a separate
> `claude` agent with a narrowed toolset — acceptable, but a cross-vendor
> reviewer is stronger.

### The gates — your deterministic Check oracles

A gate is a command plus metadata. `gating = true` means a failure **blocks** the
sign-off; `gating = false` is advisory (it still shows in the SUMMARY, but a human
adjudicates). `scope` is `bundle` (this fix) or `repo` (whole tree).

```toml
[gates]
target_default = "core"

# C4 — the one GATING per-fix correctness check
[[gates.checks]]
id = "C4-verify"
tier = "C4"
label = "fix verified: test red pre-fix, green post-fix"
cmd = "./engine/scripts/ubuntu/run-verify.sh"     # applies patch, runs ONLY its test
gating = true
scope = "bundle"

# T3 — whole gramps unit suite, baseline-diffed (ADVISORY)
[[gates.checks]]
id = "T3-unit"
tier = "T3"
label = "runtime: gramps core unit suite (whole-suite baseline)"
cmd = "CORE_VERSION=6.1 python3 ./engine/conformance/t3_baseline.py ./engine/scripts/ubuntu/run-unit.sh"
gating = false
scope = "repo"

# T1/T2/T4 conformance checkers (doc16-founded, ADVISORY)
[[gates.checks]]
id = "T1-structure"
tier = "T1"
cmd = "python3 ./engine/conformance/gate.py T1"
gating = false
scope = "bundle"
# ...T2-shape, T4-contribution likewise
```

The key design choice you're making here: **what blocks, and what merely
informs.** gramps made exactly one check gating — `C4-verify`, the red→green
proof that the fix actually fixes the bug — and left every conformance tier
advisory, so a pre-existing lint failure or an environmental test segfault
surfaces to a human (in §6 NEEDS-HUMAN) rather than silently blocking a correct
fix. You'll see that play out in [step 05](05-check.md).

One more wiring question the gate ladder can't answer for you: **does the host
repo's CI run anything on every PR that your gates don't?** A spell-checker, a
docs linter — jobs like that let a bundle pass Check green and open a PR that
immediately fails a required status. Declare them in `[gates].host_ci` (issue
#311) and the harness runs each one against the patched tree at Check *and*
re-runs it at publish, immediately before anything is pushed — a failure there
refuses the push. Empty or absent changes nothing.

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
  tracker-specific.
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
   blocks nothing, no matter how `gating` is set.
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
install their CLI yourself, since there's no known auto-installer.

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
extra_bootstrap = "pip install -e .[test]"    # gramps-shaped example; rustup for a
                                               # cargo-xtask gate, etc.
```

  Both the drop-in scripts and `extra_bootstrap` survive `copier update` — they're
  your config/data, not template files the render owns. Neither declared ⇒ the
  script just reports "no project hook" and moves on.

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

Instance-specific prerequisites (a Docker engine, sibling checkouts, a scraper
browser…) are declared as data, the same pattern as `[[gates.checks]]`:

```toml
[[doctor.checks]]
group = "engine"
id = "docker"
cmd = "docker info"
hint = "https://docs.docker.com/engine/install/ — the gates run in a container"
level = "WARN"          # status when it FAILS (default MISSING); WARN = optional
```

gramps also declared a `per_lane` row so `doctor` checks every lane's sibling
worktree exists before a parallel batch run:

```toml
[[doctor.checks]]
group = "workspace"
id = "lane worktrees lane{lane}"
cmd = "test -e ../repo-lane{lane}/.git"
hint = "make worktrees LANES={lanes}"
per_lane = true
```

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
first time they need to read something outside the project, e.g. a sibling
checkout your gates or briefs reference.

```bash
make setup
```

It computes `ws` — the parent directory of your project (the sibling-checkouts
workspace, e.g. where gramps kept `../gramps` and `../addons-source` alongside
`gramps-testbed-v2`) — and writes the machine-local, gitignored
`.claude/settings.local.json`:

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
