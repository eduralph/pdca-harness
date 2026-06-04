# Brief — issue TOY / off-by-one-in-pager

> A worked toy brief so the driver runs end-to-end offline (stub leaves/gates).
> Replace with real briefs from your Plan beat. The field labels are what the
> driver parses — keep the `- **Label:** value` shape.

- **Slug:** off-by-one-in-pager
- **Defect:** The result pager shows N-1 of N items; the last item is dropped.
- **Success criterion:** Pager shows all N items; a test over a 3-item list asserts 3 are rendered.
- **Repo + branch target:** example-repo @ main
- **Scope:** Fix the pager's range bound. / out of scope: pager styling, pagination size.
- **Repro instruction:** Load a 3-item fixture, open the pager, observe 2 items shown.
- **Test file:** test_toy.py
- **Citations expected:** Do must cite path:line on main for the changed bound.
- **Disposition hint:** likely-fix

## STOP discipline

Draft only until Check sign-off. A draft PR MAY be opened for CI; the PR MUST NOT
be marked ready before sign-off accepts.
