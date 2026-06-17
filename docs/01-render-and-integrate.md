# Step 01 — Render the harness & integrate your repo

← [00 Introduction](00-introduction.md) · [Index](README.md) · next: [02 Rehearse offline →](02-rehearse-offline.md)

This step gets you from "I have the template" to "the driver knows how to build,
gate, and publish for *my* repo." There are three sub-steps: **render**,
**concretize** (`docs/INTEGRATION.md`), and **wire** (`pdca.toml`).

---

## 1a. Render the template

The harness is a [Copier](https://copier.readthedocs.io/) template. Rendering it
copies the project-agnostic parts into a new directory and asks you a handful of
questions:

```bash
copier copy gh:eduralph/pdca-harness ../gramps-testbed-v2
cd ../gramps-testbed-v2
```

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
  process/act-log.md        # Act log (step 07)
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
reviewer) run unattended. The agent definitions themselves live under
`.claude/agents/<name>.md`.

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

---

## Before you go live

Run `make setup` once (grants the interactive Claude leaves read of your
workspace so they don't prompt per-file). Then **prove the control flow offline
before spending a single model token** — that's [step 02](02-rehearse-offline.md).

← [00 Introduction](00-introduction.md) · [Index](README.md) · next: [02 Rehearse offline →](02-rehearse-offline.md)
