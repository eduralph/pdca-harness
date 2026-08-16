#!/usr/bin/env python3
"""Run the root template-repo suite, and report the third answer `unittest` cannot give.

`python3 -m unittest discover -s tests` answers two questions — did it pass, did it fail —
and a run in which every copier-dependent case skipped comes back `OK`, exit 0: identical,
to a caller and to a workflow, to a run that rendered the template and updated an instance
for real. That is how `.github/workflows/render-check.yml` could report success having
verified nothing about rendering (the same shape #342 fixed for a shallow clone, by making
sure the tags were there rather than by making the non-run visible).

There are three possible outcomes, not two: it passed, it failed, or it could not tell. The
third one belongs here, at the suite's process boundary — a test case can only pass or fail,
and "no case produced evidence" is a property of the run as a whole. This entry point is the
`run-verify.sh` doctrine (`template/engine/scripts/run-verify.sh`, "JUDGE EVERY LEG BY TWO
FACTS … a step in which no test ran is UNVERIFIABLE … never a pass and never a fail")
applied to the suite that *supplies* the gates their evidence, in the repo's own vocabulary:
exit 77 and a line starting `PDCA-UNVERIFIABLE:` (`template/src/pdca_harness/gates.py`).

    python3 -m tests.run_root_suite [-v] [<module>...]

With no module named, discovers the root suite exactly as `discover -s tests` does. Exit 0
when copier-dependent coverage actually ran and passed, non-zero when something ran and
failed, 77 when the run produced no such evidence. The bare `python3 -m unittest discover -s
tests` is deliberately left alone: a developer without dev deps still gets skips and exit 0.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import TextIO

try:  # `discover -s tests` puts tests/ on sys.path; `-m tests.run_root_suite` puts the root
    from copier_support import UNIMPORTABLE_PREFIX
except ImportError:  # the other invocation shape — both are used by this repo's own callers
    from tests.copier_support import UNIMPORTABLE_PREFIX

# Not a new vocabulary: the exit code and marker this harness already ships for "ran, but
# produced no evidence" (template/src/pdca_harness/gates.py: UNVERIFIABLE_RC /
# UNVERIFIABLE_MARKER). Two constraints from the consumer, honoured below: the marker counts
# only at the START of a line (#428), and only on an exit that is 0 or 77 (#329) — so the
# marker is printed on the 77 path and nowhere else.
UNVERIFIABLE_RC = 77
UNVERIFIABLE_MARKER = "PDCA-UNVERIFIABLE:"

TESTS_DIR = Path(__file__).resolve().parent

USAGE = "usage: python3 -m tests.run_root_suite [-v] [<module>...]"


def classify(result: unittest.TestResult, selection: str) -> tuple[int, str]:
    """`(exit code, verdict line)` for a finished run.

    Judged by two facts, never the exit status alone: whether anything failed, and how many
    cases actually *executed*. The two "no evidence" shapes are kept distinguishable because a
    human reading them needs different things — copier missing from this interpreter is an
    install; nothing selected is a wrong module name or start directory.
    """
    reasons = [str(reason) for _case, reason in result.skipped]
    # A skip raised in a test body counts in `testsRun`; one raised in `setUpClass` does not —
    # unittest records that against a `_ErrorHolder` for the whole class (which is how
    # `UpdateCompat`'s existing shallow-clone skip already reports). Subtracting only the
    # skips that DID run keeps the executed count from going negative and, more to the point,
    # keeps "5 cases never started" from being mistaken for evidence.
    ran_and_skipped = [c for c, _r in result.skipped if isinstance(c, unittest.TestCase)]
    executed = max(result.testsRun - len(ran_and_skipped), 0)
    copier_skips = [r for r in reasons if r.startswith(UNIMPORTABLE_PREFIX)]

    if not result.wasSuccessful():
        # Something ran and failed, or a module failed to import (unittest reports that as an
        # error against a synthetic case). That is a verdict, not an absence of one.
        return 1, (
            f"root suite FAILED: {len(result.failures)} failure(s), {len(result.errors)} "
            f"error(s) out of {result.testsRun} case(s) [{selection}]"
        )
    if copier_skips:
        # One import outcome per interpreter: if any case skipped for this, every
        # copier-dependent case in the run did, so the render/update coverage did not run —
        # whatever else in the selection (this suite's own regression cases) passed.
        return UNVERIFIABLE_RC, (
            f"{UNVERIFIABLE_MARKER} no copier-dependent case executed [{selection}]: "
            f"{copier_skips[0]}"
        )
    if executed == 0 and reasons:
        return UNVERIFIABLE_RC, (
            f"{UNVERIFIABLE_MARKER} no test executed — every case selected skipped "
            f"[{selection}]: " + "; ".join(sorted(set(reasons)))
        )
    if executed == 0:
        return UNVERIFIABLE_RC, (
            f"{UNVERIFIABLE_MARKER} no test was selected at all [{selection}] — wrong module "
            "name or start directory?"
        )
    return 0, f"root suite OK: {executed} executed, {len(reasons)} skipped [{selection}]"


def main(argv: list[str] | None = None, stream: TextIO | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    out = sys.stderr if stream is None else stream

    # Rejected rather than ignored: an unrecognised flag filtered out of `names` would leave a
    # smaller selection — or none — and report the resulting silence as 77, which is the wrong
    # answer to a typo.
    unknown = [a for a in args if a.startswith("-") and a not in ("-v", "--verbose")]
    if unknown:
        print(f"{USAGE}\nunrecognised option(s): {' '.join(unknown)}", file=out)
        return 2
    verbosity = 2 if any(a in ("-v", "--verbose") for a in args) else 1
    names = [a for a in args if not a.startswith("-")]

    loader = unittest.TestLoader()
    if names:
        suite = loader.loadTestsFromNames(names)
        selection = " ".join(names)
    else:
        # Exactly what `discover -s tests` does, so this entry point and the bare developer
        # command collect the same cases and differ only in how they report a run.
        suite = loader.discover(str(TESTS_DIR))
        selection = f"discover -s {TESTS_DIR}"

    result = unittest.TextTestRunner(stream=out, verbosity=verbosity).run(suite)
    rc, verdict = classify(result, selection)
    print(verdict, file=out)
    return rc


if __name__ == "__main__":
    sys.exit(main())
