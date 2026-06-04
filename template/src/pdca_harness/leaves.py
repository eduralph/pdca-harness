"""The model leaves — the only points where a model is invoked (docs 03 §leaves).

The rest of the pipeline is deterministic code; models fill *artifacts*, never
decide control flow. There are five leaves across the cycle:

* **planner** (Plan, interactive) — the human feeds documents (e.g. a tracker CSV)
  and Claude writes ``brief.md``;
* **builder** (Do, headless) — reads ``brief.md``, writes ``patch.diff`` + the
  named test + ``build-notes.md``;
* **reviewer** (Check, headless) — advisory, decorrelated, writes ``check-review.md``;
* **signoff** (Check sign-off, interactive) — Claude reviews the result *with* the
  human and records the decision token;
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

import shutil
import subprocess
import tempfile
from pathlib import Path

from . import act as act_mod
from . import brief
from . import progress
from .config import Config, LeafConfig

# build-notes.md is DELIBERATELY ABSENT from this list (independence contract).
REVIEWER_INPUTS = ["patch.diff", "brief.md", "check-gates.json"]

# The interactive sign-off leaf writes its decision here; the flow reads it and
# routes it through the C6-guarded signoff.record (never a model-written §9).
SIGNOFF_DECISION = "signoff-decision"
VALID_DECISIONS = frozenset({"accept", "iterate-do", "iterate-plan"})


# ----------------------------------------------------------------------------
# Subprocess invocation — the one place a leaf command is run.
# ----------------------------------------------------------------------------
def _invoke(leaf: LeafConfig, workdir: Path, prompt: str) -> None:
    """Run the leaf's configured command in ``workdir``, feeding it ``prompt``.

    Interactive leaves get the prompt as a *seed positional* (``claude "<prompt>"``)
    and inherit the parent terminal (a REPL); a non-zero exit (the human leaving
    the session) is not fatal. Headless leaves get the prompt on **stdin**, not as
    a trailing positional — a variadic option such as ``--allowedTools`` would
    otherwise swallow the prompt arg (claude then errors "Input must be provided…").
    """
    argv = list(leaf.argv)
    if leaf.interactive:
        subprocess.run(argv + [prompt], cwd=workdir)
        return
    # Headless: feed the prompt on stdin (a trailing positional would be swallowed
    # by a variadic --allowedTools) and tick a heartbeat, since `claude -p` prints
    # nothing until it finishes (minutes) and would otherwise look hung.
    rc, _ = progress.run_with_heartbeat(argv, cwd=workdir, input_text=prompt)
    if rc != 0:
        raise subprocess.CalledProcessError(rc, argv)


# ----------------------------------------------------------------------------
# Leaf 0 — Plan (planner, interactive): human feeds documents → writes brief.md.
# ----------------------------------------------------------------------------
def do_plan(d: Path, cfg: Config, csv: str | None = None) -> None:
    d.mkdir(parents=True, exist_ok=True)
    if cfg.planner.mode == "command":
        _invoke(cfg.planner, cfg.root, _plan_prompt(cfg, csv, d))
        return
    _stub_plan(d, cfg)


def _plan_prompt(cfg: Config, csv: str | None, d: Path) -> str:
    fix_tpl = cfg.templates_dir / "brief.md.tpl"
    geps_tpl = cfg.templates_dir / "design-proposal.md.tpl"
    src = f"The human will share input documents (e.g. the tracker CSV at {csv})." if csv \
        else "The human will share the input documents for this issue."
    return (
        "You are the Plan leaf of a PDCA cycle. " + src + " Together with the human, "
        f"decide what to brief, then write brief.md in the bundle directory {d}. Default to "
        f"{fix_tpl} — it fits bug fixes AND ordinary new functionality. Use {geps_tpl} "
        "(a GEPS-style design proposal) ONLY for the exception: a change significant "
        "enough to warrant a proposal (major architecture / API / UX). Not every "
        "feature is a GEPS — when in doubt use the normal brief. Either way keep the "
        "parsed `- **Label:** value` field shape. One bundle = one brief.md. Plan only."
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


def do_plan_batch(cfg: Config, csv: str | None = None) -> None:
    """Batch Plan: ONE interactive session may brief several issues at once.

    The planner runs in the bundle root and creates an ``issue_<id>/brief.md`` per
    issue it and the human choose from the documents. The flow then discovers the
    new bundles and fans Do+Check over all of them (see ``flow.flow_batch``).
    """
    cfg.bundle_root.mkdir(parents=True, exist_ok=True)
    if cfg.planner.mode == "command":
        _invoke(cfg.planner, cfg.root, _plan_batch_prompt(cfg, csv))
        return
    _stub_plan_batch(cfg)


def _plan_batch_prompt(cfg: Config, csv: str | None) -> str:
    fix_tpl = cfg.templates_dir / "brief.md.tpl"
    geps_tpl = cfg.templates_dir / "design-proposal.md.tpl"
    src = f"the tracker CSV at {csv}" if csv else "the input documents the human shares"
    return (
        "You are the Plan leaf of a PDCA cycle, in BATCH mode. With the human, read "
        f"{src} and decide which issues to brief — there may be SEVERAL. For EACH "
        f"chosen issue create a bundle directory `{cfg.bundle_root}/issue_<id>/` "
        "containing a brief.md — use the fitting template: a bug fix → "
        f"{fix_tpl}; a feature / enhancement → {geps_tpl}. Keep the parsed "
        "`- **Label:** value` field shape; `<id>` is the tracker id. One issue = one "
        "`issue_<id>/brief.md`. Plan only — do not implement."
    )


def _stub_plan_batch(cfg: Config) -> None:
    # Two bundles so the batch flow is exercised offline.
    for iid in ("BATCH1", "BATCH2"):
        d = cfg.bundle(iid)
        d.mkdir(parents=True, exist_ok=True)
        _stub_plan(d, cfg)


# ----------------------------------------------------------------------------
# Leaf 1 — Do (builder, headless): writes patch.diff + the test + build-notes.md.
# ----------------------------------------------------------------------------
def do_build(d: Path, cfg: Config) -> None:
    if cfg.builder.mode == "command":
        _invoke(cfg.builder, cfg.root, _build_prompt(d))
        return
    _stub_build(d, cfg)


def _build_prompt(d: Path) -> str:
    return (
        f"You are the Do builder. Read {d}/brief.md. Produce, in the bundle directory "
        f"{d}: (1) patch.diff — a unified diff against the brief's target branch; "
        "(2) the test file the brief names, red before the fix and green after; "
        "(3) build-notes.md — your rationale (withheld from the reviewer). Cite "
        "path:line on the target branch for every change. Do NOT push, open, or mark "
        "any PR ready."
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
    "directory (build-notes.md is deliberately withheld). Write check-review.md with "
    "per-item verdicts: PASS / FAIL / NEEDS-HUMAN. Re-derive evidence yourself; emit "
    "NEEDS-HUMAN for always-human items (fitness-to-purpose, ambiguous scope)."
)


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
        _invoke(cfg.reviewer, sandbox, _REVIEW_PROMPT)
        produced = sandbox / "check-review.md"
        if produced.exists():
            shutil.copy2(produced, d / "check-review.md")


def _stub_review(d: Path, cfg: Config) -> None:
    # Model-decidable items the reviewer attempts (all PASS in the stub) plus the
    # always-human items it flags NEEDS-HUMAN by design (docs 04 §judgment cell).
    (d / "check-review.md").write_text(
        "# Cross-vendor reviewer (advisory, artifact-only)\n\n"
        f"Reviewer family: {cfg.reviewer.family or 'stub'}. "
        "Inputs: patch.diff, brief.md, check-gates.json (build-notes.md withheld).\n\n"
        "## Per-item verdicts\n"
        "- PASS — re-ran asserted evidence: stub red→green confirmed.\n"
        "- PASS — every cited path:line grounds on the target branch.\n"
        "- PASS — diff stays within one logical fix (model-decidable).\n"
        "- NEEDS-HUMAN — validation fitness-to-purpose: is this the right thing "
        "at all? (always-human by design)\n",
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
        f"into {d}/{SIGNOFF_DECISION}. Do not edit §9 yourself; the driver records it "
        "under a deterministic guard."
    )


def _stub_signoff(d: Path, cfg: Config) -> None:
    # Simulate the human clearing §6 and accepting, so the offline flow completes.
    summary = d / "SUMMARY.md"
    if summary.exists():
        text = summary.read_text(encoding="utf-8")
        summary.write_text(text.replace("- [ ]", "- [x]"), encoding="utf-8")
    (d / SIGNOFF_DECISION).write_text("accept\n", encoding="utf-8")


def signoff_decision(d: Path) -> str:
    """The token the sign-off leaf wrote, or "" if absent/invalid."""
    p = d / SIGNOFF_DECISION
    if not p.exists():
        return ""
    token = p.read_text(encoding="utf-8").strip()
    return token if token in VALID_DECISIONS else ""


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
