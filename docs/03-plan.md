# Step 03 — Plan: author the brief

← [02 Rehearse offline](02-rehearse-offline.md) · [Index](README.md) · next: [04 Do →](04-do.md)

**Beat: Plan.** Human touch point #1. The output is one artifact — `brief.md` —
the contribution spec the rest of the cycle is measured against. A bundle with no
`brief.md` is `UNPLANNED`; once the brief exists it's `PLANNED` and ready for Do.

## How Plan runs

The Plan leaf is **interactive** — it opens Claude in your terminal so you and the
planner co-author the brief from a tracker issue. gramps configured it to read a
single Mantis CSV row for the issue id:

```bash
make flow ID=11589        # opens the planner; you converge on a brief, then the
                          # driver continues unattended into Do + Check
```

Under the hood that's the `planner` leaf from [step 01](01-render-and-integrate.md)
writing `results/issue_11589/brief.md`. You can also seed a brief directly
(`pdca init-issue 11589 --from-brief path/to/brief.md`) when you've written it by
hand.

### Planning a specific id list in one session

`pdca flow --from-csv` briefs from a tracker CSV (the planner chooses the issues);
`pdca batch <ids>` drives an explicit id list but, by default, refuses an un-briefed
one. To **plan + drive a specific set** — e.g. bundles you seeded from per-bundle
triage notes, not the CSV — add the Plan pre-pass:

```bash
make batch IDS="11589 12030 12044" PLAN=1 LANES=6   # or: pdca batch <ids> --plan --lanes 6
```

`--plan` briefs every UNPLANNED id in the list in **one shared interactive session**,
then drives exactly those ids through Do+Check. Ids the planner chooses to skip are
left alone; `--from-csv` overrides the Plan source.

Each bundle's tracker thread can be fetched automatically: set `[tracker].notes_cmd`
in `pdca.toml` to your scrape tooling (a `{id}` shell template that writes
`issue_<id>/notes.json`; `$PDCA_BUNDLE` is the bundle dir). The harness runs it before
any Plan beat when `notes.json` is absent, so the planner has the comment thread
without you scraping by hand. It's best-effort — a failure just falls back to the
CSV / asking you.

## What a real brief looks like

This is the **actual** brief the planner produced for gramps issue 11589, a
PluginManager bug. Read it as a checklist of what a good spec contains — every
field earns its place downstream.

```markdown
# Brief — issue 11589 / pluginmanager-uninstall-destroys-shared-dir

- **Slug:** pluginmanager-uninstall-destroys-shared-dir
- **Defect:** In the enhanced Plugin Manager addon, uninstalling a single add-on
  filter rule deletes the **entire** shared `FilterRules` plugin directory — all
  the other rules in the pack *and* any unrelated user content the user placed
  there. Root cause: `__uninstall` calls `shutil.rmtree(pdata.fpath)` on the
  plugin's *directory* (`PluginManager.py:349`), but `pdata.fpath` is the
  directory shared by every plugin in a multi-plugin pack ...
- **Success criterion:** Uninstalling one plugin whose directory is shared by
  other registered plugins removes only the files belonging to the selected
  plugin; the sibling plugins' files and any unrelated files/sub-folders survive,
  and the directory itself is **not** removed while other registered plugins still
  live in it. (Uninstalling a plugin that is the sole occupant of its own
  directory must still remove the directory, preserving today's behaviour.)
- **Repo + branch target:** addons-source @ `maintenance/gramps60` (addons
  production branch per INTEGRATION §2; maintainer cherry-picks forward to gramps61).
- **Scope:** Make `PluginManager.__uninstall` (`...PluginManager.py:339-358`)
  non-destructive for shared directories ... /
  **out of scope:** repackaging the FilterRules pack; the reporter's secondary
  observation; the core plugin manager; changing the install path.
- **Repro instruction:** With the FilterRules pack installed ... select one rule
  and click Uninstall. Observed: the whole directory is deleted. Expected: only
  the selected rule's files are removed.
- **Test file:** `../addons-source/PluginManager/tests/test_uninstall_shared_dir.py`
  (addon convention per INTEGRATION §3 — `tests/` package, `test_*.py` prefix).
```

## Why each field is there

A brief is not free-form. Each labelled field is consumed by a later beat, which
is *why* the harness can automate Do and Check at all:

| Field | Consumed by | What would go wrong without it |
|-------|-------------|--------------------------------|
| **Defect** + root cause | Do (where to fix), Check C5 (causal adequacy) | The builder treats symptoms, not the cause |
| **Success criterion** | Check (the validation oracle), §9 sign-off | "Done" is unfalsifiable |
| **Repo + branch target** | Do (branch from), Publish (where the PR goes) | The fix lands on the wrong branch |
| **Scope / out-of-scope** | Do (stay in lane), reviewer (scope creep is a FAIL) | Scope creep; an un-reviewable diff |
| **Repro instruction** | Check C2 (red pre-fix) | No way to prove the bug existed |
| **Test file** | Do (ship it here), C4 gate (run it) | The C4 red→green proof has nothing to run |
| **Disposition hint** | sets expectations for sign-off | — |

Notice how **INTEGRATION.md is cited inline** — "addons production branch per
INTEGRATION §2", "addon convention per INTEGRATION §3". That's the payoff of
[step 01](01-render-and-integrate.md): the planner resolves abstract questions
("which branch? where does the test go?") against your repo's concretizations
instead of guessing.

### The Plan can be a pointer

If your project already plans through its own artifacts — an ADR, an enhancement
proposal, a normative spec under change control — you don't restate that here. Use
`templates/plan-pointer.md.tpl`: a thin brief that **references** the host document
(`- **Planning artifact:** docs/adr/0042-thing.md`) and carries only the few fields the
driver parses (slug, success criterion, branch target, test file). Do reads the
referenced artifact as the authoritative plan and cites it; the rest of the cycle is
unchanged. This lets PDCA wrap a host's existing planning process instead of imposing
its own document shape.

## STOP discipline

Every brief carries the rule that the cycle never ships before sign-off:

> Draft only until Check sign-off. A draft PR MAY be opened for CI; the PR MUST
> NOT be marked ready before sign-off accepts.

The brief is now written. State: `PLANNED`. The driver advances unattended into
the Do beat — [step 04](04-do.md).

← [02 Rehearse offline](02-rehearse-offline.md) · [Index](README.md) · next: [04 Do →](04-do.md)
