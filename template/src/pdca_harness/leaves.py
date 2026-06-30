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

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import act as act_mod
from . import brief
from . import gates
from . import progress
from . import sources
from . import worktree
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
class LeafError(subprocess.CalledProcessError):
    """A headless leaf exited non-zero. Carries the captured stderr tail
    (``output``) so a failed reviewer/advisory leaf leaves recoverable error text
    in the bundle (#138), and ``produced`` — whether the child emitted a substantive
    stream event (real work) before exiting, vs only the CLI's ``system``/``init``
    or ``api_retry`` events. ``produced is False`` is the transient-infra signal: the
    child died at/near invocation (usage/rate limit, 5xx, auth, network) before doing
    any work, so a retry is likely to succeed."""

    def __init__(self, returncode: int, cmd, output: str = "", produced: bool = False):
        super().__init__(returncode, cmd, output=output)
        self.produced = produced

    @property
    def transient(self) -> bool:
        """A no-output non-zero exit — almost certainly transient infra, not a
        reviewer that looked at the diff and couldn't decide."""
        return not self.produced



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
    rc, output, produced = progress.run_with_heartbeat(
        argv, cwd=workdir, input_text=prompt, label=label, status=status,
        stream_json=use_stream, env=run_env)
    if rc != 0:
        # Only the stream path gives a real "did a session start" signal. Without it
        # (a non-claude leaf) we cannot tell invocation-death from a substantive
        # failure, so report produced=True → not transient, not retried — preserving
        # the prior immediate-placeholder behavior for non-stream leaves.
        raise LeafError(rc, argv, output=output, produced=produced or not use_stream)


def _invoke_leaf_resilient(
    leaf: LeafConfig,
    workdir: Path,
    prompt: str,
    *,
    error_log: Path,
    attempts: int = 3,
    backoff: float = 4.0,
    **kw,
) -> Exception | None:
    """Run a headless reviewer/advisory leaf with bounded retry + error capture (#138).

    A non-zero exit that produced **no output** is the transient-infra signal — the
    child died at/near invocation (usage/rate limit, 5xx, auth, network), not a
    reviewer that read the diff and couldn't decide — so retry it with exponential
    backoff. A failure that *did* produce output, or a non-LeafError (e.g. command
    not found), is substantive: do not retry. On final failure the captured stderr
    tail of every attempt is written to ``error_log`` so the bundle carries
    recoverable error text, not just an exit code. Returns ``None`` on success, else
    the final exception (a :class:`LeafError` exposes ``.transient``)."""
    error_log.unlink(missing_ok=True)  # clear any stale tail from a prior cycle run
    records: list[str] = []
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            _invoke(leaf, workdir, prompt, **kw)
            return None  # success — leave no error log behind
        except Exception as exc:  # noqa: BLE001 — a failed leaf must never crash the cycle
            last = exc
            records.append(_format_leaf_attempt(exc, attempt))
            transient = getattr(exc, "transient", False)
            if not transient or attempt == attempts:
                break
            delay = backoff * (2 ** (attempt - 1))
            print(f"leaves: {workdir.name} — leaf exited {getattr(exc, 'returncode', '?')} "
                  f"with no output (transient); retry {attempt}/{attempts - 1} in "
                  f"{delay:.0f}s", file=sys.stderr)
            time.sleep(delay)
    error_log.write_text("".join(records), encoding="utf-8")
    return last


def _format_leaf_attempt(exc: Exception, attempt: int) -> str:
    """One attempt's record for the error log: the captured stderr tail, or the
    exception text when nothing was captured (e.g. command not found)."""
    tail = (getattr(exc, "output", "") or "").strip()
    rc = getattr(exc, "returncode", "?")
    body = tail if tail else f"(no output captured) {type(exc).__name__}: {exc}"
    return f"----- attempt {attempt} — exit {rc} -----\n{body}\n\n"


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
        rc, _, _ = progress.run_with_heartbeat(
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
    sources.seed(cfg, d)  # seed notes.json + sources/ from the configured providers (#65/#102)
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
    sources_line = (
        f"Also read EVERY file under {d / 'sources'} if that directory exists — the Plan "
        "sources (issue #102) compose the bundle's full context there (the tracker JSON, a "
        "linked proposal / ADR / spec, a CSV row); brief from ALL of it, not just one. "
    )
    citation_line = (
        "Cite the root cause against the target source with `git -C <checkout> log/show "
        "-- <file>` plus Read/Grep on the checkout — NEVER `cd <checkout> && git ...` "
        "(it trips a safety prompt; `git -C` is the safe idiom). Do NOT scan THIS harness "
        "repo for issue information — the tracker is the source. "
    )
    return (
        "You are the Plan leaf of a PDCA cycle. " + src_line + csv_line + notes_line
        + sources_line + citation_line
        + f"Together with the human, write brief.md in the bundle directory {d}. Default "
        f"to {fix_tpl} — it fits bug fixes AND ordinary new functionality. Use {geps_tpl} "
        "(a design proposal) ONLY for the exception: a change significant enough to "
        "warrant a proposal (major architecture / API / UX). Not every feature is a "
        f"design proposal — when in doubt use the normal brief. Use {pointer_tpl} when the "
        "plan ALREADY lives in a host artifact (an ADR / proposal / normative spec): the "
        "brief then POINTS at that document (a `Planning artifact:` reference) instead of "
        "restating it. Keep the parsed `- **Label:** value` field shape; resolve the repo + "
        "branch target per INTEGRATION §2; set `Difficulty` (the fix's blast-radius / "
        "cross-file reach, NOT edge-case density) so Do/review routing can key on it. "
        "One bundle = one brief.md. Plan only."
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
        sources.seed(cfg, cfg.bundle(iid))  # seed notes.json + sources/ per bundle (#65/#102)
    if cfg.planner.mode == "command":
        # On the CSV/default path the planner CHOOSES the ids mid-session, so the per-bundle
        # seed above never ran for them. Snapshot which bundles ALREADY HAD a brief so we can
        # flag any briefed THIS session that the seed never reached — including a brief.md
        # added to a pre-existing UNPLANNED dir, which a dir-name snapshot would miss (#190).
        before = set() if ids else {d.name for d in cfg.bundle_root.glob("issue_*")
                                    if (d / "brief.md").exists()}
        _invoke(cfg.planner, cfg.root, _plan_batch_prompt(cfg, csv, ids))
        if ids is None:
            _warn_unseeded_briefs(cfg, before)
        return
    _stub_plan_batch(cfg, ids)


def _warn_unseeded_briefs(cfg: Config, before: set[str]) -> None:
    """After a CSV/default batch Plan, flag issues briefed THIS session whose Plan sources were
    never seeded (#190).

    On the id-seeded path each bundle's notes/sources are fetched first; on the CSV/default
    path the planner picks the ids *mid-session*, so that per-bundle seed never runs — those
    briefs rest on the CSV row alone, missing the reporter thread / attached repro. ``before``
    is the set of bundles that already carried a ``brief.md`` before this session (NOT just the
    existing dir names — an ``issue_<id>`` dir can pre-exist UNPLANNED and gain its brief now),
    so a bundle is freshly briefed iff it has a brief that ``before`` lacked. We never auto-run
    the seeders unattended (a tracker scraper is human-in-the-loop — a browser, a login), so
    surface it as a VISIBLE sub-step: name the ids and tell the human to seed + refine before
    the work is driven. No-op when no Plan source is configured (the CSV/docs are then the only
    source) or every fresh brief already carries notes.json / a sources/ dir."""
    if not (cfg.notes_cmd or cfg.plan_sources):
        return
    unseeded = sorted(
        d.name.removeprefix("issue_")
        for d in cfg.bundle_root.glob("issue_*")
        if d.name not in before and (d / "brief.md").exists()
        and not (d / "notes.json").exists() and not (d / "sources").is_dir())
    if not unseeded:
        return
    print(
        f"\nplan: {len(unseeded)} issue(s) briefed this session WITHOUT seeded tracker notes "
        f"({', '.join(unseeded)}) — the planner chose them mid-session, so they rest on the CSV "
        f"row alone (no reporter discussion, attached repro, or 'fixed in' hints). Seed their "
        f"notes/sources (your configured Plan source is human-in-the-loop — a browser / login) "
        f"and refine the briefs before driving them; don't let the thin briefs flow on "
        f"unreviewed (#190).",
        file=sys.stderr)


def _plan_batch_prompt(cfg: Config, csv: str | None, ids: list[str] | None = None) -> str:
    fix_tpl = cfg.templates_dir / "brief.md.tpl"
    geps_tpl = cfg.templates_dir / "design-proposal.md.tpl"
    tpl_line = (
        f"use the fitting template: a bug fix → {fix_tpl}; a feature / enhancement → "
        f"{geps_tpl}. Keep the parsed `- **Label:** value` field shape; set `Difficulty` "
        "(the change's blast-radius / cross-file reach, NOT edge-case density) for routing")
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
def attempt_no(d: Path) -> int:
    """This bundle's current Do attempt number (1-based). Mirrors the driver's iteration
    numbering: each iterate archives the prior attempt into ``iteration-v<N>/``, so the
    count of archives + 1 is the attempt about to run."""
    return len(list(d.glob("iteration-v*"))) + 1


def _leaf_from_spec(spec: dict, default: LeafConfig) -> LeafConfig:
    """A LeafConfig from an escalation/variant spec, inheriting any field the spec omits
    from ``default`` (so a variant need only override what differs, e.g. just ``argv``)."""
    return LeafConfig(
        mode=spec.get("mode") or default.mode,
        family=spec.get("family", default.family),
        argv=list(spec.get("argv") or default.argv),
        interactive=bool(spec.get("interactive", default.interactive)),
    )


def _when_matches(when: dict | None, d: Path, *, default: bool) -> bool:
    """The single ``when = {field, substring}`` gate predicate (issue #152): the substring is
    matched case-insensitively against the named brief field. An empty/absent condition
    yields ``default`` — the one thing the callers differ on: an advisory leaf with no
    ``when`` runs (``default=True``), a builder variant with no ``when`` is opt-in
    (``default=False``). Shared by :func:`_advisory_applies` (#64) and :func:`_variant_applies`
    (#134), so the field/substring matching lives in exactly one place."""
    when = when or {}
    needle = (when.get("substring") or "").lower()
    if not needle:
        return default
    return needle in brief.field(d / "brief.md", when.get("field", "")).lower()


def _variant_applies(spec: dict, d: Path) -> bool:
    """True iff this builder variant's ``when`` matches bundle ``d``'s brief (issue #134).
    **Default-open**: a variant with no condition (or an absent/non-matching field) does NOT
    apply, so a missing difficulty tag falls back to the default builder rather than silently
    reducing capability. Delegates to the shared :func:`_when_matches`."""
    return _when_matches(spec.get("when"), d, default=False)


def _routed_variant(d: Path, cfg: Config) -> dict | None:
    """The first ``[[leaves.builder_variant]]`` whose ``when`` matches the brief (issue
    #134), or ``None``."""
    return next((spec for spec in cfg.builder_variants if _variant_applies(spec, d)), None)


def _explicit_model_variant(d: Path, cfg: Config) -> dict | None:
    """The builder variant the brief names by ``- **Do model:** <name>`` (issue #167), or
    ``None``. An explicit per-bundle choice matches a variant's ``model`` key (case-folded)
    and **overrides** the ``when`` routing — so a bundle can pin its Do backend directly,
    no ``when`` gate required. A name matching no variant is a no-op (warned), falling back
    to the ``when`` routing / default builder."""
    if not cfg.builder_variants:  # nothing to match; skip the brief read (no variants ⇒ no-op)
        return None
    wanted = brief.do_model(d / "brief.md")
    if not wanted:
        return None
    for spec in cfg.builder_variants:
        if str(spec.get("model", "")).strip().lower() == wanted.lower():
            return spec
    print(f"leaves: brief 'Do model: {wanted}' matches no [[leaves.builder_variant]] "
          "`model` — using the routed/default builder", file=sys.stderr)
    return None


def select_builder(d: Path, cfg: Config, n: int) -> LeafConfig:
    """Pick the Do builder backend for bundle ``d`` on attempt ``n`` (issues #134/#135/#167).

    Layers over the default ``[leaves.builder]`` (each later one wins):
      1. **Variant pick** — the brief may name a backend **explicitly** via
         ``- **Do model:** <name>`` (#167): the first ``[[leaves.builder_variant]]`` whose
         ``model`` matches is used, overriding the ``when`` routing. Otherwise the first
         variant whose ``when`` matches the brief wins (#134, e.g. difficulty=high).
         Default-open: no explicit name and no ``when`` match keeps the default builder.
      2. **Escalation ladder (#135)** — the entry with the highest ``min_iteration`` ≤ ``n``
         **overrides the variant**, so a bundle that iterates escalates regardless of its
         self-reported difficulty (a hard bundle mis-rated "low" can't loop forever on an
         underpowered executor)."""
    builder = cfg.builder
    spec = _explicit_model_variant(d, cfg) or _routed_variant(d, cfg)  # #167 then #134
    if spec is not None:
        builder = _leaf_from_spec(spec, cfg.builder)
    chosen = -1
    for spec in cfg.builder_escalation:  # escalation OVERRIDES the variant pick (#135)
        threshold = int(spec.get("min_iteration", 0))
        if chosen < threshold <= n:
            chosen = threshold
            builder = _leaf_from_spec(spec, cfg.builder)
    return builder


def _record_loop_attempt(d: Path, n: int, builder: LeafConfig) -> None:
    """Append this Do attempt to ``loop-telemetry.json`` (issue #135) so iterations-to-pass
    and which backend ran each pass are visible. Loop cost ≈ plan + iterations×review (an
    iterate re-runs builder *and* the frontier reviewer), so the attempt count is the
    go/no-go metric for adopting a cheaper local executor. The file persists across
    iterations (it is not archived), so it accumulates. Best-effort: never break Do."""
    path = d / "loop-telemetry.json"
    data: dict = {"attempts": []}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            loaded = None
        # Only adopt a well-shaped prior file; a hand edit / older writer that left a
        # top-level array (or a non-list `attempts`) must not abort Do via AttributeError —
        # this sidecar is best-effort. Anything else is replaced with a fresh dict.
        if isinstance(loaded, dict) and isinstance(loaded.get("attempts"), list):
            data = loaded
    label = builder.argv[0] if builder.argv else builder.mode
    data["attempts"].append({"n": n, "builder": label, "family": builder.family})
    data["iterations_to_pass"] = len(data["attempts"])
    try:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def do_build(d: Path, cfg: Config) -> None:
    # Route the builder FIRST, then dispatch on the SELECTED backend's mode — a variant /
    # escalation entry may set its own mode, so keying the command-vs-stub decision on
    # cfg.builder.mode would run a command variant as a stub (or vice versa) (#134).
    n = attempt_no(d)
    builder = select_builder(d, cfg, n)  # escalate-on-iterate (#135); difficulty (#134)
    if builder.mode == "command":
        _record_loop_attempt(d, n, builder)
        # Isolate Do in a per-cycle worktree off the base (issue #94) so the host's
        # primary checkout is never mutated. Best-effort: None ⇒ edit in place, as before.
        wt = worktree.ensure(d, cfg)
        if wt and builder.family == "claude":
            # The claude builder discovers its `builder` subagent AND the builder_guard
            # PreToolUse hook by walking up from its cwd, so cwd MUST stay the harness root
            # (.claude/agents + .claude/settings live there). Confining its cwd to the
            # worktree would hide both — `--agent builder` would not resolve and the
            # STOP-discipline guard would not load. It is grounded in the worktree via
            # --add-dir + the prompt instead (as in #94), not by cwd. (Family is the
            # SELECTED builder's, so an escalated/variant claude backend gets this too.)
            workdir, env, extra = cfg.root, {"PDCA_WORKTREE": str(wt)}, ["--add-dir", str(wt)]
        elif wt:
            # A non-claude command builder (a local agentic CLI) has no --add-dir / agent
            # machinery, so CONFINE it by running it *in* the worktree (cwd): otherwise it
            # is launched from the harness root with nothing stopping it from writing the
            # host checkout or a sibling repo, breaking one-bundle-one-diff (issue #136).
            workdir, env, extra = wt, {"PDCA_WORKTREE": str(wt)}, None
        else:
            workdir, env, extra = cfg.root, None, None  # best-effort: edit in place, as before
        # Watch the bundle d so the heartbeat shows patch.diff / build-notes.md appearing.
        _invoke(
            builder, workdir, _build_prompt(d),
            label=f"Do {d.name}",
            status=lambda: progress.bundle_activity(d, ("patch.diff", "build-notes.md")),
            stream_json=True,  # Tier 3: show the builder's live tool-use
            env=env, extra_argv=extra,
        )
        return
    _stub_build(d, cfg)


def _build_prompt(d: Path) -> str:
    return (
        f"You are the Do builder. Read {d}/brief.md. If $PDCA_WORKTREE is set, make ALL "
        "target-source edits there — it is an isolated git worktree off the target's base "
        "(the host's primary checkout is NOT touched); cite path:line against it. Build to "
        "satisfy the brief's **Success "
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
        "that, which must still drive the PRODUCTION code, not a copy. If the behaviour "
        "is IRREDUCIBLY GUI/display/IO-bound and no honest headless test can exercise "
        "production, do NOT fabricate a stand-in / mock / parallel re-implementation that "
        "passes vacuously — ship patch.diff, explain in build-notes WHY it isn't "
        "headless-testable plus concrete manual-validation steps, and ship NO test rather "
        "than a fake one (the honest 'unverifiable' result surfaces a NEEDS-HUMAN item in "
        "§6 for the human to validate at sign-off). Make the patch commit-ready for the "
        "TARGET repo: run the project's "
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
    "directory (build-notes.md is deliberately withheld). Write check-review.md: open it "
    "with a one-line outline of the task under review (the bug to fix / functionality to "
    "implement), then a complete verdict table — one row for EVERY element of the "
    "5/5/1 matrix, in order:\n"
    + "\n".join(f"  {elem} — {label}" for elem, label, _kind, _oracle in gates.canonical_elements())
    + "\nFormat it as a Markdown table `| Item | Verdict | Basis |`, the Item column "
    "carrying the element label above, the Verdict one of PASS / FAIL / NEEDS-HUMAN / "
    "N/A, the Basis a one-line reason you re-derived yourself (cite path:line where "
    "you can) — state the DECISION OWED (the context + impact the verdict turns on, "
    "what the human must decide and why), not a restatement of the implementation, "
    "especially for NEEDS-HUMAN rows. Emit NEEDS-HUMAN for the always-human items (validation "
    "fitness-to-purpose, contested root-cause, ambiguous scope) — each NEEDS-HUMAN "
    "row becomes a §6 item the human must clear. Do not omit a row; use N/A with a "
    "reason when an element does not apply. For a visual / manual-repro NEEDS-HUMAN row, "
    "verify what you can yourself — where feasible, exercise the change with the patch "
    "applied at $PDCA_TARGET (run the relevant test, or start/drive the app if the runner "
    "allows), observe, and report; only where it genuinely can't be driven, hand the human "
    "concrete runnable steps, not a bare 'needs manual check'. If a verdict turns on an "
    "investigation, run it and show the result directly — don't ask whether to investigate. "
    "Ground every cited path:line on the target source at $PDCA_TARGET (read-only); "
    "if $PDCA_TARGET is unset, ground against patch.diff alone — do NOT search other "
    "checkouts on the machine. If $PDCA_TARGET is SET yet stale or unreadable (its base "
    "lags what the patch was built/verified against — a dependent/stacked cycle's base "
    "routinely trails its prerequisite until it merges), that is a target-state caveat, "
    "NOT a patch defect: note the staleness and ground the affected citations on "
    "patch.diff. Do NOT present a stale- or unreadable-target 'patch cannot apply / does "
    "not compile' as a blocking C4 (verification) FAIL — that fabricates an ordering-gate "
    "blocker for a patch that is in fact correct."
)


def _reviewer_target(d: Path, cfg: Config) -> Path | None:
    """The local target checkout the reviewer grounds its citations on, or None (#75/#120).

    Prefer the per-cycle **worktree** (#94): it is fetched + pinned to
    ``<base_remote>/<base>`` and carries the patch, so the reviewer grounds on the *same*
    base the gates ran against — not the human's sibling working checkout, which can lag
    ``origin/<base>`` (a false "patch cannot apply" C4) or be sandbox-unreadable (#120).

    When no worktree exists (isolation off / non-git target), fall back to the resolved
    sibling checkout — but first ``git fetch`` it so grounding sees the current base. The
    fetch is **non-destructive** (refs only): never ``reset``/``checkout`` the human's
    working tree. Best-effort: any failure yields None and the reviewer grounds on the diff.
    """
    wt = worktree.path(d, cfg)
    if wt is not None:
        return wt
    from . import publish  # lazy: publish imports leaves, avoid an import cycle
    try:
        repo_spec, _base, _slug = publish._resolve_target(d)
        if not repo_spec:
            return None
        p = publish._checkout_path(cfg, repo_spec)
        if not p.exists():
            return None
        # Refresh refs so a lagging sibling doesn't drift the reviewer's grounding; do NOT
        # touch the working tree (it is the human's checkout). Best-effort.
        subprocess.run(["git", "-C", str(p), "fetch", cfg.base_remote],
                       capture_output=True, text=True)
        return p
    except Exception:  # noqa: BLE001 — grounding access is best-effort, never fatal
        return None


def run_review(d: Path, cfg: Config) -> None:
    inputs = reviewer_input_paths(d)
    assert (d / "build-notes.md") not in inputs, "independence contract violated"

    if cfg.reviewer.mode == "command":
        _run_review_sandboxed(d, cfg)
        return
    _stub_review(d, cfg)


def _seed_sandbox_agents(cfg: Config, sandbox: Path) -> None:
    """Copy the project's ``.claude/agents`` into the sandbox so a leaf running there can
    resolve ``--agent <name>`` (issue #161).

    Claude Code (>= 2.1.x) discovers project subagents by walking **up from the subprocess
    cwd**. The reviewer/advisory leaves run in a temp sandbox cwd (the independence
    contract below), which has no ``.claude/agents`` above it — so ``--agent reviewer``
    fails and the review degrades to a §6 placeholder. Seeding the agent *definitions* into
    the sandbox makes them resolvable while **preserving independence**: only the role
    prompts are copied (never ``build-notes.md``), and the sandbox cwd + each agent's own
    ``tools:`` still gate which files the leaf can read. **Best-effort**: a missing agents
    dir, or a copy error (a dangling symlink / unreadable file under ``.claude/agents``),
    degrades to a no-op — an unresolved ``--agent`` is then handled by the leaf's own
    failure path (a §6 placeholder), never an aborted Check (issue #161 review).
    """
    src = cfg.root / ".claude" / "agents"
    if not src.is_dir():
        return
    try:
        # ignore_dangling_symlinks: a broken link doesn't stop the good agents seeding; the
        # try/except: any other copy error degrades to a no-op rather than aborting Check.
        shutil.copytree(src, sandbox / ".claude" / "agents",
                        dirs_exist_ok=True, ignore_dangling_symlinks=True)
    except (shutil.Error, OSError) as exc:
        print(f"leaves: could not seed sandbox agents from {src} ({exc}); "
              "`--agent` may not resolve", file=sys.stderr)


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
        _seed_sandbox_agents(cfg, sandbox)  # so `--agent` resolves from the sandbox cwd (#161)
        # Ground citations on the brief's target checkout (#75): name it via $PDCA_TARGET
        # so the reviewer doesn't wander into unrelated checkouts, and grant read access
        # for the claude family (--add-dir). Independence holds — the target is the
        # upstream source, not build-notes.md.
        target = _reviewer_target(d, cfg)
        env = {"PDCA_TARGET": str(target)} if target else None
        extra_argv = ["--add-dir", str(target)] if target and cfg.reviewer.family == "claude" else None
        error_log = d / "check-review.error.log"
        # A transient (no-output) reviewer failure is retried with backoff before it
        # degrades to a §6 placeholder; the failed attempts' stderr lands in error_log.
        err = _invoke_leaf_resilient(
            cfg.reviewer, sandbox, _REVIEW_PROMPT,
            error_log=error_log,
            label=f"Check review {d.name}",
            status=lambda: progress.bundle_activity(sandbox, ("check-review.md",)),
            stream_json=True,  # Tier 3 (no-op unless the reviewer family is claude)
            env=env, extra_argv=extra_argv,
        )
        if err is not None:
            transient = getattr(err, "transient", False)
            _review_unavailable(d, f"reviewer leaf failed: {err}",
                                transient=transient, error_log=error_log)
            return
        produced = sandbox / "check-review.md"
        if produced.exists():
            shutil.copy2(produced, d / "check-review.md")
        else:
            _review_unavailable(d, "reviewer produced no check-review.md")


def _review_unavailable(d: Path, reason: str, *, transient: bool = False,
                        error_log: Path | None = None) -> None:
    """Write a placeholder review flagging the gap as a §6 NEEDS-HUMAN, so a failed or
    interrupted reviewer leaves a re-runnable bundle — not a half-checked one that
    crashes assemble. The bundle still reaches sign-off; accept is blocked (C6).

    ``transient`` classifies the placeholder (#138) so the human can tell a transient
    infra blip (safe to re-run) from a reviewer that genuinely needs a human; when an
    ``error_log`` with the failed attempts' output exists, the placeholder points at it."""
    print(f"leaves: {d.name} — advisory review unavailable ({reason})", file=sys.stderr)
    (d / "check-review.md").write_text(
        "# Advisory review — NOT COMPLETED\n\n"
        f"The reviewer did not produce a verdict table ({reason}).\n\n"
        + _unavailable_classification(transient, error_log)
        + "- NEEDS-HUMAN — re-run the Check reviewer; this bundle has no advisory review "
        "and must not be accepted until one exists.\n",
        encoding="utf-8",
    )


def _unavailable_classification(transient: bool, error_log: Path | None) -> str:
    """Shared classification block for a failed reviewer/advisory placeholder (#138):
    name the failure class and point at the captured error log when present."""
    if transient:
        kind = ("**transient infra — safe to re-run.** The leaf exited non-zero with no "
                "output and retries did not recover, so it almost certainly hit a usage/"
                "rate limit or a transient API/network error rather than reviewing the "
                "diff; a sibling advisory leaf of a different family may already have "
                "covered it.")
    else:
        kind = ("**substantive — needs a human.** The leaf ran but did not yield a usable "
                "verdict; do not assume an infra blip.")
    log_ref = ""
    if error_log is not None and error_log.exists():
        log_ref = f" See `{error_log.name}` in this bundle for the captured error."
    return f"Failure class: {kind}{log_ref}\n\n"


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
# Optional advisory reviewer leaves (issue #64) — an OPEN, role-distinct set of extra
# advisory reviewers (e.g. a correctness-bug + reuse/cleanup code-review lens), each a
# reviewer-shaped leaf. Always advisory: they write check-advisory-<id>.md and their
# NEEDS-HUMAN findings route into SUMMARY §6; they never gate. Conditioned per-bundle by
# an optional ``when`` ({field, substring}) brief match — empty ⇒ always run.
# ----------------------------------------------------------------------------
def advisory_artifact(d: Path, leaf_id: str) -> Path:
    """The artifact path an advisory leaf writes (parallel to check-review.md)."""
    return d / f"check-advisory-{leaf_id}.md"


def _advisory_applies(spec: dict, d: Path) -> bool:
    """True iff this advisory leaf should run for bundle ``d``. Its ``when`` ({field,
    substring}) matches a brief field case-insensitively; absent ⇒ always run. Delegates to
    the shared :func:`_when_matches` (issue #152) — one predicate for both the advisory leaf
    and the builder variant, no second implementation."""
    return _when_matches(spec.get("when"), d, default=True)


def _advisory_prompt(spec: dict, leaf_id: str) -> str:
    role = spec.get("role") or "review the patch for correctness bugs and reuse / " \
        "simplification / efficiency cleanups"
    return (
        f"You are an ADVISORY code reviewer — lens: {role}. You have ONLY patch.diff, "
        "brief.md and check-gates.json here (build-notes.md is withheld); ground every "
        "cited path:line on the target source at $PDCA_TARGET, never other checkouts. "
        f"Write check-advisory-{leaf_id}.md: a short list of findings, each a Markdown "
        "bullet with a path:line. For any finding a human must adjudicate, prefix the "
        "bullet '- NEEDS-HUMAN — ' (it becomes a SUMMARY §6 item). You are ADVISORY — you "
        "never gate; the human decides at sign-off. If you find nothing, say so explicitly."
    )


def run_advisory_leaves(d: Path, cfg: Config) -> None:
    """Run each configured advisory reviewer that applies (issue #64). Each writes
    check-advisory-<id>.md; failures degrade to a §6 NEEDS-HUMAN placeholder, never crash
    the cycle (advisory, like the main reviewer)."""
    for spec in cfg.advisory_leaves:
        leaf_id = spec.get("id") or "advisory"
        if not _advisory_applies(spec, d):
            continue
        leaf = LeafConfig(mode=spec.get("mode", "stub"), family=spec.get("family", ""),
                          argv=list(spec.get("argv", [])))
        if leaf.mode == "command":
            _run_advisory_sandboxed(d, cfg, leaf, spec, leaf_id)
        else:
            _stub_advisory(d, spec, leaf_id)


def _run_advisory_sandboxed(d: Path, cfg: Config, leaf: LeafConfig, spec: dict, leaf_id: str) -> None:
    """Run one advisory leaf in a temp dir holding ONLY the reviewer inputs (the same
    independence sandbox as the main reviewer), grounding on $PDCA_TARGET (#75)."""
    with tempfile.TemporaryDirectory(prefix="pdca-advisory-") as tmp:
        sandbox = Path(tmp)
        for name in REVIEWER_INPUTS:
            if (d / name).exists():
                shutil.copy2(d / name, sandbox / name)
        _seed_sandbox_agents(cfg, sandbox)  # so `--agent` resolves from the sandbox cwd (#161)
        target = _reviewer_target(d, cfg)
        env = {"PDCA_TARGET": str(target)} if target else None
        extra = ["--add-dir", str(target)] if target and leaf.family == "claude" else None
        out = sandbox / f"check-advisory-{leaf_id}.md"
        error_log = d / f"check-advisory-{leaf_id}.error.log"
        err = _invoke_leaf_resilient(
            leaf, sandbox, _advisory_prompt(spec, leaf_id),
            error_log=error_log,
            label=f"Advisory {leaf_id} {d.name}",
            status=lambda: progress.bundle_activity(sandbox, (out.name,)),
            stream_json=True, env=env, extra_argv=extra)
        if err is not None:  # advisory must never crash the cycle
            transient = getattr(err, "transient", False)
            _advisory_unavailable(d, leaf_id, f"leaf failed: {err}",
                                  transient=transient, error_log=error_log)
            return
        if out.exists():
            shutil.copy2(out, advisory_artifact(d, leaf_id))
        else:
            _advisory_unavailable(d, leaf_id, "produced no artifact")


def _stub_advisory(d: Path, spec: dict, leaf_id: str) -> None:
    role = spec.get("role") or "correctness bugs + reuse/simplification cleanups"
    advisory_artifact(d, leaf_id).write_text(
        f"# Advisory review — {leaf_id} (stub)\n\nLens: {role}.\n\n"
        "- NEEDS-HUMAN — advisory code-review lens is a stub here; a real "
        f"`{leaf_id}` leaf (family/argv in [[leaves.advisory]]) reviews the patch and "
        "lists findings. The human adjudicates at sign-off.\n",
        encoding="utf-8")


def _advisory_unavailable(d: Path, leaf_id: str, reason: str, *, transient: bool = False,
                          error_log: Path | None = None) -> None:
    print(f"leaves: {d.name} — advisory '{leaf_id}' unavailable ({reason})", file=sys.stderr)
    advisory_artifact(d, leaf_id).write_text(
        f"# Advisory review — {leaf_id} — NOT COMPLETED\n\n"
        + _unavailable_classification(transient, error_log)
        + f"- NEEDS-HUMAN — advisory leaf '{leaf_id}' did not produce findings ({reason}); "
        "re-run it or adjudicate by hand.\n",
        encoding="utf-8")


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
    else:
        _stub_act(cfg, date)
    # Reset the cadence marker (issue #109) whenever the Act beat runs — even if a
    # command-mode Act judged "no delta" and wrote no act-log entry, the review happened.
    act_mod.mark_reviewed(cfg)


def _act_prompt(cfg: Config, date: str) -> str:
    entries = act_mod.index(cfg)
    act_mod.register_signals(cfg, entries, date)  # track recurring signals (#149)
    recs = act_mod.recurrences(cfg, entries)
    index_md = act_mod.render_index(entries, act_mod.patterns(entries),
                                    act_mod.load_ledger(cfg), recs)
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
    act_mod.register_signals(cfg, entries, date)  # track recurring signals (#149)
    recs = act_mod.recurrences(cfg, entries)
    text = act_mod.scaffold_entry(entries, act_mod.patterns(entries), date=date, recs=recs)
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
    # Only build the tracker link for a REAL ticket id — the bare ticket NUMBER (Mantis/GitHub
    # are numeric). A slug bundle (a fork issue, e.g. `820-build-toolchain-coverage`), a
    # `--no-issue` / id_pending placeholder (e.g. `PEND`), or any non-numeric id has no real
    # ticket, so `issue_url_pattern.format(id=…)` would yield a broken link — omit it then,
    # mirroring the trailer's id_pending handling (#192/#196). A non-numeric tracker simply
    # won't auto-link: the safe failure (no broken URL; the bare id still shows).
    real_ticket = issue_id.isdigit()
    issue_url = (cfg.issue_url_pattern.format(id=issue_id)
                 if cfg.issue_url_pattern and real_ticket else "")
    link_clause = (
        f" Hyperlink the tracker ticket as a Markdown link to {issue_url} (link the id — "
        "not just the bare number) so a reader can click through to the report."
        if issue_url else ""
    )
    return (
        "You are the Publish leaf — the closing work of Check. The fix for issue "
        f"{issue_id} is ACCEPTED; with the human, write TWO contribution artifacts in "
        f"{d}, following the project's contributor rules (docs/INTEGRATION.md §4). "
        f"Target: {target}. Read {d}/brief.md + {d}/build-notes.md + {d}/patch.diff for "
        "content; cite the target source with `git -C <checkout>` (never `cd <checkout> "
        f"&& git`). Also read {d}/SUMMARY.md §10 ('Act candidates'): fold any 'PR "
        "description must include …' (or commit-scoped) note into the artifact you write "
        "before drafting; a 'tracker-comment must include …' item is NOT yours (you write "
        "only commit-msg.txt + pr-description.md) — leave it (#177).\n"
        f"1) {d}/commit-msg.txt — a summary ≤70 chars, then a blank line, then the body "
        f"wrapped ≤80; reference any other commit by its FULL hash. {trailer_line}\n"
        f"2) {d}/pr-description.md — open the Summary with the bug's USER-VISIBLE effect "
        "(what the user experiences), then the one-line change + What to look at (for "
        f"non-implementors), then Root cause / Fix, then a Verification claim→evidence "
        f"trail citing path:lines on the target branch; no internal jargon (see {pr_tpl}).{link_clause}\n"
        "Write ONLY those two files. Do NOT push, branch, or open a PR — the driver's "
        "`pdca publish` does the branch/apply/commit/push/draft-PR after you finish."
    )


def _stub_publish(d: Path, cfg: Config) -> None:
    # Offline placeholders, shaped to pass a contribution (T4) gate: summary ≤70,
    # blank line, body ≤80, the configured issue trailer last; PR body has the
    # sections that pr-description.md.tpl prescribes (accessible lead → internals →
    # verification trail, #106).
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
        "## Summary\nstub.\n\n## What to look at\nstub.\n\n## Root cause\nstub.\n\n"
        "## Fix\nstub.\n\n## Verification\n- Claim: stub.\n- Checked: path:1 — stub.\n"
        "- Test: path:1 — stub regression test, fails pre-fix / passes post-fix.\n\n"
        f"References #{issue_id}\n",
        encoding="utf-8",
    )
