---
created: 15.10.2026
type: architecture
status: living
tags:
  - architecture
---
# 10–13. Quality, risks, glossary, and testing strategy

> Living document. Combines arc42 sections 10 (quality scenarios), 11 (risks and technical
> debt) and 12 (glossary), plus the testing strategy (section 13). Figures are from
> [proposal 0001](../proposals/accepted/0001-approach-review-2026-10.md) (2026-10-15). Open
> questions are marked **[OPEN]** inline.

## 10. Quality scenarios

| # | Goal | Scenario | Expected response |
|---|------|----------|-------------------|
| Q1 | Integrity of the accept | A leaf writes `- Outcome: accepted` into §9 | Refused; the bundle stays AWAITING_SIGNOFF. **[OPEN]** not enforced for the sign-off session yet (#710). |
| Q2 | Integrity of the accept | The human records `accept` while one §6 row is open | Refused by C6 with the open rows listed. |
| Q3 | Independent review | Builder and reviewer resolve to the same vendor | Refused by `doctor`, or a recorded exemption adds a §6 note. **[OPEN]** #710. |
| Q4 | Resumability | `^C` during a wave; the command is re-run | Every bundle resumes from its files; nothing is redone that has an artifact. |
| Q5 | Maintainer attention | A bundle reaches sign-off | Every §6 row needs a person; the ritual rate is reported. **[OPEN]** #712. |
| Q6 | Cost awareness | Act reviews a window of bundles | Tokens per bundle and per round are reported. **[OPEN]** #711, #718. |
| Q7 | Fail closed | A leaf exits 0 without its artifact | The run fails for that bundle and exits non-zero. **[OPEN]** #714, #693. |

## 11. Risks and technical debt

| # | Risk or debt | Evidence | Addressed by |
|---|---|---|---|
| R1 | Accept-path holes on the interactive path | Sign-off session can edit §9; vendor rule unchecked; builder not confined | #710 |
| R2 | Check produces ritual | `Confidence: medium` hardcoded (`assemble.py:622`); ~30% of 1,263 §6 rows can never be cleared by a leaf | #717, step 1 (#712) |
| R3 | No cost or time bound on leaves | `_invoke` passes no timeout (`leaves.py:680`); no token record anywhere | step 2 (#711) |
| R4 | Scope and weight | 28,046 driver lines; `leaves.py` 4,706; cyclic imports; two drivers | steps 2–4 |
| R5 | State lives in model-authored Markdown | Every accept-path reader parses prose; #527/#606/#634/#642 recur | step 1 (#712) |
| R6 | Size is the dominant cost and its controls are soft | Patches over 250 KB take a median of 8.5 rounds; a split child outgrew its parent | step 5 (#714) |
| R7 | Contain-and-degrade hides failures | `flow._isolate` swallows every exception; nine bundles recorded "0 findings" from a leaf that never started | step 5 (#714) |
| R8 | Narrow calibration base | Sizing weights from 86 bundles of one Rust project | autobook and nl-shell data |
| R9 | Instance patches drift from upstream | wyrd carries ~2,300 lines of local `src/` patches | step 5 `pdca_ext/` (#714) |

## 12. Glossary

| Term | Meaning |
|---|---|
| Beat | One of Plan, Do, Check, Act. |
| Bundle | One contribution's directory, `results/issue_<id>/`; its files are its state. |
| Leaf | A point where a model runs inside a beat (planner, builder, reviewer, sign-off, publisher, Act, advisory, sizer, splitter). |
| Brief | `brief.md`, the spec every later step is measured against. |
| §6 NEEDS-HUMAN | The checklist in `SUMMARY.md` of items only a person can clear. |
| §9 | The sign-off section of `SUMMARY.md` holding the decision. |
| C6 | The accept guard: no `accept` while a §6 row is open. |
| C4 | The per-fix red→green gate: the test fails without the fix and passes with it. |
| Disposition | The §9 outcome: accepted, merged-wider, iterated-to-Do, iterated-to-Plan, discontinued. |
| Wave | A set of bundles with no declared edges between them, run together. |
| Lane | A parallel worker running one bundle at a time. |
| Integration line | `pdca-integration/<base>-r<key>`, the branch stack mode folds accepted waves onto. |
| Carry | Putting an earlier run's finished prerequisite onto the line before wave 0. |
| Instance | A project rendered from the template. |
| Ritual rate | Share of §6 rows no leaf can clear (planned, #712). |

## 13. Testing strategy

- **Offline first.** Every leaf has a stub, so the whole state machine runs without a model;
  about 2,460 offline tests run in under five minutes.
- **Red before green.** A fix ships with a test that fails without it (ADR-0006); the same rule
  applies to harness changes made through pdca-pdca.
- **Real git, stubbed `gh`.** Integration-line, carry and merge behaviour is tested against
  real repositories with a stubbed `gh`.
- **Template round-trip.** The root suite renders the template and runs `copier update` from
  the previous release.
- **[OPEN]** Most tests are coupled to module shapes rather than behaviour (about 122
  `subprocess`/`_invoke` patches); step 2 moves them onto one fake runner, and step 1 adds
  golden-file tests against frozen bundles.
