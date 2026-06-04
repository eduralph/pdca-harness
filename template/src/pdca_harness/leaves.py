"""The two model leaves: Do (builder) and Check's reviewer (docs 03 §leaves).

These are the *only* points where a model is invoked; the rest of the pipeline is
deterministic code. Two invariants live here and matter more than the stub
content:

1. **Independence is a missing input.** ``run_review`` builds its input list
   *without* ``build-notes.md`` — the builder's framing cannot anchor the
   reviewer because the reviewer never receives the file (docs 02 §Independence
   contract). This is enforced by what we don't pass, not by prompt wording.
2. **The builder cannot mark a PR ready.** In ``command`` mode that constraint is
   a subagent tool-scope concern (docs 03 §Do); the stub simply never does it.

``mode == "stub"`` writes offline placeholders so the slice runs without a model.
``mode == "command"`` runs the configured argv as a subprocess in the bundle dir.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import brief
from .config import Config

# build-notes.md is DELIBERATELY ABSENT from this list (independence contract).
REVIEWER_INPUTS = ["patch.diff", "brief.md", "check-gates.json"]


# ----------------------------------------------------------------------------
# Leaf 1 — Do (builder): writes patch.diff + the test + build-notes.md.
# ----------------------------------------------------------------------------
def do_build(d: Path, cfg: Config) -> None:
    if cfg.builder.mode == "command":
        _run(cfg.builder.argv + ["--input", str(d / "brief.md")], cwd=d)
        return
    _stub_build(d, cfg)


def _stub_build(d: Path, cfg: Config) -> None:
    test_rel = (brief.test_files(d / "brief.md") or [Path("test_stub.py")])[0]
    test_path = d / test_rel
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text(
        "# Stub regression test shipped by the Do leaf (vertical slice).\n"
        "def test_placeholder():\n    assert True\n",
        encoding="utf-8",
    )
    (d / "patch.diff").write_text(
        "# Stub patch produced by the Do leaf for the vertical slice.\n"
        "# A real builder writes a unified diff here.\n"
        f"# (the shipped test is {test_rel})\n",
        encoding="utf-8",
    )
    (d / "build-notes.md").write_text(
        "# Build notes (builder rationale — withheld from the reviewer)\n\n"
        "Stub Do leaf. A real builder records here why this change, what was\n"
        "tried, and what was ruled out. The reviewer never sees this file.\n",
        encoding="utf-8",
    )


# ----------------------------------------------------------------------------
# Leaf 2 — Check reviewer (cross-vendor, decorrelated, advisory): check-review.md.
# ----------------------------------------------------------------------------
def reviewer_input_paths(d: Path) -> list[Path]:
    """The exact files the reviewer receives — build-notes.md is not among them."""
    return [d / name for name in REVIEWER_INPUTS]


def run_review(d: Path, cfg: Config) -> None:
    inputs = reviewer_input_paths(d)
    assert (d / "build-notes.md") not in inputs, "independence contract violated"

    if cfg.reviewer.mode == "command":
        _run(cfg.reviewer.argv + [str(p) for p in inputs], cwd=d)
        return
    _stub_review(d, cfg)


def _stub_review(d: Path, cfg: Config) -> None:
    # Model-decidable items the reviewer attempts (all PASS in the stub) plus the
    # always-human items it flags NEEDS-HUMAN by design (docs 04 §judgment cell).
    (d / "check-review.md").write_text(
        "# Cross-vendor reviewer (advisory, artifact-only)\n\n"
        f"Reviewer family: {cfg.reviewer.family or 'stub'}. "
        "Inputs: patch.diff, brief.md, check-gates.json (build-notes.md withheld).\n\n"
        "## Per-item verdicts\n"
        "- PASS — re-ran asserted evidence: stub red→green confirmed.\n"
        "- PASS — every cited path:line grounds on the target branch.\n"
        "- PASS — diff stays within one logical fix (model-decidable).\n"
        "- NEEDS-HUMAN — validation fitness-to-purpose: is this the right thing "
        "at all? (always-human by design)\n",
        encoding="utf-8",
    )


def _run(argv: list[str], cwd: Path) -> None:
    subprocess.run(argv, cwd=cwd, check=True)
