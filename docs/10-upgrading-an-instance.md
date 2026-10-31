# Upgrading an instance — what the template owns, what is yours, and `copier update`

← [09 Configuration reference](09-configuration-reference.md) · [Index](README.md) · next: [11 Troubleshooting →](11-troubleshooting.md)

A rendered project is two things in one tree: files the template owns and
re-renders on every update, and files that are yours. Knowing which is which
decides where you make a change, how you carry one you had to make in the wrong
place, and what to check after an upgrade.

## The ownership boundary

**The template owns these.** `copier update` re-renders them from the new
release and merges that against your tree. Edit them only as a last resort, and
mark the edit (below):

| Path | What it is |
|---|---|
| `src/pdca_harness/` | The driver. Never edit; a fix belongs upstream. |
| `agents/*.md`, `.claude/agents/*.md` | The leaves' role prompts (vendor-neutral bodies and their Claude wrappers). |
| `.claude/hooks/builder_guard.py`, `handoff_guard.py` | The STOP-discipline hook and the session exit check. |
| `.claude/settings.json` | The sandbox and permission settings the driver seeds into leaves. |
| `scripts/bootstrap-tools.sh`, `scripts/install.ps1`, `scripts/size-calibrate`, `scripts/checks/` | The bootstrap, the sizing calibrator, the reference checks. |
| `templates/*.tpl` | The artifact templates ([below](#the-templates-folder)). |
| `PCDA/` | The vendored model spec. Reference only. |
| `docs/principles.md`, `docs/fork-discipline.md` | The generic planning principles and the fork contribution rules ([below](#the-documents-a-render-ships)). |
| `README.md`, `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`, `Makefile`, `pyproject.toml`, `examples/toy/`, `engine/README.md` | Project scaffolding, rendered from your answers. |
| `.github/workflows/check-gates.yml` | The merge re-gate, when `ship_ci_workflow` was on. |

**These are yours.** The template renders a skeleton once and then only merges;
your content is what the update preserves:

| Path | What you put there |
|---|---|
| `pdca.toml` | Every key you set: gate rows, leaf commands, doctor rows, checkouts. The most-edited file, so the most-tested merge path (below). |
| `docs/INTEGRATION.md` | Your repo's concretizations, the 11 items. |
| `engine/` | Your gate runners and verifiers. The template ships a skeleton `run-verify.sh` and a README; everything you add is yours. |
| `scripts/bootstrap-tools.d/*.sh`, `[install].extra_bootstrap` | Your gate toolchain's provisioning. |
| `results/`, `process/` | Bundles, the Act log and ledger. Never touched by an update. |
| `.claude/settings.local.json`, `.venv/`, `.copier-answers.yml` | Machine-local permissions, the install, and the record Copier keeps of your answers and the template commit. `.copier-answers.yml` is committed and never hand-edited, with one exception: a relative `_src_path` ([01](01-render-and-integrate.md#1a-render-the-template)). |

The rendered `CLAUDE.md` tells agents working inside the instance the same
boundary from their side: `agents/*.md` is template machinery, so a prompt change
an Act review wants is filed upstream, not edited locally.

## What `copier update` does

1. It reads `.copier-answers.yml`: `_src_path` (where the template is) and
   `_commit` (the release you are on, `v0.59.0` for Wyrd).
2. It renders the template twice, at your current release and at the new one,
   and applies the difference to your tree as a **three-way merge**, so a key you
   set in `pdca.toml` and a row you appended survive a release that changed the
   lines around them.
3. Where the template and your edit changed the same lines, the merge leaves
   **conflict markers inline** in the file. In `pdca.toml` that means every
   `pdca` command exits 2 at config load until you resolve them
   ([11](11-troubleshooting.md)). The template keeps new `[driver]` keys away
   from the lines instances commonly edit to make this rare.
4. It writes the new `_commit` into `.copier-answers.yml`.

Copier re-asks the render questions with your previous answers pre-filled;
accept them unless the release added a question. A release never switches a
leaf from `stub` to `command` or turns the sizer on: an update must never start
spending model calls an instance did not ask for.

## The procedure

```bash
# 1. never under a live flow — a running pdca imports the code you are about to replace
git worktree add ../my-instance.upgrade main && cd ../my-instance.upgrade

# 2. read what changed: each release tag carries its notes
git -C ../pdca-harness fetch --tags && git -C ../pdca-harness show v0.60.0 --no-patch

# 3. update, then look for markers
copier update
grep -rn '^<<<<<<<\|^>>>>>>>' pdca.toml docs/INTEGRATION.md engine/ || echo "no conflicts"

# 4. prove the result before any real cycle
make install          # re-provisions if pdca.toml or the bootstrap changed
pdca doctor
make check
pdca flow TOY --rehearse

# 5. commit, review the diff as a PR like any other change, merge, then pull into the live checkout
```

Step 1 is the rule from [04](04-do.md#the-harness-checkout-is-not-isolated--dont-do-git-maintenance-under-a-live-flow-issue-130):
a branch switch or `copier update` under a live `pdca flow` rewrites
`src/pdca_harness/*.py` beneath a running interpreter, and any subprocess it
spawns afterward runs mixed-version code.

Releases are tags of the form `vX.Y.0`, one minor per release, each annotated
with its notes; the next version's work is the one open milestone on the
template repo. `copier update --vcs-ref vX.Y.0` moves to a specific release
rather than the newest tag.

## Carrying a local change to a template-owned file

Sometimes an instance cannot wait: a gate script the template ships has a hole,
a prompt needs a line, a driver fix is open upstream but not released. The
practice Wyrd settled on, written into its INTEGRATION §2:

- **File it upstream first**, as an issue on the template repo, and fix it there
  if you can. An edit that only exists in your instance is a merge conflict
  waiting for the next update and a fix the other instances never get.
- **Mark every local divergence** where it lives with a comment that names the
  upstream issue in full: `INSTANCE DELTA — eduralph/pdca-harness#434`, on the
  marker line or within the two lines after it. The marker is what makes the
  divergence findable at the next update, and what lets the three-way merge show
  you a conflict you expected rather than one you forgot.
- **Check at every update whether a delta can go.** Wyrd runs a doctor row
  (`delta retirement`, level `WARN`) whose command scans the engine, scripts, role
  prompts and `pdca.toml` for the markers, asks `gh` for each named issue's
  state, and names the sites whose issue is now closed. The fix having landed
  upstream does not mean the local code is safe to drop unread; it means a human
  reads both and removes the delta deliberately.
- **Audit every off-default key at an upgrade**, not only the delta you came to
  change. Wyrd's record shows why: a `verifies_base = false` set on the wrong row
  in one upgrade cost a wave's diff-coverage figure for weeks
  ([06](06-act.md#the-779-findings-one-review-later)).

Wyrd's INTEGRATION §2 is also a record of the other direction: a list of local
changes retired at the `v0.59.0` upgrade because upstream shipped the same
(integration branches per batch, stacked PRs that land on the target, the
confirm-once gate run), each named with the issue that retired it. That list is
the instance's upgrade log, and a good shape to copy.

## The documents a render ships

Beside `pdca.toml` and `INTEGRATION.md`, the render puts six documents in your
tree. Which agent reads which:

| File | Who reads it | What it says |
|---|---|---|
| `docs/INTEGRATION.md` | Every leaf, and you | Your repo's answers to the 11 integration questions ([01](01-render-and-integrate.md#1b-concretize-fill-docsintegrationmd)). Yours. |
| `docs/principles.md` | The planner, at Plan | Solution-design discipline for briefs: minimalism is scoped to behavioural fixes; a cost used to reject an alternative must be checkable; a brief states the **invariant**, not a mechanism, quantified over the defect category (the self-test: could a one-module guard satisfy it?); invariants come from citable sources. Sections 5 and 6, the invariant catalogue and the category mapping, are project-provided, and section 8 says what evidence promotes a principle to a gate. Template-owned; add your catalogue in the marked sections. |
| `docs/fork-discipline.md` | Planner, builder, reviewer, publisher under `contribution_model = fork` | The generic fork contract: remote layout, branch from `upstream/<base>`, draft-only and STOP, targeting across maintenance lines, validation against the clean upstream, prior art by affected path. Under `own-repo` the file says so at the top and the agents do not cite it ([09](09-configuration-reference.md#publisher-and-the-contribution-model)). Template-owned; your answers go in INTEGRATION. |
| `CLAUDE.md` | Claude leaves, as project guidance | The four beats and six leaves, the builder's scope (read `brief.md` only; patch, test, notes; never ready or merge), sign-off as the human's, Act as cross-cycle. |
| `AGENTS.md` | Codex leaves, as shared project context | STOP discipline for every leaf, the boundary (write only your role's artifacts into the bundle; ground citations on `$PDCA_TARGET`), and that `agents/<name>.md` is the canonical role. Rendered only when a Codex family is configured. |
| `CONTRIBUTING.md` | Human contributors to the instance | The license, the DCO sign-off, and that the gates must be green before contributing. |

## The templates folder

`templates/` holds the artifact templates the leaves and the driver fill:

| Template | Filled by | Becomes |
|---|---|---|
| `brief.md.tpl` | the planner | `brief.md`; the `- **Label:** value` lines are what the driver parses ([03](03-plan.md#why-each-field-is-there)). |
| `plan-pointer.md.tpl` | the planner | A thin brief that points at a host planning artifact (an ADR, a proposal). |
| `design-proposal.md.tpl` | the planner | The brief form for a change that needs design buy-in (Wyrd's `Kind` and `Goal` labels come from it). |
| `split-proposal.md.tpl` | the splitter | `split-proposal.md`, one full draft brief per child between HTML-comment delimiters. |
| `SUMMARY.md.tpl` | the driver | The ten-section `SUMMARY.md` the human signs. |
| `MANUAL-VERIFICATION.md.tpl` | the driver, for a `manual-verification` close | The stub the human fills after `pdca try`. |
| `commit-msg.txt.tpl`, `pr-description.md.tpl` | the publisher | The commit message and PR body the T4 contribution check lints. |
| `tracker-comment.md.tpl` | the publisher | The comment posted to the tracker issue. |

They are template-owned, so an edit is a delta to mark. The safe way to add a
field is to **append** it: the driver parses only the labels it knows, the
planner and reviewers read everything, and an appended line merges cleanly at
the next update. Wyrd's brief template adds `Verification posture`,
`Production reach` and `Track` this way. Removing or renaming a shipped label is
different: the driver, a gate or a check may read it, so treat that as a change
to propose upstream.

← [09 Configuration reference](09-configuration-reference.md) · [Index](README.md) · next: [11 Troubleshooting →](11-troubleshooting.md)
