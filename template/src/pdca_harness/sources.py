"""Composable Plan-seeding sources (issue #102).

The Plan leaf briefs an UNPLANNED bundle from a *source*. A single ``notes_cmd`` (#65)
seeds one ``notes.json`` from one command, but a good brief often draws on more than the
ticket — a linked design doc / accepted proposal, a spec section, a CSV row. This module
runs the **list of providers** a project declares as ``[[plan.source]]`` in pdca.toml,
each contributing context into the bundle's ``sources/`` dir, so the planner briefs from
the *full* picture instead of one hand-rolled scrape.

Built-in providers:

* ``github`` — ``gh issue view {id}`` JSON (title/body/comments) → ``sources/github-<id>.json``;
* ``gitlab`` — ``glab issue view {id}`` → ``sources/gitlab-<id>.txt``;
* ``csv``    — the issue's row (or the whole export) from a CSV → ``sources/<name>.csv``;
* ``file``   — a path/glob (``{id}`` interpolated) — a linked ADR/proposal/spec — copied in;
* ``command``— the escape hatch: a ``.format(id=)`` shell command (exactly today's
  ``notes_cmd``), run with ``$PDCA_BUNDLE`` / ``$PDCA_SOURCES`` set; it writes its own output.

Every provider is **best-effort**: a missing tool, an absent file, or a failing command is
non-fatal — that source is skipped with a note and Plan falls back to the others / the
human.

**The tracker thread is sourced once (#132).** The legacy ``[tracker].notes_cmd`` still
runs for back-compat — *unless* a ``[[plan.source]]`` declares itself the tracker thread
with ``role = "tracker"``. A tracker-role ``github``/``gitlab``/``command`` source writes
the canonical ``notes.json`` (at the bundle root, where the planner and the id-seeded
batch flow read it) and ``seed()`` then **skips** ``notes_cmd``, so a GitHub-Issues
project configures the tracker fetch once instead of fetching/storing the same issue
twice. ``notes_cmd`` and a tracker-role plan.source are therefore mutually exclusive; a
project that sets neither sees no change.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import Config


def seed(cfg: Config, d: Path) -> None:
    """Seed bundle ``d`` from every configured Plan source, plus the legacy notes_cmd.

    Idempotent-ish and best-effort: providers that fail are skipped. New providers write
    into ``d/sources/``; the legacy ``notes_cmd`` writes ``d/notes.json``. The planner
    reads both.

    The tracker issue is sourced **once** (#132): if a plan.source declares ``role =
    "tracker"`` it supplies the canonical ``notes.json`` itself, and the legacy
    ``notes_cmd`` is skipped so the same issue isn't fetched and stored twice.
    """
    from . import leaves  # lazy: leaves imports sources via do_plan; avoid an import cycle

    plan_sources = cfg.plan_sources or []
    # Skip the legacy notes_cmd when a plan.source is the declared tracker thread —
    # otherwise the issue is fetched twice (notes_cmd AND the provider) and both copies
    # land in the bundle (#132). notes_cmd stays as back-compat for projects with none.
    if not any(_is_tracker_source(s) for s in plan_sources):
        leaves.ensure_notes(cfg, d)  # legacy [tracker].notes_cmd → notes.json (#65)
    if not plan_sources:
        return
    sources_dir = d / "sources"
    issue_id = d.name.removeprefix("issue_")
    for i, spec in enumerate(plan_sources):
        kind = (spec.get("type") or "").strip().lower()
        provider = _PROVIDERS.get(kind)
        if provider is None:
            print(f"sources: {d.name} — unknown plan.source type {kind!r}; skipping",
                  file=sys.stderr)
            continue
        try:
            sources_dir.mkdir(parents=True, exist_ok=True)
            provider(cfg, d, sources_dir, issue_id, spec, i)
        except Exception as exc:  # noqa: BLE001 — a failed source must never break Plan
            print(f"sources: {d.name} — {kind} source failed ({type(exc).__name__}: {exc}); "
                  "skipping (Plan falls back to the other sources / the human)",
                  file=sys.stderr)


# ----------------------------------------------------------------------------
# Providers. Each: (cfg, bundle, sources_dir, issue_id, spec, index) -> None, writing its
# context into sources_dir. Raising is caught by seed() (best-effort).
# ----------------------------------------------------------------------------
def _run_capture(cmd: list[str], cwd: Path) -> tuple[int, str]:
    r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    return r.returncode, (r.stdout or "")


def _is_tracker_source(spec: dict) -> bool:
    """True iff this plan.source declares itself the tracker thread (``role =
    "tracker"``) — a github/gitlab/command source that supplies the canonical
    ``notes.json``, making the legacy ``notes_cmd`` redundant (#132)."""
    return (spec.get("role") or "").strip().lower() == "tracker"


def _tracker_dest(d: Path, sources_dir: Path, spec: dict, default_name: str) -> Path:
    """Where a fetched issue is written: the canonical ``d/notes.json`` when this source
    is the declared tracker thread (so the planner / id-seeded flow find it), else its
    default file under ``sources/`` (supplementary context). #132."""
    if _is_tracker_source(spec):
        return d / "notes.json"
    return sources_dir / (spec.get("out") or default_name)


def _github(cfg: Config, d: Path, out: Path, issue_id: str, spec: dict, i: int) -> None:
    """`gh issue view <id>` as JSON. A ``repo`` in the spec scopes it; absent ⇒ gh's
    default. ``role = "tracker"`` writes the canonical ``notes.json`` (#132) instead of
    ``sources/github-<id>.json`` so this provider can be the single tracker source."""
    fields = spec.get("fields", "title,body,comments,url,state,labels")
    cmd = ["gh", "issue", "view", issue_id, "--json", fields]
    if spec.get("repo"):
        cmd += ["--repo", str(spec["repo"])]
    rc, text = _run_capture(cmd, cfg.root)
    if rc != 0 or not text.strip():
        print(f"sources: {d.name} — `gh issue view {issue_id}` produced nothing (rc {rc}); "
              "skipping github source", file=sys.stderr)
        return
    _tracker_dest(d, out, spec, f"github-{issue_id}.json").write_text(text, encoding="utf-8")


def _gitlab(cfg: Config, d: Path, out: Path, issue_id: str, spec: dict, i: int) -> None:
    cmd = ["glab", "issue", "view", issue_id]
    if spec.get("repo"):
        cmd += ["--repo", str(spec["repo"])]
    rc, text = _run_capture(cmd, cfg.root)
    if rc != 0 or not text.strip():
        print(f"sources: {d.name} — `glab issue view {issue_id}` produced nothing (rc {rc}); "
              "skipping gitlab source", file=sys.stderr)
        return
    _tracker_dest(d, out, spec, f"gitlab-{issue_id}.txt").write_text(text, encoding="utf-8")


def _csv(cfg: Config, d: Path, out: Path, issue_id: str, spec: dict, i: int) -> None:
    """Copy the issue's row from a CSV (matched on the configured key column), or, if no
    match column is given, the whole export, into sources/."""
    import csv as _csvmod

    path = (cfg.root / str(spec.get("path", ""))).resolve()
    if not path.is_file():
        print(f"sources: {d.name} — csv source {path} not found; skipping", file=sys.stderr)
        return
    key = spec.get("key", "")  # the id column header; "" ⇒ copy the whole file
    dst = out / (spec.get("out") or f"{path.stem}.csv")
    if not key:
        shutil.copyfile(path, dst)
        return
    with path.open(encoding="utf-8", newline="") as fh:
        reader = _csvmod.DictReader(fh)
        rows = [r for r in reader if (r.get(key) or "").strip() == issue_id]
        if not rows:
            print(f"sources: {d.name} — no row with {key}={issue_id} in {path}; skipping",
                  file=sys.stderr)
            return
        with dst.open("w", encoding="utf-8", newline="") as wfh:
            writer = _csvmod.DictWriter(wfh, fieldnames=reader.fieldnames or [])
            writer.writeheader()
            writer.writerows(rows)


def _file(cfg: Config, d: Path, out: Path, issue_id: str, spec: dict, i: int) -> None:
    """Copy a linked artifact (ADR / proposal / spec) into sources/. ``path`` interpolates
    ``{id}`` and may be a glob (e.g. ``docs/adr/*{id}*.md``)."""
    pattern = str(spec.get("path", "")).format(id=issue_id)
    if not pattern:
        return
    matches = sorted(cfg.root.glob(pattern)) if not os.path.isabs(pattern) else \
        sorted(Path(pattern).parent.glob(Path(pattern).name))
    if not matches:
        print(f"sources: {d.name} — file source {pattern!r} matched nothing; skipping",
              file=sys.stderr)
        return
    for m in matches:
        if m.is_file():
            shutil.copyfile(m, out / m.name)


def _command(cfg: Config, d: Path, out: Path, issue_id: str, spec: dict, i: int) -> None:
    """The escape hatch — a ``.format(id=)`` shell command (exactly today's notes_cmd). It
    runs with ``$PDCA_BUNDLE`` (the bundle) and ``$PDCA_SOURCES`` (sources/) set and is
    responsible for writing its own output there. Captured stdout, if any, is also saved —
    to the canonical ``notes.json`` when ``role = "tracker"`` (#132, so a notes_cmd moved
    into a tracker plan.source still seeds notes.json), else to ``sources/<out>``."""
    cmd = str(spec.get("cmd", "")).format(id=issue_id)
    if not cmd:
        return
    env = {**os.environ, "PDCA_BUNDLE": str(d), "PDCA_SOURCES": str(out)}
    r = subprocess.run(cmd, shell=True, cwd=str(cfg.root), env=env,
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"sources: {d.name} — command source failed (rc {r.returncode}): "
              f"{(r.stderr or '').strip()}", file=sys.stderr)
        return
    if _is_tracker_source(spec):
        # A migrated notes_cmd writes $PDCA_BUNDLE/notes.json ITSELF (it may also print
        # progress/logs to stdout). Only fall back to stdout when the command did NOT
        # create notes.json — otherwise we'd clobber the real thread with log text.
        notes = d / "notes.json"
        if not notes.exists() and (r.stdout or "").strip():
            notes.write_text(r.stdout, encoding="utf-8")
    elif spec.get("out") and (r.stdout or "").strip():
        (out / str(spec["out"])).write_text(r.stdout, encoding="utf-8")


_PROVIDERS = {
    "github": _github,
    "gitlab": _gitlab,
    "csv": _csv,
    "file": _file,
    "command": _command,
}
