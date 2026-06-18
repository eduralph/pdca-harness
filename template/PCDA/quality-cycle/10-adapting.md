---
title: "Adapting the Harness to a Project (worked example: Gramps Testbed v2)"
categories: []
managed: false
status: active
---

<!-- Template-native doc (born in the pdca-harness template, not vendored from the wiki). Companion to [05 - Repository Integration](05-repository-integration.md): 05 is the *requirements checklist* (what every repo must provide); this is the *render-to-running playbook* (how one concrete project, gramps-testbed-v2, was adapted from the template — and how its harness improvements flow back). Re-edit here when the template's adaptation surface changes. -->

> How to turn this template into a project that **runs** the cycle, walked end-to-end with a real instance — `gramps-testbed-v2`, rendered from this exact template. Where [05 - Repository Integration](05-repository-integration.md) lists *what* a repo must supply and [07 - Case Study - CI Hardening](07-case-study-ci-hardening.md) shows the cycle *running*, this doc shows the *adaptation itself*: render → fill config → build gates → wire intake/publish → and the discipline that keeps an instance and the template from diverging. The gramps specifics (Mantis, AT-SPI/dogtail, the doc-16 ruleset, the addon×core matrix) are **one project's answers** — read the *shape*, fill it your way.

> **Maturity legend** (as in [03 - Cycle Automation](03-cycle-automation.md)): **[built]** ships in this template and runs as-is; **[render-time]** is chosen once when you `copier copy`; **[project-provided]** you write per project because it is repo-, tracker-, or runner-specific. The harness machinery is **[built]**; the integration is **[render-time]** + **[project-provided]**.

## The one idea: a hard boundary between machinery and instance

An adapted project is the template's **machinery** (a Python package + deterministic driver, copied unchanged) wrapped around **your project's integration** (config values, gates, prompts, an engine). Keeping that boundary sharp is the whole discipline — it is what lets the template improve without forking, and an instance customize without drifting. Every file falls into exactly one of three classes:

| Class | What it is | Rendering | Who owns it | Examples |
|---|---|---|---|---|
| **Verbatim machinery** | The driver, leaves, gates engine, flow, CLI, their tests | copies byte-for-byte (`template/src/**`, `template/tests/**` — **no** `.jinja`) | the **template** | `src/pdca_harness/{flow,driver,gates,leaves,publish}.py`, `tests/test_*` |
| **Genericized (jinja)** | Config, docs, agent prompts with project tokens swapped for copier vars | rendered (`.jinja` suffix; `{{ project_name }}` etc.) | the **template**, instantiated at render | `pdca.toml.jinja`, `.claude/agents/*.md.jinja`, `CLAUDE.md.jinja`, `docs/INTEGRATION.md.jinja` |
| **Instance-only** | Your verification engine, tracker scraper, ruleset, gate rows | written in the instance; never goes back to the template | **you** | `engine/**`, the `[[gates.checks]]` rows, the tracker CSV, branch conventions |

**The rule that makes it work, both directions:**

- *Adapting (template → instance):* you only ever touch the **render-time** answers, the **`[[gates.checks]]`** rows, and the **`engine/`**. You do **not** edit `src/pdca_harness/*` — if you feel the urge to, that is a signal the *template* needs a change (see the feedback discipline below), not the instance.
- *Improving (instance → template):* a fix you make to the machinery while running a real cycle is a **template** change wearing an instance's clothes. It must flow back, or the next `copier update` overwrites it. File it as an `enhancement` issue on the template repo for exactly this — see [§ Feeding improvements back](#feeding-improvements-back-the-non-optional-part).

Everything below is organised by that boundary.

## Step 1 — Render the project (`copier copy`) — *[render-time]*

```
copier copy gh:<you>/pdca-harness path/to/your-project
cd path/to/your-project && copier update      # later, to pull template fixes
```

Copier asks the questions in `copier.yml`; each answer is a one-time integration decision baked into `pdca.toml` / the agents / docs. Only `project_name` is required. The answers gramps-testbed-v2 gave:

| Question | copier var | Gramps value | Notes for your project |
|---|---|---|---|
| Project name | `project_name` | `Gramps Testbed v2` | free text |
| Slug | `project_slug` | `gramps-testbed-v2` | defaults from the name |
| Description | `project_description` | "CI/CD harness for interface-testing Gramps and third-party addons via AT-SPI / dogtail" | one line |
| Author / email | `author_name` / `author_email` | `Eduard Ralph` / … | default §9 sign-off attribution |
| Tracker system | `tracker_system` | `mantis` | `github` / `mantis` / `jira` / `other` |
| Tracker URL | `tracker_url` | `https://gramps-project.org/bugs` | base URL |
| Issue id shape | `issue_id_example` | `13418` | how an id reads in briefs/commits/PRs |
| Default branch | `default_branch` | `main` | fixes target this unless an area rule overrides |
| Bundle / process dirs | `bundle_root` / `process_dir` | `results` / `process` | usually the defaults |
| Builder family | `builder_family` | `claude` | the Do leaf's model |
| Reviewer family | `reviewer_family` | `codex` | **MUST differ** from the builder (decorrelation, [04](04-validation-tooling.md) §reviewer); see the fallback note in Step 2 |
| Leaves mode | `leaves_mode` | `stub` | **start at `stub`** — the driver runs fully offline; flip to `command` once leaves are real |

After render, the vertical slice runs immediately on stubs (`make rehearse ID=TOY`, or the `init-issue`/`run`/`status` sequence the post-copy message prints). That offline-green slice is your proof the machinery landed before you write a single project-specific line.

## Step 2 — Fill `pdca.toml` — *[render-time] + [project-provided]*

Render seeds `pdca.toml` from your answers; you then complete the project-specific runtime config. The sections:

**`[project]` / `[paths]` / `[tracker]`** — mostly your render answers. Two fields you set by hand:
- `[tracker].issue_trailer` — the commit/PR trailer linking to the issue (`Fixes #{id}`; Jira `Fixes PROJ-{id}`; `""` for none). The publish commit-msg ends with it and the T4 gate (if any) checks it.
- `[tracker].export_csv` *(optional)* — a tracker export the Plan leaf reads the issue's row from when `flow` runs without `--from-csv`. Gramps: `engine/20260529 - Mantis Export.csv`. The leaf reads **only that issue's row**, never a repo scan.

**`[publisher]`** — the contribution mechanics ([03](03-cycle-automation.md) §Check closing step):
- `fix_branch_pattern` / `feature_branch_pattern` — `{id}`/`{slug}` format strings. Gramps uses the default `fix/{id}-{slug}` shape (its real convention `fix/bug-{id}-{slug}` is an instance tweak).
- `[publisher.checkouts]` — a `repo_spec → local checkout` map; **only the exceptions**. Unmapped repos fall back to the sibling convention (`<project>/../<repo-last-segment>`), so a fork laid out as a sibling needs no entry. The publisher derives the PR `--head` owner from that checkout's `origin` (`OWNER:BRANCH`), so the checkout must be your **fork** with `upstream` + `origin` remotes.

**The six leaves** — `mode`/`family`/`interactive`/`argv` each. Render fills `argv` for the families you chose; you flip `mode = "command"` when ready. Two things worth stating plainly:
- **`PDCA_LEAVES_MODE=stub` forces every leaf to stub**, regardless of `pdca.toml`. CI and `make` set it so the shipped tests run with no model/TTY. Keep that escape hatch — it is what makes the slice deterministic.
- **Reviewer decorrelation is the ideal; same-vendor is the documented fallback.** The template defaults the reviewer to a *different* family (codex) because independence is a Check property ([06](06-quality-cycle-guidelines.md) C-tier). gramps-testbed-v2 deliberately flips its **running** config to `family = "claude"` (`--agent reviewer`) so the live demo runs on Claude alone — the documented fallback, with the independence then enforced *physically* by the reviewer sandbox ([02](02-cycle-artifacts.md) independence contract), not by vendor. Choose decorrelated if you can; if you take the fallback, know you are leaning on the sandbox.

**`[gates]`** — the deterministic Check gates. This is the long pole; it gets its own step.

## Step 3 — Build the verification engine and gate rows — *[project-provided]*

The gates are the only blocking path in Check, and they are entirely yours: the template ships **zero** gate rows (`checks = []`, all-PASS stubs) so the slice runs, and an `engine/README.md` pointing at where your checkers go. Each `[[gates.checks]]` row runs a shell command that PASSES iff it exits 0. Two scopes:

- `scope = "bundle"` — needs the patch context; `$PDCA_BUNDLE` is exported (the gate derives its target from `$PDCA_BUNDLE/patch.diff`). Runs locally only. When the brief declares an `Onto branch:` (stack mode, issue #54), `$PDCA_BASE` is also exported as `<remote>/<branch>` — the existing PR's head the fix stacks onto; a verify/repro gate should establish red→green against `$PDCA_BASE` (the same branch publish commits onto and pushes to), not a clean upstream base. Absent ⇒ no `$PDCA_BASE`, unchanged.
- `scope = "repo"` — runs against the working tree; this is what CI re-runs via `pdca gates --working-tree` (the merge re-gate, [09](09-parallel-lanes.md)). **Same command** local and CI — single-sourced, no drift.

gramps-testbed-v2's rows, as a model of the **gating policy** (hard-won — read the long comment in `pdca.toml.jinja`):

| id | tier | scope | gating | what it is |
|---|---|---|---|---|
| `C4-verify` | C4 | bundle | **true** | the per-fix red→green: apply `patch.diff`, run **only** its test, assert green-with-fix / red-with-the-production-change-reverted |
| `T1-structure` / `T2-shape` / `T4-contribution` | T1/T2/T4 | bundle | false | conformance tiers mechanized from the project ruleset (gramps: doc 16) |
| `T3-unit` / `T3-addon-unit-60` / `T3-addon-unit-61` / `T3-interface` | T3 | repo | false | whole-suite/runtime tiers on the unmodified tree |

The instructions baked into that table, generic to any project:

1. **Exactly one gate ships `gating = true`: the per-fix C4 (red→green).** It is the only check that validates *this* change. Every other tier (runtime, conformance, interface) audits code the current fix did not introduce, so gating on pre-existing/legacy non-conformance is wrong. Promote a tier to gating only once its targeted artifacts are clean.
2. **A whole-suite gate can't gate a single fix** if the tree carries *any* pre-existing red. Ship runtime suites **advisory**; make the bundle-scoped C4 the gating correctness check.
3. **For an E2E/interface tier, gate a smoke test** ("does the app start"), not the full suite — the full suite mixes green tests with reproductions of *unmerged* upstream bugs, so it is a characterization, not a pass/fail signal.
4. **Don't re-raise standing reds every run.** Wrap a whole-suite gate in a **baseline-diff**: parse its result, diff against a checked-in baseline manifest, exit non-zero only on a *delta* (a new failing test, or a cleared baseline red). Otherwise the reviewer + human re-diagnose the same reds every cycle. When the manifest carries *both* per-test ids and whole-run signatures, a matching run-level signature classifies the run as **baseline even when per-test failures parsed** — the same crash often surfaces in per-test form (e.g. a headless-GUI `setUpClass` error), so a per-test parse must not shadow the signature into a spurious delta.
5. **A patch-and-revert (C4) gate must clean patch-*added* files by removal**, not `git checkout` (a brand-new file is untracked — `checkout` aborts under `set -e` and leaves it to dirty the next run). Revert *modified* files, `rm` *added* ones — or run against a throwaway `git worktree`.
6. **Cite each tier back to a stable anchor** in your normative ruleset (gramps: doc-16 section headings, not line numbers — line anchors rot on every edit), **select the ruleset by contribution target** (core vs addon have different rules), and ship an **anchors-exist test** so a renamed heading fails the suite instead of dangling.
7. **Every containerized/long test run needs a timeout.** Wrap `docker run` (or any runner) in a hard `timeout` + a named container you kill on expiry, so a hung test *fails* the run instead of blocking the cycle forever. The builder must use *this* runner, never improvise its own — which is exactly what the builder agent now says.

The gate *implementations* live under `engine/` and are 100% instance-only. The harness needs **no change** to host them — `scope="bundle"` + the 5/5/1 overlay-by-`tier` ([04](04-validation-tooling.md)) already carry any project's tiers.

### Mapping the gramps gates onto Check's 5/5/1 (this is the heart of it)

[04 - Validation Tooling](04-validation-tooling.md) gives the **5/5/1 × tooling** matrix abstractly, with a worked example at an *earlier* snapshot (the proposed `agent-work/dev-tooling/` layout). Here is what gramps-testbed-v2 actually **ships**, every gate row mapped to the 5/5/1 element it covers, the engine file that implements it, and its home — so you can see the abstract matrix as concrete code:

| `pdca.toml` gate (`cmd`) | 5/5/1 element ([04](04-validation-tooling.md)) | Engine implementation | Scope · home | Gating |
|---|---|---|---|---|
| `C4-verify` — `./engine/scripts/ubuntu/run-verify.sh` | **Correctness 2 (Reproduction)** + **Correctness 4 (Verification)** — red-without-fix *and* green-with-fix in one runner | `engine/scripts/ubuntu/run-verify.sh` (Docker, timeout-wrapped; classifies patched files added-vs-modified to revert correctly) | bundle · local driver | **true** |
| `T1-structure` — `python3 ./engine/conformance/gate.py T1` | **Conformance T1 (Structure)** — folder==id, `gramps_target_version`, `fname` resolves, no addon `__init__.py` | `engine/conformance/t1_structure.py` citing `doc16.py` §Structure | bundle · (canonical home: upstream addons-source CI; here a mirror) | false |
| `T2-shape` — `gate.py T2` | **Conformance T2 (Shape)** — GPL header, no diagnostic `print` | `engine/conformance/t2_shape.py` citing `doc16.py` §Coding style | bundle · (staging, pre-upstream) | false |
| `T3-unit` / `T3-addon-unit-60` / `T3-addon-unit-61` / `T3-interface` — `t3_baseline.py <runner> …` | **Conformance T3 (Runtime)** — core suite, the addon×core **matrix** (`CORE_VERSION=6.0`/`6.1`), and the GUI **smoke** | `engine/conformance/t3_baseline.py` (baseline-diff) wrapping `engine/scripts/ubuntu/run-{unit,addon-unit,interface}.sh`, diffed against `engine/baselines/*.json` | repo · upstream CI mirror (the merge re-gate, [09](09-parallel-lanes.md)) | false |
| `T4-contribution` — `gate.py T4` | **Conformance T4 (Contribution)** — commit/PR wrapper vs §Commit messages + §Contributor workflow | `engine/conformance/t4_contribution.py` citing `doc16.py` | bundle · (the gramps/addons **forks**) | false |
| *(no gate)* | **Correctness 5**, **Conformance T5**, **Validation** — the judgment path | the **sandboxed reviewer** (advisory) → `check-review.md`; **human §9 sign-off** | Check's reviewer + sign-off components | — |

Read down that table and the doc-04 concepts stop being abstract:

- **The gates path vs the judgment path** ([04](04-validation-tooling.md) §matrix observation 2–3) is literally the split between the rows with an `engine/` file and the last row with none. Mechanizable conformance (T1/T2/T4) and the correctness re-runs (C2/C4) are `engine/` code; C5/T5/validation have **no gate by design** — they are the reviewer (`leaves._run_review_sandboxed`, build-notes physically withheld) plus the human, and the reviewer emits the full 5/5/1 verdict table into `check-review.md`, routing anything unresolved to §6 NEEDS-HUMAN.
- **The dispatcher is one file.** `engine/conformance/gate.py T1|T2|T4` is the single bundle-scoped entry the three conformance rows share: it reads `$PDCA_BUNDLE/patch.diff`, derives the target, and **selects the ruleset by target** (core vs addon have different doc-16 rules) — doc 04's "what and where are independent" made mechanical.
- **Citing the ruleset by a stable anchor** ([04](04-validation-tooling.md) is downstream of [06](06-quality-cycle-guidelines.md)'s ruleset) is `engine/conformance/doc16.py`: an indirection that cites doc-16 **section headings, not line numbers**, with an anchor-drift guard test (`engine/tests/test_conformance.py::Doc16Anchors`) so a renamed heading fails the suite instead of leaving a dangling citation.
- **Single-sourcing** ([04](04-validation-tooling.md) §Single-sourcing) is `engine/scripts/lib/` — the shared `.gpr.py` exec-shim, the `requires_mod`/dep extractors (`addon_python_deps.py`, `addon_system_deps.py`), the `gi` bootstrap — imported by *both* the unit and addon runners, so there is exactly one implementation. And because T3 is `scope = "repo"`, the **same `run-*.sh`** the local driver runs is what CI re-runs over the merged tree; "passes locally" and "passes CI" collapse to one command.
- **The two families** ([04](04-validation-tooling.md) §Two rule families): every shipped gate above is **Family A** (conformance of gramps's *own* contributions). **Family B** (core defect-hunting whose findings become *upstream* PRs) is deliberately **not** wired as a Check gate — a Family B finding is a new contribution to plan, not a gate on this fix. Keeping it out of `[gates]` is the layout discipline doc 04 insists on.
- **The gates are code, so they are tested too.** `engine/tests/` carries the engine's own suite — `test_conformance.py` (tier checkers + anchor drift), `test_t3_baseline.py` (the baseline parser), `test_verify_classification.py` (the added-vs-modified revert logic), `test_root_resolution.py`. A validation tool you don't test is a gate you can't trust.

The as-built layout, annotated by which doc-04 role each piece plays:

```
engine/
  conformance/
    gate.py              # bundle-scoped DISPATCHER (T1/T2/T4) — reads $PDCA_BUNDLE/patch.diff, selects ruleset by target
    t1_structure.py      # Conformance T1 — Structure
    t2_shape.py          # Conformance T2 — Shape
    t4_contribution.py   # Conformance T4 — Contribution
    t3_baseline.py       # Conformance T3 — the BASELINE-DIFF wrapper (only flags NEW reds)
    doc16.py             # ruleset CITATION indirection (cite by §heading; select core/addon)
  scripts/
    ubuntu/run-verify.sh        # Correctness 2+4 — the GATING per-fix red→green (timeout-wrapped Docker)
    ubuntu/run-unit.sh          # T3 core suite
    ubuntu/run-addon-unit.sh    # T3 addon×core matrix (CORE_VERSION pin, git-worktree per version)
    ubuntu/run-interface.sh     # T3 GUI smoke (headless dogtail/AT-SPI under Xvfb)
    lib/                        # SINGLE-SOURCED shared helpers (exec-shim, dep extractors, gi bootstrap)
    mantis_notes.py / scrape-mantis.sh   # tracker intake (Plan), not a gate
  interface/             # dogtail/AT-SPI suite — Correctness 2 repro fixtures + the T3-interface smoke
  baselines/*.json       # recorded T3 baselines (run-unit, run-addon-unit-6{0,1}, run-interface)
  tests/                 # the engine's OWN tests — the gates are code, and code gets tested
```

Doc 04's worked example shows the *earlier* by-family proposal (`addon-conformance/tierN-*`, `core-analysis/`); the shipped shape collapses the conformance tiers into `engine/conformance/*.py` behind the `gate.py` dispatcher and the runtime runners into `engine/scripts/ubuntu/`. **Same 5/5/1 matrix, shipped form** — and a useful illustration that doc 04 names the *roles*, not a mandatory directory tree.

One timing subtlety worth carrying: **T4's inputs (`commit-msg.txt` / `pr-description.md`) don't exist until you publish**, so T4 is correctly **N/A** on a bundle that hasn't reached the publish step — publishing is what makes T4 a *real* check ([03](03-cycle-automation.md) §Check closing step). The 5/5/1 overlay renders it as N/A, not a failure, until then.

## Step 4 — Wire the tracker intake (the Plan leaf) — *[project-provided]*

Plan turns a tracker item into a `brief.md`. Hand the planner *where work comes from* rather than letting it guess:

- Point `[tracker].export_csv` at an export, or pass `flow --from-csv`. The planner prompt already names the issue id, the tracker coordinates, the CSV row to read first, and a notes-file convention — and tells the planner to cite via `git -C <repo>` (never `cd <repo> && git`) and **not** to scan the harness repo.
- The scraper that *produces* the export (gramps: a Playwright/Chrome Mantis scraper, `engine/scripts/mantis_notes.py`) is instance-only. The *mechanism* of feeding an export + notes to the planner is template machinery.
- **Permissions & trust are a one-time setup, and they are two different things.** `make setup` writes `.claude/settings.local.json` (read access to the workspace + the sibling repos you patch). Folder **trust** is *not* settable from project settings — it lives in the global `~/.claude.json` (`projects[<path>].hasTrustDialogAccepted`) and must be accepted once, interactively. `make setup` does not suppress the trust prompt; say so to whoever runs it.
- Prefer `git -C <repo>` over `cd <repo> && git` everywhere — a bare `Bash(git …)` allow-rule doesn't match `cd && git`, and the latter trips Claude Code's "cd-before-git can run hooks" prompt.

## Step 5 — Adapt the agent prompts only where the *engine contract* differs — *[render-time]*

The six agents (`.claude/agents/*.md.jinja`) ship generic and render with your `{{ project_name }}`. You should **not** rewrite them per project — but two classes of edit are legitimately yours, and both are about keeping the prompt **true to your engine**:

- **Capability facts the runner actually provides.** A leaf's prompt is part of the harness contract: if it claims the test runner gives a display/GUI/bus and yours is headless, a GUI-importing test crashes — and recurs every iterate-do because nothing corrects the belief. The builder prompt now states the runner *may* be headless and to keep the unit-under-test import-light; make such claims match *your* engine.
- **The `builder_guard.py` hook path must be rooted.** It runs from the bundle dir, so a relative `python3 .claude/hooks/builder_guard.py` resolves there, fails to exist, and (exit 2) blocks **all** Bash for the whole Do session. The template ships it rooted (`$CLAUDE_PROJECT_DIR/...`); keep it that way.

Everything else in the agents — the STOP discipline, the write allow-lists, the "interactive leaf ends its own session", the commit-ready expectation — is generic and stays.

## Step 6 — Publish wiring — *[project-provided] values, [built] mechanics*

`pdca publish` (Check's closing step) is deterministic git/`gh` code you don't touch; you supply only the layout it reads:

- The **fork must be a sibling** (or mapped in `[publisher.checkouts]`) with `upstream` + `origin` remotes, clean. The publisher branches from `upstream/<base>`, `git apply`s the bundle patch, **`git add --all`** (so the patch's *new* test is staged, not dropped), commits, pushes to `origin`, and opens a **draft** PR with `--head OWNER:BRANCH` (owner derived from `origin`). It never marks ready/merges — that is the human's sign-off disposition.
- **The patch must be commit-ready for the *target* repo.** The publish commit runs the *target's own* pre-commit hooks (its formatter/linters), which no PDCA gate models — so "all gates green" ≠ committable. The builder is told to run the project's formatter/commit hooks before declaring done; for a stronger guard, add a pre-publish Check that runs the target's hooks in `--check` mode so a formatting miss is an iterate-do, not a mid-publish failure.

## Feeding improvements back — the non-optional part

Running a real cycle *will* surface defects in the machinery, not just your integration. When it does, the fix is a **template** change — apply it in the template, not just the instance, or the next `copier update` clobbers it. **File it as an `enhancement` issue on the template repo**, one per generic machinery change. Put in the issue body what makes the hand-off mechanical: the **upstream target** (the `template/…` path), the **rendering class** (verbatim / jinja / instance-only), and an **apply note**. The issue is the shared queue the template maintainer works from; the change lands via a normal PR that closes it.

The apply rule (state it in the issue, follow it in the PR):

- **Verbatim machinery** (`src/**`, `tests/**`): copy the instance file over its `template/…` counterpart; re-run the template's tests.
- **Genericized (jinja)**: port the change by hand into the `.jinja`, swapping project literals (`Gramps Testbed v2`) back to copier vars (`{{ project_name }}`); render a throwaway project and `make check` it.
- **Instance-only** (`engine/**`, gate rows, scraper, ruleset, branch convention): **do not** feed back. The *generic lesson* often is worth a template note (e.g. "a patch-and-revert gate must clean added files"); the gramps script is not.

Why this matters as an instruction and not a nicety: the harness only stays improvable-without-forking if instance discoveries return to the source. An instance that hoards its fixes diverges until `copier update` is unusable — at which point you have a fork, not an adaptation. A shared **issue tracker on the template repo** is the seam that prevents that: one linkable, status-tracked queue both the maintainer and every instance can see. (An earlier version of this harness used an instance-local propagation-log *file*; issues replaced it because the file duplicated the template's own git history and had to be hand-synced. Feeding a fix upstream is still a deliberate step across repos — the issue makes the work explicit and reviewable, and the PR that closes it lands the change.)

## Gramps Testbed v2 at a glance

The full adaptation, every knob → its instance value, as a fill-in-the-blanks crib:

| Adaptation point | Class | Gramps value |
|---|---|---|
| Tracker | render-time | Mantis @ `https://gramps-project.org/bugs`, ids like `13418` |
| Tracker intake | project-provided | Playwright Mantis scraper → CSV export; planner reads the issue row + notes |
| Default branch / area rules | render-time + instance | `main`; addon work targets `maintenance/gramps60`, cherry-picked → `gramps61` |
| Branch convention | instance-only | `fix/bug-{id}-{slug}` / `enhancement/{id}-{slug}` |
| Builder / reviewer | render-time | `claude` / `codex` by default; **running** config takes the same-vendor `claude` reviewer fallback + sandbox |
| Gating gate | project-provided | `C4-verify` (bundle, red→green, the one gating check) |
| Advisory tiers | project-provided | T1/T2/T4 conformance (doc-16), T3 unit/addon-60/addon-61/interface (baseline-diffed) |
| Ruleset | instance-only | gramps wiki doc 16, cited by section heading, selected by core-vs-addon target |
| Engine | instance-only | `engine/**` — Docker runners, dogtail/AT-SPI interface suite, addon×core worktree matrix |
| Publish layout | project-provided | gramps fork as a sibling with `upstream`+`origin`; target runs `black` as a pre-commit hook |
| Feedback mechanism | process | `enhancement` issues on the template repo (one per machinery change) |

## What never changes

The cycle's shape and the harness's spine are **not** adaptation surface. You do not get to move the C6 accept-guard into a model, skip the deterministic gates, coin a fifth beat, or let a leaf decide control flow. Adaptation fills the *leaves and the gates*; the *driver, the state machine, the 5/5/1 anatomy, and the human touch points* are fixed ([01](01-the-quality-cycle.md), [03](03-cycle-automation.md)). When you find yourself wanting to change those, you are no longer adapting — you are proposing a template change, which is the feedback path above, deliberated in the open, not a quiet edit in one instance.
