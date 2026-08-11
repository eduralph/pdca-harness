# pdca-harness

A [Copier](https://copier.readthedocs.io/) template for spinning up an
**AI-driven PDCA quality-cycle harness** in a new project.

One contribution turns one PDCA cycle: **Plan** (author the spec) → **Do**
(implement) → **Check** (verify the built artifact against the spec —
correctness, conformance, *and* validation) → **Act** (improve the process so
the issues this cycle exposed don't recur) → back to Plan with a better
baseline. The cycle keeps a human at three irreducible touch points
(Plan-authoring, Check sign-off, Act) and automates everything around them.

This repo packages the project-agnostic parts of that cycle — the bundle
state machine (the driver), the artifact templates, the generic model docs,
and the 11-item integration scaffold each repo fills in — so a new project
starts from a prepared harness instead of tribal knowledge.

## Learn by example

New here? **[`docs/`](docs/README.md)** is a step-by-step walkthrough that drives
one real contribution through every beat of the cycle, using a live project
([Gramps Testbed v2](docs/README.md)) as the worked example. Start with the
**[introduction](docs/00-introduction.md)** for the why-and-what, then follow
steps 01–07 from rendering the template to publishing a fix and the cross-cycle
Act review.

## What you get when you render it

```
<your project>/
  pdca.toml                 # driver config: bundle paths, the model-leaf commands
  PCDA/
    quality-cycle.md        # the generic model (Plan/Do/Check/Act, 5/5/1) — reference
    quality-cycle/          # the full vendored spec (00–10), plain Markdown
  docs/
    INTEGRATION.md          # YOUR repo's concretizations — fill the TODOs
  src/pdca_harness/         # the deterministic driver (state machine over bundles)
  templates/                # brief / SUMMARY / tracker-comment / pr-description tpls
  examples/toy/             # a worked brief the driver can advance offline
  results/                  # per-cycle bundles land here
  process/act-log.md        # cross-cycle process deltas (Act)
```

## Prerequisites

The harness itself has **no runtime dependencies** — the driver is pure Python stdlib — but a
real cycle shells out to a few host tools. `make install-check` (or `pdca doctor` once
installed) reports the harness-universal tools and the configured leaf CLIs and prints the
exact fix for anything missing. (Your project **gate toolchain** is only checked if you
declare it — see the last bullet.)

**To render the template**
- **Python ≥ 3.11** — the driver parses config with stdlib `tomllib` (3.11+).
- **[Copier](https://copier.readthedocs.io/)** — the template engine; `pipx install copier`
  (or `pip install copier`).

**To run the offline slice** (`make install`, then `pdca flow TOY`)
- **git** — per-cycle worktree isolation and publish.
- **make** — the front-door target runner (on Windows use `scripts/install.ps1` instead).
- a **pip-capable venv** — `python3-venv` (a clean Debian/Ubuntu lacks `ensurepip`); `make
  install` installs it via `apt` where it can, else falls back to `get-pip.py` (needs **curl**).

**To contribute for real** (wire live leaves + publish PRs)
- **[gh](https://github.com/cli/cli)** — draft-PR publish/merge and the tracker scrape; run
  `gh auth login` after installing.
- the **leaf-backend CLI(s)** you configure — `claude` and/or `codex` (only when
  `leaves_mode = "command"`; the stub slice needs none). `make install` auto-installs a backend
  that has an official installer (currently `claude`); for others (e.g. `codex`) it reports the
  CLI as missing with an install hint — you install it yourself.
- your project's **gate toolchain** (e.g. `cargo`, `pytest`, `npm`) — instance-owned, never
  hardcoded in the harness. `[install].extra_bootstrap` in `pdca.toml` provisions it, but
  `make install-check` doesn't run that command; declare the tools as `[[doctor.checks]]` rows
  (which `pdca doctor` runs) to have them verified too.

Automatic provisioning of the system-package tier (git, gh, `python3-venv`) is **apt +
Debian/Ubuntu + sudo**; on any other host `make install` prints the exact install command and
stops rather than guess. Everything else it bootstraps idempotently — see [Use](#use).

## Use

### 1. Render the harness into a new project

```bash
copier copy gh:<you>/pdca-harness ../my-new-project   # answer the prompts
cd ../my-new-project
```

Render from a `gh:`/`https:` URL or an **absolute** path — not a relative local
path like `../pdca-harness`. Copier stores the source as given, and resolves it
from the *project* directory on the next run, so a relative source breaks
`copier update` later (issue #373). Already rendered that way? Fix it once:
edit `_src_path` in `.copier-answers.yml` to the absolute path or URL.

Re-apply template updates later, from inside the rendered project:

```bash
copier update
```

Then bootstrap the tools a real cycle needs — `make install` (issue #207) provisions the
harness-universal tools (git, gh, a pip-capable venv with a get-pip.py fallback) **and** the
leaf backends your `[leaves.*].family` configures, idempotently, on a host of unknown state:

```bash
make install         # bootstrap every tool + install the console script (re-run = no-op)
make install-check   # report each tool's status, install nothing (non-zero iff a REQUIRED one is missing)
```

The bootstrap hardcodes **no** project gate toolchain — that stays instance-owned via
`[install].extra_bootstrap` in `pdca.toml` (or `scripts/bootstrap-tools.d/*.sh`), so a Rust
instance drops in `rustup` there and a Python one its `pip` extras, surviving `copier update`.

### 2. Drive a contribution cycle

`pdca` is the entry point once the package is installed; otherwise prefix any
command with `PYTHONPATH=src python -m pdca_harness.cli …`.

```bash
pdca init-issue TOY --from-brief examples/toy/brief.md   # seed a bundle from a brief
pdca flow TOY            # the whole cycle: Plan→Do→Check→sign-off→publish→Act
pdca flow A B C          # several ids → batch fan-out + cheap-first sign-off queue
pdca status              # all bundle states (cheap-first); bare `pdca` does the same
pdca queue               # the cheap-first sign-off burn-down
pdca gates TOY           # the deterministic gates (CI runs `pdca gates --working-tree`)
pdca try TOY             # launch the patched build from its worktree for hands-on manual testing
pdca signoff TOY --accept --by you   # standalone accept (publishes on accept; --no-publish to skip), refused while §6 NEEDS-HUMAN is open (C6)
```

The vertical slice runs **offline** out of the box (stub Do/gates/reviewer
leaves), so `pdca flow TOY` drives a full cycle to COMPLETE before you wire
anything real; `pdca flow --rehearse <id>` dry-runs the whole control flow in an
isolated bundle root.

### 3. Make it yours

1. Read `PCDA/quality-cycle.md` (the model) and fill `docs/INTEGRATION.md` —
   tracker, branch targets, fixtures, and the conformance ruleset.
2. Fill real gate tiers in `pdca.toml` `[[gates.checks]]` (the long pole).
3. Wire the real model leaves: set `leaves_mode = "command"` in `pdca.toml`.

## What's built

- **Driver** — the deterministic state machine over result bundles (doc 03),
  end-to-end with **stubbed** Do/gates/reviewer leaves so the loop runs offline.
- **Single-sourced gates** — defined once in `pdca.toml` `[[gates.checks]]`, run by
  both the driver and CI via one `pdca gates` command (stub fallback until filled).
- **Registered dependencies, not cryptic build failures** — a brief's `External
  dependencies` are reconciled against the render's `[[doctor.checks]]` rows at Check.
  If a declared dependency has no row that detects it, that becomes a §6 item and the
  accept-guard holds until the row exists — so `pdca doctor` prompts you with its install
  hint up front, rather than the dependency surfacing mid-cycle as a build failure. A
  topology nothing can detect is exempt (doc 05).
- **Publish on accept** — an accepted bundle opens a draft PR on the target branch as
  the closing step of Check; `pdca flow` and a standalone `pdca signoff --accept` do
  this by default (`--no-publish` to skip), and `pdca status` shows each COMPLETE
  bundle's publish state (doc 07).
- **Batch fan-out + sign-off queue** — `pdca flow A B C` over N issues, `pdca queue`
  cheap-first burn-down. A run's pass budget is `[driver].max_passes` (`PDCA_MAX_PASSES` /
  `pdca flow --max-passes N`); a bundle still iterating when it runs out is named with a
  `pdca flow <id>` resume hint, never silently dropped.
- **Auto-iterate on implementation-only findings** — `[driver].auto_iterate`
  (`PDCA_AUTO_ITERATE` / `pdca flow --auto-iterate`, **off by default**) lets the driver
  record `iterate-do` and rebuild unattended when *every* open §6 item is a `gate` cell of
  the 5/5/1 (C2/C4/T1–T4) — the bugs a builder can fix. A judgment cell (C5 causal
  adequacy, T5, validation), a gate that couldn't run, an external dependency, or anything
  it can't classify still stops for you. It never auto-accepts, and it's bounded by
  `[driver].max_auto_iters` rounds per bundle (doc 06).
- **In-driver lane concurrency** — `[driver].lanes = N` (`PDCA_LANES` /
  `pdca flow --lanes N`) fans the unattended Do + Check band across N workers in
  one workspace; each gate sees its worker slot as `$PDCA_LANE` to keep checkouts /
  runners lane-private (doc 09).
- **Per-cycle worktree isolation** — a cycle's Do/Check run in a git worktree off the
  target base (`[driver].worktree`, on by default), so the host's primary checkout is
  never mutated; the builder + gates see it as `$PDCA_WORKTREE` (doc 04).
- **Mechanical STOP discipline** — `.claude/agents/builder.md` + a PreToolUse hook
  block the builder from marking a PR ready/merging; `reviewer.md` has execute-only
  scope; the decorrelated reviewer path is cross-vendor Codex via `AGENTS.md`.
- **Act tooling (L4)** — `pdca act index` is a read-only index of frozen cycles
  that surfaces §6/§7/§10 and recurring signals; `pdca act log` scaffolds a dated
  act-log entry with the considered bundles + patterns pre-filled (the deltas stay
  the human's). All four L-rungs of the maturity ladder are now present.
- **Full spec** vendored under `template/PCDA/quality-cycle/` (reference docs,
  kept separate from a rendered project's own `docs/`).

## Still ahead

Real gate-tier implementations for your project (the long pole) and wiring the
real model leaves (swap `leaves_mode = "command"` in `pdca.toml`). The
scaffolding for every rung exists; what remains is project-specific gate code and
the model-command wiring.

See `template/PCDA/quality-cycle.md` for the model and the maturity ladder.

## Releases

Versions are git tags of the form `vX.Y.0` — a minor bump per release (patch stays `0`);
each tag is annotated with its release notes (`git show vX.Y.0`). The queue for the next
release is a single open **GitHub milestone**:

- There is always **one open milestone — the next version to ship** (the highest released
  tag's minor `+ 1`). It is the live "in work" list.
- **New issues are filed against that open milestone**, so it shows what's actively being
  worked toward the next release.
- **When that version is released** — cut as a `vX.Y.0` tag — its milestone is **closed and a
  new milestone one minor higher is opened**. New issues then go there. Repeat.

Each change ships as its own pull request with a linked issue (one logical change per PR).

## License

Copyright © 2026 Eduard Ralph.

pdca-harness is licensed under the **Apache License, Version 2.0**
([LICENSE](LICENSE), [NOTICE](NOTICE)) — a permissive license. The driver code and
templates this project copies into a rendered repo are Apache-2.0, so the rendered
output carries no copyleft and is compatible with a permissive license policy (e.g. a
`cargo-deny`-style allowlist); a host repo keeps its own license. A rendered project
declares its own license via the `project_license` copier question (default
`Apache-2.0`).

Contributions are accepted under Apache-2.0 and gated on the
[Developer Certificate of Origin](DCO) — sign off commits with `git commit -s`. See
[CONTRIBUTING.md](CONTRIBUTING.md).
