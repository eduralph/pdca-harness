"""Assemble ``SUMMARY.md`` from brief + gates + review (docs 02 §SUMMARY.md).

Pure code, no model: the driver assembles §1–8 from the brief, the gate JSON, and
the reviewer's findings, routes every reviewer ``NEEDS-HUMAN`` into §6, and leaves
§9 (sign-off) and §10 (Act candidates) empty for the human. The section shape
mirrors ``templates/SUMMARY.md.tpl`` — keep the two in step if you edit either.
"""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from . import brief, doctor, size_signal, state
from .config import Config
from .gates import canonical_elements

# The two kinds of §6 item (issue #264).
#   IMPL  — an implementation defect the BUILDER can fix by iterating Do.
#   HUMAN — an architectural / fitness-to-purpose / environmental call only the human makes.
IMPL = "impl"
HUMAN = "human"
# The reviewer's `Validation — fitness-to-purpose` row, which its prompt hard-codes to
# NEEDS-HUMAN on EVERY cycle regardless of content (agents/reviewer.md.jinja; the 5/5/1's
# validation oracle is literally "human at sign-off"). It is the human's to settle at sign-off
# — but because it is emitted unconditionally it carries NO signal, so it must not be read as
# evidence that a human has to look *right now*. Treating it as an ordinary HUMAN item made
# auto-iterate (#264) unreachable in production: every real review artifact carries this row,
# and `eligible()` demanded that EVERY item be IMPL, so the feature never once fired (#293).
#
# It still renders in §6 as a `- [ ]` the human must clear, and the C6 accept-guard still
# blocks on it. Since #409 no HUMAN item vetoes a rebuild either — a HUMAN finding is deferred
# to the handover §6 — so what still sets this row apart is that it is never deferred: every
# Check re-emits it, and the handover §6 carries it anyway (`autoiterate.defer`).
STANDING = "standing"


class NeedsHumanItem(NamedTuple):
    """One §6 row: the text the human reads, plus who can resolve it."""

    text: str
    kind: str
    # True for a row the harness writes itself because a review or a gate gave NO verdict
    # this round — the review missing or a placeholder, an advisory leaf's placeholder, a gate
    # that could not run. It is about this Check's own run, not the patch, and every Check
    # runs the review and the gates again, so the next Check's §6 carries the row exactly
    # while the problem lasts. That is why auto-iterate never defers one (#409): held for the
    # handover, it would ask the human to clear a failure that had long since recovered.
    no_verdict: bool = False


# The implementation/architectural split is NOT a new taxonomy — it is the `kind` already
# carried by the canonical 5/5/1 (gates._FIVE_FIVE_ONE). `gate` cells (C2/C4/T1..T4) are
# mechanically checkable ⇒ builder-fixable. `judgment` cells (C5 causal adequacy, T5
# judgment, V validation) and `input` cells (C1 spec, C3 change) are the human's.
_GATE_ELEMENTS = frozenset(e for e, _label, kind, _oracle in canonical_elements()
                           if kind == "gate")

# The judgment cells the REVIEWER may hand back to Do by tagging its verdict
# `NEEDS-HUMAN [impl]` (#408): C5 causal adequacy and T5 judgment, whose substance is often a
# plain build defect. Not V — the reviewer emits that row NEEDS-HUMAN on every cycle, so it can
# never name something a rebuild fixes — and not the `input` cells C1/C3: a defect in the spec
# or the change's scope survives any rebuild against the same brief. Derived from the taxonomy,
# like `_GATE_ELEMENTS`, so it cannot drift from the matrix.
_PROMOTABLE_ELEMENTS = frozenset(e for e, _label, kind, _oracle in canonical_elements()
                                 if kind == "judgment" and e != "V")

# A §6 item's leading 5/5/1 element id, when the reviewer's table row carries one.
_ELEMENT_RE = re.compile(r"^(C[1-5]|T[1-5]|V)\b")

# An advisory leaf tags a builder-fixable finding `- NEEDS-HUMAN [impl] — …`. Unmarked
# findings stay HUMAN, so a legacy advisory file can never trigger an auto-iteration.
_IMPL_MARKER_RE = re.compile(r"^\[impl\]\s*[—:-]*\s*", re.IGNORECASE)
# Either routing tag a leaf may lead a finding with (#408): `[impl]` (a rebuild can fix it) or
# `[human]` (it cannot). Both are stripped from the §6 text; `[human]` always means HUMAN.
_TAG_MARKER_RE = re.compile(r"^\[(impl|human)\]\s*[—:-]*\s*", re.IGNORECASE)
# The reviewer's own tag: a verdict table row whose Verdict cell IS `NEEDS-HUMAN [impl]`,
# emphasis aside — matched against the WHOLE cell. A cell that only mentions it, such as
# `PASS (was NEEDS-HUMAN [impl])`, states another verdict, and promoting it would buy a
# needless rebuild.
_VERDICT_IMPL_RE = re.compile(r"[*_`\s]*needs-human[*_`\s]*\[impl\][*_`\s]*", re.IGNORECASE)

# How `_classify_finding` treats a leading `[impl]` tag, by the artifact the finding came from.
# FREE — a Check advisory bullet: honoured on any finding (advisory bullets carry no reliable
# element id to bound it by). BOUNDED — the primary review: honoured only when the finding's
# element is in `_PROMOTABLE_ELEMENTS`, otherwise ignored. IGNORED — a plan advisory: it
# reviews the brief, which no rebuild changes, so the tag is stripped and the item is HUMAN.
TAGS_FREE = "free"
TAGS_BOUNDED = "bounded"
TAGS_IGNORED = "ignored"

# Where a `- NEEDS-HUMAN` bullet's continuation ENDS (issue #527), mirroring the
# membership rule `brief._block_for` already uses for a wrapped field value
# (`brief.py:91-98`): a line indented deeper than the bullet is prose that keeps
# going, until one of these signals the block is over — a new list item at ANY
# indent (so an indented `  - NEEDS-HUMAN …` sub-bullet is still its own item), a
# heading, a table row, or a code fence.
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s")
_CODE_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _ends_needs_human_continuation(line: str) -> bool:
    s = line.strip()
    return bool(
        _LIST_ITEM_RE.match(line)
        or s.startswith("#")
        or s.startswith("|")
        or _CODE_FENCE_RE.match(line)
    )

# The one STANDING row (#293) — recognised by the canonical label, not a hardcoded string, so
# it cannot drift from the matrix the reviewer's table mirrors. `V` is the only element the
# reviewer's prompt hard-codes to NEEDS-HUMAN on every cycle; C5/T5 are judgment too, but the
# reviewer raises those only when it has an actual concern, so they stay situational HUMAN.
_V_LABEL = next(label for e, label, _kind, _oracle in canonical_elements() if e == "V")
# Every 5/5/1 Item cell, used to recognise the MANDATED verdict table itself — the row alone was
# never enough (PR #294 review, local pass): a "## Concerns" table can carry the exact same label.
_CANONICAL_LABELS = frozenset(label.strip().casefold()
                              for _e, label, _kind, _oracle in canonical_elements())
# Which element each canonical label belongs to — for `_normalized_item_label`'s prefix rule.
_ELEMENT_OF_LABEL = {label.strip().casefold(): e
                     for e, label, _kind, _oracle in canonical_elements()}
_ITEM_PREFIX_RE = re.compile(r"^(C[1-5]|T[1-5]|V)\s*—\s*(.+)$")


def _normalized_item_label(cell: str) -> str:
    """A verdict table's Item cell in the form the 5/5/1 spells it (#408).

    Reviewers write the cell three ways: the bare label, the label behind its element id
    (`V — Validation — fitness-to-purpose`, which an older reviewer prompt listed), and with
    ASCII `--` for the em dash. This folds `--` to `—`, collapses whitespace, and drops a
    leading `<element-id> —` ONLY when the label after it is that element's own label. A
    mismatched prefix is kept (`C5 — Validation — fitness-to-purpose` stays as written), so
    such a cell never equals a canonical label. The caller still compares the result EXACTLY:
    nothing here matches on a prefix of free text (the #294 rule).
    """
    s = " ".join(cell.replace("--", "—").split())
    m = _ITEM_PREFIX_RE.match(s)
    if m and _ELEMENT_OF_LABEL.get(m.group(2).casefold()) == m.group(1):
        return m.group(2)
    return s

# Leaf-status marker (issue #278). When a reviewer / advisory leaf could not produce a
# verdict, `leaves` writes a placeholder carrying one of these as a machine-readable comment.
# An EMPTY advisory artifact is otherwise ambiguous: "the adversary ran and found nothing"
# reads identically to "the adversary never ran" — and an infra failure then presents as a
# clean adversarial pass. The status lets §6 say WHY the artifact is empty, and lets a
# consumer act on it (re-run vs adjudicate) instead of parsing prose.
# Both INFRA shapes mean "no review came back" (a transient one may have worked for minutes
# first), but they call for different ACTIONS, so the §6 row must not conflate them: a
# transient blip is safe to re-run as-is, while a leaf whose command could never be launched
# will fail identically until that command is fixed — telling the operator "safe to re-run"
# there would be a false instruction (PR #285 review).
# A third infra shape (issue #526): the leaf's command launched, but the vendor sandbox the
# harness seeded it with could not start on this host, so no command the leaf tried ever ran.
# Its action differs from both others — neither a re-run nor a config fix helps until the HOST
# can start that sandbox — so it gets its own marker rather than borrowing one's instruction.
#
# The marker only ever explains WHY an artifact is a placeholder. What decides WHETHER it is
# one is the completion trailer below (#541) — a positive stamp at a fixed position, so an
# artifact's classification turns on who closed it, never on what its text happens to mention.
# The table of statuses is therefore CLOSED, at the four below: a shape needing more detail
# than a token carries — the un-owned artifact of #541, whose leaf ran and exited 0 while
# nothing at its artifact path could be attributed to it — reuses `human-empty` (true of it: no
# usable verdict reached the bundle) and states its specifics in the placeholder's own prose,
# which is where a human reads them. Rounds 2–4 of #541 each patched the marker instead —
# labelling unknown tokens, an 8-line header window, a fourth token — and each re-opened the
# same hole, the last by self-triggering on any artifact that quoted its new token. Grow this
# table only for a MACHINE consumer that provably has to act on the difference — as
# `sandbox-empty` does: the plan-advisory benefit record and its §10 line name the host as the
# cause (#526) — and then say so in that issue.
# infra-empty: ran, and died of a transient blip — before emitting any work, or on its own
# report of a transient API error (a leaf that worked for minutes, then lost the API).
LEAF_STATUS_INFRA = "infra-empty"
LEAF_STATUS_STARTUP = "startup-empty"  # never launched — binary absent / not executable
LEAF_STATUS_SANDBOX = "sandbox-empty"  # launched, but its seeded sandbox could not start
LEAF_STATUS_HUMAN = "human-empty"      # ran, but yielded no usable verdict
_LEAF_STATUS_RE = re.compile(r"<!--\s*pdca:leaf-status\s+(\S+)\s*-->")
_LEAF_STATUS_LABEL = {
    LEAF_STATUS_INFRA: ("leaf died of transient infra (before emitting any work, or on its "
                        "own report of a transient API error — safe to re-run)"),
    LEAF_STATUS_STARTUP: ("leaf did not run (its command could not be launched — fix the "
                          "leaf's config, then re-run)"),
    LEAF_STATUS_SANDBOX: ("leaf could not work (the vendor sandbox it was seeded with could "
                          "not start on this host — fix the host, then re-run)"),
    LEAF_STATUS_HUMAN: "leaf produced no usable verdict (needs a human)",
}
# The completion trailer (#541) — what a leaf writes as the very LAST line of an artifact it
# finished. The harness's own placeholders never carry it: they were written BECAUSE the leaf
# did not close one. Recognised only as the last non-blank line, whole-line and exact, so a
# report QUOTING it (or quoting a status marker) inside a fenced block cannot stamp itself
# complete — the fence's closing line is the last one, not the trailer.
#
# Absence classifies nothing. Leaves are arbitrary commands and a third-party one cannot be
# compelled to emit this, so an artifact without it behaves exactly as it always has: it falls
# through to the marker search below, unchanged — including that search's old misreading of a
# report that merely QUOTES a marker, which only the trailer's presence corrects. That is also
# why the whole back catalogue — no bundle in it carries the trailer — is unaffected on the day
# this lands.
LEAF_COMPLETE_TRAILER = "<!-- pdca:leaf-complete -->"
# Only a status in the table above ever relabels an artifact. An UNRECOGNISED token — a newer
# harness's bundle, a hand-edited artifact, or an advisory leaf QUOTING a marker while
# reviewing this harness — leaves the artifact alone, exactly as it always has: for an artifact
# with no trailer the marker is still matched ANYWHERE in the text, so any rule that labelled an
# unknown token would have to guess whether the artifact is a placeholder or merely quotes one,
# and every such guess mislabels a real verdict table in some shape: "leaf produced no verdict"
# on findings that exist, with their `[impl]` routing (#264) stripped. Nothing is lost by
# declining: a placeholder's own items are unmarked prose, so they are already HUMAN, and its
# prose says in words what the marker says in machine terms. What DOES have to hold is that
# every status `leaves` can write is in the table — asserted in
# template/tests/test_attempt_harvest.py, which also pins the table's size.

# What a harness placeholder states in its one NEEDS-HUMAN bullet when a review leaf returned
# no verdict: the reviewer's (`leaves._review_unavailable`) and an advisory leaf's
# (`leaves._advisory_unavailable`). `leaves` writes them from here, so `collect_needs_human` can
# tell that row from a finding by its text (#409). An artifact can READ as a placeholder and
# still carry its leaf's real findings — a report that quoted a status marker and never closed
# itself (`leaf_status`) — and those stay findings; only this exact row is the no-verdict one.
REVIEW_UNAVAILABLE_FINDING = ("re-run the Check reviewer; this bundle has no advisory review "
                              "and must not be accepted until one exists.")
ADVISORY_UNAVAILABLE_FINDING = ("advisory leaf '{leaf}' did not produce findings ({reason}); "
                                "re-run it or adjudicate by hand.")


def _is_advisory_unavailable(text: str, leaf: str) -> bool:
    """Is ``text`` the placeholder row :data:`ADVISORY_UNAVAILABLE_FINDING` renders for the
    advisory leaf ``leaf``, whatever its reason?"""
    head, tail = ADVISORY_UNAVAILABLE_FINDING.format(leaf=leaf, reason="\0").split("\0")
    return (len(text) >= len(head) + len(tail)
            and text.startswith(head) and text.endswith(tail))


# Every label this module renders IN FRONT of a finding, as `<label> — <text>`: a verdict-table
# row's canonical 5/5/1 Item cell (`_needs_human`) and a placeholder's leaf-status label
# (`_items_from_artifact`). A label says where a finding came from — its element, or why its
# leaf returned no verdict — not what the finding says, so two §6 rows that share one are not
# thereby the same finding. `autoiterate._same_finding` compares rows past the labels they
# share (#409).
FINDING_LABELS = (tuple(label for _e, label, _kind, _oracle in canonical_elements())
                  + tuple(_LEAF_STATUS_LABEL.values()))


def leaf_status(artifact_text: str) -> str:
    """The leaf-status marker a reviewer/advisory placeholder carries, or "" for a real
    artifact (a leaf that actually produced findings) — issue #278.

    An artifact the leaf CLOSED is real whatever it quotes (#541): the completion trailer as
    the last non-blank line is a positive statement by the writer about the whole file, which
    a mention of a marker in the body is not, and a dying attempt's half-written report is
    very unlikely to have made it — it would need to be cut off exactly on a line that quotes
    the trailer. Checked first, and only there — an anchored test, so the answer no longer
    depends on where in a report a string appears (an 8-line header window was holed by a
    fenced block opening at line 5; matching anywhere is holed by any report that quotes a
    marker, which is every advisory review OF this harness).
    """
    closing = next((ln for ln in reversed(artifact_text.splitlines()) if ln.strip()), "")
    if closing.strip() == LEAF_COMPLETE_TRAILER:
        return ""
    m = _LEAF_STATUS_RE.search(artifact_text)
    return m.group(1) if m else ""


def _one_line(value: str) -> str:
    """A brief value flattened to one line, for a context that cannot hold a newline.

    Only the SUMMARY title uses this: it is a Markdown `#` heading, so the two-space
    continuation indent :func:`_item` applies would render as literal text rather than a
    wrapped list item (#336).
    """
    return " ".join(value.split())


def _item(value: str) -> str:
    """A brief value rendered as the tail of a SUMMARY `- Label: …` bullet.

    Continuations are indented two spaces so a multi-line value stays ONE Markdown list
    item instead of terminating the list and dumping the remainder as body prose (#336).
    """
    lines = value.splitlines() or [""]
    # A value whose FIRST line is itself a list item — ORDERED (`1.`, `2)`) as well as
    # unordered — is a nested list under an empty
    # label — the documented Scope/API shape. Rendering it inline gives
    # `- Scope: - **API:** …` with the remaining bullets nested beneath, which flattens the
    # first child into the label and changes the brief's meaning in SUMMARY. Put the whole
    # block on its own lines instead, so the hierarchy the brief authored survives.
    if re.match(r"^\s*(?:[-*+]|\d+[.)])\s", lines[0]):
        return "\n" + "\n".join(f"  {line}" if line else "" for line in lines)
    first, *rest = lines
    return "\n".join([first] + [f"  {line}" if line else "" for line in rest])


def _classify_finding(text: str, *, standing: bool = False, verdict_impl: bool = False,
                      verdict_other: bool = False, tags: str = TAGS_FREE) -> NeedsHumanItem:
    """Classify one reviewer / advisory §6 item, stripping any `[impl]` / `[human]` marker.

    Three kinds. IMPL — a rebuild can address it. STANDING — the reviewer's `Validation` row,
    which its prompt emits NEEDS-HUMAN on every cycle whatever it finds, so its presence proves
    nothing (#293). HUMAN — everything else.

    ``standing`` is decided by the CALLER and defaults off. It is true only for the canonical
    5/5/1 verdict row of the PRIMARY review, identified by an exact match on its Item cell
    (:func:`_needs_human`). This function does not re-derive it from the text, deliberately: a
    prefix test on the text is what let a real objection wear the template's clothes, and two
    sources of truth for "is this the constant row" is what produced that bug (PR #294 review).

    Fail safe throughout: an item we cannot map to a gate element — an unmarked advisory bullet,
    a reviewer row whose Item cell doesn't start with a canonical id, the missing-review
    placeholder — is HUMAN. Auto-iterate only ever fires on findings we positively know a
    rebuild can address, and neither HUMAN nor STANDING is one of them: neither ever *causes*
    a rebuild. Beside an IMPL item, a HUMAN finding is deferred to the handover §6 (#409);
    STANDING is not even deferred, since every Check re-emits it — and neither is a
    ``no_verdict`` row (:class:`NeedsHumanItem`), for the same reason.

    The finding's own builder-fixability statement (#408) is honoured AFTER the STANDING check,
    so a tag can never lift the constant V row. ``verdict_impl`` is the reviewer's
    `NEEDS-HUMAN [impl]` Verdict cell on a verdict table row, decided by the caller; it
    promotes the row only on a `_PROMOTABLE_ELEMENTS` cell (C5/T5). A leading `[impl]` in the
    text is weighed by ``tags`` (see `TAGS_FREE`): free for a Check advisory bullet, bounded
    to C5/T5 for the primary review, ignored for a plan advisory. A tag that is not honoured
    is dropped and the item classifies as if untagged. A leading `[human]` is stripped and
    the item is HUMAN.

    ``verdict_other`` is a verdict table row whose Verdict cell holds some other verdict
    (PASS / FAIL / N/A) while another of its cells mentions NEEDS-HUMAN, decided by the
    caller. The row contradicts itself, so it is HUMAN whatever its element: it still reaches
    the human, and never buys a rebuild — not even on a gate cell (#408 sign-off).
    """
    if standing:
        return NeedsHumanItem(text, STANDING)   # emitted every cycle ⇒ carries no signal (#293)
    tag = ""
    m = _TAG_MARKER_RE.match(text)
    if m:
        tag = m.group(1).lower()
        text = text[m.end():].strip()
    if tag == "human" or tags == TAGS_IGNORED or verdict_other:
        return NeedsHumanItem(text, HUMAN)
    m = _ELEMENT_RE.match(text)
    element = m.group(1) if m else ""
    if verdict_impl and element in _PROMOTABLE_ELEMENTS:
        return NeedsHumanItem(text, IMPL)
    if tag == "impl" and (tags == TAGS_FREE or element in _PROMOTABLE_ELEMENTS):
        return NeedsHumanItem(text, IMPL)
    if element in _GATE_ELEMENTS:
        return NeedsHumanItem(text, IMPL)
    return NeedsHumanItem(text, HUMAN)


def _items_from_artifact(text: str, *, allow_standing: bool = False,
                         plan_advisory: bool = False,
                         no_verdict: Callable[[str], bool] = lambda _t: False,
                         ) -> list[NeedsHumanItem]:
    """§6 items from one reviewer / advisory artifact, labelled by its leaf status (#278).

    ``allow_standing`` is passed only for the PRIMARY review (#294 review) — see
    :func:`_classify_finding`. An advisory leaf's free-form bullets never earn STANDING. It
    also marks the artifact whose `[impl]` tags are bounded to C5/T5 and whose Verdict-cell
    `NEEDS-HUMAN [impl]` is read (#408). ``plan_advisory`` marks a plan advisory, whose tags
    are ignored: every item it yields is HUMAN.

    A placeholder (the leaf could not produce a verdict) has its items prefixed with WHY the
    artifact is empty — infra vs substance — so the human doesn't have to hand-annotate it,
    and forced to HUMAN: there is no finding for a rebuild to fix, so a placeholder never
    causes an auto-iterate round (#264). Beside real implementation work it does not stop
    one either. The placeholder's own row — the one ``no_verdict`` recognises — is marked
    ``no_verdict``: the next Check runs the leaf again, so auto-iterate does not defer it
    (#409). Any other row of an artifact that merely reads as a placeholder is that leaf's
    finding, deferred like any HUMAN item. A real artifact is unaffected — including one that
    merely QUOTES a marker, recognised or not, and closed itself with the completion trailer
    (:func:`leaf_status`)."""
    label = _LEAF_STATUS_LABEL.get(leaf_status(text), "")
    tags = TAGS_IGNORED if plan_advisory else TAGS_BOUNDED if allow_standing else TAGS_FREE
    items = [_classify_finding(row.text, standing=allow_standing and row.standing,
                               verdict_impl=allow_standing and row.impl,
                               verdict_other=row.other, tags=tags)
             for row in _needs_human_rows(text)]
    if not label:
        return items
    return [NeedsHumanItem(f"{label} — {it.text}", HUMAN, no_verdict=no_verdict(it.text))
            for it in items]


def collect_needs_human(d: Path, cfg: Config) -> list[NeedsHumanItem]:
    """Every §6 item for this bundle, tagged IMPL / HUMAN, in the order §6 renders them.

    Single source for both the rendered §6 and the auto-iterate decision (issue #264), so
    the classifier can never disagree with what the C6 accept-guard sees.
    """
    gates_json = json.loads((d / "check-gates.json").read_text(encoding="utf-8"))
    review_path = d / "check-review.md"
    review_text = (review_path.read_text(encoding="utf-8")
                   if review_path.exists() else _missing_review_text(d))

    # Only the PRIMARY review may carry a STANDING row: it is the one artifact whose prompt
    # mandates the Validation row unconditionally, which is the entire basis for treating it as
    # signal-free. An advisory leaf raising fitness-to-purpose means it FOUND something.
    items = _items_from_artifact(review_text, allow_standing=True,
                                 no_verdict=lambda t: t == REVIEW_UNAVAILABLE_FINDING)
    if not review_path.exists():
        # The missing-review placeholder: its one row says no review exists — no verdict.
        items = [it._replace(no_verdict=True) for it in items]
    for p in sorted(d.glob("check-advisory-*.md")):
        leaf = p.stem.removeprefix("check-advisory-")
        items += _items_from_artifact(
            p.read_text(encoding="utf-8"),
            no_verdict=lambda t, leaf=leaf: _is_advisory_unavailable(t, leaf))
    # A gate that COULD NOT RUN is not builder-fixable — a rebuild cannot supply the missing
    # mechanic — so it is HUMAN regardless of its (gate-kind) element: it never causes an
    # auto-iterate round. It is no verdict either, so it is not deferred (#409): every Check
    # runs the gates again, and the next §6 carries it exactly while it still cannot run.
    items += [NeedsHumanItem(t, HUMAN, no_verdict=True) for t in _unverifiable_items(gates_json)]
    # A gating row that failed and then passed its one confirm re-run (#371) is recorded
    # `pass` + `flaky`: it counts as the pass it recorded, but the red sample is not
    # dropped — the human acknowledges it. HUMAN whatever its element, because a rebuild
    # cannot fix an intermittent environment; NOT no_verdict, because both samples are
    # verdicts on this round's tree, so auto-iterate may defer it (#409).
    items += [NeedsHumanItem(t, HUMAN) for t in _flaky_items(gates_json)]
    items += _failed_gating_items(gates_json)
    build_notes = d / "build-notes.md"
    if build_notes.exists():
        items += [NeedsHumanItem(t, HUMAN)
                  for t in _declared_external_deps(build_notes.read_text(encoding="utf-8"))]
    # The Do-exit adjudication record (#341): a declaration the detect-cmd probe REFUTED
    # proceeded to full Check, and the refutation must reach the human — and `pdca act
    # index`, which reads §6 — rather than stay a bundle-local json only the driver saw.
    # HUMAN, never IMPL: a rebuild cannot fix a mis-declaration. Local import, because
    # dependency_halt delegates its marker parsing to `_declared_external_deps` above
    # (one parser for "did the builder declare a dependency").
    from . import dependency_halt
    items += [NeedsHumanItem(t, HUMAN) for t in dependency_halt.refuted_items(d)]
    items += [NeedsHumanItem(t, HUMAN)
              for t in _unregistered_dependency_items(d / "brief.md", cfg)]
    # Plan-advisory findings (#301 + review): folded into §6 individually, exactly like
    # the Check advisories — including the decorrelation note and any NOT-COMPLETED
    # placeholder, which no other summary path reads. Each finding stays visible until
    # the human dispositions it at sign-off: a bundle-wide "was the brief revised?" bit
    # cannot say WHICH findings the revision addressed, so it must never suppress them
    # (one cosmetic edit would have hidden every remaining objection from C6). All
    # HUMAN-kind by construction — parsed with any `[impl]` tag stripped and ignored (#408), so
    # a plan advisory can never cause an auto-iterate round (#264), whatever its prompt says;
    # beside real implementation work they are deferred to the handover §6 like any HUMAN
    # item (#409).
    for ptext in [p.read_text(encoding="utf-8")
                  for p in sorted(d.glob("plan-advisory-*.md"))]:
        items += _items_from_artifact(ptext, plan_advisory=True)
    # The empirical size backstop (#324). HUMAN, never IMPL — the tag and the text
    # together are the mechanism: `autoiterate.eligible()` rebuilds past every other HUMAN
    # item (#409) but STOPS the rebuild loop on a HUMAN item `size_signal.is_size_item`
    # recognises, which is what should happen to a bundle behaving oversized. Tagged IMPL
    # it would instead count as a reason to rebuild, turning the backstop into an
    # accelerator for the very failure it exists to stop.
    # `current`, not `read`: the recorded file wins, but its ABSENCE must not read as
    # "measured and small". A failed write would otherwise delete the backstop.
    size_reasons = size_signal.oversize_reasons(size_signal.current(d, cfg), cfg)
    if size_reasons:
        items += [NeedsHumanItem(size_signal.needs_human_text(size_reasons), HUMAN)]
    return items


def _plan_advisory_benefit(d: Path) -> dict | None:
    """The bundle's plan-advisory benefit record (#301), or None if absent/unreadable —
    the same tolerant contract as every other bundle-file read (testbed #3)."""
    p = d / "plan-advisory-benefit.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def assemble_summary(d: Path, cfg: Config) -> None:
    fields = brief.parse_fields(d / "brief.md")
    gates = json.loads((d / "check-gates.json").read_text(encoding="utf-8"))
    review_path = d / "check-review.md"
    # The review is advisory; a missing one (e.g. the reviewer's model connection
    # dropped mid-run) must not crash this deterministic step. Fall back to a
    # placeholder that routes a blocking item into §6 — so the bundle still assembles
    # and reaches sign-off, but can't be accepted until a real review exists.
    review_text = (
        review_path.read_text(encoding="utf-8")
        if review_path.exists()
        else _missing_review_text(d)
    )
    # Optional advisory reviewers (issue #64): each check-advisory-<id>.md is folded into
    # §5 and its NEEDS-HUMAN findings into §6, exactly like the main reviewer.
    advisory_paths = sorted(d.glob("check-advisory-*.md"))
    advisory_texts = [p.read_text(encoding="utf-8") for p in advisory_paths]

    # §6 is fed by the reviewer's NEEDS-HUMAN verdicts, the advisory reviewers', any gate
    # that declared itself unverifiable (issue #46), any gating gate that hard-FAILED
    # (issue #166), a builder-declared external dependency Plan didn't list (#250), and a
    # declared dependency with no registered doctor row (#263) — all become `- [ ]` items
    # the C6 guard makes the human clear before accept. `collect_needs_human` is the single
    # source (it also tags each item IMPL/HUMAN for the auto-iterate decision, #264).
    needs_human = [it.text for it in collect_needs_human(d, cfg)]
    # …plus every HUMAN finding an auto-iterate round deferred (#409), which lives only in
    # the ledger once its round's SUMMARY is archived.
    needs_human += _deferred_needs_human(d, needs_human)

    advisory_block = "\n".join(
        f"\n### Advisory — {p.stem.removeprefix('check-advisory-')}\n\n{t.strip()}"
        for p, t in zip(advisory_paths, advisory_texts)
    )

    issue = d.name.replace("issue_", "")
    # §1-8 render the brief's spec fields for a human (and the C6 accept-guard) to judge
    # against, so they must carry the WHOLE value — `parse_fields` is line-based and cut
    # every wrapped field at its first line (#336). `fields` stays for everything that
    # genuinely wants one line.
    spec = functools.partial(brief.whole_field, d / "brief.md")
    out = "\n".join(
        [
            f"# Result — issue {issue} / {_one_line(spec('slug') or fields.get('defect', '')[:40])}",
            "",
            "## 1. Spec (from brief.md)              ← Check verifies against THIS",
            f"- Defect / goal: {_item(spec('defect', 'goal'))}",
            f"- Success criterion: {_item(spec('success criterion'))}",
            f"- Repo + branch target: {_item(spec('repo + branch target', 'branch target'))}",
            f"- Scope (one logical fix) / out of scope: {_item(spec('scope'))}",
            "",
            "## 2. Disposition claimed               ← sign-off confirms or overrides",
            f"- Outcome: {_item(spec('disposition hint', default='Fixed'))}",
            "- Confidence: medium",
            "- Recommendation: (set by Do)",
            "",
            "## 3. Correctness (Check — chain)",
            _gate_lines(gates, prefix="C"),
            "",
            "## 4. Conformance (Check — stack)",
            _gate_lines(gates, prefix="T"),
            "- T5 judgment: → see §5.",
            "",
            "## 5. Advisory review (artifact-only, decorrelated)",
            "Reviewer ran without build-notes.md. Summary:",
            "",
            review_text.strip(),
            advisory_block,
            "",
            "## 6. NEEDS-HUMAN — items the human must clear before sign-off",
            _needs_human_block(needs_human),
            "",
            "## 7. Proven / not proven",
            f"- Proven by which oracle: gates overall = {gates['overall']} (stub oracles).",
            "- Unproven / needs manual run: anything flagged in §6.",
            "",
            "## 8. Ready-to-ship attachments",
            "- patch.diff",
            "- tracker-comment.md     (ALWAYS, every tracker item)",
            "- build-notes.md         (builder rationale — for the human, not the reviewer)",
            "",
            "## 9. Check sign-off                     ← human completes Check here",
            "- Disposition confirmed / overridden:",
            "- Outcome:",
            "- Iteration delta (if iterating):",
            "- By / date:",
            "",
            "## 10. Act candidates (hints for the next Act review)",
            *_plan_advisory_act_lines(d),
            "- (empty is the common case)",
            "",
        ]
    )
    (d / "SUMMARY.md").write_text(out, encoding="utf-8")


def _deferred_needs_human(d: Path, fresh: list[str]) -> list[str]:
    """The deferred-findings ledger's entries not already among this Check's §6 items (#409).

    Deduplicated on the normalised text, so a finding the reviewer raised again this round
    renders once. Never deduplicated fuzzily: a near-twin of a fresh finding is a different
    finding until the human says otherwise, and hiding it behind its neighbour would drop it.
    The normalisation is ``autoiterate._norm``, the one ``retire_cleared`` rebuilds this list
    with — were the two to differ, a re-raised finding would render twice, and a tick on it
    would match two rows and retire nothing.

    A ledger that exists but cannot be read becomes one fixed §6 row
    (``autoiterate.UNREADABLE_LEDGER_ITEM``), which blocks accept until the human clears it.
    Local import: ``autoiterate`` imports this module at its top level.
    """
    from . import autoiterate

    try:
        ledger = autoiterate.deferred(d)
    except autoiterate.DeferredLedgerUnreadable:
        return [autoiterate.UNREADABLE_LEDGER_ITEM]
    seen = {autoiterate._norm(t) for t in fresh}
    out: list[str] = []
    for text in ledger:
        key = autoiterate._norm(text)
        if key and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def _plan_advisory_act_lines(d: Path) -> list[str]:
    """§10 line for the plan-advisory benefit record (#301): benefit telemetry is process
    signal — exactly what Act reviews to judge whether plan reviews pay off over cycles."""
    benefit = _plan_advisory_benefit(d)
    if not benefit:
        return []
    counts = (f"{benefit.get('findings', 0)} finding(s); brief revised: "
              f"{'yes' if benefit.get('revised') else 'no'}")
    missing = benefit.get("not_completed")
    if benefit.get("completed") is False and isinstance(missing, dict) and missing:
        # #526: a review that never completed must not read as "ran, found nothing" — its
        # counts measure the run, not the brief. The status leads the line because
        # `act` groups §10 lines by their first words: a recurring environment fault then
        # collects under one key instead of hiding among real zero-finding reviews.
        statuses = ", ".join(sorted({str(s) for s in missing.values()}))
        why = "; ".join(f"{leaf} — {_LEAF_STATUS_LABEL.get(str(s), str(s))}"
                        for leaf, s in missing.items())
        return [f"- Plan advisory NOT completed ({statuses}): {why}. Its counts ({counts}) "
                "say nothing about the brief (plan-advisory-*.md)"]
    return [f"- Plan advisory: {counts} (plan-advisory-*.md)"]


def _gate_lines(gates: dict, *, prefix: str) -> str:
    lines = []
    for r in gates["rows"]:
        if r["check"].startswith(prefix):
            ev = r["path_line"] or r["oracle"]
            lines.append(f"- {r['check']}: {r['result']} — {ev}")
    return "\n".join(lines)


def _unverifiable_items(gates: dict) -> list[str]:
    """Gate rows the mechanic couldn't run (``result == "unverifiable"``) → §6 items, so
    the C6 accept-guard forces the human to clear them before accept (issue #46).

    ``unverifiable`` ONLY — a ``deferred`` row (issue #401) is deliberately NOT lifted, the
    single difference between the two gate-declared, non-gating results. ``unverifiable``
    means "nobody has an answer, so a human must decide"; ``deferred`` means "this row's
    substantive audit runs later, at a gate that cannot be skipped"
    (``gates._deferrable`` → ``publish.publish_gates``) — there is nothing for the human to
    clear. Lifting it anyway is the defect this closes: the Check-time T4 contribution row is
    default-open by design (its artifacts are drafted at publish), and its vacuous green fired
    a §6 NEEDS-HUMAN on 9 of 9 frozen bundles, cleared unread every time — which trains the
    human to tick §6 boxes, the very guard C6 depends on. The row stays visible in §5
    evidence (:func:`_gate_lines`) with its reason, so what is owed at publish is still read.
    """
    return [
        f"{r['check']} unverifiable — {r['path_line'] or r['oracle'] or 'no reason given'}"
        for r in gates["rows"]
        if r.get("result") == "unverifiable"
    ]


def _flaky_items(gates: dict) -> list[str]:
    """Gate rows recorded ``pass`` only after a confirm re-run (truthy ``flaky``, issue
    #371) → §6 items naming the check and both outcomes.

    ``gates._run_one`` re-runs a failed gating row once at Check; a fail→pass records
    ``pass`` so one transient red no longer parks the bundle. A pass that needed a second
    sample is still not a clean green: lifting it here makes C6 hold accept until the human
    has read the red sample (its output is in ``gate-logs/<rule_id>.log``)."""
    out = []
    for r in gates["rows"]:
        if not r.get("flaky"):
            continue
        # The samples exactly as the recorder wrote them — never a made-up history.
        attempts = [str(a) for a in r.get("attempts") or []]
        if len(attempts) >= 2:
            runs = (f"first run {attempts[0]}, confirm re-run {attempts[-1]} "
                    f"(attempts: {' → '.join(attempts)})")
        else:
            runs = "attempts not recorded"
        where = f" (both runs in {r['log']})" if r.get("log") else ""
        out.append(
            f"{r['check']} FLAKY — {runs}; recorded {r.get('result')}{where}. "
            "Confirm the red sample was environmental, not the patch — "
            f"{r['path_line'] or r['oracle'] or 'no evidence line'}")
    return out


def _failed_gating_items(gates: dict) -> list[NeedsHumanItem]:
    """A **gating** gate that returned a hard FAIL → a §6 NEEDS-HUMAN item (issue #166).

    Without this, only ``unverifiable`` rows reached §6; a gating ``fail`` set
    ``overall = fail`` and showed in §5 but added no §6 item — and the C6 accept-guard
    (:func:`signoff.open_needs_human`) only blocks on open §6 ``- [ ]`` items, so a red
    gating gate could be signed off to COMPLETE. Routing it here forces the human to clear
    it (accept with override, iterate, or discontinue) before sign-off.

    The kind comes from the row's structured ``element`` (issue #264), never from parsing
    its label — an instance names its own gates, so the label may not start with the id.
    A blank / unrecognised element is HUMAN (fail safe).
    """
    return [
        NeedsHumanItem(
            f"{r['check']} FAILED (gating) — {r['path_line'] or r['oracle'] or 'no reason given'}",
            IMPL if r.get("element") in _GATE_ELEMENTS else HUMAN,
        )
        for r in gates["rows"]
        if r.get("gating") and r.get("result") == "fail"
    ]


def _missing_review_text(d: Path) -> str:
    """Placeholder when ``check-review.md`` is absent — flags a §6 NEEDS-HUMAN so the
    bundle assembles and reaches sign-off but cannot be accepted without a review.

    Two wordings (#369), split on ``state.leaf_ran_and_failed`` — the engine's failed-leaf
    discriminator (#138: a reviewer that ran and SPENT its attempts left a settled
    ``state.REVIEW_ERROR_LOG``; a successful run removed any stale one). Without the
    split, a reviewer that NEVER RAN (the beat died between the gate write and the
    leaf) read exactly like one that ran and failed, and the record could not
    distinguish "not yet run" from "ran and yielded nothing". The second wording names
    BOTH unfinished shapes (#540): no log at all, and an unsettled log left by a death
    inside the leaf's retry loop — the discriminator treats them alike, so the prose must
    not assert only one of them.
    """
    if state.leaf_ran_and_failed(d / state.REVIEW_ERROR_LOG):
        return (
            "# Advisory review MISSING — the reviewer RAN AND FAILED\n\n"
            "- NEEDS-HUMAN — no check-review.md was produced: the reviewer leaf ran "
            f"and FAILED (see `{state.REVIEW_ERROR_LOG}` in this bundle for the "
            "captured error). Fix the cause, then re-run the Check reviewer before "
            "accepting.\n"
        )
    return (
        "# Advisory review MISSING — the reviewer NEVER RAN or was INTERRUPTED\n\n"
        "- NEEDS-HUMAN — no check-review.md was produced and no *settled* "
        f"`{state.REVIEW_ERROR_LOG}`: either the reviewer leaf NEVER RAN (the Check "
        "beat was interrupted before it), or it was interrupted INSIDE its retry loop "
        f"and the `{state.REVIEW_ERROR_LOG}` in this bundle is the unfinished account of "
        "the attempts it had made by then (#540) — read it for the post-mortem. Neither "
        "shape is a leaf that ran and exhausted its attempts. The driver recovers both on "
        "the next `advance` (#369); if this text persists, re-run the Check reviewer "
        "before accepting.\n"
    )


def _needs_human(review_text: str) -> list[tuple[str, bool]]:
    """Every reviewer NEEDS-HUMAN → ``(text, from_table)``, order-preserving and deduped.

    The reviewer always emits the 5/5/1 verdict table (see leaves._REVIEW_PROMPT);
    a table row whose verdict cell is NEEDS-HUMAN becomes a §6 item (Item — Basis).
    Legacy ``- NEEDS-HUMAN — …`` bullet lines are still honoured, continuation lines
    included (issue #527): a bullet's text is its first line plus every following line
    indented deeper than the bullet, space-joined into one line (never a newline — a
    second physical line would lose its §6 checkbox), until a blank line, a line no
    deeper than the bullet, a new list item at any indent, a heading, a table row, or a
    code fence ends it (see :func:`_ends_needs_human_continuation`).

    The second element says whether the item IS the canonical standing row — the one the prompt
    hard-codes every cycle. It demands an **exact** match on the row's *Item cell* against the
    5/5/1's own label, which is the only thing that identifies the template row:

    * a **legacy bullet** never qualifies — it is free prose the reviewer chose to write, so
      "Validation — fitness-to-purpose: patches the wrong layer" is a real objection.
    * nor does a row in some **other table** the reviewer happened to add (a "concerns" table),
      for the same reason. Keying on "came from a table" was still too wide, and keying on the
      text's *prefix* let a real objection wear the template's clothes (PR #294 review).

    Everything else keeps its signal.

    The Item cell is compared after :func:`_normalized_item_label` (#408), so the three forms
    reviewers actually write the V row in — bare, behind its `V —` id, with `--` — are all the
    constant row; the comparison itself stays exact. In the mandated table the Verdict and
    Basis cells are found by the table's header (:func:`_needs_human_rows`).
    """
    return [(row.text, row.standing) for row in _needs_human_rows(review_text)]


class _Row(NamedTuple):
    """One item as :func:`_needs_human_rows` reads it (#408)."""

    text: str
    standing: bool   # the mandated table's Validation row — see :func:`_needs_human`
    impl: bool       # a mandated-table row whose Verdict cell is `NEEDS-HUMAN [impl]`
    other: bool      # a mandated-table row whose Verdict cell holds another verdict


def _needs_human_rows(review_text: str) -> list[_Row]:
    """:func:`_needs_human`, plus what each row of the MANDATED verdict table says in its
    Verdict cell (#408).

    In that table the Verdict and Basis columns are found by the table's header
    (:func:`_table_header`), so a reviewer who orders the columns differently is still read
    by its own columns; with no `Basis` header, the Basis is the cell after the Verdict.
    ``impl`` — the Verdict cell IS `NEEDS-HUMAN [impl]` (the whole cell, emphasis aside), the
    reviewer stating a rebuild can fix it, on a row whose Item cell is a 5/5/1 label (after
    :func:`_normalized_item_label`). Only that cell carries the tag, and only as the verdict
    itself: a Basis that QUOTES `NEEDS-HUMAN [impl]` promotes nothing, and neither does a
    Verdict cell that quotes it under another verdict (`_VERDICT_IMPL_RE`).
    ``other`` — the Verdict cell holds another verdict (PASS / FAIL / N/A) while another
    cell mentions NEEDS-HUMAN. That row contradicts itself, so it still reaches the human,
    but never as IMPL and never as STANDING (#408 sign-off).

    A table whose header names no Verdict column, a row that leaves that cell empty, any
    other table, and a bullet are read as before: the first cell mentioning NEEDS-HUMAN is
    the verdict, and no tag is read from it (a bullet's tag stays in its text).

    Rows that read as the same text are one item, and it keeps the fail-safe reading of
    its copies: ``standing`` and ``impl`` only if EVERY copy has it, ``other`` if ANY copy
    has it — so no copy can hide behind another, in either order."""
    items: list[_Row] = []
    seen: dict[str, int] = {}   # lowercased text → its index in `items`
    standing_rows = 0           # counted BEFORE the dedup in `add`, for the fail-closed guard
    lines = review_text.splitlines()
    verdict_table = _verdict_table_lines(lines)

    def add(text: str, *, standing: bool, impl: bool = False, other: bool = False) -> None:
        text = text.strip()
        if not text:
            return
        k = seen.get(text.lower())
        if k is None:
            seen[text.lower()] = len(items)
            items.append(_Row(text, standing, impl, other))
        else:
            # Two rows on one finding that disagree fail safe to HUMAN, as two STANDING rows
            # grant neither, whichever copy comes first. A copy that is not the standing row
            # withdraws STANDING: it is a real objection — a "## Concerns" row, a bullet —
            # and once the V row's `V —` prefix is normalised away it can carry that row's
            # exact text (#408 review). A copy that does not state `[impl]` withdraws it, and
            # a copy whose verdict is not NEEDS-HUMAN marks the item `other`.
            items[k] = items[k]._replace(standing=items[k].standing and standing,
                                         impl=items[k].impl and impl,
                                         other=items[k].other or other)

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        s = line.strip()
        if s.startswith("- NEEDS-HUMAN"):
            bullet_indent = len(line) - len(line.lstrip())
            parts = [s[len("- NEEDS-HUMAN"):].lstrip(" —:-").strip()]
            j = i + 1
            while j < n:
                cont = lines[j]
                cont_s = cont.strip()
                if not cont_s:
                    break  # blank line ends the item (out of scope: multi-paragraph bullets)
                cont_indent = len(cont) - len(cont.lstrip())
                if cont_indent <= bullet_indent:
                    break  # not indented deeper than the bullet
                if _ends_needs_human_continuation(cont):
                    break  # a new list item, heading, table row, or code fence
                parts.append(cont_s)
                j += 1
            add(" ".join(p for p in parts if p), standing=False)
            i = j
            continue
        elif s.startswith("|") and "needs-human" in s.lower():
            cells = [c.strip() for c in s.strip("|").split("|")]
            in_table = i in verdict_table
            header = _table_header(lines, i) if in_table else []
            col = header.index("verdict") if "verdict" in header else None
            if col is not None and (col >= len(cells) or not cells[col]):
                col = None   # the row writes no verdict in that column: read it as before
            if col is None:
                vi = next((k for k, c in enumerate(cells) if "needs-human" in c.lower()), None)
            else:
                vi = col
            if vi is not None:
                label = cells[0] if cells else ""
                # A canonical label in the verdict table is rendered without its id prefix and
                # with `—`, so the bare, prefixed and `--` forms of one row read the same in
                # §6. Any other table's cell is kept as written: normalised there, a copy of
                # the V row in a "## Concerns" table could merge into the standing row.
                norm = _normalized_item_label(label)
                if in_table and norm.casefold() in _CANONICAL_LABELS:
                    label = norm
                bi = header.index("basis") if "basis" in header else vi + 1
                basis = cells[bi] if bi < len(cells) else ""
                other = "needs-human" not in cells[vi].lower()
                is_v = in_table and norm.casefold() == _V_LABEL.casefold()
                if is_v:
                    standing_rows += 1
                # The tag is read only on a row whose Item cell IS a 5/5/1 label, so the element
                # it would promote is not in doubt: `C5 — Validation — fitness-to-purpose`
                # names two, and a label with words added is not the label.
                impl = (col is not None and norm.casefold() in _CANONICAL_LABELS
                        and bool(_VERDICT_IMPL_RE.fullmatch(cells[vi])))
                add(f"{label} — {basis}" if basis else label, standing=is_v and not other,
                    impl=impl, other=other)
        i += 1

    # FAIL CLOSED on ambiguity. The template row is a CONSTANT — it occurs exactly once. If two
    # survive (a second verdict-shaped table, a duplicated row), at least one of them is not the
    # constant, and we cannot tell which. Grant STANDING to neither, so the bundle halts for the
    # human rather than risk archiving a real objection. Rows are counted before `add` dedups
    # them: two V rows with the same Basis — written in two forms, or copied — read as ONE
    # item, and counting items would grant that one STANDING (#408 review). A V row whose
    # Verdict cell contradicts it counts too: it is a second V row all the same.
    if standing_rows > 1:
        return [row._replace(standing=False) for row in items]
    return items


def _verdict_table_lines(lines: list[str]) -> set[int]:
    """Line indices belonging to the reviewer's MANDATED 5/5/1 verdict table.

    The whole basis for STANDING is that *that* table's Validation row is a constant the prompt
    emits every cycle. So the parser has to know which table a row came from — and it did not.
    Matching the Item cell alone let a "## Concerns" table carrying the **exact** canonical label
    earn the exemption, and an unattended rebuild would archive that real objection (PR #294
    review, local pass). Keying on the row was the fourth scoping of this same rule; the table is
    what the justification was always about.

    A contiguous run of ``|``-rows is the verdict table when **two or more** of its Item cells
    exactly match canonical 5/5/1 labels — the mandated table carries all eleven, while an
    ad-hoc concerns table carries its own prose. Two, not one, so a lone Validation row in a
    stray table cannot nominate itself.
    """
    out: set[int] = set()
    i = 0
    while i < len(lines):
        if not lines[i].strip().startswith("|"):
            i += 1
            continue
        j = i
        while j < len(lines) and lines[j].strip().startswith("|"):
            j += 1
        block = range(i, j)
        labels = {_normalized_item_label(lines[k].strip().strip("|").split("|")[0]).casefold()
                  for k in block}
        if len(labels & _CANONICAL_LABELS) >= 2:
            out.update(block)
        i = j
    return out


def _table_header(lines: list[str], i: int) -> list[str]:
    """The header cells of the ``|``-table holding line ``i`` — its first row — stripped of
    emphasis and casefolded, so a caller can find a column by name (#408).

    The Verdict and Basis columns are looked up here rather than assumed to be the mandated
    table's second and third, so a reviewer who orders the columns differently is still read
    by its own columns: fixed indexes would read a reordered table's Basis as the verdict,
    miss the real one, and lose the Basis a rebuild needs.
    """
    while i > 0 and lines[i - 1].strip().startswith("|"):
        i -= 1
    return [c.strip().strip("*_`").strip().casefold()
            for c in lines[i].strip().strip("|").split("|")]


def _declared_external_deps(build_notes_text: str) -> list[str]:
    """Builder-declared external dependencies (#250) → §6 items.

    ``build-notes.md`` is withheld from the reviewer (the independence contract) and is not
    otherwise read into ``SUMMARY.md``, so an external dependency Do hit that Plan didn't
    list — and that no gate happens to cover (a stub or unrelated-gate config) — would never
    reach the human. The builder marks each with a line
    ``NEEDS-HUMAN external dependency: <dep> — <what it blocks>`` (see agents/builder.md);
    this lifts them into §6 deterministically, independent of the reviewer and the gate set.
    Match is bullet- and case-insensitive; the remainder after the marker becomes the item.
    """
    items: list[str] = []
    seen: set[str] = set()
    for line in build_notes_text.splitlines():
        s = line.strip().lstrip("-*").strip()
        low = s.lower()
        if low.startswith("needs-human") and "external dependency" in low:
            item = s[len("needs-human"):].lstrip(" —:-").strip()
            if item and item.lower() not in seen:
                seen.add(item.lower())
                items.append(item)
    return items


def _unregistered_dependency_items(brief_path: Path, cfg: Config) -> list[str]:
    """The Check-time BACKSTOP for #263, delegating to the one implementation (#333).

    #333 moved the primary check to Plan exit, before Do dispatches. This stays, and is
    not redundant: ``pdca.toml`` can gain or lose rows mid-cycle, which is exactly why the
    reconciliation reads the file as it stands now rather than from the run's opening
    snapshot (PR #269 review). A row deleted after Plan passed is still caught here.
    """
    return doctor.unregistered_dependencies(brief_path, cfg)


def _needs_human_block(items: list[str]) -> str:
    if not items:
        return "- (none — every model-attempted item came back PASS, no always-human item applied)"
    return "\n".join(f"- [ ] {it}" for it in items)
