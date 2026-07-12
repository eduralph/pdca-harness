# Step 05 — Check: gates, reviewer, and the assembled SUMMARY

← [04 Do](04-do.md) · [Index](README.md) · next: [06 Sign-off →](06-signoff.md)

**Beat: Check.** This is the substance of the harness. Check verifies the built
artifact against the spec along **three axes — the "5/5/1"** — using three
mechanisms that run in order, then assembles everything into one document for the
human: `SUMMARY.md`.

```
BUILT ─► gates (deterministic) ─► reviewer (advisory) ─► assemble SUMMARY ─► AWAITING_SIGNOFF
         check-gates.{md,json}     check-review.md        (driver stops here)
```

## The 5/5/1

Check asks three different kinds of question:

- **5 correctness** — does it work? `C1` spec · `C2` reproduction (red pre-fix) ·
  `C3` change · `C4` verification (green post-fix) · `C5` causal adequacy.
- **5 conformance** — does it fit the project? `T1` structure · `T2` shape · `T3`
  runtime · `T4` contribution · `T5` judgment.
- **1 validation** — is it the *right* thing? (scope, success criterion, root
  cause vs symptom) — a human call.

Of these, the deterministic ones run as **gates**; `C5`, `T5`, and validation are
inherently judgment and route to the reviewer and to you.

## 1. Gates — the deterministic oracles

The gates you wired in [step 01](01-render-and-integrate.md) run automatically.
Each emits a row: check, result, oracle, and whether it's gating. Here is the
real `results/issue_11589/check-gates.md`:

```markdown
# Check gates — issue_11589

**Overall (gating): pass**

## Correctness (5)
| Check | Result | Oracle | Gating |
|---|---|---|---|
| C4 fix verified: test red pre-fix, green post-fix | pass | run-verify.sh | yes |
| (C1/C2/C3/C5 — none configured / judgment) | none | — | no |

## Conformance (5)
| Check | Result | Oracle | Rule | Gating |
|---|---|---|---|---|
| T1 structure | pass | gate.py T1 | 1 addon(s) conform | no |
| T2 shape | fail | gate.py T2 | __init__.py: no GPL header (doc16:99) | no |
| T3 runtime: core unit suite | fail | run-unit.sh | Trace/breakpoint trap (core dumped) [baseline] | no |
| T3 runtime: addon unit suites | fail | run-addon-unit.sh | pip install logs (3 failures) [baseline] | no |
| T4 contribution | pass | gate.py T4 | N/A: no commit-msg.txt | no |
```

**`Overall (gating): pass` even though three rows say `fail`.** This is the whole
point of the gating/advisory split. The only gating check — `C4-verify`, the
red→green proof — passed, so the contribution is *correct*. The failing rows are
all **advisory**: a GPL-header gap in a file the patch never touched, and two
runtime suites failing with `[baseline]` signatures (a pre-existing core segfault
and an environmental pip issue). They don't block — but they don't vanish either.
They become NEEDS-HUMAN items.

> **`[baseline]` vs `[delta]`.** gramps' T3 gate baseline-diffs: a known
> pre-existing failure is tagged `[baseline]` (ignore — not your fix's fault); a
> *new* failure is `[delta]` (your fix may have caused it). You'll see a `[delta]`
> bite in [step 06](06-signoff.md).

**Promoting a check.** A new gate should earn the right to block. Give a check
`promote_after = N` and run `pdca gates --promotions`: it lists the advisory checks that
have **passed in their N most-recent frozen cycles** — earned promotion from advisory to
gating. It's a hint; you flip `gating = true` yourself (nothing is auto-mutated). That is
the Act "promote a check" delta, with a concrete trigger (issue #156).

### Delegating to a host runner

If your project already single-sources its gates in its own runner (`cargo xtask`,
`make`, `just`, …), don't re-declare them in `pdca.toml` — **delegate**. Set a runner
and give each check a bare `subcmd`:

```toml
[gates]
runner = "cargo xtask"
checks = [
  { id = "C4-verify", tier = "C4", label = "fix verified red->green", subcmd = "verify", gating = true,  scope = "bundle" },
  { id = "T3-suite",  tier = "T3", label = "runtime suite",           subcmd = "test",   gating = false, scope = "repo" },
]
```

PDCA runs `cargo xtask verify` / `cargo xtask test` and maps the results onto the
5/5/1 — the host runner stays the single source of truth; PDCA only orchestrates it.
A full `cmd` (e.g. `cmd = "cargo xtask ci"`) still works for wholesale delegation. A
missing runner surfaces as a clear failing row (`runner '…' not found on PATH`), never
a crash. Set it at render time with the `gates_runner` copier question, or later in
`pdca.toml`.

## 2. Reviewer — the decorrelated second opinion

Next the `reviewer` leaf runs against `{patch.diff, test, brief.md,
check-gates.json}` — **not** `build-notes.md` ([step 04](04-do.md) explained why).
It re-runs the asserted evidence (stash → confirm red, unstash → confirm green),
re-checks that cited `path:line`s exist on the target branch, and flags scope
creep. Its output, `check-review.md`, is **advisory** — it annotates, it never
gates. The blocking path contains no LLM at all.

The reviewer runs in an isolation sandbox (only `{patch.diff, brief.md,
check-gates.json}` are present), so the driver hands it a read-only grounding target as
**`$PDCA_TARGET`** (for a `claude` reviewer also via `--add-dir`). That target is the
**per-cycle worktree** ([step 04](04-do.md)) — pinned to the *same* base the gates ran
against and carrying the patch — so a stale or unreadable sibling checkout can't drift
the reviewer's grounding (issue #120); when worktree isolation is off it falls back to
the brief's target checkout, freshly fetched (refs only — never resetting your working
tree). The reviewer grounds every citation there and is told **not** to wander into
other checkouts on the machine — without this it can't ground, or hunts the filesystem
for "the target" (issue #75).

#### Runtime tests that bind a loopback socket

That isolation sandbox is a *temp working directory*, not an OS jail. The jail a leaf
actually runs under is **Claude Code's own** (bubblewrap + seccomp on Linux), and by
default it refuses `bind()` on a loopback socket — so a runtime test that does
`TcpListener::bind("127.0.0.1:0")` (a loopback-gRPC server, a test HTTP listener) panics
with `Operation not permitted` *before its assertion runs*. Compile and non-socket unit
legs pass; only the socket-backed path fails, so C2/C4/T3 can never earn an automated
red→green (issue #261).

The rendered project's `.claude/settings.json` therefore sets:

```json
{ "sandbox": { "network": { "allowLocalBinding": true } } }
```

and the driver **seeds that one setting into the leaf's temp cwd**, because Claude Code
loads project settings relative to the subprocess cwd — the same walk-up that finds
`.claude/agents`.

What travels is an **allow-list of named `sandbox.network` keys** — never a copy of the
`sandbox` block, and never `permissions` (whose allow-list carries `Edit`/`Write`). In
particular `sandbox.excludedCommands`, recommended just below for your **gates**, makes a
command bypass the sandbox *entirely*: carrying it into the reviewer's cwd would let the
reviewer run your test runner unconfined, so it is never seeded however you configure it.
Widening the seed means adding a key to that list, deliberately.

#### Letting the reviewer settle prior art mechanically (opt-in)

The reviewer's prior-art check (`T4` contribution / `T5` judgment) spans merged history *and*
the **closed/rejected-PR corpus**. Merged history is local (`git log --all -- <paths>`), but
the closed corpus needs `gh pr list --state closed` → `api.github.com`. The sandbox blocks
network by default, so that half can't be settled and the check is correctly forced
NEEDS-HUMAN on **every** bundle — a standing per-bundle tax on a check that could be
mechanical (issue #277).

The shipped `.claude/settings.json` documents the grant but leaves it **off**:

```json
{ "sandbox": { "network": { "allowLocalBinding": true, "allowedDomains": [] } } }
```

Opt in by naming the hosts:

```json
"allowedDomains": ["github.com", "api.github.com"]
```

An **empty list seeds nothing** — that is what "off" means here — so this is an explicit
choice, not a default. Scoped to the hosts you name; `deniedDomains` is carried too.

Domain scoping is **claude** only. A `codex` leaf has no `allowedDomains` equivalent — its
`--sandbox workspace-write` denies the network wholesale — so it takes the all-or-nothing
`network_access` grant described under [Docker gates](#docker-backed-conformance-gates-opt-in)
below, which reaches `api.github.com` too and settles its prior-art check the same way.

#### Docker-backed conformance gates (opt-in)

A conformance gate that brings up a live cluster (`docker compose` → etcd / TiKV / FDB) is
**denied the Docker socket inside the leaf sandbox — even on a Docker-capable host**. The
runtime leg skips, its evidence can never be earned at Check, and it defers to a human-run
confirmer on *every* bundle. That is the harness failing at its own purpose: the maintainer
becomes the bottleneck for a check that ought to be mechanical (issue #276).

Name the commands that need Docker, and **only those** run outside the sandbox:

```toml
[leaves.sandbox]
unsandboxed_commands = ["cargo xtask fdb-conformance", "cargo xtask etcd-conformance"]
```

Every other Bash line the leaf writes stays fully confined. Empty (the default) means no
exemption at all, and a Docker-backed leg goes on deferring to a human.

**"Only those" is enforced, not merely intended** — and a list of names on its own enforces
nothing. It takes three things, because there are three ways the boundary evaporates that the
list doesn't cover:

0. **No sandbox at all** — two ways, both closed by seeding `enabled: true` *and*
   `failIfUnavailable: true`.

   `sandbox.enabled` **defaults to false**, and (2) below deliberately drops your *user-scope*
   settings — which is exactly where an operator's `sandbox.enabled: true` normally lives. So
   without seeding it, bounding the exemption would **remove the very sandbox it claims to
   bound**, and every command would escape.

   And the sandbox does not fail closed on its own: if `enabled` is true but its dependencies
   (`bubblewrap`, `socat`) are missing, Claude Code *disables* it, warns, and runs every command
   unconfined. `failIfUnavailable` — which has effect **only when `enabled` is true** — makes
   the leaf **refuse to start** instead. A bounded exemption on top of no sandbox is not
   bounded; it is nothing. `pdca doctor` checks the same dependencies *before* a run (they are
   **required** rows); this catches the operator who skipped it.
1. **The escape hatch.** The harness seeds `allowUnsandboxedCommands: false` beside the list.
   Claude Code defaults that key to **true**, and while it is true the model may retry *any*
   sandbox-denied command with the `dangerouslyDisableSandbox` parameter and have it run
   unconfined. With it false, that parameter is ignored outright.
2. **Scope concatenation.** The leaf runs with `--setting-sources project`, so it loads *only*
   the settings the harness seeds. Array-valued settings **concatenate** across scopes (user →
   project → local → managed), and the union is **monotonic** — no scope, not even managed
   policy, can remove what a lower one added. So your own `~/.claude/settings.json`
   `excludedCommands` (your *interactive* exemptions — a broad `docker *`) would merge straight
   into the leaf, and nothing the harness writes could subtract them. The list would be a
   *floor*, not a ceiling. The only fix is to not load that scope.

> **The one scope the harness cannot bound: enterprise managed policy.** `--setting-sources`
> excludes *user* and *local* settings, but Claude Code **always** loads managed policy
> (`managed-settings.json`) regardless. Since array settings only ever concatenate, a managed
> policy carrying `sandbox.excludedCommands` widens the leaf's exemption and **nothing the
> harness can do will narrow it** — the list stays a ceiling with respect to *your* settings,
> but not with respect to *your organisation's*. That is by design on Claude Code's side:
> managed policy is meant to outrank everything. If your org sets one, read it before relying
> on the boundary below. The harness cannot, and does not, override it.

> **The cost of (2).** The leaf no longer reads your user-scope settings at all. If your **auth**
> lives there (`apiKeyHelper`, or `env.ANTHROPIC_API_KEY`), move it into the environment or the
> leaf will fail to start. It fails loudly, and the error lands in the bundle's `*.error.log`.

Both ride *with* the exemption: an instance that names no command keeps today's behaviour
exactly, rather than having a policy imposed on it. And this is **claude-family only** — a
family that cannot be confined to the harness's own settings (codex) cannot have a bounded
exemption, so the harness **refuses** the grant there rather than hand out an unbounded one.

**Why a named command, and not a socket grant.** The sandbox schema *does* offer
`allowAllUnixSockets`, which would reach the Docker socket too — but it hands **every**
command the leaf runs access to **every** unix socket. And the Docker socket on a root-owned
daemon is effectively root on the host: anything that can talk to it can start a container
with `-v /:/host`. Check which you have:

```bash
ls -l /var/run/docker.sock      # root:docker  → the daemon runs as root
```

The reviewer leaf has `Bash`. Giving it a root-adjacent socket for *any* command it cares to
write is a far larger grant than letting *one command you wrote* run unconfined. So the
harness does not seed `allowAllUnixSockets` at all — it is not in the allow-list, and setting
it has no effect on a leaf.

**Hardening.** A **rootless** daemon (podman, or rootless `dockerd`) makes socket access
user-level instead of root-level, which de-fangs this entire class of risk. Prefer it where
you can. Match the command precisely, too — this is a capability, so `docker *` hands the leaf
far more than `cargo xtask fdb-conformance` does.

**The exemption list is harness-owned, on purpose.** The driver never inherits your project's
own `.claude/settings.json` `sandbox.excludedCommands` — that is *your* gate workaround, and a
leaf must not silently acquire whatever you exempted for CI. A leaf's exemption is declared
once, deliberately, in `pdca.toml`.

#### The codex leaf reaches Docker a different way

Everything above is the **claude** shape. A `codex` leaf's `--sandbox workspace-write` denies
the Docker socket too, but it cannot take *any* of it: it does not read the settings the
harness seeds, so it can neither be given a per-command exemption nor be confined to one. The
harness therefore **refuses** `unsandboxed_commands` on codex rather than hand out an unbounded
grant, and points you here.

Its denial is **not a filesystem denial** — a relayed socket in a directory codex *can* write
is refused just the same. It is the seccomp/network layer. So no path grant fixes it, and only
one thing does:

```toml
[leaves.sandbox]
network_access = true       # codex: open the leaf's socket/network layer
```

The driver then passes `-c sandbox_workspace_write.network_access=true` to codex leaves. That
reaches the Docker socket **and** `api.github.com`, so it settles the prior-art check
([above](#letting-the-reviewer-settle-prior-art-mechanically-opt-in)) at the same time.

**The two grants have different shapes, and neither is strictly tighter.** Read them together:

| | what escapes | what stays confined |
|---|---|---|
| **claude** — `unsandboxed_commands` | a **named command**, fully (filesystem too) | every *other* command |
| **codex** — `network_access` | the **socket/network layer**, for *every* command (no per-domain scoping) | the **filesystem**, for every command |

So they are deliberately **separate keys**. `unsandboxed_commands` promises *"only these
commands leave the sandbox"* — a promise codex's grant would not keep, since it frees the
network for every command the leaf writes. Naming a command therefore never implies the network
grant; you opt into each explicitly.

Both are moot against a **root-owned** daemon, mind: anything that can talk to that socket can
start a container with `-v /:/host`, so the filesystem confinement codex retains buys little
there. The rootless hardening above is the real answer for either family.

This covers the **reviewer and advisory leaves**. It does *not* cover the **gates**: gate
commands are plain subprocesses of `pdca`, so they inherit whatever sandbox the operator's
own shell already has. If you launch `pdca flow` from inside a sandboxed agent shell, a
gate that binds loopback still fails. Run `pdca` from an unsandboxed shell, or exempt the
test runner via `sandbox.excludedCommands` in your own settings (that exemption stays in
*your* settings — the driver never seeds it into a leaf).

### Optional advisory reviewers (a second lens)

The `reviewer` judges fix *adequacy*. For other lenses — correctness bugs the patch
introduces, reuse/simplification/efficiency cleanups — add **advisory reviewer leaves**
(issue #64): an open `[[leaves.advisory]]` list in `pdca.toml`, each a role-distinct,
model-agnostic (`family` + `argv`) leaf. Each writes `check-advisory-<id>.md`; its
`- NEEDS-HUMAN —` findings fold into §6 like the reviewer's. They are **always advisory**
(never gate). Condition one on a brief field with `when = { field = …, substring = … }`
(e.g. run a deeper review only when the brief says so) — the way gate targets condition
on the bundle. A shipped `code-review` agent realizes the correctness+cleanup lens for a
`claude` instance; `family = "codex"` swaps the vendor. A second shipped agent,
`adversary` (issue #151), is a **refutation** lens — it tries to *disprove* the red→green
evidence and the reviewer's verdict, defaulting to "refuted" when uncertain; the
`pdca.toml` example gates it on `Difficulty: high` so it runs only on the highest
blast-radius bundles, where a confirmatory pass is most likely to be fooled.

**Automatic vendor complement (issue #200).** Cross-vendor decorrelation is the ideal, but
the builder that *actually* runs isn't fixed — an explicit `Do model` (#167), difficulty
routing (#134), or escalation (#135) can pick Codex for one bundle and Claude for the next,
which can leave a statically-configured advisory *same-vendor* as the builder. Opt into
`[leaves.advisory_selection] mode = "vendor-complement"` to let the driver do the pairing:
it treats the `[[leaves.advisory]]` list as a **vendor pool** and runs the single leaf whose
`family` differs from the builder that ran — read from the bundle's `loop-telemetry.json`, so
it holds however the backend was chosen. Declare one leaf per vendor (same `role`, different
`family`) and a Codex-built bundle gets the Claude advisory while a Claude-built bundle gets
the Codex one, with no per-brief edits. If no leaf differs from the builder (or the builder
family is unknown) it falls back to the first applicable leaf — a same-vendor review beats
none — and files the lapse as a §6 `NEEDS-HUMAN` so you can see decorrelation didn't hold.

### Trying the build by hand — `pdca try <id>`

Some §6 rows are irreducibly a **run-it-yourself** call — a GUI/visual repro, or the
validation act ("is this the right thing?") for a change no headless test can exercise. The
gates can't decide those, and the reviewer is headless and sandboxed, so it can't hand you a
live app. `pdca try <id>` closes that gap: it **materializes the patched build on demand
from the bundle's `patch.diff`** — reconstructing the tree off the target base (the same diff
the gates and reviewer used) — and hands you the terminal so you can drive the app and see the
fix for yourself. It runs the project's `[manual_test].cmd` (e.g. `python -m gramps`) from
`$PDCA_WORKTREE` with the `PDCA_*` env exported, and imposes no timeout — you quit the app to
return. It's **advisory**: it decides nothing and mutates nothing in the bundle (edits you
make while testing are reset the next time a tree is staged); you record what you saw in a
Manual-verification note and carry it into the §9 sign-off ([step 06](06-signoff.md)).
Because it rebuilds from `patch.diff` rather than reading whatever Do last left in the shared,
reset-reused worktree, it works for **any built/parked bundle in turn** — exactly the
batch-then-review cadence, where you `pdca try` each bundle as you sign it off. (One
constraint: don't `pdca try` while a lane's Do is mid-build on the same worktree — a non-issue
in batch-then-review, where building is already done.) Needs `[driver].worktree` on and a
configured `[manual_test].cmd`; otherwise it prints a one-line hint and changes nothing.

## 3. Assembly — the SUMMARY the human signs

The driver folds brief + gates + review into `SUMMARY.md`, a 10-section document.
The two sections that drive the human decision are **§6 NEEDS-HUMAN** (what you
must clear) and **§9 Check sign-off** (where you record the verdict — [step
06](06-signoff.md)). When `SUMMARY.md` exists with an empty §9, the bundle is
`AWAITING_SIGNOFF` and the driver **stops**.

Here is the real §6 from issue 11589 — note these are the advisory gate failures
turned into explicit, adjudicable questions (shown already cleared, `- [x]`):

```markdown
## 6. NEEDS-HUMAN — items the human must clear before sign-off
- [x] T2 — Gate failed on `__init__.py: no GPL licence header` — but no
  `__init__.py` appears in `patch.diff`. Both files the patch *does* touch are
  shape-clean. The violation sits in an untouched bundle file → human must decide
  whether the pre-existing gap blocks this contribution.
- [x] T3 — All three runtime suites are `fail` but the failure modes are not
  plausibly caused by a 2-file addon change: core unit `Trace/breakpoint trap
  (core dumped)` (segfault in gramps core, which the diff never touches),
  addon-unit `pip install logs` (env), interface `_ErrorHolder`. Marked
  "whole-suite baseline" — human must diff against baseline to confirm these are
  pre-existing, not regressions.
- [x] T5 — Always-human element. Overall contribution judgment — code quality,
  idiom fit, whether the sibling-detection approach is the right shape.
- [x] V — Validation — Whether the change solves the user's problem against the
  *real* FilterRules pack layout (13 rule pairs in one dir), not just the temp-dir
  stand-in, is a fitness-to-purpose call only the human can make.
```

This is the harness working as designed: the deterministic gate proved
correctness (`C4` green) and *surfaced* everything it couldn't adjudicate — a
header gap of ambiguous scope, environmental test noise, and the two irreducibly
human checks (`T5` judgment, `V` validation) — as a short, specific checklist.
The human isn't re-reading the whole diff; they're answering four pointed
questions.

### An unregistered dependency is a §6 item (issue #263)

§6 is also where an **unregistered dependency** surfaces. When a slice needs something a
human must install or provide — a build tool (`protoc`), a runtime service (Docker, a live
etcd) — the brief's `External dependencies` names it as a **backticked token equal to the
`id` of a `[[doctor.checks]]` row** in your `pdca.toml`:

```toml
[[doctor.checks]]
id   = "protoc"                          # ← the brief writes `protoc`
cmd  = "protoc --version"                # how to detect it
hint = "apt install protobuf-compiler"   # how a human provides it
```

At Check the driver reconciles the two. A declared dependency with **no row that detects
it** becomes a §6 item, and the C6 guard below holds `--accept` until the row exists. That
makes registration a *forcing function* rather than advice: `pdca doctor` prompts you with
the install hint up front, instead of the dependency surfacing mid-cycle as a cryptic
build failure.

The reviewer can't do this — its sandbox has no `pdca.toml`, so it cannot know which rows
exist — and it isn't a judgment call anyway; it's set membership, so the driver decides it
deterministically. Two consequences worth knowing:

- **A row without a `cmd` registers nothing.** `pdca doctor` skips it, so it would never
  detect anything; it does not silence the §6 item.
- **A dependency nothing can detect is exempt.** A required *topology* — a ≥3-replica
  cluster, a partition-capable stack — goes in plain prose (unbackticked), or as a
  backticked token annotated `` `x` (no-check: <why>) ``.

Rows are read from `pdca.toml` as it stands when Check runs, so a row that Plan registered
— or that you pasted in from the builder's proposal at Do — counts within the same cycle.

## The C6 accept-guard

One rule connects §6 to the next step: **you cannot `--accept` while any §6 item
is unchecked.** That's the `C6` guard. It's why §6 items above are `- [x]` — the
human worked them before accepting. (`--iterate-*` and `--discontinue` are *not*
guarded — you can redirect or abandon a bundle with §6 still open.)

A **gating** gate that hard-FAILS (`overall = fail`, not just an advisory row) also lands
in §6 (issue #166), so the guard blocks accept on a red gate too — you clear it with a
conscious override, `--iterate`, or `--discontinue`, never by it slipping silently to
COMPLETE.

State: `AWAITING_SIGNOFF`. Your move — [step 06](06-signoff.md).

← [04 Do](04-do.md) · [Index](README.md) · next: [06 Sign-off →](06-signoff.md)
