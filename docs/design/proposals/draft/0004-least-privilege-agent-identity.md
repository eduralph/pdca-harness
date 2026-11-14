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
| Administration | read | immutable releases, the Actions workflow-permission settings, and repository self-hosted runners |
| Organization: Self-hosted runners | read | organization runners available to the repository (getwyrd, Ralphovi) |
| Secrets | read | names of repository secrets, and of organization secrets visible to the repository (`GET /repos/{repo}/actions/organization-secrets`) |
| Environments | read | names of environment secrets |
| Actions | read | environments and their protection rules |
| Contents | read | workflow files for the audit of section 4, tags and releases |
| Attestations | read | the provenance attestations of release assets (`gh attestation verify`) |
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
  | `stack` (default) | everything (`*`) | nothing: `main` also carries an update restriction whose only bypass actor is the maintainer (pull-request mode), so even an approved PR is merged by the maintainer, not the App |
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
  transitive closure of the files they name, and the implementation of its required checks:
  CI configuration, build and test-runner configuration and scripts, not the application
  source those checks build and test (that is the ordinary code `merge` mode merges).

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
  deletion to the maintainer, with **no bypass for anyone else**, GitHub Actions included.
  **Immutable releases** are on for every repository (`PUT /repos/{repo}/immutable-releases`;
  generally available since October 2025, on every plan, private repositories included): a
  published release's tag and assets cannot change, its tag name can never be reused once
  deleted, and GitHub records an attestation that `gh release verify` and
  `gh release verify-asset` check.

  The maintainer releases by pushing a tag (`v*`) from their own login. A release workflow
  triggered by that tag push builds the assets, signs a build-provenance attestation for
  each (`actions/attest-build-provenance`), and creates, fills and publishes the release in
  one job. That job runs in a `release` environment whose deployment policy admits only `v*`
  tags and which requires the maintainer's approval, so its `id-token: write` and
  `attestations: write` are reachable from nothing the App can create (section 4). There
  is no manual asset upload, because a manually uploaded asset would carry no attestation.

  `Contents: write` still lets the App create or publish a release on that tag in the
  seconds before the workflow does, or touch a draft. That can be detected, not prevented:
  the auditor fails on any release not published by `github-actions[bot]` and on any asset
  without a provenance attestation from the release workflow at that tag, and consumers
  verify with `gh attestation verify --signer-workflow <release workflow>
  --source-ref refs/tags/<tag>`, which an asset the App supplied cannot pass. Because
  immutability would lock such a release, the response is to delete it and release under
  the next version number.

  Turning immutability on protects only releases published afterwards; older ones keep
  `immutable: false`, and `Contents: write` can still replace their assets. A digest
  snapshot checked now and then cannot close that, since an asset can be swapped and
  restored between two checks. So before the agent App is installed on a repository, every
  mutable release there is either republished, which makes it immutable, or deleted, and the
  auditor fails on any mutable release from then on. `repo-settings.py` lists the releases
  concerned (for the repositories switched on, 14.11.2026).

  Immutability leaves a release's title and notes editable and the release deletable; with
  `Contents: write` the App could do either. That is accepted: downloads are trusted through
  the attestation, not the notes, and a deletion is visible in the audit log and cannot be
  replaced under the same tag.

### 4. CI privilege: enforced by settings, never by workflow content

**The principle.** A push runs the workflow files *of the pushed branch*. The App cannot
change a workflow file, but it can point a driver branch at any commit in the repository's
history and push there, which runs whatever workflow that old commit carried. So no
protection may depend on what a workflow file says: every privilege a workflow could use is
fenced by a repository or organization **setting** that GitHub enforces whatever the file
says, and the auditor (section 6) checks those settings. Reading workflow files remains a
review aid, never the gate.

- **Every environment is fenced, whatever it holds.** An environment that holds no secret
  can still be an OIDC trust anchor (below), so the rules here apply to every environment
  in the repository, not only to those with secrets.
- **Secrets live only in environments.** No repository secret, and no organization secret
  visible to a repository the App is installed on (an organization secret cannot be put
  behind an environment). Every environment that holds a secret has a deployment policy that
  admits only `main` (or, for `release`, only `v*` tags), so a job on a driver branch gets
  nothing however its file is written. An environment additionally **requires the
  maintainer's approval** unless every job that can use it runs only maintainer-reviewed
  code: code under `.github/**` (code-owned in both modes, section 3) and actions pinned by
  commit SHA. Issue triage (ADR-0017) is such a job: it hands issue text to a model call.
  In `merge` mode, any environment whose jobs build or run ordinary repository code requires
  approval, since the App merges ordinary code into `main`; approving a run approves the
  exact commit it runs on.
- **OIDC trusts environments, not refs.** Any job, on any branch, can request an OIDC token.
  So every cloud trust policy pins the subject to an environment
  (`repo:<owner>/<repo>:environment:<name>`), never to a branch or to `ref:refs/heads/main`:
  `pull_request_target`, `issue_comment` and `workflow_run` jobs also carry `main` as their
  ref, even when the App caused the event. A token from a job outside a protected
  environment then matches no policy. These policies live with the cloud provider, out of
  the auditor's reach; `repo-settings.py` prints the subjects the maintainer must pin, and
  the auditor checks the repository's OIDC subject template is the default, which includes
  the environment.
- **Privileged jobs build clean.** A job in an environment restores no Actions cache and
  downloads no artifact from another run: in `merge` mode an unprivileged run on `main` could
  have filled the cache with agent-merged output. Unlike the other workflow rules this one
  is a real gate: deployment policies admit only `main` (and `v*` tags), so only the current,
  code-owned workflow files on `main` and on release tags can reach an environment, and the
  auditor reads exactly those. It fails on any environment job that uses `actions/cache`,
  a `setup-*` action with caching on, or `download-artifact` with a `run-id`.
- **What the workflow token can still do.** The repository default for `GITHUB_TOKEN` is
  read-only, but a workflow file can raise it, and an old commit's file can too. Every
  writable scope is therefore accounted for:

  | Scope | Fenced by | Residual |
  |---|---|---|
  | `contents` | rulesets on `main`, tags and non-driver branches (no bypass for GitHub Actions) | driver branches, which the App can write anyway |
  | `pull-requests`, `issues`, `discussions` | "Allow GitHub Actions to create and approve pull requests" off; code-owner review | comments and labels, as the App |
  | `id-token` | trust policies pinned to fenced environments | none |
  | `attestations` | consumers verify `--source-ref refs/tags/<tag>` and the signer workflow | an attestation from a branch run, which that check rejects |
  | `deployments` | environment deployment policies | deployment records without access |
  | `pages` | the `github-pages` environment's deployment policy (`main` only) | none |
  | `actions` | none available | known limit: disable workflows, cancel or delete runs and artifacts |
  | `statuses`, `checks` | none available | known limit: fake status checks |
  | `packages` | none available | known limit |
  | `security-events` | none available | known limit: upload or dismiss code-scanning results |

- **No persistent self-hosted runners** are available to a repository the App is installed
  on, at repository or organization level. A job on such a runner runs App-controlled code
  on that host, and a runner group cannot be restricted by branch on a personal account.
  GitHub-hosted runners only, or ephemeral runners rebuilt for every job holding no
  credentials.

**Known limits** (accepted; the pilot shows whether any needs more):

- **Fake status checks in `merge` mode.** A job with `statuses: write` or `checks: write`
  on an old commit can post a passing status for a required check's name, and GitHub
  attributes it to GitHub Actions just like the real check. In `stack` mode the maintainer
  reviews every PR, so this changes nothing. In `merge` mode it can let the driver merge an
  ordinary-code PR whose real checks failed; governing files still wait for the maintainer.
- **Packages.** A token with `packages: write` can publish to GitHub Packages from a driver
  branch, and package access cannot be restricted by branch. Repositories that publish
  packages from Actions are listed by the auditor, and their publishing moves to the
  `release` environment's credentials or they stay outside the App installation.
- **Disruption through `actions: write`.** A workflow on an old commit can disable other
  workflows, cancel runs and delete runs, logs and artifacts. That cannot loosen a gate: a
  required check that does not run leaves the PR unmergeable. It can erase evidence, so the
  n8n audit alerts on a disabled workflow.
- **Code scanning.** With `security-events: write` such a workflow can upload or dismiss
  code-scanning results. Code scanning is not a merge gate here; the maintainer treats its
  alerts as advisory.
- **Event-data injection on `main`.** Whether a reviewed job treats event text only as data
  is a property of reviewed code; the auditor flags known patterns (head checkout, an
  event-controlled expression outside `env:`, `actions/github-script` fed event data) but
  cannot prove their absence.

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
  when the field is missing. Other bypass actors matter too: a ruleset that lets
  GitHub Actions or a deploy key bypass would let a workflow on an old commit push straight
  to `main`. Those lists are visible only with write access to the rulesets, so they are
  checked from the maintainer's side (below).
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
the settings of sections 3 and 4: immutable releases are on, every release is immutable,
published by `github-actions[bot]` and attested by the release workflow, and every tag
older than an hour has one; no repository secret and no visible organization secret; every
environment with a secret has the deployment policy and, where section 4 requires it, the
approval rule; the OIDC subject template is the default; "Allow GitHub Actions to create
and approve pull requests" is off; no persistent self-hosted runner is available; and the
review aids of section 4 report nothing new on `main`. Any failure blocks the run.

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
can see: the full `bypass_actors` lists. It fails unless every ruleset's only bypass actor
is the maintainer (the repository admin role, or the organization admin), with no
`Integration` (GitHub Actions or any App) and no `DeployKey`. It runs when an App is first
installed, and `repo-settings.py apply` runs it after every ruleset change it makes;
`doctor` shows the date of the last pass. Only an admin can change a ruleset, so between
those checks only the maintainer can add a bypass actor; that residual is accepted.

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
