"""Unit tests for target-aware gate selection (stdlib unittest, offline).

Covers the two pure helpers that decide *which* gates run: ``gates._bundle_target``
(classify a bundle into a label SET — a primary axis plus additive flags) and
``gates._applies`` (a gate runs iff its target labels are a SUBSET of that set, i.e.
AND). No subprocess, no Docker.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pdca_harness import gates


def _bundle(target_line: str, surfaces: str | None = None) -> Path:
    d = Path(tempfile.mkdtemp())
    body = f"- **Slug:** x\n- **Repo + branch target:** {target_line}\n"
    if surfaces is not None:
        body += f"- **Surfaces:** {surfaces}\n"
    (d / "brief.md").write_text(body, encoding="utf-8")
    return d


MATCH = {"addon": "addons-source"}        # primary axis (else default)
DEFAULT = "core"
FLAGS = {"frontend": {"field": "surfaces", "substring": "gui"}}   # additive flag


class BundleTarget(unittest.TestCase):
    def test_primary_match(self) -> None:
        self.assertEqual(
            gates._bundle_target(_bundle("org/addons-source @ g60"), MATCH, DEFAULT),
            frozenset({"addon"}),
        )

    def test_primary_default(self) -> None:
        self.assertEqual(
            gates._bundle_target(_bundle("org/gramps @ g61"), MATCH, DEFAULT),
            frozenset({"core"}),
        )

    def test_flag_adds_to_primary(self) -> None:  # frontend addon = {addon, frontend}
        b = _bundle("org/addons-source @ g60", surfaces="gui")
        self.assertEqual(
            gates._bundle_target(b, MATCH, DEFAULT, FLAGS), frozenset({"addon", "frontend"})
        )

    def test_flag_on_core(self) -> None:  # core GUI fix = {core, frontend}
        b = _bundle("org/gramps @ g61", surfaces="gui")
        self.assertEqual(
            gates._bundle_target(b, MATCH, DEFAULT, FLAGS), frozenset({"core", "frontend"})
        )

    def test_flag_absent(self) -> None:  # backend addon = {addon}
        b = _bundle("org/addons-source @ g60", surfaces="data")
        self.assertEqual(
            gates._bundle_target(b, MATCH, DEFAULT, FLAGS), frozenset({"addon"})
        )

    def test_no_bundle_is_none(self) -> None:
        self.assertIsNone(gates._bundle_target(None, MATCH, DEFAULT, FLAGS))

    def test_no_config_is_none(self) -> None:
        self.assertIsNone(gates._bundle_target(_bundle("org/gramps @ g61"), {}, "", {}))

    def test_flags_only_no_primary(self) -> None:  # only flags configured
        b = _bundle("org/gramps @ g61", surfaces="gui")
        self.assertEqual(gates._bundle_target(b, {}, "", FLAGS), frozenset({"frontend"}))


class Applies(unittest.TestCase):
    SCOPES = ("repo", "bundle")
    ADDON_FE = frozenset({"addon", "frontend"})
    ADDON = frozenset({"addon"})
    CORE = frozenset({"core"})

    def test_untargeted_always_runs(self) -> None:
        chk = {"scope": "repo"}
        self.assertTrue(gates._applies(chk, self.SCOPES, self.ADDON))
        self.assertTrue(gates._applies(chk, self.SCOPES, None))

    def test_single_label_subset(self) -> None:
        self.assertTrue(gates._applies({"scope": "repo", "target": "addon"}, self.SCOPES, self.ADDON_FE))
        self.assertFalse(gates._applies({"scope": "repo", "target": "core"}, self.SCOPES, self.ADDON_FE))

    def test_list_target_is_AND(self) -> None:  # ["addon","frontend"] needs BOTH
        chk = {"scope": "repo", "target": ["addon", "frontend"]}
        self.assertTrue(gates._applies(chk, self.SCOPES, self.ADDON_FE))   # both present
        self.assertFalse(gates._applies(chk, self.SCOPES, self.ADDON))     # frontend missing
        self.assertFalse(gates._applies(chk, self.SCOPES, self.CORE))

    def test_frontend_runs_for_core_gui_and_frontend_addon(self) -> None:
        chk = {"scope": "repo", "target": "frontend"}
        self.assertTrue(gates._applies(chk, self.SCOPES, self.ADDON_FE))
        self.assertTrue(gates._applies(chk, self.SCOPES, frozenset({"core", "frontend"})))
        self.assertFalse(gates._applies(chk, self.SCOPES, self.ADDON))  # backend addon skips

    def test_unknown_labels_never_over_skip(self) -> None:
        self.assertTrue(gates._applies({"scope": "repo", "target": ["addon", "frontend"]}, self.SCOPES, None))

    def test_scope_filter_still_applies(self) -> None:
        self.assertFalse(gates._applies({"scope": "bundle", "target": "addon"}, ("repo",), self.ADDON))


if __name__ == "__main__":
    unittest.main()
