"""Reviewer/advisory leaf resilience (issue #138) — stdlib unittest, no deps.

A Check reviewer/advisory leaf that exits non-zero used to collapse to an opaque §6
placeholder whose only diagnostic was the exit code, and a transient `claude -p`
blip (usage/rate limit, 5xx) wedged sign-off the same as a substantive failure.
`_invoke_leaf_resilient` now: (1) persists the failed attempts' stderr tail to an
error log, (2) retries a *transient* (no-output) failure with backoff before
degrading, and (3) classifies the placeholder transient-vs-substantive. Run with:
    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from pdca_harness import leaves
from pdca_harness.config import LeafConfig

# A claude-family leaf so the stream path engages (that is the only path that yields
# the "did a session start" signal). argv is a python interpreter running an inline
# script; `_invoke` appends --output-format/--verbose (ignored) and feeds the prompt
# on stdin. The script counts its own invocations into $CNT so a test can assert the
# retry count.
_TRANSIENT = (  # dies at invocation: only stderr, no stdout → no session started
    "import os,sys; open(os.environ['CNT'],'a').write('x'); "
    "sys.stderr.write('overloaded_error 529\\n'); sys.exit(1)"
)
_SUBSTANTIVE = (  # ran (emitted a stream event on stdout) then failed
    "import os,sys; open(os.environ['CNT'],'a').write('x'); "
    "print('{\"type\": \"assistant\"}'); sys.stderr.write('boom\\n'); sys.exit(1)"
)


def _leaf(script: str) -> LeafConfig:
    return LeafConfig(mode="command", family="claude",
                      argv=[sys.executable, "-c", script], interactive=False)


class LeafResilience(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cnt = self.tmp / "count.txt"
        self.error_log = self.tmp / "check-review.error.log"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _runs(self) -> int:
        return len(self.cnt.read_text()) if self.cnt.exists() else 0

    def test_transient_failure_retries_then_persists_classified(self) -> None:
        err = leaves._invoke_leaf_resilient(
            _leaf(_TRANSIENT), self.tmp, "review please",
            error_log=self.error_log, attempts=3, backoff=0.0,
            stream_json=True, env={"CNT": str(self.cnt)})
        self.assertIsInstance(err, leaves.LeafError)
        self.assertTrue(err.transient)            # no stdout → transient infra signal
        self.assertEqual(self._runs(), 3)         # retried up to the attempt budget
        self.assertTrue(self.error_log.exists())  # recoverable error text persisted
        self.assertIn("overloaded_error 529", self.error_log.read_text())

    def test_substantive_failure_is_not_retried(self) -> None:
        err = leaves._invoke_leaf_resilient(
            _leaf(_SUBSTANTIVE), self.tmp, "review please",
            error_log=self.error_log, attempts=3, backoff=0.0,
            stream_json=True, env={"CNT": str(self.cnt)})
        self.assertIsInstance(err, leaves.LeafError)
        self.assertFalse(err.transient)   # emitted a session event → substantive
        self.assertEqual(self._runs(), 1)  # no retry for a non-transient failure
        self.assertIn("boom", self.error_log.read_text())

    def test_success_leaves_no_error_log(self) -> None:
        ok = _leaf("import os; open(os.environ['CNT'],'a').write('x'); print('{}')")
        err = leaves._invoke_leaf_resilient(
            ok, self.tmp, "review please", error_log=self.error_log,
            attempts=3, backoff=0.0, stream_json=True, env={"CNT": str(self.cnt)})
        self.assertIsNone(err)
        self.assertEqual(self._runs(), 1)
        self.assertFalse(self.error_log.exists())

    def test_stale_error_log_cleared_on_success(self) -> None:
        self.error_log.write_text("stale tail from a prior cycle")
        ok = _leaf("print('{}')")
        leaves._invoke_leaf_resilient(
            ok, self.tmp, "review please", error_log=self.error_log,
            attempts=3, backoff=0.0, stream_json=True, env={"CNT": str(self.cnt)})
        self.assertFalse(self.error_log.exists())

    def test_placeholder_classification_transient_vs_substantive(self) -> None:
        leaves._review_unavailable(self.tmp, "reviewer leaf failed: x",
                                   transient=True, error_log=self.error_log)
        txt = (self.tmp / "check-review.md").read_text()
        self.assertIn("transient infra", txt)
        self.assertIn("NEEDS-HUMAN", txt)

        leaves._review_unavailable(self.tmp, "reviewer produced no check-review.md")
        txt = (self.tmp / "check-review.md").read_text()
        self.assertIn("substantive", txt)


if __name__ == "__main__":
    unittest.main()
