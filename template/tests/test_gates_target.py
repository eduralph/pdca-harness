"""Unit tests for target-aware gate selection (stdlib unittest, offline).

Covers the two pure helpers that decide *which* gates run:
``gates._bundle_target`` (classify a bundle from its brief) and ``gates._applies``
(does a check run for this scope set + bundle target). No subprocess, no Docker.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pdca_harness import gates


def _bundle_with_target(line: str) -> Path:
    d = Path(tempfile.mkdtemp())
    (d / "brief.md").write_text(
        f"- **Slug:** x\n- **Repo + branch target:** {line}\n", encoding="utf-8"
    )
    return d


MATCH = {"addon": "addons-source"}
DEFAULT = "core"


class BundleTarget(unittest.TestCase):
    def test_match_hits_label(self) -> None:
        b = _bundle_with_target("org/addons-source @ maintenance/gramps60")
        self.assertEqual(gates._bundle_target(b, MATCH, DEFAULT), "addon")

    def test_no_match_falls_to_default(self) -> None:
        b = _bundle_with_target("org/gramps @ maintenance/gramps61")
        self.assertEqual(gates._bundle_target(b, MATCH, DEFAULT), "core")

    def test_case_insensitive(self) -> None:
        b = _bundle_with_target("org/Addons-Source @ branch")
        self.assertEqual(gates._bundle_target(b, MATCH, DEFAULT), "addon")

    def test_no_bundle_is_none(self) -> None:  # CI working-tree re-gate
        self.assertIsNone(gates._bundle_target(None, MATCH, DEFAULT))

    def test_no_match_config_is_none(self) -> None:  # unconfigured project
        b = _bundle_with_target("org/gramps @ main")
        self.assertIsNone(gates._bundle_target(b, {}, ""))

    def test_no_default_returns_none(self) -> None:  # match config but no default
        b = _bundle_with_target("org/gramps @ main")
        self.assertIsNone(gates._bundle_target(b, MATCH, ""))


class Applies(unittest.TestCase):
    SCOPES = ("repo", "bundle")

    def test_untargeted_check_always_runs(self) -> None:
        chk = {"id": "c", "scope": "repo"}
        self.assertTrue(gates._applies(chk, self.SCOPES, "addon"))
        self.assertTrue(gates._applies(chk, self.SCOPES, "core"))
        self.assertTrue(gates._applies(chk, self.SCOPES, None))

    def test_matching_target_runs(self) -> None:
        chk = {"id": "c", "scope": "repo", "target": "addon"}
        self.assertTrue(gates._applies(chk, self.SCOPES, "addon"))

    def test_mismatched_target_skips(self) -> None:
        chk = {"id": "c", "scope": "repo", "target": "core"}
        self.assertFalse(gates._applies(chk, self.SCOPES, "addon"))

    def test_unknown_bundle_target_never_over_skips(self) -> None:
        chk = {"id": "c", "scope": "repo", "target": "core"}
        self.assertTrue(gates._applies(chk, self.SCOPES, None))

    def test_scope_filter_still_applies(self) -> None:
        chk = {"id": "c", "scope": "bundle", "target": "addon"}
        self.assertFalse(gates._applies(chk, ("repo",), "addon"))  # wrong scope


if __name__ == "__main__":
    unittest.main()
