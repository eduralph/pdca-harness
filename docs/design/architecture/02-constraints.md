---
created: 15.10.2026
type: architecture
status: living
tags:
  - architecture
---
# 2. Constraints

> Living document. What the design must work within; the decisions that follow from these are
> the ADRs. Open questions are marked **[OPEN]** inline.

## 2.1 Technical constraints

| Constraint | Source |
|---|---|
| Python ≥ 3.11, standard library only for the driver | ADR-0008 |
| Distributed as a Copier template; instances update with `copier update` | ADR-0007 |
| Models are reached only through their CLIs (`claude`, `codex`, optionally `gemini`) as subprocesses | `families.py` |
| Tracker and PR operations go through `gh`; git operations through `git` | `doctor.py` checks both |
| Sandboxed leaves need an OS sandbox (bubblewrap) and memory caps via `systemd-run` on Linux | `doctor.py`, #551 |
| A rendered instance's `main` may be protected; the driver must not need to push to it | `pdca record` pr mode |

## 2.2 Organizational constraints

- One maintainer signs off every bundle; their attention is the scarce resource.
- Apache-2.0 with DCO sign-off on every commit (ADR-0002).
- The harness is developed with itself through the pdca-pdca instance, so a broken driver
  blocks its own fix. Changes therefore land as small strangler steps (ADR-0016).

## 2.3 Conventions

- Bundle state lives in files under `results/issue_<id>/` (ADR-0003).
- Every claim in a brief, review or design doc cites `path:line` on a stated commit.
- One logical change per PR; every change ships with a test or a stated reason why none is
  possible (`CONTRIBUTING.md`).
- Diagrams are Mermaid source; no Obsidian `[[wikilinks]]` in `docs/` (checked by
  `docs/publishing/tools/lint_docs.py`).
