"""``pdca`` command-line entry point.

Thin wrapper over the driver: create a bundle, advance it, inspect the sign-off
queue, and record the human sign-off. Run as ``pdca <cmd>`` (installed) or
``python -m pdca_harness.cli <cmd>`` (from a source checkout with PYTHONPATH=src).
"""

from __future__ import annotations

import argparse
import datetime
import shutil
import sys
from pathlib import Path

from . import driver, signoff, state
from .config import Config

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
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pdca", description="PDCA quality-cycle driver")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init-issue", help="create a bundle and seed brief.md")
    p_init.add_argument("issue_id")
    p_init.add_argument("--from-brief", type=Path, help="copy this file as brief.md")

    p_run = sub.add_parser("run", help="advance an issue to a parked state")
    p_run.add_argument("issue_id")

    p_status = sub.add_parser("status", help="list bundle states (cheap-first queue)")
    p_status.add_argument("issue_id", nargs="?")

    p_signoff = sub.add_parser("signoff", help="record the human Check sign-off (§9)")
    p_signoff.add_argument("issue_id")
    g = p_signoff.add_mutually_exclusive_group(required=True)
    g.add_argument("--accept", action="store_true", help="accept — merge wider")
    g.add_argument("--iterate-do", action="store_true", help="rebuild against same brief")
    g.add_argument("--iterate-plan", action="store_true", help="revise the brief")
    p_signoff.add_argument("--by", default="", help="who signed off")
    p_signoff.add_argument("--delta", default="", help="iteration delta note")

    args = parser.parse_args(argv)
    cfg = Config.load()

    if args.cmd == "init-issue":
        return _init_issue(cfg, args.issue_id, args.from_brief)
    if args.cmd == "run":
        return _run(cfg, args.issue_id)
    if args.cmd == "status":
        return _status(cfg, args.issue_id)
    if args.cmd == "signoff":
        return _signoff(cfg, args)
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
        print(f"{s:18}{d.name}{flag}")
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
    else:
        action = "iterate-plan"

    date = datetime.date.today().isoformat()
    signoff.record(summary, action=action, by=args.by or "unknown", date=date, delta=args.delta)

    # Apply the transition: accept freezes; iterate clears and re-runs the body.
    final = driver.run_issue(d, cfg)
    print(f"{final}\t{d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
