"""Vendor-neutral STOP-discipline guard for the Do beat.

The claude builder gets the STOP discipline (no ``gh pr ready`` / ``merge`` /
``review --approve`` — that is the human's Check sign-off) mechanically from a
PreToolUse hook (``.claude/hooks/builder_guard.py``). A builder of any OTHER
family has no hook machinery, so it used to run with nothing but cwd confinement
between it and marking its own PR ready.

This module closes that gap without a second rule set: it writes a tiny ``gh``
shim into a private directory, prepends that directory to the leaf's ``PATH``,
and the shim asks THE SAME ``builder_guard.py`` (its ``--command`` CLI mode) for
a verdict before ``exec``-ing the real ``gh``. Blocked calls exit 2 with the
guard's explanation on stderr; everything else runs unchanged. The blocklist
therefore stays single-sourced in the hook file.

Best-effort by design: no real ``gh`` on PATH or no hook file ⇒ the env is
returned unchanged (there is nothing to guard / nothing to guard with). The
threat model is a confused model, not an adversary — same as the claude hook.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

from .config import Config

_SHIM = """\
#!/bin/sh
# pdca guard shim — STOP discipline for the Do beat. Single-sourced verdict:
# .claude/hooks/builder_guard.py --command (same rules as the claude PreToolUse hook).
"{python}" "{guard}" --command "gh $*" || exit 2
exec "{gh}" "$@"
"""


def shim_env(cfg: Config, env: dict | None) -> dict:
    """``env`` with a guarded ``gh`` first on ``PATH`` (see module docstring).

    Returns a copy; the input mapping is never mutated. The shim directory is a
    per-invocation tempdir (mode 0700 via mkdtemp) left to the OS tmp reaper."""
    out = dict(env or {})
    guard_py = cfg.root / ".claude" / "hooks" / "builder_guard.py"
    real_gh = shutil.which("gh")
    if not guard_py.is_file() or not real_gh:
        return out
    shim_dir = Path(tempfile.mkdtemp(prefix="pdca-guard-"))
    shim = shim_dir / "gh"
    shim.write_text(
        _SHIM.format(python=sys.executable, guard=guard_py, gh=real_gh),
        encoding="utf-8",
    )
    shim.chmod(0o755)
    base_path = out.get("PATH", os.environ.get("PATH", ""))
    out["PATH"] = f"{shim_dir}{os.pathsep}{base_path}" if base_path else str(shim_dir)
    return out
