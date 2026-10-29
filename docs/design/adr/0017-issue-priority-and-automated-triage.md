---
created: 23.10.2026
type: adr
status: Proposed
tags:
  - adr
  - governance
  - triage
---
# 0017. Issue priority and automated triage: severity and reach set P0–P3, and a GitHub Action applies it to this repository's issues with one Claude API call per issue

## Context

pdca-harness has over 150 open issues, from the maintainer, from the instances that run it (Stemma, Wyrd, nl-shell, Autobook) and from review findings deferred out of pull requests. An issue gets `bug`, `enhancement` or `documentation`, and the `new` label while it has no milestone (the Issue triage workflow). Nothing records how urgent it is, so ordering the backlog means rereading issues one by one.

Triage needs the rules written down, so that two people, or the same person on different days, put the same issue at the same priority, and so that a bot can apply them with the maintainer making the final call. The structure is the one Stemma, Wyrd and nl-shell already use: severity and reach give a priority from a fixed table, and a short list of overrides moves some issues up. The severity meanings, areas and overrides below are the harness's own, taken from what it promises.

Four things shape the scheme and how it runs.

- **What the harness promises.** Its value is the accept path: no model writes the decision and `accept` is refused while a NEEDS-HUMAN item is open (ADR-0004); the reviewer is decorrelated from the builder (ADR-0005); only red-before / green-after evidence may block a sign-off, and a check that cannot run becomes a NEEDS-HUMAN item, not a pass (ADR-0006); bundle state is what the files say (ADR-0003); instances take changes by `copier update` (ADR-0007). A bug that lets an unreviewed or unevidenced change be signed off breaks the reason the harness exists, even when it needs an unusual bundle to show. Architecture §8.10 and risk R1 already list such holes.
- **What a hostile input can reach.** Leaves read tracker text that anyone can write and run with `Edit`, `Write` and `Bash` (§8.10). An attack through that text is rare by construction and still what the sandboxes and guards are built against.
- **The repository is public.** Anyone can open an issue, so whatever reads issues reads text written by strangers, and whatever an issue triggers, a stranger can trigger. Issue text is already public, so sending it to a model provider exposes little that was not already exposed. But a report can carry a token pasted from a log, or describe a vulnerability that should not be public. The repository has no SECURITY.md and private vulnerability reporting is off.
- **Whoever sets the milestone starts the work.** The harness's own PDCA instance picks up issues on the open milestone, so the milestone must stay a human decision.

## Decision

We will qualify every issue with three labels, derive a priority from two of them with a fixed table, and apply a short list of overrides (sections 1–3). Section 4 says what an automated triager may and may not do, and section 5 how ours runs.

### 1. Labels describing the issue

**Severity: how bad it is when it happens**

| Label | Meaning |
|---|---|
| `sev:critical` | A model writes or changes §9, or `accept` succeeds while a NEEDS-HUMAN item is open (ADR-0004); a change can be signed off without red-green evidence, or a gate reports a pass it did not run (ADR-0006); a leaf merges, approves or marks a pull request ready, or escapes its sandbox or worktree to write the target project; `copier update` or a render silently drops or overwrites an instance's own changes (ADR-0007); tracker or leaf text reaches code execution outside a leaf |
| `sev:major` | Bundle state is misread or a bundle is lost (ADR-0003); the reviewer sees `build-notes.md` or runs on the builder's vendor without a §6 note (ADR-0005); a guard or STOP-discipline check is off or bypassable without harm yet; `copier update` or a render fails for an instance; the driver crashes or a cycle cannot advance with no workaround; a check that cannot run is recorded as a fail or hidden instead of becoming a NEEDS-HUMAN item |
| `sev:minor` | Wrong behaviour with a workaround, a cost, time or size regression, or misleading output, logs or SUMMARY text |
| `sev:trivial` | Cosmetic, wording, docs, or a test-only gap |

**Reach: how likely someone hits it**

| Label | Meaning |
|---|---|
| `reach:common` | A normal cycle with a default or documented configuration triggers it |
| `reach:edge` | Needs an unusual configuration, target project, bundle shape, model behaviour or timing |
| `reach:adversarial` | Only triggers when someone sets it up on purpose: hostile issue or tracker text, a pull request crafted to fool a gate, a leaf deliberately acting against its role |

**Area: where the issue lives** (for routing and assignment)

Following the target layers of the building block view (architecture §5.1, §5.2): `area:records`, `area:machine`, `area:leaves`, `area:gates`, `area:publish` (worktrees, draft PRs, tracker sources), `area:roles`, `area:policies`, `area:orchestration`, `area:act`, `area:cli` (the `pdca` command, configuration, doctor, the guard), `area:template` (Copier and `copier update`), `area:spec` (the vendored quality-cycle model), plus `area:docs`, `area:tests` and `area:ci`. An issue gets one or two. The areas follow the target layers rather than today's modules, so they survive the strangler migration (ADR-0016).

### 2. Priority from severity and reach

| | common | edge | adversarial |
|---|---|---|---|
| **critical** | P0 | P1 | P1 |
| **major** | P1 | P2 | P2 |
| **minor** | P2 | P3 | P3 |
| **trivial** | P3 | P3 | P3 |

- **P0:** drop other work, fix now, block the release.
- **P1:** goes on the current milestone.
- **P2:** next milestone, or whenever it fits.
- **P3:** backlog.

The labels are `priority:P0` to `priority:P3`, the same names as in Stemma and Wyrd, so a priority means the same in the harness and in the projects it runs on. P1 and P2 line up roughly with Codex's review badges.

### 3. Overrides

- **Security bugs get at least P1, even when `reach:adversarial`.** A leaf steered by hostile tracker text, escaping its sandbox or tools, or exposing a credential runs on other people's projects.
- **Decision-integrity bugs get at least P1, even when `reach:edge`.** Anything that lets a change be signed off or published without the human decision or the red-green evidence: a model writing §9, the C6 guard bypassed, a gate reporting a pass it did not earn, a leaf merging or marking a pull request ready. The harness exists to make those impossible, so a rare case still undermines every sign-off.
- **Deferred review findings** keep the review's priority, unless the triager can show the pull request changed the impact.
- **When unsure, leave the priority off.** If the triager cannot place severity or reach, they set `needs-triage` and say what is missing. Never guess a lower priority.

There is no `release-gating` override: the repository has no such label. If one is introduced, it should mean at least P1, as in Wyrd and Stemma.

### 4. Automated triage

A bot that triages does this on each new issue:

1. Set type (`bug`, `enhancement` or `documentation`) unless one is already set, plus severity, reach, area and priority. When the issue already has a type and reads as a different one, the bot keeps the label and says so in its comment.
2. Post one comment with a line of reasoning per label, so the maintainer can check the call quickly and correct it by changing a label.
3. Assign from a fixed owner table by area, unless someone is already assigned.
4. Leave the milestone empty. The `new` label therefore stays until the maintainer sets one.

Issue text is untrusted input. The bot's job MUST hold only `contents: read`, `issues: write` and `id-token: write`; it gets no write access to code and no secret beyond its own short-lived tokens. GitHub has no narrower permission than `issues: write`, and that one allows more than the bot uses: it can also edit an issue's title and body, close, reopen or lock it, set its milestone, and edit or delete comments. The bot's code calls only the label, assignee and comment endpoints, with arguments it builds itself, so the model's answer cannot reach the rest. The wider capability is reachable only by compromising the job's code or its dependencies, which are hash-pinned (section 5). Such a compromise could rewrite, close or re-milestone issues, which shows in each issue's history and can be undone; it cannot touch code, releases or secrets. A milestone set this way would start the PDCA instance on that issue, behind the cycle's own sign-off. What the model returns MUST be validated against the label sets before anything is written, and the priority MUST be computed by code from sections 2 and 3, not taken from the model.

The bot MUST NOT quote the issue in its comment: no log lines, paths, commands, identifiers or tokens, only descriptions in general terms. When an issue appears to contain a credential or other secret, the comment SHOULD say so and ask the reporter to rotate it and edit it out. When an issue appears to report an undisclosed vulnerability, the bot still sets the labels, but its comment MUST NOT restate the details; it asks the reporter to add nothing further and says a maintainer will follow up privately.

The bot skips pull requests, closed issues, issues opened by bots, and issues that already carry a priority.

### 5. How the bot runs

We will run triage as a GitHub Action, `.github/workflows/triage-priority.yml`, with a Python script, `.github/triage/triage_priority.py`. It runs when an issue is opened, reopened or edited and has no `priority:*` label, and on demand to back-fill open issues.

The names say *priority* on purpose. The driver already has a `triage` command, `template/src/pdca_harness/triage.py`, which sorts pull-request review findings into the ledger. The two share nothing: this bot is repository CI tooling for this repository's issues and is not rendered into instances.

- **ADR-0008 does not apply to it.** The stdlib-only rule binds the driver rendered into every instance (`template/src/pdca_harness/`). This script runs only in this repository's CI, so it may use the Anthropic SDK, pinned and hash-checked as below.
- **One request per issue.** Claude Sonnet 5.5 at low effort, with no tools, structured JSON output, and the issue title and description capped at fixed lengths; the comment says when it cut the text. The static rules are sent as a cached system prompt. An issue is classified once; an edit triggers a new call only while it carries `needs-triage`.
- **The model classifies; the script decides.** The model returns type, severity, reach, area, the override flags, the secret and vulnerability flags, and one reason per label. Each reason field in the output schema says what it should explain. The script validates the answer, computes the priority, and applies labels, assignee and comment with the workflow's own `GITHUB_TOKEN`; `id-token: write` lets the job fetch its identity token and nothing more. A reason that repeats from the issue a run of five words, a number of three or more digits, or any single token that looks like an identifier (eight characters or more, containing a digit, one of `. / _ : = @ +`, or two hyphens, such as a token, hostname, path or repository name; a word with one hyphen does not count) is withheld whole. One under three words is shown as "(no reason given)", and mentions and markup are neutralised.
- **A decline or a malformed answer sets `needs-triage`** and a short comment; nothing is guessed. A safety-classifier decline is first retried server-side on Anthropic's recommended fallback model.
- **No stored API key.** The job authenticates by Workload Identity Federation: it fetches GitHub's OIDC identity token (audience `https://api.anthropic.com`) and the Anthropic SDK exchanges it for a Claude API token that acts as a service account. The federation rule accepts only subject `repo:eduralph/pdca-harness:ref:refs/heads/main` and requires the claims `repository_owner_id: 15236434`, `repository_id: 1259114053` and `ref: refs/heads/main`. The numeric IDs matter because the default subject format names the repository, and a renamed or re-created repository could otherwise reuse the name. That covers issue events and manual runs from main, and no pull request, fork or other branch. The rule grants `workspace:developer` for 600 seconds; GitHub's token can be exchanged once, so a run must finish within about eight minutes. The service account and workspace may be shared with the other repositories that run this bot, but each repository has its own rule. The rule, organisation, service-account and workspace IDs are repository variables, not secrets, and the workflow does nothing until they are set. A manual run with `check` ticked makes one 16-token request to test the sign-in.
- **Cost is capped outside the bot as well as inside it.** Anyone can open an issue on a public repository, so the service account acts in a workspace with its own spending limit; that limit, not the script, is the backstop against a flood of issues. Each comment states its token use.
- **Dependencies are pinned and hash-checked.** The SDK and everything it pulls in are compiled with hashes and installed with `--require-hashes`, each version at least 7 days old when chosen.
- **The label set lives in `.github/triage/labels.json`**, with descriptions under GitHub's 100-character limit, and the workflow creates missing labels.
- **Back-fill is batched and checked first.** The open issues are triaged by manual runs in batches with a limit, the first as a dry run that prints each plan without writing.

We reject running a model on a subscription in the Action: its credential is a whole personal account, exposed to every issue author's prompt injection. We reject a stored `ANTHROPIC_API_KEY` secret: it lasts until someone revokes it, while a federated token expires within minutes and works only from this repository's main branch. We reject shipping the bot in the template: instances are PDCA working repositories, and the projects they serve run their own copy (as Stemma and Wyrd do); this repository is the canonical copy they take it from.

### 6. Known risks run through it

As a check of the scheme rather than real triage, here are holes from architecture §8.10 and §11, and ordinary issues, classified as if each had been reported as a bug:

| Issue | Severity / reach | Priority |
|---|---|---|
| The sign-off session can edit §9 (R1) | critical / edge, plus the decision-integrity override | P1 |
| A leaf that never started is recorded as "0 findings" (R7) | critical / common | P0 |
| The claude builder is not confined to its worktree (R1) | critical / edge, plus the security override | P1 |
| Hostile issue text steers the builder into running a command outside its tools | critical / adversarial, plus the security override | P1 |
| Nothing checks that the reviewer's vendor differs from the builder's (R1) | major / edge, plus the decision-integrity override | P1 |
| `copier update` from the previous release fails for an instance with local patches | major / edge | P2 |
| `Confidence: medium` is hard-coded in every SUMMARY (R2) | minor / common | P2 |

## Consequences

Every issue carries a priority that says why it has it, and the backlog can be sorted by label instead of by rereading issues. Manual and automated triage use the same rules, so either can check the other, and a priority means the same in the harness and in the projects it runs on.

The decision-integrity and security overrides move more issues up than any other rule. They are chosen deliberately: an accept-path hole that needs an odd bundle to show competes with common crashes for the current milestone. Leaving the milestone to the maintainer keeps the harness's own PDCA instance from starting work on the bot's judgement alone, at the cost of one manual step per issue.

Every issue's title and description is sent to Anthropic's API. For a public repository that adds little exposure; what it does add is any secret a reporter pasted, which the bot asks them to rotate. A vulnerability reported in a public issue is labelled but not restated.

The rules exist twice, in this ADR and in `.github/triage/prompt.md`, and the table and overrides a third time in the script. A change to this ADR has to update both files; the script's tests pin the table, so a drift shows up as a failing test. The same script runs in Stemma and Wyrd with their own rules and labels; a fix to the shared parts should reach all copies.

On Stemma the bot used about 2,800 to 3,100 input and 140 to 290 output tokens per issue, much of the input read from the cache. This prompt is similar in size, so back-filling the open issues should cost a few dollars; that is an estimate until the first batch reports its use. A comment can lack a reason for a label or two; the labels are unaffected.

The bot's trust boundary is recorded here (section 4) rather than in architecture §8.10, which describes the harness that instances run; the bot is tooling of this repository only.

Follow-on work: create the federation rule and repository variables, run the connection check, back-fill the open issues in batches, and review the first comments before relying on the bot.

Left open:

- **[OPEN]** The repository has no private channel for vulnerability reports: no SECURITY.md, and GitHub's private vulnerability reporting is off. Until one exists, the bot's comment on a suspected vulnerability can only promise a private follow-up.
- **[OPEN]** Whether P3 issues ever close automatically, or stay open until the maintainer closes them.
