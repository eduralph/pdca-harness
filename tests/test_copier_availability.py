"""Regression: a skip states the condition that stopped it, and a run of nothing is not `OK`.

Two errors were shipped together in the three root suites. Each decided at *import* time
whether `copier` was importable in the running interpreter, then reported that as "copier not
installed" — a different proposition, and a false one on the documented pipx-style install
(a `copier` executable on `PATH` whose shebang points at its own venv). And a run in which
all of that coverage skipped came back `OK`, exit 0: `.github/workflows/render-check.yml`
could report success having rendered nothing.

The postures are supplied HERE — the import failure through a meta-path hook, the `PATH`
lookup through `shutil.which` — so these cases assert the same thing on a machine where
copier is importable (CI, a dev `.venv`) and on one where it is not. Nothing below depends on
what the host happens to have installed, and nothing below re-runs the render/update suites
for real: they are exercised for their *skip* behaviour, which is the behaviour under test.

Covers, in the order of the criteria they belong to: the reason names the failed import, its
error, and where the executable was found (i); nothing decides availability before a test
body runs (ii); the entry point that turns "no evidence" into exit 77 +
`PDCA-UNVERIFIABLE:` (iii); and the bare `python3 -m unittest discover -s tests` still
skipping, still successful, with no environment variable able to change that (iv).
"""

from __future__ import annotations

import ast
import contextlib
import importlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock

try:  # `discover -s tests` puts tests/ on sys.path; `-m unittest tests.<mod>` puts the root
    import copier_support
    import run_root_suite
except ImportError:  # the other invocation shape — this repo's own callers use both
    from tests import copier_support, run_root_suite

REPO = Path(__file__).resolve().parents[1]
GATES_PY = REPO / "template" / "src" / "pdca_harness" / "gates.py"

# The three suites that carried the conflated probe. Named, not discovered: a module that
# stopped being copier-dependent should show up here as a decision, not as a silent gap.
SUITE_MODULES = ("test_render_and_run", "test_render_cli_name", "test_update_compat")

# What the shipped probe used to say. Asserted absent, because it is the false proposition.
OLD_FALSE_REASON = "not installed"


class _NoCopierFinder:
    """A meta-path hook: `import copier` fails here the way it does under a CLI-only install."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "copier" or fullname.startswith("copier."):
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        return None


@contextlib.contextmanager
def copier_unimportable(found_on_path: str | None = "/home/dev/.local/bin/copier"):
    """This interpreter cannot import copier; `found_on_path` is what `PATH` holds.

    The default is the pipx posture that made the old reason false: the tool IS installed and
    on `PATH`, and it still cannot be imported here.
    """
    finder = _NoCopierFinder()
    stashed = {k: v for k, v in sys.modules.items()
               if k == "copier" or k.startswith("copier.")}
    for name in stashed:
        del sys.modules[name]
    sys.meta_path.insert(0, finder)
    try:
        with mock.patch("shutil.which",
                        side_effect=lambda name: found_on_path if name == "copier" else None):
            yield
    finally:
        sys.meta_path.remove(finder)
        sys.modules.update(stashed)


def load_suite_module(name: str) -> ModuleType:
    """Import one of the three suites FRESH under the posture in force right now.

    Reloaded on purpose: whether the module decides anything at import time is exactly what is
    under test, and a copy imported earlier by the collector would answer for a different
    posture than the one this case set up.
    """
    for candidate in (f"tests.{name}", name):
        try:
            module = importlib.import_module(candidate)
        except ImportError:
            continue
        return importlib.reload(module)
    raise ImportError(f"neither tests.{name} nor {name} is importable")


def cases_of(module: ModuleType) -> list[type[unittest.TestCase]]:
    return [obj for obj in vars(module).values()
            if isinstance(obj, type) and issubclass(obj, unittest.TestCase)
            and obj.__module__ == module.__name__]


def run_suites(modules: list[ModuleType]) -> unittest.TestResult:
    suite = unittest.TestSuite(
        unittest.TestLoader().loadTestsFromModule(module) for module in modules)
    result = unittest.TestResult()
    suite.run(result)
    return result


@contextlib.contextmanager
def suites_under_pipx_posture():
    """The three suites, freshly imported with copier unimportable and a tag answer supplied.

    `UpdateCompat.setUpClass` checks for release tags before it reaches copier, and a shallow
    clone (CI's default checkout) has none — so the tag lookup is answered here too, or these
    cases would assert one skip reason on a full clone and another on a shallow one.
    """
    with copier_unimportable():
        modules = [load_suite_module(name) for name in SUITE_MODULES]
        with contextlib.ExitStack() as stack:
            for module in modules:
                if hasattr(module, "prior_release_ref"):
                    stack.enter_context(mock.patch.object(
                        module, "prior_release_ref", return_value="v9.9.9"))
            yield modules


class TruthfulSkipReason(unittest.TestCase):
    """(i) The reason names the import that failed, in this interpreter, and never claims
    the tool is not installed."""

    def test_names_the_failed_import_and_the_executable_on_path(self) -> None:
        with copier_unimportable("/home/dev/.local/bin/copier"):
            with self.assertRaises(unittest.SkipTest) as raised:
                copier_support.import_copier()
        reason = str(raised.exception)

        self.assertTrue(reason.startswith(copier_support.UNIMPORTABLE_PREFIX), reason)
        self.assertIn(sys.executable, reason)  # WHICH interpreter could not import it
        self.assertIn("No module named 'copier'", reason)  # the real error, not a paraphrase
        self.assertIn("/home/dev/.local/bin/copier", reason)  # where PATH found the tool
        self.assertNotIn(OLD_FALSE_REASON, reason)

    def test_says_so_when_no_executable_is_on_path_either(self) -> None:
        with copier_unimportable(None):
            with self.assertRaises(unittest.SkipTest) as raised:
                copier_support.import_copier()
        reason = str(raised.exception)

        self.assertIn("no `copier` executable was found on PATH", reason)
        self.assertIn("No module named 'copier'", reason)
        self.assertNotIn(OLD_FALSE_REASON, reason)

    def test_an_importable_copier_is_returned_not_skipped(self) -> None:
        """The other half: when the import works, nothing is skipped and the caller gets the
        module it asked for."""
        stub = ModuleType("copier")
        stub.run_copy = stub.run_update = lambda *a, **k: None
        with mock.patch.dict(sys.modules, {"copier": stub}):
            self.assertIs(copier_support.import_copier(), stub)


class NoVerdictBeforeATestBodyRuns(unittest.TestCase):
    """(ii) Availability is decided where copier is used, not at import/collection."""

    def test_modules_import_and_collect_with_copier_unimportable(self) -> None:
        with copier_unimportable():
            for name in SUITE_MODULES:
                with self.subTest(module=name):
                    module = load_suite_module(name)
                    classes = cases_of(module)
                    self.assertTrue(classes, f"{name} collected no TestCase at all")
                    for cls in classes:
                        self.assertFalse(
                            getattr(cls, "__unittest_skip__", False),
                            f"{name}.{cls.__name__} was marked skipped before it ran — "
                            "availability is being decided at import time")

    def test_no_module_level_copier_probe_survives(self) -> None:
        """No `HAVE_COPIER` computed-but-unread, and no second copier import that could drift
        from the one that decides availability."""
        with copier_unimportable():
            for name in SUITE_MODULES:
                module = load_suite_module(name)
                for leftover in ("HAVE_COPIER", "copier", "run_copy", "run_update"):
                    with self.subTest(module=name, name=leftover):
                        self.assertFalse(
                            hasattr(module, leftover),
                            f"{name} still binds `{leftover}` at module level")


class BareDeveloperRunIsUnchanged(unittest.TestCase):
    """(iv) `python3 -m unittest discover -s tests` still skips, still succeeds, whatever is
    in the environment."""

    def test_every_case_skips_with_the_truthful_reason_and_nothing_fails(self) -> None:
        with suites_under_pipx_posture() as modules:
            result = run_suites(modules)

        self.assertTrue(result.wasSuccessful(), "a missing precondition must not fail the run")
        self.assertEqual(result.failures, [])
        self.assertEqual(result.errors, [])
        self.assertTrue(result.skipped, "nothing skipped — the posture did not take effect")
        for case, reason in result.skipped:
            with self.subTest(case=str(case)):
                self.assertTrue(str(reason).startswith(copier_support.UNIMPORTABLE_PREFIX),
                                f"{case}: {reason}")
                self.assertNotIn(OLD_FALSE_REASON, str(reason))

    def test_a_skipped_run_leaves_no_temp_directory_behind(self) -> None:
        """A skipped run allocates nothing — the precondition is settled before any fixture.

        `unittest.suite` sets `_classSetupFailed` when `setUpClass` raises, and then never
        calls `tearDownClass` — so whatever a class allocated ABOVE its precondition check
        survives the run, once per invocation, on exactly the pipx posture this module exists
        to serve. Asserted over a temp root this case owns and inspects afterwards, so it
        catches a leak whatever produced it, rather than encoding one statement's position.
        """
        sandbox = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, sandbox, ignore_errors=True)

        with mock.patch.object(tempfile, "tempdir", str(sandbox)):
            with suites_under_pipx_posture() as modules:
                result = run_suites(modules)

        # `UpdateCompat` reaches copier from `setUpClass`, which is the leaking shape — so
        # assert that path was actually taken, or an empty sandbox would only prove the run
        # never got that far.
        self.assertTrue(
            any("UpdateCompat" in str(case) for case, _reason in result.skipped),
            f"UpdateCompat never reached setUpClass under the pipx posture: {result.skipped}")
        self.assertEqual(
            sorted(entry.name for entry in sandbox.iterdir()), [],
            "a run in which every copier-dependent case skipped left a temp directory "
            "behind — a fixture was allocated before the precondition was checked")

    def test_no_environment_variable_turns_a_skip_into_a_failure(self) -> None:
        """A bundle-identity variable is not a declaration that coverage is required: an
        ad-hoc run inside a leaf session must behave exactly like a developer's."""
        ambient = {"PDCA_BUNDLE": "/tmp/results/issue_495", "PDCA_WORKTREE": "/tmp/wt",
                   "PDCA_TARGET": "/tmp/target", "CI": "true"}
        with mock.patch.dict(os.environ, ambient):
            with suites_under_pipx_posture() as modules:
                result = run_suites(modules)

        self.assertTrue(result.wasSuccessful(), f"{ambient} changed a skip into a failure")
        self.assertEqual(result.failures, [])
        self.assertEqual(result.errors, [])
        self.assertTrue(result.skipped)


class NoEvidenceIsNotSuccess(unittest.TestCase):
    """(iii) The entry point CONTRIBUTING.md and render-check.yml use reports a run that
    verified nothing as unverifiable, not as a pass."""

    fixtures: dict[str, str] = {}

    @classmethod
    def setUpClass(cls) -> None:
        # The reason the runner must recognise is the one production produces — taken from it
        # here, so the two halves cannot drift into two vocabularies without this failing.
        copier_reason = ""
        with copier_unimportable():
            try:
                copier_support.import_copier()
            except unittest.SkipTest as skip:
                copier_reason = str(skip)
        assert copier_reason, "import_copier() did not skip under an unimportable copier"
        # Everything that can raise is settled above: a `setUpClass` that raises never gets a
        # `tearDownClass` (unittest.suite), so nothing is allocated until it cannot leak.
        cls.tmp = tempfile.mkdtemp()
        # Fixture modules, so the classification is driven with runs this case owns rather
        # than with the real suites (whose outcome depends on the host's copier).
        cls.fixtures = {
            "rrs_copier_skip": f"raise unittest.SkipTest({copier_reason!r})",
            "rrs_other_skip": 'raise unittest.SkipTest("no vX.Y.Z tags in this checkout")',
            "rrs_pass": "self.assertTrue(True)",
            "rrs_fail": 'self.fail("a real failure")',
        }
        for name, body in cls.fixtures.items():
            (Path(cls.tmp) / f"{name}.py").write_text(
                "import unittest\n\n\n"
                "class Case(unittest.TestCase):\n"
                f"    def test_it(self):\n        {body}\n",
                encoding="utf-8")
        (Path(cls.tmp) / "rrs_empty.py").write_text("import unittest\n", encoding="utf-8")
        sys.path.insert(0, cls.tmp)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.tmp in sys.path:
            sys.path.remove(cls.tmp)
        for name in list(sys.modules):
            if name.startswith("rrs_"):
                del sys.modules[name]
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_entry_point(self, *names: str) -> tuple[int, str]:
        out = io.StringIO()
        rc = run_root_suite.main(list(names), stream=out)
        return rc, out.getvalue()

    def assertMarkerLine(self, output: str) -> str:
        """The marker counts only at the START of a line (#428) — assert it that way."""
        lines = [ln for ln in output.splitlines()
                 if ln.startswith(run_root_suite.UNVERIFIABLE_MARKER)]
        self.assertEqual(len(lines), 1, f"expected exactly one marker line in:\n{output}")
        return lines[0]

    def test_a_run_with_no_copier_case_executed_is_unverifiable(self) -> None:
        rc, output = self.run_entry_point("rrs_copier_skip")

        self.assertEqual(rc, run_root_suite.UNVERIFIABLE_RC)
        self.assertEqual(rc, 77)
        line = self.assertMarkerLine(output)
        self.assertIn("no copier-dependent case executed", line)
        self.assertIn(copier_support.UNIMPORTABLE_PREFIX, line)

    def test_other_cases_passing_does_not_make_it_evidence(self) -> None:
        """The shape `python3 -m tests.run_root_suite` (discovery) actually takes: this
        regression module passes while every copier case skips. That is still no evidence
        about rendering, and it must not exit 0."""
        rc, output = self.run_entry_point("rrs_pass", "rrs_copier_skip")

        self.assertEqual(rc, run_root_suite.UNVERIFIABLE_RC)
        self.assertIn("no copier-dependent case executed", self.assertMarkerLine(output))

    def test_a_selection_that_only_skips_is_unverifiable_too(self) -> None:
        """#342's other half: a shallow clone skips the update suite, and the CI step that
        ran only it produced nothing either."""
        rc, output = self.run_entry_point("rrs_other_skip")

        self.assertEqual(rc, run_root_suite.UNVERIFIABLE_RC)
        line = self.assertMarkerLine(output)
        self.assertIn("no test executed", line)
        self.assertIn("no vX.Y.Z tags", line)

    def test_a_selection_with_nothing_in_it_is_unverifiable_too(self) -> None:
        rc, output = self.run_entry_point("rrs_empty")

        self.assertEqual(rc, run_root_suite.UNVERIFIABLE_RC)
        self.assertIn("no test was selected at all", self.assertMarkerLine(output))

    def test_a_run_that_executed_and_passed_still_exits_zero(self) -> None:
        rc, output = self.run_entry_point("rrs_pass")

        self.assertEqual(rc, 0)
        self.assertNotIn(run_root_suite.UNVERIFIABLE_MARKER, output)

    def test_an_unrecognised_option_is_a_usage_error_not_a_silent_77(self) -> None:
        """A typo must not shrink the selection into a run that reports no evidence."""
        rc, output = self.run_entry_point("--no-such-flag", "rrs_pass")

        self.assertEqual(rc, 2)
        self.assertIn("usage:", output)
        self.assertNotIn(run_root_suite.UNVERIFIABLE_MARKER, output)

    def test_a_real_failure_still_fails(self) -> None:
        """And carries no marker: the driver honours it only at exit 0 or 77 (#329), so a
        failing run that printed one would be claiming a channel it does not have."""
        rc, output = self.run_entry_point("rrs_fail")

        self.assertNotEqual(rc, 0)
        self.assertNotEqual(rc, run_root_suite.UNVERIFIABLE_RC)
        self.assertNotIn(run_root_suite.UNVERIFIABLE_MARKER, output)

    def test_the_real_suites_under_the_pipx_posture_classify_as_no_evidence(self) -> None:
        """End to end, through the production modules: the three suites, run with copier
        unimportable, are classified unverifiable — not `OK`."""
        with suites_under_pipx_posture() as modules:
            result = run_suites(modules)
        rc, verdict = run_root_suite.classify(result, "the three root suites")

        self.assertEqual(rc, run_root_suite.UNVERIFIABLE_RC)
        self.assertTrue(verdict.startswith(run_root_suite.UNVERIFIABLE_MARKER), verdict)
        self.assertIn(copier_support.UNIMPORTABLE_PREFIX, verdict)
        self.assertNotIn(OLD_FALSE_REASON, verdict)

    def test_the_vocabulary_is_the_harness_s_own(self) -> None:
        """Not a new convention: the exit code and marker are the ones gates.py classifies on.
        If those ever move, this fails here rather than by silently exiting 77 at a driver
        that no longer reads it."""
        self.assertTrue(GATES_PY.exists(), f"{GATES_PY} missing")
        shipped = {}
        for node in ast.parse(GATES_PY.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in (
                            "UNVERIFIABLE_RC", "UNVERIFIABLE_MARKER"):
                        shipped[target.id] = node.value.value

        self.assertEqual(shipped.get("UNVERIFIABLE_RC"), run_root_suite.UNVERIFIABLE_RC)
        self.assertEqual(shipped.get("UNVERIFIABLE_MARKER"), run_root_suite.UNVERIFIABLE_MARKER)


if __name__ == "__main__":
    unittest.main()
