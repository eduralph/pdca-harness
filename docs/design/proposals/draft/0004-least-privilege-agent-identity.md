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

### 1. Identity: one GitHub App, one installation per owner

A GitHub App (working name `pdca-bot`) is installed on each owner the loops write to
(getwyrd, Ralphovi, eduralph), each installation limited to the repositories of that
owner's instances and targets.

- The driver mints an **installation token** per run from the App's private key; it expires
  after one hour, so no long-lived GitHub credential is on disk.
- The private key is readable only by the loop's OS user (section 4). Model leaves and gates
  never see it or a token (ADR-0007's sandbox rules stand).
- Commits and pull requests appear as `pdca-bot[bot]`, never as the maintainer, so rulesets
  can name the agent and review rules can require a human.
- The App replaces the personal token of ADR-0006 and the deploy key of ADR-0021 §4.

### 2. Permissions

| Permission | App | Why |
|---|---|---|
| Contents | read & write | push bundle branches |
| Pull requests | read & write | open and update pull requests |
| Issues | read & write | backlog comments, labels, cleanup |
| Metadata | read | mandatory |
| Workflows | **none** | GitHub refuses a push that creates or changes `.github/workflows/**` |
| Administration, Secrets, Environments, Actions, Pages, Members | **none** | no settings, protection, secrets or CI control |

### 3. Rulesets and code owners (template `repo-settings.py`)

- **`main`**: changes only through a pull request with one approving review, approval of the
  most recent push by someone other than its pusher, and no bypass for the App.
- **Bundle branches**: the App may create and update only `bundle/*` (and `bug/*`,
  `bugfix/*` for the bug track); every other branch name is refused to it.
- **Code owners**: `.github/**`, `.secret-guard/**`, `.pre-commit-config.yaml`, `AGENTS.md`,
  `pdca.toml`, `scripts/**` and `CODEOWNERS` itself are owned by the maintainer and require
  that review. Where push rulesets are available, the same paths are blocked for the App
  outright, not just reviewed.

### 4. CI secrets: nothing worth stealing on an agent branch

- Every repository secret moves into a GitHub Environment that requires the maintainer's
  approval or deploys only from `main`. Workflows on `pull_request` keep no secret.
- The default `GITHUB_TOKEN` stays read-only (`repo-settings.py` sets this today).
- `doctor` (or `repo-settings.py check`) lists any repository-level secret and any workflow
  that reads a secret outside an approval-gated environment.

### 5. The local side

As in client-rust-pdca, generalized: one OS user per unattended loop (`setup-loop-user.sh`
moves into the template), holding the App key, the pipx tools, and the secret guard
activated as that user (the `doctor` secret-guard row, #959, reports it). Attended runs from
the maintainer's account stay allowed and `doctor` keeps warning about them.

### 6. Proof in `doctor`, not trust

Rows that ask GitHub what the credential can do, as `token-scope` does today:

- the token belongs to the App, not to a person;
- it can push a `bundle/*` branch to the instance and target repositories, and cannot push to
  `main`, to a branch outside the allowed patterns, or a change to `.github/workflows/**`;
- it cannot reach a canary repository outside the installation;
- the private key file is readable by the loop user only.

Each probe uses a ref that cannot exist (an all-zero commit, as `token-scope` does), so nothing
is ever written.

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

1. **Pilot** on wyrd-pdca and its target wyrd: App installed, the instance's driver publishes
   through it, rulesets and code owners applied, secrets moved into environments.
   Done when a full flow (plan to publish) runs on the App token, and the `doctor` rows above
   pass and are shown to fail on a token with workflow or admin rights.
2. **Template**: `repo-settings.py` applies the rulesets, code owners and the secret audit;
   `setup-loop-user.sh` and the App-token minting move into the template; the `doctor` rows
   ship with tests against a stub API.
3. **Roll out** to the other instances through `copier update`, one owner at a time.
4. An ADR records the decision; it supersedes ADR-0006's credential section in
   client-rust-pdca and ADR-0021 §4's deploy key.

## Security impact

This narrows what an agent-controlled process can change; it adds no untrusted input. The App
private key becomes a new high-value secret (it can mint tokens for every installation of
that owner): it is stored for the loop user only, rotated on a schedule, and its installation
is limited to the listed repositories. The threat models (`docs/design/architecture/14-threat-model.md`,
`.oss-scanner/threat_model.md`) get the credential boundary, the CI secret rule and the
protected paths in the same change that lands step 2.

What it does not change: prompt injection through issues, comments and model output stays
possible; this proposal limits its reach to "push a bundle branch and open a pull request the
maintainer reviews".

## Backward compatibility

Instances keep working on a personal token or the maintainer's login until they switch; the
publisher takes the credential from configuration. Rulesets and code owners change what the
maintainer must review, not what the loop produces. Workflows that read a repository secret
on `pull_request` break until that secret moves into an environment, which is the point; the
`doctor` row names them first.

## Open questions

- **[OPEN]** Do approvals by a GitHub App count toward required reviews? If they do, the
  ruleset must exclude the App from reviewers, or rely on code-owner review by the maintainer.
- **[OPEN]** Push rulesets with file-path restrictions: availability on public repositories
  and on personal accounts (eduralph) must be checked; code-owner review is the fallback.
- **[OPEN]** One App for all three owners, or one per owner, so that a stolen key reaches only
  one owner's repositories.
- **[OPEN]** Where the App key lives for ADR-0021's driver container (secret manager versus a
  mounted file), to be settled with that ADR's pilot.
