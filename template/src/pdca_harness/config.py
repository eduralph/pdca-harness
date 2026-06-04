"""Project configuration for the PDCA driver.

The driver itself is project-agnostic; everything repo-specific is read from
``pdca.toml`` at the project root (the integration, docs 05). Parsed with the
stdlib ``tomllib`` so the harness has no runtime dependencies.
"""

from __future__ import annotations

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
    """How one of the two model leaves (Do builder, Check reviewer) is invoked.

    ``mode == "stub"`` runs the offline placeholder (the vertical slice default);
    ``mode == "command"`` runs ``argv`` as a subprocess in the bundle directory.
    """

    mode: str = "stub"
    family: str = ""
    argv: list[str] = field(default_factory=list)


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
        leaves = data.get("leaves", {})

        def leaf(name: str) -> LeafConfig:
            d = leaves.get(name, {})
            return LeafConfig(
                mode=d.get("mode", "stub"),
                family=d.get("family", ""),
                argv=list(d.get("argv", [])),
            )

        return cls(
            root=root,
            bundle_root=root / paths.get("bundle_root", "results"),
            process_dir=root / paths.get("process_dir", "process"),
            templates_dir=root / paths.get("templates_dir", "templates"),
            default_branch=data.get("project", {}).get("default_branch", "main"),
            tracker_system=tracker.get("system", ""),
            tracker_url=tracker.get("url", ""),
            issue_id_example=tracker.get("issue_id_example", ""),
            builder=leaf("builder"),
            reviewer=leaf("reviewer"),
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
