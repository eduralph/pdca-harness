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

#: "The caller did not tell us" — distinct from "the caller told us there is no worktree".
#: A plain `None` default cannot express that difference, and the difference is the whole
#: point: a builder whose `ensure()` FAILED passes None, and must not then have the stale
#: lane rediscovered on its behalf.
_UNSET = object()


def _section(text: str, heading: str) -> str:
    """The named Markdown section — from its heading to the next same-or-higher one.

    Instances commonly keep the rubric as one section of a larger ``AGENTS.md``; feeding
    the whole file would bury the rubric in unrelated project context and inflate every
    prompt that carries it.
    """
    wanted = heading.strip().lower()
    out: list[str] = []
    level = 0
    open_fence: tuple[str, int] | None = None
    in_comment = False
    for line in text.splitlines():
        # HTML comments first: a commented-out rubric draft is a realistic thing to find in
        # an AGENTS.md, and its headings are not structural Markdown. Selecting them would
        # hand every leaf rules the author had explicitly switched off.
        if not open_fence:
            if in_comment:
                if "-->" in line:
                    in_comment = False
                continue
            if "<!--" in line and "-->" not in line:
                in_comment = True
                continue
        f = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if f:
            marker, rest = f.group(1), f.group(2)
            if open_fence is None:
                open_fence = (marker[0], len(marker))
            elif (marker[0] == open_fence[0] and len(marker) >= open_fence[1]
                  and not rest.strip()):
                # A CLOSER may carry only trailing whitespace. ```python inside an open
                # backtick fence is an info string on a nested example, not a close — and
                # treating it as one exposes the example's headings to the scanner.
                # A closing fence must MATCH its opener: a ``` block quoting a ~~~ line
                # would otherwise close early, exposing a heading inside the example and
                # handing every leaf the sample instead of the real rubric.
                open_fence = None
        # ATX headings may be indented up to three spaces and still be structural Markdown;
        # a stricter anchor silently degraded such a section to "no rubric".
        m = None if open_fence else re.match(r"^ {0,3}(#{1,6})\s+(.*?)\s*$", line)
        if m:
            depth, title = len(m.group(1)), m.group(2).strip().lower()
            if level:
                if depth <= level:
                    break          # the next same-or-higher heading ends the section
            elif title == wanted or title.startswith(wanted):
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


def _owned_lane(d: Path, cfg) -> Path | None:
    """This bundle's worktree, when a caller did not say which tree is live.

    OWNERSHIP, not mere existence: an overflow gate can leave a lane preserved for a
    different bundle, and `worktree.path()` hands it back simply because the directory is
    there — which would snapshot that bundle's branch-specific rubric.
    """
    from . import worktree
    try:
        wt = worktree.path(d, cfg)
        if wt and wt.is_dir() and worktree.owner_of(wt) == d.name:
            return wt
    except Exception:  # noqa: BLE001 — isolation is optional
        pass
    return None


def _target_root(d: Path, cfg, worktree_root=_UNSET) -> Path | None:
    """Where to read the rubric from, most specific first.

    1. The bundle's ACTIVE worktree, when isolation is on. `_do_build_command` has already
       created it pinned to the brief's target base, so the primary checkout may be on
       another branch, stale, or carrying a dirty AGENTS.md — and for a stacked bundle the
       worktree includes a prerequisite branch the rubric may depend on.
    2. The resolved target checkout.
    3. The mapped checkout directly. `worktree._target` requires a `.git` entry, so a
       supported non-Git target (Do runs in place) would otherwise silently lose its rubric.
    """
    from . import worktree
    if worktree_root is not _UNSET:
        # The caller knows which tree is live. A path wins outright — it came from a
        # SUCCESSFUL `ensure()` and is what the builder is editing. An explicit None means
        # setup FELL BACK to in-place, and the lane probe is skipped entirely: `ensure()`
        # can fail AFTER creating the lane and leave its owner stamp behind, so an
        # ownership check alone would hand back a tree nobody is editing.
        if worktree_root is not None and Path(worktree_root).is_dir():
            return Path(worktree_root)
    elif (wt := _owned_lane(d, cfg)) is not None:
        return wt
    try:
        resolved = worktree._target(d, cfg)
        if resolved:
            return resolved[0]
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import publish
        repo_spec, _base, _slug = publish._resolve_target(d)
        if not str(repo_spec).strip():
            # `_checkout_path(cfg, "")` resolves to cfg.root.parent, so a brief with no
            # usable target would read a rubric out of an unrelated sibling directory.
            return None
        mapped = publish._checkout_path(cfg, repo_spec)
        return mapped if mapped.is_dir() else None
    except Exception:  # noqa: BLE001 — no target is a warning, never a crash
        return None


def load(d: Path, cfg, worktree_root=_UNSET) -> str:
    """The rubric text for bundle ``d`` — snapshotting on first use. "" when unconfigured.

    Later callers get the snapshot even if the target has moved on, which is the whole
    point: the builder and both reviewers must be judged against the same contract.
    """
    snapshot = d / SNAPSHOT
    if snapshot.exists():
        try:
            return snapshot.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return ""

    rel = str(getattr(cfg, "rubric_file", "") or "").strip()
    if not rel:
        # Pinned like every other fail-open path: if the key is unset at Do and set before
        # Check, the reviewer would otherwise apply a rubric the builder never received.
        # Configuration changes take effect on the NEXT attempt, not mid-cycle.
        _record(snapshot, "")
        return ""

    target = _target_root(d, cfg, worktree_root)
    if target is None:
        print(f"rubric: cannot resolve the target checkout for {d.name} — "
              "continuing without the rubric", file=sys.stderr)
        _record(snapshot, "")
        return ""
    path = _resolve(target, rel)
    if path is None or not path.is_file():
        print(f"rubric: [project].rubric_file = {rel!r} does not resolve to a file inside "
              f"the target checkout — continuing without it", file=sys.stderr)
        _record(snapshot, "")
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError is NOT an OSError: a rubric file that is not valid UTF-8 would
        # otherwise propagate out of the Do beat, turning the documented fail-open into a
        # hard abort.
        print(f"rubric: {path} unreadable ({exc}) — continuing without it", file=sys.stderr)
        _record(snapshot, "")
        return ""

    section = str(getattr(cfg, "rubric_section", "") or "").strip()
    if section:
        text = _section(text, section)
        if not text:
            print(f"rubric: no ATX heading matching {section!r} in {path} — continuing "
                  "without it. Section selection reads `## Heading` only; a Setext "
                  "heading (underlined with --- or ===) is not recognised.",
                  file=sys.stderr)
            _record(snapshot, "")
            return ""
    text = text.strip()
    _record(snapshot, text)
    return text


def _record(snapshot: Path, text: str) -> None:
    """Snapshot the outcome — INCLUDING an empty one.

    Recording only successes leaves the drift window open in the other direction: if the
    builder found no rubric and the target then gains one before Check (an in-place build
    creates it, an operator restores it), the reviewer retries the live lookup and is
    handed rules the builder never saw. An empty snapshot pins "no rubric for this
    attempt" for every later leaf.
    """
    try:
        snapshot.write_text(text + "\n" if text else "", encoding="utf-8")
    except OSError:
        pass  # the snapshot is a drift guard, never a hard requirement


def for_builder(d: Path, cfg, worktree_root=_UNSET) -> str:
    """The rubric block appended to the builder prompt, or "" when unconfigured.

    Carries an explicit self-review instruction: the point is not that the builder has
    *seen* the criteria but that it applies them before emitting, which is what removes
    the guaranteed round.
    """
    text = load(d, cfg, worktree_root)
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
