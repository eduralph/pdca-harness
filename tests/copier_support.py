"""Reach copier at the point of use, and skip on the condition that actually stopped us.

The three root suites need copier as a LIBRARY (`run_copy` / `run_update`). Whether this
interpreter can import that library is a different proposition from whether the tool is
installed: the documented pipx-style install puts a `copier` executable on `PATH` whose
shebang points at its own private venv, so `copier --version` answers 9.x while
`import copier` raises `ModuleNotFoundError` here. Reporting the second as the first told
readers to install a tool they already had, and it was decided at *collection* time — before
any test body ran — which is why the reason could not carry what actually failed.

So: no module-level probe, no `skipUnless` computed at import. Each suite reaches copier
where it uses it, and above whatever that use allocates — `RenderAndRun` / `RenderCliName`
at the top of their test body, `UpdateCompat` at the top of the `setUpClass` that renders —
because a skip raised out of `setUpClass` means `tearDownClass` never runs, so a temp dir
created before the check would outlive the run. The same shape `UpdateCompat` already uses
for its other precondition — `raise unittest.SkipTest("no vX.Y.Z tags in this checkout
(shallow clone? needs fetch-depth: 0)")` — a condition discovered where it is needed, stated
with what to do about it.

Skipping is still the right outcome for a bare checkout: a developer running
`python3 -m unittest discover -s tests` is not failed by dev deps they never installed. That
a *whole run* of skips is not evidence of anything is a property of the run, not of a test
case, so it is answered one level up, at the process boundary — see `run_root_suite.py`.
"""

from __future__ import annotations

import shutil
import sys
import unittest
from types import ModuleType

# Every skip this module produces starts with these words. `run_root_suite` matches on the
# prefix to tell "this run produced no copier evidence" apart from "this run passed", so the
# reason and the classification stay single-sourced instead of drifting into two vocabularies.
UNIMPORTABLE_PREFIX = "copier is not importable by this interpreter"


def unimportable_reason(exc: BaseException) -> str:
    """Why the copier-dependent coverage cannot run *here*, in this interpreter.

    Carries the real import error and where (if anywhere) a `copier` executable was found on
    `PATH`, because those two facts together are the diagnosis: an executable on `PATH` plus a
    failing import is a CLI-only install, and the fix for it is not "install copier".
    """
    found = shutil.which("copier")
    if found:
        found_note = (
            f"a `copier` executable IS on PATH at {found} — the tool is installed, but a "
            "CLI-only install (pipx-style, in its own venv) is not importable from here"
        )
    else:
        found_note = "no `copier` executable was found on PATH either"
    return (
        f"{UNIMPORTABLE_PREFIX} ({sys.executable}): {type(exc).__name__}: {exc}; "
        f"{found_note}. These suites use copier as a LIBRARY (run_copy/run_update): install "
        f"it for THIS interpreter with `{sys.executable} -m pip install copier`."
    )


def import_copier() -> ModuleType:
    """The `copier` module, imported at the point of use — or a truthful `SkipTest`.

    `except Exception` rather than `ImportError`: an installed-but-broken copier (a bad
    dependency pin, a partially written wheel) raises other things at import, and that is a
    missing precondition for these suites too — the reason then names what it actually was.
    """
    try:
        import copier
    except Exception as exc:
        raise unittest.SkipTest(unimportable_reason(exc)) from exc
    return copier
