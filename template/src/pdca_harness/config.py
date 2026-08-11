"""Project configuration for the PDCA driver.

The driver itself is project-agnostic; everything repo-specific is read from
``pdca.toml`` at the project root (the integration, docs 05). Parsed with the
stdlib ``tomllib`` so the harness has no runtime dependencies.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Disposition hints whose Plan already concluded a CLOSE / no-fix outcome. A bundle
# whose brief carries one of these takes the driver's fast path (skip the builder +
# reviewer leaves, route straight to sign-off). The set is a sensible default;
# instances retune it to their tracker vocabulary via [driver].close_dispositions.
DEFAULT_CLOSE_DISPOSITIONS = [
    "likely-close",
    "wontfix",
    "by-design",
    "duplicate",
    "not-reproducible",
    "manual-verification",
    # No-patch-lands-here triage outcomes (canonical vocabulary, quality-cycle P-rules):
    # UPSTREAM = "not this repo's defect", EXTERNAL = "not a defect in scope at all".
    "upstream",
    "external",
    # NB: "split" is deliberately NOT here. `close_class` SUBSTRING-matches every token, so
    # a generic "split" would send `likely-fix — split parser failure`, `split-brain
    # repro` and `not-split` down the close fast path, skipping builder and reviewer for
    # ordinary fixes. An accepted split writes an explicit close MARKER, which
    # `driver._close_class` honours outright — no hint token is needed for it (#323 review).
]

# A leaf memory bound (issue #420) as systemd's resource-control grammar writes it: a
# byte value with an optional K/M/G/T/P/E suffix, a percentage of physical RAM, the
# literal `infinity`, or `off` (an explicit "no bound here", the per-leaf opt-out).
_MEMORY_MAX_RE = re.compile(r"(?i)\A(off|infinity|\d+(\.\d+)?%|\d+(\.\d+)?[KMGTPE]?)\Z")


def memory_max_value(raw, where: str) -> str:
    """Validate one configured leaf memory bound; nonsense ⇒ ``""`` plus a note (#420).

    Fail-safe like ``sweep_worktrees``' unknown-mode fallback — but in the OTHER
    direction: an unparseable bound degrades to *unbounded* (today's behaviour), never
    to a guessed number, because a wrong cap kills a run exactly as dead as no cap. The
    note is what keeps the degradation from being silent. ``""``/absent ⇒ ``""``.
    """
    val = str(raw or "").strip()
    if not val:
        return ""
    if not _MEMORY_MAX_RE.match(val):
        print(f"config: {where} must be a systemd byte value such as \"8G\", a "
              f"percentage (\"50%\"), or \"off\" — got {val!r}; ignoring it, so leaf "
              "spawns stay unbounded (today's behaviour)", file=sys.stderr)
        return ""
    return val


# ----------------------------------------------------------------------------
#
# LeafConfig
#
# ----------------------------------------------------------------------------
@dataclass
class LeafConfig:
    """How one model leaf (planner, Do builder, Check reviewer, sign-off, Act) runs.

    ``mode == "stub"`` runs the offline placeholder (the vertical slice default);
    ``mode == "command"`` runs ``argv`` as a subprocess in the bundle directory.
    ``interactive`` hands the terminal to the human (a seeded REPL, no ``-p``); a
    headless leaf (``interactive == False``) runs autonomously and writes a doc.

    ``agent`` (optional) names the role-prompt file (``.claude/agents/<agent>.md``);
    how it reaches the model is the family profile's ``role_injection`` — the
    claude family passes ``--agent <name>``, inline families get the file's body
    prepended to the task prompt. ``model`` / ``effort`` (optional) are mapped
    through the profile's ``model_flag`` / ``effort_argv``; flags already present
    in ``argv`` remain the explicit escape hatch and always win.

    ``memory_max`` (optional, issue #420) is this leaf's memory bound — a systemd
    byte value (``"8G"``, ``"50%"``) that overrides ``[driver].leaf_memory_max``
    for this leaf alone, or the literal ``"off"`` to opt this leaf OUT of a
    driver-level bound (the same "explicit setting always wins" escape hatch as
    ``argv``). ``""`` (the default) inherits the driver-level bound, which is
    itself unset by default ⇒ today's unbounded spawn, unchanged.
    """

    mode: str = "stub"
    family: str = ""
    argv: list[str] = field(default_factory=list)
    interactive: bool = False
    agent: str = ""
    model: str = ""
    effort: str = ""
    memory_max: str = ""


# ----------------------------------------------------------------------------
#
# Config
#
# ----------------------------------------------------------------------------
@dataclass
class Config:
    root: Path
    bundle_root: Path
    process_dir: Path
    templates_dir: Path
    default_branch: str
    tracker_system: str
    tracker_url: str
    issue_id_example: str
    builder: LeafConfig
    reviewer: LeafConfig
    # The target repo's standing review rubric ([project] in pdca.toml, #314). A path
    # RELATIVE TO THE TARGET CHECKOUT, optionally narrowed to one Markdown section — the
    # common shape is an "AGENTS.md" heading rather than a whole dedicated file. Unset =>
    # every prompt is byte-identical to today.
    rubric_file: str = ""
    rubric_section: str = ""
    planner: LeafConfig = field(default_factory=LeafConfig)
    #: The cheap-model size judgment (#320). Absent from pdca.toml => stub => never runs,
    #: so an instance taking a `copier update` gains no model call it did not ask for.
    sizer: LeafConfig = field(default_factory=LeafConfig)
    #: The splitter (#322) — interactive, like the other human-in-the-loop leaves. Absent
    #: from pdca.toml => stub => `pdca split` still works offline.
    splitter: LeafConfig = field(default_factory=LeafConfig)
    signoff: LeafConfig = field(default_factory=LeafConfig)
    publisher: LeafConfig = field(default_factory=LeafConfig)
    act: LeafConfig = field(default_factory=LeafConfig)
    author: str = ""  # default §9 sign-off attribution (the maintainer)
    tracker_export_csv: str = ""  # default tracker CSV the planner reads the issue row from
    # Notes-fetch (issue #65): a .format(id=) shell command run before a Plan beat to
    # retrieve a bundle's tracker thread into issue_<id>/notes.json (the planner reads it).
    # $PDCA_BUNDLE = the bundle dir; the command writes notes.json itself. "" ⇒ no fetch.
    notes_cmd: str = ""
    # Composable Plan-seeding sources (issue #102): a list of [[plan.source]] providers the
    # Plan beat runs to seed a bundle's sources/ dir, so a brief can draw on the ticket AND
    # a linked proposal AND a spec — not just one notes_cmd. Each: {type, ...} where type ∈
    # {github, gitlab, csv, file, command}; empty ⇒ only the legacy notes_cmd path.
    plan_sources: list[dict] = field(default_factory=list)
    # Publish mechanics — config-driven so the harness ships project-agnostic.
    # Branch patterns are .format(id=, slug=) strings; issue_trailer is .format(id=).
    fix_branch_pattern: str = "fix/{id}-{slug}"
    feature_branch_pattern: str = "enhancement/{id}-{slug}"
    # The remote the new-PR path branches the fix off of (issue #83). A fork branches off
    # ``upstream`` (the canonical repo); an own-repo target has only ``origin``. The
    # rendered pdca.toml sets it per contribution_model; default ``upstream`` preserves the
    # prior fork behavior for a config lacking the key.
    base_remote: str = "upstream"
    issue_trailer: str = "Fixes #{id}"  # commit/PR trailer; "" → none enforced
    # Optional issue-URL template (``.format(id=)``) so the publisher HYPERLINKS the tracker
    # ticket in the PR body, not just the bare id (e.g. Mantis ".../view.php?id={id}",
    # GitHub ".../issues/{id}"). "" ⇒ no link (the bare trailer, as today).
    issue_url_pattern: str = ""
    repo_checkouts: dict[str, str] = field(default_factory=dict)  # repo_spec → local path
    gates_checks: list[dict] = field(default_factory=list)
    # Host-only CI parity commands (issue #311): ``[gates] host_ci`` — CI jobs the host
    # runs on EVERY PR but the delegated gate runner does not cover (a spell-checker like
    # `typos`, a docs lint). The T4 slot runs before ``patch.diff`` is applied, so it
    # structurally cannot see content that arrives in the patch — a bundle could pass
    # Check green and open a PR that immediately fails a required status. Each declared
    # command runs FROM a reconstructed base + ``patch.diff`` tree: at Check as a gate
    # row (``gates._run_checks``, the lane worktree) and again in ``publish`` immediately
    # before anything is pushed (``publish._host_ci_passes``, an ephemeral tree pinned to
    # the exact base commit the push will build on). A bare string is shorthand for
    # ``{cmd = "..."}``; rows are normalized by :func:`_normalize_host_ci` (always
    # gating). Empty (the default) ⇒ byte-identical behaviour everywhere.
    host_ci_checks: list[dict] = field(default_factory=list)
    # Reverse registry-consistency (issue #205): [gates.registry_consistency] naming an
    # instance's manifest files ({files=[...], pattern="<regex, group 1 = path>"}). The
    # `registry-check` subcommand (wired as a bundle-scoped gate) fails a patch that adds a
    # line to one of these files for a path the same patch doesn't touch. Empty ⇒ no check.
    registry_consistency: dict = field(default_factory=dict)
    # Instance-owned gate-toolchain bootstrap (issue #207): [install].extra_bootstrap in
    # pdca.toml. One idempotent command scripts/bootstrap-tools.sh runs after the harness-
    # universal + leaf tiers, so the generic template ships no project toolchain (a Rust
    # instance drops in rustup here, a Python one its pip extras). "" ⇒ nothing.
    install_extra_bootstrap: str = ""
    # Manual-test launch command ([manual_test].cmd): the shell command `pdca try <id>`
    # runs to launch the PATCHED build from a bundle's per-cycle worktree, so a human can
    # hands-on test it during Check (the GUI/visual/validation §6 rows the gates + headless
    # reviewer can't decide). Run from $PDCA_WORKTREE with the PDCA_* env exported, terminal
    # inherited, no timeout. "" ⇒ `pdca try` errors with a configure-me hint. Instance-owned
    # data (project-specific, like a gate cmd) — e.g. "python -m gramps".
    manual_test_cmd: str = ""
    # Optional advisory reviewer leaves (issue #64): an OPEN list of extra, role-distinct
    # advisory reviewers ([[leaves.advisory]] in pdca.toml), so an instance adds N of them
    # (e.g. a correctness/cleanup code-review lens) with no driver change. Each:
    # {id, role, family, mode, argv, when?}. Always advisory (never gates); their
    # NEEDS-HUMAN findings route into SUMMARY §6. ``when`` ({field, substring}) conditions
    # a leaf on a brief field (e.g. a "Review depth" field), the way gate targets do — empty
    # ⇒ always run.
    advisory_leaves: list[dict] = field(default_factory=list)
    # Advisory-selection policy (issue #200): [leaves.advisory_selection] in pdca.toml.
    # Empty / ``mode`` unset ⇒ every applicable advisory leaf runs (the #64 default). Under
    # ``mode = "vendor-complement"`` the advisory list is treated as a VENDOR POOL and the
    # driver runs the single leaf whose ``family`` differs from the builder that actually ran
    # (read from loop-telemetry.json), so a Codex-built bundle gets a Claude reviewer and
    # vice-versa with no per-brief edits — the cross-vendor decorrelation Check relies on
    # (INTEGRATION §4), made automatic. No different-vendor leaf ⇒ same-vendor fallback + §6.
    advisory_selection: dict = field(default_factory=dict)
    # Plan-beat advisory reviewers (issue #301): an OPEN list mirroring [[leaves.advisory]]
    # but reviewing the BRIEF right after Plan ([[leaves.plan_advisory]] in pdca.toml) —
    # an antagonist of the plan, not the patch. Same spec shape ({id, role, family, mode,
    # argv, agent, when?}); always advisory. A separate list, not a `beat` key on the
    # Check list: the vendor-complement anchor differs (the PLANNER authored the brief;
    # the builder authored the patch) and so does the input contract (brief/notes/sources,
    # no patch/gates). Empty (default) ⇒ the Plan beat is unchanged.
    plan_advisory_leaves: list[dict] = field(default_factory=list)
    # Plan-advisory selection ([leaves.plan_advisory_selection], issue #301): same policy
    # vocabulary as advisory_selection, anchored on the PLANNER family under
    # ``mode = "vendor-complement"`` (pre-Do there is no builder telemetry, and the brief
    # is the planner's artifact — reviewer ≠ author).
    plan_advisory_selection: dict = field(default_factory=dict)
    # Commands a Check leaf may run OUTSIDE its sandbox ([leaves.sandbox]
    # unsandboxed_commands, issue #276). The reviewer/advisory leaves run under Claude Code's
    # sandbox, which denies the docker socket — so a Docker-backed conformance gate (a live
    # etcd/TiKV/FDB cluster via `docker compose`) skips even on a Docker-capable host, and its
    # runtime evidence can never be earned at Check. Naming that command here exempts THAT
    # COMMAND, and nothing else: every other Bash line the leaf writes stays sandboxed.
    #
    # Deliberately a HARNESS-owned list, not the project's own
    # `.claude/settings.json` `sandbox.excludedCommands` — that one is the operator's gate
    # workaround and must never be inherited by a leaf (PR #268). An exemption a leaf gets is
    # declared here, on purpose, in one auditable place. Empty (the default) ⇒ nothing is
    # exempt and the leaf is fully sandboxed.
    leaf_unsandboxed_commands: list[str] = field(default_factory=list)
    # Open a Check leaf's SOCKET/NETWORK layer ([leaves.sandbox] network_access, issue #291) —
    # the codex-shaped counterpart of the list above, and deliberately a SEPARATE key because
    # the two sandboxes grant along different axes and neither is strictly tighter:
    #
    #   claude  a NAMED COMMAND leaves the sandbox entirely (filesystem too); every other
    #           command stays confined.
    #   codex   `--sandbox workspace-write` has no per-command escape. Its denial of the docker
    #           socket is not a filesystem denial at all (a relayed socket in a granted writable
    #           dir is still refused) — it is the seccomp/network layer. Opening it frees the
    #           socket/network layer for EVERY command in the leaf, while the FILESYSTEM stays
    #           confined for every command (verified: a write outside the workspace is denied).
    #
    # So this must not ride on `unsandboxed_commands`, whose promise is "only these commands
    # leave the sandbox" — a promise the codex realization would not keep. Named for what it
    # actually does instead. Also grants codex api.github.com, so the reviewer's prior-art check
    # (#277) can be settled mechanically too. Empty/False (the default) ⇒ today's behaviour.
    leaf_network_access: bool = False
    # Builder escalation ladder (issue #135): an OPEN list of stronger Do backends keyed
    # on the attempt number ([[leaves.builder_escalation]] in pdca.toml). Each:
    # {min_iteration, family, mode, argv}. On iterate, do_build picks the entry with the
    # highest min_iteration <= the current attempt, so a hard bundle can't loop forever on
    # an underpowered executor (e.g. min_iteration=2 → stronger, =3 → frontier). Empty ⇒
    # every attempt uses the default [leaves.builder].
    builder_escalation: list[dict] = field(default_factory=list)
    # Sizer escalation ([[leaves.sizer_escalation]], #320). Triggers on the leaf's OWN
    # first-pass verdict — runtime state, not a brief field — which is why it is an
    # escalation rather than a variant: a `watch` or low-confidence answer is exactly when
    # a stronger model is worth paying for, and no brief field predicts that.
    sizer_escalation: list[dict] = field(default_factory=list)
    # Difficulty-routed builder variants (issue #134): an OPEN list of per-bundle Do
    # backends ([[leaves.builder_variant]] in pdca.toml), each {family, mode, argv, when}
    # where when = {field, substring} matches a brief field (e.g. difficulty=high), like a
    # gate target / advisory leaf. do_build routes the first matching variant; non-matching
    # / absent fields fall back to the default [leaves.builder] (default-open — a missing
    # difficulty tag never reduces capability). The escalation ladder overrides the variant.
    builder_variants: list[dict] = field(default_factory=list)
    # Per-gate wall-clock bound fallback (issue #368): ``[gates] default_timeout_secs``.
    # A ``[[gates.checks]]`` row's own ``timeout_secs`` wins over it; a row that times
    # out is recorded ``unverifiable`` (→ SUMMARY §6 NEEDS-HUMAN), never pass/fail.
    # ``None`` (unset / 0) ⇒ unbounded — today's behaviour, unchanged.
    gates_default_timeout_secs: int | None = None
    # Delegated gates (issue #67): a host runner that single-sources its own gates
    # (e.g. "cargo xtask"). A check's bare ``subcmd`` is run as ``<runner> <subcmd>``, so
    # PDCA orchestrates the host runner instead of re-declaring the gates. "" ⇒ inline only.
    gates_runner: str = ""
    # Target-aware gate selection (docs 04). A check may carry ``target`` (a label or
    # list); it runs iff its labels are a SUBSET of the bundle's label set. The bundle is
    # classified from its brief on two axes: a PRIMARY one (``gate_target_match``: label →
    # substring vs the "Repo + branch target" field; ``gate_target_default`` when none
    # match — mutually exclusive, e.g. core vs addon) plus additive FLAGS
    # (``gate_target_flags``: label → {field, substring} vs any brief field, e.g.
    # ``frontend`` ← a "Surfaces" field). All empty ⇒ no filtering (every gate runs).
    gate_target_default: str = ""
    gate_target_match: dict[str, str] = field(default_factory=dict)
    gate_target_flags: dict[str, dict[str, str]] = field(default_factory=dict)
    # In-driver lane concurrency (docs 09): the worker-pool size for the unattended
    # Do+Check band. ``1`` (the default) keeps the driver strictly serial. ``[driver].lanes``
    # in pdca.toml; ``PDCA_LANES`` overrides for a single run (like ``PDCA_BUNDLE_ROOT``).
    lanes: int = 1
    # Sign-off pass budget for one `pdca flow` run (issue #260): how many build-all →
    # sign-off passes a wave (or iterations a single issue) gets before the driver stops
    # driving it. It also sizes the RUN's pool (#469) — this many passes per wave the run
    # set out to drive, spent down by every wave in turn — so a run whose drive set GROWS
    # (a split whose children it adopts) draws the extra waves from the same pool instead
    # of multiplying what the operator allowed. A run that adopts nothing can never reach
    # that pool, so nothing about it changes. A bundle still iterating when the budget runs
    # out is left un-terminal and NAMED on stderr with a resume hint — never silently
    # dropped. ``[driver].max_passes``; ``PDCA_MAX_PASSES`` overrides for one run;
    # ``--max-passes`` overrides both.
    max_passes: int = 20
    # Auto-iterate (issue #264): when every open SUMMARY §6 item is implementation-level
    # (a `gate` cell of the 5/5/1 — C2/C4/T1..T4), let the driver record `iterate-do` and
    # rebuild instead of stopping for a human. A judgment cell (C5/T5/validation), an
    # unverifiable gate, an external dependency, or an unmarked advisory finding still
    # halts. It never auto-accepts. OFF by default. ``[driver].auto_iterate``;
    # ``PDCA_AUTO_ITERATE`` / ``--auto-iterate`` override.
    auto_iterate: bool = False
    # The per-bundle cap on those automatic rounds; on exhaustion the bundle halts at
    # AWAITING_SIGNOFF for the human. Clamped below ``max_passes``
    # (``max_auto_iters = min(max_auto_iters, max(1, max_passes - 1))``, ``config.py:686``)
    # so a wave driven on its FULL allowance can't run its pass budget out
    # mid-auto-iteration (which #260 would then report as abandoned).
    #
    # That clamp is a statement about the ALLOWANCE, not a promise about every wave (#469).
    # A wave that split adoption ADDED is drawn from what is left of the run's pool
    # (``min(allowance, budget - spent)``, ``flow.py:1429``), which can be smaller than
    # ``max_passes`` — so an adopted child can be cut off mid-auto-iteration after all.
    # That is the run-wide cap binding, and it is the intended trade: the alternative —
    # flooring an adopted wave at ``max_auto_iters`` — spends more passes than the operator
    # allowed, which is the one thing the pool exists to prevent
    # (``test_the_pass_budget_is_one_cap_for_the_whole_run`` measures exactly that). It is
    # never silent: such a wave is named with its allowance and a resume hint
    # (``flow.py:1310``, pinned by
    # ``test_an_adopted_wave_only_gets_what_is_left_of_the_run_budget``). A run that adopts
    # nothing cannot reach the pool at all, so the clamp holds there exactly as before.
    max_auto_iters: int = 3
    # Worktree isolation (issue #94): run a cycle's Do/Check in a dedicated git worktree
    # off the target's base, so the host's primary checkout is never mutated in place.
    # On by default; ``[driver].worktree = false`` disables (then Do/Check edit the
    # checkout directly, as before). Best-effort: a target that isn't a worktree-capable
    # git checkout silently falls back to in-place.
    worktree: bool = True
    # Overflow worktrees (issue #226): the cap on concurrent EPHEMERAL "overflow" worktrees.
    # 0 (default) disables — a gate reading a lane worktree that a DIFFERENT bundle owns heals
    # it in place (``worktree.resync``), as before. >0 reframes ``[driver].lanes`` as a warm
    # CACHED pool and hands such an out-of-cadence / contended read its OWN throwaway tree
    # (built off the base + this bundle's patch, then removed) instead of mutating a lane
    # another bundle may still want — correctness by construction (a fresh tree can't carry a
    # foreign orphan) at the cost of a cold checkout, only on that exceptional path. At the cap
    # it falls back to the in-place heal. ``[driver].overflow`` in pdca.toml.
    overflow: int = 0
    # Per-lane resource preflight (issue #213): [driver].lane_preflight, a shell command run
    # ONCE before a lanes>1 fan-out ({lanes} interpolated); a non-zero exit aborts the run
    # before any lane spawns, so a batch never runs against missing per-lane resources (and
    # the REQUIRED per_lane [[doctor.checks]] are also run). "" ⇒ only the doctor rows (or
    # nothing). Serial (lanes<=1) runs never preflight.
    lane_preflight: str = ""
    # Wave-based batch sequencing (#wave-model). A batch handed to `flow` runs as an
    # ordered sequence of dependency waves; `wave_mode` selects how each wave's accepted
    # work reaches the next: "stack" (default) folds it onto a run-scoped integration
    # branch the next wave builds on — push-only, fork-safe, STOP discipline intact;
    # "merge" (own-repo / CD) instead `gh pr merge`s each wave before the next.
    # [driver].wave_mode in pdca.toml.
    wave_mode: str = "stack"
    # The `gh pr merge` strategy for wave_mode="merge" (issue #wave-model): merge | squash |
    # rebase. Default "merge" (a merge commit — auditable, bisectable). [driver].merge_method.
    merge_method: str = "merge"
    # Which checks must be green before wave_mode="merge" merges a non-final wave's PR
    # (issue #413). "all" (the default) reads the PR's FULL check rollup — `gh pr checks`,
    # after the ready-mark, immediately before the merge — and refuses on any failing,
    # pending or MISSING check, so a later wave cannot build on a base whose CI never went
    # green. "required" restores host-config-only semantics: skip that gate and let `gh pr
    # merge`'s own enforcement (whatever branch protection marks required — possibly
    # nothing) decide, including merging with an empty rollup. [driver].merge_requires.
    merge_requires: str = "all"
    # Optional integration re-gate (#wave-model): after each wave folds onto the
    # integration branch, run the repo-scoped gates over that tip before the next wave
    # builds on it, so a combination that is red though each fix was green alone STOPs the
    # run. Off by default (needs the project's repo-scoped gates). [driver].regate_between_waves.
    regate_between_waves: bool = False
    # Footprint sweep (issue #297): what the flow does with the harness's sibling
    # worktrees once a run's waves complete (the publish/freeze boundary). "clean"
    # (default) strips lane worktrees of build state but keeps the checkout warm, and
    # removes integration/overflow trees outright; "remove" removes lane worktrees too;
    # "off" never sweeps automatically (``pdca sweep`` still works). Left unbounded, the
    # footprint (dominated by per-lane build dirs) has exhausted disk quotas and
    # false-redded gating gates mid-run. ``[driver].sweep_worktrees``.
    sweep_worktrees: str = "clean"
    # Per-leaf memory bound (issue #420): a systemd byte value ("8G", "50%") every leaf
    # subprocess the driver spawns is confined to, so a leaf that overruns dies as ITSELF
    # and the flow survives to record that leaf's failure. Left unbounded, one leaf's
    # build footprint takes down the run: two concurrent reviewer leaves wrote ~69 GB of
    # cold build trees and systemd-oomd killed the whole terminal cgroup — driver, every
    # lane, every bundle — with nothing in any gate log to say why, because oomd kills the
    # CGROUP, not the offending process. "" (the default) ⇒ no wrapping and byte-identical
    # argv, i.e. today's behaviour: there is no portable numeric default, and a wrong cap
    # is itself a way to kill a run. A host with no usable containment facility degrades to
    # the same no-op (leaves.py's probe), never to a hard failure. Per-leaf override:
    # ``[leaves.*].memory_max``. ``[driver].leaf_memory_max``.
    leaf_memory_max: str = ""
    # Free-space preflight threshold in GiB for `pdca doctor`'s workspace row (issue
    # #297): WARN when the filesystem under the project root has less free space, so
    # quota exhaustion is a preflight warning instead of a mid-gate `os error 122`.
    # 0 disables the row. ``[doctor].min_free_gb``.
    doctor_min_free_gb: float = 10.0
    # Act cadence (issue #109): Act is a cross-cycle beat that only yields a real delta
    # once enough cycles have frozen to show a pattern, so ``flow`` auto-runs it only when
    # this many cycles have frozen SINCE the last Act review (counted across flow
    # invocations, not per-run). Below it, the flow skips Act with a hint. ``1`` restores
    # run-after-every-flow; ``--no-act`` always forces skip. ``[driver].act_cadence``.
    act_cadence: int = 5
    # Close-disposition fast path (issue #60): the disposition-hint classes that mark a
    # bundle as close / no-fix, so the driver skips the builder + reviewer leaves and
    # routes it straight to sign-off. ``[driver].close_dispositions`` in pdca.toml; the
    # built-in default covers the common tracker vocabulary.
    # Structural size-estimate weights + cutoffs ([driver.sizing], issue #320). A raw
    # table so an instance can retune against its OWN corpus without patching the engine —
    # the whole point of #324's calibration loop. Empty => sizing.DEFAULT_* apply.
    # Also carries `model_weight` (#359) — how much a sizer-leaf escalation adds to the
    # numeric score (sizing.DEFAULT_MODEL_WEIGHT = 0, i.e. band-only, today's behaviour).
    # Like every key here it is REVIEWED AT ACT CADENCE against the Act index's
    # estimate-vs-outcome sizing column plus a fresh scripts/size-calibrate run — the
    # retuning walk is documented in pdca.toml's [driver.sizing] comment block.
    sizing: dict = field(default_factory=dict)
    # Empirical Check-time backstop thresholds ([driver.size_signal], issue #324). Same
    # shape and same reason as `sizing` above: an instance retunes against its own corpus.
    # Empty => size_signal.DEFAULT_THRESHOLDS apply.
    size_signal: dict = field(default_factory=dict)
    # Pre-dispatch size advisory ([driver].size_guard, #321): "off" (default) | "warn".
    # Default OFF, not "warn": a rendered default that emits output and consults a leaf
    # would change behaviour for every instance taking a `copier update` — the property
    # #342's update test asserts. An instance opts in.
    size_guard: str = "off"
    # Plan-exit reconciliation of brief-declared external dependencies
    # ([driver].dependency_guard, #333): "hold" (default) | "warn" | "off". Since #340
    # the same guard also EXECUTES the detect cmd of exactly the registered rows the
    # brief's tokens name — registration alone was dischargeable on a host where the
    # dependency is absent, because nothing ever ran the row.
    #
    # Defaults to HOLD, unlike size_guard, because the verdict is deterministic rather
    # than a heuristic — set membership plus an exit code, with no false-positive class
    # to trade against — and because it moves an EXISTING block earlier rather than
    # adding one: the same condition already refuses `signoff --accept` through the C6
    # guard.
    dependency_guard: str = "hold"
    # Do-exit halt on a BUILDER-declared unmet external dependency
    # ([driver].dependency_halt, #341): when build-notes.md carries the builder-contract
    # marker `NEEDS-HUMAN external dependency:` AND the claim is confirmed
    # deterministically — the named [[doctor.checks]] row (registered, or parsed from
    # the builder's proposed fenced TOML block) has a detect cmd that exits non-zero —
    # the driver reroutes BUILT through the close fast path to sign-off instead of
    # spending gates + reviewer + adversary adjudicating a patch already stated to be
    # unverifiable. A refuted or unresolvable claim runs full Check unchanged (fail
    # toward review). STRICT boolean, default False: opt-in for one release, and while
    # off the beat is byte-identical to today.
    dependency_halt: bool = False
    close_dispositions: list[str] = field(
        default_factory=lambda: list(DEFAULT_CLOSE_DISPOSITIONS))
    # Family-profile overrides ([families.<name>] in pdca.toml): per-vendor CLI
    # capabilities as data — see pdca_harness.families. Raw tables; resolved lazily
    # via :meth:`profile` so built-ins apply and unknown names fall back to generic.
    families: dict[str, dict] = field(default_factory=dict)
    # Instance-declared doctor rows ([[doctor.checks]]): {id, cmd, hint, required}.
    # `pdca doctor` runs each cmd (exit 0 = OK) after its config-derived checks, the
    # same declare-in-config pattern as [[gates.checks]]. A row a brief's `External
    # dependencies` token names is also executed by the pre-dispatch dependency guard
    # (#340), every beat the policy is consulted — detect cmds must stay cheap and
    # side-effect-free.
    doctor_checks: list[dict] = field(default_factory=list)
    # PR-review triage (#316): the optional single MODEL pass over the findings the
    # keyword heuristics could not classify — a shell command fed the unclassified
    # findings as a JSON list on stdin that must print a JSON list of class names.
    # "" (the default) ⇒ keyword-only: the model pass never runs, and an instance
    # taking a `copier update` gains no model call it did not ask for.
    # ``[triage].model_cmd``.
    triage_model_cmd: str = ""
    # Result-bundle recording ([records], issue #317): `pdca record` commits the bundles
    # whose state is terminal-finished (state.TERMINAL — the cycle is over) to the
    # instance repo as ONE batch commit; mode = "pr" additionally branches, pushes and
    # opens ONE draft PR for the batch. "off" (the default) disables the whole feature —
    # no new behaviour anywhere, including the post-publish call-in and instances that
    # do not version results/ at all.
    records_mode: str = "off"
    # The records branch (pr mode) — a `.format(date=)` pattern, like the publisher's
    # branch patterns.
    records_branch: str = "records/{date}"
    # The batch commit's conventional subject — `.format(n=, ids=, date=)`.
    records_subject: str = "chore(records): record {n} result bundle(s)"
    # The tracker issue a records PR references (one-issue-per-PR instance rules):
    # "" = none, "ask" = prompt when interactive (a headless run falls back to
    # commit-only and reports), or a literal issue number.
    records_issue: str = ""

    def profile(self, leaf: LeafConfig):
        """The resolved :class:`~pdca_harness.families.FamilyProfile` for ``leaf``."""
        from . import families as _families  # local import: keep config import-light
        return _families.resolve(leaf.family, self.families)

    def bundle(self, issue_id: str) -> Path:
        """The per-cycle bundle directory for an issue id."""
        return self.bundle_root / f"issue_{issue_id}"

    def find_bundle(self, issue_id: str) -> Path:
        """Resolve an EXISTING bundle dir for dependency / state **reads** (issue #171).

        The active ``results/issue_<id>`` if it exists, else the archived
        ``results/completed/issue_<id>`` if it exists, else the active path. A prerequisite
        finished and moved to ``completed/`` (a manual archive convention) still satisfies a
        dependent's ``Depends on`` — the dep resolver looks at ``bundle()`` only and would
        otherwise miss it and abort the batch — while a genuinely-missing id resolves to its
        canonical active path and reads as ``UNPLANNED``, preserving the misconfigured-brief
        guard. Use ``bundle()`` to create / locate the *active* bundle; use this only to
        resolve a *dependency* by id.
        """
        active = self.bundle(issue_id)
        if active.exists():
            return active
        archived = self.bundle_root / "completed" / f"issue_{issue_id}"
        return archived if archived.exists() else active

    def close_class(self, disposition: str) -> str:
        """The close class matching ``disposition``, or "" if it is not a close hint.

        Returns the first configured close class whose token appears (case-insensitively)
        in the disposition value — substring-matched like publish's feature detection, so
        a hint such as ``likely-close`` or ``manual-verification → mac only`` still matches.
        """
        low = disposition.lower()
        for cls in self.close_dispositions:
            if cls.lower() in low:
                return cls
        return ""

    def current_doctor_checks(self) -> list[dict]:
        """The ``[[doctor.checks]]`` rows **as they are on disk right now** (issue #263).

        ``Config`` is loaded once per ``pdca`` invocation, but a `pdca flow` can run for
        hours and ``pdca.toml`` is edited *during* it: the Plan beat registers a row for a
        dependency it just enumerated, and the human pastes in the row the builder proposed
        at Do. A snapshot taken before Plan would then report a correctly-registered
        dependency as unregistered at Check — a §6 blocker for work already done.

        Falls back to the snapshot when ``pdca.toml`` is absent or unparseable (a test's
        synthetic ``Config``, a mid-write file), so this never crashes an assemble.
        """
        try:
            data = tomllib.loads((self.root / "pdca.toml").read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return list(self.doctor_checks)
        return list(data.get("doctor", {}).get("checks", []))

    @classmethod
    def load(cls, root: Path | None = None) -> "Config":
        """Load ``pdca.toml`` from ``root`` (or the nearest ancestor that has one)."""
        root = _find_root(root or Path.cwd())
        data = tomllib.loads((root / "pdca.toml").read_text(encoding="utf-8"))

        paths = data.get("paths", {})
        tracker = data.get("tracker", {})
        plan_sources = list(data.get("plan", {}).get("source", []))  # [[plan.source]] (#102)
        publisher_cfg = data.get("publisher", {})
        leaves = data.get("leaves", {})
        gates = data.get("gates", {})
        gates_checks = list(gates.get("checks", []))
        host_ci_checks = _normalize_host_ci(gates.get("host_ci", []))
        gates_runner = gates.get("runner", "")
        # Per-gate timeout fallback (issue #368): [gates] default_timeout_secs. Unset,
        # 0 or a non-numeric value ⇒ None (unbounded — behaviour unchanged).
        try:
            gates_default_timeout_secs = int(gates.get("default_timeout_secs", 0)) or None
        except (TypeError, ValueError):
            gates_default_timeout_secs = None
        if gates_default_timeout_secs is not None and gates_default_timeout_secs < 0:
            gates_default_timeout_secs = None
        registry_consistency = dict(gates.get("registry_consistency", {}))
        install_extra_bootstrap = data.get("install", {}).get("extra_bootstrap", "")
        # `pdca try <id>` launch command (project-specific); "" ⇒ the command errors with a hint.
        manual_test_cmd = data.get("manual_test", {}).get("cmd", "")
        # Additive target flags: label → {field, substring}. A bare string is shorthand
        # for the "Repo + branch target" field (so flags and the primary axis can share it).
        gate_target_flags = {
            label: (rule if isinstance(rule, dict)
                    else {"field": "repo + branch target", "substring": rule})
            for label, rule in gates.get("target_flags", {}).items()
        }
        # PDCA_GATES_MODE=stub empties the configured checks → the all-PASS stub
        # rows, so an offline "rehearse" runs the control flow without Docker.
        # host_ci rows are emptied too (#311): same determinism reason — a rehearse
        # must not run instance CI commands (or reconstruct worktrees) offline.
        if os.environ.get("PDCA_GATES_MODE") == "stub":
            gates_checks = []
            host_ci_checks = []

        # PDCA_LEAVES_MODE forces every leaf's mode regardless of pdca.toml — so
        # CI and the offline self-test (`make`) stay deterministic (=stub, no
        # Claude/TTY) even when the shipped config wires the leaves to "command".
        mode_override = os.environ.get("PDCA_LEAVES_MODE") or None

        def leaf(name: str) -> LeafConfig:
            d = leaves.get(name, {})
            return LeafConfig(
                mode=mode_override or d.get("mode", "stub"),
                family=d.get("family", ""),
                argv=list(d.get("argv", [])),
                interactive=bool(d.get("interactive", False)),
                agent=d.get("agent", ""),
                model=d.get("model", ""),
                effort=d.get("effort", ""),
                # Per-leaf memory bound (#420): overrides [driver].leaf_memory_max for
                # this leaf; "off" opts it out entirely, "" inherits the driver bound.
                memory_max=memory_max_value(
                    d.get("memory_max", ""), f"[leaves.{name}].memory_max"),
            )

        # Advisory reviewer leaves (issue #64) — an open list under [[leaves.advisory]].
        # PDCA_LEAVES_MODE forces their mode too (CI / offline determinism).
        advisory_leaves = [
            {**spec, "mode": mode_override or spec.get("mode", "stub")}
            for spec in leaves.get("advisory", [])
        ]
        # Advisory-selection policy (issue #200) — how the driver picks from that list.
        advisory_selection = dict(leaves.get("advisory_selection", {}))

        # Plan-beat advisory leaves (issue #301) — [[leaves.plan_advisory]], mirroring the
        # Check list; PDCA_LEAVES_MODE forces their mode too.
        plan_advisory_leaves = [
            {**spec, "mode": mode_override or spec.get("mode", "stub")}
            for spec in leaves.get("plan_advisory", [])
        ]
        plan_advisory_selection = dict(leaves.get("plan_advisory_selection", {}))

        # Commands a Check leaf may run outside its sandbox (issue #276) — a harness-owned
        # exemption list, never the project's own settings.json `excludedCommands`.
        leaf_unsandboxed_commands = [
            str(c) for c in (leaves.get("sandbox", {}).get("unsandboxed_commands") or [])
            if str(c).strip()
        ]
        # …and the codex-shaped grant: open the leaf's socket/network layer (issue #291).
        # STRICT, and fail CLOSED. `bool("false")` is True — every non-empty string is — so a
        # quoted `network_access = "false"` (an easy TOML slip) silently handed a codex leaf full
        # network/socket access, which is the exact opposite of what it says (PR #292 review,
        # local pass). This is a security grant: it turns on for the boolean `true` and for
        # nothing else, and a non-boolean is reported rather than guessed at.
        _net = leaves.get("sandbox", {}).get("network_access", False)
        if not isinstance(_net, bool):
            print(f"config: [leaves.sandbox] network_access must be a boolean, got "
                  f"{_net!r} — treating it as FALSE (the grant stays closed). Write "
                  "`network_access = true`, unquoted.", file=sys.stderr)
        leaf_network_access = _net is True

        # Builder escalation ladder (issue #135) — stronger Do backends keyed on attempt
        # number. PDCA_LEAVES_MODE forces their mode too (CI / offline determinism); ""
        # leaves it unset so select_builder falls back to the default builder's mode.
        builder_escalation = [
            {**spec, "mode": mode_override or spec.get("mode", "")}
            for spec in leaves.get("builder_escalation", [])
        ]
        sizer_escalation = [
            dict(spec) for spec in leaves.get("sizer_escalation", [])
            if isinstance(spec, dict)
        ]

        # Difficulty-routed builder variants (issue #134) — per-bundle backends keyed on a
        # brief field via `when`. PDCA_LEAVES_MODE forces their mode too; "" inherits the
        # default builder's mode in select_builder.
        builder_variants = [
            {**spec, "mode": mode_override or spec.get("mode", "")}
            for spec in leaves.get("builder_variant", [])
        ]

        # PDCA_BUNDLE_ROOT redirects bundles to a throwaway location so an offline
        # `rehearse` never collides with the real `results/` a live run would use.
        bundle_root = root / paths.get("bundle_root", "results")
        if os.environ.get("PDCA_BUNDLE_ROOT"):
            env_root = Path(os.environ["PDCA_BUNDLE_ROOT"])
            bundle_root = env_root if env_root.is_absolute() else root / env_root

        # In-driver lane pool size. PDCA_LANES overrides [driver].lanes for one run
        # (e.g. to rehearse parallelism without editing pdca.toml). Floor of 1 = serial.
        driver_cfg = data.get("driver", {})
        lanes = int(driver_cfg.get("lanes", 1))
        if os.environ.get("PDCA_LANES"):
            lanes = int(os.environ["PDCA_LANES"])
        lanes = max(1, lanes)
        # Sign-off pass budget. PDCA_MAX_PASSES overrides [driver].max_passes for one run.
        # Floor of 1 = a single build-all + sign-off pass (issue #260).
        max_passes = int(driver_cfg.get("max_passes", 20))
        if os.environ.get("PDCA_MAX_PASSES"):
            max_passes = int(os.environ["PDCA_MAX_PASSES"])
        max_passes = max(1, max_passes)
        # Auto-iterate on implementation-only Check findings (issue #264). Opt-in.
        auto_iterate = bool(driver_cfg.get("auto_iterate", False))
        if os.environ.get("PDCA_AUTO_ITERATE"):
            auto_iterate = os.environ["PDCA_AUTO_ITERATE"] not in ("0", "false", "")
        # Keep the auto budget strictly below the pass budget, so exhausting it always lands
        # the bundle on a clean AWAITING_SIGNOFF halt rather than leaving it mid-flight at
        # ITERATE_DO when the wave's passes run out (issue #260's abandonment shape).
        max_auto_iters = max(1, int(driver_cfg.get("max_auto_iters", 3)))
        if os.environ.get("PDCA_MAX_AUTO_ITERS"):
            max_auto_iters = max(1, int(os.environ["PDCA_MAX_AUTO_ITERS"]))
        max_auto_iters = min(max_auto_iters, max(1, max_passes - 1))
        worktree = bool(driver_cfg.get("worktree", True))  # issue #94; on by default
        overflow = max(0, int(driver_cfg.get("overflow", 0)))  # issue #226; 0 ⇒ heal in place
        lane_preflight = driver_cfg.get("lane_preflight", "")  # issue #213
        wave_mode = driver_cfg.get("wave_mode", "stack")  # #wave-model: stack | merge
        merge_method = driver_cfg.get("merge_method", "merge")  # merge | squash | rebase
        # Check-rollup policy for merge mode (issue #413). An unknown value falls back to
        # "all" with a note — the fail-safe direction is the STRICTER reading (verify the
        # rollup ourselves), never a typo silently buying host-config-only semantics.
        merge_requires = str(driver_cfg.get("merge_requires", "all")).strip().lower()
        if merge_requires not in ("all", "required"):
            print(f"config: unknown [driver].merge_requires '{merge_requires}' — expected "
                  "all | required; using 'all'", file=sys.stderr)
            merge_requires = "all"
        regate_between_waves = bool(driver_cfg.get("regate_between_waves", False))
        act_cadence = max(1, int(driver_cfg.get("act_cadence", 5)))  # issue #109
        # Footprint sweep mode (issue #297). An unknown value falls back to "clean" with a
        # note — the fail-safe direction is "still sweeps", never a silently-growing quota.
        sweep_worktrees = str(driver_cfg.get("sweep_worktrees", "clean")).strip().lower()
        if sweep_worktrees not in ("clean", "remove", "off"):
            print(f"config: unknown [driver].sweep_worktrees '{sweep_worktrees}' — "
                  "expected clean | remove | off; using 'clean'", file=sys.stderr)
            sweep_worktrees = "clean"
        # Per-leaf memory bound (issue #420). Unset ⇒ "" ⇒ no wrapping at all; an
        # unparseable value degrades to the same unbounded default, with a note.
        leaf_memory_max = memory_max_value(
            driver_cfg.get("leaf_memory_max", ""), "[driver].leaf_memory_max")
        try:  # free-space WARN threshold (issue #297); 0 disables the doctor row
            doctor_min_free_gb = max(0.0, float(data.get("doctor", {}).get("min_free_gb", 10.0)))
        except (TypeError, ValueError):
            doctor_min_free_gb = 10.0

        # Close-disposition classes (issue #60): a configured list retunes the default
        # for an instance's tracker vocabulary; absent ⇒ the built-in default.
        close_dispositions = list(
            driver_cfg.get("close_dispositions", DEFAULT_CLOSE_DISPOSITIONS))
        sizing = dict(driver_cfg.get("sizing", {}))
        size_signal = dict(driver_cfg.get("size_signal", {}))
        size_guard = str(driver_cfg.get("size_guard", "off"))
        dependency_guard = str(driver_cfg.get("dependency_guard", "hold"))
        # Do-exit dependency halt (#341) — STRICT boolean, like [leaves.sandbox]
        # network_access (PR #292): this setting lets a confirmed claim skip the
        # reviewer, so a non-boolean (`dependency_halt = "false"` is a truthy string)
        # must fail CLOSED — feature off, full Check runs — and be reported, not guessed.
        _halt = driver_cfg.get("dependency_halt", False)
        if not isinstance(_halt, bool):
            print(f"config: [driver].dependency_halt must be a boolean, got {_halt!r} — "
                  "treating it as FALSE (full Check runs). Write "
                  "`dependency_halt = true`, unquoted.", file=sys.stderr)
        dependency_halt = _halt is True

        # Result-bundle recording ([records], issue #317). An unknown mode fails CLOSED
        # to "off", loudly — the fail-safe direction here is "invent no git commits",
        # the opposite call from sweep_worktrees' "still sweeps" (#297): recording
        # writes repo history, sweeping reclaims scratch.
        records_cfg = data.get("records", {})
        records_mode = str(records_cfg.get("mode", "off")).strip().lower()
        if records_mode not in ("off", "commit", "pr"):
            print(f"config: unknown [records] mode '{records_mode}' — expected "
                  "off | commit | pr; using 'off' (recording disabled)", file=sys.stderr)
            records_mode = "off"

        return cls(
            root=root,
            bundle_root=bundle_root,
            process_dir=root / paths.get("process_dir", "process"),
            templates_dir=root / paths.get("templates_dir", "templates"),
            default_branch=data.get("project", {}).get("default_branch", "main"),
            rubric_file=data.get("project", {}).get("rubric_file", ""),
            rubric_section=data.get("project", {}).get("rubric_section", ""),
            tracker_system=tracker.get("system", ""),
            tracker_url=tracker.get("url", ""),
            issue_id_example=tracker.get("issue_id_example", ""),
            tracker_export_csv=tracker.get("export_csv", ""),
            notes_cmd=tracker.get("notes_cmd", ""),
            plan_sources=plan_sources,
            fix_branch_pattern=publisher_cfg.get("fix_branch_pattern", "fix/{id}-{slug}"),
            feature_branch_pattern=publisher_cfg.get("feature_branch_pattern", "enhancement/{id}-{slug}"),
            base_remote=publisher_cfg.get("base_remote", "upstream"),
            issue_trailer=tracker.get("issue_trailer", "Fixes #{id}"),
            issue_url_pattern=tracker.get("issue_url_pattern", ""),
            repo_checkouts=dict(publisher_cfg.get("checkouts", {})),
            gate_target_default=gates.get("target_default", ""),
            gate_target_match=dict(gates.get("target_match", {})),
            gate_target_flags=gate_target_flags,
            builder=leaf("builder"),
            reviewer=leaf("reviewer"),
            planner=leaf("planner"),
            signoff=leaf("signoff"),
            publisher=leaf("publisher"),
            act=leaf("act"),
            author=data.get("project", {}).get("author", ""),
            gates_checks=gates_checks,
            host_ci_checks=host_ci_checks,
            registry_consistency=registry_consistency,
            install_extra_bootstrap=install_extra_bootstrap,
            manual_test_cmd=manual_test_cmd,
            advisory_leaves=advisory_leaves,
            leaf_unsandboxed_commands=leaf_unsandboxed_commands,
            leaf_network_access=leaf_network_access,
            advisory_selection=advisory_selection,
            plan_advisory_leaves=plan_advisory_leaves,
            plan_advisory_selection=plan_advisory_selection,
            builder_escalation=builder_escalation,
            sizer=leaf("sizer"),
            splitter=leaf("splitter"),
            sizer_escalation=sizer_escalation,
            builder_variants=builder_variants,
            gates_runner=gates_runner,
            gates_default_timeout_secs=gates_default_timeout_secs,
            lanes=lanes,
            max_passes=max_passes,
            auto_iterate=auto_iterate,
            max_auto_iters=max_auto_iters,
            worktree=worktree,
            overflow=overflow,
            lane_preflight=lane_preflight,
            wave_mode=wave_mode,
            merge_method=merge_method,
            merge_requires=merge_requires,
            regate_between_waves=regate_between_waves,
            act_cadence=act_cadence,
            sweep_worktrees=sweep_worktrees,
            leaf_memory_max=leaf_memory_max,
            doctor_min_free_gb=doctor_min_free_gb,
            close_dispositions=close_dispositions,
            sizing=sizing,
            size_signal=size_signal,
            size_guard=size_guard,
            dependency_guard=dependency_guard,
            dependency_halt=dependency_halt,
            families={k.strip().lower(): dict(v)
                      for k, v in data.get("families", {}).items()},
            doctor_checks=list(data.get("doctor", {}).get("checks", [])),
            triage_model_cmd=str(data.get("triage", {}).get("model_cmd", "") or ""),
            records_mode=records_mode,
            records_branch=str(records_cfg.get("branch", "records/{date}")),
            records_subject=str(records_cfg.get(
                "subject", "chore(records): record {n} result bundle(s)")),
            records_issue=str(records_cfg.get("issue", "") or ""),
        )


def _normalize_host_ci(entries: list) -> list[dict]:
    """Normalize ``[gates] host_ci`` (issue #311) into gate-check-shaped rows.

    A bare string is shorthand for ``{cmd = "<string>"}``. Defaults, chosen so a
    declared row closes the blind spot without further configuration: ``tier = "T4"``
    (the CI-parity slot the issue names — the contribution as the host's CI will see
    it), ``scope = "bundle"`` (they need the patched tree), and an ``id``/``label``
    derived from the command so a failure always names what ran. Explicit id / label /
    tier keys on a table row win over those defaults — but ``gating`` is FORCED true,
    a contract rather than a default: the host's CI will fail the PR on every declared
    command regardless of what an advisory row believed, and the #311 criterion is
    literal — a command that exits non-zero blocks publish. A declared
    ``gating = false`` is therefore ignored, loudly (the sign-off round that reviewed
    this feature did not bless a bypass carve-out).
    """
    out: list[dict] = []
    for i, entry in enumerate(entries):
        row = dict(entry) if isinstance(entry, dict) else {"cmd": str(entry)}
        cmd = str(row.get("cmd", "") or row.get("subcmd", ""))
        if not cmd.strip():
            # The #338 lesson: `subprocess.run("")` exits 0, so a command-less row
            # would pass VACUOUSLY at every seam it is meant to guard. Drop it loudly.
            print(f"config: [gates] host_ci entry {i} has no cmd/subcmd — ignoring it",
                  file=sys.stderr)
            continue
        if not row.pop("gating", True):
            print(f"config: [gates] host_ci entry {i} declares gating = false — "
                  "ignored: a declared host-CI command always blocks (the host's CI "
                  "will fail the PR on it either way, #311)", file=sys.stderr)
        row["gating"] = True
        row.setdefault("id", f"host-ci-{i}")
        row.setdefault("tier", "T4")
        row.setdefault("scope", "bundle")
        row.setdefault("label", f"host CI: {cmd}")
        out.append(row)
    return out


def _find_root(start: Path) -> Path:
    """Walk up from ``start`` to the directory containing ``pdca.toml``."""
    start = start.resolve()
    for d in (start, *start.parents):
        if (d / "pdca.toml").exists():
            return d
    raise FileNotFoundError(
        f"no pdca.toml found at or above {start} — run inside a rendered project"
    )
