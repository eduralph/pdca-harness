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

See the vendored process model under `template/PCDA/quality-cycle/` for the full
Plan · Do · Check · Act discipline this harness embodies.
