"""Assemble ``SUMMARY.md`` from brief + gates + review (docs 02 §SUMMARY.md).

Pure code, no model: the driver assembles §1–8 from the brief, the gate JSON, and
the reviewer's findings, routes every reviewer ``NEEDS-HUMAN`` into §6, and leaves
§9 (sign-off) and §10 (Act candidates) empty for the human. The section shape
mirrors ``templates/SUMMARY.md.tpl`` — keep the two in step if you edit either.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import brief
from .config import Config


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
    # §6 is fed by the reviewer's NEEDS-HUMAN verdicts AND any gate that declared itself
    # unverifiable (issue #46) — both become `- [ ]` items the C6 guard makes the human
    # clear before accept.
    needs_human = _needs_human(review_text) + _unverifiable_items(gates)

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


def _needs_human_block(items: list[str]) -> str:
    if not items:
        return "- (none — every model-attempted item came back PASS, no always-human item applied)"
    return "\n".join(f"- [ ] {it}" for it in items)
