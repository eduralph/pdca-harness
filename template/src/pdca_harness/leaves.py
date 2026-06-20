"""The model leaves — the only points where a model is invoked (docs 03 §leaves).

The rest of the pipeline is deterministic code; models fill *artifacts*, never
decide control flow. The cycle has exactly **four beats** (Plan · Do · Check · Act);
the leaves are model touchpoints *within* those beats, not beats of their own — in
particular review, sign-off and publish are all **steps of the Check beat**. The six
leaves:

* **planner** (Plan, interactive) — the human feeds documents (e.g. a tracker CSV)
  and Claude writes ``brief.md``;
* **builder** (Do, headless) — reads ``brief.md``, writes ``patch.diff`` + the
  named test + ``build-notes.md``;
* **reviewer** (Check — review step, headless) — advisory, decorrelated, writes
  ``check-review.md``;
* **signoff** (Check — sign-off step, interactive) — Claude reviews the result
  *with* the human and records the decision token;
* **publisher** (Check — publish step, interactive) — on an accepted bundle, writes
  the contribution artifacts (the ``publish`` module does the git/draft-PR);
* **act** (Act, interactive) — reviews frozen cycles and proposes process deltas.

Two invariants live here and matter more than any prompt:

1. **Independence is a missing input.** The reviewer never sees ``build-notes.md``.
   In ``stub`` mode it simply isn't passed; in ``command`` mode the reviewer runs
   in a temp sandbox containing *only* the reviewer inputs, so the file is
   physically absent (a prompt instruction would not be enough).
2. **The builder cannot mark a PR ready.** Enforced by the ``builder`` subagent's
   tool scope + the ``builder_guard.py`` PreToolUse hook; the stub never does it.

``mode == "stub"`` writes offline placeholders (no Claude/TTY). ``mode ==
"command"`` runs the configured ``argv`` with the leaf's prompt appended, as a
subprocess in the working dir; ``interactive`` leaves inherit the terminal.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import act as act_mod
from . import brief
from . import gates
from . import progress
from .config import Config, LeafConfig

# build-notes.md is DELIBERATELY ABSENT from this list (independence contract).
REVIEWER_INPUTS = ["patch.diff", "brief.md", "check-gates.json"]

# The interactive sign-off leaf writes its decision here; the flow reads it and
# routes it through the C6-guarded signoff.record (never a model-written §9).
SIGNOFF_DECISION = "signoff-decision"
VALID_DECISIONS = frozenset({"accept", "iterate-do", "iterate-plan", "discontinue"})


# ----------------------------------------------------------------------------
# Subprocess invocation — the one place a leaf command is run.
# ----------------------------------------------------------------------------
def _invoke(
    leaf: LeafConfig,
    workdir: Path,
    prompt: str,
    *,
    label: str = "",
    status=None,
    stream_json: bool = False,
    env: dict | None = None,
    extra_argv: list[str] | None = None,
) -> None:
    """Run the leaf's configured command in ``workdir``, feeding it ``prompt``.

    Interactive leaves get the prompt as a *seed positional* (``claude "<prompt>"``)
    and inherit the parent terminal (a REPL); a non-zero exit (the human leaving
    the session) is not fatal. Headless leaves get the prompt on **stdin**, not as
    a trailing positional — a variadic option such as ``--allowedTools`` would
    otherwise swallow the prompt arg (claude then errors "Input must be provided…").

    ``label`` / ``status`` decorate the headless heartbeat (which leaf, and a live
    snapshot of its work — see :func:`progress.bundle_activity`). ``stream_json``
    (Tier 3) asks for the live tool-use stream: for a headless **claude** leaf it adds
    ``--output-format stream-json --verbose`` and the heartbeat shows the tool the
    leaf is using right now. Ignored for non-claude families (e.g. a codex reviewer),
    which don't speak that format.
    """
    argv = list(leaf.argv) + list(extra_argv or [])
    run_env = {**os.environ, **env} if env else None
    if leaf.interactive:
        subprocess.run(argv + [prompt], cwd=workdir, env=run_env)
        return
    # Headless: feed the prompt on stdin (a trailing positional would be swallowed
    # by a variadic --allowedTools) and tick a heartbeat, since `claude -p` prints
    # nothing until it finishes (minutes) and would otherwise look hung.
    use_stream = stream_json and leaf.family == "claude"
    if use_stream:
        argv += ["--output-format", "stream-json", "--verbose"]
    rc, _ = progress.run_with_heartbeat(
        argv, cwd=workdir, input_text=prompt, label=label, status=status,
        stream_json=use_stream, env=run_env)
    if rc != 0:
        raise subprocess.CalledProcessError(rc, argv)


# ----------------------------------------------------------------------------
# Notes-fetch (issue #65): retrieve a bundle's tracker thread before the Plan beat.
# ----------------------------------------------------------------------------
def ensure_notes(cfg: Config, d: Path) -> None:
    """Run the configured ``[tracker].notes_cmd`` to seed ``d/notes.json`` if it is absent.

    The command (a ``.format(id=)`` shell template) is the project's tracker-scrape tooling;
    it runs with ``$PDCA_BUNDLE`` set to the bundle dir and is responsible for writing
    ``notes.json`` there. So the Plan leaf can read the thread without the operator
    pre-scraping by hand. Best-effort: no command configured, notes already present, or a
    failing fetch are all non-fatal — Plan then falls back to the CSV / asking the human.
    """
    if not cfg.notes_cmd or (d / "notes.json").exists():
        return
    d.mkdir(parents=True, exist_ok=True)
    issue_id = d.name.removeprefix("issue_")
    cmd = cfg.notes_cmd.format(id=issue_id)
    env = {**os.environ, "PDCA_BUNDLE": str(d)}
    try:
        rc, _ = progress.run_with_heartbeat(
            cmd, cwd=cfg.root, shell=True, env=env, capture=True,
            label=f"fetch notes {d.name}")
    except Exception as exc:  # noqa: BLE001 — a failed scrape must not break Plan
        print(f"leaves: notes fetch for {d.name} failed ({exc}); "
              "Plan will fall back to the CSV / human", file=sys.stderr)
        return
    if rc != 0 or not (d / "notes.json").exists():
        print(f"leaves: notes fetch for {d.name} produced no notes.json (rc {rc}); "
              "Plan will fall back to the CSV / human", file=sys.stderr)


# ----------------------------------------------------------------------------
# Leaf 0 — Plan (planner, interactive): human feeds documents → writes brief.md.
# ----------------------------------------------------------------------------
def do_plan(d: Path, cfg: Config, csv: str | None = None) -> None:
    d.mkdir(parents=True, exist_ok=True)
    ensure_notes(cfg, d)  # seed notes.json from the tracker scraper if configured (#65)
    if cfg.planner.mode == "command":
        _invoke(cfg.planner, cfg.root, _plan_prompt(cfg, csv, d))
        return
    _stub_plan(d, cfg)


def _plan_prompt(cfg: Config, csv: str | None, d: Path) -> str:
    fix_tpl = cfg.templates_dir / "brief.md.tpl"
    geps_tpl = cfg.templates_dir / "design-proposal.md.tpl"
    pointer_tpl = cfg.templates_dir / "plan-pointer.md.tpl"
    issue_id = d.name.removeprefix("issue_")
    tracker_csv = csv or cfg.tracker_export_csv
    notes = d / "notes.json"
    # Source of truth = the tracker row for THIS issue, not a scan of the harness repo.
    src_line = (
        f"The issue is {issue_id} on the {cfg.tracker_system or 'tracker'}"
        + (f" ({cfg.tracker_url}). " if cfg.tracker_url else ". ")
    )
    csv_line = (
        f"Read the row for {issue_id} in the tracker export at '{tracker_csv}' FIRST — "
        "that row (summary / description / steps) is the authoritative statement of what "
        "to brief. " if tracker_csv else
        "Ask the human for the issue's tracker export or details. "
    )
    notes_line = (
        f"If {notes} exists, read it for the full comment thread; if you need the "
        "discussion and it is absent, ask the human to produce it with the project's "
        "tracker-scrape tooling, and stop. "
    )
    citation_line = (
        "Cite the root cause against the target source with `git -C <checkout> log/show "
        "-- <file>` plus Read/Grep on the checkout — NEVER `cd <checkout> && git ...` "
        "(it trips a safety prompt; `git -C` is the safe idiom). Do NOT scan THIS harness "
        "repo for issue information — the tracker is the source. "
    )
    return (
        "You are the Plan leaf of a PDCA cycle. " + src_line + csv_line + notes_line
        + citation_line
        + f"Together with the human, write brief.md in the bundle directory {d}. Default "
        f"to {fix_tpl} — it fits bug fixes AND ordinary new functionality. Use {geps_tpl} "
        "(a design proposal) ONLY for the exception: a change significant enough to "
        "warrant a proposal (major architecture / API / UX). Not every feature is a "
        f"design proposal — when in doubt use the normal brief. Use {pointer_tpl} when the "
        "plan ALREADY lives in a host artifact (an ADR / proposal / normative spec): the "
        "brief then POINTS at that document (a `Planning artifact:` reference) instead of "
        "restating it. Keep the parsed `- **Label:** value` field shape; resolve the repo + "
        "branch target per INTEGRATION §2. One bundle = one brief.md. Plan only."
    )


def _stub_plan(d: Path, cfg: Config) -> None:
    tpl = cfg.templates_dir / "brief.md.tpl"
    if tpl.exists():
        shutil.copyfile(tpl, d / "brief.md")
        return
    (d / "brief.md").write_text(
        "# Brief — stub\n\n"
        "- **Slug:** stub-issue\n"
        "- **Defect:** stub defect authored by the planner stub.\n"
        "- **Success criterion:** the stub test passes.\n"
        "- **Repo + branch target:** example-repo @ main\n"
        "- **Test file:** test_stub.py\n",
        encoding="utf-8",
    )


def do_plan_batch(cfg: Config, csv: str | None = None, ids: list[str] | None = None) -> None:
    """Batch Plan: ONE interactive session may brief several issues at once.

    Default (``ids is None``): the planner reads the documents/CSV and CHOOSES which issues
    to brief, creating an ``issue_<id>/brief.md`` per chosen issue (``flow.flow_batch``).

    Id-seeded (``ids`` given, issue #65): the planner briefs EACH listed id, reading that
    bundle's ``notes.json`` as the source — so an explicit set seeded from per-bundle notes
    (not a tracker CSV) briefs in one shared session. Each id's notes are fetched first via
    :func:`ensure_notes`; the flow then drives exactly those ids (``flow.flow_ids``).
    """
    cfg.bundle_root.mkdir(parents=True, exist_ok=True)
    for iid in ids or []:
        ensure_notes(cfg, cfg.bundle(iid))  # seed notes.json before the session (#65)
    if cfg.planner.mode == "command":
        _invoke(cfg.planner, cfg.root, _plan_batch_prompt(cfg, csv, ids))
        return
    _stub_plan_batch(cfg, ids)


def _plan_batch_prompt(cfg: Config, csv: str | None, ids: list[str] | None = None) -> str:
    fix_tpl = cfg.templates_dir / "brief.md.tpl"
    geps_tpl = cfg.templates_dir / "design-proposal.md.tpl"
    tpl_line = (
        f"use the fitting template: a bug fix → {fix_tpl}; a feature / enhancement → "
        f"{geps_tpl}. Keep the parsed `- **Label:** value` field shape")
    if ids:
        listing = ", ".join(ids)
        return (
            "You are the Plan leaf of a PDCA cycle, in BATCH mode over a SPECIFIC id list: "
            f"{listing}. Brief EACH listed id. For each, read its bundle's "
            f"`{cfg.bundle_root}/issue_<id>/notes.json` (the seeded triage notes / comment "
            "thread) as the source of truth"
            + (f", and consult the row for it in the tracker export at '{csv}' too" if csv else "")
            + ". The notes/tracker are the source: do NOT scan THIS harness repo for issue "
            "info, and cite the target source via `git -C <checkout> ...` (never "
            f"`cd <checkout> && ...`). Write `{cfg.bundle_root}/issue_<id>/brief.md` for each "
            f"— {tpl_line}. If a listed id genuinely should NOT be briefed (no actionable "
            "defect), leave it UNPLANNED (write no brief.md) and say why. One id = one "
            "`issue_<id>/brief.md`. Plan only — do not implement."
        )
    tracker_csv = csv or cfg.tracker_export_csv
    src = f"the tracker export at '{tracker_csv}'" if tracker_csv \
        else "the input documents the human shares"
    return (
        "You are the Plan leaf of a PDCA cycle, in BATCH mode. With the human, read "
        f"{src} on the {cfg.tracker_system or 'tracker'} and decide which issues to brief "
        "— there may be SEVERAL. The tracker rows are the source of truth: do NOT scan "
        "THIS harness repo for issue info, and cite the target source via "
        "`git -C <checkout> ...` (never `cd <checkout> && ...`). For EACH chosen issue "
        f"create a bundle directory `{cfg.bundle_root}/issue_<id>/` containing a brief.md "
        f"— {tpl_line}; `<id>` is the "
        "tracker id. One issue = one `issue_<id>/brief.md`. Plan only — do not implement."
    )


def _stub_plan_batch(cfg: Config, ids: list[str] | None = None) -> None:
    # Id-seeded: brief exactly the listed ids; else two default bundles (offline slice).
    for iid in (ids if ids else ("BATCH1", "BATCH2")):
        d = cfg.bundle(iid)
        d.mkdir(parents=True, exist_ok=True)
        _stub_plan(d, cfg)


# ----------------------------------------------------------------------------
# Leaf 1 — Do (builder, headless): writes patch.diff + the test + build-notes.md.
# ----------------------------------------------------------------------------
def do_build(d: Path, cfg: Config) -> None:
    if cfg.builder.mode == "command":
        # The builder runs from cfg.root but writes into the bundle d — watch d so the
        # heartbeat shows patch.diff / build-notes.md appearing as it works.
        _invoke(
            cfg.builder, cfg.root, _build_prompt(d),
            label=f"Do {d.name}",
            status=lambda: progress.bundle_activity(d, ("patch.diff", "build-notes.md")),
            stream_json=True,  # Tier 3: show the builder's live tool-use
        )
        return
    _stub_build(d, cfg)


def _build_prompt(d: Path) -> str:
    return (
        f"You are the Do builder. Read {d}/brief.md. Build to satisfy its **Success "
        "criterion** (the real end result), not a narrower proxy — an item is done only "
        "when that end result holds, proven red→green; a green mechanical check on "
        "something adjacent is not done. If brief.md names a **Planning artifact** (an "
        "ADR / proposal / spec), READ that document — it is the authoritative plan and the "
        "brief only points at it; build to it and cite it. If brief.md carries an '## Iteration N — "
        "carry-forward' block, address it (the previous attempt's rationale + failing "
        "gate) and do NOT repeat the rejected approach. Produce, in the bundle directory "
        f"{d}: (1) patch.diff — a unified diff against the brief's target branch; "
        "(2) the test file the brief names, red before the fix and green after; "
        "(3) build-notes.md — your rationale (withheld from the reviewer). Cite "
        "path:line on the target branch for every change. To run the test red→green, "
        "use the project's own test runner (it provides a timeout and whatever "
        "environment it is configured for); do NOT hand-roll your own runner command "
        "(a raw container or ad-hoc test invocation) — it has no timeout and can hang "
        "forever, stalling the cycle. "
        "Do NOT assume the runner gives you a display / GUI / other rich runtime: if it "
        "is headless, a test that imports a heavy module (a GUI toolkit, etc.) AT LOAD "
        "can crash it (and recur every iterate-do) — keep the unit under test "
        "import-light by extracting the logic into an import-free module and testing "
        "that. Make the patch commit-ready for the TARGET repo: run the project's "
        "configured formatter / commit hooks before declaring done — the publish commit "
        "runs the target's own hooks (formatter/linters), which no PDCA gate models, so a patch the target's "
        "commit hook would reject is not done even if every gate is green. Do NOT push, "
        "open, or mark any PR ready."
    )


def _stub_build(d: Path, cfg: Config) -> None:
    test_rel = (brief.test_files(d / "brief.md") or [Path("test_stub.py")])[0]
    test_path = d / test_rel
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text(
        "# Stub regression test shipped by the Do leaf (vertical slice).\n"
        "def test_placeholder():\n    assert True\n",
        encoding="utf-8",
    )
    (d / "patch.diff").write_text(
        "# Stub patch produced by the Do leaf for the vertical slice.\n"
        "# A real builder writes a unified diff here.\n"
        f"# (the shipped test is {test_rel})\n",
        encoding="utf-8",
    )
    (d / "build-notes.md").write_text(
        "# Build notes (builder rationale — withheld from the reviewer)\n\n"
        "Stub Do leaf. A real builder records here why this change, what was\n"
        "tried, and what was ruled out. The reviewer never sees this file.\n",
        encoding="utf-8",
    )


# ----------------------------------------------------------------------------
# Leaf 2 — Check reviewer (headless, decorrelated, advisory): check-review.md.
# ----------------------------------------------------------------------------
def reviewer_input_paths(d: Path) -> list[Path]:
    """The exact files the reviewer receives — build-notes.md is not among them."""
    return [d / name for name in REVIEWER_INPUTS]


_REVIEW_PROMPT = (
    "You are the Check reviewer — advisory, artifact-only, decorrelated from the "
    "builder. You have ONLY patch.diff, brief.md and check-gates.json in this "
    "directory (build-notes.md is deliberately withheld). Write check-review.md. "
    "It MUST contain a complete verdict table — one row for EVERY element of the "
    "5/5/1 matrix, in order:\n"
    + "\n".join(f"  {elem} — {label}" for elem, label, _kind, _oracle in gates.canonical_elements())
    + "\nFormat it as a Markdown table `| Item | Verdict | Basis |`, the Item column "
    "carrying the element label above, the Verdict one of PASS / FAIL / NEEDS-HUMAN / "
    "N/A, the Basis a one-line reason you re-derived yourself (cite path:line where "
    "you can). Emit NEEDS-HUMAN for the always-human items (validation "
    "fitness-to-purpose, contested root-cause, ambiguous scope) — each NEEDS-HUMAN "
    "row becomes a §6 item the human must clear. Do not omit a row; use N/A with a "
    "reason when an element does not apply. "
    "Ground every cited path:line on the target source at $PDCA_TARGET (read-only); "
    "if $PDCA_TARGET is unset, ground against patch.diff alone — do NOT search other "
    "checkouts on the machine."
)


def _reviewer_target(d: Path, cfg: Config) -> Path | None:
    """The local target checkout the reviewer grounds its citations on, or None (#75).

    Single-sourced from the brief's "Repo + branch target" via the same resolution
    publish uses (``_checkout_path`` — configured ``[publisher.checkouts]`` or the
    sibling convention). Returned only if it exists on disk; the reviewer is told to
    ground against ``$PDCA_TARGET`` and not to wander into other checkouts. Best-effort:
    any failure (no target, unresolved) yields None and the reviewer falls back to the diff.
    """
    from . import publish  # lazy: publish imports leaves, avoid an import cycle
    try:
        repo_spec, _base, _slug = publish._resolve_target(d)
        if not repo_spec:
            return None
        p = publish._checkout_path(cfg, repo_spec)
        return p if p.exists() else None
    except Exception:  # noqa: BLE001 — grounding access is best-effort, never fatal
        return None


def run_review(d: Path, cfg: Config) -> None:
    inputs = reviewer_input_paths(d)
    assert (d / "build-notes.md") not in inputs, "independence contract violated"

    if cfg.reviewer.mode == "command":
        _run_review_sandboxed(d, cfg)
        return
    _stub_review(d, cfg)


def _run_review_sandboxed(d: Path, cfg: Config) -> None:
    """Run the reviewer in a temp dir holding ONLY the reviewer inputs.

    This makes the independence contract mechanical, not prompt-based: with the
    reviewer's cwd containing no build-notes.md, the builder's framing cannot
    leak in even though the model has a Read tool. check-review.md is copied back.
    """
    with tempfile.TemporaryDirectory(prefix="pdca-review-") as tmp:
        sandbox = Path(tmp)
        for name in REVIEWER_INPUTS:
            src = d / name
            if src.exists():
                shutil.copy2(src, sandbox / name)
        # Ground citations on the brief's target checkout (#75): name it via $PDCA_TARGET
        # so the reviewer doesn't wander into unrelated checkouts, and grant read access
        # for the claude family (--add-dir). Independence holds — the target is the
        # upstream source, not build-notes.md.
        target = _reviewer_target(d, cfg)
        env = {"PDCA_TARGET": str(target)} if target else None
        extra_argv = ["--add-dir", str(target)] if target and cfg.reviewer.family == "claude" else None
        try:
            _invoke(
                cfg.reviewer, sandbox, _REVIEW_PROMPT,
                label=f"Check review {d.name}",
                status=lambda: progress.bundle_activity(sandbox, ("check-review.md",)),
                stream_json=True,  # Tier 3 (no-op unless the reviewer family is claude)
                env=env, extra_argv=extra_argv,
            )
        except Exception as exc:  # a failed reviewer (e.g. dropped connection) must
            _review_unavailable(d, f"reviewer leaf failed: {exc}")  # not crash the cycle
            return
        produced = sandbox / "check-review.md"
        if produced.exists():
            shutil.copy2(produced, d / "check-review.md")
        else:
            _review_unavailable(d, "reviewer produced no check-review.md")


def _review_unavailable(d: Path, reason: str) -> None:
    """Write a placeholder review flagging the gap as a §6 NEEDS-HUMAN, so a failed or
    interrupted reviewer leaves a re-runnable bundle — not a half-checked one that
    crashes assemble. The bundle still reaches sign-off; accept is blocked (C6)."""
    print(f"leaves: {d.name} — advisory review unavailable ({reason})", file=sys.stderr)
    (d / "check-review.md").write_text(
        "# Advisory review — NOT COMPLETED\n\n"
        f"The reviewer did not produce a verdict table ({reason}).\n\n"
        "- NEEDS-HUMAN — re-run the Check reviewer; this bundle has no advisory review "
        "and must not be accepted until one exists.\n",
        encoding="utf-8",
    )


# Stub bases per 5/5/1 element — what a real reviewer would re-derive; the offline
# stub asserts the same complete table shape every command-mode reviewer must emit.
_STUB_BASIS = {
    "C1": "brief.md present and parsed",
    "C2": "stub: reproduction red pre-fix",
    "C3": "patch.diff present — one logical fix",
    "C4": "stub red→green confirmed",
    "C5": "stub: fix addresses the cited root cause",
    "T1": "bundle structure complete",
    "T2": "no forbidden constructs",
    "T3": "imports resolve in a clean env",
    "T4": "commit-msg / branch-target / version conform",
    "T5": "conformance judgment clear",
    "V":  "is this the right thing at all? (always-human by design)",
}


def _stub_review(d: Path, cfg: Config) -> None:
    # Emit the SAME complete 5/5/1 verdict table the command-mode reviewer must
    # produce: every element a row, all PASS except the always-human validation cell
    # (NEEDS-HUMAN by design — it becomes the §6 item the human clears).
    rows = ["| Item | Verdict | Basis |", "|------|---------|-------|"]
    for elem, label, _kind, _oracle in gates.canonical_elements():
        verdict = "NEEDS-HUMAN" if elem == "V" else "PASS"
        rows.append(f"| {label} | {verdict} | {_STUB_BASIS.get(elem, '')} |")
    (d / "check-review.md").write_text(
        "# Cross-vendor reviewer (advisory, artifact-only)\n\n"
        f"Reviewer family: {cfg.reviewer.family or 'stub'}. "
        "Inputs: patch.diff, brief.md, check-gates.json (build-notes.md withheld).\n\n"
        "## Per-item verdicts (5 correctness · 5 conformance · 1 validation)\n"
        + "\n".join(rows)
        + "\n\nValidation fitness-to-purpose stays NEEDS-HUMAN by design — the human "
        "decides at sign-off.\n",
        encoding="utf-8",
    )


# ----------------------------------------------------------------------------
# Leaf 3 — Check sign-off (signoff, interactive): Claude + human reach the OK.
# ----------------------------------------------------------------------------
def run_signoff(d: Path, cfg: Config) -> None:
    if cfg.signoff.mode == "command":
        _invoke(cfg.signoff, cfg.root, _signoff_prompt(d))
        return
    _stub_signoff(d, cfg)


def _signoff_prompt(d: Path) -> str:
    return (
        f"You are the Check sign-off leaf. Review {d}/SUMMARY.md, {d}/patch.diff, "
        f"{d}/check-gates.md and {d}/check-review.md together with the human. Help "
        f"the human clear the §6 NEEDS-HUMAN items in {d}/SUMMARY.md (change "
        f"`- [ ]` to `- [x]` only with their explicit OK). Then write the agreed "
        f"decision as a single token — one of: {', '.join(sorted(VALID_DECISIONS))} — "
        f"into {d}/{SIGNOFF_DECISION}. For an iterate, add the rationale (why rejected / "
        f"what to change) on the lines below the token; for discontinue, the rationale (why "
        f"discontinued / where the work goes instead). Do not edit §9 yourself; the "
        "driver records it under a deterministic guard."
    )


def _stub_signoff(d: Path, cfg: Config) -> None:
    # Simulate the human clearing §6 and accepting, so the offline flow completes.
    summary = d / "SUMMARY.md"
    if summary.exists():
        text = summary.read_text(encoding="utf-8")
        summary.write_text(text.replace("- [ ]", "- [x]"), encoding="utf-8")
    (d / SIGNOFF_DECISION).write_text("accept\n", encoding="utf-8")


def run_signoff_batch(cfg: Config, bundles: list[Path]) -> None:
    """Batch sign-off: ONE interactive session walks several halted bundles.

    Mirrors :func:`do_plan_batch` — command mode runs a single seeded session over
    the whole (cheap-first) chunk, so the human signs off N bundles without N session
    startups + re-orientations; stub mode loops the per-bundle stub. Each bundle's
    decision is written as soon as it is decided, so a session that ends early keeps
    the bundles already done. The flow chunks the queue so one session is bounded
    (``flow.SIGNOFF_BATCH_SIZE``). The headless reviewer is deliberately NOT batched
    (kept per-bundle/sandboxed for independence + drop-isolation)."""
    if not bundles:
        return
    if cfg.signoff.mode == "command":
        _invoke(cfg.signoff, cfg.root, _signoff_batch_prompt(bundles))
        return
    for d in bundles:
        _stub_signoff(d, cfg)


def _signoff_batch_prompt(bundles: list[Path]) -> str:
    listing = "\n".join(f"  - {d}" for d in bundles)
    return (
        "You are the Check sign-off leaf, in BATCH mode: this ONE session covers "
        f"several bundles (cheap-first):\n{listing}\n"
        "Work them in order. For EACH bundle, review its SUMMARY.md / patch.diff / "
        "check-gates.md / check-review.md with the human, help clear that bundle's §6 "
        "NEEDS-HUMAN items (`- [ ]` → `- [x]` only with their explicit OK), then write "
        f"the agreed decision token — one of: {', '.join(sorted(VALID_DECISIONS))} — into "
        f"THAT bundle's {SIGNOFF_DECISION} file **as soon as it is decided** (so if the "
        "session ends early the finished bundles keep their decisions). Every write names "
        "its own `issue_<id>` bundle — never leave an item ambient to the batch or write "
        "it into the wrong bundle. Do not edit §9 yourself; the driver records it under a "
        "deterministic guard."
    )


def signoff_decision(d: Path) -> str:
    """The decision token (first line of ``signoff-decision``), or "" if absent/invalid.

    The file is ``<token>`` optionally followed by a free-text **rationale** on the
    remaining lines (read by :func:`signoff_rationale`) — the human's "why iterate /
    what to change" the driver carries forward into the brief on an iterate."""
    p = d / SIGNOFF_DECISION
    if not p.exists():
        return ""
    lines = p.read_text(encoding="utf-8").splitlines()
    token = lines[0].strip() if lines else ""
    return token if token in VALID_DECISIONS else ""


def signoff_rationale(d: Path) -> str:
    """The iterate rationale the sign-off leaf wrote below the token, or "" if none.

    Lines after the first of ``signoff-decision`` — the actionable insight ("why this
    Do attempt was rejected / what to change next") that the flow records into §9 and
    the driver folds into the brief's carry-forward so the next iteration isn't blind."""
    p = d / SIGNOFF_DECISION
    if not p.exists():
        return ""
    return "\n".join(p.read_text(encoding="utf-8").splitlines()[1:]).strip()


# ----------------------------------------------------------------------------
# Leaf 4 — Act (act, interactive): review frozen cycles, suggest deltas if sensible.
# ----------------------------------------------------------------------------
def run_act(cfg: Config, date: str) -> None:
    if cfg.act.mode == "command":
        _invoke(cfg.act, cfg.root, _act_prompt(cfg, date))
        return
    _stub_act(cfg, date)


def _act_prompt(cfg: Config, date: str) -> str:
    entries = act_mod.index(cfg)
    index_md = act_mod.render_index(entries, act_mod.patterns(entries))
    return (
        "You are the Act leaf — cross-cycle process review. Below is the read-only "
        "index of frozen cycles and recurring signals. With the human, decide which "
        "process deltas (spec template / ruleset / gates / agent skills) are sensible "
        f"— suggest improvements ONLY if warranted. Append a dated entry for {date} to "
        "process/act-log.md, or state that no delta is warranted. Never re-decide a "
        "contribution's disposition.\n\n--- ACT INDEX ---\n" + index_md
    )


def _stub_act(cfg: Config, date: str) -> None:
    entries = act_mod.index(cfg)
    text = act_mod.scaffold_entry(entries, act_mod.patterns(entries), date=date)
    act_mod.append_entry(cfg, text)


# ----------------------------------------------------------------------------
# Leaf 5 — Publish (publisher, interactive): the closing STEP of Check.
# Writes the two contribution artifacts (commit-msg.txt + pr-description.md, the
# T4 gate's inputs); the deterministic `publish` module does the git/draft-PR.
# ----------------------------------------------------------------------------
def run_publish(d: Path, cfg: Config) -> None:
    if cfg.publisher.mode == "command":
        _invoke(cfg.publisher, cfg.root, _publish_prompt(d, cfg))
        return
    _stub_publish(d, cfg)


def _publish_prompt(d: Path, cfg: Config) -> str:
    issue_id = d.name.removeprefix("issue_")
    target = brief.field(d / "brief.md", "repo + branch target", "target")
    pr_tpl = cfg.templates_dir / "pr-description.md.tpl"
    trailer = cfg.issue_trailer.format(id=issue_id) if cfg.issue_trailer else ""
    trailer_line = (
        f"The LAST line of commit-msg.txt is the issue trailer `{trailer}` (the T4 gate "
        "enforces it), preceded by a blank line with NOTHING appended after it — do not "
        "add a Co-Authored-By or any other trailer below it (a project may require the "
        "trailer to stand alone as a blank-separated last line). If no tracker id is "
        "assigned yet (the bundle id is not a real tracker number), OMIT the trailer "
        "entirely rather than invent a placeholder — `pdca publish --no-issue` records "
        "the contribution as id_pending for the human to fill the id in later. "
        if trailer else ""
    )
    return (
        "You are the Publish leaf — the closing work of Check. The fix for issue "
        f"{issue_id} is ACCEPTED; with the human, write TWO contribution artifacts in "
        f"{d}, following the project's contributor rules (docs/INTEGRATION.md §4). "
        f"Target: {target}. Read {d}/brief.md + {d}/build-notes.md + {d}/patch.diff for "
        "content; cite the target source with `git -C <checkout>` (never `cd <checkout> "
        "&& git`).\n"
        f"1) {d}/commit-msg.txt — a summary ≤70 chars, then a blank line, then the body "
        f"wrapped ≤80; reference any other commit by its FULL hash. {trailer_line}\n"
        f"2) {d}/pr-description.md — sections Root cause / Fix / Verified against / Test, "
        f"citing path:lines on the target branch (see {pr_tpl}).\n"
        "Write ONLY those two files. Do NOT push, branch, or open a PR — the driver's "
        "`pdca publish` does the branch/apply/commit/push/draft-PR after you finish."
    )


def _stub_publish(d: Path, cfg: Config) -> None:
    # Offline placeholders, shaped to pass a contribution (T4) gate: summary ≤70,
    # blank line, body ≤80, the configured issue trailer last; PR body has the four
    # sections that pr-description.md.tpl prescribes.
    issue_id = d.name.removeprefix("issue_")
    trailer = cfg.issue_trailer.format(id=issue_id) if cfg.issue_trailer else ""
    body = (
        f"Fix issue {issue_id} (stub contribution artifact)\n\n"
        "Stub commit body for the offline publish slice, wrapped under eighty\n"
        "characters so a contribution gate validates it cleanly.\n"
    )
    if trailer:
        body += f"\n{trailer}\n"
    (d / "commit-msg.txt").write_text(body, encoding="utf-8")
    (d / "pr-description.md").write_text(
        "## Root cause\nstub.\n\n## Fix\nstub.\n\n## Verified against\n"
        "- path:1 — stub.\n\n## Test\nstub regression test.\n\n"
        f"References #{issue_id}\n",
        encoding="utf-8",
    )
