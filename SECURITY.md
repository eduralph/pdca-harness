# Security policy

<!-- How to fill this in: say how mature the project is and what it promises (wyrd's first
     paragraph says it is pre-release and carries no security promise yet), name the tools
     that run today (Dependabot, secret scanning, any fuzzing or static analysis), and
     narrow the Scope section to what is and isn't a vulnerability for this project.
     Example: https://github.com/getwyrd/wyrd/blob/main/SECURITY.md -->

pdca-harness is early software. We still want to hear about vulnerabilities early,
including in the design itself.

The project relies on **coordinated disclosure** and free, continuous tooling: Dependabot
security updates, GitHub secret scanning and CodeQL code scanning run on every push, and
[OpenSSF Scorecard](https://scorecard.dev) rates the repository's practices weekly. GitHub
Actions are pinned to commit hashes. Reporters are credited, and
we will work with you on a disclosure timeline.

**Safe harbor.** We will not pursue or support legal action against researchers acting in
good faith under this policy: accessing only their own data, avoiding privacy violations,
service disruption and data destruction, and giving us reasonable time to fix the problem
before public disclosure.

## Reporting a vulnerability

Please report suspected vulnerabilities **privately**, not in a public issue or pull
request. Use **Report a vulnerability** on the repository's
[Security tab](https://github.com/eduralph/pdca-harness/security/advisories/new), or
email **policy@getwyrd.dev**, with:

- a description of the issue and its impact,
- the steps or proof of concept needed to reproduce it, and
- the affected version or commit.

We will acknowledge your report and keep you informed as we investigate.

## Scope

Until a format or interface is marked stable, behavioural changes to it are expected and
are not in themselves security issues.
