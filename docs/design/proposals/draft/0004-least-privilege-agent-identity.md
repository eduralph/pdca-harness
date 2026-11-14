---
created: 14.11.2026 10:38
type: proposal
status: draft
author: Eduard Ralph
tracking-issue: https://github.com/eduralph/pdca-harness/issues/960
tags:
  - proposal
  - security
  - credentials
---
# Proposal: Least-privilege identity for the agents

> Enhancement proposals (KEP/RFC-style) are how larger changes and milestone plans are
> proposed. Move this file from `draft/` to `accepted/` when accepted; an accepted proposal
> is never edited — a later proposal supersedes it, recorded in `../proposals/README.md`.
> If a decision is foundational rather than a change, it may instead warrant an ADR.

## Motivation

The loop's GitHub writes (publish, records, reverts) run in the driver's own process with
whatever login the driver's OS user has. Run from the maintainer's account, that is the
maintainer: every repository, every setting, `.github/workflows/**` included. A leaf that is
misled by an issue, a comment or a dependency, or a driver bug, therefore acts with
the maintainer's full reach.

One instance has already narrowed this. client-rust-pdca's ADR-0006 runs unattended loops as
a separate OS user (`crpdca`) holding one fine-grained token that is limited to three
repositories (contents, pull requests, issues). Its sandboxes keep every model leaf and gate
away from any git credential (ADR-0007), and `doctor` proves the split (`token-scope`,
`loop-user`). Nothing in the template carries this, and three gaps remain even there:

- **A personal token is long-lived and tied to a person.** It sits on disk for a quarter, and
  commits and pull requests carry a human identity, so bypass lists and review rules cannot
  tell the agent from the maintainer.
- **Write access to code is write access to CI.** Without the Workflows permission a token
  cannot change `.github/workflows/**`, but it can still change the tests and build scripts
  those workflows run. A `pull_request` workflow on a branch in the same repository runs with
  that repository's secrets, so a pushed branch can exfiltrate them without touching a
  workflow file.
- **The files that govern the agent are not protected from it.** `AGENTS.md`, `pdca.toml`,
  `scripts/`, the secret guard and the workflows are changed by the same pull requests as
  everything else, reviewed the same way.

ADR-0021's driver container adds a fourth credential of the same kind: a deploy key with
write access, which can change workflow files.

## Design

### 1. Identity: one GitHub App per owner

Each owner the loops write to (getwyrd, Ralphovi, eduralph) gets its **own** GitHub App
(working names `pdca-bot-getwyrd`, …), installed only on that owner and limited to the
repositories of that owner's instances and targets.

- **The private key is the long-lived credential.** It signs the JWT that mints installation
  tokens for every installation of its App, so its reach is exactly that App's installation:
  one owner's listed repositories, with the permissions of section 2. One App per owner keeps
  a stolen key from reaching the other owners. The key is readable only by the loop's OS user
  (section 5), rotated on a schedule (GitHub allows two active keys, so rotation has no gap),
  and revoked in the App settings if exposed.
- **Tokens are short-lived.** The driver mints installation tokens just in time, scoped down
  to the repositories the run touches, and keeps them only in memory. A token lives one hour
  while a bundle runs for hours (the median was 6.1 hours in proposal 0001), so the driver's
  GitHub client mints a fresh one before each authenticated call once the current one is
  older than 50 minutes; a call that still meets an expired token re-mints once and retries.
  Model leaves and gates see neither key nor token (ADR-0007's sandbox rules stand).
- **The bot is the author.** A token only authenticates the push and the API calls; the
  commit object keeps whatever identity git is configured with. The driver therefore commits
  with the App's bot identity (`user.name` `pdca-bot-<owner>[bot]`, `user.email`
  `<bot-user-id>+pdca-bot-<owner>[bot]@users.noreply.github.com`, passed with `git -c`) and
  signs off with the same identity, so commits, pull requests and DCO trailers all name the
  agent. Rulesets can then name it, and review rules can require a human.
- The Apps replace the personal token of ADR-0006 and the deploy key of ADR-0021 §4.

### 2. Permissions

| Permission | App | Why |
|---|---|---|
| Contents | read & write | push driver branches (this also covers tags and releases; section 3 fences those) |
| Pull requests | read & write | open and update pull requests |
| Issues | read & write | backlog comments, labels, cleanup |
| Checks, Commit statuses | read | read a PR's check rollup (`gh pr checks`), which `merge` mode's merge gate needs |
| Metadata | read | mandatory |
| Workflows | **none** | GitHub refuses a push that creates or changes `.github/workflows/**` |
| Administration, Secrets, Environments, Actions, Pages, Members | **none** | no settings, protection, secrets or CI control |

A second, **read-only auditor App** per owner (`pdca-auditor-<owner>`, private like the agent
App, so no one else can install it)
checks the settings the agent App must not be able to read (section 6). It has no write
permission of any kind, so its key can at most reveal how the repositories are configured and
the *names* of their secrets, never the values:

| Permission | Auditor | Reads |
|---|---|---|
| Administration | read | whether immutable releases are on (`GET /repos/{repo}/immutable-releases`) and which self-hosted runners exist (`GET /repos/{repo}/actions/runners`) |
| Secrets | read | names of repository secrets, and of organization secrets visible to the repository (`GET /repos/{repo}/actions/organization-secrets`) |
| Environments | read | names of environment secrets |
| Actions | read | environments and their protection rules |
| Contents | read | workflow files for the audit of section 4, tags and releases |
| Metadata | read | mandatory; also rulesets |

### 3. Rulesets and code owners (template `repo-settings.py`)

Everything here works on GitHub's free plan; PDCA cannot assume a paid one.

- **Public repositories only.** Branch protection and rulesets are enforced on public
  repositories on every plan, but not on private repositories of a free personal account. The
  App is therefore installed only where they are enforced; `doctor` fails an
  installation that includes a repository where they are not. A private repository's loop
  keeps publishing from the maintainer's own login in `stack` mode, as today.
- **`main` is reached only through a pull request.** No bypass for the App, required status
  checks from `.github/required-checks.txt`, and **code-owner review** as the review rule, with
  no general approval count. `CODEOWNERS` names the maintainer; only users and teams can be
  code owners, so the App can never satisfy that review, whether or not GitHub counts an
  App's approval as a review. The rule also sets **dismiss stale approvals on push**, so a
  push the App makes after the maintainer's approval voids that approval instead of riding on
  it. It does not set "require approval of the most recent push": that demands a human
  approval of every App push and would block `merge` mode's ordinary PRs, which carry no
  required review at all.
- **What `CODEOWNERS` covers follows the instance's `[driver].wave_mode`.** PDCA's opt-in
  `merge` mode merges each non-final wave's PR itself so the next wave can build on it (the
  final wave's PR stays a draft for the maintainer); `stack` mode, the default, merges nothing.

  | `wave_mode` | `CODEOWNERS` covers | The App can merge |
  |---|---|---|
  | `stack` (default) | everything (`*`) | nothing: every PR needs the maintainer |
  | `merge` | the files that govern or run the agent (list below) | any PR that touches no governing file, once its required checks pass; a PR touching a governing file still waits for the maintainer |

  GitHub does not know a PR's wave position, so "the final wave stays a draft" is the
  driver's convention, not a rule GitHub enforces: in `merge` mode a driver bug or a stolen
  App key can mark the final PR ready and merge it like any other. The enforced boundary in
  `merge` mode is the governing set, not the final wave. This is part of the trade below.

  The governing set in `merge` mode is everything the next run executes or obeys, not only
  CI: `CODEOWNERS`, `.github/**`, `.secret-guard/**`, `.pre-commit-config.yaml`, `pdca.toml`,
  `AGENTS.md`, `CLAUDE.md`, the harness code and its packaging (`src/pdca_harness/**`,
  `pyproject.toml`, `Makefile`, `scripts/**`, `copier.yml` and the copier answers file), the prompts and
  hooks the leaves and gates run with (`agents/**`, `engine/**`, `process/**`, `templates/**`),
  every instruction file an agent tool reads at any depth, including the ones that take
  precedence over `AGENTS.md` (`**/AGENTS*.md`, which covers `AGENTS.override.md`,
  `**/CLAUDE*.md`, `**/.claude/**`, `**/.codex/**`), and every document those prompts tell the agents to obey (`docs/**` and
  `PCDA/**` in an instance, which today covers `docs/INTEGRATION.md`, `docs/principles.md`,
  `docs/fork-discipline.md` and `PCDA/quality-cycle.md`). Without this, a merged PR could
  change the driver that holds the App key, switch off a gate or rewrite the agents'
  instructions before the maintainer sees it. Because prompts gain references over time,
  `doctor` follows references transitively: starting from every instruction file above and
  the files under `agents/**`, it collects every repository path they name, then
  every path *those* files name, until nothing new appears, and fails when any file in that
  closure is not covered by `CODEOWNERS`. In a
  target repository the set is its `.github/**`, `CODEOWNERS`, the same instruction files
  at any depth (`**/AGENTS*.md`, `**/CLAUDE*.md`, `**/.claude/**`, `**/.codex/**`) with the
  transitive closure of the files they name, and whatever its required checks build or run with.

  `doctor` and `repo-settings.py check` read `wave_mode` and **fail** when `CODEOWNERS` does
  not cover the set its mode requires; the driver re-reads `CODEOWNERS` from `main` before
  every merge and refuses to merge while it is short.

  `merge` mode trusts the agent with ordinary code by definition: a non-final wave can change
  an existing test's assertions and reach `main` without review, to be seen only when the
  maintainer reviews the final wave. That is the trade the maintainer opts into. Instances
  that cannot accept it stay in `stack` mode, or add `tests/**` to `merge` mode's governing
  set, so that any PR touching a test waits for the maintainer.
- **Driver branches**: a branch ruleset lets the App create and update only the namespaces
  the driver itself uses, and refuses every other branch name to it. `repo-settings.py`
  derives the list from the instance's configuration rather than hard-coding it: the prefixes
  of `[publisher].fix_branch_pattern` and `feature_branch_pattern` (defaults `fix/*`,
  `enhancement/*`), `pdca-integration/*` for stacked batches (force-pushes allowed there, as
  `integrate.py` requires), `revert/*` for `pdca revert`, the bug track's branches, and
  `[records].branch` (default `records/*`) when records are published as pull requests, and
  `pdca-doctor/*` for the probe of section 6. `doctor` **fails** when a configured pattern falls
  outside the ruleset, so a renamed pattern blocks the run before a publish would fail.
- **No push rulesets.** Rulesets that block file paths on push are not assumed. Instead:
  - Without the Workflows permission the App cannot change `.github/workflows/**`, so every
    guard whose logic is written inline in its workflow file is out of its reach as it is
    (in the project template: `docs-immutability`, `dco`, `require-linked-issue`).
  - Code-owner review covers all of `.github/**` in both modes, so a change to a script a
    workflow runs (for example `.github/docs-check/*.py`) always needs the maintainer.
  - Required checks that run scripts take them from the base commit, not from the pull
    request's checkout, so even an approved change cannot make its own check pass by
    changing the checker in the same PR. That means the checker's **whole closure**, not just
    its entry script: the job checks out the base commit into its own directory and runs the
    checker, its helpers and its test runner from there, treating the pull request's tree as
    data. The tests themselves are the pull request's (they are what it changes), so a
    base-loaded `test-inventory` check fails when the pull request deletes, renames, skips or
    marks as expected-failure a test that exists on the base; such a pull request waits for
    the maintainer in both modes. The template's `docs-check`, `render-check` and
    `template-test` move to this pattern.
- **Tags and releases.** A tag ruleset on `refs/tags/**` restricts creation, update and
  deletion to the maintainer and to the release workflow, with no bypass for the agent App.
  The workflow's `GITHUB_TOKEN` acts as GitHub Actions, not as the maintainer, so the ruleset
  lists GitHub Actions as a bypass actor. That would let *any* workflow with
  `contents: write` create tags, so the audit of section 4 requires that the release job is
  the only job with `contents: write` and that it starts only from `workflow_dispatch`,
  which the agent App cannot trigger (it has no Actions permission). **Immutable releases** are on for
  every repository (`PUT /repos/{repo}/immutable-releases`; generally available since October
  2025, on every plan, private repositories included): a published release's tag and assets
  cannot change, its tag name can never be reused once deleted, and GitHub records an
  attestation that `gh release verify` and `gh release verify-asset` check. Releases are
  created only by the maintainer or by a workflow on `main`, as a draft that gets its assets
  before it is published, and **a tag is created together with its release** in that one step.
  `Contents: write` lets the App publish a release on any existing tag that has none yet, and
  under immutability its assets would then be locked in; with no tag ever left without a
  release there is nothing to attach to, and the auditor check (section 6) flags any tag that has no
  release.

  `Contents: write` also reaches **drafts**: while a draft waits for its assets, the App could
  replace them or publish it, and immutability would then lock in its version. So no draft
  waits. A release is assembled and published in one uninterrupted step by the release
  workflow on `main` (create the draft, upload, publish, in one job), which the maintainer
  starts with `workflow_dispatch`; there is no manual asset upload, because a manually
  uploaded asset would carry no attestation. Trust in the assets does not rest on that window
  being short: the release workflow signs a build-provenance attestation for every asset it
  built (`actions/attest-build-provenance`, which needs `id-token: write` and
  `attestations: write` on `main` only, out of the App's reach), and after publishing it
  compares each published asset's digest with its own build output and fails loudly on a
  mismatch. Consumers verify with `gh attestation verify --signer-workflow` naming that
  workflow, which an asset the App swapped in cannot pass.

  Immutability leaves a release's title and notes editable and the
  release deletable; with `Contents: write` the App could do either. That is accepted:
  downloads are trusted through the attestation, not the notes, and a deletion is visible in
  the audit log and cannot be replaced under the same tag.

### 4. CI secrets: nothing worth stealing on an agent branch

- Every repository secret moves into a GitHub Environment that requires the maintainer's
  approval or deploys only from `main`. Workflows on `pull_request` keep no secret.
- The default `GITHUB_TOKEN` stays read-only (`repo-settings.py` sets this today).
- **OIDC counts as a secret.** Any job with `id-token: write` that the App can start can
  exchange its token for a cloud or model credential with no stored secret involved. The App
  starts workflows through more than pull requests: its pushes fire `push` and `create`
  triggers on its own branches, and its comments, labels and issues fire `issue_comment`,
  `issues` and `pull_request_target`, and through them `workflow_run`; and `Contents: write`
  lets it call `POST /repos/{repo}/dispatches`, which fires `repository_dispatch` with a
  `client_payload` it chooses. That list is not meant to be complete: **every trigger
  outside the maintainer-only set below counts as App-causable**, including ones not named
  here (`delete`, for instance, which the App fires by deleting one of its branches).
- **Running from `main` is not the same as being trusted.** `pull_request_target`,
  `workflow_run`, `issue_comment`, `issues` and `repository_dispatch` run the workflow file from `main`, so an
  environment limited to `main` and a trust policy pinned to `refs/heads/main` both admit
  them, even when the App caused the event. A job that holds a secret or `id-token: write`
  is therefore allowed only in one of two shapes:
  - **triggered by the maintainer on code only the maintainer put there**: in `stack`
    mode, `workflow_dispatch`, `schedule` and `push` limited to `main`, because nothing
    reaches `main` there without the maintainer's review. In `merge` mode the App merges
    ordinary code into `main`, so even a maintainer-triggered run may build and execute
    agent-merged code; there **every** job holding a secret, `id-token: write` or a
    write-scoped token runs in an environment that requires the maintainer's approval, which
    approves that run and with it the exact commit it runs on. `release` is always
    App-causable (`Contents: write` covers releases), so it is never in this group; or
  - **triggered by an event the App can cause, and reviewed as such** (`stack` mode only,
    where everything the job imports or runs from the repository reached `main` through the
    maintainer's review; in `merge` mode such jobs are approval-gated like all others, since
    a hashed script could import ordinary code the App merged later): whether a job treats
    event data only as data cannot be decided from where the expressions sit (a fixed step
    can still `eval` an environment variable or read `GITHUB_EVENT_PATH`). So these jobs are
    allowed only from a maintainer-reviewed **allowlist** (`.github/privileged-jobs.toml`,
    covered by `CODEOWNERS`), which names each workflow and job with the SHA-256 of the
    whole workflow file (triggers, `permissions`, `env` and `defaults` included) and of the
    job's own directory under `.github/` (for example `.github/triage/**`), the only
    repository code the job may run: it checks out at most that directory (a sparse
    checkout), and third-party actions are pinned by commit SHA. Any change to the workflow
    file or to any file in that directory voids the entry, so there is no import or sourced
    file outside the digest.
    Reviewing an entry means confirming that the job does not check out the pull request's
    head, download the triggering run's artifacts or run anything the event names, and that
    event data reaches only consumers that treat it as data. ADR-0017's issue triage is such
    a job: it hands issue text to a model call. Every other privileged job on an App-causable
    event runs in an environment that requires the maintainer's approval.

  Every federation trust policy such a job could match pins the subject to
  `refs/heads/main` or to that environment.
- **A write-scoped `GITHUB_TOKEN` counts too.** The repository default is read-only, but a
  workflow's own `permissions:` block can raise it; such a job is treated like one holding a
  secret.
- **Workflows on every App-writable branch, not only `main`.** A `push` to a driver branch
  runs that branch's own copy of each workflow. A branch that predates the installation can
  carry an older workflow the App could then trigger without touching a workflow file. So
  before the App is installed, existing branches in the driver namespaces are audited or
  deleted, and the auditor reads the workflow files on every branch the App can write, not
  only on `main`.
- **Self-hosted runners.** A job the App can trigger on a persistent self-hosted runner runs
  App-controlled code on that host, with no GitHub secret needed to do harm. Such jobs run
  on GitHub-hosted runners, or on ephemeral runners that are rebuilt for every job and hold
  no credentials. The auditor reads each job's `runs-on` and the repository's runners and
  fails on an App-causable job that can land on a persistent self-hosted runner unless it
  is approval-gated.
- **Organization secrets** cannot be put behind an environment, and an organization secret
  selected for a repository reaches its workflows like a repository secret. So no
  organization secret may be visible to a repository the agent App is installed on; the
  auditor fails on any that is.
- The auditor check (section 6) lists any repository-level secret, any organization secret
  visible to an installed repository, any workflow that reads a
  secret outside an approval-gated environment, and every job with a secret,
  `id-token: write` or a write-scoped `GITHUB_TOKEN` that matches neither shape: not
  approval-gated, and not on the allowlist with matching digests. As a review aid it also
  flags known-dangerous patterns by line (head checkout, artifact download, an
  event-controlled expression outside `env:`, including in an action's `with:` inputs such as
  `actions/github-script`'s `script`), but passing those patterns never admits a job.

### 5. The local side

As in client-rust-pdca, generalized: one OS user per unattended loop (`setup-loop-user.sh`
moves into the template), holding the App key, the pipx tools, and the secret guard
activated as that user (the `doctor` secret-guard row, #959, reports it). Attended runs from
the maintainer's account stay allowed and `doctor` keeps warning about them.

### 6. Proof in `doctor`, not trust

Each row reads what GitHub actually grants and fails when the answer is missing or
ambiguous; none infers a restriction from an error it did not provoke. The rows run before
every unattended run, so no proof is cached between runs.

**With the agent App's own token** (it already has Metadata: read, which covers rulesets):

- **Installations**: the key can mint tokens for every installation of its App, not only
  the one in use, so the row first lists `GET /app/installations` with the App's JWT and fails
  unless there is exactly one installation, on the expected owner, and reads the App's own
  metadata (`GET /app`) to confirm it is private (`public: false`), because a public App with
  one installation today can gain another tomorrow. Each agent App is created
  as a private App by the owner it serves, so GitHub refuses installation anywhere else; the
  auditor Apps are checked the same way.
- **Identity and permissions**: the row mints an *unscoped* diagnostic token (no repository
  list), so the response shows the installation's full grant, and enumerates
  `GET /installation/repositories`. It fails unless the permissions are exactly section 2's
  set (no `workflows`, no `administration`, no `secrets`), the repositories are exactly the
  configured ones, and every one of them is public (section 3); a repository the App was
  installed on by mistake shows up here.
- **Rules, including tags**: `GET /repos/{repo}/rules/branches/{branch}` returns the effective
  rules for `main` and for a branch outside the allowed patterns, and
  `GET /repos/{repo}/rulesets?includes_parents=true` with `GET …/rulesets/{id}` returns every
  ruleset's target, enforcement and conditions. The row checks that section 3's branch and
  tag rulesets exist, are `active`, cover `main` and `refs/tags/**`, and carry **every rule**
  section 3 requires (pull request with code-owner review and stale-approval dismissal,
  required status checks on `main`; creation, update and deletion restrictions on tags and
  on every branch that is neither a driver namespace nor `main`, which is left to its
  pull-request rule so that `merge` mode's merges can land without a bypass), compared rule by rule with their parameters, since a ruleset with
  the right conditions but a missing rule has nothing to bypass, and that the
  namespaces the App may write are **exactly** the derived list of section 3: it compares the
  ruleset's include and exclude conditions with that list and fails on any extra pattern (a
  stale `release/*` exclusion, say) as well as on a missing one. Sampling one forbidden
  branch name would not prove that.
- **No bypass for the App**: GitHub hides a ruleset's `bypass_actors` list from anyone without
  write access to the ruleset, but every ruleset response carries `current_user_can_bypass`
  for the caller. Asked with the agent App's token, it answers exactly the question that
  matters: the row fails unless it is `never` for every ruleset, and fails as inconclusive
  when the field is missing. Who *else* may bypass (the maintainer, say) is not this
  proposal's concern and is not checked here.
- **Reach**: repository access to a canary repository outside the installation is refused
  (the all-zero-ref probe of `token-scope` suffices for this one: it only asks whether the
  repository is reachable).
- **Key files**: the agent and auditor keys are readable by the loop user only.

`doctor` itself stays read-only, as it is today. The one check that has to write runs
separately, when an App is installed and whenever its permissions change, against a
scratch repository of the same owner (`pdca-probe`, part of the installation's configured
repositories) rather than a live one:

- **Workflow changes are refused** (`pdca setup-identity --probe`, not `doctor`): a probe pushes a commit that adds an inert workflow
  (triggered only by `workflow_dispatch`, with a single job guarded by `if: false`) to a
  fresh `pdca-doctor/probe-<random>` branch and expects GitHub to refuse it. If the push is
  accepted, the row fails, deletes the branch (the ruleset allows the App to delete
  `pdca-doctor/*`), retries the deletion up to three times and confirms with
  `git ls-remote` that the branch is gone. If it is still there, the row fails with the
  branch name to remove by hand, and records it in the instance state, so every later run refuses
  to start until it is gone. Any other response, a timeout or a lost reply
  included, fails as inconclusive and takes the same path: query the remote for the branch,
  delete it if it exists, and record it if it cannot be removed. Nothing is left behind on
  success.

**With the auditor App's token**: first the same identity check as for the agent App, an
unscoped token whose permissions must equal the auditor table of section 2 exactly (any write
permission fails) and whose repositories must be exactly the configured ones. Then:
immutable releases are on, every tag has a release, every release is immutable, and the secret, OIDC and write-token audit of section 4 passes. Any
failure blocks the run.

Turning immutability on protects only releases published afterwards; older ones keep
`immutable: false`, and `Contents: write` can still replace their assets. A digest snapshot
checked now and then cannot close that, since an asset can be swapped and restored between
two checks. So before the agent App is installed on a repository, every mutable release there
is either republished, which makes it immutable, or deleted, and the auditor fails on any
mutable release from then on. `repo-settings.py` lists the releases concerned when the
setting is switched on (for the existing repositories, 14.11.2026).

**Independently, from n8n**: the same auditor checks run daily from n8n with the auditor
keys held as n8n credentials, and a failure is pushed to ntfy. This catches a loosened
setting between runs and does not depend on the loop host, so a compromised loop host
cannot hide one. It covers only what the auditor keys can see: whether the agent App could
bypass a ruleset, and which permissions its installation holds, can be asked only with the
agent App's own key, which n8n does not get. Those two are checked by `doctor` before every
run; between runs only the maintainer can change them, since the agent App has no
Administration permission. n8n calls the GitHub API directly rather than running `repo-settings.py`,
so the logic exists twice. What keeps the two equal is a shared set of **test vectors**:
recorded API responses, each with the verdict every check must return (passing and failing
cases for each check). The harness's CI runs the Python checks against them; the n8n workflow
runs its checks against the same vectors, fetched from `main`, before every daily audit,
and alerts instead of auditing when any verdict differs. Vectors alone would not catch a check
that one side simply lacks, so a **check manifest** (one ID per check, in the repository next
to the vectors) is the shared definition: every manifest ID must have at least one passing and
one failing vector, which the harness's CI enforces, and each side reports the IDs it ran.
The run fails when that set differs from the manifest, on either side.

`repo-settings.py check` with the maintainer's admin login remains for what only an admin
can see, the full `bypass_actors` lists, and for applying settings. It is a maintenance
tool, not a gate on runs.

The checks rest on two API facts confirmed in GitHub's REST documentation on 14.11.2026:
rulesets are readable with Metadata: read, while `bypass_actors` "is only returned if the
user making the API request has write access to the ruleset". The documentation puts no
such restriction on `current_user_can_bypass`; the pilot confirms that it is returned to the
agent App, and the row fails closed if it is not.

## Alternatives considered

- **Fine-grained personal token (ADR-0006 as is).** Long-lived, tied to a person, and it acts
  as that person for bypass lists. Kept as the fallback for an owner that cannot install an App.
- **Machine user that works from a fork.** Fork pull requests run CI with no secrets, a
  read-only token and an approval gate, which is the strongest CI isolation. But the account
  holds long-lived credentials, needs its own login and two-factor setup, and the loop's
  fold, stack and revert flows assume branches in the target repository. Gated environment
  secrets (section 4) reach nearly the same isolation without forks.
- **Deploy keys per repository.** Write access includes workflows and every branch, and there
  is no API probe of what a key can do.
- **Only CODEOWNERS, no permission changes.** It catches what the maintainer reviews, but not
  what CI executes before review.

## Graduation criteria

1. **Pilot** on wyrd-pdca and its target wyrd: agent App and auditor App installed, the
   instance's driver publishes through the agent App, rulesets and code owners applied,
   secrets moved into environments, and the daily n8n audit alerting through ntfy.
   Done when a full flow (plan to publish) runs on the App token, the `doctor` rows above
   pass and are shown to fail on a token with workflow or admin rights and on a ruleset that
   lists the App as a bypass actor, and `current_user_can_bypass` is confirmed to reach the
   agent App.
2. **Template**: `repo-settings.py` applies the rulesets, `CODEOWNERS` matching `wave_mode`,
   immutable releases and the secret audit, and refuses an App installation on a repository
   without enforced branch protection; `docs-check`, `render-check` and `template-test` load
   their checker closure from the base commit;
   `setup-loop-user.sh` and the App-token minting move into the template; the `doctor` rows
   ship with tests against a stub API.
3. **Roll out** to the other instances through `copier update`, one owner at a time.
4. An ADR records the decision; it supersedes ADR-0006's credential section in
   client-rust-pdca and ADR-0021 §4's deploy key.

## Security impact

This narrows what an agent-controlled process can change; it adds no untrusted input. Each App's
private key becomes a new high-value, long-lived secret: it can mint tokens for every
repository of its owner's installation (section 1), so it is stored for the loop user only,
rotated on a schedule and revoked on exposure; one App per owner bounds a stolen key to one
owner. The auditor App's key is read-only and reveals configuration and secret names, not
values; it sits with the loop user and in n8n. The threat models (`docs/design/architecture/14-threat-model.md`,
`.oss-scanner/threat_model.md`) get the credential boundary, the CI secret rule and the
protected paths in the same change that lands step 2.

What it does not change: prompt injection through issues, comments and model output stays
possible. In `stack` mode this proposal limits its reach to "push a driver branch and open a
pull request the maintainer reviews". In `merge` mode it also reaches "merge ordinary code
into `main`" for non-final waves, as section 3 accepts; governing files still wait for the
maintainer.

## Backward compatibility

Instances keep working on a personal token or the maintainer's login until they switch; the
publisher takes the credential from configuration. Rulesets and code owners change what the
maintainer must review, not what the loop produces. Workflows that read a repository secret
on `pull_request` break until that secret moves into an environment, which is the point; the
`doctor` row names them first.

## Decisions taken while drafting

- **App approvals** (was open): the review rule is code-owner review, which an App cannot
  satisfy, so whether GitHub counts an App's approval no longer matters. The App keeps the
  ability to merge for `merge`-mode instances (section 3).
- **Push rulesets** (was open): not assumed, since PDCA cannot rely on a paid plan; the
  missing Workflows permission, code-owner review of `.github/**` and checkers loaded from the
  base commit replace them. The App is installed only on repositories where branch protection
  is enforced (public ones on a free plan).
- **Immutable releases** (was open): available on every plan. Switched on for all 25 public,
  non-fork, non-archived repositories of getwyrd, Ralphovi and eduralph on 14.11.2026;
  `repo-settings.py` keeps them on. Editable notes and deletable releases are accepted (section 3).
- **App key location for the bug-track driver** (was open): the key goes only to the driver,
  never into the job VM, and is stored by the driver's backend. On a local host it is a
  Podman secret (or systemd credential) readable only by the driver's container; on Google
  Cloud it is a Secret Manager secret readable only by the driver's service account. Once a
  model-key gateway exists, a shared token broker that hands the driver short-lived
  installation tokens replaces both, so no backend holds the key. ADR-0021 is to be
  superseded by a backend-neutral decision (a disposable job VM on a dedicated host); this
  rule applies to whichever backend that decision allows.

## Open questions

None remain; question 4 was settled on 14.11.2026 (below, under decisions).
