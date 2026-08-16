"""Template-repo test: render with copier, then run the generated slice.

This verifies the *template* (copier.yml + .jinja rendering) and the generated
driver together. Skips cleanly if copier isn't importable by the interpreter
running it — reached at the point of use, so the reason names that and not some
proposition settled at import time (see tests/copier_support.py). A run in which
that skip happened is not evidence of anything: `python3 -m tests.run_root_suite`
reports it as such. Run with: python -m unittest discover -s tests
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


try:  # `discover -s tests` puts tests/ on sys.path; `-m unittest tests.<mod>` puts the root
    from copier_support import import_copier
except ImportError:  # the other invocation shape — this repo's own callers use both
    from tests.copier_support import import_copier


class RenderAndRun(unittest.TestCase):
    def test_render_then_slice(self) -> None:
        # At the point of use, before any fixture work: an interpreter that cannot import
        # copier skips here, with the import error and where the CLI was found on PATH.
        copier = import_copier()
        tmp = Path(tempfile.mkdtemp())
        try:
            # Render from a tagged git copy so copier records a version (what
            # `copier update` later needs). Build a throwaway repo from the source.
            src = tmp / "src"
            shutil.copytree(REPO, src, ignore=shutil.ignore_patterns(".git", "__pycache__"))
            _git(src, "init", "-q")
            _git(src, "add", "-A")
            _git(src, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x")
            _git(src, "tag", "v0test")
            out = tmp / "out"
            copier.run_copy(
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

            # …and the result is VALID TOML. Stripped suffixes prove nothing about syntax:
            # a stray comment or a duplicated key renders happily and then breaks every
            # `pdca` command at config load (#337).
            import tomllib
            try:
                tomllib.loads((out / "pdca.toml").read_text(encoding="utf-8"))
            except tomllib.TOMLDecodeError as exc:  # pragma: no cover - the failure path
                self.fail(f"rendered pdca.toml is not valid TOML: {exc}")

            # The answers file must be written with a recorded version, or
            # `copier update` cannot work — the whole reason for using Copier.
            answers = out / ".copier-answers.yml"
            self.assertTrue(answers.exists(), "copier answers file not written")
            self.assertIn("_commit: v0test", answers.read_text(encoding="utf-8"))

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
