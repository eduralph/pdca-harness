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
  a stolen key from reaching the other owners. The key is readable only by a separate key
  user, never by the loop user that runs the driver and its model leaves (section 5), rotated on a schedule (GitHub allows several active keys, so rotation has no gap),
  and revoked in the App settings if exposed. The key user holds the App's **only** key:
  GitHub offers no API that lists an App's keys, so `pdca setup-identity` and every
  rotation print the minter key's SHA-256 fingerprint for the maintainer to compare with
  the App settings page, which must show that key alone (two during a rotation). The App
  is registered by the maintainer's account, or by an organization whose only owner is the
  maintainer and which has no App managers (neither organization-wide nor for this App),
  so no one else can add a key. No API shows App managers or an App's keys, so this is a
  **recurring manual attestation**: at setup, at each rotation and at least every 30 days
  (n8n sends the reminder through ntfy), the maintainer checks both pages and records the
  date with `pdca setup-identity --attest`, which writes it to a file owned by the key user
  (the command runs through `sudo -u <owner>-keys` from the maintainer's own login, never
  as the loop user). The minter refuses to mint once that date is older than 35 days, and
  `doctor` reports it. Section 4's known limits record that the check is manual.
- **Tokens are short-lived.** The driver mints installation tokens just in time, scoped down
  to the repositories the run touches and to **exactly** section 2's permissions (the
  minter names them in every token request, so a grant the installation gained by mistake,
  such as `workflows` or `administration`, never reaches a token; it also runs the
  installation check of section 6 itself before minting and refuses while that fails), and keeps them only in memory. A token lives one hour
  while a bundle runs for hours (the median was 6.1 hours in proposal 0001), so the driver's
  GitHub client mints a fresh one before each authenticated call once the current one is
  older than 50 minutes; a call that still meets an expired token re-mints once and retries.
  Model leaves and gates are not handed key or token. The driver never puts the token in its
  own environment: git gets it from a credential helper, invoked with
  `-c credential.helper= -c credential.helper=<driver helper>` so that the empty entry resets
  any system, global or repository helper that could otherwise store the token, and each
  `gh` call gets `GH_TOKEN` in that one subprocess's environment only, with `GH_CONFIG_DIR`
  set to an empty directory so no stored login can stand in for it. Every `gh` call also
  names its target explicitly (`--repo github.com/<owner>/<repo>`, or the full
  `repos/<owner>/<repo>/…` path for `gh api`) after checking it against the loop's
  repository allowlist, with `GH_HOST`, `GH_REPO` and `GH_ENTERPRISE_TOKEN` removed from its
  environment, so neither an inherited variable nor a leaf-edited remote can point it at
  another repository the token reaches.

  **Driver git and `gh` get a built environment, not an inherited one.** Each subprocess
  starts from an empty environment plus an allowlist: a fixed `PATH` of root-owned
  directories, the fresh `HOME` below, locale variables, and the variables named in this
  section; everything else, `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`,
  `GIT_OBJECT_DIRECTORY`, `GIT_ALTERNATE_OBJECT_DIRECTORIES` and `GIT_EXEC_PATH` among
  them, is absent, so nothing inherited can point git at another repository or another
  helper. The removals listed below are what that allowlist guarantees in particular.

  **Authentication never falls back.** Every driver git call runs with
  `GIT_TERMINAL_PROMPT=0`, with `GIT_ASKPASS`, `SSH_ASKPASS` and every command-scope
  configuration variable (`GIT_CONFIG_PARAMETERS`, `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_*`,
  `GIT_CONFIG_VALUE_*`) removed from its environment, so an inherited header or URL rewrite
  cannot ride along, and with every tracing variable removed too (`GIT_TRACE*`,
  `GIT_CURL_VERBOSE`, `GIT_TRACE_REDACT`, and for `gh` `GH_DEBUG`), since curl tracing with
  redaction off writes the `Authorization` header, token included, to a log, and with `-c core.askPass= -c credential.interactive=false`, so git has no prompt or askpass
  program to try after the helper. When the minter fails or refuses, the helper answers
  `quit=1`, which ends git's credential lookup, and the call fails; the driver stops the
  run rather than retrying with anything else. Remotes are plain `https://` URLs (section
  5), so no SSH path exists to fall back to.

  **Git configuration is not trusted either.** A leaf can write the checkout's `.git/config`
  or the loop user's `~/.gitconfig` at any time after `doctor` ran, for example a
  `url.*.insteadOf` that redirects the push to another host. So **every** driver git call,
  authenticated or not (a commit runs hooks and trailer commands outside the leaf's
  namespace just as a push does), runs with `GIT_CONFIG_NOSYSTEM=1` and `GIT_CONFIG_GLOBAL`
  set to a root-owned, read-only file installed with the harness (a file the loop user
  could write would be no better than `~/.gitconfig`), with `HOME` pointed at a fresh empty directory created for that call and `NETRC`
  removed (git's HTTP layer reads `$HOME/.netrc` on its own, apart from any helper, so a
  `.netrc` a gate writes after `doctor` ran is never seen), pushes to an explicit `https://github.com/<owner>/<repo>.git` URL rather than a
  named remote, and is preceded by a check of the checkout's repository configuration (`.git/config`
  and any `config.worktree`) against an **allowlist**: only the keys a plain clone writes
  and the driver itself sets (`core.repositoryformatversion`, `core.filemode`, `core.bare`,
  `core.logallrefupdates`, `extensions.worktreeconfig`, `remote.<name>.url` and
  `.fetch`, `branch.<name>.remote` and `.merge`) are accepted, each only with its exact expected
  value (`remote.origin.url` the repository's `https://github.com/<owner>/<repo>.git`,
  `remote.origin.fetch` git's default refspec, `branch.<name>.remote` `origin` and
  `.merge` `refs/heads/<name>`, the `core.*` values a clone writes), and any other key or
  value fails the call. Driver fetches also name the URL and refspec explicitly, like
  pushes, so even a value the check accepted is not what steers them. A denylist would have to name every key that makes git run a program or contact a
  host (`trailer.*.cmd`, `filter.*`, `core.fsmonitor`, `url.*`, `http.*` and more), and
  would fail open on the first one it missed.

  A check made before a git call cannot protect that call if a leaf can rewrite the config
  in between. So the driver never runs git in a repository a leaf can write: it keeps its
  own clone, whose git directory lies outside every leaf's mount namespace, and takes a
  leaf's work into it as **files of data**: a leaf's output is today an uncommitted
  change that the publisher applies (`leaves.py`, `publish.py`), so the leaf hands over exactly one
  **patch file**, written inside its own namespace: it stages everything in its worktree
  (`git add -A`, which takes new untracked files too and leaves out ignored ones) and
  writes `git diff --cached --binary --no-ext-diff --no-textconv <base>`, so commits it
  made, staged and unstaged edits and new files all end up in one diff against the base it
  was given. No commits or refs cross over. The driver applies the patch with `git apply`
  in its own clone and requires the result to be exactly base plus patch: the tree it
  publishes is the one it built from that file, and nothing else the leaf produced can
  reach it. A fetch from the leaf's repository itself
  would not do, since the `git upload-pack` it starts reads that repository's
  configuration and can run programs from it (`core.alternateRefsCommand`, for one),
  outside the leaf's namespace; a patch is a plain file with no configuration. The configuration check above still runs on the driver's clone. It is not
  a boundary against the loop user itself: a gate, or a daemon a gate leaves behind, runs
  as that user, can rewrite the driver's clone and can reach the minter directly, which is
  why section 5 treats the loop user as possibly compromised and puts the boundary on
  GitHub's side. Against a leaf, which runs in its own namespace, the separate clone is the
  boundary. As defence in depth, every gate runs in a transient systemd scope that is
  stopped when the gate ends, so no process it started outlives it.
  Hooks are taken out of the checkout's hands too: a leaf that can write `.git/config` can
  also drop an executable into `.git/hooks/`, and the driver's git would run it outside the
  leaf's namespace, next to the minter. So every driver git call sets `core.hooksPath` to a
  root-owned directory holding only the hooks the driver runs (the secret guard's
  pre-commit, installed there from a reviewed copy), and the checkout's own hooks
  directory is never used. The helper is the last line: it runs with `credential.useHttpPath=true`
  and hands out a token only for host `github.com` and a path in the loop's repository
  allowlist, answering `quit=1` to anything else, so a redirect that slips past the check
  fails without the token leaving the machine. Every leaf starts with an environment scrubbed
  of GitHub and git credentials (`GH_TOKEN`, `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN`,
  `GITHUB_ENTERPRISE_TOKEN`, `GIT_ASKPASS`, `SSH_ASKPASS`, `GIT_CONFIG_*`, the minter
  socket's path), with `GH_CONFIG_DIR` pinned to an empty, read-only directory (so neither
  `~/.config/gh`, an `XDG_CONFIG_HOME` elsewhere nor a profile setting can supply a stored
  login) and `GIT_TERMINAL_PROMPT=0`, while
  the model provider's key it needs (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) stays, in its own PID namespace
  so it cannot read other processes' environments (section 5; ADR-0007's sandbox rules
  stand).
- **The human is the author and signs off; the bot commits.** A token only authenticates the
  push and the API calls; the commit object keeps whatever identity git is configured with.
  The person who runs the loop sets the design, scope and acceptance, answers for the output
  and certifies the Developer Certificate of Origin; a bot can do none of this and cannot hold
  copyright. So that person is the commit **author**, and the `Signed-off-by` trailer names
  the same person. Both come from a required `[publisher].signoff = "Name <email>"` in
  `pdca.toml`. The App's bot identity is the **committer** (`pdca-bot-<owner>[bot]`,
  `<bot-user-id>+pdca-bot-<owner>[bot]@users.noreply.github.com`), so the history still
  records which machine identity made and pushed each commit; GitHub shows these as
  "authored by <human>, committed by pdca-bot-<owner>". Every driver commit also carries an
  `Assisted-by: pdca-harness (<model>)` trailer that marks agent involvement without claiming
  authorship (not `Co-authored-by`, which would).

  The driver passes the identities per command with `git -c` and the `GIT_AUTHOR_*` and
  `GIT_COMMITTER_*` variables, never through the checkout's config. Because `git commit -s`
  and `git merge --signoff` take the trailer from the *committer*, which is now the bot, the
  driver stops using them and adds the trailers explicitly at every commit site: publish,
  records and revert commit with `git commit --trailer "Signed-off-by: <signoff>" --trailer
  "Assisted-by: ..."`, and the integration merges run `git merge --no-ff --no-commit`
  followed by that same `git commit` (`git merge` itself has no `--trailer` option; `--no-ff`
  keeps a fast-forward from leaving nothing to commit).

  One commit is made by GitHub, not the driver: the merge commit `gh pr merge` creates in
  `merge` mode with the default `merge_method = "merge"`. It carries no content of its own
  (its tree is the merge of two reviewed or checked parents) and no authorship, so it gets
  no sign-off, as common DCO checkers already exempt merge commits. The exemption is
  narrow and checked against an authenticated fact: the `dco` check and the auditor accept
  a commit without sign-off only when it is the `merge_commit_sha` GitHub reports for a
  merged pull request into that branch **and** has two parents (for a rebase merge,
  `merge_commit_sha` names the last rebased content commit, which needs its sign-off like
  any other); any other unsigned commit fails. `squash` would
  instead write a new commit under the PR author's name from the PR title and body, so
  `merge` mode accepts only `merge_method = "merge"` or `"rebase"`, and `repo-settings.py`
  refuses `squash`. The driver records the line's tip
  before the merge and, if either the merge or the commit step fails, runs `git merge --abort`
  and resets to that
  tip, so no half-made merge is left for the next fold.

  The same applies where `merge` mode brings a PR that fell behind up to date: today the
  driver calls `gh pr update-branch`, which makes GitHub write a merge commit with no human
  author or sign-off, and the `dco` check would reject it. A local merge of the base would
  not work either: when the base gained a workflow change, pushing that merge commit
  counts as a workflow edit, which the App's token may not make. The driver therefore
  **rebases** the PR branch onto the new base when the base gained no workflow change since
  the branch's merge base: each commit is re-created with its author, trailers and the bot
  as committer, and the driver pushes with `--force-with-lease` to the commit it rebased
  from. The driver namespaces allow force-pushes for this, as `pdca-integration/*` already
  does; the push dismisses stale approvals as usual. When the base **did** gain a workflow
  change, GitHub refuses even the rebased push to a token without the Workflows permission,
  so the driver does not try: it reports the PR as needing the maintainer, who refreshes it
  with GitHub's "Update branch" in rebase mode, which keeps each commit's author and
  `Signed-off-by` and so passes the `dco` check. A rebase that conflicts is aborted and the
  PR reported, never pushed half-done. The driver records the branch's tip before the
  rebase and the rebased tip after it, persists both in the instance state, and when the
  push fails or its result is unclear (a lease race, a network error, a ruleset refusal)
  it reads the remote head and acts on what it finds: the rebased tip means the push
  landed, so the local branch moves there and the state records success; the old tip
  means it did not, so the local branch is reset to it; anything else means another
  writer, so the run stops and reports the PR. Nothing touches the branch until that is
  settled. `gh pr update-branch` is no longer used.

  `doctor` fails when `signoff` is missing or names a bot (a name, or an email's local part,
  ending in `[bot]`; a human's private `<id>+<login>@users.noreply.github.com` address is
  fine and can match the author like any other). The template's `dco` check requires a `Signed-off-by`
  whose email matches the commit author's, the rule common DCO checkers apply, and rejects
  one that names a bot, so a commit signed off only by a bot cannot merge. Dependabot's PRs
  are the one exception: when the PR was opened by the Dependabot App (an authenticated
  fact, unlike the commit fields), the check accepts commits authored by `dependabot[bot]`
  with whatever committer and sign-off they carry (today `GitHub <noreply@github.com>` and
  `Signed-off-by: dependabot[bot] <support@github.com>`); the maintainer's merge is the
  human certification there. A bot sign-off is accepted in that case and in no other.
  The committer field
  and the `Assisted-by` trailer are **advisory**: git identities are unsigned strings that
  whoever pushes can set, so a stolen App token could write any name and drop the trailer.
  GitHub records authenticated *who opened the PR*, not who pushed each commit. The
  auditor's claim is therefore narrow: it flags a PR opened by the agent App whose commits
  lack the trailer or claim a committer other than the bot, with one exception: commits
  re-created by GitHub's "Update branch" in rebase mode (section 1) carry GitHub as
  committer, and the auditor accepts that only when the PR's timeline shows the maintainer
  updating the branch (an authenticated event) and the commits still carry their author,
  `Signed-off-by` and `Assisted-by`. A commit the App pushes to a PR
  someone else opened is not attributable from the PR data; the driver never pushes to a PR
  it did not open, and per-push attribution would need webhook or audit-log retention, which
  this proposal does not add. Commits carry the human's
  name without the human's signing key, so they stay unsigned; with GitHub's vigilant mode
  on they show as "Unverified", and the committer field and trailer are what mark them as the
  loop's. A ruleset that requires signed commits cannot be used on branches the loop pushes to.
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
permission of any kind. Through the secrets APIs its key reveals only the *names* of
secrets, never their values; but its `Actions: read` also reaches workflow logs and
artifacts, which can hold secret-derived data GitHub's masking misses, so a stolen auditor
key is treated as a confidentiality incident, not a harmless one (see the security impact):

| Permission | Auditor | Reads |
|---|---|---|
| Administration | read | immutable releases, the Actions workflow-permission settings, and repository self-hosted runners |
| Organization: Self-hosted runners | read | organization runners available to the repository (getwyrd, Ralphovi) |
| Pages | read | the Pages build type (`GET /repos/{repo}/pages`), for the Pages rule of section 4 |
| Pull requests | read | PR authors, commits, `merge_commit_sha` and timelines, for the DCO and attribution checks of section 1 |
| Organization: Members | read | the organization's owners (`GET /orgs/{org}/members?role=admin`), for the sole-owner check of section 6 |
| Secrets | read | names of repository secrets, and of organization secrets visible to the repository (`GET /repos/{repo}/actions/organization-secrets`) |
| Environments | read | names of environment secrets |
| Variables | read | repository Actions variables and organization variables visible to the repository, compared with the maintainer's allowlist |
| Dependabot secrets | read | names of the repository's Dependabot secrets, and of organization Dependabot secrets visible to it |
| Organization: Organization dependabot secrets | read | which organization Dependabot secrets are visible to the repository (getwyrd, Ralphovi) |
| Actions | read | environments and their protection rules; waiting runs, pending deployments and their approvals (the approval baseline of section 4) |
| Deployments | read | deployment history, compared with the approval baseline of section 4 |
| Contents | read | workflow files for the audit of section 4, tags and releases |
| Attestations | read | the provenance attestations of release assets (`gh attestation verify`) |
| Discussions | read | the discussions copy of section 4 |
| Metadata | read | mandatory; also rulesets |

### 3. Rulesets and code owners (template `repo-settings.py`)

Everything here works on GitHub's free plan; PDCA cannot assume a paid one. `main` in this
proposal means the instance's `[project].default_branch` (default `main`); `repo-settings.py`,
the environment policies and the `doctor` rows all take the name from there.

- **Public repositories only.** Branch protection and rulesets are enforced on public
  repositories on every plan, but not on private repositories of a free personal account. The
  App is therefore installed only where they are enforced; `doctor` fails an
  installation that includes a repository where they are not. A private repository's loop
  keeps publishing from the maintainer's own login in `stack` mode, as today.
- **`main` is reached only through a pull request.** It cannot be deleted or force-pushed
  (the ruleset carries deletion and non-fast-forward restrictions, needed because the
  configured base need not be GitHub's undeletable repository default). The same ruleset
  covers every other branch a brief may target (`Repo + branch target`, such as
  `release/1.x`): `repo-settings.py` takes them from `[publisher].target_branches`
  (default: only `main`), and the driver refuses a brief whose target is not listed there
  before writing anything. No bypass for the App, required status
  checks from `.github/required-checks.txt` with the strict policy (the PR must be up to date
  with its base branch, `main` or the listed target it merges into, so every check reruns
  on the current base), and **code-owner review** as the review rule, with
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
  | `stack` (default) | everything (`*`) | nothing: `main` and every listed target branch carry an update restriction whose only bypass actor is the maintainer (pull-request mode), so even an approved PR is merged by the maintainer, not the App |
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
  `**/CLAUDE*.md`, `**/GEMINI*.md`, `**/.claude/**`, `**/.codex/**`, `**/.gemini/**`, and
  the instruction and settings paths of every other family `families.py` configures, which
  `doctor` takes from that registry so a new family cannot be missed), and every document those prompts tell the agents to obey (`docs/**` and
  `PCDA/**` in an instance, which today covers `docs/INTEGRATION.md`, `docs/principles.md`,
  `docs/fork-discipline.md` and `PCDA/quality-cycle.md`). Without this, a merged PR could
  change the driver that holds the App key, switch off a gate or rewrite the agents'
  instructions before the maintainer sees it. Because prompts gain references over time,
  `doctor` follows references transitively: starting from every instruction file above and
  the files under `agents/**`, it collects every repository path they name, then
  every path *those* files name, until nothing new appears, and fails when any file in that
  closure is not covered by `CODEOWNERS`. `CODEOWNERS` itself, and every directory on its
  path, must be a regular tree entry, never a symlink: GitHub reads the file at its fixed
  path without following symlinks, so a symlinked `CODEOWNERS` would protect nothing while
  a `doctor` that followed it would believe otherwise. A path counts as named when it appears literally,
  as a Markdown link or a backticked repository path. Symlinks are followed too: a governed
  path that is a symlink, at any level of its directories, puts its resolved in-repository
  target into the closure, recursively, and `doctor` fails when that target is not covered
  by `CODEOWNERS` (a symlink pointing outside the repository fails outright), since
  otherwise a PR could change the governing content through the target without touching
  any governed path. Literal names are the only reference syntax
  instruction files may use. An indirect reference (a path held in a variable or an
  environment setting, or built at run time) cannot be followed, so instruction files must
  not use one: they are themselves governing files, so adding one needs the maintainer's
  review, and the review rubric rejects it. One already present when the App is installed
  is the maintainer's to find; section 4's known limits record this. In a
  target repository the set is its `.github/**`, `CODEOWNERS`, the same instruction files
  at any depth (`**/AGENTS*.md`, `**/CLAUDE*.md`, `**/GEMINI*.md`, `**/.claude/**`,
  `**/.codex/**`, `**/.gemini/**`) with the
  transitive closure of the files they name, and the implementation of its required checks:
  CI configuration, build and test-runner configuration and scripts, not the application
  source those checks build and test (that is the ordinary code `merge` mode merges).

  `doctor` and `repo-settings.py check` read `wave_mode` and **fail** when `CODEOWNERS` does
  not cover the set its mode requires; the driver re-reads `CODEOWNERS` from the PR's own base
  branch before every merge (GitHub applies the base branch's file, so `main` and each listed
  target branch are checked separately) and refuses to merge while it is short. Coverage is judged on the file GitHub actually
  uses, not on a local parse: the check takes the first of `.github/CODEOWNERS`,
  `CODEOWNERS` and `docs/CODEOWNERS` that exists on that branch (GitHub's order), requires
  it to be under GitHub's 3 MB limit, and requires
  `GET /repos/{repo}/codeowners/errors?ref=<branch>` (Contents: read) to report no
  errors, since GitHub ignores a file that is too large and any entry it cannot parse or
  whose owner it cannot resolve.

  `merge` mode trusts the agent with ordinary code by definition: a non-final wave can change
  an existing test's assertions and reach `main` without review, to be seen only when the
  maintainer reviews the final wave. That is the trade the maintainer opts into. Instances
  that cannot accept it stay in `stack` mode, or add `tests/**` to `merge` mode's governing
  set, so that any PR touching a test waits for the maintainer.
- **Driver branches**: a branch ruleset lets the App create and update only the namespaces
  the driver itself uses, and refuses every other branch name to it, except `main` and the
  listed target branches, which carry `main`'s pull-request ruleset instead (so `merge`
  mode can merge a non-final PR into `release/1.x` the same way as into `main`). That
  ruleset also restricts their **creation**, with the maintainer as the only bypass actor,
  because the pull-request rule governs updates, not a branch's first push; and
  `repo-settings.py` refuses to install the App while a listed target does not exist yet. `repo-settings.py`
  derives the list from the instance's configuration rather than hard-coding it: the prefixes
  of `[publisher].fix_branch_pattern` and `feature_branch_pattern` (defaults `fix/*`,
  `enhancement/*`; force-pushes allowed, for the rebase that refreshes a PR in section 1),
  `pdca-integration/*` for stacked batches (force-pushes allowed there, as
  `integrate.py` requires), `revert/*` for `pdca revert`, the bug track's branches, and
  `[records].branch` (default `records/*`) when records are published as pull requests, and
  `pdca-doctor/*` for the probe of section 6. Every configured pattern must start with a
  fixed, non-empty prefix ending in `/` (`fix/`, not `{id}-{slug}`) that overlaps neither
  `main` nor another protected name; `repo-settings.py` refuses to apply a ruleset otherwise,
  since an empty prefix would open every branch name to the App. A brief's `Onto branch`
  (stacking on an existing PR, #54) is published by the App only when that head lies in one
  of these namespaces on a repository the App is installed on **and** its open PR was
  opened by this App (the PR's author, an authenticated fact), so the App never pushes to a
  PR a human or another bot opened and the attribution rule of section 1 holds; any other head, such as
  `origin/feature/x`, a branch on a third remote or another author's PR, is refused by the driver before it
  writes anything, with a message to publish that fix from the maintainer's login, so the
  path fails closed instead of meeting a ruleset rejection mid-publish. `doctor` **fails** when a
  configured pattern falls outside the ruleset, so a renamed pattern blocks the run before a
  publish would fail.

  **Dependabot** keeps its branches because a bypass applies only to the ruleset that grants
  it: the general branch restriction excludes `dependabot/**`, and a separate ruleset covers
  exactly `dependabot/**` with the same creation and update restriction and one bypass actor
  besides the maintainer, the Dependabot App (an `Integration` with GitHub's fixed
  Dependabot App id). `dependabot/**` thus stays closed to the agent App, and `doctor`'s
  namespace check treats the two rulesets as one composition: every branch name outside the
  driver namespaces must be restricted by exactly one of them. Dependabot's PRs then reach `main` through
  the same pull-request rule as any other.
- **No push rulesets.** Rulesets that block file paths on push are not assumed. Instead:
  - Without the Workflows permission the App cannot change `.github/workflows/**`, so every
    guard whose logic is written inline in its workflow file is out of its reach as it is
    (in the project template: `docs-immutability`, `dco`, `require-linked-issue`).
    A fix that itself changes `.github/workflows/**` therefore cannot be published by the
    App at all. The driver detects that before writing anything (the patch touches a path
    under `.github/workflows/`), publishes nothing and records the fix as needing the
    maintainer, who publishes it from their own login in an attended run; no half-made
    branch or local publication state is left for the next fold.
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
  seconds before the workflow does, or publish the workflow's draft early, before all its
  assets are attached. The release API names only a release's creator (`author`), not who
  published it, so publication by the workflow cannot be proven; what the auditor checks
  instead is completeness and provenance. The release workflow builds every asset first and
  attests a manifest (`SHA256SUMS`) that lists them all, and the auditor fails on a release
  whose author is not `github-actions[bot]`, that lacks the attested manifest, whose assets
  differ from the manifest in either direction (the manifest itself excluded from that
  comparison, since it cannot list its own checksum; it needs its own provenance
  attestation instead), or that holds any asset
  without a provenance attestation from the release workflow at that tag, and consumers
  verify with `gh attestation verify --signer-workflow <release workflow>
  --source-ref refs/tags/<tag> --deny-self-hosted-runners`, which an asset the App supplied
  cannot pass. Path and ref alone do not prove the release went through the approved path:
  an old workflow with `actions: write` could dispatch the release workflow on an existing
  tag whose older copy of that workflow lacked the environment. So the release workflow is
  triggered by tag pushes only (no `workflow_dispatch`), and the auditor and the documented
  consumer check also require the attestation certificate's build trigger to be `push`,
  read from `gh attestation verify --format json`; only the maintainer can push a tag. A
  trigger alone is not enough either, since an old workflow with `actions: write` can
  re-run a historical tag-push run, which keeps its original event, ref and SHA. So the
  auditor also binds each attestation to an approved run: the certificate names the run
  and attempt that signed it, and that attempt must be one n8n recorded as approved by the
  maintainer in the `release` environment (section 4's approval poll); an attestation from
  any other attempt fails. Migration deletes the tags of releases it deletes, so no old tag
  is left whose historical run could be re-run. Because
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
  behind an environment). Every environment, whether it holds a secret, anchors an OIDC
  trust or deploys Pages, has a deployment policy that admits only `main` and the
  listed target branches (`[publisher].target_branches`, protected like `main`, section 3)
  or, for `release`, only `v*` tags, so a job on a driver branch gets nothing however its file is
  written. Every environment also **requires the maintainer's approval**, with no exception. Each environment also sets `prevent_self_review: false`, since the maintainer
  is the only reviewer and also the one who pushes tags and dispatches runs (with it on,
  such runs would wait for ever), and `can_admins_bypass: false`: by default GitHub lets an
  administrator start waiting jobs past the protection rules, which would release secrets
  and OIDC tokens with no approval recorded and no cumulative review. A deployment policy alone is not enough: any
  workflow token with `actions: write`, including one from an old commit on a driver branch,
  can re-run an earlier run whose ref was `main`, and the re-run passes the policy while
  executing that earlier commit's workflow. GitHub asks for approval again on every run
  and re-run of a job in a protected environment, and the request names the commit; the
  maintainer approves only runs on the current head of the line the run belongs to (`main`
  or a listed target branch) or on the tag being released.
  Approval is also the **review boundary** for what the job executes: a privileged job
  builds and runs the whole tree, not only its workflow file. In `stack` mode every commit on
  `main` and on the listed target branches passed the maintainer's review. In `merge` mode, App PRs before the final wave can
  merge unreviewed source, and those merges sit in the final PR's base, not its
  diff. So the maintainer approves an environment run only after a **cumulative** review of
  the run's line (`main`, the listed target branch it runs on, or the line a release tag
  maps to) from the last commit they approved for a privileged run on that line up to the
  commit asking for approval, and never on a head that holds App merges after it. The baseline must come
  from outside both the loop user, which may be compromised, and GitHub's run and
  deployment records, which an old workflow with `actions: write` or `deployments: write`
  can delete or inactivate. So the **n8n auditor keeps it in its own store** (an n8n data
  table, per repository and environment), which neither the loop user nor any GitHub token
  can write:

  - Baselines are kept **per line**: one for `main` and one for each listed target branch
    (`[publisher].target_branches`, section 3), per environment. A run on a branch uses
    that branch's baseline. A release tag's line is never guessed from ancestry, since one
    commit can be reachable from several lines: `[publisher].release_lines` maps tag
    patterns to lines (default: every `v*` tag to `main`; for example `v1.*` to
    `release/1.x`). A tag must match **exactly one** pattern; there is no precedence to
    get wrong, so the default `v*` entry has to be narrowed (for example to `v[2-9]*`) when a
    line takes `v1.*`. n8n uses that line's baseline. A tag that matches no pattern or more
    than one, or whose commit is not on its mapped line, gets no range and is flagged. Parallel release lines such as `release/1.x` and `release/2.x` therefore each
    advance on their own.
  - The maintainer sets each first baseline when the App is installed.
  - n8n polls for waiting runs every few minutes (`GET …/actions/runs?status=waiting` and
    `…/pending_deployments`, Actions: read), and for each one sends the range to review
    through ntfy: the exact command for a complete local diff
    (`git fetch` then `git diff <baseline> <sha>`), which is the review reference, and the
    `compare/<baseline>...<sha>` page only as a convenience. The web page caps a diff at 300
    files and truncates large files, so when the range exceeds what it renders, the notice
    says so and the maintainer reviews from the local diff only.
  - Approvals are tracked **per run attempt**. GitHub's approval history
    (`GET …/actions/runs/{id}/approvals`) keeps the run ID across re-runs and names no
    attempt, so n8n records the run's current `run_attempt` each time it sees the run
    waiting, and counts an approval for that attempt only when, between two polls, the run
    leaves the waiting state for that same attempt and the approval history gains a new
    entry by the maintainer whose `state` is `approved` for the `release` or line
    environment; a `rejected` entry leaves the baseline where it was. A re-run is a new attempt that waits, and needs approval, again.
  - Once n8n has recorded an attempt as approved by the maintainer this way, it advances
    that line's baseline to the commit. It only moves forward along the line's
    history: a commit that is not a descendant of the stored baseline is not taken.
  - Each poll also compares the store with GitHub's approval and deployment history. A
    recorded approval that GitHub no longer shows, or an approved run still listed that n8n
    never saw, is a **discontinuity**: n8n alerts, keeps its own baseline, and marks the environment so that
    every later ntfy notice for it says to withhold approval until the maintainer has looked
    and reset the baseline by hand. An approved run that completes and is deleted, together
    with its deployment, between two polls leaves no trace the list APIs show, so the check
    cannot see it; the baseline then simply does not advance, and the next notice shows a
    longer range than needed, never a shorter one. Detecting that case would need webhook or
    audit-log events kept outside GitHub, which this proposal does not add.
  The maintainer reviews against that range, never one `pdca` prints, so the review
  covers the whole range, not the final PR alone.
  This costs ADR-0017's issue triage its automatic run: it either waits for approval or
  moves to the driver side, where the loop already holds a model key; the pilot decides.
- **OIDC trusts environments, not refs.** Any job, on any branch, can request an OIDC token.
  So every cloud trust policy pins the subject to an environment
  in GitHub's **immutable** format, which carries the owner and repository IDs
  (`repo:<owner>@<owner-id>/<repo>@<repo-id>:environment:<name>`), so a renamed or
  re-created repository cannot reuse the name. Repositories created, renamed or transferred
  after 15.07.2026 use that format by default; older ones are opted in (repository or
  organization OIDC setting) by `repo-settings.py` before the App is installed. Policies
  may additionally require the `repository_owner_id` and `repository_id` claims, as
  ADR-0017's federation rule already does; never to a branch or to `ref:refs/heads/main`:
  `pull_request_target`, `issue_comment` and `workflow_run` jobs also carry `main` as their
  ref, even when the App caused the event. A token from a job outside a protected
  environment then matches no policy. These policies live with the cloud provider, out of
  the auditor's reach; `repo-settings.py` prints the immutable subject (and the two ID claims) the maintainer must pin, and
  the auditor checks the repository's OIDC subject template is the default, which includes
  the environment. Because GitHub cannot see them, installing the App requires the
  maintainer to record, per repository, every cloud that trusts it and that each of its
  policies matches only environment subjects (or that none trusts it); `pdca setup-identity`
  refuses to install without that record and `doctor` shows its date.
- **Privileged jobs run only pinned code**, and so does everything that feeds them. A
  privileged job can take same-run artifacts or `needs.*.outputs` from other jobs of its
  workflow, so the rule covers **every job of a workflow that defines an environment job**,
  not only the environment job itself: every action any of those jobs uses is
  pinned to a full commit SHA and every container image to a digest, since a tag such as
  `owner/action@v1` or `image:latest` can be moved after the maintainer reviewed and
  approved; the auditor fails on any other reference in **any job** of such a workflow. Code fetched
  at run time is pinned the same way: package installs in such a workflow use a lockfile
  with hashes (`pip install --require-hashes`, `npm ci`, `cargo --locked`), tools are
  downloaded only with a checksum the workflow states, and the auditor flags unpinned
  installs it recognises. What a pinned action or a tool fetches beyond that is not
  provable by a check and is the review's to catch; section 4's known limits record it as
  an accepted path for secret exfiltration.
- **Privileged jobs run only on events the maintainer controls.** A job in an environment
  may only be triggered by `push`, a tag push, `schedule` or `workflow_dispatch`, the last
  only without inputs and only when the maintainer started it: an old workflow with
  `actions: write` can dispatch a workflow on `main` too. The approval notice names the
  run's `triggering_actor`, and the maintainer approves a dispatched run only when that is
  themselves; the auditor fails on an environment job in a workflow whose
  `workflow_dispatch` declares inputs. Events
  such as `pull_request_target`, `issue_comment` or `workflow_run` report `main` as their
  ref even when the App caused them, and carry PR heads or event text the cumulative
  review never saw. The auditor fails on any workflow that defines an environment job and
  has another trigger, and every approval notice names the run's event so the maintainer
  refuses any other. ADR-0017's issue triage, triggered by `issues`, is the pilot decision
  above: it runs without an environment or moves to the driver side.
- **Privileged jobs build clean**, and so does every job of their workflow: a job that
  feeds an environment job through `needs` outputs or same-run artifacts could otherwise
  restore a poisoned cache or another run's artifact and pass it on as same-run data. So
  every rule in this item applies to each job of a workflow that defines an environment
  job, and the auditor checks each of them. A job in such a workflow neither restores nor saves an
  Actions cache (a cache it saved on `main` could carry secret-derived output, and driver
  branches can restore `main`'s caches) and
  downloads no artifact from another run: in `merge` mode an unprivileged run on `main` could
  have filled the cache or an artifact with agent-merged output. Two parts are enforced:
  every job of such a workflow declares `permissions:` with `actions: none` (neither `read` nor
  `write`, since write includes read; a job without its own `permissions:` block fails too,
  as it would inherit the workflow's), so its
  `GITHUB_TOKEN` cannot fetch another run's artifacts by any route (`gh run download`, the
  REST API, a custom action), and the auditor fails on a job whose effective Actions permission is anything else. The
  cache is not fenced that way, because cache restore uses the runner's own token whatever
  the job's permissions; there, and for downloads through a credential the job is handed,
  the rule is the maintainer's review. Every line such a job runs is on `main` or a release
  tag and inside the cumulative review that precedes approval, and that review rejects any
  cache restore, cache save or cross-run download in a privileged job. The auditor flags
  the known spellings (`actions/cache`, `actions/cache/restore`, `actions/cache/save`, a
  `setup-*` action with caching on, `download-artifact` with a `run-id`) as an aid; it cannot prove their absence, so section 4's known limits list it.
- **What the workflow token can still do.** The repository default for `GITHUB_TOKEN` is
  read-only, but a workflow file can raise it, and an old commit's file can too. Every
  writable scope is therefore accounted for:

  | Scope | Fenced by | Residual |
  |---|---|---|
  | `contents` | rulesets on `main`, tags and non-driver branches (no bypass for GitHub Actions) | driver branches, which the App can write anyway |
  | `pull-requests`, `issues` | "Allow GitHub Actions to create and approve pull requests" off; code-owner review | editing, closing and reopening issues and PRs, retargeting a PR's base, editing and deleting comments, labels, marking a draft ready; it cannot approve. In `stack` mode it cannot merge (only the maintainer can update `main`); in `merge` mode it can merge a PR that touches no governing file once the required checks pass, the same reach the App has there (known limits) |
  | `discussions` | nothing on GitHub's side (Discussions stay on: the project's discussions happen there) | editing or deleting discussions and comments; see known limits |
  | `id-token` | trust policies pinned to fenced environments | none |
  | `attestations` | consumers verify `--source-ref refs/tags/<tag>`, the signer workflow and a `push` trigger | an attestation from a branch run, which that check rejects; deleting a release's attestations, which the auditor reports as missing. Rebuilding may not reproduce the same bytes, so the release workflow has a second, approval-gated job that downloads the immutable release's existing assets, checks them against the release's attested `SHA256SUMS` manifest (whose own attestation must still exist) and attests those exact bytes again; if the manifest's attestation is gone too, the release is retired (marked in its notes, superseded by the next version) and recorded, and the auditor stops failing on it |
  | `deployments` | environment deployment policies | deployment records without access |
  | `pages` | Pages is published from a **branch** (`main`, `/docs` or another protected branch), not by an Actions deployment: for an Actions-based site the `github-pages` environment is optional, so an old workflow without it could deploy. The auditor fails on a repository whose Pages build type is `workflow`, unless the maintainer has recorded that repository as an accepted exception | for a recorded exception, a site deployment from an old workflow (known limit) |
  | `actions` | none available | known limit: disable workflows, cancel or delete runs and artifacts |
  | `statuses`, `checks` | none available | known limit: fake status checks |
  | `packages` | none available | known limit |
  | `security-events` | none available | known limit: upload or dismiss code-scanning results |
  | `artifact-metadata` | none available | known limit: create or alter artifact storage and deployment records; no PDCA check relies on them |
  | `code-quality` | none available | known limit: upload code-quality and coverage reports; no required check reads them |
  | `copilot-requests` | none available | known limit: spend Copilot requests billed to an organization, where its billing policy allows it; personal accounts are unaffected |

  This is GitHub's complete scope list as of 15.11.2026 (`actions` through
  `vulnerability-alerts`, whose only levels are `read` and `none`). A scope GitHub adds
  later is unfenced until this table and the auditor's manifest add it; the auditor's check
  manifest records the list it was written against.

- **No persistent self-hosted runners** are available to a repository the App is installed
  on, at repository or organization level. Enterprise-level runner groups are out of the
  auditor's reach (their API takes no App token), so the App is not installed in an
  organization that belongs to an enterprise account; `pdca setup-identity` asks the
  maintainer to confirm that, and the known limits record it. A job on such a runner runs App-controlled code
  on that host, and a runner group cannot be restricted by branch on a personal account.
  **GitHub-hosted runners only**: the runner APIs do not say whether a self-hosted runner
  registered as ephemeral, so the auditor could not tell an ephemeral one from a
  persistent one, and it fails on any self-hosted runner at all. Listing runners is not
  enough on its own, since a runner scale set (Actions Runner Controller) can idle at zero
  runners and appear only once a matching job is queued. For an organization the auditor
  therefore also lists the runner groups (`GET /orgs/{org}/actions/runner-groups`,
  Organization: Self-hosted runners read) and fails on any group whose settings admit the
  repository, through `allows_public_repositories` or its repository list, whether or
  not it has runners right now. A scale set registered to the repository itself shows in
  no group API, so `pdca setup-identity` asks the maintainer to confirm there is none, and
  the known limits record that.

**Known limits** (accepted; the pilot shows whether any needs more):

- **Fake status checks in `merge` mode.** A job with `statuses: write` or `checks: write`
  on an old commit can post a passing status for a required check's name, and GitHub
  attributes it to GitHub Actions just like the real check. In `stack` mode the maintainer
  reviews every PR, so this changes nothing. In `merge` mode it can let the driver merge an
  ordinary-code PR whose real checks failed, and together with `pull-requests: write` an old
  workflow can ready and merge such a PR itself, the final wave's included; governing files
  still wait for the maintainer. This is the same reach `merge` mode already gives the App,
  and the cumulative review before any privileged run (section 4) is what catches it.
- **Privileged runs' logs and artifacts are readable.** Any workflow that requests
  `actions: read`, an old one on a driver branch included, can download the logs and
  artifacts of an approved environment run. So privileged jobs upload no artifacts (the
  auditor flags `upload-artifact` in an environment job), register every credential they
  derive with `::add-mask::` before use, and print no secret-derived data; what masking
  misses in a log stays readable, and the review that precedes approval is where a job
  that leaks is caught.
- **Cache poisoning of privileged jobs.** Cache restore uses the runner's token, so no
  permission keeps an environment job from restoring a cache an unprivileged `main` run
  wrote. Only the maintainer's review of the job keeps it out; the auditor's pattern scan
  is an aid.
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
- **Discussions.** Discussions stay enabled because the project's discussions happen there,
  and no setting stops an old workflow from requesting `discussions: write`. The accepted
  residual is that such a run can edit or delete discussions and comments. It is detected,
  not prevented, and recovery is manual: the n8n auditor keeps a copy of every discussion
  and comment in its own store (Discussions: read), on the same few-minute schedule as the
  approval poll: each poll fetches the content of what changed since the last one (ordered
  by `updatedAt`) and lists the IDs of every discussion and comment, which it compares with
  its store, because a deletion leaves no `updatedAt` change or tombstone to find. Polling sees that an object vanished, not who removed it, so n8n
  alerts through ntfy on **every** deletion and on every edit whose last editor is
  `github-actions[bot]` or the agent App. The auditor is read-only, so it restores
  nothing itself: the alert carries the saved content and the maintainer reposts it. Content created or
  edited and then deleted between two polls was never copied and is lost; the window is
  the poll interval, and closing it would need webhook events kept outside GitHub.
- **The auditor can be switched off with its own key.** Any App's JWT can suspend or
  delete its own installation, so a stolen auditor key can stop the audit. Both sides fail
  closed on that: `doctor`'s installation check fails when the auditor installation is
  missing or suspended, and n8n alerts through ntfy whenever a run cannot mint its token or
  finish, so silence never reads as a pass.
- **Apps on a personal account are reviewed by hand.** No API lists the Apps a user has
  installed, so the rule that no other App holds write access to the repositories rests on
  the maintainer's review at setup (section 6).
- **Old personal tokens are revoked by hand.** A maintainer token left in a file the loop
  user can read would bypass every App boundary. No API lists a user's tokens, so their
  revocation at migration rests on the maintainer (section 5).
- **Run-time fetches in privileged workflows.** An action pinned by SHA, or a build tool,
  can still download code when it runs, beyond what lockfiles and stated checksums cover.
  A compromised upstream package reached that way runs with the job's secrets or OIDC
  token; only the review of the workflow and its dependencies keeps such a fetch out, and
  this is an accepted path for secret exfiltration.
- **App keys are counted by hand.** No API lists an App's private keys, so that the key
  user holds the only one rests on the maintainer comparing fingerprints on the App
  settings page at setup and at each rotation (section 1).
- **Repository-level runner scale sets** idle at zero runners are invisible to the runner
  APIs; that none exists rests on the maintainer's confirmation at setup.
- **Enterprise runners.** The auditor cannot see enterprise runner groups; the rule that
  the App is not installed under an enterprise account rests on the maintainer's
  confirmation.
- **Cloud-side OIDC policies.** Whether a cloud still trusts a ref-based or wildcard
  subject is checked only by the maintainer's recorded assertion (section 4), not by any
  automated row.
- **Indirect references in instruction files.** The governing-set closure follows only
  literal paths (section 3). An indirect reference already present when the App is
  installed, or one the maintainer misses in review, leaves its target outside
  `CODEOWNERS` without `doctor` noticing.
- **Event-data injection on `main`.** Whether a reviewed job treats event text only as data
  is a property of reviewed code; the auditor flags known patterns (head checkout, an
  event-controlled expression outside `env:`, `actions/github-script` fed event data) but
  cannot prove their absence.

### 5. The local side

As in client-rust-pdca, generalized: one OS user per unattended loop (`setup-loop-user.sh`
moves into the template), holding the pipx tools and the secret guard activated as that
user (the `doctor` secret-guard row, #959, reports it).

The loop user holds **no ambient GitHub credential**: no `gh` login (`~/.config/gh/hosts.yml`),
no `.git-credentials`, no credential helper or `http.*.extraHeader` in its global, system or
any checkout's repository git configuration, no `GH_TOKEN` in its profile, no `~/.netrc` or
`~/_netrc` (nor a `NETRC` variable pointing elsewhere), no SSH path
to GitHub at all: a key in a file the loop user can read anywhere, not only in `~/.ssh`,
would be found by no file check, so the host's firewall drops outgoing SSH (port 22, and
443 to `ssh.github.com`) for the loop user's UID, and `doctor` checks it by attempting the
connection and requiring it to fail at the network level, judged by the output, not the
exit status (a successful `ssh -T git@github.com` also exits 1). A relay on another port
could still carry SSH, so the firewall is not the boundary; GitHub's side is: no key a loop
user could hold has write access, because the auditor fails on any deploy key with write
(section 6), and no key on the maintainer's GitHub account is reachable
by the loop user. `pdca setup-identity` lists the account's SSH keys (`GET /user/keys`,
with the maintainer's login) and asks, for each, where its private half lives; it refuses
to continue until every key whose private half is on the loop host is either removed from
the account or kept in a home directory the loop user cannot read, and no agent socket of
the maintainer's is reachable from the loop user, both of which `doctor` re-checks as the
loop user (opening the maintainer's `~/.ssh` and any `SSH_AUTH_SOCK` must fail). Keys of
the old setup are removed from the account outright, no `url.*.insteadOf` or
`pushInsteadOf` rewrite, and every remote a plain `https://github.com/` URL with no
credential in it. Otherwise a leaf or a compromised gate could skip the App and use an
older, broader personal token or deploy key. Migration removes them (existing loop
users hold the ADR-0006 personal token today) and, because a copy could sit in any file
the loop user can read (a backup, an `.env`) where no file check finds it, **revokes**
them on GitHub: the ADR-0006 token and every other token that was ever stored on the loop
host are deleted in the maintainer's token settings, which `pdca setup-identity` walks the
maintainer through and records; no API lists a user's own tokens, so section 4's known
limits record that this is manual, and `doctor` fails on any it finds, checking
from a probe leaf and a probe gate as well as from the driver. For the driver it reads the
live environment of the running service (`/proc/<pid>/environ` of the unit), not only the
profile, and fails on any `GIT_CONFIG_*`, askpass, tracing or token variable there. Because `gh` can also keep a
login under `GH_CONFIG_DIR`, `XDG_CONFIG_HOME` or the system keyring, `doctor` checks the
effective store rather than one file: `gh auth status --no-refresh` (a plain `gh auth status`
refreshes an expiring token, which would extend the very login it is looking for), run as the loop user with its own
profile's environment, must report no login, and the loop user has no keyring daemon
holding one (no Secret Service item for `gh:github.com`).

The App keys do **not** live with the loop user. The model leaves run as that user, and a
sandbox that limits writes and network (Codex `workspace-write`, for one) does not hide
readable files, so a prompt-injected leaf could read a key in the loop user's home. The keys
belong to a separate OS user, **one per owner** (`<owner>-keys`), the App's only key
holder on the host, which runs a small token minter. Several loops of the same owner share
that one minter, each through its own socket (in a directory owned by the key user, group
that loop's own group with only its loop user in it, mode `0750`, the socket `0660`); the
minter checks the caller's UID with `SO_PEERCRED` against the loop that socket belongs to
and refuses any other, so one loop cannot draw tokens on another's allowlist. The driver
asks it over its loop's socket for an agent-App installation token and gets back only that token, valid one hour,
for repositories on the minter's own per-loop allowlist (a file owned by the key user, so
the caller cannot widen it); the minter logs every request. The minter is **not** one of the
loop user's pipx tools: it is installed root-owned outside the loop user's home (its own
virtual environment under `/opt`, its systemd unit and its interpreter included), so
nothing the loop user can write is ever executed as the key user. For `doctor` it also answers the
checks that need the App's JWT (`GET /app`, `GET /app/installations`) and returns their
results, never the JWT.

**What the key user protects, and what it does not.** The loop user runs code the target
repository controls: gates run its tests and builds, and git runs its hooks, outside any leaf
sandbox. So the loop user is treated as possibly compromised, and anything it can do counts
as something an attacker can do, including asking the minter for a one-hour token for the
allowlisted repositories. That token's reach is exactly the App's, which sections 3 and 4
fence on GitHub's side; those fences, not the hiding of the token, are the security
boundary. What the key user keeps out of reach is the **key**: a long-lived credential that
would otherwise outlast the compromise, mint tokens for every repository of the
installation, and could be used from anywhere.

As defence in depth, model leaves do not reach the minter at all: every leaf runs in its own
mount and PID namespace (bubblewrap, as Claude Code's sandbox already uses) in which the
socket's directory, the key user's files and the driver's processes do not exist. `doctor`
checks this by starting a probe leaf the way the driver starts real ones and failing if that
leaf can see the socket or any other process. Under the backends of the bug-track runner the same split holds (decision Q4): the
key sits with a minter running under its own identity, and the driver receives only
tokens from it. Attended runs from
the maintainer's account stay allowed and `doctor` keeps warning about them.

### 6. Proof in `doctor`, not trust

Each row reads what GitHub actually grants and fails when the answer is missing or
ambiguous; none infers a restriction from an error it did not provoke. The rows run before
every unattended run, so no proof is cached between runs.

**With the agent App's own token** (it already has Metadata: read, which covers rulesets):

- **Installations**: the key can mint tokens for every installation of its App, not only
  the one in use, so the row first lists `GET /app/installations` with the App's JWT and fails
  unless there is exactly one installation, on the expected owner, and confirms the App is
  private, because a public App with one installation today can gain another tomorrow. The
  App object carries no visibility field, so the row takes the slug from `GET /app` (JWT)
  and requires an **unauthenticated** `GET /apps/<slug>` to answer 404: GitHub serves a
  public App's page to anyone and a private App's only to authenticated callers. Any other
  answer, a network error included, fails the row. Each agent App is created
  as a private App by the owner it serves, so GitHub refuses installation anywhere else; the
  auditor Apps are checked the same way.
- **Identity and permissions**: the minter reads the installation record itself with the
  App's JWT (`GET /app/installations/{id}`), which shows the installation's full
  permission grant; a minted token would not, since GitHub reports the permissions of the
  token, and the minter's tokens always request section 2's set. For the repository list it
  mints a diagnostic token with no repository list and no permission list and enumerates
  `GET /installation/repositories`; it returns only the permissions and the repository
  list to `doctor`, never that token, which would reach every repository of the
  installation and bypass the per-loop allowlist. It fails unless the permissions are exactly section 2's
  set (no `workflows`, no `administration`, no `secrets`), the repositories are exactly
  the **owner-wide** set (the union of every loop's repositories for this owner, kept in a
  key-side file of the key user that the maintainer edits), and every one of them is
  public (section 3); a repository the App was
  installed on by mistake shows up here. The installation's `repository_selection` must be
  `selected`: an "All repositories" installation that happens to cover exactly the
  configured set today would gain every repository the owner creates tomorrow.
- **Minter allowlist**: the minter returns its effective per-loop allowlist, and the row
  fails unless it equals this loop's configured repositories exactly and is a subset of
  the owner-wide set; the installation covers every loop of the owner, but each loop's
  tokens reach only its own repositories; an entry for another
  loop's repository would otherwise let this loop mint tokens there, unseen by the
  installation check.
- **Rules, including tags**: `GET /repos/{repo}/rules/branches/{branch}` returns the effective
  rules for `main` and for a branch outside the allowed patterns, and
  `GET /repos/{repo}/rulesets?includes_parents=true` with `GET …/rulesets/{id}` returns every
  ruleset's target, enforcement and conditions. The row checks that section 3's branch and
  tag rulesets exist, are `active`, cover `main` and `refs/tags/**`, and carry **every rule**
  section 3 requires (pull request with code-owner review and stale-approval dismissal,
  required status checks on `main` and on every listed target branch, each with the strict
  "up to date with the base" policy, so a green result from an older checker cannot carry
  over after the base moves; creation, update
  and deletion restrictions on tags and on every branch that is neither a driver namespace,
  `main` nor a listed target branch; and for `main` and every listed target branch, in `stack` mode, the update restriction
  whose only bypass actor is the maintainer, while `merge` mode leaves them to their
  pull-request rule
  so its merges can land without a bypass), compared rule by rule with their parameters, since a ruleset with
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
- **Key files**: the agent and auditor keys are owned by the key user and unreadable to the
  loop user (`doctor`, running as the loop user, fails if it can open either), and nothing
  the minter's unit executes or imports (unit file, interpreter, virtual environment,
  `PYTHONPATH`, working directory) is writable by the loop user. Nor does the loop user
  hold anything root-equivalent, which would reach the key regardless of file modes:
  `doctor` fails when `sudo -n -l` lists any rule for it (a command-specific `NOPASSWD`
  rule, such as `cat` as the key user, is enough to read the key, so the user must have no
  sudo rule at all), when it belongs to `sudo`, `wheel`, `disk`, `docker`, `lxd`,
  `libvirt` or `incus` (or any group that owns a privileged daemon socket or a block
  device), when it can open any block device or the socket of a rootful Docker, Podman,
  LXD or libvirt daemon, when the host's setuid and setgid files differ from a
  root-owned manifest the maintainer records at setup (path, owner, group, mode and
  checksum of each; a package update re-records it through the same step) in any of those
  fields, or any set-ID file appears that the manifest lacks, a stray one or a
  `chmod u+s` on `/bin/bash` being as good as root (`dpkg --verify` alone checks content,
  not mode), when the host's polkit configuration differs from a
  root-owned manifest of approved files and checksums the maintainer records at setup:
  every authorization source polkit reads, the JavaScript rules (`/etc/polkit-1/rules.d`,
  `/usr/share/polkit-1/rules.d`), the action definitions whose defaults can allow without
  authentication (`/usr/share/polkit-1/actions/*.policy`) and, where the legacy backend is
  installed, `localauthority` (`*.pkla`), and an authorization source outside that list
  fails the check, since a rule is
  arbitrary JavaScript and no probe can show what it grants, or when an
  executable it can run carries any file capability outside a short allowlist (`cap_net_raw`, `cap_net_bind_service`): a list of dangerous
  ones would always miss one (`cap_setuid`, `cap_setfcap`, `cap_sys_module` each lead to
  root). Finally, nothing root or the key user runs may be writable by the loop user, and since
  no scan can prove what a job loads at run time, this rests on two checkable parts. The
  root host check lists every path the loop user can write (its UID and groups, outside
  `/proc` and `/sys`) and fails when that reaches anywhere outside its home, its loop's
  working directories and the shared temporary directories, so a group-writable
  `/usr/local/bin/backup` fails directly. And every job that runs as root or the key user
  (systemd units and timers, cron entries, login scripts) is on a root-owned manifest the
  maintainer records at setup with each definition's checksum, having reviewed that none
  reads from the loop user's directories; a new or changed one fails until the
  maintainer reviews and records it. The loop host is dedicated, so that list stays short.

`doctor` itself stays read-only, as it is today. The one check that has to write runs
separately, when an App is installed and whenever its permissions change, against a
scratch repository of the same owner (`pdca-probe`, part of the installation's configured
repositories) rather than a live one:

- **Workflow changes are refused** (`pdca setup-identity --probe`, not `doctor`): a probe pushes a commit that adds an inert workflow
  (triggered only by `workflow_dispatch`, with a single job guarded by `if: false`) to a
  fresh `pdca-doctor/probe-<random>` branch and expects GitHub to refuse it. The push is a
  conditional creation (`git push --force-with-lease=refs/heads/<branch>:`, an empty
  expected value meaning the ref must not exist), so if anyone else created the name in
  between, the push fails instead of moving their branch. The probe takes the instance's lane lock first and
  holds it until its cleanup is confirmed, so two probes, or a probe and a run, never
  journal or clean up at the same time; the journal holds a list of obligations, appended
  to and removed from one by one, never overwritten. Before any
  write, `git ls-remote` must show the branch absent (otherwise another name is drawn). The
  branch name and the probe commit's OID are then written to the instance state **before**
  the push and cleared only once `git ls-remote` shows the branch absent, so a crash at any
  point leaves an obligation that the next run resolves before it starts. Cleanup deletes the
  branch only while its remote OID equals the journaled probe commit, with a lease
  (`git push --force-with-lease=<ref>:<oid> --delete`); a branch at any other commit was not
  made by the probe and is reported for the maintainer, never deleted. If the push is
  accepted, the row fails, deletes the branch (the ruleset allows the App to delete
  `pdca-doctor/*`), retries the deletion up to three times and confirms with
  `git ls-remote` that the branch is gone. If it is still there, the row fails with the
  branch name to remove by hand, and records it in the instance state, so every later run refuses
  to start until it is gone. Any other response, a timeout or a lost reply
  included, fails as inconclusive and takes the same path: query the remote for the branch,
  delete it if it exists at the probe commit, and record it if it cannot be removed. Nothing is left behind on
  success.

**With the auditor App's token**, which never leaves the key side: the minter does not
mint auditor tokens for anyone. It runs these checks itself, as the key user, and returns
to `doctor` only a verdict per manifest check ID (pass, fail, inconclusive) and the names
of what failed, so a compromised loop user that talks to the socket gets no auditor token,
no logs, no artifacts and no general API access. The minter does not leave enforcement
to the caller either, since the loop user may be compromised and could skip `doctor`. It
refuses while the **host checks** are stale or failing: the key-file and root-equivalence
rows of section 5 need root to see everything (sudo rules, setuid files), so a root-owned
systemd timer runs them at least every hour and writes its verdict to a root-owned file
the minter reads, and the minter refuses tokens when that verdict fails or is older than
two hours. It also
runs these auditor checks, and the agent-token rows above (installation, permissions,
rules, `current_user_can_bypass`), itself when a run starts and again at least every
hour, and refuses to mint agent tokens while any of them fails, so a lost approval rule,
a self-hosted runner that appears or the App added as a bypass actor stops token issue, not only the `doctor` report. The n8n audit holds its own copy of the
auditor key. The checks: first the same identity check as for the agent App, an
installation record whose permissions must equal the auditor table of section 2 exactly (any write
permission fails) and whose repositories must be exactly the configured ones. Then:
the settings of sections 3 and 4: immutable releases are on, every release is immutable,
authored by `github-actions[bot]`, complete against its attested manifest and attested by
the release workflow (section 3), and every release tag (`v*`) has a release within an hour
of the time n8n **first saw** the tag. GitHub records no creation time for a ref and an
annotated tag's date is whatever the client wrote, so n8n keeps its own first-seen time
per tag from its few-minute poll. The minter and `doctor` need two pieces of that n8n
state (tags overdue for a release, and the run attempts n8n recorded as approved), which
GitHub cannot reconstruct and the loop user must not be trusted to relay. So the minter
fetches them itself from an n8n webhook that only it can call (a shared secret held by
the key user and in n8n), over TLS, as a document stamped with n8n's time and the
poll it came from; it refuses to mint when the fetch fails or the document is older than
two hours, and passes the verdicts on to `doctor`. With that state, the check is that
every `v*` tag n8n has listed as overdue has a release (other tags need none, and any
release the App put on one fails the author, manifest and attestation checks); no repository secret and no visible organization secret, for Actions and for Dependabot
alike: Dependabot's secrets reach any workflow run for a Dependabot PR (and its manual
re-runs), which in `merge` mode can run agent-merged source before any review, so private
registry credentials for Dependabot are not used on repositories the App is installed on;
and no credential in an Actions **variable**, since variables are readable by every
workflow run, an old one on a driver branch included: the auditor reads every repository,
visible organization and environment variable (Variables: read, and Environments: read for
`/repos/{repo}/environments/{name}/variables`) and fails on any that is not on an
allowlist the maintainer records (name and a hash of the value, each attested as not a
credential), so a new or changed variable fails until the maintainer has looked at it;
pattern matching alone would miss an opaque token or a plain password; every
environment, whether it holds a secret or not, has the deployment policy and, where
section 4 requires it, the approval rule, with `prevent_self_review` and `can_admins_bypass` false. The policy list API returns a policy's name but
not whether it matches branches or tags, so `repo-settings.py` records the IDs of the
policies it created (with their type) and the auditor fails when an environment's policy
IDs differ from that record: a `v*` branch policy put in place of the `v*` tag policy has
a new ID, and it also compares each policy's current name with the recorded pattern, since
a policy edited in place (`main` renamed to `*`) keeps its ID; the OIDC subject uses the immutable format and otherwise the default template; "Allow GitHub Actions to create
and approve pull requests" is off; no persistent self-hosted runner is available; and the
review aids of section 4 report nothing new. All workflow-content checks (triggers,
permissions, pins, caches, artifacts, the review aids) run on every ref an environment
admits: the heads of `main` and every listed target branch, and the commit of every
release tag (`v*`), released or not, since a release's re-attestation job or a re-run
executes that tag's workflow again; a divergent `release/1.x` could otherwise carry
a workflow `main` does not. Any failure blocks the run.

Turning immutability on protects only releases published afterwards; older ones keep
`immutable: false`, and `Contents: write` can still replace their assets. A digest snapshot
checked now and then cannot close that, since an asset can be swapped and restored between
two checks. So before the agent App is installed on a repository, every mutable release there
is either republished, which makes it immutable, or deleted, and the auditor fails on any
mutable release from then on. Releases older than the switch to tag-push releases cannot
meet the rest of the audit (their author is the maintainer, and they have no manifest or
provenance), whether they are mutable or already immutable. They are **grandfathered**:
`repo-settings.py` records their IDs and asset digests at installation, the author,
manifest and provenance checks apply only to releases not in that record, and the
auditor fails if a recorded release's assets or digests change or a recorded release is
mutable. Their tags are recorded too and exempt from the workflow-content checks, whose
rules their historical workflows predate and cannot be changed to meet; instead, every
approval notice for a run on a grandfathered tag says not to approve it, and the
re-attestation job never runs for a grandfathered release (it has no manifest to check
against). `repo-settings.py` lists the releases concerned when the
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
The run fails when that set differs from the manifest, on either side. The vectors and
the manifest live under one path (`audit/`) that is part of the governing set in both
modes and named explicitly in `CODEOWNERS`, which `doctor` verifies like any other
governed path; otherwise a `merge`-mode PR could merge a shortened manifest or weakened
verdicts, and n8n would validate itself against them.

`repo-settings.py check` with the maintainer's admin login remains for what only an admin
can see: the full `bypass_actors` lists. It fails unless every ruleset's only bypass actor
is the maintainer, with no
`Integration` (GitHub Actions or any App) and no `DeployKey`; the one exception is the
Dependabot App on the `dependabot/**` ruleset of section 3, and only there. A role
bypass (repository admin, or organization admin) is accepted only when the maintainer is
the only one holding it, since a role bypass covers everyone in the role.

The App is installed only on repositories where the maintainer is the **only human with
write access**: otherwise anyone with write could push to an App-opened PR's branch (the
driver namespaces cannot be made App-exclusive, as GitHub's restriction rules admit only
bypass actors) and have `merge` mode merge it under the App's name, and a custom
repository role with "Edit repository rules" could add GitHub Actions as a bypass actor
without being an admin. So this check, and the auditor on every run (collaborators are
readable with Metadata: read), list every collaborator with their permission and role name
and fail on anyone other than the maintainer with write, maintain, admin or any custom
role, and, for an organization, on any owner other than the maintainer. Pending
invitations count as granted: the check also lists the repository's invitations
(Administration: read) and the organization's (Members: read) and fails on any that would
grant write or more, or ownership, since an invitee can accept in the middle of a run.
Teams are excluded outright rather than inspected: a team's membership changes through
team invitations and team maintainers, out of the repository's view, so the check also
lists the repository's teams (`GET /repos/{repo}/teams`, Administration: read) and fails on any
team with write, maintain, admin or a custom role there; the maintainer's own access comes
from ownership, not a team.

Machine writers count too, since they are not collaborators. The auditor lists the
repository's deploy keys on every run (Administration: read) and fails on any with write
access. Other GitHub Apps are not visible to an installation token. For an organization,
`repo-settings.py check`, with the maintainer's login, lists every installation
(`GET /orgs/{org}/installations`) and fails on any App other than the agent App, the
auditor App and GitHub's own `github-actions` and Dependabot (built in; their reach is
what sections 3 and 4 fence) that holds **any organization-level** write or admin
permission, wherever it is installed, since repository selection does not limit
organization permissions (`Members: write` alone can add an owner). It also fails on any
such App with repository-level write or admin installed on all repositories. For one
with only repository-level write installed on selected repositories, the API does not
show which ones to the maintainer's login, so `pdca setup-identity` lists it and the
maintainer confirms on the organization's installed-Apps page that it does not cover the
repository, as for a personal account. The
rule is an allowlist of read-only grants,
not a list of dangerous ones, since combinations such as Organization secrets write with
Actions write (add the repository to a secret, re-run an old workflow) are as harmful as
Contents write. For a personal account GitHub offers no API that lists the Apps a user
installed, so the same rule is a step of `pdca setup-identity`: the maintainer reviews the
account's installed Apps (Settings, Applications) and confirms, and section 4's known
limits record that this is manual. Only the account owner can install an App, so between
these checks only the maintainer can add one, the same accepted residual as for bypass
actors. It runs when an App is first
installed, and `repo-settings.py apply` runs it after every ruleset change it makes;
`doctor` shows the date of the last pass. Only an admin can change a ruleset, so between
those checks only the maintainer can add a bypass actor; that residual is accepted.

The checks rest on two API facts confirmed in GitHub's REST documentation on 14.11.2026:
rulesets are readable with Metadata: read, while `bypass_actors` "is only returned if the
user making the API request has write access to the ruleset". The same documentation lists `current_user_can_bypass` in the ruleset response schema
(enum `always`, `pull_requests_only`, `never`, `exempt`; checked again on 15.11.2026)
and puts no such restriction on it; the pilot confirms that it is returned to the
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
   their checker closure from the base commit; the `dco` check requires a sign-off matching
   the author and rejects bot sign-offs, and every driver commit site authors and signs off as `[publisher].signoff` (instead of
   `-s`/`--signoff`), commits as the bot and adds the `Assisted-by` trailer;
   `setup-loop-user.sh` and the App-token minting move into the template; the `doctor` rows
   ship with tests against a stub API.
3. **Roll out** to the other instances through `copier update`, one owner at a time.
4. **Before acceptance**, the pilot records where ADR-0017's issue triage runs (approval
   per run in its environment, or on the driver side) and the proposal is updated with it.
5. An ADR records the decision; it supersedes ADR-0006's credential section in
   client-rust-pdca and ADR-0021 §4's deploy key.

## Security impact

This narrows what an agent-controlled process can change. It adds one untrusted input: the
minter's Unix socket, which accepts requests from the loop user (possibly compromised,
section 5) into a key-holding service under another OS user. The protocol is a fixed,
versioned schema with a small set of request types (mint for a listed repository, run the
named `doctor` checks), size and rate limits per request, repository names checked
against the allowlist before any API call, no path, command or URL taken from the request,
and tests for malformed and oversized requests; the minter answers anything else with a
refusal and logs it. Each App's
private key becomes a new high-value, long-lived secret: it can mint tokens for every
repository of its owner's installation (section 1), so it is held by a separate key user
behind a token minter that leaves cannot reach (section 5), rotated on a schedule and revoked
on exposure; one App per owner bounds a stolen key to one owner. The auditor App's key is
read-only and reveals configuration, secret names (not values), and through `Actions: read`
the repositories' workflow logs and artifacts, which can carry secret-derived data GitHub's
log masking misses, and through `Variables: read` the plaintext value of every repository
and visible organization variable, a credential stored there by mistake included, so it is handled with the same care as the agent key; it is held by the same key
user and, for the daily audit, in n8n. The threat models (`docs/design/architecture/14-threat-model.md`,
`.oss-scanner/threat_model.md`) get the credential boundary, the CI secret rule and the
protected paths in the same change that lands step 2.

What it does not change: prompt injection through issues, comments and model output stays
possible. A compromised loop user can obtain an App token for its allowlisted repositories
and so has the App's whole grant there, not only its branches: in `stack` mode that is
pushing driver branches and opening pull requests the maintainer reviews, editing,
closing and retargeting issues and pull requests, writing and deleting comments and labels,
editing or deleting releases the immutability rule and the auditor do
not protect, and pushing a branch whose old workflows run with their own `GITHUB_TOKEN`
scopes, which reach the residuals of section 4's table and known limits (Actions runs,
discussions, packages, commit statuses, code-scanning data). None of that reaches `main`,
a release tag, an environment secret or a cloud credential directly; what a privileged run
leaves behind is the exception, as such a workflow can request `actions: read` and read
that run's logs and artifacts (section 4's known limits). In `merge` mode it also
reaches "merge ordinary code into `main` or a listed target branch" for any PR that touches
no governing file, the final wave's included, since GitHub does not enforce wave position
(section 3);
governing files still wait for the maintainer. The threat-model update of step 2 lists
this reach in full.

## Backward compatibility

Instances keep working on a personal token or the maintainer's login until they switch; the
publisher takes the credential from configuration. Rulesets and code owners change what the
maintainer must review, not what the loop produces. Workflows that read a repository secret
on `pull_request` break, and moving the secret into an environment does not bring them back:
environments admit only `main`, the listed target branches and release tags and need
approval, so a PR check can never
reach one. The `doctor` row names these workflows first, and migration rewrites each before
the App is installed: a required check that needs a secret is split into an unprivileged
PR check and a post-merge job on `main` in an environment, or loses the secret; a required
check is never left depending on an environment.

## Decisions taken while drafting

- **`merge` mode and `AGENTS.md`** (decided 15.11.2026): `merge` mode works differently from
  the STOP discipline by nature, since its driver merges non-final waves itself, and it
  stays. Switching it on is therefore not a configuration change alone: whoever sets
  `wave_mode = "merge"` in an instance rewrites that instance's `AGENTS.md` for it, so
  that the written rules describe what the loop actually does. The harness repository's
  own STOP convention is unchanged; this proposal fences `merge` mode where it is used
  and does not introduce it. No check judges the rewrite, since whether prose matches the
  loop's behaviour is not something a tool can decide; `AGENTS.md` is a governing file, so
  the rewrite, like any change to it, waits for the maintainer's code-owner review.

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
- **App key location for the bug-track driver** (was open, revised with section 5): the key
  never goes into the job VM, and not to the driver either: as on a loop host, it is held by
  a minter running under its own identity, and the driver receives only repository-scoped,
  permission-scoped one-hour tokens from it. On a local host the minter is its own
  container (or systemd service) that alone can read the key as a Podman secret or systemd
  credential; on Google Cloud it runs under its own service account, the only one that can
  read the key's Secret Manager secret. Once a
  model-key gateway exists, a shared token broker that hands the driver short-lived
  installation tokens replaces both, so no backend holds the key. ADR-0021 is to be
  superseded by a backend-neutral decision (a disposable job VM on a dedicated host); this
  rule applies to whichever backend that decision allows.

## Open questions

None of design; question 4 was settled on 14.11.2026 (below, under decisions). One
placement is left to the pilot and must be recorded before acceptance (graduation step 4):
where ADR-0017's issue triage runs.
