"""Assemble ``SUMMARY.md`` from brief + gates + review (docs 02 §SUMMARY.md).

Pure code, no model: the driver assembles §1–8 from the brief, the gate JSON, and
the reviewer's findings, routes every reviewer ``NEEDS-HUMAN`` into §6, and leaves
§9 (sign-off) and §10 (Act candidates) empty for the human. The section shape
mirrors ``templates/SUMMARY.md.tpl`` — keep the two in step if you edit either.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import NamedTuple

from . import brief, doctor
from .config import Config
from .gates import canonical_elements

# The two kinds of §6 item (issue #264).
#   IMPL  — an implementation defect the BUILDER can fix by iterating Do.
#   HUMAN — an architectural / fitness-to-purpose / environmental call only the human makes.
IMPL = "impl"
HUMAN = "human"


class NeedsHumanItem(NamedTuple):
    """One §6 row: the text the human reads, plus who can resolve it."""

    text: str
    kind: str


# The implementation/architectural split is NOT a new taxonomy — it is the `kind` already
# carried by the canonical 5/5/1 (gates._FIVE_FIVE_ONE). `gate` cells (C2/C4/T1..T4) are
# mechanically checkable ⇒ builder-fixable. `judgment` cells (C5 causal adequacy, T5
# judgment, V validation) and `input` cells (C1 spec, C3 change) are the human's.
_GATE_ELEMENTS = frozenset(e for e, _label, kind, _oracle in canonical_elements()
                           if kind == "gate")

# A §6 item's leading 5/5/1 element id, when the reviewer's table row carries one.
_ELEMENT_RE = re.compile(r"^(C[1-5]|T[1-5]|V)\b")

# An advisory leaf tags a builder-fixable finding `- NEEDS-HUMAN [impl] — …`. Unmarked
# findings stay HUMAN, so a legacy advisory file can never trigger an auto-iteration.
_IMPL_MARKER_RE = re.compile(r"^\[impl\]\s*[—:-]*\s*", re.IGNORECASE)

# Leaf-status marker (issue #278). When a reviewer / advisory leaf could not produce a
# verdict, `leaves` writes a placeholder carrying one of these as a machine-readable comment.
# An EMPTY advisory artifact is otherwise ambiguous: "the adversary ran and found nothing"
# reads identically to "the adversary never ran" — and an infra failure then presents as a
# clean adversarial pass. The status lets §6 say WHY the artifact is empty, and lets a
# consumer act on it (re-run vs adjudicate) instead of parsing prose.
# Both INFRA shapes mean "nothing reviewed the diff", but they call for different ACTIONS, so
# the §6 row must not conflate them: a transient blip is safe to re-run as-is, while a leaf
# whose command could never be launched will fail identically until that command is fixed —
# telling the operator "safe to re-run" there would be a false instruction (PR #285 review).
LEAF_STATUS_INFRA = "infra-empty"      # ran, died with no output — a transient blip
LEAF_STATUS_STARTUP = "startup-empty"  # never launched — binary absent / not executable
LEAF_STATUS_HUMAN = "human-empty"      # ran, but yielded no usable verdict
_LEAF_STATUS_RE = re.compile(r"<!--\s*pdca:leaf-status\s+(\S+)\s*-->")
_LEAF_STATUS_LABEL = {
    LEAF_STATUS_INFRA: "leaf did not run (transient infra — safe to re-run)",
    LEAF_STATUS_STARTUP: ("leaf did not run (its command could not be launched — fix the "
                          "leaf's config, then re-run)"),
    LEAF_STATUS_HUMAN: "leaf produced no usable verdict (needs a human)",
}


def leaf_status(artifact_text: str) -> str:
    """The leaf-status marker a reviewer/advisory placeholder carries, or "" for a real
    artifact (a leaf that actually produced findings) — issue #278."""
    m = _LEAF_STATUS_RE.search(artifact_text)
    return m.group(1) if m else ""


def _classify_finding(text: str) -> NeedsHumanItem:
    """Classify one reviewer / advisory §6 item, stripping any `[impl]` marker.

    Fail safe: an item we cannot map to a gate element — an unmarked advisory bullet, a
    reviewer row whose Item cell doesn't start with a canonical id, the missing-review
    placeholder — is HUMAN. Auto-iterate only ever fires on findings we positively know
    a rebuild can address.
    """
    stripped = _IMPL_MARKER_RE.sub("", text, count=1)
    if stripped != text:
        return NeedsHumanItem(stripped.strip(), IMPL)
    m = _ELEMENT_RE.match(text)
    if m and m.group(1) in _GATE_ELEMENTS:
        return NeedsHumanItem(text, IMPL)
    return NeedsHumanItem(text, HUMAN)


def _items_from_artifact(text: str) -> list[NeedsHumanItem]:
    """§6 items from one reviewer / advisory artifact, labelled by its leaf status (#278).

    A placeholder (the leaf could not produce a verdict) has its items prefixed with WHY the
    artifact is empty — infra vs substance — so the human doesn't have to hand-annotate it,
    and forced to HUMAN: there is no finding for a rebuild to fix, so an infra-empty must
    never be auto-iterated (#264). A real artifact is unaffected."""
    label = _LEAF_STATUS_LABEL.get(leaf_status(text), "")
    items = [_classify_finding(t) for t in _needs_human(text)]
    if not label:
        return items
    return [NeedsHumanItem(f"{label} — {it.text}", HUMAN) for it in items]


def collect_needs_human(d: Path, cfg: Config) -> list[NeedsHumanItem]:
    """Every §6 item for this bundle, tagged IMPL / HUMAN, in the order §6 renders them.

    Single source for both the rendered §6 and the auto-iterate decision (issue #264), so
    the classifier can never disagree with what the C6 accept-guard sees.
    """
    gates_json = json.loads((d / "check-gates.json").read_text(encoding="utf-8"))
    review_path = d / "check-review.md"
    review_text = (review_path.read_text(encoding="utf-8")
                   if review_path.exists() else _missing_review_text())
    advisory_texts = [p.read_text(encoding="utf-8")
                      for p in sorted(d.glob("check-advisory-*.md"))]

    items = _items_from_artifact(review_text)
    for atext in advisory_texts:
        items += _items_from_artifact(atext)
    # A gate that COULD NOT RUN is not builder-fixable — rebuilding would spin against the
    # same missing mechanic — so it is HUMAN regardless of its (gate-kind) element.
    items += [NeedsHumanItem(t, HUMAN) for t in _unverifiable_items(gates_json)]
    items += _failed_gating_items(gates_json)
    build_notes = d / "build-notes.md"
    if build_notes.exists():
        items += [NeedsHumanItem(t, HUMAN)
                  for t in _declared_external_deps(build_notes.read_text(encoding="utf-8"))]
    items += [NeedsHumanItem(t, HUMAN)
              for t in _unregistered_dependency_items(d / "brief.md", cfg)]
    return items


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
        else _missing_review_text()
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

    advisory_block = "\n".join(
        f"\n### Advisory — {p.stem.removeprefix('check-advisory-')}\n\n{t.strip()}"
        for p, t in zip(advisory_paths, advisory_texts)
    )

    issue = d.name.replace("issue_", "")
    out = "\n".join(
        [
            f"# Result — issue {issue} / {fields.get('slug', fields.get('defect', '')[:40])}",
            "",
            "## 1. Spec (from brief.md)              ← Check verifies against THIS",
            f"- Defect / goal: {fields.get('defect', fields.get('goal', ''))}",
            f"- Success criterion: {fields.get('success criterion', '')}",
            f"- Repo + branch target: {fields.get('repo + branch target', fields.get('branch target', ''))}",
            f"- Scope (one logical fix) / out of scope: {fields.get('scope', '')}",
            "",
            "## 2. Disposition claimed               ← sign-off confirms or overrides",
            f"- Outcome: {fields.get('disposition hint', 'Fixed')}",
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
            "- (empty is the common case)",
            "",
        ]
    )
    (d / "SUMMARY.md").write_text(out, encoding="utf-8")


def _gate_lines(gates: dict, *, prefix: str) -> str:
    lines = []
    for r in gates["rows"]:
        if r["check"].startswith(prefix):
            ev = r["path_line"] or r["oracle"]
            lines.append(f"- {r['check']}: {r['result']} — {ev}")
    return "\n".join(lines)


def _unverifiable_items(gates: dict) -> list[str]:
    """Gate rows the mechanic couldn't run (``result == "unverifiable"``) → §6 items, so
    the C6 accept-guard forces the human to clear them before accept (issue #46)."""
    return [
        f"{r['check']} unverifiable — {r['path_line'] or r['oracle'] or 'no reason given'}"
        for r in gates["rows"]
        if r.get("result") == "unverifiable"
    ]


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


def _missing_review_text() -> str:
    """Placeholder when ``check-review.md`` is absent — flags a §6 NEEDS-HUMAN so the
    bundle assembles and reaches sign-off but cannot be accepted without a review."""
    return (
        "# Advisory review MISSING\n\n"
        "- NEEDS-HUMAN — no check-review.md was produced (the reviewer leaf failed or "
        "its model connection dropped). Re-run the Check reviewer before accepting.\n"
    )


def _needs_human(review_text: str) -> list[str]:
    """Every reviewer NEEDS-HUMAN → a §6 item, order-preserving and deduped.

    The reviewer always emits the 5/5/1 verdict table (see leaves._REVIEW_PROMPT);
    a table row whose verdict cell is NEEDS-HUMAN becomes a §6 item (Item — Basis).
    Legacy ``- NEEDS-HUMAN — …`` bullet lines are still honoured.
    """
    items: list[str] = []
    seen: set[str] = set()

    def add(text: str) -> None:
        text = text.strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            items.append(text)

    for line in review_text.splitlines():
        s = line.strip()
        if s.startswith("- NEEDS-HUMAN"):
            add(s[len("- NEEDS-HUMAN"):].lstrip(" —:-").strip())
        elif s.startswith("|") and "needs-human" in s.lower():
            cells = [c.strip() for c in s.strip("|").split("|")]
            vi = next((i for i, c in enumerate(cells) if "needs-human" in c.lower()), None)
            if vi is None:
                continue
            label = cells[0] if cells else ""
            basis = cells[vi + 1] if vi + 1 < len(cells) else ""
            add(f"{label} — {basis}" if basis else label)
    return items


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
    """A brief-declared external dependency with no registered ``[[doctor.checks]]`` row (#263).

    The principle: when a change needs something a human must install or provide, the system
    must REGISTER it — a doctor row with a detect ``cmd`` and an install ``hint`` — rather
    than let it surface mid-cycle as a cryptic build failure. Registration has to be a
    forcing function, so an unregistered declaration becomes a §6 item and the C6 guard
    blocks accept until the row exists (or the human clears it as a false positive).

    This cannot be the reviewer's job: its sandbox holds only ``REVIEWER_INPUTS``, so it
    never sees ``pdca.toml`` and cannot know which rows are registered. Nor is it a judgment
    call — it is set membership — so the driver decides it deterministically here.

    :func:`doctor.registered_ids` owns what "registered" means: a row that would actually
    RUN (it has a detect ``cmd``), read from ``pdca.toml`` as it stands **now** rather than
    from the snapshot the run opened with — Plan and Do both add rows mid-cycle (PR #269
    review).
    """
    registered = doctor.registered_ids(cfg)
    return [
        f"external dependency `{token}` is declared in the brief but has no matching "
        f"[[doctor.checks]] row — register a detect cmd + install hint in pdca.toml, or "
        f"annotate it `(no-check: …)` if nothing can detect it"
        for token in brief.external_dependency_tokens(brief_path)
        if token.strip().lower() not in registered
    ]


def _needs_human_block(items: list[str]) -> str:
    if not items:
        return "- (none — every model-attempted item came back PASS, no always-human item applied)"
    return "\n".join(f"- [ ] {it}" for it in items)
