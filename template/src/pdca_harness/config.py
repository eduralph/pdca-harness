"""Project configuration for the PDCA driver.

The driver itself is project-agnostic; everything repo-specific is read from
``pdca.toml`` at the project root (the integration, docs 05). Parsed with the
stdlib ``tomllib`` so the harness has no runtime dependencies.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


# ----------------------------------------------------------------------------
#
# LeafConfig
#
# ----------------------------------------------------------------------------
@dataclass
class LeafConfig:
    """How one model leaf (planner, Do builder, Check reviewer, sign-off, Act) runs.

    ``mode == "stub"`` runs the offline placeholder (the vertical slice default);
    ``mode == "command"`` runs ``argv`` as a subprocess in the bundle directory.
    ``interactive`` hands the terminal to the human (a seeded REPL, no ``-p``); a
    headless leaf (``interactive == False``) runs autonomously and writes a doc.
    """

    mode: str = "stub"
    family: str = ""
    argv: list[str] = field(default_factory=list)
    interactive: bool = False


# ----------------------------------------------------------------------------
#
# Config
#
# ----------------------------------------------------------------------------
@dataclass
class Config:
    root: Path
    bundle_root: Path
    process_dir: Path
    templates_dir: Path
    default_branch: str
    tracker_system: str
    tracker_url: str
    issue_id_example: str
    builder: LeafConfig
    reviewer: LeafConfig
    planner: LeafConfig = field(default_factory=LeafConfig)
    signoff: LeafConfig = field(default_factory=LeafConfig)
    publisher: LeafConfig = field(default_factory=LeafConfig)
    act: LeafConfig = field(default_factory=LeafConfig)
    author: str = ""  # default §9 sign-off attribution (the maintainer)
    tracker_export_csv: str = ""  # default tracker CSV the planner reads the issue row from
    # Publish mechanics — config-driven so the harness ships project-agnostic.
    # Branch patterns are .format(id=, slug=) strings; issue_trailer is .format(id=).
    fix_branch_pattern: str = "fix/{id}-{slug}"
    feature_branch_pattern: str = "enhancement/{id}-{slug}"
    issue_trailer: str = "Fixes #{id}"  # commit/PR trailer; "" → none enforced
    repo_checkouts: dict[str, str] = field(default_factory=dict)  # repo_spec → local path
    gates_checks: list[dict] = field(default_factory=list)
    # Target-aware gate selection (docs 04). A check may carry ``target`` (a label or
    # list); it runs iff its labels are a SUBSET of the bundle's label set. The bundle is
    # classified from its brief on two axes: a PRIMARY one (``gate_target_match``: label →
    # substring vs the "Repo + branch target" field; ``gate_target_default`` when none
    # match — mutually exclusive, e.g. core vs addon) plus additive FLAGS
    # (``gate_target_flags``: label → {field, substring} vs any brief field, e.g.
    # ``frontend`` ← a "Surfaces" field). All empty ⇒ no filtering (every gate runs).
    gate_target_default: str = ""
    gate_target_match: dict[str, str] = field(default_factory=dict)
    gate_target_flags: dict[str, dict[str, str]] = field(default_factory=dict)

    def bundle(self, issue_id: str) -> Path:
        """The per-cycle bundle directory for an issue id."""
        return self.bundle_root / f"issue_{issue_id}"

    @classmethod
    def load(cls, root: Path | None = None) -> "Config":
        """Load ``pdca.toml`` from ``root`` (or the nearest ancestor that has one)."""
        root = _find_root(root or Path.cwd())
        data = tomllib.loads((root / "pdca.toml").read_text(encoding="utf-8"))

        paths = data.get("paths", {})
        tracker = data.get("tracker", {})
        publisher_cfg = data.get("publisher", {})
        leaves = data.get("leaves", {})
        gates = data.get("gates", {})
        gates_checks = list(gates.get("checks", []))
        # Additive target flags: label → {field, substring}. A bare string is shorthand
        # for the "Repo + branch target" field (so flags and the primary axis can share it).
        gate_target_flags = {
            label: (rule if isinstance(rule, dict)
                    else {"field": "repo + branch target", "substring": rule})
            for label, rule in gates.get("target_flags", {}).items()
        }
        # PDCA_GATES_MODE=stub empties the configured checks → the all-PASS stub
        # rows, so an offline "rehearse" runs the control flow without Docker.
        if os.environ.get("PDCA_GATES_MODE") == "stub":
            gates_checks = []

        # PDCA_LEAVES_MODE forces every leaf's mode regardless of pdca.toml — so
        # CI and the offline self-test (`make`) stay deterministic (=stub, no
        # Claude/TTY) even when the shipped config wires the leaves to "command".
        mode_override = os.environ.get("PDCA_LEAVES_MODE") or None

        def leaf(name: str) -> LeafConfig:
            d = leaves.get(name, {})
            return LeafConfig(
                mode=mode_override or d.get("mode", "stub"),
                family=d.get("family", ""),
                argv=list(d.get("argv", [])),
                interactive=bool(d.get("interactive", False)),
            )

        # PDCA_BUNDLE_ROOT redirects bundles to a throwaway location so an offline
        # `rehearse` never collides with the real `results/` a live run would use.
        bundle_root = root / paths.get("bundle_root", "results")
        if os.environ.get("PDCA_BUNDLE_ROOT"):
            env_root = Path(os.environ["PDCA_BUNDLE_ROOT"])
            bundle_root = env_root if env_root.is_absolute() else root / env_root

        return cls(
            root=root,
            bundle_root=bundle_root,
            process_dir=root / paths.get("process_dir", "process"),
            templates_dir=root / paths.get("templates_dir", "templates"),
            default_branch=data.get("project", {}).get("default_branch", "main"),
            tracker_system=tracker.get("system", ""),
            tracker_url=tracker.get("url", ""),
            issue_id_example=tracker.get("issue_id_example", ""),
            tracker_export_csv=tracker.get("export_csv", ""),
            fix_branch_pattern=publisher_cfg.get("fix_branch_pattern", "fix/{id}-{slug}"),
            feature_branch_pattern=publisher_cfg.get("feature_branch_pattern", "enhancement/{id}-{slug}"),
            issue_trailer=tracker.get("issue_trailer", "Fixes #{id}"),
            repo_checkouts=dict(publisher_cfg.get("checkouts", {})),
            gate_target_default=gates.get("target_default", ""),
            gate_target_match=dict(gates.get("target_match", {})),
            gate_target_flags=gate_target_flags,
            builder=leaf("builder"),
            reviewer=leaf("reviewer"),
            planner=leaf("planner"),
            signoff=leaf("signoff"),
            publisher=leaf("publisher"),
            act=leaf("act"),
            author=data.get("project", {}).get("author", ""),
            gates_checks=gates_checks,
        )


def _find_root(start: Path) -> Path:
    """Walk up from ``start`` to the directory containing ``pdca.toml``."""
    start = start.resolve()
    for d in (start, *start.parents):
        if (d / "pdca.toml").exists():
            return d
    raise FileNotFoundError(
        f"no pdca.toml found at or above {start} — run inside a rendered project"
    )
