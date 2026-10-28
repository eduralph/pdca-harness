# Contributing to pdca-harness

Thanks for contributing. This project is licensed under the
[Apache License, Version 2.0](LICENSE); contributions are accepted under the same
license.

## Developer Certificate of Origin (sign-off)

Contributions are gated on the [Developer Certificate of Origin](DCO) (DCO 1.1).
By signing off you certify the DCO's terms for your contribution. Sign off every
commit with:

```bash
git commit -s
```

This appends a `Signed-off-by: Your Name <you@example.com>` trailer (using your
`git config user.name` / `user.email`). Amend a commit that's missing it with
`git commit --amend -s`.

## Engineering discipline

- One logical change per commit / PR; split unrelated changes.
- A change ships with the means to verify it — a test, or a stated reason why a test
  is impractical plus a manual repro.
- Keep the offline suite green: `cd template && PYTHONPATH=src python3 -m unittest discover -s tests`.
- Keep the root template suite green: `python3 -m tests.run_root_suite` (renders the template
  with copier and runs `copier update` from the previous release; it is what
  `.github/workflows/render-check.yml` runs). Those cases need copier importable **by the
  interpreter you run it with** — a CLI-only install (pipx-style, in its own venv) is not
  enough, so use `python3 -m pip install copier` in that interpreter or a venv. When they did
  not run, this entry point exits `77` with a `PDCA-UNVERIFIABLE:` line rather than reporting
  a run that verified nothing as success. `python3 -m unittest discover -s tests` still just
  skips them and exits 0.

See the vendored process model under `template/PCDA/quality-cycle/` for the full
Plan · Do · Check · Act discipline this harness embodies.

## Checking stack mode on real GitHub

The offline suites test stack mode against a local bare `origin` and a stubbed `gh`.
`tools/live/stack_mode_live.py` (standard library only, not shipped to rendered instances)
runs the same workflow on GitHub, on the private sandbox repository
`eduralph/pdca-stack-sandbox`: real `git`, real `gh`, PRs merged on GitHub. Each run goes
through the driver as `pdca flow <ids>` does, with the model leaves replaced by fixed patches,
so it makes no model calls. Run it after a change to stack mode, or when GitHub's behaviour
may have changed:

```bash
W=$(mktemp -d)
python3 tools/live/stack_mode_live.py preflight                 # checks only; writes nothing
python3 tools/live/stack_mode_live.py setup    --workdir "$W"   # run base live-<UTC stamp>
python3 tools/live/stack_mode_live.py run      --workdir "$W"   # writes observations.json
python3 tools/live/stack_mode_live.py judge    --workdir "$W"   # writes report.md, report.json
python3 tools/live/stack_mode_live.py teardown --workdir "$W"   # closes PRs, deletes branches
```

**What it needs.** The sandbox repository, private, with merge commits and squash merges both
allowed, and a fine-grained token with read and write access to its contents and pull
requests in `~/.config/pdca/sandbox-gh-token` (`--token-file` points elsewhere). Every `gh`
and `git` call of the tool and the driver uses that token, never an ambient `GH_TOKEN`,
`GITHUB_TOKEN` or `gh` login. Before any network call the tool refuses a missing or empty
token file and a repository not named `pdca-stack-sandbox` and without `sandbox` in its name
(this repository by name); before writing anything it refuses a repository that is not
private or not pushable, or that forbids merge commits or squash merges. The driver opens
every PR as a draft, so the sandbox must also allow draft pull requests; `preflight` warns
when it may not (a private repository of a personal account).

**What it costs.** One run base branch, `live-<UTC stamp>`, cut from the sandbox's `main`;
about seven PRs and a dozen branches against it; about ten minutes, mostly waiting for GitHub
to recompute PR file lists. `main`, the repository and its settings are never changed.
`teardown` closes the run's open PRs and deletes its branches; merged and closed PRs stay in
the sandbox's history. It also removes the clone and the bundles from the work directory and
keeps `state.json`, `observations.json` and the report. If `setup` stops part-way, it prints
the `teardown` command that removes what it made.

**Reading the report.** `judge` reads only `observations.json`, needs no network, and exits 0
only if every check passes. Each check is PASS, FAIL or INCOMPLETE. INCOMPLETE means something
the check needs was not observed: a PR that was not opened, a branch delete that was not
confirmed, a merge or "Update branch" that GitHub refused, a PR file list `run` did not see
settle. Such a run cannot be used; fix the cause and run again in a fresh work directory. A
FAIL is a complete observation that disagrees with what the code and docs say: file each one
as its own bug. The file lists are the ones GitHub shows on the PR. GitHub moves a PR's base
commit and file list when the PR is pushed to, not when its base branch moves, so a list read
after a merge with no push to the PR counts once the PR's head is unchanged and GitHub has
recomputed its mergeability against the new base tip; beside each list the report records
GitHub's compare of that tip and the PR's head. `tests/test_stack_mode_live_tool.py` tests
`judge`, the guard, `preflight` and `teardown` offline against a stand-in `gh`; nothing in CI
runs the live steps.

The STOP discipline in [AGENTS.md](AGENTS.md) covers this project's own pull requests. Its one
exception is the ready-marks and merges that `tools/live/stack_mode_live.py` makes itself,
started by a human, on a repository that passes the tool's repo guard. The exception covers no
other merge on the sandbox repository, by hand or by an agent.
