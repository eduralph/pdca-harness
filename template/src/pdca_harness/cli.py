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
import shutil
import sys
from pathlib import Path

from . import act, brief, driver, flow, gates, publish, queue, revalidate, signoff, state
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
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=_prog_name(), description="PDCA quality-cycle driver")
    # No subcommand → status (the bundle dashboard), the most-reached-for view (#88).
    sub = parser.add_subparsers(dest="cmd")

    p_init = sub.add_parser("init-issue", help="create a bundle and seed brief.md")
    p_init.add_argument("issue_id")
    p_init.add_argument("--from-brief", type=Path, help="copy this file as brief.md")

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

    p_status = sub.add_parser("status", help="list bundle states (cheap-first queue)")
    p_status.add_argument("issue_id", nargs="?")

    sub.add_parser("queue", help="the cheap-first sign-off burn-down (AWAITING_SIGNOFF)")

    p_gates = sub.add_parser("gates", help="run the deterministic Check gates (driver + CI share this)")
    p_gates.add_argument("issue_id", nargs="?")
    p_gates.add_argument("--working-tree", action="store_true", help="repo-scoped gates only (the CI merge re-gate)")

    p_reval = sub.add_parser("revalidate",
                             help="re-run gates on a COMPLETE bundle vs the current engine; write a dated stamp (never re-decides §9)")
    p_reval.add_argument("issue_id")
    p_reval.add_argument("--date", help="ISO date for the stamp (default: today)")

    # Act tooling as one command group (#89): `act index` / `act log`.
    p_act = sub.add_parser("act", help="cross-cycle Act tooling (index / log)")
    act_sub = p_act.add_subparsers(dest="act_cmd", required=True)
    p_actidx = act_sub.add_parser("index", help="read-only index of frozen cycles + recurring signals")
    p_actidx.add_argument("--since", help="only cycles signed off on/after this ISO date")
    p_actlog = act_sub.add_parser("log", help="scaffold a dated act-log entry (deltas left to the human)")
    p_actlog.add_argument("--since", help="only consider cycles signed off on/after this ISO date")
    p_actlog.add_argument("--date", required=True, help="review date (ISO; Act is out-of-band so pass it)")
    p_actlog.add_argument("--append", action="store_true", help="append to process/act-log.md (default: print)")

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
                           help="no tracker id yet: relax T4 to a flag, record id_pending (vs a magic #0000)")
    p_publish.add_argument("--by", default="", help="who published (recorded in publish.json)")

    args = parser.parse_args(argv)
    # --rehearse (#87): a dry-run of the SAME control flow with stub leaves + stub gates
    # in an isolated bundle root — set before Config.load reads the env. setdefault so an
    # explicit env wins.
    if getattr(args, "rehearse", False):
        os.environ.setdefault("PDCA_LEAVES_MODE", "stub")
        os.environ.setdefault("PDCA_GATES_MODE", "stub")
        os.environ.setdefault("PDCA_BUNDLE_ROOT", ".rehearse")
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
    if args.cmd == "run":
        return _run(cfg, args.issue_id)
    if args.cmd == "flow":
        return _flow(cfg, args)
    if args.cmd == "status":
        return _status(cfg, args.issue_id)
    if args.cmd == "queue":
        return _queue(cfg)
    if args.cmd == "gates":
        return _gates(cfg, args)
    if args.cmd == "revalidate":
        return _revalidate(cfg, args)
    if args.cmd == "act":
        return _act(cfg, args)
    if args.cmd == "signoff":
        return _signoff(cfg, args)
    if args.cmd == "publish":
        return publish.publish(cfg, args.issue_id, dry_run=args.dry_run,
                               open_pr=not args.no_pr, by=args.by, pending_id=args.no_issue)
    return 2


def _init_issue(cfg: Config, issue_id: str, from_brief: Path | None) -> int:
    d = cfg.bundle(issue_id)
    if d.exists():
        print(f"bundle already exists: {d}", file=sys.stderr)
        return 1
    d.mkdir(parents=True)
    src = from_brief or (cfg.templates_dir / "brief.md.tpl")
    if not src.exists():
        print(f"no brief source: {src}", file=sys.stderr)
        return 1
    shutil.copyfile(src, d / "brief.md")
    print(f"{state.state(d)}\t{d}")
    return 0


def _run(cfg: Config, issue_id: str) -> int:
    d = cfg.bundle(issue_id)
    if not d.exists():
        print(f"no such bundle: {d}", file=sys.stderr)
        return 1
    final = driver.run_issue(d, cfg)
    print(f"{final}\t{d}")
    if final == state.AWAITING_SIGNOFF:
        open_items = signoff.open_needs_human(d / "SUMMARY.md")
        if open_items:
            print(f"  §6 NEEDS-HUMAN ({len(open_items)} open) — clear before accept:")
            for it in open_items:
                print(f"    {it}")
    return 0


def _flow(cfg: Config, args: argparse.Namespace) -> int:
    """Run the whole cycle for one or more issues (the single ``flow`` verb, #86).

    Arity selects the mode: **one id** is a single sequential cycle (Plan→Do→Check→
    sign-off→publish→Act); **several ids** fan out across lanes with a cheap-first
    sign-off queue; **zero ids + --from-csv** plans a batch the planner picks from the
    export. Unbriefed ids are auto-planned (one shared interactive Plan session) — no
    --plan flag. Act runs by default after COMPLETE (--no-act to skip).
    """
    if getattr(args, "lanes", None) is not None:
        cfg.lanes = max(1, args.lanes)
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
        return _report_batch(flow.flow_batch(
            cfg, csv=args.from_csv, do_publish=do_publish, do_act=do_act, by=args.by))

    if len(ids) == 1:  # single sequential cycle (auto-plans if unbriefed)
        iid = ids[0]
        d = cfg.bundle(iid)
        if d.exists() and state.state(d) == state.COMPLETE:
            print(f"{state.COMPLETE}\t{d}", file=sys.stderr)
            print(f"  already complete — nothing to run. To redo it: rm -rf {d}", file=sys.stderr)
            return 0
        if not d.exists():
            d.mkdir(parents=True)
        final = flow.flow(cfg, iid, csv=args.from_csv,
                          do_publish=do_publish, do_act=do_act, by=args.by)
        print(f"{final}\t{d}")
        if final == state.AWAITING_SIGNOFF:
            for it in signoff.open_needs_human(d / "SUMMARY.md"):
                print(f"    {it}")
        return 0 if final in (state.COMPLETE, state.AWAITING_SIGNOFF) else 1

    # Several ids: batch — auto-plan unbriefed, drive concurrently, cheap-first sign-off.
    return _report_batch(flow.flow_ids(
        cfg, ids, plan_missing=True, csv=args.from_csv,
        do_publish=do_publish, do_act=do_act, by=args.by))


def _report_batch(results: dict[str, str]) -> int:
    """Print a batch result map and return a process code (0 iff all COMPLETE)."""
    if not results:
        print("flow: nothing to drive — no in-flight briefs among the ids.", file=sys.stderr)
        return 0
    for iid, st in sorted(results.items()):
        print(f"{st}\t{iid}")
    done = sum(1 for s in results.values() if s == state.COMPLETE)
    print(f"flow: {done}/{len(results)} complete")
    return 0 if done == len(results) else 1


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
        if s == state.AWAITING_SIGNOFF:
            n = len(signoff.open_needs_human(d / "SUMMARY.md"))
            flag = "  [cheap: confirm]" if n == 0 else f"  [{n} NEEDS-HUMAN]"
        if s == state.COMPLETE:  # publish visibility (#97): is the accepted fix actually out?
            flag += _publish_flag(d)
        blocked = _blocked_by(cfg, d) if s != state.COMPLETE else []
        if blocked:
            flag += f"  [blocked-by: {', '.join(blocked)}]"
        print(f"{s:18}{d.name}{flag}")
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
        url = json.loads(pj.read_text(encoding="utf-8")).get("pr_url")
    except (ValueError, OSError):
        return "  [published]"
    return f"  [PR {url}]" if url else "  [published]"


def _blocked_by(cfg: Config, d: Path) -> list[str]:
    """Declared `Depends on` ids of bundle ``d`` that are not yet COMPLETE (issue #36)."""
    bp = d / "brief.md"
    if not bp.exists():
        return []
    return [dep for dep in brief.depends_on(bp)
            if state.state(cfg.bundle(dep)) != state.COMPLETE]


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
    return 2


def _act_index(cfg: Config, args: argparse.Namespace) -> int:
    """Print the read-only Act bundle index across frozen cycles."""
    entries = act.index(cfg, since=args.since)
    print(act.render_index(entries, act.patterns(entries)))
    return 0


def _act_log(cfg: Config, args: argparse.Namespace) -> int:
    """Scaffold a dated act-log entry; print it, or append with --append.

    The scaffold pre-fills the considered bundles and recurring signals; the
    Process-deltas section is left TODO because choosing them is Act's
    irreducible human work.
    """
    entries = act.index(cfg, since=args.since)
    if not entries:
        print("no frozen cycles to review (need COMPLETE bundles)", file=sys.stderr)
        return 1
    text = act.scaffold_entry(entries, act.patterns(entries), date=args.date)
    if args.append:
        log = act.append_entry(cfg, text)
        print(f"appended entry to {log}")
    else:
        print(text)
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
    signoff.record(summary, action=action, by=args.by or "unknown", date=date, delta=args.delta)

    # Apply the transition: accept freezes; iterate clears and re-runs the body.
    final = driver.run_issue(d, cfg)
    print(f"{final}\t{d}")

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
