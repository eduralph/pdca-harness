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
