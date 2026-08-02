"""Suite output hygiene — a test suite's output is its own report (issue #402).

`unittest` writes its report to STDERR. Anything a suite writes to STDOUT is therefore
output it leaked from the production code it drives — and under a pipe (which is how every
gate captures a command: one merged `stdout=PIPE, stderr=STDOUT` stream,
`progress.run_with_heartbeat`) block-buffered stdout flushes LAST, so those leaked lines
land after `OK` and become what a reader — or a gate's evidence line — sees as the run's
verdict. `tests/test_split.py` drives two production printers, `leaves.do_split`
(`print(f"{d / split.PROPOSAL}")`) and `cli._split` (`print(child)`), and on paths that did
not apply the file's own `redirect_stdout(io.StringIO())` convention it published five
`/tmp/…` scratch paths that outlived nothing; a green C4 gate was frozen with one of them
as its whole recorded evidence.

Kept in its own module on purpose: it runs `tests.test_split` in a SUBPROCESS, and a suite
that shelled out to the module it is a part of would recurse.

Run from the project root: PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

# tests/ lives directly under the project root; `src/` is its sibling.
ROOT = Path(__file__).resolve().parents[1]

# The module this issue names. Deliberately explicit rather than a sweep over every
# module: the suite-wide hygiene sweep is a separate, much larger slice.
LEAKY_CANDIDATE = "tests.test_split"


class SuiteWritesNothingToStdout(unittest.TestCase):
    def test_test_split_leaks_no_driven_stdout(self) -> None:
        env = {**os.environ, "PYTHONPATH": "src"}
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", LEAKY_CANDIDATE],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=300,
        )
        # The suite itself must be green, or "no stdout" would be vacuous.
        self.assertEqual(proc.returncode, 0,
                         f"{LEAKY_CANDIDATE} is not green:\n{proc.stderr}")
        self.assertIn("OK", proc.stderr, "the unittest report is not on stderr")
        self.assertEqual(
            proc.stdout, "",
            f"{LEAKY_CANDIDATE} leaked the output of the code it drives onto stdout — "
            f"under a gate's merged capture these lines flush after the report and are "
            f"read as the run's result:\n{proc.stdout}")


if __name__ == "__main__":
    unittest.main()
