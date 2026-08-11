"""``pdca`` command-line entry point.

Thin wrapper over the driver: create a bundle, advance it, inspect the sign-off
queue, and record the human sign-off. Run as ``pdca <cmd>`` (installed) or
``python -m pdca_harness.cli <cmd>`` (from a source checkout with PYTHONPATH=src).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from . import (act, brief, cleanup, doctor, drift, driver, flow, gates, leaves, manual_test,
               merged, publish, queue, record, registry, revalidate, revert, signoff, sizing,
               split, state, sweep, triage, waves, worktree)
from .config import Config


def _prog_name() -> str:
    """The command name to show in ``--help``.

    The console-script name is a per-instance copier choice (``cli_name``; issue #73),
    so the rendered project installs e.g. ``pdca-gramps`` — not always ``pdca``. Resolve
    it from the actually-invoked script (``argv[0]``) so ``--help`` shows the real command;
    fall back to ``pdca`` when invoked as a module (``python -m pdca_harness.cli``), where
    ``argv[0]`` is a file path, not the command.
    """
    name = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else ""
    if not name or name.endswith(".py") or name == "__main__":
        return "pdca"
    return name

# Ordering for the cheap-first sign-off queue (docs 03 §sign-off queue).
_STATE_ORDER = [
    state.AWAITING_SIGNOFF,
    state.CHECKED,
    state.BUILT,
    state.PLANNED,
    state.UNPLANNED,
    state.ITERATE_DO,
    state.ITERATE_PLAN,
    state.COMPLETE,
    state.DISCONTINUED,
    state.RESOLVED,
]


# (binary, argv prefix) in preference order: Linux first, then macOS.
# -i asserts against IDLE system sleep (valid on battery too); -s only holds on AC power,
# so -i is the load-bearing flag for an unattended laptop run. Include both to mirror
# systemd's idle:sleep. (`caffeinate -s` alone would silently no-op on battery.)
_INHIBITORS = [
    ("systemd-inhibit", ["systemd-inhibit", "--what=idle:sleep", "--why=pdca flow"]),
    ("caffeinate", ["caffeinate", "-i", "-s"]),
]


def _inhibitor_works(prefix: list[str]) -> bool:
    """Does this inhibitor prefix actually exec its command argument? (#259)

    Presence of the binary is not evidence the inhibitor works. On a container / CI host
    where ``/usr/bin/systemd-inhibit`` is installed but no systemd bus is reachable, it
    exits 1 with ``Failed to connect to … bus`` and **never execs the wrapped command** —
    so re-exec'ing under it kills the run before it starts. Probe by wrapping ``true``:
    exit 0 means the inhibitor took its lock and ran the command.
    """
    try:
        return subprocess.run([*prefix, "true"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):  # missing, unexecutable, or hung
        return False


def _suspend_inhibitor_argv(argv: list[str], env: dict, *,
                            probe: Callable[[list[str]], bool] = _inhibitor_works,
                            ) -> list[str] | None:
    """The keep-awake wrapper for a ``pdca flow`` run, or ``None`` to run unwrapped (#244).

    A ``pdca flow`` — a batch, or a high-difficulty bundle on a strong Do model — can run
    for **hours, unattended**. If the host auto-suspends on idle, suspend pauses every
    process and cuts the cycle off mid-run. Hold a suspend inhibitor for the command's
    lifetime by re-exec'ing under the platform inhibitor; it releases automatically at exit.

    Returns the argv to exec (inhibitor + the command that re-invokes this run), or ``None``
    when no wrapping applies: already wrapped (``PDCA_FLOW_INHIBITED``), opted out
    (``--no-inhibit`` / ``PDCA_NO_INHIBIT``), or no inhibitor is available **and working**.
    A binary that is present but broken (no systemd bus) is skipped, not returned: this
    fails OPEN — an un-inhibited run — rather than closed, because a flow that runs without
    keep-awake beats a flow that cannot start at all (#259).

    Pure decision — no exec — so it is unit-testable; ``probe`` is injected for the same
    reason. The opt-out / already-wrapped short-circuits come first, so the common paths
    never pay for a probe. Advisory by design: it inhibits only idle sleep, never shutdown,
    so an operator can still deliberately power off.
    """
    if env.get("PDCA_FLOW_INHIBITED"):            # already re-exec'd under an inhibitor
        return None
    if env.get("PDCA_NO_INHIBIT") or "--no-inhibit" in argv:  # opted out (CI / containers)
        return None
    cmd = _reexec_command(argv)
    for binary, prefix in _INHIBITORS:
        if shutil.which(binary) and probe(prefix):
            return [*prefix, *cmd]
    return None                                   # none available, or all present-but-broken


def _reexec_command(argv: list[str]) -> list[str]:
    """The command that re-invokes this run, for the inhibitor to exec.

    The inhibitor (``systemd-inhibit`` / ``caffeinate``) execs its command argument directly,
    so it must be something the OS can exec. An installed console script (``pdca`` /
    ``pdca-<name>``) is — forward ``argv`` as-is. But the documented source-checkout entry
    ``python -m pdca_harness.cli flow …`` has ``argv[0]`` = the ``cli.py`` file path, which
    has no shebang/execute bit; exec'ing it directly fails. Detect that (argv[0] is a ``.py``
    file or ``__main__``) and rebuild via the current interpreter so ``-m`` is preserved.
    """
    prog = Path(argv[0]).name if argv and argv[0] else ""
    if not prog or prog.endswith(".py") or prog == "__main__":
        return [sys.executable, "-m", "pdca_harness.cli", *argv[1:]]
    return list(argv)


def _inhibit_suspend_and_reexec() -> None:
    """Re-exec ``pdca flow`` under a suspend inhibitor (#244), or return to run unwrapped.

    Replaces the current process (``os.execvpe``) so the inhibitor owns the run's whole
    lifetime. Returns only when no wrapping applies; warns once on stderr when the reason is
    "no working inhibitor" (not when opted out or already wrapped) so a long run isn't
    silently left unprotected.
    """
    wrapped = _suspend_inhibitor_argv(sys.argv, dict(os.environ))
    if wrapped is None:
        opted_out = bool(os.environ.get("PDCA_NO_INHIBIT")) or "--no-inhibit" in sys.argv
        already = bool(os.environ.get("PDCA_FLOW_INHIBITED"))
        if not opted_out and not already:  # missing, or present-but-broken (#259)
            print("pdca flow: no working systemd-inhibit/caffeinate (missing, or no systemd "
                  "bus) — running WITHOUT keep-awake; disable host auto-suspend manually for "
                  "long unattended runs.", file=sys.stderr)
        return
    os.execvpe(wrapped[0], wrapped, {**os.environ, "PDCA_FLOW_INHIBITED": "1"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=_prog_name(), description="PDCA quality-cycle driver")
    # No subcommand → status (the bundle dashboard), the most-reached-for view (#88).
    sub = parser.add_subparsers(dest="cmd")

    p_init = sub.add_parser("init-issue",
                            help="seed a bundle from a pre-authored brief (requires --from-brief; "
                                 "to start from a ticket use `flow <id>`, which auto-plans)")
    p_init.add_argument("issue_id")
    p_init.add_argument("--from-brief", type=Path,
                        help="REQUIRED: copy this file as the bundle's brief.md")

    p_run = sub.add_parser("run", help="advance an issue to a halted state")
    p_run.add_argument("issue_id")

    # One verb for the whole cycle (#86): arity selects mode — one id is a single
    # sequential cycle, several ids fan out across lanes with a cheap-first sign-off
    # queue. Unbriefed ids are auto-planned (one shared Plan session); --from-csv with
    # no ids plans a batch the planner picks from the export. --rehearse (#87) dry-runs.
    p_flow = sub.add_parser("flow", help="run the cycle for one or more issues (Plan→Do→Check→sign-off→publish→Act)")
    p_flow.add_argument("issue_ids", nargs="*", help="issue ids; 1 → single cycle, N → batch; 0 + --from-csv → plan a batch from the export")
    p_flow.add_argument("--from-csv", help="tracker export to seed the interactive Plan of unbriefed ids")
    p_flow.add_argument("--from-briefs", type=Path, help="init any missing bundle from DIR/<id>.md before driving")
    p_flow.add_argument("--rehearse", action="store_true", help="dry-run: stub leaves + stub gates in an isolated bundle root (no Claude/Docker)")
    p_flow.add_argument("--no-publish", action="store_true", help="don't open the draft PR after an accept")
    p_flow.add_argument("--no-act", action="store_true", help="skip the Act leaf (Act runs by default after COMPLETE)")
    p_flow.add_argument("--by", default="", help="who signed off (recorded in §9)")
    p_flow.add_argument("--lanes", type=int, help="unattended Do+Check worker-pool size (docs 09; overrides [driver].lanes / PDCA_LANES)")
    p_flow.add_argument("--max-passes", type=int, help="sign-off pass budget before the driver stops driving a bundle (#260; overrides [driver].max_passes / PDCA_MAX_PASSES)")
    p_flow.add_argument("--auto-iterate", action="store_true", help="rebuild without stopping when every Check finding is implementation-level; a judgment finding still halts (#264; overrides [driver].auto_iterate / PDCA_AUTO_ITERATE)")
    p_flow.add_argument("--no-inhibit", action="store_true", help="don't hold a suspend inhibitor for the run (also PDCA_NO_INHIBIT=1) — for CI/containers where it's unavailable or unwanted (#244)")

    p_size = sub.add_parser("size",
                            help="print the a-priori slice-size estimate for one bundle or the queue (#320)")
    p_size.add_argument("issue_ids", nargs="*", help="ids to size; none => every briefed bundle")

    p_split = sub.add_parser("split",
                             help="propose a split for an oversized slice, then materialize it (#322/#323)")
    p_split.add_argument("issue_id")
    p_split.add_argument("--accept", action="store_true",
                         help="materialize the proposal's children; files their tracker "
                              "issues too unless --ids says they already exist")
    p_split.add_argument("--ids", default="",
                         help="comma-separated tracker ids, one per child, IN PROPOSAL "
                              "ORDER. Omit to have the child issues filed for you, each as "
                              "a sub-issue of the parent")

    p_status = sub.add_parser("status", help="list bundle states (cheap-first queue)")
    p_status.add_argument("issue_id", nargs="?")

    p_waves = sub.add_parser("waves",
                             help="show the computed dependency-wave plan for a batch (no build)")
    p_waves.add_argument("issue_ids", nargs="*",
                         help="ids to schedule; none → every in-flight briefed bundle")

    sub.add_parser("queue", help="the cheap-first sign-off burn-down (AWAITING_SIGNOFF)")

    p_gates = sub.add_parser("gates", help="run the deterministic Check gates (driver + CI share this)")
    p_gates.add_argument("issue_id", nargs="?")
    p_gates.add_argument("--working-tree", action="store_true", help="repo-scoped gates only (the CI merge re-gate)")
    p_gates.add_argument("--promotions", action="store_true",
                         help="list advisory checks clean for their promote_after cycles (#156)")

    # Reverse registry-consistency (issue #205) — a bundle-scoped gate cmd. Reads the
    # bundle from its arg or $PDCA_BUNDLE (set by the gate runner), so a [[gates.checks]]
    # entry is simply `cmd = "<cli> registry-check"`.
    p_reg = sub.add_parser("registry-check",
                           help="fail a patch that adds a manifest line for a path it doesn't touch (#205)")
    p_reg.add_argument("issue_id", nargs="?")

    # Contribution conformance — a bundle-scoped T4 gate. Lints the publisher's two
    # artifacts INDEPENDENTLY: the PR body must open with a user-impact line (before Root
    # cause) and both files must carry the tracker id. Reads $PDCA_BUNDLE like
    # registry-check, so a [[gates.checks]] entry is simply `cmd = "<cli> contribcheck"`.
    p_contrib = sub.add_parser("contribcheck",
                               help="fail a contribution whose PR body lacks a user-impact opener or the tracker id (T4)")
    p_contrib.add_argument("issue_id", nargs="?")
    p_contrib.add_argument("--no-issue", action="store_true",
                           help="pending-id: don't require the tracker trailer (still require the user-impact opener)")

    p_reval = sub.add_parser("revalidate",
                             help="re-run gates on a COMPLETE bundle vs the current engine; write a dated stamp (never re-decides §9)")
    p_reval.add_argument("issue_id")
    p_reval.add_argument("--date", help="ISO date for the stamp (default: today)")

    # Manual-test launch — `pdca try <id>` launches the patched build from the bundle's
    # worktree so a human can hands-on test it during Check (the visual/GUI §6 rows the
    # gates + headless reviewer can't decide). Runs [manual_test].cmd; advisory.
    p_try = sub.add_parser("try",
                           help="launch the patched build from the bundle's worktree for hands-on Check")
    p_try.add_argument("issue_id")

    # Drift sweep (issue #206) — flag published bundles whose patch no longer applies to the
    # current upstream base. Report-only; never re-decides §9.
    p_drift = sub.add_parser("drift",
                             help="flag COMPLETE-with-open-PR bundles whose patch no longer applies to the current base (#206)")
    p_drift.add_argument("--no-fetch", action="store_true",
                         help="skip `git fetch` (check against already-fetched base refs)")

    # Act tooling as one command group (#89): `act index` / `act log` / `act resolve`.
    # The help text is the operator's contract for the OUT-OF-TURN review path (#298):
    # every load-bearing fact an operator needs to run an Act review outside the flow's
    # cadence lives here, not only in module docstrings. RawDescription keeps the
    # epilog's command sequence lines intact; no literal `%` (argparse %-substitution),
    # and `%(prog)s` renders the per-instance command name (#73).
    p_act = sub.add_parser(
        "act", help="cross-cycle Act tooling (index / log / resolve)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Manual, out-of-turn Act review across frozen (COMPLETE) cycles.\n"
            "These commands carry no cadence gate — [driver].act_cadence throttles only\n"
            "the flow's auto-run Act — so run them whenever a review is worth doing.\n"
            "Needs at least one frozen COMPLETE bundle (otherwise `log` exits 1)."),
        epilog=(
            "typical out-of-turn review:\n"
            "  %(prog)s index                      survey frozen cycles + recurring signals\n"
            "  %(prog)s log --date <ISO>           preview the scaffolded entry (prints only)\n"
            "  %(prog)s log --date <ISO> --append  record it; also stamps process/.act-reviewed,\n"
            "                                      so the flow's next auto-Act won't re-cover\n"
            "                                      these cycles\n"
            "  %(prog)s resolve <signal>           mark an applied process delta (act-ledger.json)\n"
            "index/log default to the cycles frozen since the last review (the frontier\n"
            "recorded in .act-reviewed) — --all or --since widens to the full history;\n"
            "the scaffold's Process-deltas section is deliberately TODO — choosing the deltas\n"
            "is Act's irreducible human work"))
    act_sub = p_act.add_subparsers(dest="act_cmd", required=True)
    p_actidx = act_sub.add_parser(
        "index", help="read-only index of frozen cycles + recurring signals",
        description="Read-only index of frozen (COMPLETE) cycles, their §6/§7/§10 extracts "
                    "and recurring signals. No cadence gate; writes nothing. Defaults to "
                    "the cycles frozen since the last review (#299).")
    p_actidx.add_argument("--since", help="only cycles signed off on/after this ISO date "
                                          "(implies the full frozen history as base scope)")
    p_actidx.add_argument("--all", action="store_true",
                          help="cover every frozen cycle, not just those unreviewed since "
                               "the last Act")
    p_actlog = act_sub.add_parser(
        "log", help="scaffold a dated act-log entry (deltas left to the human)",
        description="Scaffold a dated act-log entry over the frozen (COMPLETE) cycles "
                    "(exits 1 when none exist). The Process-deltas section is left TODO "
                    "deliberately — choosing the deltas is Act's irreducible human work. "
                    "Without --append the entry is only printed (a safe preview). "
                    "Defaults to the cycles frozen since the last review (#299).")
    p_actlog.add_argument("--since", help="only consider cycles signed off on/after this ISO "
                                          "date (implies the full frozen history as base scope)")
    p_actlog.add_argument("--all", action="store_true",
                          help="cover every frozen cycle, not just those unreviewed since "
                               "the last Act")
    p_actlog.add_argument("--date", required=True, help="review date (ISO; Act is out-of-band so pass it)")
    p_actlog.add_argument("--append", action="store_true",
                          help="append to process/act-log.md AND advance the review frontier in "
                               "process/.act-reviewed — a manual Act review resets the flow's "
                               "cadence too, so the next auto-Act won't re-cover these cycles "
                               "(default: print only)")
    p_actres = act_sub.add_parser(
        "resolve",
        help="mark a tracked recurring signal as a delta you applied (#149)",
        description="Mark a tracked recurring signal as a process delta you applied. The "
                    "record lands in process/act-ledger.json; a later Act flags the signal "
                    "as an ineffective delta if it recurs after the applied date (#149).")
    p_actres.add_argument("signal", help="substring of the recurring signal to mark applied")
    p_actres.add_argument("--location", default="", help="where the delta landed (path:line / rule)")
    p_actres.add_argument("--date", help="applied date (ISO; default today)")

    # PR-review triage (issue #316): ingest a published PR's external review findings
    # into the Act ledger — the outer loop's highest-value signal, otherwise invisible
    # unless a human transcribes it.
    p_triage = sub.add_parser(
        "triage",
        help="ingest a published PR's external review findings into the Act ledger (#316)",
        description="Pull a published PR's reviews + review comments via `gh api`, "
                    "classify each finding (BUG / CONVENTION / NOISE / TEST-GAP) by "
                    "keyword heuristics (tunable from the instance rubric's class "
                    "list), route by class — a BUG on a merged PR files a tracker "
                    "issue whose body carries a carry-forward note; CONVENTION / "
                    "NOISE / TEST-GAP append candidate gate-row, rubric-line and "
                    "rubric-exclusion entries to process/act-log.md — and register "
                    "every finding in the Act ledger under a class-keyed signal "
                    "(codex-pr:<slug>), so `act` flags a class that recurs after its "
                    "process delta was applied. Proposes only: it never edits "
                    "pdca.toml or the rubric. Re-running the same PR ingests only "
                    "findings that arrived since the last run.")
    p_triage.add_argument("pr", help="the PR: a URL (https://github.com/OWNER/REPO/pull/N), "
                                     "OWNER/REPO#N, or a bare number with --repo")
    p_triage.add_argument("--repo", default="",
                          help="the PR's repository (OWNER/REPO) when `pr` is a bare number")
    p_triage.add_argument("--date", help="triage date (ISO; default today)")

    # Tracker reconciliation (issue #300): bundles and the issue tracker drift out of
    # sync; cleanup reports the discrepancies (dry-run default) and --apply acts.
    p_cleanup = sub.add_parser(
        "cleanup",
        help="reconcile bundle state with the issue tracker (dry-run; --apply acts; #300)",
        description="Match each bundle's state against its tracker issue: a closed issue "
                    "resolves its notes-only tracker bundle (RESOLVED, #302) or "
                    "discontinues one awaiting sign-off; a COMPLETE/DISCONTINUED bundle "
                    "whose issue is still open gets it commented and closed; a merged PR "
                    "on an unaccepted bundle is reported (never auto-accepted — the C6 "
                    "verdict stays human). Dry-run by default; --apply executes.")
    p_cleanup.add_argument("issue_ids", nargs="*",
                           help="bundle ids to reconcile (default: every issue_* bundle)")
    p_cleanup.add_argument("--apply", action="store_true",
                           help="execute the planned actions (default: report only)")
    p_cleanup.add_argument("--repo", default="",
                           help="GitHub repo of the tracker issues (OWNER/REPO; default: "
                                "the [[plan.source]] github provider's repo, or gh's default)")
    p_cleanup.add_argument("--by", default="", help="§9 attribution for discontinue records")

    # Footprint reclaim (issue #297): the on-demand counterpart of the flow's end-of-run
    # sweep. Distinct from tracker cleanup — this touches only harness-named sibling
    # worktrees of target checkouts, never bundles.
    p_sweep = sub.add_parser("sweep",
                             help="reclaim harness worktree/build footprint (lane, "
                                  "integration, overflow trees; #297)")
    p_sweep.add_argument("--remove", action="store_true",
                         help="remove lane worktrees entirely (default: clean their build "
                              "state, keep the checkouts warm)")
    p_sweep.add_argument("--dry-run", action="store_true",
                         help="report what would be reclaimed without touching anything")

    p_signoff = sub.add_parser("signoff", help="record the human Check sign-off (§9)")
    p_signoff.add_argument("issue_id")
    g = p_signoff.add_mutually_exclusive_group(required=True)
    g.add_argument("--accept", action="store_true", help="accept — merge wider")
    g.add_argument("--iterate-do", action="store_true", help="rebuild against same brief")
    g.add_argument("--iterate-plan", action="store_true", help="revise the brief")
    g.add_argument("--discontinue", action="store_true",
                   help="discontinue — record §9, no transition, drop from the pending set")
    p_signoff.add_argument("--by", default="", help="who signed off")
    p_signoff.add_argument("--delta", default="", help="iteration delta note")
    p_signoff.add_argument("--no-publish", action="store_true",
                           help="don't publish-on-accept (record §9, stop at COMPLETE)")

    p_publish = sub.add_parser("publish", help="Check's closing work: contribute an accepted fix as a draft PR")
    p_publish.add_argument("issue_id")
    p_publish.add_argument("--dry-run", action="store_true", help="print the git/gh commands without running them")
    p_publish.add_argument("--no-pr", action="store_true", help="push the branch but don't open the draft PR")
    p_publish.add_argument("--no-issue", action="store_true",
                           help="no tracker id yet: T4 drops only the tracker-id rule "
                                "(all else still blocks), record id_pending (vs a magic #0000)")
    p_publish.add_argument("--by", default="", help="who published (recorded in publish.json)")

    # Result-bundle recording (issue #317): commit terminal-finished bundles to the
    # instance repo (batch-by-default: one commit / one PR per invocation). Selection
    # is state.state ∈ state.TERMINAL — never a bundle in motion or one halted for a
    # human. Gated on [records] mode ("off" default: the verb refuses with a hint).
    p_record = sub.add_parser(
        "record",
        help="commit terminal-finished result bundles (COMPLETE / DISCONTINUED / "
             "RESOLVED) to the instance repo as one batch commit; [records] "
             'mode = "pr" also opens one PR for the batch (#317)')
    p_record.add_argument("issue_ids", nargs="*",
                          help="bundle ids to record (default: every terminal-finished "
                               "bundle; a non-terminal id is excluded, loudly)")
    p_record.add_argument("--dry-run", action="store_true",
                          help="print the git/gh commands without running them")

    p_doctor = sub.add_parser("doctor",
                              help="report every prerequisite (OK/MISSING/UNAUTH/WARN + fix hint); changes nothing")
    p_doctor.add_argument("--strict", action="store_true",
                          help="exit non-zero on ANY non-OK row (CI)")

    p_revert = sub.add_parser("revert",
                              help="undo a published contribution: a revert PR if merged, else withdraw the PR (#158)")
    p_revert.add_argument("issue_id")
    p_revert.add_argument("--dry-run", action="store_true", help="print the git/gh plan without mutating anything")
    p_revert.add_argument("--by", default="", help="who reverted (recorded in revert.json)")

    args = parser.parse_args(argv)
    # --rehearse (#87): a dry-run of the SAME control flow with stub leaves + stub gates
    # in an isolated bundle root — set before Config.load reads the env. setdefault so an
    # explicit env wins.
    if getattr(args, "rehearse", False):
        os.environ.setdefault("PDCA_LEAVES_MODE", "stub")
        os.environ.setdefault("PDCA_GATES_MODE", "stub")
        os.environ.setdefault("PDCA_BUNDLE_ROOT", ".rehearse")
    # Keep a long unattended `pdca flow` alive across host idle-suspend (#244): re-exec the
    # run under a platform suspend inhibitor before any real work. No-op when already
    # wrapped, opted out, or no inhibitor is available; execs (never returns) otherwise.
    # Only on the real CLI entry (argv is None → sys.argv): a programmatic main([...]) call
    # (tests, embedding) owns its own process and must not be replaced via sys.argv.
    if args.cmd == "flow" and argv is None:
        _inhibit_suspend_and_reexec()
    # Surface config problems as a clean one-line error, not a traceback (issue #92):
    # running outside a rendered project (no pdca.toml) is operator error, not a crash.
    try:
        cfg = Config.load()
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)  # "no pdca.toml found … — run inside a rendered project"
        return 2
    except ValueError as exc:  # malformed pdca.toml (tomllib) or a bad config value
        print(f"pdca: invalid pdca.toml — {exc}", file=sys.stderr)
        return 2

    if not args.cmd:  # bare invocation → the status dashboard (#88)
        return _status(cfg, None)
    if args.cmd == "init-issue":
        return _init_issue(cfg, args.issue_id, args.from_brief)
    if args.cmd in ("run", "flow"):
        # Fail closed on an unresolvable worktree base (#235): a clean one-line error, not a
        # traceback. Batch flow isolates this per bundle already; this covers single-bundle
        # `run` / `flow <id>`, which drive Do (the only WorktreeError-raising beat) directly.
        try:
            return _run(cfg, args.issue_id) if args.cmd == "run" else _flow(cfg, args)
        except worktree.WorktreeError as exc:
            print(f"pdca: {exc}", file=sys.stderr)
            return 1
    if args.cmd == "status":
        return _status(cfg, args.issue_id)
    if args.cmd == "waves":
        return _waves(cfg, args.issue_ids)
    if args.cmd == "queue":
        return _queue(cfg)
    if args.cmd == "gates":
        return _gates(cfg, args)
    if args.cmd == "registry-check":
        return _registry_check(cfg, args)
    if args.cmd == "contribcheck":
        return _contribcheck(cfg, args)
    if args.cmd == "revalidate":
        return _revalidate(cfg, args)
    if args.cmd == "try":
        return manual_test.launch(cfg, args.issue_id)
    if args.cmd == "drift":
        return _drift(cfg, args)
    if args.cmd == "act":
        return _act(cfg, args)
    if args.cmd == "triage":
        return triage.run(cfg, args.pr, repo=args.repo,
                          date=args.date or datetime.date.today().isoformat())
    if args.cmd == "signoff":
        return _signoff(cfg, args)
    if args.cmd == "publish":
        return publish.publish(cfg, args.issue_id, dry_run=args.dry_run,
                               open_pr=not args.no_pr, by=args.by, pending_id=args.no_issue)
    if args.cmd == "record":
        return record.record(cfg, args.issue_ids, dry_run=args.dry_run)
    if args.cmd == "cleanup":
        return cleanup.run(cfg, args.issue_ids, apply=args.apply, repo=args.repo, by=args.by)
    if args.cmd == "doctor":
        return doctor.run(cfg, strict=args.strict)
    if args.cmd == "size":
        return _size(cfg, args.issue_ids)
    if args.cmd == "split":
        return _split(cfg, args)
    if args.cmd == "sweep":
        # Explicit mode so the manual command works even under sweep_worktrees = "off".
        lines = sweep.sweep(cfg, mode="remove" if args.remove else "clean",
                            dry_run=args.dry_run)
        for line in lines:
            print(line)
        if not lines:
            print("sweep: nothing to reclaim (no harness worktrees found)")
        return 0
    if args.cmd == "revert":
        return revert.revert(cfg, args.issue_id, dry_run=args.dry_run, by=args.by)
    return 2


def _init_issue(cfg: Config, issue_id: str, from_brief: Path | None) -> int:
    # init-issue seeds a bundle from a brief you authored OUTSIDE the loop. With no
    # --from-brief it used to copy the blank brief.md.tpl, which left a content-less
    # PLANNED bundle that bypassed the planner (the Plan pre-pass only plans UNPLANNED)
    # and whose hint lines parsed as a bogus depends_on — a footgun (#113). To start a
    # new issue from its ticket, `pdca flow <id>` auto-plans; init-issue is now strictly
    # the pre-authored-brief seeder.
    if from_brief is None:
        print("init-issue needs --from-brief <file>. To start a new issue from its "
              f"ticket, run `pdca flow {issue_id}` — it auto-plans (scrapes the ticket "
              "and authors the brief).", file=sys.stderr)
        return 2
    if not from_brief.exists():
        print(f"no brief source: {from_brief}", file=sys.stderr)
        return 1
    d = cfg.bundle(issue_id)
    if d.exists():
        print(f"bundle already exists: {d}", file=sys.stderr)
        return 1
    d.mkdir(parents=True)
    shutil.copyfile(from_brief, d / "brief.md")
    print(f"{state.state(d)}\t{d}")
    return 0


def _run(cfg: Config, issue_id: str) -> int:
    d = cfg.bundle(issue_id)
    if not d.exists():
        print(f"no such bundle: {d}", file=sys.stderr)
        return 1
    final = driver.run_issue(d, cfg)
    print(f"{final}\t{d}")
    if driver.held(final):
        # Non-zero, or automation reads a bundle blocked before Do as a completed run. The
        # reasons were already printed by the driver; this is the exit code that carries
        # them to a caller that never sees stderr.
        print(f"run: {d.name} is held at {final} — resolve the item(s) above and re-run",
              file=sys.stderr)
        return 1
    if final == state.AWAITING_SIGNOFF:
        open_items = signoff.open_needs_human(d / "SUMMARY.md")
        if open_items:
            print(f"  §6 NEEDS-HUMAN ({len(open_items)} open) — clear before accept:")
            for it in open_items:
                print(f"    {it}")
    return 0


def _flow(cfg: Config, args: argparse.Namespace) -> int:
    """Run the whole cycle for one or more issues (the single ``flow`` verb, #86).

    Arity selects the PRESENTATION, not the machinery (issue #468): **one or more ids**
    all drive through :func:`flow.flow_ids` and the ONE results map it returns — one id
    reports as ``state<TAB>path`` (+ the §6 listing), several as the batch table — while
    **zero ids + --from-csv** plans a batch the planner picks from the export. A single id
    therefore runs a one-bundle wave (Plan→Do→Check→sign-off→publish→Act), not a second,
    parallel implementation of the same cycle. Unbriefed ids are auto-planned (one shared
    interactive Plan session) — no --plan flag. Act runs by default (--no-act to skip).
    """
    if getattr(args, "lanes", None) is not None:
        cfg.lanes = max(1, args.lanes)
    if getattr(args, "max_passes", None) is not None:
        cfg.max_passes = max(1, args.max_passes)   # issue #260
    if getattr(args, "auto_iterate", False):
        cfg.auto_iterate = True                    # issue #264 (flag only opts IN)
    ids = list(args.issue_ids)

    # --from-briefs: seed any missing bundle from DIR/<id>.md before driving.
    if args.from_briefs:
        for iid in ids:
            d = cfg.bundle(iid)
            if d.exists():
                continue
            src = args.from_briefs / f"{iid}.md"
            if not src.exists():
                print(f"  skip {iid}: no brief at {src}", file=sys.stderr)
                continue
            d.mkdir(parents=True)
            shutil.copyfile(src, d / "brief.md")

    do_publish, do_act = not args.no_publish, not args.no_act

    if not ids:  # batch the planner picks from the export
        if not args.from_csv:
            print("flow needs one or more issue ids, or --from-csv to plan a batch from "
                  "a tracker export", file=sys.stderr)
            return 2
        try:
            return _report_batch(flow.flow_batch(
                cfg, csv=args.from_csv, do_publish=do_publish, do_act=do_act, by=args.by))
        except flow.PreflightError as exc:
            print(f"flow: {exc}", file=sys.stderr)
            return 1

    # ONE routing path and ONE results map, for BOTH CLI shapes (issue #468). The single-id
    # route used to be structurally different machinery — a pre-run short-circuit on raw disk
    # state, then `flow.flow` returning a bare state string, with no `except PreflightError`
    # — and five iterations of #449 broke "both shapes do the same thing to the same disk" by
    # a new route each round. There is nothing left to keep in step: `flow_ids` revalidates a
    # cached RESOLVED marker against the live tracker (`flow.py:1619-1633`), skips an
    # already-terminal bundle with its recovery hint (`flow.py:1654-1660`), and reports a
    # disposition for EVERY id it was given — so both decisions, and the map the answer comes
    # from, live in exactly one place.
    try:
        results = flow.flow_ids(cfg, ids, plan_missing=True, csv=args.from_csv,
                                do_publish=do_publish, do_act=do_act, by=args.by)
    except flow.PreflightError as exc:
        print(f"flow: {exc}", file=sys.stderr)
        return 1

    # Two PRESENTATIONS of that one map — never two drive paths, and never a second source
    # of truth: both read only `results`, so neither can report what the other cannot see.
    return _report_single(cfg, ids[0], results) if len(ids) == 1 else _report_batch(results)


#: States a flow run counts as a SUCCESSFUL terminal: finished, or settled in the tracker
#: outside the cycle (#302 review round 11) — a correctly skipped item is not a failed flow.
_FLOW_OK = (state.COMPLETE, state.RESOLVED)


def _results_rc(results: dict[str, str], ok: tuple[str, ...] = _FLOW_OK) -> int:
    """The ONE exit-code rule over a flow results map (issue #468): 0 iff every bundle in it
    reached a successful terminal. An EMPTY map — nothing was asked for, or nothing driven —
    is 0: a no-op is not a failure. (Only ``flow_batch``, which is handed no ids, can produce
    one: :func:`flow.flow_ids` answers for every id it is given.)

    ``ok`` carries the single documented difference between the shapes, in one place instead
    of as an emergent property of two drive paths: a SINGLE-id run adds AWAITING_SIGNOFF,
    because stopping for the human who just typed the command is the intended end of that
    run, not a failure. A multi-id set keeps the batch rule (rc 0 iff all COMPLETE/RESOLVED).
    """
    return 0 if all(s in ok for s in results.values()) else 1


def _report_entry(cfg: Config, iid: str, st: str) -> None:
    """One bundle's stdout report in the single-id shape: the documented ``state<TAB>path``
    machine contract, plus the §6 listing when it stopped for the human.

    One shape for every bundle the run answers for, named or adopted (#473) — so a caller
    parsing stdout reads the same two fields for each, and the run cannot describe a child
    it drove in a format it never described the named id in.
    """
    d = cfg.bundle(iid)
    print(f"{st}\t{d}")
    if st == state.AWAITING_SIGNOFF:
        for it in signoff.open_needs_human(d / "SUMMARY.md"):
            print(f"    {it}")


def _report_single(cfg: Config, iid: str, results: dict[str, str]) -> int:
    """The single-id presentation of the shared results map (issue #468).

    Preserved from the pre-#468 single-id route, now DERIVED from the map the batch shape
    also reports: the `state<TAB>path` line on stdout, the AWAITING_SIGNOFF listing of open
    §6 items, and the rc-0 stop-for-the-human semantics.

    Indexed, never ``.get``-with-a-fallback: :func:`flow.flow_ids` answers for every id it
    was given, so a disk read here would be a SECOND authority — the one that let the two
    shapes disagree about the same bundle. If the key is ever missing the map contract is
    broken and the loud KeyError is the correct outcome, not a quietly divergent report.

    The map can hold bundles the operator never typed — split adoption puts the children of
    a bundle this run drove, or recovered, into it (#469, #473) — and the exit code is
    derived from ALL of them. Reporting only ``iid`` then printed ``COMPLETE`` while exiting
    1: a caller reading the machine contract saw a successful run, and the bundle that
    actually failed it was named nowhere it was looking. The worst shape is the recovery
    one, where ``iid``'s own ``COMPLETE`` was written by an EARLIER run, so stdout carried
    nothing this run did at all. Every other entry therefore gets the SAME line, after the
    named id's — the whole answer, in the one format, whatever the rc.

    Printing them unconditionally rather than only on a failure is deliberate: a child left
    AWAITING_SIGNOFF keeps the run at rc 0 (stopping for the human is this shape's intended
    end, :func:`_results_rc`), so a failure-gated report would stay silent about the bundle
    that is waiting for them. The terse one-line shape a non-adopting run prints is
    untouched — ``flow_ids`` answers for exactly the ids it was given, so a second entry can
    only come from an adoption (pinned by
    ``test_a_single_id_run_that_adopts_nothing_still_prints_exactly_one_line``).
    """
    _report_entry(cfg, iid, results[iid])
    for other in sorted(results):
        if other != iid:
            _report_entry(cfg, other, results[other])
    return _results_rc(results, ok=(*_FLOW_OK, state.AWAITING_SIGNOFF))


def _report_batch(results: dict[str, str]) -> int:
    """Print a batch result map and return a process code — :func:`_results_rc`'s rule
    (0 iff every bundle reached a SUCCESSFUL terminal — COMPLETE, or RESOLVED (#302 review
    round 11): a tracker item settled outside the cycle is a successful no-op, and
    automation must not read it as a failed flow), which the single-id shape derives its
    own exit code from too (issue #468)."""
    if not results:
        print("flow: nothing to drive — no in-flight briefs among the ids.", file=sys.stderr)
        return 0
    for iid, st in sorted(results.items()):
        print(f"{st}\t{iid}")
    done = sum(1 for s in results.values() if s in _FLOW_OK)
    resolved = sum(1 for s in results.values() if s == state.RESOLVED)
    tail = f" ({resolved} resolved in the tracker)" if resolved else ""
    print(f"flow: {done}/{len(results)} complete{tail}")
    return _results_rc(results)


def _prog() -> str:
    """The command name this instance actually installs — rendered projects namespace it
    (`pdca-gramps`), so hard-coding `pdca` prints guidance that is not executable there."""
    name = Path(sys.argv[0] or "").name
    # `-`, `-c`, `__main__.py` and the like are not command names an operator can retype.
    return name if name and name.isidentifier() or name.startswith("pdca") else "pdca"


def _split(cfg: Config, args) -> int:
    """`pdca split <id>` drafts a proposal; `--accept` materializes it.

    Two verbs in one because they are two halves of one decision and the second is
    meaningless without the first: the human reads the proposal, then accepts it.

    Every PR needs its own issue, and child slices are no exception. Without `--ids` this
    now FILES those issues — one per child, each a real sub-issue of the parent (#358).
    #323 left that to the human to keep the tracker the source of truth; inside an
    interactive Plan session that objection is much weaker (the human is present and
    approving) and the friction is real — leave the session, file N issues by hand, come
    back with the numbers. `--ids` stays for a human who has already filed them, and is
    REQUIRED for a tracker this cannot reach.
    """
    d = cfg.bundle(args.issue_id)
    if not args.accept:
        return leaves.do_split(d, cfg)

    # Normalise `#601` -> `601`. The configured issue_id_example is `#123` and the brief
    # parser strips the prefix from dependency lists, so `--ids '#601,#602'` is the natural
    # thing to type — but it would create `issue_#601` while `compute_waves` looked for
    # `issue_601`, and the printed follow-up command would be truncated by the shell at `#`.
    # Normalise the two shapes an operator naturally types. `#601` comes from
    # issue_id_example; `issue_601` comes from copying a bundle directory name. Both are
    # the same tracker id, and `brief._id_list` already strips either when reading a
    # dependency — so leaving them raw creates `issue_issue_601` while the rewritten
    # `Depends on` resolves to `601`, and `pdca flow` aborts on an unresolved dependency
    # AFTER the parent has been marked split.
    ids = []
    for token in args.ids.split(","):
        token = token.strip().lstrip("#").strip()
        if token.startswith("issue_"):
            token = token[len("issue_"):]
        if token:
            ids.append(token)
    filed = False
    # Read the proposal and run `preflight` on BOTH shapes, before either does anything
    # irreversible (issue #459). It carries the checks that do not need the ids — a second
    # acceptance filed a whole second set of real sub-issues before discovering the parent
    # was already split; a cyclic proposal filed its children before `validate` refused
    # them — and now also the CONVERGENCE REPORT, the one statement of whether this split
    # actually makes the children smaller. `--ids` reached neither: it went straight to
    # `accept`, and it is the path the docs call *required* for a tracker this cannot reach
    # — the operator who has already paid for the issues by hand, and so most needs the
    # verdict. Tracker issues cannot be withdrawn and a materialised bundle is barely
    # better, so this order is the whole guarantee.
    try:
        children = split.parse((d / split.PROPOSAL).read_text(encoding="utf-8"))
        split.preflight(d, children, cfg)
    except OSError:
        split.advisory(f"split: {d.name} has no {split.PROPOSAL} — run "
                       f"`{_prog()} split {args.issue_id}` first")
        return 1
    except split.SplitError as exc:
        split.advisory(f"split: {exc}")
        return 1
    if not ids:
        # No ids given: file one issue per child, parented to this bundle's issue.
        try:
            ids = split.file_children(d, children, cfg, prog=_prog())
        except split.TrackerUnavailable as exc:
            # Never a silent skip: name the reason AND the way forward. A split that
            # filed nothing and materialised nothing would otherwise look like a no-op.
            split.advisory(f"split: {exc}. File one issue per child yourself and pass them "
                           f"in proposal order:\n  {_prog()} split {args.issue_id} "
                           "--accept --ids "
                           + ",".join(f"<id-{n}>" for n in range(1, len(children) + 1)))
            return 1
        except split.SplitError as exc:
            split.advisory(f"split: {exc}")
            return 1
        filed = True
        # A closed or full stderr must not abort the run HERE: the issues exist, and
        # stopping between filing and accepting is the one state with no artifact naming
        # them. `split.advisory` absorbs it; the failure paths below re-print the numbers.
        split.advisory(f"filed {len(ids)} child issue(s): "
                       + ", ".join("#" + i for i in ids))
    try:
        created = split.accept(d, ids, cfg)
    except split.SplitError as exc:
        split.advisory(f"split: {exc}")
        if filed:
            # The issues are real and cannot be withdrawn. Say so explicitly and give the
            # command that resumes against them, or they are orphaned with nothing on
            # screen naming them — the one failure this feature must not have.
            split.advisory("split: the child issues were already filed and CANNOT be "
                           "rolled back: " + ", ".join("#" + i for i in ids) + ".")
            if "already marked" in str(exc):
                # `preflight` passed and `accept` then found the parent terminal, so
                # ANOTHER acceptance won the race between them. Printing the ordinary
                # retry here would be a false instruction: it cannot succeed against an
                # already-split parent, and following it would file a third set.
                split.advisory(
                    "split: the parent was marked split by another run while these were "
                    "being filed, so its children already exist. Do NOT re-run --accept: "
                    "close the issues above as duplicates, or reopen the parent if this "
                    "run's split is the one you want.")
            else:
                split.advisory("Fix the problem above, then re-run against them:\n"
                               f"  {_prog()} split {args.issue_id} --accept --ids "
                               f"{','.join(ids)}")
        return 1
    # Everything below is advisory in the strongest sense: the bundles are on disk and the
    # issues are filed, so no write here can change the outcome — but an unguarded one
    # could still change the EXIT CODE. `pdca split 500 --accept 2>&1 | head` breaks both
    # streams part-way and, before #459, turned a completed acceptance into a traceback.
    for child in created:
        split.advisory(str(child), file=sys.stdout)
    split.advisory(f"{d.name} marked split; run `{_prog()} flow {' '.join(ids)}` to drive "
                   "the children")
    return 0


def _size(cfg: Config, issue_ids: list[str]) -> int:
    """Read-only: the structural size estimate and WHY, for one bundle or the queue.

    Read-only on purpose — it decides nothing and writes nothing, so it can be run against
    a live queue at any time. The reasons matter more than the band: "3 conflicts declared"
    and "predicts a large patch" call for different responses, and a bare band hides which
    fired (#320).
    """
    if issue_ids:
        bundles = []
        for i in issue_ids:
            d = cfg.bundle(i)
            # An explicit id that names nothing must be REPORTED, not sized: the estimator
            # fail-opens to `ok` by design (a detector that crashes Plan is worse than one
            # that abstains), so a typo would otherwise print a confident `ok` and exit 0.
            if not (d / "brief.md").is_file():
                print(f"size: {d.name} has no brief.md — nothing to size", file=sys.stderr)
                return 1
            bundles.append(d)
    else:
        bundles = sorted(b for b in cfg.bundle_root.glob("issue_*")
                         if b.is_dir() and (b / "brief.md").exists()) \
            if cfg.bundle_root.exists() else []
    if not bundles:
        print("(no briefed bundles to size)")
        return 0
    for d in bundles:
        # Fold in a STORED sizer verdict — read, never invoked: `pdca size` is documented
        # read-only and must stay safe to run against a live queue. Without this the one
        # deliberate way to ask "how big is this?" showed only the structural bands and
        # never the decomposability answer the instance had already paid a model for.
        est = sizing.combine(sizing.estimate(d / "brief.md", cfg), leaves.current_sizing(d, cfg), cfg)
        print(f"{est.band}\t{d.name}\tscore={est.score} "
              f"churn={est.churn_band} patch={est.patch_band}"
              + (f" sizer={est.model_band}" if est.model_band else ""))
        for reason in est.reasons:
            print(f"    - {reason}")
        stored = leaves.current_sizing(d, cfg) or {}
        # LIST or nothing. The verdict is model output and the contract is deliberately
        # tolerant of an untidy schema — but tolerant has to mean "ignored", not "iterated":
        # `proposed_seams: 1` crashed this command, and a string printed one "seam" per
        # character.
        seams = stored.get("proposed_seams")
        for seam in seams if isinstance(seams, list) else []:
            print(f"    seam: {seam}")
    return 0


def _status(cfg: Config, issue_id: str | None) -> int:
    if issue_id:
        d = cfg.bundle(issue_id)
        print(f"{state.state(d)}\t{d}")
        return 0
    bundles = sorted(cfg.bundle_root.glob("issue_*")) if cfg.bundle_root.exists() else []
    if not bundles:
        print("(no bundles yet)")
        return 0
    rows = [(state.state(d), d) for d in bundles if d.is_dir()]
    rows.sort(key=lambda r: (_STATE_ORDER.index(r[0]) if r[0] in _STATE_ORDER else 99, r[1].name))
    for s, d in rows:
        flag = ""
        # Oversize marker (#320/#321): visible in the queue without running anything,
        # since the estimate is a pure read of the brief.
        # Computed first, APPENDED below — the sign-off annotation used to overwrite it,
        # hiding the marker precisely at the queue's human touch point.
        # Same combination `pdca size` uses. Structure alone would omit the marker in the
        # one case the sizer exists for — a brief structure scores `ok`/`watch` that the
        # model finds decomposable — so the two commands would disagree at the sign-off
        # queue, which is where a human is actually looking.
        oversized = (d / "brief.md").exists() and sizing.combine(
            sizing.estimate(d / "brief.md", cfg),
            leaves.current_sizing(d, cfg), cfg).band == sizing.OVERSIZED
        if s == state.AWAITING_SIGNOFF:
            n = len(signoff.open_needs_human(d / "SUMMARY.md"))
            flag = "  [cheap: confirm]" if n == 0 else f"  [{n} NEEDS-HUMAN]"
        if s == state.COMPLETE:  # publish visibility (#97): is the accepted fix actually out?
            flag += _publish_flag(d)
        blocked = _blocked_by(cfg, d) if s != state.COMPLETE else []
        if blocked:
            flag += f"  [blocked-by: {', '.join(blocked)}]"
        if oversized:
            flag += "  [oversized]"
        print(f"{s:18}{d.name}{flag}")
    return 0


def _waves(cfg: Config, ids: list[str]) -> int:
    """Print the computed dependency-wave plan for a batch — deterministic, no build
    (#wave-model). With no ids, schedules every in-flight briefed bundle. An unschedulable
    graph (cycle / unresolved dep) is reported, not run."""
    if ids:
        # The explicit-id branch applies the SAME terminal filter as the no-id scan
        # (#302 review rounds 3/9): `pdca flow <id>` skips a terminal bundle, so the
        # preview must agree instead of reporting settled work as a runnable wave.
        bundles = []
        for i in ids:
            d = cfg.bundle(i)
            if not (d / "brief.md").exists():
                continue
            s = state.state(d)
            if s in (state.COMPLETE, state.DISCONTINUED, state.RESOLVED):
                print(f"waves: {d.name} — already terminal ({s}), excluded",
                      file=sys.stderr)
                continue
            bundles.append(d)
    elif cfg.bundle_root.exists():
        # RESOLVED is terminal too (#302 review round 2, mirrored round 8): a resolved
        # bundle with a stray placeholder brief has brief.md on disk, so the file test
        # alone would schedule settled work — filter on the terminal set, not just
        # COMPLETE/DISCONTINUED; the preview must match the flow's drive set.
        bundles = sorted((d for d in cfg.bundle_root.glob("issue_*")
                          if d.is_dir() and (d / "brief.md").exists()
                          and state.state(d) not in (state.COMPLETE, state.DISCONTINUED,
                                                     state.RESOLVED)),
                         key=lambda p: p.name)
    else:
        bundles = []
    if not bundles:
        print("(no briefed bundles to schedule)")
        return 0
    try:
        plan = waves.compute_waves(cfg, bundles)
    except ValueError as exc:
        print(f"unschedulable: {exc}", file=sys.stderr)
        return 1
    print(f"{len(bundles)} bundle(s) → {len(plan)} wave(s) ({cfg.wave_mode} mode; "
          f"each wave builds on the prior's accepted work):")
    for k, wave in enumerate(plan):
        print(f"  wave {k}: " + ", ".join(d.name.removeprefix("issue_") for d in wave))
    return 0


def _publish_flag(d: Path) -> str:
    """A COMPLETE bundle's publish state (#97): a real publish writes publish.json with the
    PR url; absent ⇒ accepted-but-unpublished (dry-run / no-target / failed / not-yet-run),
    so it's visible instead of looking published. A close/no-fix bundle has no patch to ship."""
    pj = d / "publish.json"
    if not pj.exists():
        if not (d / "patch.diff").is_file() or not (d / "patch.diff").read_text(encoding="utf-8").strip():
            return "  [close: no PR]"
        return "  [unpublished]"
    try:
        rec = json.loads(pj.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return "  [published]"
    url, base = rec.get("pr_url"), rec.get("base")
    if not url:
        return "  [published]"
    # A stacked PR (#wave-model / #123) targets the wave integration branch, not the base —
    # show ↑<base> so the human knows to merge the stack bottom-up.
    stacked = rec.get("mode") in ("stacked-pr", "stacked")
    return f"  [PR {url}{f' ↑{base}' if stacked else ''}]"


def _blocked_by(cfg: Config, d: Path) -> list[str]:
    """Declared prerequisites of bundle ``d`` that aren't satisfied yet.

    `Depends on` ids not yet COMPLETE (issue #36), plus `Depends on (merged)` ids whose
    PR isn't merged yet, tagged ``(unmerged)`` so the held dependent reads as awaiting a
    human merge, not a stuck cycle (issue #107)."""
    bp = d / "brief.md"
    if not bp.exists():
        return []
    blocked = [dep for dep in brief.depends_on(bp)
               if state.state(cfg.find_bundle(dep)) != state.COMPLETE]  # archived prereq too (#171)
    blocked += [f"{dep} (unmerged)" for dep in brief.depends_on_merged(bp)
                if not merged.is_merged(cfg, dep)]
    return blocked


def _queue(cfg: Config) -> int:
    """Render the cheap-first sign-off burn-down."""
    entries = queue.awaiting_signoff(cfg)
    if not entries:
        print("(sign-off queue empty)")
        return 0
    cheap = sum(1 for e in entries if e.cheap)
    print(f"sign-off queue — {len(entries)} awaiting ({cheap} cheap-confirm, {len(entries) - cheap} need adjudication):")
    for e in entries:
        flag = "[cheap: confirm]" if e.cheap else f"[{e.open_needs_human} NEEDS-HUMAN]"
        print(f"  {e.bundle.name:24}{flag}")
    return 0


def _gates(cfg: Config, args: argparse.Namespace) -> int:
    """Run gates; print the table; exit nonzero iff a gating row failed.

    The single-sourced entry point: the driver runs gates per bundle during Do,
    CI runs ``pdca gates --working-tree`` on the PR — same impl, same pdca.toml.
    """
    if getattr(args, "promotions", False):
        return _gates_promotions(cfg)
    if args.working_tree:
        result = gates.run_working_tree(cfg)
    else:
        if not args.issue_id:
            print("gates needs an issue id (or --working-tree)", file=sys.stderr)
            return 2
        d = cfg.bundle(args.issue_id)
        if not d.exists():
            print(f"no such bundle: {d}", file=sys.stderr)
            return 1
        result = gates.run_gates(d, cfg)
    print(gates.render_md(result))
    return 1 if result["overall"] == "fail" else 0


def _gates_promotions(cfg: Config) -> int:
    """List advisory checks that have earned promotion to gating (#156) — hint-only."""
    cands = gates.promotion_candidates(cfg)
    if not cands:
        print("no advisory checks ready to promote "
              "(none with `promote_after` clean across the threshold of recent cycles)")
        return 0
    print("Advisory checks that have earned promotion to gating "
          "(flip `gating = true` in pdca.toml):")
    for c in cands:
        print(f"  - {c['id']}: {c['label']}  "
              f"(passed ≥ {c['threshold']} most-recent frozen cycles)")
    return 0


def _drift(cfg: Config, args: argparse.Namespace) -> int:
    """Drift sweep (#206): report published bundles whose patch no longer applies to the
    current upstream base. Report-only — always exits 0 (never re-decides §9)."""
    rows = drift.sweep(cfg, fetch=not getattr(args, "no_fetch", False))
    if not rows:
        print("drift: no published COMPLETE bundles to check.")
        return 0
    stale = [r for r in rows if r["status"] == "needs-rebase"]
    errors = [r for r in rows if r["status"] == "error"]
    for r in stale:
        print(f"  needs-rebase  {r['bundle']}  (vs {r['base']})  {r['pr_url']}")
        print(f"      {r['detail']}")
    for r in errors:
        print(f"  unknown       {r['bundle']}  (vs {r['base']})  {r['detail']}")
    ok = len(rows) - len(stale) - len(errors)
    print(f"\ndrift: {len(rows)} checked · {ok} apply-clean · "
          f"{len(stale)} needs-rebase · {len(errors)} unknown")
    return 0


def _registry_check(cfg: Config, args: argparse.Namespace) -> int:
    """Reverse registry-consistency gate (#205): fail iff the bundle's patch adds a line to a
    configured manifest for a path the patch doesn't touch. The bundle is the ``issue_id``
    arg or ``$PDCA_BUNDLE`` (set by the gate runner). Not-configured / no-patch ⇒ pass
    (default-open, like an unconfigured gate)."""
    files = cfg.registry_consistency.get("files") or []
    if not files:
        return 0  # no registry files declared → nothing to enforce
    if args.issue_id:
        d = cfg.bundle(args.issue_id)
    elif os.environ.get("PDCA_BUNDLE"):
        d = Path(os.environ["PDCA_BUNDLE"])
    else:
        print("registry-check needs an issue id or $PDCA_BUNDLE", file=sys.stderr)
        return 2
    patch = d / "patch.diff"
    if not patch.is_file() or not patch.read_text(encoding="utf-8").strip():
        return 0  # no patch to inspect (close/no-fix bundle)
    pattern = cfg.registry_consistency.get("pattern", "")
    violations = registry.find_violations(patch.read_text(encoding="utf-8"), files, pattern)
    for v in violations:
        print(v)
    return 1 if violations else 0


def _contribcheck(cfg: Config, args: argparse.Namespace) -> int:
    """Contribution-conformance gate (T4): lint the publisher's PR body + commit message so
    a weak model's output is caught before publish. Fails when the PR body has no non-empty
    ``**User impact:**`` opener (or it falls AFTER Root cause), or — for a real numeric
    ticket — the tracker id is absent from either ``commit-msg.txt`` or ``pr-description.md``
    (the two are linted INDEPENDENTLY, so a ticketed fix needs the id in BOTH). The bundle is
    the ``issue_id`` arg or ``$PDCA_BUNDLE`` (set by the gate runner). No patch (close/no-fix)
    ⇒ pass (default-open, like an unconfigured gate).

    **Artifacts not drafted yet ⇒ a declared deferral, not a bare pass** (issue #401). This
    row is registered for Check *and* re-run by ``publish`` (``publish.publish_gates``), and
    at Check time the two artifacts it lints do not exist — publish drafts them later. The
    exit code stays 0 (nothing failed), but the gate now DECLARES that its substantive audit
    has no subject yet (``gates.DEFERRED_MARKER``), so the matrix records ``deferred`` with
    the reason instead of a green the reviewer cannot reproduce — the artifacts the row names
    are not among its inputs, which is why every cycle escalated this by-design condition to
    SUMMARY §6 NEEDS-HUMAN. The substantive verdict is unchanged and still hard-gates the
    push (``publish._t4_passes``)."""
    if args.issue_id:
        d = cfg.bundle(args.issue_id)
    elif os.environ.get("PDCA_BUNDLE"):
        d = Path(os.environ["PDCA_BUNDLE"])
    else:
        print("contribcheck needs an issue id or $PDCA_BUNDLE", file=sys.stderr)
        return 2
    patch = d / "patch.diff"
    if not patch.is_file() or not patch.read_text(encoding="utf-8").strip():
        return 0  # close / no-fix bundle: nothing contributed
    if not (d / publish.PR_BODY).is_file():
        # Artifacts not drafted yet (Check-time gate, pre-publish): the gate ran and found
        # its subject absent BY DESIGN. Declare the deferral (#401) rather than exit 0 mute,
        # so the row records `deferred — re-gated at publish` instead of a vacuous green.
        print(f"{gates.DEFERRED_MARKER} {publish.PR_BODY} not drafted yet — the substantive "
              "T4 audit of the contribution artifacts runs at publish")
        return 0
    # `--no-issue` also arrives as $PDCA_PENDING_ID (#384): `publish._t4_passes` derives it
    # from the `publish --no-issue` flag on each run (scrubbing any inherited value), so the
    # SHIPPED gate row relaxes exactly the tracker-id requirement in pending-id mode without
    # its registered cmd line ever changing — an in-place edit of that line breaks `copier
    # update` for any instance that appended a row beside it (tests/test_update_compat.py).
    no_issue = args.no_issue or bool(os.environ.get("PDCA_PENDING_ID"))
    problems = contribution_problems(d, no_issue=no_issue)
    for p in problems:
        print(f"contribcheck: {p}", file=sys.stderr)
    return 1 if problems else 0


def contribution_problems(d: Path, *, no_issue: bool = False) -> list[str]:
    """The T4 lint core, as a problem list — the single source of the contribution rules.

    Extracted from :func:`_contribcheck` (issue #331) so the publisher's exit-contract
    check (``handoff.check_publisher``) reuses the instance's own deterministic lint
    rather than re-declaring it; the gate and the /handoff verdict cannot drift apart.
    """
    issue_id = d.name.removeprefix("issue_")
    pr_path = d / publish.PR_BODY
    commit_path = d / publish.COMMIT_MSG
    pr_text = pr_path.read_text(encoding="utf-8") if pr_path.is_file() else ""
    problems: list[str] = []
    # 1) A non-empty `**User impact:**` opener that PRECEDES Root cause — the user-visible
    #    effect must lead (what a weak model tends to drop).
    impact = re.search(r"(?im)^[ \t>]*\*\*User impact:\*\*[ \t]*(\S.*)$", pr_text)
    if not impact:
        problems.append("PR body must open with a non-empty `**User impact:**` line "
                        "(the user-visible effect, before Root cause)")
    else:
        root = re.search(r"(?im)^#+[ \t]*Root cause\b", pr_text)
        if root and impact.start() > root.start():
            problems.append("`**User impact:**` must come BEFORE `## Root cause`")
    # 2) The tracker id in BOTH artifacts — only for a real numeric ticket; a slug /
    #    --no-issue (pending-id) bundle legitimately carries no trailer.
    if issue_id.isdigit() and not no_issue:
        needle = re.compile(r"#" + re.escape(issue_id) + r"\b")
        commit_text = commit_path.read_text(encoding="utf-8") if commit_path.is_file() else ""
        if not needle.search(pr_text):
            problems.append(f"{publish.PR_BODY} does not reference the tracker id #{issue_id}")
        if not needle.search(commit_text):
            problems.append(f"{publish.COMMIT_MSG} does not reference the tracker id #{issue_id}")
    return problems


def _revalidate(cfg: Config, args: argparse.Namespace) -> int:
    """Re-gate a COMPLETE bundle against the current engine; write a dated stamp.

    Reuses the single-sourced gate runner (``gates.run_gates_dry`` — no write to the
    frozen ``check-gates.json``) and records ``revalidation-<date>.json``. Refuses a
    non-COMPLETE bundle; never re-decides §9. Exits nonzero iff a row changed, so a
    delta is visible to the caller; an unchanged result is a quiet confirmation.
    """
    d = cfg.bundle(args.issue_id)
    if not d.exists():
        print(f"no such bundle: {d}", file=sys.stderr)
        return 1
    if state.state(d) != state.COMPLETE:
        print(f"revalidate refuses {d.name}: not COMPLETE (state {state.state(d)}). "
              "Revalidation re-gates a frozen bundle; finish sign-off first.",
              file=sys.stderr)
        return 2
    date = args.date or datetime.date.today().isoformat()
    result = revalidate.revalidate(cfg, d, date)
    print(revalidate.render_md(result))
    return 1 if result["changed"] else 0


def _act(cfg: Config, args: argparse.Namespace) -> int:
    """Dispatch the `act` command group (#89): `act index` / `act log`."""
    if args.act_cmd == "index":
        return _act_index(cfg, args)
    if args.act_cmd == "log":
        return _act_log(cfg, args)
    if args.act_cmd == "resolve":
        return _act_resolve(cfg, args)
    return 2


def _act_scope(cfg: Config, args: argparse.Namespace) -> tuple[list, list, bool]:
    """``(scoped_entries, all_entries, full)`` for an act command (#299).

    Default scope = the unreviewed frozen set (resume from the frontier); ``--all``
    or ``--since`` widens to every frozen cycle (``--since`` then date-filters).
    Patterns / ledger registration / recurrence detection always run over
    ``all_entries`` — a signal seen once before the frontier and once after must
    still count as recurring, so narrowing the narrative scope must never narrow
    the signal history.

    Both scopes derive from ONE frozen snapshot (#299 review round 3): a bundle
    freezing between two globs would enter the scoped set (and be marked reviewed on
    --append) while missing from the signal history — its recurring signals never
    registered yet its cycles pushed past the frontier.

    ``all_entries`` is NEVER date-filtered (#299 review round 7): ``--since``
    narrows only the narrative scope — a signal seen once before the requested date
    and once after must still register as recurring under ``--since --append``.
    """
    frozen = act.frozen_bundles(cfg)
    all_entries = act.index(cfg, bundles=frozen)          # full signal history, always
    if args.all or args.since:
        scoped = ([e for e in all_entries if e.date and e.date >= args.since]
                  if args.since else all_entries)
        return scoped, all_entries, True
    if not act.has_frontier(cfg):
        # A legacy count marker records no names — cover the full history once,
        # loudly; the first --append then records a real frontier (#299 review r3).
        if frozen:
            print("act: legacy count marker (no frontier recorded) — covering the "
                  "full frozen history; `--append` records the frontier", file=sys.stderr)
        return all_entries, all_entries, False
    unreviewed = set(act.unreviewed_bundles(cfg, frozen=frozen))
    return [e for e in all_entries if e.bundle in unreviewed], all_entries, False


def _act_index(cfg: Config, args: argparse.Namespace) -> int:
    """Print the read-only Act bundle index (default: unreviewed cycles only, #299)."""
    entries, all_entries, full = _act_scope(cfg, args)
    if not full:
        print(f"act index: {len(entries)} unreviewed of {len(all_entries)} frozen "
              "cycle(s) (--all for the full index)", file=sys.stderr)
    print(act.render_index(entries, act.patterns(all_entries),
                           act.load_ledger(cfg), act.recurrences(cfg, all_entries)))
    return 0


def _act_log(cfg: Config, args: argparse.Namespace) -> int:
    """Scaffold a dated act-log entry; print it, or append with --append.

    The scaffold pre-fills the considered bundles and recurring signals; the
    Process-deltas section is left TODO because choosing them is Act's
    irreducible human work. Default scope resumes from the review frontier (#299);
    --append advances the frontier past the covered cycles.
    """
    started = time.time()  # before the scaffold's index is built (#299 review round 6)
    entries, all_entries, full = _act_scope(cfg, args)
    if not all_entries:
        print("no frozen cycles to review (need COMPLETE bundles)", file=sys.stderr)
        return 1
    if not entries:
        print(f"no unreviewed frozen cycles ({len(all_entries)} frozen, all covered by "
              "the last Act) — use --all or --since to re-review", file=sys.stderr)
        return 1
    text = act.scaffold_entry(entries, act.patterns(all_entries), date=args.date,
                              recs=act.recurrences(cfg, all_entries))
    if args.append:
        # Recording is the ONLY writing path (#298 review): the ledger registration
        # (#149) rides --append with the entry, so a plain `act log` stays the safe,
        # read-only preview the help promises — over the FULL signal history (#299).
        # Log first, frontier second: a crash between the two re-reviews the cycles
        # next time — never silently skips them. The marker write itself is atomic.
        # The whole write rides the SHARED Act session lock (#299 review round 12):
        # a manual append overlapping a flow's auto-Act would otherwise log-and-mark
        # the very snapshot the automatic leaf is still reviewing, and the leaf
        # would then append a duplicate entry the frontier union cannot undo.
        with act.act_session(cfg) as held:
            if not held:
                print("act: another Act session is running (a flow's auto-Act or a "
                      "concurrent append) — retry when it finishes", file=sys.stderr)
                return 1
            return _act_log_append(cfg, args, entries, all_entries, text, full,
                                   started)
    print(text)
    return 0


def _act_log_append(cfg: Config, args: argparse.Namespace, entries, all_entries,
                    text: str, full: bool, started: float) -> int:
    """The writing half of ``act log --append`` — runs INSIDE the Act session lock."""
    act.register_signals(cfg, all_entries, args.date)  # track recurring signals (#149)
    if full:
        # Explicit --all/--since re-review: duplicating coverage is the point.
        # delta_guard still applies the in-session delta protection (#299 r6/7),
        # and the entries' EXTRACTION-time fingerprints ride along (#299 review
        # round 18) — a bundle recreated between the scaffold and this write must
        # not have its new generation attested by a post-append hash.
        log = act.append_entry(cfg, text)
        withheld = act.mark_reviewed(
            cfg, reviewed=[e.bundle for e in entries], date=args.date,
            delta_guard=started,
            fingerprints={e.bundle.name: e.fingerprint
                          for e in entries if e.fingerprint})
    else:
        # Default frontier scope: re-check + append + advance under ONE marker
        # critical section (#299 review round 10) — two overlapping appends must
        # not both log the same cycles; the loser re-scopes to what is STILL
        # unreviewed (re-rendering the entry) or records nothing at all.
        log, kept, withheld = act.append_reviewed(
            cfg, entries,
            lambda kept: act.scaffold_entry(kept, act.patterns(all_entries),
                                            date=args.date,
                                            recs=act.recurrences(cfg, all_entries)),
            date=args.date, delta_guard=started)
        if log is None:
            print("act: a concurrent Act covered these cycles while this entry "
                  "was prepared — nothing left to record (rerun to preview the "
                  "new scope)", file=sys.stderr)
            return 1
        if len(kept) < len(entries):
            print(f"act: {len(entries) - len(kept)} cycle(s) were covered by a "
                  f"concurrent Act — logged the remaining {len(kept)}",
                  file=sys.stderr)
    if withheld:
        print(f"act: {len(withheld)} cycle(s) got a revalidation delta while "
              "this entry was written — left unreviewed for the next Act",
              file=sys.stderr)
    print(f"appended entry to {log}")
    return 0


def _act_resolve(cfg: Config, args: argparse.Namespace) -> int:
    """Mark a tracked recurring signal as a process-delta the human applied (#149)."""
    date = args.date or datetime.date.today().isoformat()
    raw = act.resolve(cfg, args.signal, args.location, date)
    if raw is None:
        # The registration path is --append (a plain `act log` is a read-only preview,
        # #298 review) — the recovery hint must name the WRITING invocation, or the
        # operator follows it and the next resolve fails identically.
        print(f"act resolve: no open ledger signal matching '{args.signal}' — run "
              f"`pdca act log --date <ISO> --append` to register recurring signals first",
              file=sys.stderr)
        return 1
    print(f"marked applied ({date}): {raw}")
    return 0


def _signoff(cfg: Config, args: argparse.Namespace) -> int:
    d = cfg.bundle(args.issue_id)
    summary = d / "SUMMARY.md"
    if not summary.exists():
        print(f"no SUMMARY.md — run the issue first: {d}", file=sys.stderr)
        return 1

    if args.accept:
        action = "accept"
        open_items = signoff.open_needs_human(summary)
        if open_items:
            print("cannot accept — §6 NEEDS-HUMAN still open (C6):", file=sys.stderr)
            for it in open_items:
                print(f"  {it}", file=sys.stderr)
            return 1
    elif args.iterate_do:
        action = "iterate-do"
    elif args.iterate_plan:
        action = "iterate-plan"
    else:  # --discontinue: deliberate abandon, no C6 guard
        action = "discontinue"

    date = datetime.date.today().isoformat()
    try:
        signoff.record(summary, action=action, by=args.by or "unknown", date=date,
                       delta=args.delta)
    except ValueError as exc:
        # A SUMMARY with no §9 is refused rather than written to (#327). Same answer as the
        # absent-SUMMARY check above — report and exit non-zero — because this is a direct
        # CLI boundary with nothing to contain a traceback.
        print(f"cannot sign off — {exc}", file=sys.stderr)
        return 1

    # Apply the transition: accept freezes; iterate clears and re-runs the body.
    final = driver.run_issue(d, cfg)
    print(f"{final}\t{d}")
    if driver.held(final):
        # Same contract as `pdca run` (#351 review): an iterate that archives the attempt,
        # returns to PLANNED and is then held before Do has NOT rebuilt anything, and
        # exiting 0 would tell automation the sign-off decision was carried out.
        print(f"signoff: {d.name} is held at {final} — the transition was recorded but the "
              "rebuild did not run; resolve the item(s) above and re-run",
              file=sys.stderr)
        return 1

    # Accept → publish by default, like `flow`'s closing step (#97): a standalone
    # `signoff --accept` otherwise left bundles COMPLETE-but-unpublished with no signal.
    # `--no-publish` opts out (then the bundle is deliberately, not silently, unpublished).
    if action == "accept" and final == state.COMPLETE and not getattr(args, "no_publish", False):
        rc = publish.publish(cfg, args.issue_id, dry_run=cfg.publisher.mode == "stub",
                             by=args.by, skip_if_no_target=True)
        if rc != 0:
            print(f"  publish did not complete (rc {rc}) — {d.name} is COMPLETE but NOT "
                  f"published; fix and re-run `pdca publish {args.issue_id}`.", file=sys.stderr)
            return rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
