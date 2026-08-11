"""A-priori slice-size estimate over ``brief.md`` (issue #320).

Two independent signals, combined by deterministic code:

1. **Structural** (this module) — stdlib only, no model, always available offline.
2. **Model** (``[leaves.sizer]``) — a cheap headless leaf answering the question
   structure demonstrably cannot: *how many independently shippable outcomes does this
   brief describe?* Its verdict reaches here through :func:`combine`, which lets it
   **escalate and never downgrade**.

## What the corpus says, and what it forbids

Calibrated against 86 settled bundles of a real instance (`size-calibrate`; #318/#319).
Spearman ρ of each a-priori brief feature against the two outcomes:

===================  ==========  ==============
feature              vs rounds   vs patch bytes
===================  ==========  ==============
conflicts_with            0.32             0.03
difficulty_rank           0.31             0.67
ext_deps                  0.28             0.33
brief_bytes               0.27             0.69
is_plan_pointer          -0.24             0.15
scope_words               0.07             0.47
declares_prod_reach       0.08             0.13
success_words            -0.06             0.36
has_out_of_scope         -0.06            -0.14
test_files                0.03             0.06
success_clauses           0.02             0.34
===================  ==========  ==============

Only the first five are weighted. The rest are indistinguishable from noise against
rounds, and weighting them would be fitting noise — ``scope_words`` in particular reads
as a size signal (0.47 against patch bytes) while carrying nothing about churn, which is
exactly the confusion this module has to avoid.

``is_plan_pointer`` is carried with a NEGATIVE weight: a brief pointing at a host
planning artifact converges *better* than a self-contained one, and without it the score
has no de-escalating term at all.

Those ρ values were measured over ORGANIC bundles, which is what makes ``conflicts_with``
mean *organic* conflicts here (issue #457). A split child declares a `Conflicts with`
entry for each of its siblings because ``split.materialise`` put it there — the ordering
fields between children are the point of a split — so counting them scored the process's
own scheduling metadata as churn: with ``difficulty_high`` inherited from the parent and
``ext_deps`` copied down, 3+3+3 = 9 banded every materialised child `oversized` before
anyone read its scope, and ``is_plan_pointer`` is a term a split child never has.
:func:`sibling_conflict_count` excludes exactly those ids and nothing else; the count it
excluded is reported on the estimate rather than dropped.

## Two readouts, because they are two questions

Structure predicts **patch size** well (0.67–0.69) and **churn** weakly (best 0.32).
Those are not the same target and neither is a proxy for the other — of 14 bundles with
a ≥100 KB patch, 10 churned; of 16 churners, 10 had a big patch. `issue_408` converged in
two rounds on a 267 KB patch (large but coherent); `issue_504` took three rounds on 11 KB
across two files (ill-specified, and splitting would not have helped).

So both are reported, separately labelled, and the combined ``band`` is the higher.
Collapsing them would trade one error set for another and hide which signal fired.

## Nothing here gates

Best measured precision is 62% (score ≥ 7 against ≥3 rounds) and 57% (patch ≥ 100 KB).
Roughly one wrong hold for every right one, which is the failure mode #321 exists to
avoid — a gate people learn to override. Both bands are advisory; the human decides.

## Every weighted feature reads the A-PRIORI text (issue #355)

A brief is not immutable. ``driver._carry_forward_into_brief`` **appends** an
``## Iteration N — carry-forward`` section recording the sign-off rationale and the
failing gates — text written after Do ran, and written only when an attempt was
*rejected*. Measuring anything from below that heading lets the outcome leak into the
predictor: ``brief.parse_fields`` keeps the FIRST match for a label, so a field the brief
never declared, appearing in carry-forward, switches its predictor on **because the bundle
churned**. The correlation improves, and the estimator looks better than it is — the one
direction nobody audits.

The cost is measured, not assumed: reading the file as-is moved ``brief_bytes`` from
ρ 0.21 to 0.64 and pushed the churned-median threshold ~70% too high.

So the split is defined **once**, here, and :class:`AprioriBrief` carries it to every
helper — the estimator and ``scripts/size-calibrate`` import the same two names, because a
second definition would drift the first time either side changed and the drift would be
invisible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import brief, split

#: Weights, proportional to |ρ| against rounds over the surviving features. In
#: ``[driver.sizing]`` so an instance retunes against its own corpus without patching the
#: engine — which is the point of #324's calibration loop.
DEFAULT_WEIGHTS = {
    "conflicts_with": 3,     # ρ 0.32 — strongest churn signal
    "difficulty_high": 3,    # ρ 0.31 (declared in only 60 of 86 briefs)
    "ext_deps": 3,           # ρ 0.28 — highest single-feature precision (57%)
    "brief_bytes": 3,        # ρ 0.27
    "is_plan_pointer": -2,   # ρ -0.24 — de-escalates
}

#: Weight of the sizer LEAF's verdict in the numeric score when :func:`combine` folds one
#: in (``[driver.sizing].model_weight``, issue #359). The shipped default 0 is today's
#: behaviour exactly: an above-``ok`` verdict escalates the BAND only and the score stays
#: purely structural — the corpus that would justify a non-zero weight accumulates per
#: instance, and weighting an unmeasured signal would be fitting folklore.
#:
#: A CONFIG VALUE, not a constant, so the review can actually move it: revisit at Act
#: cadence, against the Act index's per-cycle `sizing:` estimate-vs-outcome column and a
#: fresh ``scripts/size-calibrate`` run — once model escalations demonstrably track real
#: churn in YOUR corpus, raise it there like any other ``[driver.sizing]`` weight.
#:
#: Neither surface can show that yet: the index's `sizing:` line joins the STRUCTURAL
#: estimate only, and ``size-calibrate`` mines no model-verdict feature, so the
#: escalation-vs-outcome correlation a non-zero weight would rest on is unobservable
#: today. The retuning walk in pdca.toml's ``[driver.sizing]`` block names this blind
#: spot so an Act-cadence review does not mistake "no visible correlation" for
#: "no correlation" — or, worse, move the weight without evidence either way.
DEFAULT_MODEL_WEIGHT = 0

#: Brief-size cutoff, in KB, measured ABOVE the first carry-forward heading. 12 KB is the
#: knee: below it recall barely improves, above it recall falls faster than precision
#: rises. Measuring the file as-is instead would leak the outcome into the predictor — an
#: iterate APPENDS the sign-off rationale to the brief, so a bundle's brief is larger
#: *because* it churned (#319; the leak moved brief_bytes from ρ 0.21 to 0.64).
DEFAULT_BRIEF_KB = 12

#: Score cutoffs. `oversized` at 7 is where precision peaks (50% recall / 62% precision
#: against ≥3 rounds — the best of any rule tried); `watch` at 4 catches the band where
#: the churn rate first rises above the corpus base rate of 19%.
DEFAULT_WATCH = 4
DEFAULT_OVERSIZED = 7

#: The heading `driver._carry_forward_into_brief` writes — `## Iteration N — carry-forward
#: (from the previous attempt)`. ONE definition, shared with `scripts/size-calibrate`: the
#: two must agree on where the a-priori text ends or the estimator and the calibration
#: measure different things.
#:
#: Tight in both directions, because both are real. Too loose and a legitimate heading is
#: truncated away: a bare "starts with Iteration" test swallowed `## Iteration strategy`
#: (#349), and `\s+.*carry-forward` still swallowed `## Iteration 2 plan for carry-forward
#: compatibility` — discarding the scope below it and scoring a large slice as small. Too
#: tight and post-Do text is measured as input, which is the leak this module exists to
#: prevent.
#:
#: So the rule is stated as itself: after the number, NOTHING BUT PUNCTUATION before
#: `carry-forward`. `[^\w\n]*` is that sentence — prose cannot intervene, because prose
#: contains word characters, so `## Iteration 2 plan for carry-forward compatibility` is
#: rejected; and every separator a human might type is accepted without enumerating them.
#:
#: Enumerating was the mistake. Requiring a dash missed `## Iteration 2 carry-forward`;
#: allowing an optional dash still missed `## Iteration 2 : carry-forward`. Each was a
#: MISS, which is the leak direction and the more expensive of the two errors — and each
#: was a new guess at which punctuation a human would choose. `\n` is excluded because
#: `[^\w]` matches newlines, and without that the scan would run past the heading's own
#: line into the body.
#:
#: Verified against 94 real briefs: the split point moves on none of them.
_CARRY_FORWARD_RE = re.compile(r"^##\s+Iteration\s+\d+[^\w\n]*carry-forward",
                               re.IGNORECASE | re.MULTILINE)

#: Difficulty bands, WORD-matched. Substring alone mirrors `leaves._when_matches`, but
#: bare `"hard"` also fires on "hardening" / "hard-coded" — so
#: `Difficulty: medium — certificate hardening is localized` scored as high. Bands are
#: tested highest-first so a value naming more than one is not scored down, the same
#: direction of caution the routing takes.
_DIFFICULTY_BANDS = (("high", (r"high", r"hard")),
                     ("medium", (r"medium", r"moderate")),
                     ("low", (r"low", r"easy", r"trivial")))

OK, WATCH, OVERSIZED = "ok", "watch", "oversized"
_ORDER = {OK: 0, WATCH: 1, OVERSIZED: 2}


def higher(a: str, b: str) -> str:
    """The more severe of two bands — the only way bands are ever combined."""
    return a if _ORDER.get(a, 0) >= _ORDER.get(b, 0) else b


@dataclass(frozen=True)
class SizeEstimate:
    """A slice's estimated size, and why.

    ``band`` is the combined verdict (the higher of the two readouts, then escalated by
    the model signal if one is present). ``churn_band`` and ``patch_band`` are kept
    separate because they answer different questions and a human needs to know which
    fired — "3 conflicts declared" calls for different action from "predicted ~120 KB".
    """

    score: int
    band: str
    reasons: list[str] = field(default_factory=list)
    churn_band: str = OK
    patch_band: str = OK
    #: The sizer's own band, when one was given. Recorded even where it did not raise the
    #: combined band, because "two independently shippable outcomes" is the evidence that
    #: justifies a SPLIT — and a caller choosing between "split this" and "expect a large
    #: coherent patch" needs it whether or not it moved the number.
    model_band: str = ""
    #: How many of this bundle's `Conflicts with` entries name its own split siblings
    #: (`split.py:493-499`) and so were EXCLUDED from what `conflicts_with` contributed to
    #: ``score`` (issue #457). 0 for every bundle with no lineage record, which is today's
    #: behaviour exactly.
    #:
    #: Recorded even though it no longer moves the number, and for the same reason
    #: ``model_band`` is: the count is evidence about the SPLIT, not about the score. A
    #: proposal whose children all conflict pairwise is the splitter's own statement that
    #: the split separated nothing — and a caller that keyed off "does this bundle have
    #: lineage" instead would report that non-convergence identically to a clean split.
    sibling_conflicts: int = 0


def _cfg_int(cfg, key: str, default: int) -> int:
    sizing = getattr(cfg, "sizing", None) or {}
    try:
        return int(sizing.get(key, default))
    except (TypeError, ValueError, OverflowError):
        return default


def _weights(cfg) -> dict[str, int]:
    sizing = getattr(cfg, "sizing", None) or {}
    weights = dict(DEFAULT_WEIGHTS)
    for name in weights:
        if name in sizing:
            try:
                weights[name] = int(sizing[name])
            except (TypeError, ValueError, OverflowError):
                pass
    return weights


def sibling_conflict_count(brief_path: Path, conflict_ids: list[str]) -> int:
    """How many of ``conflict_ids`` name THIS bundle's own split siblings (issue #457).

    ``split.materialise`` writes a `Conflicts with` entry into every child naming its
    siblings (`split.py:493-499`), and ``split.rewrite_ordering`` turns the proposal-local
    labels into the same real ids the lineage record stores under ``siblings``
    (`split.py:333-358`) — the splitter is told outright that those ordering fields
    "BETWEEN children are the point" (`leaves.py:1261`). So a sibling id in `Conflicts
    with` is the split's own scheduling metadata, not organic churn: the ρ 0.32 behind the
    ``conflicts_with`` weight was measured over organic bundles (module docstring), and
    scoring the artifact the process itself created inflated every materialised child
    regardless of its scope.

    ``brief_path`` is the bundle's real brief path — the lineage record is read as
    ``brief_path.parent / split.LINEAGE``. Deliberately NOT reachable through
    :class:`AprioriBrief`: its allowlist (:data:`_DELEGATED`) refuses everything that could
    hand back a real ``Path``, and widening it to reach a *sibling file* would reopen the
    route to the brief's own post-Do bytes that the allowlist exists to close.

    Defined ONCE, here, and imported by both :func:`estimate` and
    ``scripts/size-calibrate`` — the same discipline as :func:`apriori_text` and
    :class:`AprioriBrief` above. A second definition would make one shared feature name
    denote two different quantities, and the next calibration would retune the weight
    against a number the engine no longer scores.

    **Total, like the reader it stands on.** ``split.read_lineage`` abstains on any file it
    cannot parse (`split.py:373-402`); this abstains on any VALUE it cannot compare with,
    the same division of labour as ``split._recorded_depth`` (`split.py:405-421`) —
    tolerating the file but not its contents only moves the throw one line down, into
    :func:`estimate`, whose whole contract is that a malformed brief never crashes the Plan
    beat. The record is a hand-editable hint, so ``{"siblings": "602"}`` (iterable, but a
    string — membership would match single CHARACTERS) and ``{"siblings": [[]]}`` (a member
    that cannot even be hashed, so building the set raises ``TypeError``) are both
    reachable. Hence the rule, stated once rather than as a list of the malformed shapes
    someone thought of: **both sides are narrowed to ``str`` before anything hashes them.**
    Whatever that drops is simply not a sibling, so it scores at full weight — pre-#457
    behaviour, the direction that under-corrects rather than silently discarding a real
    conflict.
    """
    raw = (split.read_lineage(brief_path.parent) or {}).get("siblings")
    siblings = {s for s in raw if isinstance(s, str)} if isinstance(raw, list) else set()
    return sum(1 for c in conflict_ids if isinstance(c, str) and c in siblings)


def estimate(brief_path: Path, cfg) -> SizeEstimate:
    """The structural estimate for one brief. Pure function of the file plus config.

    Never raises on a malformed or absent brief: an unreadable brief scores 0 / ``ok``,
    because a *detector* that crashes the Plan beat is worse than one that abstains.
    """
    if not brief_path.exists():
        return SizeEstimate(0, OK, ["no brief to size"])

    w = _weights(cfg)
    brief_kb = _cfg_int(cfg, "brief_bytes_kb", DEFAULT_BRIEF_KB)
    watch_at = _cfg_int(cfg, "watch", DEFAULT_WATCH)
    oversized_at = _cfg_int(cfg, "oversized", DEFAULT_OVERSIZED)

    try:
        # ONE read, ONE split, and every weighted feature measured from the result (#355).
        # Passing `brief_path` to these helpers is the leak: they read the file, so a
        # `Difficulty:` or `Conflicts with:` line the brief never declared but that appears
        # in an appended carry-forward section switches its predictor on — and only ever on
        # a bundle that churned. `AprioriBrief` keeps their field semantics and withholds
        # the text below the heading.
        ap = AprioriBrief(brief_path, apriori_text(brief_path))
        apriori = len(ap.read_text().encode("utf-8"))
        difficulty = brief.field(ap, "difficulty").lower()
        conflict_ids = brief.conflicts_with(ap)
        ext_deps = len(brief.external_dependency_tokens(ap))
        plan_pointer = bool(brief.planning_artifact(ap))
    except OSError:
        # A brief that cannot be READ abstains. A brief that cannot be DECODED no longer
        # does: `apriori_text` replaces the bad bytes, so the helpers now parse the same
        # already-decoded string rather than each re-reading with a strict decode. See its
        # docstring — an abstention scores `ok`, which is a confident "small" for a brief
        # nobody read, and one stray byte is a bad reason to emit one.
        return SizeEstimate(0, OK, ["brief unreadable — not sized"])

    # ORGANIC conflicts only (#457). A materialised child's siblings are in its `Conflicts
    # with` because the SPLIT put them there, and counting them made three of the five
    # weighted features artifacts of the process — 3+3+3 = 9 against a cutoff of 7 before
    # anyone looked at the child's scope, with `is_plan_pointer` (the one de-escalating
    # term) something a split child never has. Outside the `try` deliberately:
    # `sibling_conflict_count` is total (its docstring), so it cannot turn a hand-edited
    # lineage record into a crashed Plan beat, and there is no OSError here to catch.
    sibling_conflicts = sibling_conflict_count(brief_path, conflict_ids)
    conflicts = len(conflict_ids) - sibling_conflicts

    # Word-matched, not equality and not bare substring. The field is prose in practice
    # ("high — the widest-surface slice: …") so equality scores nearly every real brief as
    # unset — but a bare substring fires on "hardening", and the highest band must win so a
    # hedged value is not scored down.
    is_high = _band(difficulty) == "high"
    over_size = apriori >= brief_kb * 1024

    score = 0
    reasons: list[str] = []
    if is_high:
        score += w["difficulty_high"]
        reasons.append("difficulty=high")
    if over_size:
        score += w["brief_bytes"]
        reasons.append(f"brief {apriori / 1024:.1f} KB (cutoff {brief_kb} KB)")
    if conflicts:
        score += w["conflicts_with"]
        reasons.append(f"{conflicts} conflict(s) declared")
    if ext_deps:
        score += w["ext_deps"]
        reasons.append(f"{ext_deps} external dependency token(s)")
    if plan_pointer:
        score += w["is_plan_pointer"]
        reasons.append("points at a host planning artifact (converges better)")

    churn_band = (OVERSIZED if score >= oversized_at
                  else WATCH if score >= watch_at else OK)

    # The patch readout uses the rule that actually performed against patch size —
    # `difficulty=high AND brief >= cutoff` predicted a >=100 KB patch at 86% recall /
    # 55% precision. Reported separately because a large-but-coherent slice is not a slice
    # that needs splitting.
    if is_high and over_size:
        patch_band = OVERSIZED
        reasons.append("structurally predicts a large patch (~100 KB+)")
    elif is_high or over_size:
        patch_band = WATCH
    else:
        patch_band = OK

    return SizeEstimate(score, higher(churn_band, patch_band), reasons,
                        churn_band=churn_band, patch_band=patch_band,
                        sibling_conflicts=sibling_conflicts)


def _band(value: str) -> str:
    """The declared difficulty band, or "" when the brief names none.

    The LEADING token wins. Briefs write `- **Difficulty:** low — hard-won but small`:
    the band is what the field declares, and everything after the dash is justification
    prose that must not override it. Scanning the whole value highest-band-first reads
    "hard-won" as high and inverts the author's own answer.

    Only when the leading token names no band is the rest scanned, highest-first — so a
    value that hedges across bands is still not scored down.
    """
    head = re.split(r"[\s\u2014\u2013:;,()\[\]-]+", value.strip(), maxsplit=1)
    # Strip Markdown around the token: briefs write `low`, **low**, _low_ as readily as a
    # bare word, and an unstripped leading token falls through to the prose scan — where
    # `\`low\` — hard-won but small` is read as HIGH, inverting the author's own answer.
    lead = head[0].strip("`*_'\"").lower() if head else ""
    for band, needles in _DIFFICULTY_BANDS:
        if lead in needles:
            return band
    for band, needles in _DIFFICULTY_BANDS:
        if any(re.search(rf"\b{n}\b", value) for n in needles):
            return band
    return ""


def apriori_text(brief_path: Path) -> str:
    """The brief as it stood BEFORE any iterate — everything above the first carry-forward
    heading. The ONLY text a weighted feature may be measured from (module docstring).

    ``errors="replace"`` deliberately: this is the sole read behind every feature now, so a
    strict decode would turn one stray byte into an abstention that scores the slice ``ok``
    — a confident "small" for a brief nobody managed to read. Replacing the byte scores it
    on the other 99.99% instead. ``scripts/size-calibrate`` has always decoded this way, and
    the published figures come from that corpus.
    """
    text = brief_path.read_text(encoding="utf-8", errors="replace")
    m = _CARRY_FORWARD_RE.search(text)
    # rstrip: the append begins with its own newline, which would otherwise land on this
    # side of the split and make brief_bytes depend on whether an iterate happened — a
    # one-byte version of exactly the leakage this function exists to prevent.
    return (text[:m.start()] if m else text).rstrip()


def carry_forward_bytes(brief_path: Path) -> int:
    """Bytes of appended carry-forward — the outcome-dependent text excluded from every
    feature. Reported as its own column by the calibrator so the leak is auditable rather
    than invisible."""
    text = brief_path.read_text(encoding="utf-8", errors="replace")
    m = _CARRY_FORWARD_RE.search(text)
    return len(text[m.start():].encode("utf-8")) if m else 0


#: The ONLY attributes :class:`AprioriBrief` delegates to the real path — an allowlist, and
#: it has to be. Blocking known escape routes cannot work: ``resolve()``, ``absolute()``,
#: ``with_name()``, even ``parent``, each hand back a genuine ``Path`` whose ``read_text``
#: returns the FULL brief, and ``Path`` has more path-returning methods than a denylist could
#: chase. So the default is refuse, and the exceptions are narrowed to strings about the
#: name and existence predicates. A helper needing more gets a loud AttributeError pointing
#: here — the right trade, because the failure this guards against is a silently wrong
#: predictor, not a crash.
#:
#: What this is NOT: a sandbox. `ap._path` is an ordinary instance attribute and hands back
#: the real `Path` to anyone who asks for it by name, and no `__getattr__` can prevent that.
#: The guarantee is narrower and is the one that matters here — no helper reaches the file's
#: bytes through an attribute this class *approves*, so a brief helper written against the
#: `Path` API cannot silently defeat the split. Deliberate misuse is out of scope; the threat
#: model is a colleague's helper, not an adversary.
_DELEGATED = frozenset({"name", "stem", "suffix", "exists", "is_file"})


class AprioriBrief:
    """A stand-in for ``brief.md`` whose text is only the pre-iterate portion.

    Every ``brief`` / ``waves`` helper reads its brief through ``Path.read_text``, so handing
    them one of these instead of the real path makes them parse the *a priori* text while
    keeping their own (authoritative) field semantics. That matters: four of the five
    weighted features reach the text through one of those helpers, and read straight from
    disk they defeat the guard this module is built around.

    Re-parsing the fields here instead would mean a second implementation of the harness's
    field grammar, drifting silently the first time either side changed. So this narrows
    *what is read* rather than reimplementing ``Path``: :func:`read_text` answers with the
    a-priori text, a short allowlist of name-and-existence attributes delegates to the real
    path, and everything else is refused — see :data:`_DELEGATED` for why that has to be an
    allowlist rather than a list of blocked escape routes.
    """

    def __init__(self, path: Path, text: str) -> None:
        self._path = path
        self._text = text

    def read_text(self, *_args, **_kwargs) -> str:
        """The a-priori text, whatever encoding arguments the caller passed.

        The text is already decoded, so ``encoding`` / ``errors`` are accepted and ignored
        rather than rejected — a helper passing them is asking for the brief, not for a
        different decoding of it.
        """
        return self._text

    # No __fspath__: without it os.fspath() / open() raise TypeError at the call site instead
    # of quietly yielding the real file. AttributeError (not a custom type) is deliberate for
    # the rest — it is the signal "no such attribute", so incidental dunder probing by copy,
    # pickle or a test framework degrades normally instead of exploding.
    def __getattr__(self, name: str):
        if name not in _DELEGATED:
            raise AttributeError(
                f"AprioriBrief does not delegate {name!r}. It offers read_text() — the "
                f"a-priori text — plus {sorted(_DELEGATED)}. Anything else risks handing "
                "back a real Path, whose read_text() returns the whole brief.md including "
                "the post-Do carry-forward this class exists to withhold. Add a name here "
                "only if it cannot reach the file's bytes.")
        value = getattr(self._path, name)
        if callable(value):
            # Hand back a plain function, not the BOUND method. A bound method carries
            # `__self__` — the real `Path` — so `ap.exists.__self__.read_text()` returned
            # the whole brief through an attribute the allowlist had approved. `name` /
            # `stem` / `suffix` are plain strings and carry nothing.
            #
            # This closes the ATTRIBUTE route, not every route: the wrapper's closure
            # still holds the bound method, so `ap.exists.__closure__[0].cell_contents`
            # reaches it. That is the same category as `self._path` above — deliberate
            # introspection, not a helper written against the `Path` API — and no
            # `__getattr__` can close it while the object still holds the path it needs.
            # Accepted knowingly; the line between the two is "would a colleague's helper
            # do this by accident", and closure-walking is on the far side of it.
            return lambda *a, **k: value(*a, **k)
        return value


def combine(structural: SizeEstimate, model: dict | None, cfg=None) -> SizeEstimate:
    """Fold the sizer leaf's verdict into the structural estimate — **escalate only**.

    The model reads meaning; structure counts fields. So the model may raise a band that
    structure scored low (the case structure provably cannot see: one tidy-looking brief
    describing three independently shippable outcomes), but it may never lower one. A
    model that could downgrade would be a single point of failure over a signal that at
    least fails predictably, and "combined so the model can only escalate" is the property
    #320 is named for — asserted directly in the tests.

    ``cfg`` reads ``[driver.sizing].model_weight`` (#359): an above-``ok`` verdict adds
    that weight to the numeric score, the same shape as a structural feature firing. At
    the default 0 (:data:`DEFAULT_MODEL_WEIGHT` — see its Act-cadence note) the score is
    byte-identical to today's, so ``cfg=None`` callers and untouched configs lose nothing.
    Escalate-only holds for the score too: the weight is added only on a verdict that
    names watch/oversized, and never subtracted.

    **What is guaranteed:** a missing verdict, a non-dict verdict, or one whose ``band`` is
    absent or not one of ok/watch/oversized leaves the structural estimate exactly as it
    was. The leaf is optional and offline runs must be unaffected.

    **What is deliberately NOT required:** the rest of the schema. A verdict with a valid
    band but a sloppy ``independent_outcomes`` (a string rather than a list) or an
    unrecognised ``confidence`` still escalates, and the malformed fields are simply not
    quoted in the reasons. The band IS the answer this leaf was asked for; the other fields
    explain it. Discarding a real escalation because its explanation was untidy throws away
    the one signal worth paying a model for — and escalate-only means a wrong escalation
    costs a warning, never a block.
    """
    if not isinstance(model, dict):
        return structural
    band = str(model.get("band", "")).strip().lower()
    if band not in _ORDER:
        return structural
    combined = higher(structural.band, band)
    score = structural.score
    if _ORDER[band] > _ORDER[OK]:
        # The model flagged something — its configured weight joins the score exactly
        # like a structural feature's would. 0 by default (current behaviour), clamped
        # at 0: a negative weight would let the model LOWER a structural score, the
        # single point of failure this whole function exists to forbid.
        score += max(0, _cfg_int(cfg, "model_weight", DEFAULT_MODEL_WEIGHT))
    reasons = list(structural.reasons)
    outcomes = model.get("independent_outcomes")
    detail = f"sizer says {band}"
    if isinstance(outcomes, list) and outcomes:
        detail += f" — {len(outcomes)} independently shippable outcome(s)"
    # Only a RECOGNISED confidence is quoted. `null` rendered as "(confidence none)" and
    # "certain" as "(confidence certain)" — both read to a human as an answer the model
    # gave on the scale it was asked for, when in fact it gave none. Dropping them is what
    # the tolerant contract above already promises.
    confidence = str(model.get("confidence", "")).strip().lower()
    if confidence in ("low", "medium", "high"):
        detail += f" (confidence {confidence})"
    reasons.append(detail)
    return SizeEstimate(score, combined, reasons,
                        churn_band=structural.churn_band,
                        patch_band=structural.patch_band,
                        model_band=band,
                        # Carried, not recomputed: an escalation is about the brief's
                        # meaning and says nothing about the split's own metadata, so
                        # dropping the count here would hide a non-converged split behind
                        # exactly the verdict most likely to be attached to one.
                        sibling_conflicts=structural.sibling_conflicts)
