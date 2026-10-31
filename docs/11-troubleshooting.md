# Troubleshooting — by symptom

← [10 Upgrading an instance](10-upgrading-an-instance.md) · [Index](README.md)

The walkthrough explains each failure where it belongs; this page collects them
by what you see. The quoted text is what the driver prints. Every command's
exit codes are in the [command reference](08-command-reference.md).

## Nothing runs

**`no pdca.toml found … — run inside a rendered project`, exit 2.** Every
command loads `pdca.toml` from the current directory. Change into the rendered
project, not the target checkout.

**`pdca: invalid pdca.toml — …`, exit 2.** The file does not parse or a value is
wrong. The usual causes, in order of frequency:

- conflict markers (`<<<<<<<`) left by a `copier update`
  ([10](10-upgrading-an-instance.md#what-copier-update-does));
- a table declared twice, most often a second `[leaves.sandbox]` header
  ("Cannot declare ('leaves','sandbox') twice");
- a bare `[driver]` key written below `[driver.sizing]` or
  `[driver.size_signal]`, which TOML files under the sub-table. The symptom is
  subtler: no error, the key is simply ignored ([09](09-configuration-reference.md));
- a quoted boolean: `dependency_halt = "true"` or `network_access = "true"` is a
  string and stays off.

**`config: … — merge mode will refuse every PR until …`.** `[driver].expected_checks`
is malformed. It takes a list of check names or one bare name; merge mode fails
closed on anything else.

**`pdca flow: no working systemd-inhibit/caffeinate …`.** The flow could not hold
a suspend inhibitor. Pass `--no-inhibit` or set `PDCA_NO_INHIBIT=1`; the run is
otherwise unaffected.

**`copier update` fails on the first run after rendering.** `_src_path` in
`.copier-answers.yml` is a relative path; Copier resolves it from the project
directory. Hand-edit it to the template URL or an absolute path
([01](01-render-and-integrate.md#1a-render-the-template)).

## A bundle stops where you did not expect

**`PolicyHold` with an unregistered token, before Do.** The brief's
`External dependencies` names a token that matches no `[[doctor.checks]]` row,
or the row's command exits non-zero on this machine. Register the row (its
`cmd` must detect the tool, its `hint` must say how to install it), or annotate
the token `(no-check: <why>)` for a dependency nothing can detect. The guard is
re-evaluated every beat, so no re-plan is needed
([03](03-plan.md#the-two-guards)). `signoff: <bundle> is held at <state>` after
an iterate is the same guard firing on the way back to Do.

**`cannot accept — §6 NEEDS-HUMAN still open (C6)`.** Tick every `- [ ]` in
the §6 the driver assembled, which is the **last** `## 6. NEEDS-HUMAN` heading in
`SUMMARY.md`. A §6 block a reviewer quoted sits in §5 and does not count. A
gating row that failed also lands in §6, so a red gate blocks accept until you
override it consciously or choose `--iterate-*` or `--discontinue`
([05](05-check.md#the-c6-accept-guard)).

**`flow: <bundle> — auto-iterate budget spent`, or a `size backstop` item in §6.**
Auto-iterate hit `[driver].max_auto_iters`, or the patch crossed a
`[driver.size_signal]` threshold. Either way the bundle waits for you; the second
case recommends `iterate-plan`, since rebuilding an oversized slice produces
implementation-shaped findings every round
([07](07-crosscutting.md#auto-iterate-the-driver-deciding-for-itself)).

**`revalidate refuses <bundle>: not COMPLETE`** or
**`split: <bundle> is <state> — refusing to split a frozen bundle`.** Both act
only on the state they name. `--accept` on a bundle two splits deep is refused
too, unless `--force`.

**`pdca act log` exits 1.** No frozen (`COMPLETE`) cycle exists yet, or none
since the last review; `--all` widens the scope.

## A batch run stops, holds, or exits 3

**`unschedulable: …`** (`pdca waves`, exit 1) or **`pdca flow` exit 2 before any
build.** The dependency graph cannot be scheduled: a cycle, a `Depends on` that
is neither in the batch nor `COMPLETE`, or a `Merges with` group under
`wave_mode = "merge"`. The message names the bundle and the edge
([03](03-plan.md#ordering-and-routing-the-batch)).

**`flow: <bundle> is <reason> — refusing to drive it`** or
**`refusing to start a second driver over …`.** A live `pdca flow` already holds
that bundle's claim (`process/.drive-claims/`). One driver per bundle; wait for
that run to end, or pin lanes with `PDCA_LANE` for runs that share a workspace
([07](07-crosscutting.md#lanes)).

**`flow: <bundle> held — prerequisite(s) … not merged`.** A plain `Depends on`
names a bundle this run is not driving, and its PR has not merged into the
dependent's base. Name the prerequisite in the same `pdca flow <ids>` so the run
carries it onto its line, or wait for the merge and re-run
([07](07-crosscutting.md#waves-in-execution)).

**`flow: <bundle> held — it `Conflicts with` …`**, **`held out of this run's
integration line`**, and a run that **exits 3** with every bundle `COMPLETE`.
A fold left a bundle off the line: its publish pushed no branch, or its branch
conflicts with a same-wave bundle that merged before it. The run names the pair
and the files, and the step: declare `Conflicts with: <the other id>` in the held
bundle's brief and rebuild it on a base that holds the other one, once that PR
has merged. Exit 3 is the signal that this happened with nothing else wrong.

**`flow: wave k did not integrate … STOPPING`.** The integration line could not
be advanced: a carried PR branch was rewritten (force-pushed, re-published, or
"Update with rebase"), the base would undo work a bundle put on the line, or the
line on origin holds a commit the run cannot account for. The message says which
and what to do; the common fix is to restore the branch or delete the stale
`pdca-integration/<base>-r<key>` on origin, then re-issue the same command.

**`merge: <bundle> … was NOT merged — … STOP`** (merge mode). The driver read a
red, pending or empty check rollup, the branch update failed, or the PR's base
was not the bundle's target base. The PR is returned to draft; fix CI or the
base and re-run. Raise `[driver].merge_wait_secs` if your checks are slow to
register.

## A gate or review did not say pass or fail

**A row reads `unverifiable`.** The oracle did not answer: the command exceeded
its wall clock (`timeout_secs` or `[gates].default_timeout_secs`), a tool it
needs is missing, or the test target ran zero tests. Read
`gate-logs/<id>.log` in the bundle, then `pdca doctor`. A per-fix verifier that
cannot compile the test on the red leg reports it this way on purpose: a test
that calls a symbol the fix adds does not prove a red
([03](03-plan.md#what-a-real-brief-looks-like)).

**`runner '…' not found on PATH`.** `[gates].runner` names a command the shell
cannot find from the project root. Fix the path or the `PATH` the driver runs
under; `make install` puts the console script on it.

**A row reads `deferred`.** Normal for the contribution check at Check: the
commit message and PR body do not exist yet. Publish runs it for real
([05](05-check.md#the-gates-run-automatically)).

**A row reads `pass` with `flaky = true`, and §6 has an item for it.** The
gating row failed once and passed on the confirm run. Look at both runs in the
log before accepting; turn the re-run off per row with `confirm_fail = false` for
model-backed rows ([05](05-check.md#gates--the-deterministic-oracles)).

**The reviewer says it "could not independently rerun" the test, with
`Operation not permitted` on a loopback bind.** The leaf sandbox refuses
`bind()` on 127.0.0.1 unless `allowLocalBinding` is seeded; the shipped
`.claude/settings.json` sets it, so check it was not removed. If a **gate** fails
the same way, you launched `pdca` from a sandboxed shell: gates inherit your
shell's sandbox, not the leaf's ([05](05-check.md#runtime-tests-that-bind-a-loopback-socket)).

**A Docker-backed gate skips inside the reviewer on a Docker-capable host.** The
leaf sandbox denies the Docker socket. Name the command in
`[leaves.sandbox] unsandboxed_commands` (Claude) or set `network_access = true`
(Codex) ([05](05-check.md#docker-backed-conformance-gates-opt-in)).

**A leaf fails to start after you added a sandbox exemption.** The leaf now
loads project settings only, so authentication kept in your user settings
(`apiKeyHelper`, `env.ANTHROPIC_API_KEY`) is not seen. Move it into the
environment. The error is in the bundle's `*.error.log`.

**`leaves: memory bound … is configured but this host cannot enforce it`.** No
usable `systemd-run --user --scope` (no systemd, a container, macOS). The leaves
run unbounded; the note is the only effect.

**An advisory review came back empty or as a placeholder.** The leaf did not
deliver (a missing CLI, a sandbox that could not start). §6 shows it only while
it lasts: the next Check runs the leaf again.

## The run died or the machine ran out of something

**The driver vanished mid-Check with nothing in any log.** `systemd-oomd` killed
the whole terminal cgroup for memory pressure, usually two reviewer leaves
building cold trees at once. Set `[driver].leaf_memory_max` so a leaf that
overruns dies alone and the run records it
([07](07-crosscutting.md#bounding-what-a-leaf-may-use)).

**A gating gate goes red with `Disk quota exceeded` or `No space left`.** Lane
worktrees have accumulated build state. `pdca sweep` reclaims it;
`[driver].sweep_worktrees` controls the automatic sweep; `[doctor].min_free_gb`
makes the doctor warn before it happens
([07](07-crosscutting.md#housekeeping-sweep--cleanup)).

**A leaked test process is still running after the gate finished.** The driver
sweeps each gate's process group on exit and prints one note naming the
command. If something survives, it was started outside that group; kill it and
report it.

**A bundle's Check artifacts were overwritten by older ones.** Someone ran
`git stash` (or switched branches) in the harness checkout under a live flow.
Bundles are untracked files while in flight; a stash sweeps them up. Do
harness-repo maintenance in a separate worktree
([04](04-do.md#the-harness-checkout-is-not-isolated--dont-do-git-maintenance-under-a-live-flow-issue-130)).

## Publishing

**`publish: checkout not found: <repo> — …`.** The target repo's checkout is not
at the sibling default `../<repo-last-segment>`. Map it in
`[publisher.checkouts]` ([09](09-configuration-reference.md#publisher-and-the-contribution-model)).

**Publish refused the push on a host-CI row or the contribution check.** A
`[gates].host_ci` command failed against the exact base to be pushed, or the PR
body lacks a `**User impact:**` opener, or the commit message or PR body lacks
the tracker id. Fix the drafted texts and publish again; `--no-issue` drops only
the tracker-id rule for a ticketless change.

**`publish: no open PR with head <owner>:<branch> on <repo> — refusing`.**
Publish expected to update an existing PR (an `Onto branch` or a re-publish) and
found none; check the branch name and the fork owner.

**A `COMPLETE` bundle shows `[unpublished]` in `pdca status`.** Publish did not
run or did not complete (dry run, `--no-publish`, a refused push). Run
`pdca publish <id>`. If a later run's fold has replaced the integration line the
bundle was built on, publish refuses and pushes nothing; re-drive the bundle in
a new run ([07](07-crosscutting.md#waves-in-execution)).

**The stacked PR shows its prerequisite's commits.** Expected until the
prerequisite merges; afterwards press "Update branch" on the host and the diff
is the bundle's own change ([05](05-check.md#publishing-the-accepted-change)).

## Interactive leaves

**The planner or sign-off REPL opened with no prompt, and Remote Control
reported an error.** `--remote-control` sits last in the leaf's `argv`; the
driver appends the seed prompt after the argv and the flag swallowed it as a
session name. Move the flag anywhere but last
([01](01-render-and-integrate.md#the-leaves--one-command-per-beat)).

**The leaf prompts you for permission file by file.** Run `make setup` once; it
grants the interactive leaves read of the sibling workspace. Folder trust is
separate and global: the first interactive flow asks once
([01](01-render-and-integrate.md#make-setup--grant-the-interactive-claude-leaves-workspace-read)).

**The session ended and the driver printed a FAIL from the exit check.** The
session did not leave its expected artifact well-formed. The report lists the
items; fix them and re-run the beat. `/handoff <id>` inside the session shows the
same check before you leave. A deliberate walk-away is recorded with
`handoff_guard.py --abandon "<why>"`.

**`pdca try` prints a configure-me hint.** `[manual_test].cmd` is empty or
`[driver].worktree` is off ([09](09-configuration-reference.md#manual_test)).

← [10 Upgrading an instance](10-upgrading-an-instance.md) · [Index](README.md)
