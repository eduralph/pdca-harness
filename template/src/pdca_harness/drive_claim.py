"""One live driver per bundle (issue #565).

A bundle's state is nothing but the files in ``results/issue_<id>/`` — "nothing is hidden
in a database" — so two ``pdca flow`` runs over the same bundle have nothing to serialize
on: they write the same artifacts with no ordering between them. Act's session lock
(:func:`act.act_session`) states the rule for Act's one shared resource; this module states
it for bundles, on every CLI drive path (named ids, the CSV batch's in-flight sweep, split
adoption):

* **A run claims a bundle before it drives it, and keeps the claim for the rest of its
  life.** A claim is an exclusive, NON-blocking advisory lock (the cross-platform
  :func:`act._lock_exclusive` Act's session uses) on a per-bundle file, held on an open
  handle. The OS drops it when that handle closes — when the run returns, when it raises,
  and when its process dies (SIGKILL, a host crash) — so a claim never outlives its run and
  there is no stale-lock cleanup to get wrong. The one early release is for a bundle the
  run claimed and then decided NOT to drive (a named id it skips, a swept bundle the
  scheduler holds, an adopted child the reschedule drops): the run never touches it again,
  and the resume command it prints for that bundle must not be refused by the run that
  printed it.
* **A claim that cannot be recorded refuses, exactly as a claim another run holds does.**
  A bundle driven without a claim has nothing keeping a second run off it, so a claim file
  that cannot be opened or locked (an unwritable process dir, a filesystem without locks)
  fails CLOSED: a named id refuses the run and an implicitly reached one is skipped, each
  with a line saying why — the way Act's session reports and skips when its lock cannot be
  opened (``act.py:143-151``).
* **The RUN owns its claims, not its process.** The lock belongs to the open handle, so two
  runs in one process hold separate handles and the second is refused exactly as a second
  process is; a bundle a run already holds it never takes again, so a run never refuses
  itself, and a bundle it adopts becomes its own under the same rule. That is a property of
  the LOCK, and it is the only state here: nothing in this module is kept in the
  environment or in a module global, so there is no per-process state for a second
  in-process run to share. (Where the filesystem emulates ``flock`` with per-process POSIX
  locks — NFS — the lock itself narrows to "one run per process" and two runs in one
  process are no longer kept apart; two processes still are.)
* **The files live outside every bundle** — ``<process_dir>/.drive-claims/``, gitignored in
  the render — so :func:`state.state` never sees one and no results commit can carry one.
  They are keyed by the bundle's RESOLVED path, so a ``--rehearse`` bundle root never
  collides with the real one, and a symlinked alias of a bundle is the same claim as the
  bundle it aliases. A claim file is never deleted: unlinking a lock file another process
  may already have open is how two holders of "the same" lock happen.

What the claim does NOT cover is writing to a bundle that nobody is driving: a CSV batch's
Plan session runs before that batch claims anything, so the planner — or a ``pdca split
--accept`` it runs — can still rewrite or split a bundle another live run holds, and the
single-step verbs (``pdca run`` / ``signoff`` / ``publish``) take no claim at all. Stated
where an operator meets it, in ``docs/07-crosscutting.md`` (§Lanes).
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from . import act
from .config import Config

#: Where the claim files live, under ``cfg.process_dir``. Gitignored in the render
#: (``.gitignore.jinja``) — keep the two in step.
CLAIMS_DIR = ".drive-claims"

#: A non-blocking lock attempt that failed because ANOTHER handle holds the lock — as
#: opposed to a filesystem that cannot lock at all. flock reports EWOULDBLOCK (== EAGAIN);
#: Windows' LK_NBLCK reports EACCES or EDEADLOCK (== EDEADLK).
_CONTENDED = ({errno.EACCES, errno.EDEADLK} if os.name == "nt"
              else {errno.EAGAIN, errno.EWOULDBLOCK})


def _digest(p: Path) -> str:
    """12 hex digits naming ``p``'s RESOLVED path (lexical, if it will not resolve)."""
    try:
        real = p.resolve()
    except (OSError, RuntimeError):   # a symlink loop — name it by its lexical path
        real = p.absolute()
    return hashlib.sha256(str(real).encode("utf-8", "surrogateescape")).hexdigest()[:12]


def _claims_dir(cfg: Config) -> Path:
    return cfg.process_dir / CLAIMS_DIR


def _claim_file(cfg: Config, d: Path) -> Path:
    """Bundle ``d``'s claim file: its name, for a human reading the directory, plus the
    digest of its resolved path, which is what makes the claim unique."""
    try:
        name = d.resolve().name
    except (OSError, RuntimeError):
        name = d.name
    return _claims_dir(cfg) / f"{name[:100]}-{_digest(d)}.lock"


def _contended(exc: OSError) -> bool:
    return isinstance(exc, BlockingIOError) or exc.errno in _CONTENDED


def _stamp_of(path: Path) -> list[str]:
    """The words the holder stamped into its claim file — its pid — or ``[]``. Only ever a
    hint on top of the lock (the LOCK is the claim): best-effort, and it never raises."""
    try:
        return path.read_text(encoding="utf-8").split()
    except (OSError, ValueError):
        return []


def _stamp(fh) -> None:
    """Rewrite the claim file this run holds open with its pid, for the refusal line the
    loser prints. Best-effort: a claim whose stamp cannot be written is still a claim."""
    with contextlib.suppress(OSError):
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()


def _release(fh) -> None:
    try:
        with contextlib.suppress(OSError):
            act._unlock(fh)
    finally:
        with contextlib.suppress(OSError):
            fh.close()


@dataclass(frozen=True)
class Refusal:
    """Why this run may not drive a bundle. ``held``: another live run holds it. Otherwise
    this run could not record a claim on it, which is refused just the same (fail closed)."""

    held: bool
    reason: str   # completes "issue_7 is …" and "issue_7 — NOT driven: …"
    remedy: str   # what the operator can do about it (a lower-case imperative)


class Run:
    """The bundles one live ``flow`` run holds. Built by :func:`run`, which releases them."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._held: dict[Path, object] = {}   # claim file -> the open, locked handle

    def take(self, d: Path) -> Refusal | None:
        """Claim bundle ``d`` for this run: ``None`` once this run holds it — or already
        did, which is what keeps a run from ever refusing itself. Otherwise why it may not
        drive ``d``. Non-blocking, and it never raises."""
        path = _claim_file(self.cfg, d)
        if path in self._held:
            return None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # "a+", never "w": opening must not rewrite a file another run holds.
            fh = path.open("a+", encoding="utf-8")
        except OSError as exc:
            return self._unrecorded(path, exc)
        try:
            act._lock_exclusive(fh, wait=False)
        except OSError as exc:
            with contextlib.suppress(OSError):
                fh.close()
            if not _contended(exc):
                return self._unrecorded(path, exc)
            pid = _stamp_of(path)[:1]
            return Refusal(True, "held by another live `flow` run"
                           + (f" (pid {pid[0]})" if pid and pid[0].isdigit() else ""),
                           "let that run finish (or stop it)")
        _stamp(fh)
        self._held[path] = fh
        return None

    @staticmethod
    def _unrecorded(path: Path, exc: OSError) -> Refusal:
        return Refusal(False,
                       f"unclaimable — this run could not record its claim on it ({exc}), "
                       "and a bundle driven without a claim has nothing keeping a second "
                       "`flow` run off it",
                       f"make {path.parent} a writable directory on a filesystem that "
                       "supports file locks")

    def release(self, d: Path) -> None:
        """Give up the claim on ``d`` — a bundle this run claimed and then decided not to
        drive, so the resume command it prints for it is not refused by this very run."""
        fh = self._held.pop(_claim_file(self.cfg, d), None)
        if fh is not None:
            _release(fh)

    def close(self) -> None:
        held, self._held = self._held, {}
        for fh in held.values():
            _release(fh)


@contextlib.contextmanager
def run(cfg: Config) -> Iterator[Run]:
    """The claim scope of one live ``flow`` run: every claim taken through the yielded
    :class:`Run` is released when this exits — normally or by raising — and by the OS if
    the process dies first. Enter it in the process that DRIVES: after any re-exec, never
    before one (``cli.main`` may re-exec ``flow`` under a keep-awake inhibitor,
    ``cli.py:131-148``, and a claim taken before that exec is lost with the process image).
    """
    claims = Run(cfg)
    try:
        yield claims
    finally:
        claims.close()
