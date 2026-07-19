"""On-demand manual-test launch — ``pdca try <id>`` (hands-on Check).

Check's **validation act** ("is this the right thing?") and the visual / GUI /
manual-repro §6 NEEDS-HUMAN rows are irreducibly a human call — for a GUI app, clearing
them means the human actually *running the patched build* and driving it. The deterministic
gates can't, and the reviewer leaf is headless + sandboxed + read-only-grounded (it can't
hand a human an interactive session).

``launch`` MATERIALIZES the bundle's patched tree on demand from its ``patch.diff``
(:func:`worktree.stage`) — reconstructed off the target base — so a human reviewing a *batch*
can ``pdca try <id>`` any parked bundle in turn, not only the last one Do left in the shared,
reset-reused per-cycle worktree (batch-then-review is the default cadence). It then runs the
instance-configured ``[manual_test].cmd`` from that tree, inheriting the terminal (no capture,
no timeout — the human quits the app to return), with the same ``PDCA_*`` env the gate /
reviewer commands get. It leaves BUNDLE state untouched (advisory): edits made while testing
are reset the next time a tree is staged; the human records the outcome in a Manual-verification
note and signs off in §9.
"""

from __future__ import annotations

import os
import subprocess
import sys

from . import lane, state, worktree
from .config import Config


def launch(cfg: Config, issue_id: str) -> int:
    """Launch the patched build for bundle ``issue_id`` for hands-on manual testing.

    Returns the launch command's own exit code on success, or a nonzero operator-error
    code: ``1`` (no such bundle / no built patch / no patched worktree on disk), ``2``
    (``[manual_test].cmd`` unset). Never mutates bundle or worktree state.
    """
    d = cfg.bundle(issue_id)
    if not d.exists():
        print(f"no such bundle: {d}", file=sys.stderr)
        return 1

    # Gate on a real built patch rather than a state allowlist: this admits BUILT / CHECKED /
    # AWAITING_SIGNOFF / accepted-COMPLETE (all carry patch.diff) and excludes UNPLANNED /
    # PLANNED and a close/no-fix COMPLETE (which reach COMPLETE with no patch), with no state
    # enumeration to keep in sync.
    patch = d / "patch.diff"
    if not patch.is_file() or not patch.read_text(encoding="utf-8").strip():
        print(f"{state.state(d)}\t{d.name}: no built patch to try — run the cycle through "
              f"Do first (`{_prog()} flow {issue_id}`).", file=sys.stderr)
        return 1

    if not cfg.manual_test_cmd.strip():
        print("manual test not configured — set [manual_test].cmd in pdca.toml "
              '(e.g. cmd = "python -m gramps") so `pdca try` can launch the patched build.',
              file=sys.stderr)
        return 2

    # Materialize this bundle's patched tree from its patch.diff ON DEMAND (worktree.stage),
    # so a human reviewing a batch can `pdca try` ANY parked bundle in turn — not only the
    # last one Do left in the shared, reset-reused per-cycle worktree. patch.diff is Do's
    # canonical output, so the reconstruction is deterministic; it mirrors the gate's resync.
    if not cfg.worktree:
        print("[driver].worktree is off — enable worktree isolation ([driver].worktree) so "
              "`pdca try` can materialize the patched tree from patch.diff.", file=sys.stderr)
        return 1
    # The whole session — stage AND the interactive command — holds the lane lifecycle
    # lock (#297 review round 8): the footprint sweeper (and any Do/gate run) tries the
    # same lock, so it can no longer clean/reset/remove the patched tree BENEATH the
    # application the human is validating for sign-off. Non-blocking: a busy lane means
    # a Do or gate run owns it right now — refuse with a reason rather than corrupt it.
    try:
        with worktree.lane_lock(d, cfg, wait=False):
            wt = worktree.stage(d, cfg)
            if wt is None:
                print(f"could not materialize {d.name}'s patched tree — its patch.diff "
                      "may not apply onto the target base, or the target isn't a git "
                      "checkout (see the worktree messages above).", file=sys.stderr)
                return 1

            env = {**os.environ,
                   "PDCA_WORKTREE": str(wt), "PDCA_BUNDLE": str(d), "PDCA_TARGET": str(wt)}
            slot = lane.current()
            if slot is not None:
                env["PDCA_LANE"] = str(slot)

            mv = cfg.templates_dir / "MANUAL-VERIFICATION.md.tpl"
            print(f"launching the patched build for {d.name} from {wt}", file=sys.stderr)
            print("  edits made here are RESET on the next Do — don't rely on them.",
                  file=sys.stderr)
            print(f"  record what you tried in a Manual-verification note (template: {mv}).",
                  file=sys.stderr)
            print("  quit the app to return to the shell.", file=sys.stderr)

            return subprocess.run(cfg.manual_test_cmd, shell=True, cwd=str(wt),
                                  env=env).returncode
    except worktree.WorktreeError as exc:
        print(f"pdca try: {exc}", file=sys.stderr)
        return 1


def _prog() -> str:
    """The invoked command name for hints (the per-instance ``cli_name``, else ``pdca``)."""
    from .cli import _prog_name  # lazy: cli imports this module — avoid an import cycle
    return _prog_name()
