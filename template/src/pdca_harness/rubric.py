"""The target repo's standing review rubric, fed to every model leaf that needs it (#314).

A host repo often carries its own review contract — an ``AGENTS.md`` "Review rubric &
protocol" section listing hard conventions, recurring defect classes, and the finding
classes reviewers should stop spending on. Nothing put it in front of the leaves, which
creates an asymmetry that costs a guaranteed review round: **the builder generates without
ever seeing the criteria the reviewer applies**, so convention violations ship and come
back as findings.

Three consumers, one text: the builder (with a self-review-before-emit instruction), the
Check reviewer, and the adversary. The issue asks for all three; feeding only two would
reproduce the asymmetry between the two reviewers instead of between builder and reviewer.

## Why it is snapshotted rather than re-read

"One artifact, both sides, no drift" is not achieved by three leaves each reading a file in
the target checkout: the builder reads it at **Do** and the reviewers at **Check**, and the
target is a live repo that can change in between — including *because of* work in this very
cycle. Each leaf would then be judged against a different contract.

So the first reader copies it into the bundle as ``rubric-snapshot.md`` and every later
reader uses that copy. It is a Do/Check-era artifact, so it is in
``DOWNSTREAM_OF_BRIEF``: an iterate archives it with its attempt and the rebuild takes a
fresh snapshot, which is right — a rubric that changed between attempts *should* apply to
the next one.

## Fail-open, deliberately

An unset key, a missing file, an unreadable file, or a path that escapes the target all
degrade to "no rubric" with a warning. A broken rubric path must never stop a build: the
rubric improves review quality, and trading a working pipeline for it would be a bad
exchange in the one direction that matters.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SNAPSHOT = "rubric-snapshot.md"


def _section(text: str, heading: str) -> str:
    """The named Markdown section — from its heading to the next same-or-higher one.

    Instances commonly keep the rubric as one section of a larger ``AGENTS.md``; feeding
    the whole file would bury the rubric in unrelated project context and inflate every
    prompt that carries it.
    """
    wanted = heading.strip().lower()
    out: list[str] = []
    level = 0
    for line in text.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*?)\s*$", line)
        if m:
            depth, title = len(m.group(1)), m.group(2).strip().lower()
            if level:
                if depth <= level:
                    break          # the next same-or-higher heading ends the section
            elif wanted in title:
                level = depth
                out.append(line)
                continue
        if level:
            out.append(line)
    return "\n".join(out).strip()


def _resolve(target: Path, rel: str) -> Path | None:
    """``target/rel``, or None if it escapes the target checkout.

    Rejects absolute paths, ``..`` traversal and symlink escapes. The value comes from
    ``pdca.toml`` rather than from a model, so this is defence against a mistake rather
    than an attack — but a rubric path silently reading outside the target is a mistake
    worth failing on rather than obeying.
    """
    if not rel or Path(rel).is_absolute():
        return None
    try:
        resolved = (target / rel).resolve()
        resolved.relative_to(target.resolve())
    except (OSError, ValueError):
        return None
    return resolved


def load(d: Path, cfg) -> str:
    """The rubric text for bundle ``d`` — snapshotting on first use. "" when unconfigured.

    Later callers get the snapshot even if the target has moved on, which is the whole
    point: the builder and both reviewers must be judged against the same contract.
    """
    snapshot = d / SNAPSHOT
    if snapshot.exists():
        try:
            return snapshot.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    rel = str(getattr(cfg, "rubric_file", "") or "").strip()
    if not rel:
        return ""

    from . import worktree
    resolved_target = worktree._target(d, cfg)
    if not resolved_target:
        print(f"rubric: cannot resolve the target checkout for {d.name} — "
              "continuing without the rubric", file=sys.stderr)
        return ""
    path = _resolve(resolved_target[0], rel)
    if path is None or not path.is_file():
        print(f"rubric: [project].rubric_file = {rel!r} does not resolve to a file inside "
              f"the target checkout — continuing without it", file=sys.stderr)
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"rubric: {path} unreadable ({exc}) — continuing without it", file=sys.stderr)
        return ""

    section = str(getattr(cfg, "rubric_section", "") or "").strip()
    if section:
        text = _section(text, section)
        if not text:
            print(f"rubric: no section matching {section!r} in {path} — continuing "
                  "without it", file=sys.stderr)
            return ""
    text = text.strip()
    if text:
        try:
            snapshot.write_text(text + "\n", encoding="utf-8")
        except OSError:
            pass  # the snapshot is an optimisation + drift guard, never a hard requirement
    return text


def for_builder(d: Path, cfg) -> str:
    """The rubric block appended to the builder prompt, or "" when unconfigured.

    Carries an explicit self-review instruction: the point is not that the builder has
    *seen* the criteria but that it applies them before emitting, which is what removes
    the guaranteed round.
    """
    text = load(d, cfg)
    if not text:
        return ""
    return ("\n\n## The target repo's standing review rubric — you are judged against "
            "THIS\n\nBefore you emit, re-read your own diff against every point below and "
            "fix what it flags. The reviewer applies the same text, so a violation you "
            "leave costs a guaranteed round.\n\n" + text)


def for_reviewer(d: Path, cfg) -> str:
    """The rubric block appended to a reviewer / adversary prompt, or "" when unconfigured.

    Includes the rubric's own rejected-finding classes, so a reviewer does not spend
    findings on classes the host has already declared noise.
    """
    text = load(d, cfg)
    if not text:
        return ""
    return ("\n\n## The target repo's standing review rubric — apply THIS\n\nJudge against "
            "the text below. Where it names finding classes the project rejects as noise, "
            "do not raise them: a finding the host has already declined costs a round and "
            "teaches the next reviewer nothing.\n\n" + text)
