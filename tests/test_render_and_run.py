"""Template-repo test: render with copier, then run the generated slice.

This verifies the *template* (copier.yml + .jinja rendering) and the generated
driver together. Skips cleanly if copier isn't importable, so a bare checkout
without dev deps still collects. Run with: python -m unittest discover -s tests
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

try:
    from copier import run_copy  # type: ignore

    HAVE_COPIER = True
except Exception:  # pragma: no cover - environment without copier
    HAVE_COPIER = False


@unittest.skipUnless(HAVE_COPIER, "copier not installed")
class RenderAndRun(unittest.TestCase):
    def test_render_then_slice(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            # Render from a .git-free copy so copier treats it as a plain template.
            src = tmp / "src"
            shutil.copytree(REPO, src, ignore=shutil.ignore_patterns(".git", "__pycache__"))
            out = tmp / "out"
            run_copy(
                str(src),
                str(out),
                data={"project_name": "Render Test", "tracker_url": "https://x/issues"},
                defaults=True,
                unsafe=True,
                quiet=True,
            )

            # Sanity: .jinja suffixes stripped, placeholders substituted.
            self.assertTrue((out / "pdca.toml").exists())
            self.assertIn("Render Test", (out / "pdca.toml").read_text(encoding="utf-8"))
            self.assertFalse(list(out.rglob("*.jinja")), "unstripped .jinja files remain")

            # Run the generated project's own shipped slice test.
            env = {"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"}
            r = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                cwd=out,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(r.returncode, 0, msg=r.stderr)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
