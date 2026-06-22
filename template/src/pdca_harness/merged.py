"""Is a prerequisite's contribution merged into its base? (issue #107)

``Depends on`` gates a dependent until its prerequisite reaches **COMPLETE** — but
COMPLETE means "a draft PR was opened", not merged. A dependent's Do runs in a worktree
off the target base (``origin/<base>``), which does **not** contain a prereq whose PR is
still open, so file-overlapping work is built without the predecessor's diff and its PR
conflicts at merge. The stricter ``Depends on (merged):`` field gates the dependent until
the prereq is genuinely merged; this module answers "is it merged yet?".

Merge state is read from the prerequisite bundle's recorded PR (``publish.json``) via
``gh pr view --json state``. It is **best-effort and fail-closed**: anything we cannot
confirm as merged (no PR yet, or a ``gh`` failure) returns ``False`` so the dependent
stays safely blocked rather than building off an unmerged base — the dependent is then
picked up by a later ``pdca flow`` run, after the prereq's PR is merged.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from . import state
from .config import Config


def is_merged(cfg: Config, dep_id: str) -> bool:
    """True iff prerequisite ``dep_id``'s contribution is merged into its base.

    A close/no-fix prereq (COMPLETE with no patch) ships nothing to merge ⇒ ``True``. A
    prereq not yet COMPLETE, or accepted-but-unpublished, ⇒ ``False`` (wait). Otherwise
    the recorded PR's ``state`` is queried; ``MERGED`` ⇒ ``True``, and any ``gh`` failure
    is treated as not-merged.
    """
    d = cfg.bundle(dep_id)
    if state.state(d) != state.COMPLETE:
        return False  # prereq hasn't even finished its own cycle
    patch = d / "patch.diff"
    if not patch.is_file() or not patch.read_text(encoding="utf-8").strip():
        return True  # close/no-fix disposition — no contribution to wait on
    rec = _publish_record(d)
    pr_url = rec.get("pr_url") if rec else None
    if not pr_url:
        return False  # accepted but no PR published yet
    r = subprocess.run(["gh", "pr", "view", str(pr_url), "--json", "state"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"merged: could not read PR state for {dep_id} ({pr_url}); "
              "treating as not merged", file=sys.stderr)
        return False
    try:
        return json.loads(r.stdout or "{}").get("state") == "MERGED"
    except ValueError:
        return False


def _publish_record(d: Path) -> dict | None:
    """The bundle's ``publish.json`` (the recorded PR), or ``None`` if absent/unreadable."""
    pj = d / "publish.json"
    if not pj.exists():
        return None
    try:
        return json.loads(pj.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
