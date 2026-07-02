"""On-demand manual-test launch — ``pdca try <id>`` (hands-on Check).

Check's **validation act** ("is this the right thing?") and the visual / GUI /
manual-repro §6 NEEDS-HUMAN rows are irreducibly a human call — for a GUI app, clearing
them means the human actually *running the patched build* and driving it. The deterministic
gates can't, and the reviewer leaf is headless + sandboxed + read-only-grounded (it can't
hand a human an interactive session). But the patch is already applied on disk: Do runs in
a per-cycle git **worktree** (:mod:`pdca_harness.worktree`) that carries the edits and
persists through Check/sign-off (it's only reset on the next Do).

``launch`` runs the instance-configured ``[manual_test].cmd`` from that worktree, inheriting
the terminal (no capture, no timeout — the human quits the app to return), with the same
``PDCA_*`` env the gate / reviewer commands get. It NEVER calls :func:`worktree.ensure`
(which resets the tree to base and would throw the patch away): the launch is read-only over
the harness's state. Advisory only — it decides nothing; the human records the outcome in a
Manual-verification note and signs off in §9.
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

    # The patch is physically applied only inside the per-cycle worktree. path() is READ-ONLY
    # (do NOT call ensure() — it hard-resets the tree to base, discarding the patch).
    wt = worktree.path(d, cfg)
    if wt is None:
        if not cfg.worktree:
            print("[driver].worktree is off — there is no patched worktree to launch. "
                  "Enable worktree isolation and re-run this bundle's Do.", file=sys.stderr)
        else:
            print(f"no patched worktree on disk for {d.name} — the patch isn't applied "
                  "anywhere to run. This happens when the target isn't a git checkout or a "
                  f"later Do reset the tree. Re-run this bundle's Do (`{_prog()} flow "
                  f"{issue_id}`).", file=sys.stderr)
        return 1

    # The per-lane worktree is reset-and-reused across bundles (issue #94): a LATER bundle's
    # Do hard-resets it and applies its own patch. So "the tree exists and this bundle has a
    # patch.diff" is not enough — the tree may now hold a different bundle's build. Confirm
    # this bundle still owns it (the marker `ensure` stamps), else launching would test the
    # wrong build under this bundle's name.
    occupant = worktree.owner_of(wt)
    if occupant != d.name:
        why = (f"it now holds {occupant}'s build (a later Do reused this lane's worktree)"
               if occupant else "its owner can't be confirmed (built by an older run)")
        print(f"the worktree at {wt} is not {d.name}'s build — {why}. Re-run this bundle's "
              f"Do to reload its patch before testing (`{_prog()} flow {issue_id}`).",
              file=sys.stderr)
        return 1

    env = {**os.environ,
           "PDCA_WORKTREE": str(wt), "PDCA_BUNDLE": str(d), "PDCA_TARGET": str(wt)}
    slot = lane.current()
    if slot is not None:
        env["PDCA_LANE"] = str(slot)

    mv = cfg.templates_dir / "MANUAL-VERIFICATION.md.tpl"
    print(f"launching the patched build for {d.name} from {wt}", file=sys.stderr)
    print("  edits made here are RESET on the next Do — don't rely on them.", file=sys.stderr)
    print(f"  record what you tried in a Manual-verification note (template: {mv}).",
          file=sys.stderr)
    print("  quit the app to return to the shell.", file=sys.stderr)

    return subprocess.run(cfg.manual_test_cmd, shell=True, cwd=str(wt), env=env).returncode


def _prog() -> str:
    """The invoked command name for hints (the per-instance ``cli_name``, else ``pdca``)."""
    from .cli import _prog_name  # lazy: cli imports this module — avoid an import cycle
    return _prog_name()
