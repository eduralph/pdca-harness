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

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import act as act_mod
from . import rubric as rubric_mod
from . import sizing, split
from . import assemble
from . import brief
from . import families
from . import gates
from . import guard
from . import progress
from . import sources
from . import state
from . import worktree
from .config import Config, LeafConfig

# build-notes.md is DELIBERATELY ABSENT from this list (independence contract).
REVIEWER_INPUTS = ["patch.diff", "brief.md", "check-gates.json"]

# The interactive sign-off leaf writes its decision here; the flow reads it and
# routes it through the C6-guarded signoff.record (never a model-written §9).
SIGNOFF_DECISION = "signoff-decision"

# Where a FAILED Do builder leaves its captured error tail (#279) — the Do-side twin of the
# reviewer/advisory `check-*.error.log` (#138), so a failed batch can be post-mortem'd from
# the bundle instead of terminal scrollback.
BUILD_ERROR_LOG = "build.error.log"
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



def _role_injection(
    cfg: Config | None, leaf: LeafConfig, profile: families.FamilyProfile,
) -> tuple[list[str], str]:
    """How the leaf's role prompt (``leaf.agent``) reaches the model: extra argv
    (``role_injection == "flag"`` — the CLI resolves ``.claude/agents/<name>.md``
    itself) or a prompt prefix (``"inline"`` — the file's body, frontmatter
    stripped, prepended to the task prompt). Only active when the leaf names an
    ``agent`` (backward compatibility: existing configs bake ``--agent`` into
    argv and set no ``agent`` key). Best-effort: an unreadable role file degrades
    to no injection, never a crashed leaf."""
    if not leaf.agent or cfg is None:
        return [], ""
    if profile.role_injection == "flag":
        if profile.agent_flag and profile.agent_flag not in leaf.argv:
            return [profile.agent_flag, leaf.agent], ""
        return [], ""
    if profile.role_injection == "inline":
        # The role prompt's canonical, vendor-neutral source of truth is `agents/<name>.md`;
        # `.claude/agents/<name>.md` is Claude-only packaging (frontmatter + the same body)
        # generated from it. Prefer the canonical file; fall back to the legacy
        # `.claude/agents/` location for an instance rendered before the split. strip_frontmatter
        # is a no-op on the frontmatter-less canonical body and still correct on the legacy one.
        canonical = cfg.root / "agents" / f"{leaf.agent}.md"
        legacy = cfg.root / ".claude" / "agents" / f"{leaf.agent}.md"
        path = canonical if canonical.is_file() else legacy
        try:
            body = families.strip_frontmatter(path.read_text(encoding="utf-8")).strip()
        except OSError as exc:
            print(f"leaves: role prompt {path} unreadable ({exc}) — proceeding "
                  "without it", file=sys.stderr)
            return [], ""
        # Migration guard (#228): a pre-split instance kept its role prompt ONLY in the legacy
        # `.claude/agents/<name>.md` and may have CUSTOMIZED it. Now that the canonical file
        # wins, those edits would be silently shadowed. If a legacy file exists and its body
        # diverges from the canonical one we're using, say so — the fix is to migrate the edits
        # into `agents/<name>.md` (the vendor-neutral source), not to leave them stranded.
        if path == canonical and legacy.is_file():
            try:
                legacy_body = families.strip_frontmatter(legacy.read_text(encoding="utf-8")).strip()
            except OSError:
                legacy_body = body
            if legacy_body != body:
                print(f"leaves: {legacy} diverges from the canonical {canonical} and is being "
                      f"ignored — migrate any customizations into agents/{leaf.agent}.md "
                      "(the vendor-neutral role-prompt source).", file=sys.stderr)
        return [], (body + "\n\n---\n\n") if body else ""
    return [], ""


def _mapped_argv(leaf: LeafConfig, profile: families.FamilyProfile,
                 argv: list[str]) -> list[str]:
    """argv additions from the opt-in per-leaf ``model`` / ``effort`` keys, mapped
    through the family profile. Explicit argv is the escape hatch and always wins:
    a flag already present in ``argv`` is never added twice."""
    extra: list[str] = []
    if leaf.model and profile.model_flag and profile.model_flag not in argv:
        extra += [profile.model_flag, leaf.model]
    if leaf.effort and profile.effort_argv:
        rendered = [a.format(effort=leaf.effort) for a in profile.effort_argv]
        # The dedup probe: a "--effort"-style flag, or the key of a "-c key=value" pair.
        probe = rendered[0] if rendered[0].startswith("--") else rendered[-1].split("=", 1)[0]
        if not any(probe in a for a in argv):
            extra += rendered
    return extra


# A single argv string is bounded by the OS, and an oversized interactive SEED overflows it
# with "OSError: [Errno 7] Argument list too long" before the child ever execs. Linux caps a
# single argument at MAX_ARG_STRLEN (~128 KiB) — not total ARG_MAX; Windows caps the WHOLE
# command line at 32,767 characters, which is why this is per-platform rather than one
# "portable" number. A flat POSIX budget would leave the crash intact on a platform the
# template supports (scripts/install.ps1, and the os.name == "nt" branches in act/worktree).
_SEED_ARG_BUDGET = 24 * 1024 if os.name == "nt" else 96 * 1024

#: Prefix for a spilled seed. Dot-prefixed and matched by the rendered `.gitignore`, so the
#: file never shows up as untracked in the instance's tree — keep the two in step (a test
#: asserts it).
_SEED_SPILL_PREFIX = ".pdca-prompt-"


def _seed_positional(prompt: str, workdir: Path) -> tuple[str, Path | None]:
    """The interactive REPL seed, spilling an oversized prompt to a file (issue #313).

    Interactive leaves inherit the TTY to open a REPL, so the prompt cannot ride **stdin**
    the way a headless leaf's does — it goes as ``claude "<seed>"``. The Act leaf is what
    trips the limit first: its prompt embeds the whole cross-cycle ACT INDEX, which grows
    with every frozen cycle (observed at 151,653 bytes on a mature instance), so `pdca flow`
    began dying the moment it auto-ran Act. Any interactive leaf can hit it — a large
    planner or sign-off batch does the same.

    Over budget, the prompt is written to a scratch file **inside ``workdir``** — the REPL's
    cwd, so it reads it with no out-of-tree permission prompt — and the seed becomes a short
    pointer. Under budget the prompt is passed inline, byte-for-byte as before.

    Measured in BYTES, not characters: the OS limit is on the encoded argument, and a prompt
    of mostly non-ASCII would otherwise pass a character-count check and still fail to exec.

    Returns ``(seed, spill|None)``; the caller unlinks ``spill`` once the session ends.
    """
    if len(prompt.encode("utf-8")) <= _SEED_ARG_BUDGET:
        return prompt, None
    fh = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=workdir,
        prefix=_SEED_SPILL_PREFIX, suffix=".md", delete=False)
    with fh:
        fh.write(prompt)
    spill = Path(fh.name)
    seed = (
        "Your full instructions were too large to pass on the command line, so they "
        f"were written to `{spill.name}` in your current directory. Read that file in "
        "full now — it IS your prompt (task and context) — then carry it out."
    )
    return seed, spill


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
    cfg: Config | None = None,
) -> None:
    """Run the leaf's configured command in ``workdir``, feeding it ``prompt``.

    Interactive leaves get the prompt as a *seed positional* (``claude "<prompt>"``)
    and inherit the parent terminal (a REPL); a non-zero exit (the human leaving
    the session) is not fatal. Headless leaves get the prompt on **stdin**, not as
    a trailing positional — a variadic option such as ``--allowedTools`` would
    otherwise swallow the prompt arg (claude then errors "Input must be provided…").

    ``label`` / ``status`` decorate the headless heartbeat (which leaf, and a live
    snapshot of its work — see :func:`progress.bundle_activity`). ``stream_json``
    (Tier 3) asks for the live tool-use stream when the leaf's family profile has
    one (``profile.stream_argv``, e.g. claude's ``--output-format stream-json``);
    families without a stream format ignore it. ``cfg`` enables the profile-driven
    extras (role injection, model/effort mapping, ``[families.*]`` overrides);
    ``None`` falls back to the built-in profile for the leaf's family.
    """
    profile = families.resolve(leaf.family, cfg.families if cfg else None)
    role_argv, prompt_prefix = _role_injection(cfg, leaf, profile)
    argv = list(leaf.argv) + role_argv
    argv += _mapped_argv(leaf, profile, argv)
    argv += list(extra_argv or [])
    prompt = prompt_prefix + prompt
    run_env = {**os.environ, **env} if env else None
    if leaf.interactive:
        # The seed may be spilled to a file when it would blow the OS single-argument
        # limit (#313). `finally` so a non-zero exit or a raising spawn still cleans up;
        # a SIGKILLed session can still orphan one, which is why the name is gitignored.
        seed, spill = _seed_positional(prompt, workdir)
        try:
            subprocess.run(argv + [seed], cwd=workdir, env=run_env)
        finally:
            if spill is not None:
                spill.unlink(missing_ok=True)
        return
    # Headless: feed the prompt on stdin (a trailing positional would be swallowed
    # by a variadic --allowedTools) and tick a heartbeat, since `claude -p` prints
    # nothing until it finishes (minutes) and would otherwise look hung.
    # progress.py's stream reader dispatches on the family's stream_format; a family
    # declaring a format it doesn't recognize runs stream-less (heartbeat Tiers 1+2).
    # tee_stderr regardless: the stream path already tees, and a stream-LESS family
    # (generic, gemini) otherwise captures nothing at all, so its `*.error.log` reads
    # "(no output captured)" — a post-mortem artifact that explains nothing (#286 review).
    use_stream = (stream_json and bool(profile.stream_argv)
                  and profile.stream_format in progress.STREAM_FORMATS)
    if use_stream:
        argv += list(profile.stream_argv)
    rc, output, produced = progress.run_with_heartbeat(
        argv, cwd=workdir, input_text=prompt, label=label, status=status,
        stream_json=use_stream, tee_stderr=True, stream_format=profile.stream_format,
        env=run_env)
    if rc != 0:
        # Only the stream path gives a real "did a session start" signal. Without it
        # (a stream-less family) we cannot tell invocation-death from a substantive
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
    # The seed above can be what FIRST writes notes.json — including a tracker item
    # already settled in-issue (#302 review). Re-check AFTER seeding: a RESOLVED bundle
    # is terminal, and invoking the planner would author a brief that overrides the
    # marker, letting a settled ticket be built and published.
    if state.state(d) == state.RESOLVED:
        print(f"leaves: {d.name} — tracker item is resolved (notes.json `resolved`); "
              "skipping Plan (terminal, #302)", file=sys.stderr)
        return
    if cfg.planner.mode == "command":
        _invoke(cfg.planner, cfg.root, _plan_prompt(cfg, csv, d), cfg=cfg)
    else:
        _stub_plan(d, cfg)
    run_plan_advisory(d, cfg)  # opt-in antagonistic review of the brief (#301); no-op unless configured


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
        "One bundle = one brief.md. Plan only.\n\n"
        # The split belongs to THIS beat and no later one (#358): Do builds what it is
        # given, and Check can only report that what it built is misshapen. Stated in the
        # runtime prompt as well as agents/planner.md because the role file alone has
        # twice proved insufficient — the prompt the model actually receives is built here.
        "If this slice turns out to be several slices, SPLIT IT IN THIS BEAT — a split "
        "produces briefs, and briefs are yours. Run `pdca split "
        f"{issue_id}` to have the splitter draft a proposal, read it with the human, then "
        f"`pdca split {issue_id} --accept`: that files one tracker issue per child as a "
        "sub-issue of this one and materialises a bundle each. You do not leave the "
        "session to file issues by hand. A CSV-DRIVEN batch run re-enumerates every "
        "in-flight bundle from disk after the Plan beat, so it picks the children up and "
        "schedules them into waves by itself — independent ones in parallel, dependent "
        "ones stacked. EVERY OTHER SHAPE, including an explicit id list like `pdca flow "
        "500 501`, drives exactly the ids it was given and never looks for new ones; "
        "`--accept` prints the `pdca flow <child-ids>` command that drives them. Prefer "
        "fewer, larger children: each costs a full cycle."
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
    # Snapshot the briefed set BY CONTENT HASH so the #301 plan-advisory pass covers
    # exactly the bundles THIS session briefed or REWROTE (#301 review round 5 — a
    # name-only snapshot skipped the review when a rerun session updated an existing
    # brief; unchanged resumptions still skip). An unfilled template copy is NOT
    # briefed (round 2 — the same placeholder semantics as state.state(), #113): the
    # session replaces it with a real brief that must get its plan review.
    briefed_before = {d.name: _brief_sha(d) for d in cfg.bundle_root.glob("issue_*")
                      if (d / "brief.md").exists()
                      and not brief.is_placeholder(d / "brief.md")}
    for iid in ids or []:
        sources.seed(cfg, cfg.bundle(iid))  # seed notes.json + sources/ per bundle (#65/#102)
    # RESOLVED trackers are terminal and must not enter the Plan session (#302 review):
    # an authored brief deliberately overrides the marker, so a batch planner briefing
    # one would re-open a settled ticket for Do/Check. Ids are filtered up front (the
    # seed just above may be what first resolved them); the CSV/default path — where the
    # planner picks ids MID-session — is guarded after the session below.
    if ids is not None:
        kept = []
        for iid in ids:
            if state.state(cfg.bundle(iid)) == state.RESOLVED:
                print(f"plan: issue_{iid} — tracker item is resolved; excluded from the "
                      "Plan session (terminal, #302)", file=sys.stderr)
            else:
                kept.append(iid)
        if not kept:
            print("plan: every listed issue is resolved — nothing to brief", file=sys.stderr)
            return
        ids = kept
    resolved_before = {b.name for b in cfg.bundle_root.glob("issue_*")
                       if state.state(b) == state.RESOLVED}
    if cfg.planner.mode == "command":
        # On the CSV/default path the planner CHOOSES the ids mid-session, so the per-bundle
        # seed above never ran for them. Snapshot which bundles ALREADY HAD a brief so we can
        # flag any briefed THIS session that the seed never reached — including a brief.md
        # added to a pre-existing UNPLANNED dir, which a dir-name snapshot would miss (#190).
        before = set() if ids else {d.name for d in cfg.bundle_root.glob("issue_*")
                                    if (d / "brief.md").exists()}
        _invoke(cfg.planner, cfg.root, _plan_batch_prompt(cfg, csv, ids), cfg=cfg)
        if ids is None:
            _warn_unseeded_briefs(cfg, before)
    else:
        _stub_plan_batch(cfg, ids)
    # RESOLVED rejection runs BEFORE the plan-advisory pass: a brief set aside here no
    # longer exists, so the advisory batch never reviews (or revises against) a brief
    # the resolution guard is about to retract.
    _reject_resolved_briefs(cfg, resolved_before)
    # #301: one advisory pass over the freshly briefed OR rewritten bundles, then ONE
    # revision session if any review found something. No-op unless
    # [[leaves.plan_advisory]] is configured.
    fresh = sorted(d for d in cfg.bundle_root.glob("issue_*")
                   if (d / "brief.md").exists()
                   and (d.name not in briefed_before
                        or _brief_sha(d) != briefed_before[d.name]))
    run_plan_advisory_batch(cfg, fresh)


def _reject_resolved_briefs(cfg: Config, resolved_before: set[str]) -> None:
    """Reject a brief the Plan session authored for a bundle that was RESOLVED going in
    (#302 review). On the CSV/default path the planner picks ids MID-session, so the
    up-front id filter cannot protect a resolved tracker; an authored brief would
    override the marker and re-open the settled ticket for Do/Check. The brief is set
    aside (not deleted — the planner's work stays inspectable), loudly, so the bundle
    reads RESOLVED again before the drive set is built.

    Revalidated first (#302 review round 6): the marker is a CACHE of the closure, and
    on this path no up-front id filter ever checked the live tracker — the planner may
    have briefed the item precisely BECAUSE the tracker reopened it. Discarding that
    brief would lock the reopened issue out of every batch run until someone hand-edits
    notes.json. Only the bundles the session actually briefed are checked (one tracker
    call each), never the whole RESOLVED population."""
    for name in sorted(resolved_before):
        b = cfg.bundle_root / name
        bp = b / "brief.md"
        if bp.exists() and state.state(b) != state.RESOLVED:
            if sources.tracker_issue_reopened(cfg, name.removeprefix("issue_")):
                # DEFER, don't drive (#302 review round 10): this brief was authored
                # while the closure-era notes.json was still in place — it never saw
                # the reopen discussion, and keeping it would carry that stale
                # context through Do/Check (and possibly publish) in this very run.
                # Set THIS brief aside, clear the marker + set the notes aside, and
                # the bundle reads UNPLANNED — the next Plan seeds the fresh thread
                # and re-briefs with the reopen context in view.
                # Brief FIRST, marker SECOND (#302 review round 15): clearing the
                # marker while the stale brief could not be moved would leave the
                # bundle reading PLANNED — straight into this run's drive set with
                # the stale context the deferral exists to keep out.
                aside = _brief_aside(bp, "brief.stale-reopen-context")
                if aside is None:
                    # The helper printed what happened; the marker was NOT touched,
                    # so the bundle stays terminal (RESOLVED) — fail closed.
                    continue
                cleared = sources.clear_resolved_marker(b)  # closure-era notes aside
                brief_note = ("the brief aside (" + aside.name + ")"
                              if aside is not bp else "the brief removed")
                if cleared:
                    print(f"plan: {name} — the tracker issue is OPEN again, but this "
                          f"session's brief was authored from the closure-era notes; "
                          f"cleared the stale resolved marker, set the notes aside / "
                          f"{brief_note}, and DEFERRED the bundle — the next Plan "
                          "re-briefs it from the fresh thread", file=sys.stderr)
                else:
                    # #302 review round 11: never claim "cleared" over a failed
                    # rename — the bundle honestly remains RESOLVED (the stale brief
                    # is still set aside: it must not drive in any case).
                    print(f"plan: {name} — the tracker issue is OPEN again, but the "
                          f"closure-era notes could not be set aside; {brief_note} "
                          "and the bundle remains RESOLVED — fix the bundle "
                          "directory, then re-run", file=sys.stderr)
                continue
            aside = _brief_aside(bp, "brief.superseded-by-resolution")
            if aside is None or aside is bp:
                continue  # the helper printed what happened (or the DELETED line)
            print(f"plan: {name} — the session briefed a RESOLVED tracker item; the brief "
                  f"was set aside as {aside.name} (the issue was settled in the tracker; "
                  "reopen it there to plan it again)", file=sys.stderr)


def _brief_aside(bp: Path, stem: str) -> Path | None:
    """Move ``bp`` out of the active brief slot, FAIL CLOSED (#302 review round 14).

    A unique destination per rejection (#302 review round 3) keeps every set-aside
    artifact inspectable. When the rename fails (locked file on Windows, an I/O
    error) the brief is DELETED instead — losing the planner's inspectable copy
    beats the alternative, where an authored brief survives the failed rejection,
    shadows the still-present resolved marker as PLANNED on the next run, and drives
    stale/settled work through Do/Check.

    Returns the set-aside path on a successful rename; ``bp`` ITSELF when the
    fallback deletion emptied the slot (#302 review round 16 — the slot IS empty, so
    a reopen deferral may still proceed to clear the marker; renaming being
    unavailable must not keep suppressing the reopened issue run after run); and
    ``None`` only when the slot could NOT be emptied, after a loud
    manual-intervention line. The helper prints what happened on every non-rename
    path; contained per-bundle — a failure must not abort the batch Plan session's
    remaining bundles."""
    aside = bp.with_name(f"{stem}.md")
    n = 2
    while aside.exists():
        aside = bp.with_name(f"{stem}-{n}.md")
        n += 1
    try:
        bp.rename(aside)
        return aside
    except OSError:
        try:
            bp.unlink()
            print(f"plan: {bp.parent.name} — could not set the brief aside (rename "
                  f"failed); it was DELETED instead so it cannot drive settled/stale "
                  "work", file=sys.stderr)
            return bp  # slot emptied — the caller's deferral/rejection proceeds
        except OSError as exc:
            print(f"plan: {bp.parent.name} — could not set aside OR remove the "
                  f"active brief ({exc}); MANUAL INTERVENTION required: the bundle "
                  f"will read PLANNED over a resolved tracker item until {bp} is "
                  "moved out of the way", file=sys.stderr)
            return None


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
        agent=spec.get("agent", default.agent),
        # NB: a variant spec's `model` key is the #167 SELECTOR name (matched by the
        # brief's `Do model:`), not a CLI model id — so the profile-mapped CLI model
        # is inherited from the default leaf only; a variant sets its model via argv.
        model=default.model,
        effort=spec.get("effort", default.effort),
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


SIZING_FILE = "sizing.json"


def _pointer_clause(d: Path, cfg: Config) -> str:
    """What to tell the sizer about a pointer brief's planning artifact."""
    artifact = brief.planning_artifact(d / "brief.md")
    if not artifact:
        return ""
    resolved = _artifact_path(d, cfg, artifact)
    if resolved is None:
        # Say so rather than naming a path the leaf cannot open: a URL, or an artifact
        # outside the tree. Sizing the pointer alone is then the honest answer, and the
        # verdict is not cached because neither the model nor the digest saw the plan.
        return (f" — the brief points at `{artifact}`, which is not readable from here, so "
                "size what the brief itself states and say in `confidence` that the "
                "authoritative plan was unavailable")
    return (f" AND the planning artifact it points at ({resolved}) — for a pointer brief "
            "THAT document is the plan, and sizing the pointer alone would score a "
            "three-migration project as one small slice")


def _sizer_prompt(d: Path, cfg: Config) -> str:
    return (
        "You are the SIZER. Read " + str(d / "brief.md")
        + _pointer_clause(d, cfg)
        + ". Answer ONE question: "
        "how many INDEPENDENTLY SHIPPABLE outcomes does this brief describe? An outcome is "
        "independently shippable if it could be its own PR — its own defect, its own success "
        "criterion, its own test — without waiting on the others.\n\n"
        "This is the judgment structural features cannot make. Do NOT re-estimate size from "
        "word counts or file counts; the driver already has those. Size is not the question; "
        "DECOMPOSABILITY is.\n\n"
        "Write exactly one file, " + str(d / SIZING_FILE) + ", and nothing else:\n"
        '{"band": "ok|watch|oversized", "independent_outcomes": ["…"], '
        '"proposed_seams": ["…"], "confidence": "low|medium|high"}\n\n'
        "band: `ok` = one outcome. `watch` = arguably two, or one with a large uncertain "
        "surface. `oversized` = two or more that could each ship alone.\n"
        "Propose seams; do NOT cut them — the split is authored in PLAN, by the human, "
        "before Do dispatches."
    )


def _read_sizing(d: Path) -> dict | None:
    """The sizer's verdict, or None if absent/unreadable/not an object.

    Tolerant like every other bundle-file read: a malformed verdict must leave the
    structural estimate exactly as it was, never crash the beat that consulted it.
    """
    p = d / SIZING_FILE
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _sizer_escalates(verdict: dict | None, spec: dict) -> bool:
    """Whether ``spec`` fires against the first-pass verdict.

    Matches on the leaf's own output — band and/or confidence — because that is the only
    place the signal exists. An absent verdict never escalates: a leaf that failed to
    answer is not evidence that a stronger one would.
    """
    if not verdict:
        return False
    bands = [str(b).lower() for b in spec.get("on_band", [])]
    confs = [str(c).lower() for c in spec.get("on_confidence", [])]
    band = str(verdict.get("band", "")).lower()
    conf = str(verdict.get("confidence", "")).lower()
    # OR across the declared conditions, and a spec declaring NEITHER never fires — an
    # empty spec must not escalate every bundle, which is the failure a truthiness test
    # would produce.
    return (bool(bands) and band in bands) or (bool(confs) and conf in confs)


def run_sizer(d: Path, cfg: Config) -> dict | None:
    """Run the cheap-model size judgment over a brief, returning its verdict (#320).

    Optional by construction: with no ``[leaves.sizer]`` in ``pdca.toml`` the leaf is a
    stub and this writes nothing a model produced, so an instance taking a `copier update`
    gains no model call it never asked for.

    Escalation is over the leaf's OWN first pass — a `watch` or low-confidence answer is
    exactly when a stronger model earns its cost, and no brief field predicts that. At most
    one escalation runs: this is a corroborating signal, not a search.
    """
    bp = d / "brief.md"
    if not bp.exists():
        return None
    if cfg.sizer.mode != "command":
        return _stub_sizer(d)

    # One paid verdict per BRIEF, not per beat. The policy is evaluated before Do and
    # again before Check (#321), so a naive re-invoke doubles the cost of every cycle —
    # four calls with an escalation — and lets the second nondeterministic answer overwrite
    # the first. The verdict is a function of the brief, so it is stamped with the brief's
    # digest and reused while that digest holds; an iterate that rewrites the brief changes
    # it and earns a fresh pass. This also subsumes the stale-artifact problem the
    # unconditional unlink was guarding: a verdict from a DIFFERENT brief never matches.
    digest = _sizer_key(d, cfg, bp)
    existing = _read_sizing(d)
    if digest and existing is not None and existing.get("brief_sha") == digest:
        return existing

    verdict = _sizer_pass(cfg.sizer, d, cfg, "sizer")
    for spec in cfg.sizer_escalation:
        if _sizer_escalates(verdict, spec):
            escalated = _sizer_pass(_leaf_from_spec(spec, cfg.sizer), d, cfg,
                                    "sizer (escalated)")
            if escalated is not None:
                return _stamp(d, escalated, digest)
            # An escalation that produced nothing must not discard the first pass: the
            # cheap verdict is still the best evidence available. Restore it to DISK too —
            # the escalation pass unlinks the artifact before running, so returning it only
            # in memory would leave the bundle without the sizing record it did earn.
            return _stamp(d, verdict, digest)
    return _stamp(d, verdict, digest)


def current_sizing(d: Path, cfg: Config) -> dict | None:
    """The stored verdict IF it was given for the brief as it stands now — else None.

    `_read_sizing` is the raw read and does not check the stamp. Every FREE reader — the
    BUILT-time advisory, `pdca size` — must use this instead: `sizing.json` is not archived
    by an iterate, so a bundle re-planned from `oversized` to a small single-outcome brief
    still carries the old verdict on disk. Showing those seams, or folding that band into
    a fresh estimate, states the opposite of the truth about the current brief.

    A verdict whose inputs cannot be fingerprinted (an unfetchable planning artifact) was
    never stamped, so it is not reusable either — the same safe direction `_sizer_key` takes.
    """
    verdict = _read_sizing(d)
    bp = d / "brief.md"
    if verdict is None or not bp.exists():
        return None
    key = _sizer_key(d, cfg, bp)
    return verdict if key and verdict.get("brief_sha") == key else None


def _sizer_key(d: Path, cfg: Config, bp: Path) -> str:
    """The cache key for a sizing verdict, or "" when the inputs cannot be fingerprinted.

    A POINTER brief is the reason this is not just the brief's digest: for those, the
    planning artifact IS the plan and the sizer is told to read it, so hashing `brief.md`
    alone would reuse an `ok` verdict after the artifact grew from one outcome to three —
    suppressing exactly the advisory the pointer case exists to produce.

    An artifact that cannot be read — a URL, or a path outside the tree — yields "" and the
    verdict is NOT cached. Paying for a re-run is the safe direction when the alternative
    is silently trusting a verdict whose input may have changed underneath it.
    """
    h = hashlib.sha256(bp.read_bytes())
    # The CONFIGURATION is an input too. Adding a `[[leaves.sizer_escalation]]` that fires
    # on low confidence, or pointing the leaf at a stronger model, must earn a fresh
    # verdict — otherwise the cached answer from the weaker pass is returned and the
    # escalation the operator just configured never runs.
    h.update(repr([
        (cfg.sizer.mode, cfg.sizer.family, tuple(cfg.sizer.argv), cfg.sizer.agent,
         cfg.sizer.model, cfg.sizer.effort),
        # ORDERED per-spec, not a flattened sorted set: `run_sizer` returns on the FIRST
        # matching escalation, so reordering two rules changes which stronger model runs.
        # Flattening gave both orders the same key, and the cached verdict from the rule
        # that used to win was returned instead of running the one now promoted.
        tuple(tuple(sorted((k, repr(v)) for k, v in spec.items()))
              for spec in cfg.sizer_escalation),
    ]).encode("utf-8"))
    artifact = brief.planning_artifact(bp)
    if not artifact:
        return h.hexdigest()[:16]
    resolved = _artifact_path(d, cfg, artifact)
    if resolved is None:
        return ""
    try:
        h.update(resolved.read_bytes())
    except OSError:
        return ""
    return h.hexdigest()[:16]


def _artifact_path(d: Path, cfg: Config, artifact: str) -> Path | None:
    """The planning artifact as a path the LEAF can open, or None.

    Resolved against the bundle first and then the target checkout, and returned ABSOLUTE
    — the sizer runs with the bundle as its cwd, so handing it the brief's target-relative
    string (`docs/adr/0042.md`) names a file it cannot find. The prompt and the cache key
    both go through here, or the key hashes a document the model never read.

    A URL, or a path that resolves nowhere, yields None: the leaf then sizes the brief
    alone and the verdict is not cached, since neither the model nor the digest can see
    what the pointer points at.

    **CONTAINED to the bundle or the target checkout.** Absolute paths, `..` traversal and
    symlink escapes are refused. `Path(root) / "/etc/passwd"` returns `/etc/passwd` — an
    absolute join silently discards the root — so without this a brief declaring
    `Planning artifact: /etc/passwd` would have the prompt instruct a command-mode sizer,
    with `Read` pre-authorised, to open it.

    The rubric loader already refuses the same shapes, and the argument is stronger here:
    a rubric path comes from `pdca.toml`, which a human wrote, while a planning artifact
    comes from `brief.md`, which a MODEL wrote.
    """
    if not artifact or Path(artifact).is_absolute():
        return None
    for root in (d, rubric_mod._target_root(d, cfg)):
        if root is None:
            continue
        try:
            base = Path(root).resolve()
            candidate = (base / artifact).resolve()
            candidate.relative_to(base)          # refuses `..` and symlink escapes
            if candidate.is_file():
                return candidate
        except (OSError, ValueError):
            continue
    return None


def _stamp(d: Path, verdict: dict | None, digest: str) -> dict | None:
    """Record which brief a verdict was given for, and (re)write it to the bundle.

    Also restores the artifact after a failed escalation: `_sizer_pass` unlinks before each
    run, so a fallback that returned the cheap verdict only in memory left the bundle with
    no sizing record at all.
    """
    if verdict is None:
        return None
    stamped = {**verdict, "brief_sha": digest} if digest else dict(verdict)
    try:
        (d / SIZING_FILE).write_text(json.dumps(stamped, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass  # the stamp is a cache key, never a hard requirement
    return stamped


def _sizer_pass(leaf: LeafConfig, d: Path, cfg: Config, label: str) -> dict | None:
    """One sizer invocation. Never raises, never reuses a previous verdict.

    ADVISORY means advisory: a non-zero exit, a rate limit or a missing executable must
    leave the structural estimate usable rather than abort the beat that consulted it —
    an optional corroborating signal has no business taking the cycle down with it.

    The artifact is unlinked FIRST so a pass that exits cleanly without writing cannot be
    read as having produced the previous run's answer — most likely when an existing
    bundle is switched from stub to command mode, where a stale `ok` would silently stand
    in for a verdict the model never gave.
    """
    (d / SIZING_FILE).unlink(missing_ok=True)
    try:
        _invoke(leaf, d, _sizer_prompt(d, cfg), cfg=cfg, label=label)
    except Exception as exc:  # noqa: BLE001 — an advisory leaf never aborts the beat
        print(f"leaves: {label} did not run ({exc}) — continuing on the structural "
              "estimate alone", file=sys.stderr)
        return None
    return _read_sizing(d)


def _stub_sizer(d: Path) -> dict | None:
    """Offline placeholder: a deterministic `ok` verdict so the suite stays green with no
    model, and so `combine()` is exercised on the stub path exactly as on the real one."""
    verdict = {"band": "ok", "independent_outcomes": [], "proposed_seams": [],
               "confidence": "low", "stub": True}
    (d / SIZING_FILE).write_text(json.dumps(verdict, indent=2) + "\n", encoding="utf-8")
    return verdict


def _split_prompt(d: Path, cfg: Config) -> str:
    tpl = cfg.templates_dir / "split-proposal.md.tpl"
    # READ the sizer's stored verdict, never re-invoke it: the leaf that judged this slice
    # oversized already answered "how many independently shippable outcomes?" and proposed
    # where they divide. Sizing the brief again here would pay a second model to rediscover
    # what the first one wrote down — and the splitter is the one consumer that needs those
    # seams most.
    # `current_sizing`, not the raw read: after an iterate-plan the brief changes while
    # `sizing.json` stays, and handing the splitter seams drawn from a replaced brief tells
    # it the old decomposition describes the current one.
    verdict = current_sizing(d, cfg) or {}
    est = sizing.combine(sizing.estimate(d / "brief.md", cfg), verdict or None)
    # LIST or nothing. The verdict is model output and the contract tolerates an untidy
    # schema — but tolerant has to mean ignored, not iterated: `proposed_seams: 1` raised
    # TypeError here, and `do_split` has already unlinked the previous proposal by then.
    _out = verdict.get("independent_outcomes")
    _seam = verdict.get("proposed_seams")
    outcomes = [str(o) for o in _out] if isinstance(_out, list) else []
    seams = [str(s_) for s_ in _seam] if isinstance(_seam, list) else []
    prior = ""
    if outcomes or seams:
        prior = (
            "\n\nThe sizer has already looked at this brief. Treat its answer as a "
            "STARTING POINT, not a verdict to ratify — it saw only the brief, you may "
            "disagree, and saying so with a reason is more useful than agreeing.\n"
            + ("  outcomes it identified: " + "; ".join(outcomes) + "\n" if outcomes else "")
            + ("  seams it proposed: " + "; ".join(seams) + "\n" if seams else ""))
    return (
        f"You are the SPLITTER. Read {d / 'brief.md'}. This slice has been judged too "
        "large to build as one cycle. The driver sized it "
        f"{est.band}: {'; '.join(est.reasons) or 'no structural signal'}.{prior}\n\n"
        f"Fill {tpl} and write the result to {d / split.PROPOSAL} — exactly one file, "
        "nothing else. Do NOT create bundles, branches or tracker items, and do NOT edit "
        "brief.md. The split is authored in PLAN, by the human: they read your proposal "
        "and run `pdca split <id> --accept`, which files the child issues and materialises "
        "the briefs. You write prose; that command does the rest.\n\n"
        "Each child must be independently shippable — its own defect, success criterion, "
        "test and PR. Prefer fewer, larger children: each costs a full cycle, so a split "
        "into six that could have been two is its own kind of oversizing.\n\n"
        "The `Depends on:` / `Conflicts with:` fields BETWEEN children are the point. Get "
        "them right and the scheduler needs no new code — independent children run in one "
        "parallel wave, dependent ones stack. Keep the `<!-- pdca:child … -->` delimiters "
        "exactly as the template writes them: a child body is a full draft brief and may "
        "contain arbitrary headings and fenced code, so nothing that could appear inside a "
        "child can mark its edge."
    )


def do_split(d: Path, cfg: Config) -> int:
    """Run the splitter leaf over a briefed bundle (#322). Returns a process code."""
    if not (d / "brief.md").exists():
        print(f"split: {d.name} has no brief.md to split", file=sys.stderr)
        return 1
    # A frozen bundle is history. Writing a fresh proposal into a COMPLETE or DISCONTINUED
    # record — and letting --accept overwrite its close marker and build notes — would
    # rewrite an audit trail and spawn work nobody asked for.
    st = state.state(d)
    if st in (state.COMPLETE, state.DISCONTINUED, state.RESOLVED):
        print(f"split: {d.name} is {st} — refusing to split a frozen bundle",
              file=sys.stderr)
        return 1
    # Clear any previous proposal FIRST: `_invoke` ignores an interactive leaf's exit code,
    # so a cancelled rerun would otherwise leave the old file in place and report success,
    # and --accept would materialise a proposal for an earlier version of the brief.
    (d / split.PROPOSAL).unlink(missing_ok=True)
    if cfg.splitter.mode == "command":
        _invoke(cfg.splitter, d, _split_prompt(d, cfg), cfg=cfg, label="splitter")
    else:
        _stub_split(d)
    if not (d / split.PROPOSAL).exists():
        print(f"split: the splitter produced no {split.PROPOSAL} in {d}", file=sys.stderr)
        return 1
    print(f"{d / split.PROPOSAL}")
    return 0


def _stub_split(d: Path) -> None:
    """Offline placeholder: a two-child proposal, the second DEPENDING on the first.

    Deliberately not two independent children: a stub whose output produced a single wave
    would let the round-trip test pass without ever exercising the label→id rewrite, which
    is the part of `--accept` most worth proving.
    """
    (d / split.PROPOSAL).write_text(
        "<!-- pdca:split-proposal v1 -->\n"
        f"# Split proposal — {d.name}\n\n## Wave sketch\n\n"
        "child-2 stacks on child-1 (stub).\n\n"
        "<!-- pdca:child child-1 -->\n"
        "- **Slug:** stub-child-one\n"
        "- **Defect / goal:** the first independently shippable outcome\n"
        "- **Success criterion:** it ships alone\n"
        "- **Test file:** tests/test_one.py\n"
        "- **Difficulty:** low\n"
        "<!-- pdca:end child-1 -->\n\n"
        "<!-- pdca:child child-2 -->\n"
        "- **Slug:** stub-child-two\n"
        "- **Defect / goal:** the second, which builds on the first\n"
        "- **Success criterion:** it ships after child-1\n"
        "- **Test file:** tests/test_two.py\n"
        "- **Difficulty:** low\n"
        "- **Depends on:** child-1\n"
        "<!-- pdca:end child-2 -->\n",
        encoding="utf-8")


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
    # Clear a stale tail from a prior attempt before EITHER backend runs — an iterate-do
    # archives it with its attempt, but a rebuild that didn't archive (a resumed run, a
    # backend switched to stub) would otherwise leave a log at the top level that describes
    # a failure this build never had (#280 review).
    error_log = d / BUILD_ERROR_LOG
    error_log.unlink(missing_ok=True)
    if builder.mode != "command":
        _stub_build(d, cfg)
        return
    # The capture wraps the WHOLE of Do — its SETUP as well as the leaf invocation. Do can
    # die before the leaf ever launches, and the most likely way is `worktree.ensure`, which
    # deliberately raises WorktreeError when the target's base ref doesn't resolve (#235,
    # fail-closed — it refuses to run Do in the operator's primary checkout). In a wave batch
    # that is precisely what an unpushed folded base looks like. Wrapping only `_invoke` left
    # those failures with NO bundle-local trace at all — worse than before, since the stale
    # log was already cleared above — so a post-mortem was back to terminal scrollback for the
    # one failure mode most likely to hit a whole wave (#286 review).
    try:
        # The lane lock (#296 review) spans ensure + the whole builder invocation, so an
        # out-of-band gate read can never reconstruct the lane under the builder's feet
        # (it fails closed "lane busy" instead). Blocking: Do waits out a transient gate.
        with worktree.lane_lock(d, cfg, wait=True):
            _do_build_command(d, cfg, builder, n)
    except Exception as exc:  # noqa: BLE001 — capture, then re-raise for the caller
        try:
            error_log.write_text(_format_leaf_attempt(exc, 1), encoding="utf-8")
            print(f"leaves: {d.name} — Do failed; captured the error tail in "
                  f"{BUILD_ERROR_LOG}", file=sys.stderr)
        except OSError:
            pass  # never let error-capture mask the real failure
        raise


def _do_build_command(d: Path, cfg: Config, builder: LeafConfig, n: int) -> None:
    """Run Do on a command backend: set up isolation, then invoke the leaf.

    Every failure here — setup or invocation — is captured to `build.error.log` by the
    caller and re-raised, so `flow._isolate` still contains it and drops just this bundle.
    """
    _record_loop_attempt(d, n, builder)
    # Isolate Do in a per-cycle worktree off the base (issue #94) so the host's
    # primary checkout is never mutated. Best-effort for the cases isolation can't apply
    # (None ⇒ edit in place); a real checkout whose base ref won't resolve RAISES (#235).
    wt = worktree.ensure(d, cfg)
    profile = cfg.profile(builder)
    if wt and profile.cwd_discovery:
        # A cwd-discovery family (claude) finds its subagents AND the builder_guard
        # PreToolUse hook by walking up from its cwd, so cwd MUST stay the harness root
        # (.claude/agents + .claude/settings live there). Confining its cwd to the
        # worktree would hide both — `--agent builder` would not resolve and the
        # STOP-discipline guard would not load. It is grounded in the worktree via
        # the profile's grounding flag + the prompt instead (as in #94), not by cwd.
        # (The profile is the SELECTED builder's, so an escalated/variant claude
        # backend gets this too.)
        extra = [profile.grounding_flag, str(wt)] if profile.grounding_flag else None
        workdir, env = cfg.root, {"PDCA_WORKTREE": str(wt)}
    elif wt:
        # Other command builders (codex, a local agentic CLI) have no cwd-walking agent
        # machinery, so CONFINE them by running *in* the worktree (cwd): otherwise the
        # leaf is launched from the harness root with nothing stopping it from writing
        # the host checkout or a sibling repo, breaking one-bundle-one-diff (issue #136).
        # But the builder must ALSO read brief.md and write its artifacts (patch.diff /
        # the test / build-notes.md) in the BUNDLE dir, which is outside that cwd — and a
        # sandboxing family (codex `--sandbox workspace-write`) can only write cwd + roots
        # granted with its grounding flag. So grant the bundle dir as an extra writable
        # root (#230); a family with no grounding flag (generic) is unsandboxed and reaches
        # it anyway. cwd stays the worktree, so #136 still confines source edits.
        workdir, env = wt, {"PDCA_WORKTREE": str(wt)}
        extra = [profile.grounding_flag, str(d)] if profile.grounding_flag else None
    else:
        workdir, env, extra = cfg.root, None, None  # best-effort: edit in place, as before
    if not profile.native_guard:
        # A family without its own PreToolUse STOP hook gets the driver's `gh`
        # PATH shim — the same builder_guard rules, enforced vendor-neutrally.
        env = guard.shim_env(cfg, env)
    # Watch the bundle d so the heartbeat shows patch.diff / build-notes.md appearing.
    _invoke(
        builder, workdir, _build_prompt(d, cfg, worktree_root=wt),
        label=f"Do {d.name}",
        status=lambda: progress.bundle_activity(d, ("patch.diff", "build-notes.md")),
        stream_json=True,  # Tier 3: show the builder's live tool-use
        env=env, extra_argv=extra, cfg=cfg,
    )


def _build_prompt(d: Path, cfg: Config | None = None, *,
                  worktree_root: Path | None = None) -> str:
    # The target repo's standing rubric (#314), so the builder self-reviews against
    # the same criteria the reviewer will apply — the asymmetry that costs a
    # guaranteed round. "" when unconfigured, so the prompt is byte-identical.
    # APPENDED, not prepended (#314 review): prefixing glued the rubric's last rule
    # straight onto "You are the Do builder…" with no separator, merging the two
    # instructions. The task prompt also reads better first — the rubric is a standing
    # constraint on the work, not the framing for it.
    # `worktree_root` is what `worktree.ensure` ACTUALLY returned — None when setup failed
    # and `_do_build_command` fell back to running in place. Passing it explicitly is the
    # only way the rubric lookup can tell "this lane is mine and live" from "this lane is
    # mine and stale": a failed ensure() leaves the directory and its owner stamp behind,
    # so an ownership check alone would still prefer a tree the builder is not editing.
    rubric = (rubric_mod.for_builder(d, cfg, worktree_root=worktree_root)
              if cfg is not None else "")
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
    ) + rubric


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


# The ONLY `sandbox.network` keys the driver will carry into a leaf's temp cwd, each with the
# value shape that counts as a real grant (issues #261, #277). An allow-list, not a copy: a
# key absent from here — above all `sandbox.excludedCommands`, which makes a command bypass
# the sandbox entirely — is never seeded, however an instance configures it. A grant whose
# value fails its filter (an empty domain list, a non-boolean) seeds nothing, which is how a
# knob ships documented-but-OFF.
_SEEDED_NETWORK_KEYS = {
    "allowLocalBinding": lambda v: isinstance(v, bool),                    # #261 loopback bind
    "allowedDomains": lambda v: isinstance(v, list) and bool(v),           # #277 e.g. github
    "deniedDomains": lambda v: isinstance(v, list) and bool(v),            # its counterpart
}


def _sandbox_argv(cfg: Config, profile: families.FamilyProfile, *,
                  seeded: bool) -> list[str]:
    """Every sandbox flag this leaf's family needs, for the grants the instance opted into.

    ``seeded`` gates ONLY the claude confinement flag, never the codex network grant. The
    two depend on entirely different things, and conflating them breaks one of them:

    * the confinement flag (:func:`_settings_scope_argv`) is meaningless AND DANGEROUS
      without the seeded settings file on disk — it drops the operator's ambient sandbox in
      favour of a project scope that does not exist, leaving the leaf wholly unconfined
      (#290). A failed seed therefore withholds it: fail closed.
    * the codex network grant rides on ``argv``, and codex never reads that file at all, so
      a failed write says nothing about it. Gating it on ``seeded`` would silently kill a
      codex leaf's Docker access because of a claude-shaped failure it has no stake in.

    Two grants, two shapes, because the vendors' sandboxes differ and neither is strictly
    tighter (#291) — so they are separate opt-ins, each named for what it actually does:

    * ``[leaves.sandbox] unsandboxed_commands`` (claude) — a NAMED command leaves the sandbox
      entirely; every other command stays confined. Realized by the seeded ``excludedCommands``
      + the confinement flags from :func:`_settings_scope_argv`.
    * ``[leaves.sandbox] network_access`` (codex) — ``--sandbox workspace-write`` has no
      per-command escape, and its docker-socket denial is **seccomp, not filesystem** (a relayed
      socket in a granted writable dir is still refused), so only opening the network layer
      works. That frees the socket/network layer for EVERY command in the leaf, while the
      filesystem stays confined for every command. It cannot be scoped to one command, which is
      exactly why it does not ride on ``unsandboxed_commands`` — that key promises "only these
      commands leave the sandbox", and this would not keep the promise.

    claude deliberately takes no ``network_argv``: it scopes network by DOMAIN instead
    (``allowedDomains``, #277), which is strictly better where it exists.
    """
    argv = _settings_scope_argv(cfg, profile) if seeded else []
    if cfg.leaf_network_access and profile.network_argv:
        argv += list(profile.network_argv)
    return argv


def _settings_scope_argv(cfg: Config, profile: families.FamilyProfile) -> list[str]:
    """Flags confining the leaf to the settings the harness SEEDS — nothing of the operator's.

    Only when an exemption is granted, and only for a family that has such a flag (claude:
    ``--setting-sources project``). Without it the seeded ``sandbox.excludedCommands`` is a
    floor rather than a ceiling: array settings CONCATENATE across scopes and the union is
    monotonic, so the operator's own ``~/.claude/settings.json`` exemptions merge into the
    leaf and nothing can remove them (PR #288 review). Dropping the user scope also stops the
    operator's ``permissions`` and ``allowedDomains`` riding in the same way.

    The cost is that the leaf no longer sees user-scope settings at all, so an instance whose
    **auth** lives there (``apiKeyHelper``, ``env.ANTHROPIC_API_KEY``) must move it into the
    environment. That fails loudly at leaf start — and now lands in ``check-*.error.log``.
    """
    if cfg.leaf_unsandboxed_commands and profile.settings_scope_argv:
        return list(profile.settings_scope_argv)
    return []


def _seed_sandbox_settings(cfg: Config, sandbox: Path,
                           profile: families.FamilyProfile) -> bool:
    """Carry the sandbox capabilities a Check needs into the leaf sandbox (#261, #277).

    Claude Code loads **project** settings from ``.claude/settings.json`` relative to the
    subprocess cwd — the same walk-up that finds ``.claude/agents`` (#161). The reviewer /
    advisory leaves run in a temp cwd, so the rendered project's ``.claude/settings.json``
    is invisible to them and its ``sandbox`` policy silently does not apply. Two capabilities
    a Check legitimately needs are denied as a result:

    * ``network.allowLocalBinding`` (#261) — without it the leaf's Bash tool runs under
      Claude Code's bubblewrap+seccomp sandbox where ``TcpListener::bind("127.0.0.1:0")``
      fails ``Operation not permitted``, so every loopback-socket runtime test panics before
      its assertion and C2/C4/T3 can only ever be *provisional*.
    * ``network.allowedDomains`` (#277) — the reviewer's prior-art check needs the
      closed/rejected-PR corpus (``gh pr list --state closed`` → api.github.com). Blocked, it
      cannot be settled mechanically and is forced NEEDS-HUMAN on *every* bundle.

    Separately, a **Docker-backed conformance gate** (a live etcd/TiKV/FDB cluster via
    ``docker compose``) is denied the docker socket inside the sandbox even on a Docker-capable
    host, so its runtime evidence can never be earned at Check and always defers to a
    human-run confirmer — the process gets burdensome exactly where it should be mechanical
    (#276). The fix is NOT a socket-wide grant (``allowAllUnixSockets`` would hand *every*
    Bash line the leaf writes access to *every* unix socket — and a root-owned docker daemon
    is root-adjacent). It is a **named-command exemption**: ``[leaves.sandbox]
    unsandboxed_commands`` in pdca.toml lists the conformance commands, and only those run
    outside the sandbox. Everything else the leaf does stays confined — which holds only
    because the exemption ships with ``allowUnsandboxedCommands: false`` beside it; the list
    is a *ceiling*, not a floor (see below).

    That list is **harness-owned on purpose**. This function never copies the project's own
    ``sandbox.excludedCommands`` — that is the operator's *gate* workaround, and inheriting it
    would let the leaf run whatever the operator exempted for CI (PR #268). A leaf's exemption
    is declared once, deliberately, in pdca.toml.

    **Seeded through an ALLOW-LIST of individual keys** (:data:`_SEEDED_NETWORK_KEYS`), never
    by copying the ``sandbox`` block, and never ``permissions``. Each wider copy would hand
    the leaf a capability its ``tools:`` frontmatter does not grant: ``permissions.allow``
    carries ``Edit``/``Write``, and ``sandbox.excludedCommands`` — which docs 05 recommends to
    a project as the workaround for its *gates* — makes the named command bypass the sandbox
    **entirely**, so a reviewer could run the test runner unconfined (PR #268 review). Widening
    the seed means adding a key here, deliberately — not loosening the copy.

    Each key is **value-filtered**, so a present-but-empty grant seeds nothing: that is how a
    grant stays OFF by default (the shipped ``allowedDomains: []`` documents the knob without
    enabling it). Nothing granted at all ⇒ no file written, so an instance that configures no
    sandbox is unaffected.

    The two sources are **independent**. The network grants are the project's, read from its
    ``.claude/settings.json`` best-effort; the command exemptions are the harness's, read from
    ``pdca.toml``. An absent or unparseable settings file costs the network grant and nothing
    else — it must never suppress a pdca.toml exemption (PR #288 review). Best-effort
    throughout, like the agent seeding: any read/parse/write error degrades to a no-op, never
    an aborted Check.

    Scope: this covers the reviewer / advisory **leaves**. Gate commands are plain
    subprocesses of ``pdca`` and inherit the operator's ambient sandbox instead (docs 05).
    The **codex** family sandbox (``codex exec --sandbox workspace-write``) is not configured by
    this file at all — it reads none of it. Its grants ride on ``argv`` instead: ``[leaves.sandbox]
    network_access`` opens its socket/network layer, which is the only thing that reaches the
    docker socket *or* api.github.com there (#291, :func:`_sandbox_argv`).
    """
    src = cfg.root / ".claude" / "settings.json"
    granted: dict = {}

    # The NETWORK grants are the project's (claude reads them from its own settings.json), so
    # they are read from there — best-effort. An absent or unparseable file means no network
    # grant, and nothing more: it must not suppress the harness-owned exemptions below.
    if src.is_file():
        try:
            settings = json.loads(src.read_text(encoding="utf-8"))
            network = (settings.get("sandbox") or {}).get("network") or {}
            net_granted = {key: network[key] for key, valid in _SEEDED_NETWORK_KEYS.items()
                           if key in network and valid(network[key])}
            if net_granted:
                granted["network"] = net_granted
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            print(f"leaves: could not read sandbox settings from {src} ({exc}); the leaf gets "
                  "no network grant", file=sys.stderr)

    # A leaf's sandbox EXEMPTIONS are HARNESS-owned — `[leaves.sandbox] unsandboxed_commands`
    # in pdca.toml (#276) — and NEVER this settings file's own ``excludedCommands``, which is
    # the operator's *gate* workaround and must not be inherited by a leaf (#268). Because
    # they are the harness's, they must not depend on the project having (or being able to
    # parse) a `.claude/settings.json` AT ALL: gating them on that made the documented Docker
    # exemption silently do nothing for an instance without one (PR #288 review).
    #
    # An exemption LIST alone does not bound what escapes the sandbox. TWO holes, and BOTH
    # must be closed or "only these commands run outside the sandbox" — the promise made in
    # this docstring, in docs 05 and in pdca.toml — is not true (PR #288 review):
    #
    # 1. `allowUnsandboxedCommands` defaults to TRUE (settings schema, v2.1.207:
    #    `sandbox?.allowUnsandboxedCommands ?? true`), and while true the model may retry ANY
    #    sandbox-denied command with the `dangerouslyDisableSandbox` parameter and have it run
    #    unconfined. False makes that parameter "completely ignored" (the schema's own words).
    #    It is a SCALAR, so the seeded project scope genuinely overrides the operator's.
    # 2. Array-valued settings CONCATENATE across scopes (user → project → local → managed):
    #    the CLI folds each scope through a merge customizer that unions any two arrays, and
    #    that union is MONOTONIC — no scope, not even managed policy, can remove what a lower
    #    one added. So the operator's own `~/.claude/settings.json` `excludedCommands` (their
    #    INTERACTIVE exemptions — a broad `docker *`) merges straight into the leaf, and a
    #    seeded list can only ever be a FLOOR. The one way to bound it is to not load the lower
    #    scope at all: the family's `settings_scope_argv` (claude: `--setting-sources
    #    project`), applied by the callers. A family without that flag cannot be bounded, so the
    #    exemption is REFUSED rather than granted unbounded — fail closed, and say why.
    #
    # …and a THIRD hole, which swallows the other two whole (#289). When `sandbox.enabled` is
    # true but the sandbox's own dependencies are missing, Claude Code does NOT fail — it
    # DISABLES the sandbox, warns, and runs every command unconfined ("Sandbox disabled:
    # …dependencies are missing: socat not installed · Commands will run WITHOUT sandboxing").
    # A bounded exemption on top of no sandbox at all is not bounded; it is nothing. So seed
    # `failIfUnavailable` — "Exit with an error at startup if sandbox.enabled is true but the
    # sandbox cannot start" (its schema) — and let the leaf REFUSE rather than run unconfined
    # under a boundary this file, docs 05 and pdca.toml all claim it has. It fails loudly, and
    # the tail lands in the bundle's `*.error.log` (#280/#286) instead of scrollback. `pdca
    # doctor` catches the same gap BEFORE a run; this catches the operator who skipped it.
    if cfg.leaf_unsandboxed_commands:
        if profile.settings_scope_argv:
            # `enabled` FIRST — without it none of the rest means anything, and this seed was
            # worse than useless (PR #290 review). `sandbox.enabled` defaults to FALSE
            # (`sandbox?.enabled ?? false`), and `failIfUnavailable` is gated on it
            # (`enabled && … && failIfUnavailable`). Worse: `--setting-sources project` drops
            # the user/local scope, which is exactly where an operator's `sandbox.enabled: true`
            # lives — so BOUNDING the exemption was REMOVING the sandbox it claims to bound. The
            # leaf ran fully unconfined and the fail-closed guard never fired. Verified: with
            # these keys but no `enabled`, a leaf starts silently on a socat-less host; with it,
            # it refuses — "sandbox required but unavailable … refusing to start without a
            # working sandbox".
            granted["enabled"] = True
            granted["excludedCommands"] = list(cfg.leaf_unsandboxed_commands)
            granted["allowUnsandboxedCommands"] = False
            granted["failIfUnavailable"] = True
        else:
            # The posture line must describe the posture the leaf ACTUALLY gets. With
            # `network_access` also set, this same run appends the network grant a few lines
            # later — so "the leaf stays fully sandboxed" was a lie whenever BOTH keys were
            # configured, and a warning that misstates the active security posture is worse than
            # no warning at all (PR #292 review, local pass).
            if cfg.leaf_network_access and profile.network_argv:
                posture = ("The leaf keeps its FILESYSTEM confinement — but `network_access = "
                           "true` is set, so its socket/network layer IS open, for every command "
                           "it runs and not just the named ones.")
            elif profile.network_argv:
                posture = ("The leaf stays fully sandboxed. For codex, use `[leaves.sandbox] "
                           "network_access = true` instead: its sandbox has no per-command "
                           "escape, and its docker-socket denial is the network layer, not the "
                           "filesystem (#291).")
            else:
                posture = "The leaf stays fully sandboxed."
            print("leaves: [leaves.sandbox] unsandboxed_commands is set, but the "
                  f"'{profile.name}' family cannot be confined to the harness's own settings, "
                  f"so a per-command exemption cannot be bounded — NOT granted. {posture}",
                  file=sys.stderr)

    if not granted:
        return True   # nothing promised, nothing to seed
    try:
        dest = sandbox / ".claude"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "settings.json").write_text(
            json.dumps({"sandbox": granted}, indent=2), encoding="utf-8")
    except OSError as exc:
        # FAIL CLOSED (PR #290 review). This used to warn "the leaf runs under the ambient
        # sandbox policy" and carry on — the exact OPPOSITE of what happened. The caller still
        # passed `--setting-sources project`, so the leaf loaded ONLY project scope … which is
        # this file, which does not exist. No `sandbox.enabled` (it defaults FALSE), and the
        # operator's own user-scope sandbox dropped along with it: the leaf ran COMPLETELY
        # unconfined, under a message asserting it was protected.
        #
        # False makes the caller WITHHOLD `--setting-sources`, so the leaf keeps the operator's
        # ambient sandbox. The exemption then simply does not happen and a Docker-backed leg
        # defers to a human, exactly as when none is configured. Degrade the FEATURE, never the
        # BOUNDARY.
        print(f"leaves: could not seed sandbox settings into {sandbox} ({exc}); the exemption "
              "did NOT take effect — the leaf keeps the operator's ambient sandbox and a "
              "Docker-backed leg will defer to a human", file=sys.stderr)
        return False
    return True


def _seed_plan_sandbox_settings(sandbox: Path, profile: families.FamilyProfile) -> bool:
    """A MINIMAL fail-closed sandbox policy for the plan reviewer (#301 review round 8).

    Withholding :func:`_seed_sandbox_settings` from plan reviews (round 6 — the Check
    opt-ins must not extend to them) left the temp cwd with NO settings file at all,
    and claude's ``sandbox.enabled`` defaults to FALSE — so a Bash-capable
    plan-reviewer agent ran with no sandbox and the claimed "brief/notes/sources +
    pinned target" boundary was prose, not policy. Seed the sandbox ON with NONE of
    the Check grants: no ``excludedCommands``, no network keys —
    ``allowUnsandboxedCommands: false`` (the retry escape hatch stays ignored) and
    ``failIfUnavailable: true`` (a socat-less host REFUSES rather than running
    unconfined under a claimed boundary, #289/#290).

    Returns whether the seed landed, so the caller passes the confinement flag
    (``--setting-sources project`` — dropping the operator's user scope, whose own
    ``excludedCommands`` would otherwise union in monotonically, #288) exactly iff
    the seeded file exists; on a failed write the flag is withheld and the leaf
    keeps the operator's ambient sandbox (degrade the feature, never the boundary).
    Families without a settings mechanism (codex: its default workspace-write
    sandbox is its own, argv-configured) need no seed: False."""
    if not profile.settings_scope_argv:
        return False
    try:
        dest = sandbox / ".claude"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "settings.json").write_text(
            json.dumps({"sandbox": {"enabled": True,
                                    "allowUnsandboxedCommands": False,
                                    "failIfUnavailable": True}}, indent=2),
            encoding="utf-8")
        return True
    except OSError as exc:
        print(f"leaves: could not seed the plan-review sandbox into {sandbox} ({exc}); "
              "the confinement flag is withheld — the leaf keeps the operator's ambient "
              "sandbox", file=sys.stderr)
        return False


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
        profile = cfg.profile(cfg.reviewer)
        # Seed unconditionally: flag families need it to resolve `--agent` (#161);
        # for inline families it is harmless (role prompts only, never build-notes).
        _seed_sandbox_agents(cfg, sandbox)
        # …and the project's sandbox policy, which is likewise invisible from a temp cwd
        # (#261) — without it a loopback-socket runtime test can't bind, so it can never
        # earn an automated red→green at Check.
        seeded = _seed_sandbox_settings(cfg, sandbox, profile)
        # Ground citations on the brief's target checkout (#75): name it via $PDCA_TARGET
        # so the reviewer doesn't wander into unrelated checkouts, and grant read access
        # via the family's grounding flag (claude: --add-dir). Independence holds — the
        # target is the upstream source, not build-notes.md.
        target = _reviewer_target(d, cfg)
        env = {"PDCA_TARGET": str(target)} if target else None
        extra_argv = ([profile.grounding_flag, str(target)]
                      if target and profile.grounding_flag else [])
        # The confinement flag rides on `seeded` (a file that is not there must not cost
        # the leaf its ambient sandbox, #290); the codex network grant does not (#291).
        extra_argv += _sandbox_argv(cfg, profile, seeded=seeded)
        error_log = d / "check-review.error.log"
        # A transient (no-output) reviewer failure is retried with backoff before it
        # degrades to a §6 placeholder; the failed attempts' stderr lands in error_log.
        err = _invoke_leaf_resilient(
            cfg.reviewer, sandbox, _REVIEW_PROMPT + rubric_mod.for_reviewer(d, cfg),
            error_log=error_log,
            label=f"Check review {d.name}",
            status=lambda: progress.bundle_activity(sandbox, ("check-review.md",)),
            stream_json=True,  # Tier 3 (no-op unless the reviewer family has a stream)
            env=env, extra_argv=extra_argv, cfg=cfg,
        )
        if err is not None:
            _review_unavailable(d, f"reviewer leaf failed: {err}",
                                failure=_failure_class(err), error_log=error_log)
            return
        produced = sandbox / "check-review.md"
        if produced.exists():
            shutil.copy2(produced, d / "check-review.md")
        else:
            _review_unavailable(d, "reviewer produced no check-review.md")


# How a reviewer / advisory leaf failed (#138, #278). The split that matters downstream is
# INFRA (nothing reviewed the diff) vs SUBSTANTIVE (it reviewed, and yielded nothing usable) —
# but the two infra shapes need different *actions* from the operator, so keep them distinct.
_FAIL_TRANSIENT = "transient"      # ran, exited non-zero with no output; retries exhausted
_FAIL_STARTUP = "startup"          # never ran at all — the command could not be launched
_FAIL_SUBSTANTIVE = "substantive"  # ran and produced output, but no usable verdict


def _failure_class(exc: Exception | None) -> str:
    """Classify a failed leaf invocation.

    A :class:`LeafError` means the child actually ran: ``transient`` (no output — a rate
    limit / 5xx / network blip) or substantive. But a **startup** failure never produces a
    LeafError at all — the spawn raises ``FileNotFoundError`` before one exists, when the
    configured binary is absent or not executable (the canonical ``[Errno 2] … 'codex'``).
    Reading ``.transient`` off such an exception yields ``False``, so it was reported as "the
    leaf ran but did not yield a usable verdict" — for a leaf that never started (PR #285
    review). It is infra, and it is precisely the case #278 exists to distinguish; but a
    *plain* re-run fails the same way, so it is not the same action as a transient blip."""
    if isinstance(exc, LeafError):
        return _FAIL_TRANSIENT if exc.transient else _FAIL_SUBSTANTIVE
    if isinstance(exc, OSError):  # FileNotFoundError / PermissionError from the spawn
        return _FAIL_STARTUP
    return _FAIL_SUBSTANTIVE


def _review_unavailable(d: Path, reason: str, *, failure: str = _FAIL_SUBSTANTIVE,
                        error_log: Path | None = None) -> None:
    """Write a placeholder review flagging the gap as a §6 NEEDS-HUMAN, so a failed or
    interrupted reviewer leaves a re-runnable bundle — not a half-checked one that
    crashes assemble. The bundle still reaches sign-off; accept is blocked (C6).

    ``failure`` (see :func:`_failure_class`) classifies the placeholder (#138) so the human
    can tell infra — a transient blip, or a leaf that never started — from a reviewer that
    genuinely needs a human; when an ``error_log`` with the failed attempts' output exists,
    the placeholder points at it."""
    print(f"leaves: {d.name} — advisory review unavailable ({reason})", file=sys.stderr)
    (d / "check-review.md").write_text(
        "# Advisory review — NOT COMPLETED\n\n"
        f"The reviewer did not produce a verdict table ({reason}).\n\n"
        + _unavailable_classification(failure, error_log)
        + "- NEEDS-HUMAN — re-run the Check reviewer; this bundle has no advisory review "
        "and must not be accepted until one exists.\n",
        encoding="utf-8",
    )


def _unavailable_classification(failure: str, error_log: Path | None) -> str:
    """Shared classification block for a failed reviewer/advisory placeholder (#138):
    name the failure class and point at the captured error log when present.

    Leads with a machine-readable leaf-status marker (#278). Without it, an empty advisory
    artifact is ambiguous — "the adversary ran and found nothing" reads exactly like "the
    adversary never ran", so an infra failure (no Docker, missing binary) presents as a clean
    adversarial pass and the operator has to hand-annotate "infra, not substance". `assemble`
    reads the marker and labels the §6 row accordingly.

    Both infra shapes (transient, startup) carry the INFRA marker — nothing reviewed the diff
    either way — but their prose differs, because the operator's next action does: a transient
    blip is safe to re-run as-is; a leaf that never started will fail the same way until its
    command is fixed."""
    status = {
        _FAIL_TRANSIENT: assemble.LEAF_STATUS_INFRA,
        _FAIL_STARTUP: assemble.LEAF_STATUS_STARTUP,
    }.get(failure, assemble.LEAF_STATUS_HUMAN)
    marker = f"<!-- pdca:leaf-status {status} -->\n\n"
    if failure == _FAIL_TRANSIENT:
        kind = ("**transient infra — safe to re-run.** The leaf exited non-zero with no "
                "output and retries did not recover, so it almost certainly hit a usage/"
                "rate limit or a transient API/network error rather than reviewing the "
                "diff; a sibling advisory leaf of a different family may already have "
                "covered it.")
    elif failure == _FAIL_STARTUP:
        kind = ("**startup infra — the leaf never ran.** Its configured command could not be "
                "launched at all (the binary is absent, or not executable), so nothing "
                "reviewed the diff — this is NOT an empty verdict. A plain re-run will fail "
                "the same way: fix the leaf's `argv` / PATH first (`pdca doctor` checks each "
                "command leaf's CLI), then re-run.")
    else:
        kind = ("**substantive — needs a human.** The leaf ran but did not yield a usable "
                "verdict; do not assume an infra blip.")
    log_ref = ""
    if error_log is not None and error_log.exists():
        log_ref = f" See `{error_log.name}` in this bundle for the captured error."
    return f"{marker}Failure class: {kind}{log_ref}\n\n"


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


def _advisory_prompt(spec: dict, leaf_id: str, rubric: str = "") -> str:
    role = spec.get("role") or "review the patch for correctness bugs and reuse / " \
        "simplification / efficiency cleanups"
    return (
        f"You are an ADVISORY code reviewer — lens: {role}. You have ONLY patch.diff, "
        "brief.md and check-gates.json here (build-notes.md is withheld); ground every "
        "cited path:line on the target source at $PDCA_TARGET, never other checkouts. "
        f"Write check-advisory-{leaf_id}.md: a short list of findings, each a Markdown "
        "bullet with a path:line. For any finding a human must adjudicate, prefix the "
        "bullet '- NEEDS-HUMAN — ' (it becomes a SUMMARY §6 item). If the finding is an "
        "IMPLEMENTATION defect the builder can fix by iterating — a logic bug, a missed "
        "case, a weak or incorrect test, a conformance nit — prefix it "
        "'- NEEDS-HUMAN [impl] — ' instead, so the driver can route it straight back to Do "
        "without spending the human's attention (issue #264). Keep the plain "
        "'- NEEDS-HUMAN — ' form for anything needing a human ARCHITECTURAL / scope / "
        "fitness-to-purpose decision; when in doubt, OMIT '[impl]'. You are ADVISORY — you "
        "never gate; the human decides at sign-off. If you find nothing, say so explicitly."
    ) + rubric


def _resolved_builder_family(d: Path) -> str:
    """The family of the builder that actually ran, read from the last ``loop-telemetry.json``
    attempt (issue #200 — the entry :func:`_record_loop_attempt` wrote in Do). This is the
    *resolved* fact, so it holds whichever way the backend was chosen — an explicit
    ``Do model`` (#167), difficulty routing (#134) or escalation (#135). Best-effort: an
    absent / garbled file ⇒ ``""`` (unknown), never a crash."""
    path = d / "loop-telemetry.json"
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        attempts = data.get("attempts") if isinstance(data, dict) else None
        if attempts:
            return str(attempts[-1].get("family", "") or "")
    except (ValueError, OSError, AttributeError, IndexError):
        pass
    return ""


def _decorrelation_note(d: Path, msg: str) -> None:
    """Record an advisory-selection lapse (issue #200) as a §6 item. Written as a
    check-advisory-*.md so :func:`assemble.assemble_summary` folds its NEEDS-HUMAN line into
    §6 like any advisory finding — a human sees that decorrelation didn't hold for the bundle."""
    advisory_artifact(d, "decorrelation").write_text(
        "# Advisory review — decorrelation\n\n- NEEDS-HUMAN — " + msg + "\n", encoding="utf-8")


def _select_advisory(specs: list[dict], d: Path, cfg: Config) -> list[dict]:
    """Apply the advisory-selection policy (issue #200) to the already-``when``-filtered
    ``specs``. Default (``mode`` unset) returns them unchanged — every applicable leaf runs
    (#64). Under ``mode = "vendor-complement"`` the list is a VENDOR POOL: return the single
    leaf whose ``family`` differs from the builder that ran, so a Codex-built bundle gets a
    Claude advisory and vice-versa, automatically. If no different-vendor leaf exists (or the
    builder family is unknown) fall back to the first applicable leaf rather than skip review
    — a same-vendor review still beats none — and record the lapse in §6."""
    if cfg.advisory_selection.get("mode") != "vendor-complement":
        return specs
    advisory_artifact(d, "decorrelation").unlink(missing_ok=True)  # a prior attempt's note
    if not specs:
        return specs
    builder_family = _resolved_builder_family(d)
    if builder_family:
        # A complement must declare a KNOWN family that differs — a leaf with a blank/absent
        # `family` is an unknown vendor (possibly the builder's own), never a guaranteed
        # complement, so it falls through to the same-vendor §6 note rather than masquerading
        # as decorrelated (a #64 config never had to set `family`).
        complement = next(
            (s for s in specs
             if (fam := s.get("family", "").strip().lower()) and fam != builder_family.lower()),
            None)
        if complement is not None:
            return [complement]
        reason = (f"the builder ran family '{builder_family}' and no configured advisory "
                  "declares a different (non-empty) family")
    else:
        reason = "the builder family that ran is unknown (no loop-telemetry.json)"
    chosen = specs[0]
    _decorrelation_note(
        d, f"advisory reviewer '{chosen.get('id') or 'advisory'}' could not be decorrelated "
           f"from the builder — {reason}; it ran same-vendor. Confirm the review's "
           "independence by hand, or add a different-`family` [[leaves.advisory]] entry.")
    return [chosen]


def run_advisory_leaves(d: Path, cfg: Config) -> None:
    """Run each configured advisory reviewer that applies (issue #64), after the
    advisory-selection policy narrows the list (issue #200). Each writes
    check-advisory-<id>.md; failures degrade to a §6 NEEDS-HUMAN placeholder, never crash
    the cycle (advisory, like the main reviewer)."""
    applicable = [spec for spec in cfg.advisory_leaves if _advisory_applies(spec, d)]
    for spec in _select_advisory(applicable, d, cfg):
        leaf_id = spec.get("id") or "advisory"
        leaf = LeafConfig(mode=spec.get("mode", "stub"), family=spec.get("family", ""),
                          argv=list(spec.get("argv", [])), agent=spec.get("agent", ""),
                          model=spec.get("model", ""), effort=spec.get("effort", ""))
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
        profile = cfg.profile(leaf)
        # Seed unconditionally: flag families need it to resolve `--agent` (#161);
        # for inline families it is harmless (role prompts only, never build-notes).
        _seed_sandbox_agents(cfg, sandbox)
        # …and the project's sandbox policy, which is likewise invisible from a temp cwd
        # (#261) — without it a loopback-socket runtime test can't bind, so it can never
        # earn an automated red→green at Check.
        seeded = _seed_sandbox_settings(cfg, sandbox, profile)
        target = _reviewer_target(d, cfg)
        env = {"PDCA_TARGET": str(target)} if target else None
        extra = ([profile.grounding_flag, str(target)]
                 if target and profile.grounding_flag else [])
        extra += _sandbox_argv(cfg, profile, seeded=seeded)   # see _run_review_sandboxed
        out = sandbox / f"check-advisory-{leaf_id}.md"
        error_log = d / f"check-advisory-{leaf_id}.error.log"
        err = _invoke_leaf_resilient(
            leaf, sandbox,
            _advisory_prompt(spec, leaf_id, rubric_mod.for_reviewer(d, cfg)),
            error_log=error_log,
            label=f"Advisory {leaf_id} {d.name}",
            status=lambda: progress.bundle_activity(sandbox, (out.name,)),
            stream_json=True, env=env, extra_argv=extra, cfg=cfg)
        if err is not None:  # advisory must never crash the cycle
            _advisory_unavailable(d, leaf_id, f"leaf failed: {err}",
                                  failure=_failure_class(err), error_log=error_log)
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


def _advisory_unavailable(d: Path, leaf_id: str, reason: str, *,
                          failure: str = _FAIL_SUBSTANTIVE,
                          error_log: Path | None = None) -> None:
    print(f"leaves: {d.name} — advisory '{leaf_id}' unavailable ({reason})", file=sys.stderr)
    advisory_artifact(d, leaf_id).write_text(
        f"# Advisory review — {leaf_id} — NOT COMPLETED\n\n"
        + _unavailable_classification(failure, error_log)
        + f"- NEEDS-HUMAN — advisory leaf '{leaf_id}' did not produce findings ({reason}); "
        "re-run it or adjudicate by hand.\n",
        encoding="utf-8")


# ----------------------------------------------------------------------------
# Plan-beat advisory reviewers (issue #301) — antagonists of the BRIEF, mirroring the
# Check advisory machinery (#64/#200) at Plan: right after the planner writes brief.md,
# each configured [[leaves.plan_advisory]] leaf reviews the PLAN (brief + notes +
# sources — no patch, no gates), writes plan-advisory-<id>.md, the planner gets ONE
# bounded revision pass over the findings, and a per-bundle BENEFIT record
# (plan-advisory-benefit.json: brief hash before/after, revised?, finding count) captures
# whether the review changed anything — the raw signal Act needs to judge whether plan
# reviews pay off. Opt-in; an empty list leaves the Plan beat untouched.
# ----------------------------------------------------------------------------
PLAN_ADVISORY_INPUTS = ["brief.md", "notes.json"]  # + the sources/ dir, copied whole
PLAN_ADVISORY_BENEFIT = "plan-advisory-benefit.json"


def plan_advisory_artifact(d: Path, leaf_id: str) -> Path:
    """The artifact a plan-advisory leaf writes. A distinct prefix from
    ``check-advisory-*`` — the Check-side globs (assemble §5, archive) must not
    pick these up as patch reviews."""
    return d / f"plan-advisory-{leaf_id}.md"


def _plan_advisory_prompt(spec: dict, leaf_id: str) -> str:
    role = spec.get("role") or ("refute the brief: wrong root cause, untestable success "
                                "criterion, hidden scope")
    return (
        f"You are an ADVISORY plan reviewer — an antagonist of the BRIEF, lens: {role}. "
        "You have ONLY brief.md, notes.json and the sources/ dir here (no patch exists "
        "yet); ground every claim about the code on the target source at $PDCA_TARGET, "
        "never other checkouts. Attack the plan, not the prose: does the stated defect "
        "match the tracker thread in notes.json/sources (wrong root-cause framing?); is "
        "the success criterion something a gate or reviewer can actually verify, or "
        "vibes; is the scope one logical fix or a hidden second change; do the repo + "
        "branch target and any `Depends on` ids resolve (if dependency-state.json is "
        "present it lists each declared prerequisite bundle's existence and state — "
        "judge the declarations against it); did the brief ignore a "
        "load-bearing comment in the thread. "
        f"Write plan-advisory-{leaf_id}.md: a short list of findings, each a Markdown "
        "bullet prefixed '- NEEDS-HUMAN — ' with the evidence (a brief line, a thread "
        "quote, a path:line). You are ADVISORY — you never gate, and you never edit "
        "brief.md yourself. \"Could not fault the brief after a real attempt\" is an "
        "acceptable strong answer — say so explicitly."
    )


def _plan_decorrelation_note(d: Path, msg: str) -> None:
    """The plan-side twin of :func:`_decorrelation_note` (#200/#301)."""
    plan_advisory_artifact(d, "decorrelation").write_text(
        "# Plan advisory — decorrelation\n\n- NEEDS-HUMAN — " + msg + "\n", encoding="utf-8")


def _select_plan_advisory(specs: list[dict], d: Path, cfg: Config) -> list[dict]:
    """The #200 selection policy anchored on the PLANNER family (issue #301).

    Pre-Do there is no builder telemetry, and the brief is the planner's artifact —
    "reviewer ≠ author" therefore keys on ``cfg.planner.family`` (static config, no
    telemetry needed). Unknown/empty planner family or no different-vendor leaf ⇒
    same-vendor fallback + a decorrelation note, mirroring the Check-side contract."""
    if cfg.plan_advisory_selection.get("mode") != "vendor-complement":
        return specs
    plan_advisory_artifact(d, "decorrelation").unlink(missing_ok=True)
    if not specs:
        return specs
    planner_family = (cfg.planner.family or "").strip().lower()
    if planner_family:
        complement = next(
            (s for s in specs
             if (fam := s.get("family", "").strip().lower()) and fam != planner_family),
            None)
        if complement is not None:
            return [complement]
        reason = (f"the planner runs family '{planner_family}' and no configured "
                  "plan-advisory declares a different (non-empty) family")
    else:
        reason = "the planner's family is not declared in [leaves.planner]"
    chosen = specs[0]
    _plan_decorrelation_note(
        d, f"plan reviewer '{chosen.get('id') or 'plan-advisory'}' could not be "
           f"decorrelated from the planner — {reason}; it ran same-vendor. Confirm the "
           "review's independence by hand, or add a different-`family` "
           "[[leaves.plan_advisory]] entry.")
    return [chosen]


def _brief_sha(d: Path) -> str:
    """sha256 of brief.md's bytes ("" if absent) — the before/after benefit signal."""
    bp = d / "brief.md"
    return hashlib.sha256(bp.read_bytes()).hexdigest() if bp.is_file() else ""


def _plan_findings(d: Path) -> int:
    """SUBSTANTIVE findings across this bundle's plan-advisory artifacts.

    Excluded: the decorrelation note (a selection lapse, not a brief finding) and any
    NOT-COMPLETED placeholder — its NEEDS-HUMAN line reports infrastructure, not the
    brief (#301 review round 3): counting it triggered a planner revision (and
    ``findings: 1`` telemetry) over a missing CLI or transient outage. Placeholders
    carry the machine-readable leaf-status marker (#278), the same signal §6 uses;
    they still fold into §6 for the human, they just never drive the revision pass."""
    count = 0
    for p in sorted(d.glob("plan-advisory-*.md")):
        if p.name == "plan-advisory-decorrelation.md":
            continue
        text = p.read_text(encoding="utf-8")
        if assemble.leaf_status(text):
            continue  # a placeholder, not a review
        count += sum(1 for line in text.splitlines()
                     if line.lstrip().startswith("- NEEDS-HUMAN"))
    return count


def _run_plan_advisory_leaves(d: Path, cfg: Config) -> list[str]:
    """Run the applicable plan-advisory leaves for one briefed bundle; return the leaf
    ids that ran. Artifacts only — the revision + benefit record are the caller's."""
    applicable = [s for s in cfg.plan_advisory_leaves if _advisory_applies(s, d)]
    ran: list[str] = []
    for spec in _select_plan_advisory(applicable, d, cfg):
        leaf_id = spec.get("id") or "plan-advisory"
        leaf = LeafConfig(mode=spec.get("mode", "stub"), family=spec.get("family", ""),
                          argv=list(spec.get("argv", [])), agent=spec.get("agent", ""),
                          model=spec.get("model", ""), effort=spec.get("effort", ""))
        if leaf.mode == "command":
            _run_plan_advisory_sandboxed(d, cfg, leaf, spec, leaf_id)
        else:
            _stub_plan_advisory(d, spec, leaf_id)
        ran.append(leaf_id)
    return ran


def _dependency_manifest(d: Path, cfg: Config) -> dict:
    """``{dep id: {declared, exists, state}}`` for the brief's declared prerequisites
    (#301 review round 5). The review sandbox holds only the plan inputs and
    ``$PDCA_TARGET`` is the target repository — without this, the reviewer cannot
    judge the ``Depends on`` / ``Depends on (merged)`` / ``Stacks on`` declarations it
    is explicitly told to validate. ``find_bundle`` resolves archived copies too."""
    bp = d / "brief.md"
    out: dict[str, dict] = {}
    for kind, ids in (("Depends on", brief.depends_on(bp)),
                      ("Depends on (merged)", brief.depends_on_merged(bp)),
                      ("Stacks on", brief.stacks_on(bp))):
        for dep in ids:
            b = cfg.find_bundle(dep)
            out[dep] = {"declared": kind, "exists": b.is_dir(),
                        "state": state.state(b) if b.is_dir() else None}
    return out


@contextlib.contextmanager
def _plan_fallback_target(d: Path, cfg: Config):
    """A DISPOSABLE grounding checkout when the brief's exact base cannot be
    materialized (#301 review rounds 7/8): a temp DETACHED worktree at the resolved
    primary's HEAD, removed after the review.

    Never the lane worktree :func:`_reviewer_target` prefers (round 7) — pre-Do it
    holds whatever its LAST user left there (another bundle's patch, or this
    bundle's prior attempt after an iterate-to-Plan), and the antagonist would
    fault the new brief against the wrong source. And never the primary checkout
    itself (round 8): the family grounding flag is read/WRITE for codex
    (``--add-dir``), so exposing the operator's working tree would let a reviewer
    command mutate their uncommitted work despite the read-only contract. HEAD may
    lag the brief's intended base — a loosely-grounded review still beats none
    (advisory, never a gate). Unresolvable/non-git target or a failed add ⇒ ``None``
    (the review grounds on the plan inputs alone)."""
    from . import publish  # lazy: publish imports leaves, avoid an import cycle
    try:
        repo_spec, _base, _slug = publish._resolve_target(d)
        primary = publish._checkout_path(cfg, repo_spec) if repo_spec else None
    except Exception:  # noqa: BLE001 — grounding is best-effort, never fatal
        primary = None
    if primary is None or not (primary / ".git").exists():
        yield None
        return
    tmp = tempfile.mkdtemp(prefix="pdca-plan-target-")
    pinned = Path(tmp) / "target"
    if worktree._git(primary, "worktree", "add", "--detach", str(pinned), "HEAD") != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        yield None
        return
    try:
        yield pinned
    finally:
        if worktree._git(primary, "worktree", "remove", "--force", str(pinned)) != 0:
            shutil.rmtree(pinned, ignore_errors=True)
            worktree._git(primary, "worktree", "prune")
        shutil.rmtree(tmp, ignore_errors=True)


@contextlib.contextmanager
def _pinned_plan_target(d: Path, cfg: Config):
    """A read-only checkout PINNED to the brief's resolved base ref, for grounding the
    plan review (#301 review round 2).

    Pre-Do there is no per-cycle worktree the review may trust, so this materializes
    a temp DETACHED worktree at the exact ``base_ref`` the brief resolves to (the
    drift.py pattern), removed after the review. Unresolvable target / failed add ⇒
    :func:`_plan_fallback_target` — a disposable detached tree at the primary's
    HEAD, deliberately neither :func:`_reviewer_target`'s lane worktree (another
    bundle's patched content, round 7) nor the writable primary checkout itself
    (round 8). A loosely-grounded review still beats none — advisory, never a
    gate."""
    tgt = worktree._target(d, cfg)
    if tgt is None:
        with _plan_fallback_target(d, cfg) as fb:
            yield fb
        return
    primary, base_ref = tgt
    worktree._git(primary, "fetch", cfg.base_remote)  # best-effort refresh of the base
    if base_ref.startswith("origin/") and cfg.base_remote != "origin":
        # A stacked base lives on origin (#123): with base_remote = "upstream", fetching
        # only it leaves origin/<parent-branch> stale/absent, the worktree add fails and
        # the review silently grounds on the sibling checkout instead of the stacked
        # base (#301 review round 4). Mirror worktree.ensure's dual fetch.
        worktree._git(primary, "fetch", "origin")
    tmp = tempfile.mkdtemp(prefix="pdca-plan-target-")
    pinned = Path(tmp) / "target"
    if worktree._git(primary, "worktree", "add", "--detach", str(pinned), base_ref) != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        with _plan_fallback_target(d, cfg) as fb:
            yield fb
        return
    try:
        yield pinned
    finally:
        if worktree._git(primary, "worktree", "remove", "--force", str(pinned)) != 0:
            shutil.rmtree(pinned, ignore_errors=True)
            worktree._git(primary, "worktree", "prune")
        shutil.rmtree(tmp, ignore_errors=True)


def _run_plan_advisory_sandboxed(d: Path, cfg: Config, leaf: LeafConfig, spec: dict,
                                 leaf_id: str) -> None:
    """One plan-advisory leaf in a temp dir holding ONLY the plan inputs (the reviewer
    independence sandbox, minus patch/gates), grounding on $PDCA_TARGET — a checkout
    pinned to the brief's resolved base (#301 review round 2)."""
    with tempfile.TemporaryDirectory(prefix="pdca-plan-advisory-") as tmp, \
            _pinned_plan_target(d, cfg) as target:
        sandbox = Path(tmp)
        for name in PLAN_ADVISORY_INPUTS:
            if (d / name).exists():
                shutil.copy2(d / name, sandbox / name)
        if (d / "sources").is_dir():
            shutil.copytree(d / "sources", sandbox / "sources")
        manifest = _dependency_manifest(d, cfg)
        if manifest:
            (sandbox / "dependency-state.json").write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        profile = cfg.profile(leaf)
        _seed_sandbox_agents(cfg, sandbox)
        # DELIBERATELY no _seed_sandbox_settings / _sandbox_argv here (#301 review
        # round 6): those carry the CHECK-leaf sandbox grants ([leaves.sandbox]
        # network_access / unsandboxed_commands / seeded network keys) an operator
        # opted into for Docker-backed gates and the reviewer's prior-art fetch. A
        # plan review needs none of that — it reads the brief, notes/sources and the
        # pinned read-only target — so the leaf gets a MINIMAL fail-closed sandbox
        # instead (#301 review round 8): _seed_plan_sandbox_settings turns the vendor
        # sandbox ON with none of those grants (claude's sandbox.enabled defaults
        # FALSE, so seeding nothing left a Bash-capable reviewer unconfined), and the
        # confinement flag rides exactly iff the seed landed (#290).
        seeded = _seed_plan_sandbox_settings(sandbox, profile)
        env = {"PDCA_TARGET": str(target)} if target else None
        extra = ([profile.grounding_flag, str(target)]
                 if target and profile.grounding_flag else [])
        if seeded:
            extra += list(profile.settings_scope_argv)
        out = sandbox / f"plan-advisory-{leaf_id}.md"
        error_log = d / f"plan-advisory-{leaf_id}.error.log"
        err = _invoke_leaf_resilient(
            leaf, sandbox, _plan_advisory_prompt(spec, leaf_id),
            error_log=error_log,
            label=f"Plan advisory {leaf_id} {d.name}",
            status=lambda: progress.bundle_activity(sandbox, (out.name,)),
            stream_json=True, env=env, extra_argv=extra, cfg=cfg)
        if err is not None:  # advisory must never crash Plan
            _plan_advisory_unavailable(d, leaf_id, f"leaf failed: {err}",
                                       failure=_failure_class(err), error_log=error_log)
            return
        if out.exists():
            shutil.copy2(out, plan_advisory_artifact(d, leaf_id))
        else:
            _plan_advisory_unavailable(d, leaf_id, "produced no artifact")


def _stub_plan_advisory(d: Path, spec: dict, leaf_id: str) -> None:
    role = spec.get("role") or "refute the brief (root cause, success criterion, scope)"
    plan_advisory_artifact(d, leaf_id).write_text(
        f"# Plan advisory — {leaf_id} (stub)\n\nLens: {role}.\n\n"
        f"- NEEDS-HUMAN — plan-advisory lens is a stub here; a real `{leaf_id}` leaf "
        "(family/argv in [[leaves.plan_advisory]]) reviews the brief and lists findings. "
        "The human adjudicates at sign-off.\n",
        encoding="utf-8")


def _plan_advisory_unavailable(d: Path, leaf_id: str, reason: str, *,
                               failure: str = _FAIL_SUBSTANTIVE,
                               error_log: Path | None = None) -> None:
    print(f"leaves: {d.name} — plan advisory '{leaf_id}' unavailable ({reason})",
          file=sys.stderr)
    plan_advisory_artifact(d, leaf_id).write_text(
        f"# Plan advisory — {leaf_id} — NOT COMPLETED\n\n"
        + _unavailable_classification(failure, error_log)
        + f"- NEEDS-HUMAN — plan-advisory leaf '{leaf_id}' did not produce findings "
        f"({reason}); re-run it or adjudicate by hand.\n",
        encoding="utf-8")


def _plan_revision_prompt(cfg: Config, bundles: list[Path]) -> str:
    per_bundle = "\n".join(
        f"- {d}: findings in " + ", ".join(
            p.name for p in sorted(d.glob("plan-advisory-*.md"))
            if p.name != "plan-advisory-decorrelation.md")
        for d in bundles)
    return (
        "You are the Plan leaf on a REVISION pass (issue #301) — do not re-plan from "
        "scratch and do not implement. An antagonistic plan review raised findings "
        "against the brief(s) below. For each bundle: read its plan-advisory-*.md, then "
        "either revise brief.md in place to address a finding, or append a short "
        "`Plan-review response:` line under the brief stating why the brief stands. "
        "Keep the parsed `- **Label:** value` field shape. One pass, no new bundles.\n"
        + per_bundle
    )


def run_plan_advisory_batch(cfg: Config, bundles: list[Path]) -> None:
    """The Plan-beat advisory pass over freshly briefed bundles (issue #301).

    Per bundle: run the selected plan-advisory leaves (artifacts). Then, if any bundle
    has findings, ONE planner revision invocation covers them all (bounded by
    construction — never a loop), and each reviewed bundle gets its benefit record.
    No-op when nothing is configured or nothing is reviewable (a placeholder brief is
    a template, not a plan — reviewing it would grade boilerplate)."""
    if not cfg.plan_advisory_leaves:
        return
    reviewed = [d for d in bundles
                if (d / "brief.md").exists() and not brief.is_placeholder(d / "brief.md")]
    ran: dict[Path, list[str]] = {}
    for d in reviewed:
        # A rewritten brief (or a changed pool/`when` selection) must not inherit the
        # PREVIOUS review's artifacts (#301 review round 6): stale findings would
        # re-enter _plan_findings and §6 and could trigger a revision — or block
        # sign-off — against a brief they never reviewed. Cleared BEFORE selection,
        # so they vanish even when the new brief matches no leaf.
        for stale in d.glob("plan-advisory-*"):
            stale.unlink(missing_ok=True)
        ids = _run_plan_advisory_leaves(d, cfg)
        if ids:
            ran[d] = ids
    if not ran:
        return
    before = {d: _brief_sha(d) for d in ran}
    with_findings = [d for d in ran if _plan_findings(d) > 0]
    if with_findings and cfg.planner.mode == "command":
        # Contained like the advisory leaves themselves (#301 review round 3): the
        # revision is an OPT-IN advisory step, and a planner that exits non-zero here
        # must not fail an otherwise completed Plan beat (or skip the benefit records
        # below). The original briefs are untouched on failure → revised stays False.
        try:
            _invoke(cfg.planner, cfg.root, _plan_revision_prompt(cfg, with_findings), cfg=cfg)
        except Exception as exc:  # noqa: BLE001 — advisory: never crash the Plan beat
            print(f"leaves: plan-advisory revision pass failed ({type(exc).__name__}: "
                  f"{exc}); briefs left as authored — findings stay open in §6",
                  file=sys.stderr)
    for d, ids in ran.items():
        after = _brief_sha(d)
        (d / PLAN_ADVISORY_BENEFIT).write_text(json.dumps({
            "before_sha": before[d],
            "after_sha": after,
            "revised": after != before[d],
            "findings": _plan_findings(d),
            "leaves": ids,
        }, indent=2) + "\n", encoding="utf-8")


def run_plan_advisory(d: Path, cfg: Config) -> None:
    """Single-bundle convenience over :func:`run_plan_advisory_batch`."""
    run_plan_advisory_batch(cfg, [d])


# ----------------------------------------------------------------------------
# Leaf 3 — Check sign-off (signoff, interactive): Claude + human reach the OK.
# ----------------------------------------------------------------------------
def run_signoff(d: Path, cfg: Config) -> None:
    if cfg.signoff.mode == "command":
        _invoke(cfg.signoff, cfg.root, _signoff_prompt(d), cfg=cfg)
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
        _invoke(cfg.signoff, cfg.root, _signoff_batch_prompt(bundles), cfg=cfg)
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
    # Concurrent Act WRITERS serialize via the shared session lock (#299 review
    # rounds 11/12): two flows completing at once both pass act_due before either
    # advances the marker — and a manual `act log --append` takes the SAME lock —
    # so the frontier union is never asked to undo duplicate act-log entries over
    # one snapshot. The auto path WAITS for the active session (#299 review round
    # 14) rather than skipping: a skip would leave this flow's newly frozen
    # bundles without their promised automatic review until some unrelated later
    # flow completed. The cadence re-check below then decides whether anything is
    # left to review.
    with act_mod.act_session(cfg, wait=True) as held:
        if not held:  # only an unopenable lock file (never contention) lands here
            print("leaves: cannot open the Act session lock — Act skipped this run; "
                  "its cycles stay unreviewed for the next due Act", file=sys.stderr)
            return
        # Re-check the cadence UNDER the session lock: the other session may have
        # just finished and advanced the frontier past our threshold — reviewing
        # again would duplicate its entry over the same cycles.
        if not act_mod.act_due(cfg):
            print("leaves: Act no longer due — a concurrent session advanced the "
                  "review frontier; skipped", file=sys.stderr)
            return
        # Snapshot the frozen set BEFORE the session (#299 review round 5): the
        # review can only have covered what existed when it started — a bundle
        # freezing mid-session must stay unreviewed, and re-globbing afterwards
        # would push it past the frontier unseen. Fingerprints ride the SAME
        # snapshot (#299 review round 17): a bundle recreated while the leaf runs
        # must be attested by the hash the review read, not by post-session disk.
        covered = act_mod.frozen_bundles(cfg)
        snap_fps = {d.name: act_mod._fingerprint(d) for d in covered}
        started = time.time()
        if cfg.act.mode == "command":
            _invoke(cfg.act, cfg.root, _act_prompt(cfg, date, bundles=covered), cfg=cfg)
        else:
            _stub_act(cfg, date, bundles=covered)

        # Advance the review frontier (issues #109/#299) whenever the Act beat
        # runs — even if a command-mode Act judged "no delta" and wrote no act-log
        # entry, the review happened, over exactly the pre-session snapshot.
        # delta_guard applies the mid-session delta protection INSIDE the marker's
        # critical section (#299 review round 7 — a scan out here would race
        # revalidate's unmark_reviewed); the stamp's `changed` verdict decides, so
        # a confirming revalidation doesn't withhold.
        act_mod.mark_reviewed(cfg, reviewed=covered, date=date, delta_guard=started,
                              fingerprints=snap_fps)


def _act_prompt(cfg: Config, date: str, bundles: list[Path] | None = None) -> str:
    # `bundles` is run_act's pre-session snapshot (#299 review round 13): indexing
    # here must describe EXACTLY the set the frontier will advance over — a bundle
    # freezing between the snapshot and this call would otherwise be reviewed (and
    # logged) now, left out of the frontier, and reviewed AGAIN next cadence.
    entries = act_mod.index(cfg, bundles=bundles)
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


def _stub_act(cfg: Config, date: str, bundles: list[Path] | None = None) -> None:
    # Same snapshot rule as _act_prompt (#299 review round 13).
    entries = act_mod.index(cfg, bundles=bundles)
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
        # A non-claude publisher has no PreToolUse STOP hook, so give it the same `gh` PATH
        # shim the builder gets (guard.py) — else a codex/other publisher could `gh pr ready`
        # / `merge` itself, which is the human's Check sign-off, not the model's (best-effort;
        # a no-op for claude, whose native hook already enforces this).
        profile = families.resolve(cfg.publisher.family, cfg.families)
        env = None if profile.native_guard else guard.shim_env(cfg, None)
        _invoke(cfg.publisher, cfg.root, _publish_prompt(d, cfg), env=env, cfg=cfg)
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
        f" Put a clickable tracker link on the Summary's `Reported in [#{issue_id}]"
        f"({issue_url})` line so a reader can click through. Keep the closing `Fixes` "
        "trailer a BARE `#<id>` (never a Markdown link) — GitHub auto-closes only on a "
        "bare id after the keyword, so a linked trailer silently fails to close the issue."
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
        f"2) {d}/pr-description.md — the Summary MUST open with a `**User impact:**` line "
        "stating the bug's USER-VISIBLE effect (what the user experiences) BEFORE Root "
        "cause, then the one-line change + What to look at (for non-implementors), then "
        f"Root cause / Fix, then a Verification claim→evidence trail citing path:lines on "
        f"the target branch; no internal jargon (see {pr_tpl}).{link_clause}\n"
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
    # PR body mirrors pr-description.md.tpl: a `**User impact:**` opener BEFORE Root cause
    # and the issue-trailer form last, so the offline path keeps passing the T4
    # contribution gate (contribcheck).
    pr_trailer = trailer if trailer else f"References #{issue_id}"
    (d / "pr-description.md").write_text(
        "## Summary\n**User impact:** stub user-visible effect.\n\nstub one-line change.\n\n"
        "## What to look at\nstub.\n\n## Root cause\nstub.\n\n"
        "## Fix\nstub.\n\n## Verification\n- Claim: stub.\n- Checked: path:1 — stub.\n"
        "- Test: path:1 — stub regression test, fails pre-fix / passes post-fix.\n\n"
        f"{pr_trailer}\n",
        encoding="utf-8",
    )
