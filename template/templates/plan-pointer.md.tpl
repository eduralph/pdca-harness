# Brief (pointer) — issue <id> / <slug>

> A Plan artifact that is a **pointer**, for hosts that plan through their OWN
> artifacts (an ADR, an enhancement proposal, a normative spec) rather than a brief
> authored here (docs 03). The host's document IS the plan; this file just references
> it and carries the few fields the driver parses. Use this instead of `brief.md.tpl`
> when the planning decision already lives — and is reviewed/governed — elsewhere.
>
> Keep the `- **Label:** value` lines: Do reads the spec fields from them, and the
> driver/SUMMARY read slug / success criterion / branch. Do reads the **Planning
> artifact** as the authoritative plan; this brief does not restate it.

- **Slug:** <short-kebab-slug>
- **Planning artifact:** <path or URL to the host's ADR / proposal / spec that IS the
  plan — e.g. `docs/adr/0042-thing.md`, or a permalinked spec section. Do treats this
  as authoritative; cite it.>
- **Defect / goal:** <one line: what this realizes — the observable problem or capability>
- **Success criterion:** <the observable condition that means it works — what the shipped test asserts>
- **Falsifiability:** <WHERE the binding success criterion can be made to go RED, and on
  WHICH harness/topology Do is pointed at. If no available environment can currently produce
  the red — a topology that can't exhibit the forbidden failure, or code no gate compiles —
  that is a Plan-blocking gap: provision the environment or narrow the criterion before Do
  runs. See `brief.md.tpl` for the worked example.>
- **Repo + branch target:** <owner/repo> @ <branch>   (resolve here at Plan — do not leave to Do)
- **Onto branch:** <remote>/<existing-pr-branch>   (optional — stack onto an open PR's branch; docs 03)
- **Depends on:** <id>[, <id>…]   (optional — ids only, any trailing note is ignored; scheduling waits until these are COMPLETE; docs 09)
- **Depends on (merged):** <id>[, <id>…]   (optional — ids only, any trailing note is ignored; hold until each prereq's PR is MERGED, not just COMPLETE; use when this edits files a prereq also edits; docs 09)
- **Conflicts with:** <id>[, <id>…]   (optional — ids only, any trailing note is ignored; never co-schedule in one wave; docs 09)
- **Scope:** <the one logical change this realizes> / out of scope: <what is excluded>
- **Difficulty:** <`low` | `medium` | `high` — the change's **blast-radius / cross-file
  reach** (files/call-sites touched and how far effects propagate, what a diff-reviewer
  must hold in view), NOT edge-case density. Routes the Do backend and review depth
  (issues #133/#134). Optional; absent/unknown is the safe default — nothing is skipped.>
- **External dependencies:** <build tools (e.g. `protoc`), runtime services (Docker, a live
  etcd/TiKV), and required topology/environment shape (a ≥3-replica cluster) the slice needs
  to build AND to make the success criterion go red→green — enumerated here so they preflight
  (seed the render's `[[doctor.checks]]`) rather than surface mid-cycle. `none` if the base
  toolchain suffices. Do MUST declare any it discovers that is not listed here rather than
  silently work around it. See `brief.md.tpl`.>
- **Test file:** <path where the regression test ships — must fail pre-change, pass post-change>
- **Citations expected:** Do must cite path:line on the target branch AND the Planning artifact for every change.
- **Disposition hint:** <likely-fix | likely-close | … see brief.md.tpl for the full set>

## STOP discipline

Draft only until Check sign-off. A draft PR MAY be opened for CI; the PR MUST NOT be
marked ready before sign-off accepts.
