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
- **Tokens are short-lived.** The driver mints an installation token per run, scoped down to
  the repositories that run touches; it expires after one hour and is never written to disk.
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
| Contents | read & write | push bundle branches (this also covers tags and releases; section 3 fences those) |
| Pull requests | read & write | open and update pull requests |
| Issues | read & write | backlog comments, labels, cleanup |
| Metadata | read | mandatory |
| Workflows | **none** | GitHub refuses a push that creates or changes `.github/workflows/**` |
| Administration, Secrets, Environments, Actions, Pages, Members | **none** | no settings, protection, secrets or CI control |

### 3. Rulesets and code owners (template `repo-settings.py`)

Everything here works on GitHub's free plan; PDCA cannot assume a paid one.

- **Public repositories only.** Branch protection and rulesets are enforced on public
  repositories on every plan, but not on private repositories of a free personal account. The
  App is therefore installed only where they are enforced; `repo-settings.py check` fails an
  installation that includes a repository where they are not. A private repository's loop
  keeps publishing from the maintainer's own login in `stack` mode, as today.
- **`main` is reached only through a pull request.** No bypass for the App, required status
  checks from `.github/required-checks.txt`, and **code-owner review** as the review rule, with
  no general approval count. `CODEOWNERS` names the maintainer; only users and teams can be
  code owners, so the App can never satisfy that review, whether or not GitHub counts an
  App's approval as a review.
- **What `CODEOWNERS` covers follows the instance's `[driver].wave_mode`.** PDCA's opt-in
  `merge` mode merges each non-final wave's PR itself so the next wave can build on it (the
  final wave's PR stays a draft for the maintainer); `stack` mode, the default, merges nothing.

  | `wave_mode` | `CODEOWNERS` covers | The App can merge |
  |---|---|---|
  | `stack` (default) | everything (`*`) | nothing: every PR needs the maintainer |
  | `merge` | the files that govern the agent: `.github/**`, `.secret-guard/**`, `.pre-commit-config.yaml`, `AGENTS.md`, `pdca.toml`, `scripts/**`, `CODEOWNERS` | an ordinary code PR once its required checks pass; a PR touching a governing file still waits for the maintainer |

  `doctor` and `repo-settings.py check` read `wave_mode` and warn when `CODEOWNERS` does not
  match it.
- **Bundle branches**: the App may create and update only `bundle/*` (and `bug/*`,
  `bugfix/*` for the bug track); every other branch name is refused to it.
- **No push rulesets.** Rulesets that block file paths on push are not assumed. Instead:
  - Without the Workflows permission the App cannot change `.github/workflows/**`, so every
    guard whose logic is written inline in its workflow file is out of its reach as it is
    (in the project template: `docs-immutability`, `dco`, `require-linked-issue`).
  - Code-owner review covers all of `.github/**` in both modes, so a change to a script a
    workflow runs (for example `.github/docs-check/*.py`) always needs the maintainer.
  - Required checks that run scripts take them from the base commit
    (`git show "$BASE_SHA":<path>`), not from the pull request's checkout, so even an approved
    change cannot make its own check pass by changing the checker in the same PR. The
    template's `docs-check` and `template-test` move to this pattern.
- **Tags and releases.** A tag ruleset on `refs/tags/**` restricts creation, update and
  deletion to the maintainer, with no bypass for the App. **Immutable releases** are on for
  every repository (`PUT /repos/{repo}/immutable-releases`; generally available since October
  2025, on every plan, private repositories included): a published release's tag and assets
  cannot change, its tag name can never be reused once deleted, and GitHub records an
  attestation that `gh release verify` and `gh release verify-asset` check. Releases are
  created only by the maintainer or by a workflow on `main`, as a draft that gets its assets
  before it is published, and **a tag is created together with its release** in that one step.
  `Contents: write` lets the App publish a release on any existing tag that has none yet, and
  under immutability its assets would then be locked in; with no tag ever left without a
  release there is nothing to attach to, and `repo-settings.py check` flags any tag that has no
  release. Immutability leaves a release's title and notes editable and the
  release deletable; with `Contents: write` the App could do either. That is accepted:
  downloads are trusted through the attestation, not the notes, and a deletion is visible in
  the audit log and cannot be replaced under the same tag.

### 4. CI secrets: nothing worth stealing on an agent branch

- Every repository secret moves into a GitHub Environment that requires the maintainer's
  approval or deploys only from `main`. Workflows on `pull_request` keep no secret.
- The default `GITHUB_TOKEN` stays read-only (`repo-settings.py` sets this today).
- **OIDC counts as a secret.** A `pull_request` job with `id-token: write` can exchange its
  token for a cloud or model credential with no stored secret involved. Such jobs are removed,
  or every federation trust policy they could match pins the subject to
  `refs/heads/main` (as ADR-0017 already does for issue triage).
- `repo-settings.py check` lists any repository-level secret, any workflow that reads a
  secret outside an approval-gated environment, and any `pull_request` workflow that requests
  `id-token: write`.

### 5. The local side

As in client-rust-pdca, generalized: one OS user per unattended loop (`setup-loop-user.sh`
moves into the template), holding the App key, the pipx tools, and the secret guard
activated as that user (the `doctor` secret-guard row, #959, reports it). Attended runs from
the maintainer's account stay allowed and `doctor` keeps warning about them.

### 6. Proof in `doctor`, not trust

Each row reads what GitHub actually grants and fails when the answer is missing or
ambiguous; none infers a restriction from an error it did not provoke:

- **Identity and permissions**: the row mints an *unscoped* diagnostic token (no repository
  list), so the response shows the installation's full grant, and enumerates
  `GET /installation/repositories`. It fails unless the permissions are exactly section 2's
  set (no `workflows`, no `administration`, no `secrets`) and the repositories are exactly the
  configured ones; a repository the App was installed on by mistake shows up here.
- **Branch rules**: `GET /repos/{repo}/rules/branches/{branch}` returns the effective rules
  for `main` and for a branch outside the allowed patterns; the row checks that the rules of
  section 3 apply. That endpoint covers branches only; tag rules are checked from the
  maintainer's side (below). It cannot see bypass actors (that needs Administration access the
  App does not have), so it does not claim to.
- **Workflow changes are refused**: a probe pushes a commit that touches
  `.github/workflows/` to a fresh `bundle/doctor-probe-<random>` branch and expects GitHub to
  refuse it. If the push is accepted, the row deletes the branch and fails; any other
  response fails as inconclusive. Nothing is left behind on success.
- **Reach**: repository access to a canary repository outside the installation is refused
  (the all-zero-ref probe of `token-scope` suffices for this one: it only asks whether the
  repository is reachable).
- **Key file**: readable by the loop user only.

What the App cannot read is checked from the maintainer's side: `repo-settings.py check`,
run with the maintainer's admin login, reads the ruleset definitions and fails unless the
tag ruleset of section 3 targets `refs/tags/**` and no ruleset lists the App among
`bypass_actors`, checks that immutable releases are on and that every tag has a release, and runs the secret and
OIDC audit of section 4. `doctor` reports when that check last passed rather than repeating
it with credentials it must not hold.

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
2. **Template**: `repo-settings.py` applies the rulesets, `CODEOWNERS` matching `wave_mode`,
   immutable releases and the secret audit, and refuses an App installation on a repository
   without enforced branch protection; `docs-check` and `template-test` load their scripts
   from the base commit;
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
owner. The threat models (`docs/design/architecture/14-threat-model.md`,
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

## Open questions

- **[OPEN]** Where the App key lives for ADR-0021's driver container (secret manager versus a
  mounted file), to be settled with that ADR's pilot.
