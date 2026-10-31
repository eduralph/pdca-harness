# Configuration reference — every `pdca.toml` section and key

← [08 Command reference](08-command-reference.md) · [Index](README.md) · next: [10 Upgrading an instance →](10-upgrading-an-instance.md)

`pdca.toml` is the driver's only configuration file. The rendered copy carries a
long comment above almost every key — the authority for the version you have —
and this page is the map: each section, each key, what it changes, and where the
walkthrough shows it in use. Defaults are those of template `v0.59.0`. A key
shown commented out in the rendered file is optional and off until you uncomment
it.

Two rules of TOML that have bitten instances, worth stating up front:

- **A key belongs to the table header above it.** A bare `[driver]` key written
  after `[driver.sizing]` lands in the sub-table and is silently ignored, so new
  `[driver]` keys go above the sub-tables. The rendered file keeps `size_guard`
  last among the bare keys for that reason.
- **A table may be declared once.** `[leaves.sandbox]` takes both of its keys
  under one header; a second header makes `tomllib` refuse the file and every
  command exits 2 ([11](11-troubleshooting.md)).
- **Booleans are unquoted.** `dependency_halt = "true"` and
  `network_access = "true"` are strings and stay off.

## `[project]`

| Key | Default | What it does |
|---|---|---|
| `name` | render answer | Human-readable project name, used in prompts and headings. |
| `default_branch` | render answer | The branch fixes target unless an INTEGRATION §2 area rule or a brief's `Repo + branch target` says otherwise. |
| `author` | render answer | Default §9 sign-off attribution, so `flow` and `signoff` need no `--by`. |
| `rubric_file`, `rubric_section` | unset | The target repo's standing review rubric, a path relative to the **target** checkout, optionally narrowed to one `## Heading`. Snapshotted per attempt and handed to the builder, reviewer and adversary alike, so the builder sees the criteria it will be judged by. `pdca triage` reads its class list too. Wyrd points it at the target's `AGENTS.md` § "Review rubric & protocol". |

## `[paths]`

| Key | Default | What it does |
|---|---|---|
| `bundle_root` | `results` | Where `issue_<id>/` bundles live. `PDCA_BUNDLE_ROOT` redirects it for one run; `--rehearse` points it at `.rehearse/`. |
| `process_dir` | `process` | Cross-cycle artifacts: `act-log.md`, `act-ledger.json`, `.act-reviewed`, `.drive-claims/`. |
| `templates_dir` | `templates` | The artifact templates ([10](10-upgrading-an-instance.md#the-templates-folder)). |

## `[driver]`

The run's shape. Covered in [07](07-crosscutting.md) unless noted.

| Key | Default | What it does |
|---|---|---|
| `lanes` | `1` | Worker-pool size for the unattended Do + Check band; Plan, sign-off, publish and Act stay serial. `PDCA_LANES` or `--lanes` override for one run. Gates see their slot as `$PDCA_LANE`. |
| `max_passes` | `20` | Build → sign-off passes a wave gets before the driver stops driving a bundle; also sizes the run's pass pool. Never silent: the bundle is named with a resume hint. |
| `auto_iterate` | `false` | Record `iterate-do` and rebuild while §6 carries implementation findings; human-judgment findings are deferred to the handover. Never accepts. `PDCA_AUTO_ITERATE` or `--auto-iterate` override. |
| `max_auto_iters` | `3` | Hard cap on automatic rounds per bundle, clamped below `max_passes`. `PDCA_MAX_AUTO_ITERS` overrides. |
| `worktree` | `true` | Run Do and Check in a per-cycle git worktree off the target's base (`$PDCA_WORKTREE`); the primary checkout is never edited. Best-effort: a non-git target falls back to in place ([04](04-do.md#isolated-in-a-worktree-issue-94)). |
| `overflow` | `0` | Cap on throwaway worktrees for a gate read against a lane another bundle owns. `0` disables; the lane is healed in place instead. |
| `lane_preflight` | unset | One command run before a `lanes > 1` fan-out, `{lanes}` interpolated; non-zero aborts the run. The `per_lane` doctor rows run regardless. |
| `wave_mode` | `"stack"` | How a wave's accepted work reaches the next wave's base: `"stack"` folds PR branches onto a run-scoped integration line, push-only and fork-safe; `"merge"` has the driver merge each non-final wave's PRs (own-repo only, needs merge rights). |
| `merge_method` | `"merge"` | The `gh pr merge` strategy in merge mode: `merge`, `squash` or `rebase`. |
| `merge_requires` | `"all"` | Merge mode: `"all"` reads the PR's full check rollup, brings a behind-base head up to date and merges pinned to it; `"required"` trusts branch protection alone. |
| `merge_wait_secs` | `300` | Merge mode: how long to wait for a fresh PR's checks to register and report before refusing; also covers the branch update. `0` reads once. A green is confirmed by a second read 15 s later, so values 1 to 14 refuse every PR. |
| `expected_checks` | `[]` | Merge mode: check names a complete rollup must contain, so a slow job that has not registered cannot be mistaken for absent. |
| `regate_between_waves` | `false` | Stack mode: run the repo-scoped gates over each folded tip before the next wave builds on it. |
| `act_cadence` | `5` | Frozen cycles since the last Act review before `flow` runs Act on its own. `1` restores "after every flow"; `pdca act log` ignores it ([06](06-act.md#cadence--why-act-doesnt-fire-every-cycle)). |
| `sweep_worktrees` | `"clean"` | What `flow` does with the harness's worktrees after a run: `clean` strips build state, `remove` deletes lane worktrees too, `off` never sweeps automatically. |
| `close_dispositions` | built-in list | `Disposition hint` values that skip the builder and reviewer and route straight to a confirm-the-close sign-off ([04](04-do.md#the-close-disposition-fast-path)). |
| `size_guard` | `"off"` | `"warn"` prints the a-priori size signal before Do. There is no blocking mode; `"hold"` is accepted and treated as `"warn"` ([03](03-plan.md#the-two-guards)). |
| `dependency_guard` | `"hold"` | Hold a bundle before Do when a brief's `External dependencies` token names no doctor row, or its row's command fails. `"warn"` or `"off"` relax it. Not pre-populated; add the key to change it. |
| `dependency_halt` | `false` | Adjudicate a dependency the **builder** declares in `build-notes.md` at `BUILT`: confirmed ⇒ park at sign-off without a reviewer spend; refuted ⇒ full Check. Unquoted `true` to enable. |
| `leaf_memory_max` | unset | A systemd byte value (`"16G"`, `"50%"`) every leaf runs under; a leaf that overruns dies as itself and the run survives. Turns on per-leaf memory telemetry (`*.memory.jsonl`). A host without `systemd-run --user` degrades to a no-op with one note. |

Two sub-tables, both commented out because the shipped values are the calibrated
ones, and both placed after every bare `[driver]` key:

| Sub-table | Keys | What it does |
|---|---|---|
| `[driver.sizing]` | `conflicts_with`, `difficulty_high`, `ext_deps`, `brief_bytes`, `is_plan_pointer`, `brief_bytes_kb`, `watch`, `oversized`, `model_weight` | Weights, size cutoff and band thresholds of the a-priori size estimate; retune from `scripts/size-calibrate` at Act cadence ([07](07-crosscutting.md#the-estimate)). |
| `[driver.size_signal]` | `patch_kb` (100), `patch_files` (20), `rounds` (2) | The empirical size backstop measured at Check; crossing one raises a §6 item recommending `iterate-plan` and stops auto-iterate. `0` switches a rule off ([07](07-crosscutting.md#auto-iterate-the-driver-deciding-for-itself)). |

## `[install]`

| Key | Default | What it does |
|---|---|---|
| `extra_bootstrap` | `""` | One idempotent command `make install` runs last to provision your gate toolchain. Survives `copier update`. For several concerns, drop scripts into `scripts/bootstrap-tools.d/` instead ([01](01-render-and-integrate.md#make-install--provision-every-tool-flow-needs)). |

## `[manual_test]`

| Key | Default | What it does |
|---|---|---|
| `cmd` | `""` | What `pdca try <id>` runs from the patched worktree, terminal inherited, no timeout. Reference your own scripts by absolute path (`$PDCA_BUNDLE/../../scripts/…`), since the cwd is the worktree. Empty ⇒ `try` prints a configure-me hint ([05](05-check.md#trying-the-build-by-hand--pdca-try-id)). |

## `[tracker]`

| Key | Default | What it does |
|---|---|---|
| `system`, `url`, `issue_id_example` | render answers | Which tracker, its base URL, and how an id looks in briefs and commits. |
| `issue_trailer` | `"Fixes #{id}"` | The commit and PR trailer that links the contribution to its issue; `""` enforces none. `publish --no-issue` omits it and records `id_pending`. |
| `issue_url_pattern` | `""` | A `{id}` URL template; set, publish hyperlinks the id in the PR body deterministically. |
| `export_csv` | unset | A tracker export the planner reads the issue's row from when `flow` runs without `--from-csv`. |
| `notes_cmd` | unset | A `{id}` shell template run before Plan to write `issue_<id>/notes.json` (the comment thread); `$PDCA_BUNDLE` is set. Best-effort; skipped when the file exists. Wyrd: `./engine/scripts/scrape-notes.sh {id}` ([03](03-plan.md#how-to-use)). |

## `[[plan.source]]`

Zero or more providers that write context into the bundle's `sources/` before
Plan ([03](03-plan.md#composing-several-sources)). Each is best-effort.

| Key | What it does |
|---|---|
| `type` | `github` (`gh issue view`), `gitlab` (`glab`), `csv`, `file` (a path or glob with `{id}`), or `command` (a `{id}` shell template run with `$PDCA_BUNDLE` and `$PDCA_SOURCES` set). |
| `role = "tracker"` | Makes this source the tracker thread: it writes `notes.json` itself and suppresses `notes_cmd`. The two are mutually exclusive. |
| `repo` | `github`: `OWNER/REPO` when not `gh`'s default. |
| `path`, `key` | `file`: the glob; `csv`: the export and the id column (omit `key` to copy the whole export). |
| `cmd` | `command`: the shell template. |

## `[publisher]` and the contribution model

| Key | Default | What it does |
|---|---|---|
| `fix_branch_pattern` | `"fix/{id}-{slug}"` | Branch name for a fix; `{id}` and `{slug}` from the brief. |
| `feature_branch_pattern` | `"enhancement/{id}-{slug}"` | Branch name for an enhancement (the brief's `Kind` or disposition decides which pattern applies). |
| `base_remote` | per `contribution_model` | The remote the new branch is cut from: `upstream` for a fork, `origin` for an own-repo target. Change it if your remotes are named differently. |
| `[publisher.checkouts]` | `{}` | `"org/repo" = "../dir"` entries for target repos whose checkout is **not** at the sibling default `../<repo-last-segment>`. Most setups need none. |

**Own repo or fork.** The render-time `contribution_model` answer sets
`base_remote` and decides whether the shipped `docs/fork-discipline.md` applies.
Under `own-repo` (Wyrd) the target is the instance's own project: one remote,
`origin`, and publish branches from `origin/<base>`. Under `fork` the target is
someone else's project: `upstream` is the canonical, read-only repo and `origin`
is your fork. Then publish branches from `upstream/<base>` so the diff is exactly
the fix and none of your fork's tooling commits, pushes to `origin`, and opens
the PR with `--head OWNER:BRANCH` against the upstream repo. The fork-discipline
file carries the rest of that contract: validate against the clean upstream
base in a pinned worktree, treat a cherry-pick as a correctness check rather
than a conflict check, search prior art by affected path across merged history
and closed PRs, and keep `wave_mode = "stack"`, since a fork has no merge rights
upstream. The per-project answers (which branches, which remotes, which
runners) go in INTEGRATION §2, §5 and §8, not in that file.

## `[records]`

Commented out by default; enable only if the instance versions its bundle root.

| Key | Default | What it does |
|---|---|---|
| `mode` | `off` | `commit` commits finished bundles to the instance repo as one batch; `pr` pushes a branch and opens one draft PR for the batch ([05](05-check.md#publishing-the-accepted-change)). |
| `branch` | `"records/{date}"` | `pr` mode: the branch name. |
| `subject` | `"chore(records): record {n} result bundle(s)"` | The commit subject; `{n}`, `{ids}`, `{date}` interpolate. |
| `issue` | `"ask"` | `pr` mode: the issue the batch PR references; `"ask"` prompts when interactive. |

## `[leaves.*]`

One table per leaf: `builder`, `reviewer` (headless); `planner`, `signoff`,
`publisher`, `act`, `splitter` (interactive); `sizer` (headless, `stub` by
default). Keys every leaf table takes:

| Key | What it does |
|---|---|
| `mode` | `"stub"` (offline placeholder) or `"command"` (run `argv`). `PDCA_LEAVES_MODE` forces one value everywhere. |
| `family` | `claude`, `codex`, `gemini`, or any name with a `[families.<name>]` profile; unknown names get the generic profile. Drives grounding flags, role injection and the STOP guard. |
| `interactive` | `true` hands the terminal to the human (a seeded REPL); `false` runs headless and writes its artifact. |
| `agent` | The role prompt: the canonical body is `agents/<name>.md`; Claude leaves resolve it through `--agent <name>` and the generated `.claude/agents/<name>.md` wrapper, other families have it inlined into the prompt. |
| `argv` | The whole command. A flag with an optional value (`--remote-control`) must not sit last: the driver appends the seed prompt after it ([01](01-render-and-integrate.md#the-leaves--one-command-per-beat)). |
| `model`, `effort` | Mapped through the family profile to its model flag and effort mapping; recorded per attempt in `loop-telemetry.json`. Explicit flags in `argv` win. |
| `memory_max` | This leaf's own memory bound, overriding `[driver].leaf_memory_max`; `"off"` opts it out. Variants and escalations inherit their base leaf's. |

Array tables that extend a leaf:

| Table | Keys | What it does |
|---|---|---|
| `[[leaves.builder_escalation]]` | `min_iteration` + any leaf key | A stronger backend from that attempt on; the highest `min_iteration` ≤ the attempt wins ([04](04-do.md#choosing-the-do-model-per-bundle-issues-134167)). |
| `[[leaves.builder_variant]]` | `model`, `when = {field, substring}` + any leaf key | A backend a brief selects: by `Do model: <model>` or by a matching field. First match wins; no match keeps the default. |
| `[[leaves.sizer_escalation]]` | `on_band`, `on_confidence` + any leaf key | Re-ask the size question with a stronger model when the first verdict is `watch` or low-confidence. At most one runs. |
| `[[leaves.advisory]]` | `id`, `role`, `when` + any leaf key | Extra Check lenses, each writing `check-advisory-<id>.md`; always advisory ([05](05-check.md#adding-a-second-opinion)). |
| `[leaves.advisory_selection]` | `mode = "vendor-complement"` | Run the one advisory leaf whose family differs from the builder that ran; no such leaf ⇒ first applicable one plus a §6 note. |
| `[[leaves.plan_advisory]]` | `id`, `role`, `when` + any leaf key | Antagonists of the brief, run right after Plan; one bounded planner revision follows ([03](03-plan.md#a-second-opinion-on-the-brief--plan-advisory-leaves-issue-301)). |
| `[leaves.plan_advisory_selection]` | `mode = "vendor-complement"` | As above, anchored on the planner's family. |
| `[leaves.sandbox]` | `unsandboxed_commands` (claude), `network_access` (codex) | What a Check leaf may reach outside its sandbox: named commands for a Claude leaf, the whole socket layer for a Codex leaf. One table, both keys ([05](05-check.md#docker-backed-conformance-gates-opt-in)). |

## `[families.<name>]`

Vendor behaviour is data. Built-in profiles ship for `claude`, `codex`, `gemini`
and `generic`; declare a table to add or override one, no driver change needed.

| Key | Default (generic) | What it does |
|---|---|---|
| `grounding_flag` | `""` | The flag that grants the leaf an extra directory (`--add-dir` for Claude, `--include-directories` for Gemini); empty means grounding by `$PDCA_TARGET` and `$PDCA_WORKTREE` only. |
| `role_injection` | `inline` | `flag` (Claude's `--agent`), `inline` (the `agents/<name>.md` body prepended to the prompt) or `none`. |
| `stream_argv` | `[]` | Flags that turn on a live tool-use stream, if the CLI has one. |
| `cwd_discovery` | `false` | `true` runs the CLI from the harness root so it discovers project settings (Claude-style). |
| `native_guard` | `false` | `true` means the CLI has its own STOP hook; otherwise a non-Claude builder gets the driver's `gh` PATH shim, the same rules as `builder_guard.py`. |

## `[doctor]` and `[[doctor.checks]]`

| Key | Default | What it does |
|---|---|---|
| `[doctor].min_free_gb` | `10.0` | Free-space floor under the project root; below it the workspace row warns. `0` disables. |
| `[[doctor.checks]]` rows | none | `group`, `id`, `cmd`, `hint`, `level` (`MISSING` or `WARN`), `required`, `per_lane`. A brief's `External dependencies` token must equal a row `id`; that row's `cmd` then also runs as the dependency guard, so keep it cheap and read-only ([08](08-command-reference.md#pdca-doctor--report-every-prerequisite-change-nothing)). |

## `[gates]`

| Key | Default | What it does |
|---|---|---|
| `runner` | `""` or the `gates_runner` answer | A host gate runner (`cargo xtask`, `./engine/xtask.sh`); a row's bare `subcmd` runs as `<runner> <subcmd>`. |
| `checks` | the shipped `T4-contribution` row, or empty when delegating | The gate rows (below). Empty ⇒ built-in all-PASS stubs so the offline slice runs. |
| `default_timeout_secs` | unset | Wall-clock bound for every row without its own; expiry records `unverifiable`. |
| `confirm_gating_fail` | `true` | Re-run a failing gating row once at Check; fail→pass records `pass` with `flaky = true` and a §6 item. |
| `host_ci` | `[]` | Commands the host's CI runs on every PR that the gate ladder does not: run at Check and again at publish against the exact base to be pushed; always gating. A bare string is `{ cmd = "…" }`. |
| `target_default` | `""` | Primary target label when no `target_match` entry matches. |
| `[gates.target_match]` | `{}` | `label = "substring"` matched against the brief's `Repo + branch target`; mutually exclusive primary labels such as `core` versus `addon`. |
| `[gates.target_flags]` | `{}` | `label = { field = "…", substring = "…" }` matched against any brief field; additive flags, such as `frontend = { field = "surfaces", substring = "gui" }`. |
| `[gates.registry_consistency]` | `{}` | `files` (manifest paths) and an optional `pattern` (regex, group 1 = the path) for the `registry-check` gate ([08](08-command-reference.md#pdca-registry-check--a-manifest-line-must-point-at-a-touched-path)). |

**A gate row's keys** (an entry in `checks`):

| Key | What it does |
|---|---|
| `id`, `tier`, `label` | The row's name, its 5/5/1 slot (`C4`, `T3`, …), and the text the matrix shows. |
| `cmd` or `subcmd` | A full shell command, or a sub-command of `runner`. |
| `gating` | `true` blocks sign-off on failure; `false` is advisory and lands in §6 on failure. |
| `scope` | `"repo"` (whole tree; what `gates --working-tree` re-runs in CI) or `"bundle"` (needs `$PDCA_BUNDLE`; local only). |
| `target` | A label or list; the row runs only when its labels are a subset of the bundle's. |
| `timeout_secs` | This row's wall clock; `0` opts out of the default. |
| `promote_after` | `N` passing frozen cycles earn the row a listing in `gates --promotions`. |
| `verifies_base` | `true` marks the row that resets to the bundle's base and receives the base ladder (`PDCA_BASE`, `PDCA_VERIFY_BASE`, `PDCA_BRIEF_BASE`). Undeclared, a `C4` row is assumed to be it. Every row that resets to the base needs it, not only one (the mistake behind Wyrd's diff-coverage finding in [06](06-act.md#the-779-findings-one-review-later)). |
| `at_publish` | Whether `publish` re-runs the row before pushing. Defaults from `scope`: a bundle-scoped T4 row does, a repo-scoped one does not. Set `false` on a model-backed review row Check already paid for. |
| `confirm_fail` | Per-row override of `confirm_gating_fail`; `false` for a model-backed row, where a second run is a fresh sample. |

How the gating policy is meant to be used — one gating red→green row, the
rest advisory until promoted — is in [01](01-render-and-integrate.md#the-gates--your-deterministic-check-oracles)
and [05](05-check.md#gates--the-deterministic-oracles).

## Environment overrides

The `PDCA_*` variables that override a key for one run, and the ones the driver
exports to your gate commands, are listed in the
[command reference](08-command-reference.md#environment-variables).

← [08 Command reference](08-command-reference.md) · [Index](README.md) · next: [10 Upgrading an instance →](10-upgrading-an-instance.md)
