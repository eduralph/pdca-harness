"""`[driver].wave_mode` accepts `stack` and `merge` and refuses everything else (issue #842).

Every check on the mode compares against `"merge"`, so a value the loader kept as given
behaved as `"stack"` with no error. `Config.load` now raises `ValueError` instead of going on
with any value at all, and the CLI's existing `except ValueError` turns that into one stderr
line and exit 2. These tests go through the real loader and the real CLI; nothing is mocked.

Run from the project root: PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from pdca_harness import cli
from pdca_harness.config import Config

BASE = '[paths]\nbundle_root = "results"\n'


class WaveModeValidation(unittest.TestCase):
    def _load(self, driver_line: str | None) -> tuple[Config, str]:
        """Run the real loader on a fresh `pdca.toml`; return the config and its stderr.

        A refused value raises out of here, so the buffer also stays on `self` for the caller
        to read after `assertRaises`. A `redirect_stderr` in the caller would not do: the one
        below replaces it, so its buffer stays empty whatever `Config.load` prints.
        """
        self.err = err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            text = BASE if driver_line is None else f"{BASE}\n[driver]\n{driver_line}\n"
            (root / "pdca.toml").write_text(text, encoding="utf-8")
            with redirect_stderr(err):
                cfg = Config.load(root)
        return cfg, err.getvalue()

    def test_known_values_and_absent_key_are_silent_and_kept(self) -> None:
        cases = (
            ('wave_mode = "stack"', "stack"),
            ('wave_mode = "merge"', "merge"),
            (None, "stack"),
        )
        for line, expected in cases:
            with self.subTest(line=line):
                cfg, err = self._load(line)
                self.assertEqual(cfg.wave_mode, expected)
                self.assertEqual(err, "")

    def test_unknown_values_raise(self) -> None:
        cases = (  # the TOML line, then the value as the message must show it
            ('wave_mode = "bogus"', "'bogus'"),
            ('wave_mode = "Merge"', "'Merge'"),  # exact match: no case folding...
            ('wave_mode = "merge "', "'merge '"),  # ...and no stripping
            ('wave_mode = "none"', "'none'"),
            ("wave_mode = 1", "1"),
            ("wave_mode = true", "True"),
            ('wave_mode = ["merge"]', "['merge']"),  # unhashable: must not be a TypeError
            ('wave_mode = { mode = "merge" }', "{'mode': 'merge'}"),  # likewise
        )
        for line, shown in cases:
            with self.subTest(line=line):
                with self.assertRaises(ValueError) as cm:
                    self._load(line)
                message = str(cm.exception)
                self.assertIn("wave_mode", message)
                self.assertIn(shown, message)
                self.assertIn("stack | merge", message)
                self.assertEqual(self.err.getvalue(), "")  # the CLI prints it, not the loader

    def test_cli_reports_one_line_and_exits_2(self) -> None:
        cwd = os.getcwd()
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            text = f'{BASE}\n[driver]\nwave_mode = "bogus"\n'
            (Path(tmp) / "pdca.toml").write_text(text, encoding="utf-8")
            try:
                os.chdir(tmp)
                with redirect_stderr(err), redirect_stdout(out):
                    rc = cli.main(["status"])
            finally:
                os.chdir(cwd)
        self.assertEqual(rc, 2)
        lines = err.getvalue().splitlines()
        self.assertEqual(len(lines), 1, err.getvalue())
        self.assertIn("wave_mode", lines[0])
        self.assertIn("bogus", lines[0])
        self.assertIn("stack | merge", lines[0])
        self.assertNotIn("Traceback", lines[0])


if __name__ == "__main__":
    unittest.main()
