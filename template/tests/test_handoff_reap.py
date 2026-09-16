"""The interactive leaves' exit contract is judged when the driver reaps the session,
never at a turn end (issue #534; stdlib unittest, offline, no Claude/TTY).

#331 registered `handoff_guard.py` as a Claude Code **Stop** hook. Stop fires every time
the main agent finishes a turn, not when the session ends, and exit 2 sends the hook's
stderr back to the MODEL instead of handing the turn to the human. An interactive leaf
that asked the human a question therefore never reached the human: the model was told
to write the missing artifact (for sign-off, the human's own decision) and was blocked
again on every turn.

Covers, against the #534 success criterion:
  (a) no turn end is intercepted: the template registers no Stop hook, and the hook run
      the way the old registration ran it (no arguments, a Stop envelope on stdin, a
      registered session whose contract is UNDISCHARGED) exits 0 and prints nothing, so
      an instance whose settings.json still carries the old registration (a `copier
      update` merge that kept it) cannot deadlock either;
  (b) the contract is reported when the driver reaps the leaf (`handoff.session`): what
      `stop_problems()` finds goes to stderr naming the role and each bundle, and a
      discharged contract prints nothing;
  (c) report only: nothing under the project root changes, and nothing raises out of
      the context manager — not a check that fails, not a scratch file the session
      broke; the leaf's own exception passes through untouched;
  (d) the report hides nothing: an abandon reason is printed first and the problem list
      still follows; a blank `abandoned` is no reason; a bundle whose check raises is
      named and every other bundle is still reported; a `[[doctor.checks]]` table that
      cannot be read is ONE line at every planner reap (bundles registered or not,
      briefs or not) naming the file, the error and the unchecked dependency clause,
      while the rest of every brief is still checked and reported;
  (e) nothing the session wrote is printed raw: every line the reap prints is printable
      (checked on every reap below), so a terminal escape in an abandon reason or a
      brief cannot hide the lines after it;
  (g) the text the MODEL reads — the `/handoff` command body, the leaf prompts, the
      hook's own replies — no longer says a hook enforces the contract when a turn or
      the session ends;
  (h) `/handoff <id>` (`--check`) and `--abandon` keep working.
(f), a quiet suite, is a property of the whole driver suite: every reap here is
captured, and so must be any test elsewhere that reaps an undischarged session.

RED on a tree without the fix: settings.json registers the Stop hook and the hook's
no-argument mode returns 2 for an undischarged contract (a); `session()` prints only an
abandon reason, raw, when it exits, and `stop_problems()` returns nothing once
`abandoned` is set (b–e); the prompts and the command body name the Stop hook (g).
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from pdca_harness import handoff, leaves
from pdca_harness.config import Config, LeafConfig

TEMPLATE_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = TEMPLATE_ROOT / "templates"
SRC = TEMPLATE_ROOT / "src"
HOOK = TEMPLATE_ROOT / ".claude" / "hooks" / "handoff_guard.py"
SETTINGS = TEMPLATE_ROOT / ".claude" / "settings.json"


def _first(*names: str) -> Path:
    """`<name>.jinja` in the template checkout, `<name>` in a rendered instance: this
    suite runs in both (the same convention as tests/test_handoff.py::_first)."""
    paths = [TEMPLATE_ROOT / n for n in names]
    return next((p for p in paths if p.is_file()), paths[0])


COMMAND = _first(".claude/commands/handoff.md.jinja", ".claude/commands/handoff.md")

# The hook events that fire at a TURN end: Stop (the main agent finished responding) and
# its subagent twin. A hook on either can turn a turn end into feedback to the model.
TURN_END_EVENTS = ("Stop", "SubagentStop")


def _stop_envelope(active: bool = False) -> str:
    """What Claude Code writes on a Stop hook's stdin when a turn ends."""
    return json.dumps({"session_id": "t", "transcript_path": "/dev/null", "cwd": ".",
                       "hook_event_name": "Stop", "stop_hook_active": active})


def _brief_text(*, criterion: str = "the observable condition that means it is fixed.",
                target: str = "example-org/example-repo @ main",
                deps: str = "none") -> str:
    """An authored brief; pass ``""`` for a field to leave it empty."""
    fields = (("Slug", "real-slug"), ("Defect", "something observable is wrong."),
              ("Success criterion", criterion), ("Repo + branch target", target),
              ("External dependencies", deps))
    return "# Brief — issue 7 / real\n\n" + "".join(
        f"- **{label}:** {value}".rstrip() + "\n" for label, value in fields)


_AUTHORED_BRIEF = _brief_text()

# pdca.toml as a session might leave it, each making the read of the [[doctor.checks]]
# table that the dependency clause does raise a different way (bytes, so that a file
# which is not UTF-8 is one of them). A file that does not parse at all is the other
# kind of broken table: that read does not raise on it (see _UNPARSEABLE_TOML).
_BROKEN_TABLES = (
    ("doctor is not a table", b"doctor = 1\n"),
    ("doctor.checks is not an array", b"[doctor]\nchecks = 5\n"),
    ("a row is not a table", b'[doctor]\nchecks = ["protoc"]\n'),
    ("not UTF-8", '[[doctor.checks]]\nid = "café"\ncmd = "true"\n'.encode("cp1252")),
)
_UNPARSEABLE_TOML = b'[[doctor.checks]]\nid = "frobnicator\ncmd = "true"\n'


def _cfg(root: Path) -> Config:
    # The contract Config, built the way tests/test_handoff.py::_cfg builds it: the four
    # contract roles are interactive command leaves.
    return Config(
        root=root,
        bundle_root=root / "results",
        process_dir=root / "process",
        templates_dir=TEMPLATES,
        default_branch="main",
        tracker_system="github",
        tracker_url="",
        issue_id_example="#1",
        builder=LeafConfig(mode="command", family="claude"),
        reviewer=LeafConfig(mode="command", family="codex"),
        planner=LeafConfig(mode="command", family="claude", interactive=True,
                           agent="planner"),
        signoff=LeafConfig(mode="command", family="claude", interactive=True,
                           agent="signoff"),
        publisher=LeafConfig(mode="command", family="claude", interactive=True,
                             agent="publisher"),
        act=LeafConfig(mode="command", family="claude", interactive=True, agent="act"),
    )


def _load_hook():
    """The hook module, loaded in-process (tests/test_handoff.py loads it this way)."""
    spec = importlib.util.spec_from_file_location("handoff_guard_under_test", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tree(root: Path) -> dict[str, bytes]:
    """Every file under ``root`` with its bytes: the before/after of "report only"."""
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _items(err: str) -> list[str]:
    """The report's items: the ``  - …`` lines, without the bullet."""
    return [line[4:] for line in err.split("\n") if line.startswith("  - ")]


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = _cfg(self.tmp)

    def bundle(self, iid: str = "7") -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def reap(self, role: str, bundles: list[Path] | None = None, during=None,
             **kw) -> str:
        """Run one driver-registered session of ``role`` and return what its reap wrote
        to stderr. ``during(env)`` runs inside the session, standing in for the leaf.

        Every reap is also held to (e): no line it prints carries a raw non-printable
        character."""
        err = io.StringIO()
        with redirect_stderr(err):
            with handoff.session(self.cfg, role, bundles, **kw) as env:
                self.assertTrue(env, f"no contract registered for {role} — the fixture "
                                "must make it an interactive leaf")
                if during is not None:
                    during(env)
        out = err.getvalue()
        for line in out.split("\n"):
            self.assertTrue(line.isprintable(),
                            f"the reap printed a raw non-printable character: {line!r}")
        return out

    def hook_main(self, hook, env: dict, *args: str) -> tuple[int, str]:
        """Run the hook's CLI in-process the way the session runs it: with the session's
        ``env`` and this test's Config. Returns the exit code and all it printed."""
        out = io.StringIO()
        with mock.patch.object(hook, "_bootstrap", lambda: (handoff, self.cfg)), \
                mock.patch.dict(os.environ, env), \
                mock.patch.object(sys, "argv", ["handoff_guard.py", *args]), \
                redirect_stdout(out), redirect_stderr(out):
            rc = hook.main()
        return rc, out.getvalue()


# ----------------------------------------------------------------------------
# (a) No turn end is intercepted.
# ----------------------------------------------------------------------------
class NoTurnEndIsIntercepted(Base):
    def test_the_template_registers_no_turn_end_hook(self) -> None:
        settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
        hooks = settings.get("hooks") or {}
        for event in TURN_END_EVENTS:
            cmds = [h.get("command", "") for entry in hooks.get(event) or []
                    for h in entry.get("hooks", [])]
            self.assertEqual(cmds, [], f"settings.json registers a {event} hook: "
                             f"{cmds} — a turn end must hand control to the human")

    @contextlib.contextmanager
    def undischarged_session(self):
        """A registered sign-off session whose bundle has NO signoff-decision; yields
        ``(bundle, env)``. Its reap report is swallowed — (b) tests that separately."""
        d = self.bundle()
        with redirect_stderr(io.StringIO()), \
                handoff.session(self.cfg, "signoff", [d]) as env:
            self.assertTrue(env)
            yield d, env

    def test_the_hook_run_as_the_old_registration_ran_it_lets_the_turn_end(self) -> None:
        hook = _load_hook()
        # The contract check the hook runs must be LIVE, or the pre-fix hook takes its
        # "contract check unavailable — allowing the stop" branch and this test would be
        # a false green: point the hook's config lookup at this test's Config...
        live = mock.patch.object(hook, "_bootstrap", lambda: (handoff, self.cfg))
        with self.undischarged_session() as (d, env), live, \
                mock.patch.dict(os.environ, env):
            # ...and prove it, in the exact environment the Stop invocation sees: the
            # hook's own `--check` finds the contract undischarged.
            out = io.StringIO()
            argv = ["handoff_guard.py", "--check", d.name]
            with mock.patch.object(sys, "argv", argv), \
                    redirect_stdout(out), redirect_stderr(io.StringIO()):
                self.assertEqual(hook.main(), 1)
            self.assertIn("FAIL", out.getvalue())
            self.assertIn(leaves.SIGNOFF_DECISION, out.getvalue())

            # Now the way Claude Code ran the #331 registration: no arguments, the Stop
            # event on stdin — including the re-entry Claude Code flags with
            # stop_hook_active after a block.
            for active in (False, True):
                with self.subTest(stop_hook_active=active):
                    out, err = io.StringIO(), io.StringIO()
                    with mock.patch.object(sys, "argv", ["handoff_guard.py"]), \
                            mock.patch.object(sys, "stdin",
                                              io.StringIO(_stop_envelope(active))), \
                            redirect_stdout(out), redirect_stderr(err):
                        rc = hook.main()
                    self.assertEqual(rc, 0, "a turn end was blocked; stderr: "
                                     f"{err.getvalue()!r}")
                    self.assertEqual(err.getvalue(), "",
                                     "exit-0 stderr is still text for the model to read")
                    self.assertEqual(out.getvalue(), "")

    def test_the_old_registration_command_exits_0_silently(self) -> None:
        # The same, as a real process, exactly as the registration spelled it:
        # `python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/handoff_guard.py"`. The process
        # loads its OWN config from $CLAUDE_PROJECT_DIR/pdca.toml, which marks sign-off
        # interactive, so its contract check is live.
        (self.tmp / "pdca.toml").write_text(
            '[leaves.signoff]\nmode = "command"\nfamily = "claude"\n'
            'interactive = true\nagent = "signoff"\n', encoding="utf-8")
        with self.undischarged_session() as (d, env):
            penv = {k: v for k, v in os.environ.items()
                    if not k.startswith(("PDCA_", "CLAUDE_")) and k != "PYTHONPATH"}
            penv.update(env, CLAUDE_PROJECT_DIR=str(self.tmp), PYTHONPATH=str(SRC))

            def run(*args: str, stdin: str = "") -> subprocess.CompletedProcess:
                return subprocess.run([sys.executable, str(HOOK), *args], input=stdin,
                                      capture_output=True, text=True, env=penv,
                                      cwd=self.tmp, timeout=60)

            # Liveness: the process itself sees the unmet contract.
            check = run("--check", d.name)
            self.assertEqual(check.returncode, 1, check.stdout + check.stderr)
            self.assertIn("FAIL", check.stdout)
            self.assertIn(leaves.SIGNOFF_DECISION, check.stdout)

            stop = run(stdin=_stop_envelope())
            self.assertEqual(stop.returncode, 0,
                             f"a turn end was blocked: {stop.stderr!r}")
            self.assertEqual(stop.stderr, "")
            self.assertEqual(stop.stdout, "")


# ----------------------------------------------------------------------------
# (b) The contract is reported at reap.
# ----------------------------------------------------------------------------
class ReportedAtReap(Base):
    def test_signoff_without_a_decision(self) -> None:
        d = self.bundle()
        err = self.reap("signoff", [d])
        self.assertTrue(err.startswith("handoff: the signoff session"), err)
        self.assertEqual(_items(err)[0].split(" — ")[0],
                         f"{d.name}: {leaves.SIGNOFF_DECISION} is missing")

    def test_iterate_without_a_rationale(self) -> None:
        # Nothing downstream catches this one: sign-off records an empty Iteration
        # delta and the next Do is never told why its attempt was rejected.
        d = self.bundle()
        (d / leaves.SIGNOFF_DECISION).write_text("iterate-do\n", encoding="utf-8")
        err = self.reap("signoff", [d])
        self.assertIn(f"{d.name}: decision 'iterate-do' has no rationale", err)

    def test_planner_brief_with_an_empty_success_criterion(self) -> None:
        d = self.bundle()
        (d / "brief.md").write_text(_brief_text(criterion=""), encoding="utf-8")
        err = self.reap("planner", [d])
        self.assertTrue(err.startswith("handoff: the planner session"), err)
        self.assertIn(f"{d.name}: brief.md field 'success criterion' is empty", err)

    def test_planner_dependency_clause_with_a_readable_table(self) -> None:
        # The clause runs at the reap whenever the [[doctor.checks]] table can be read.
        d = self.bundle()
        (d / "brief.md").write_text(_brief_text(deps="`frobnicator` (build)"),
                                    encoding="utf-8")
        (self.tmp / "pdca.toml").write_text(
            '[[doctor.checks]]\nid = "protoc"\ncmd = "true"\n', encoding="utf-8")
        err = self.reap("planner", [d])
        self.assertIn(f"{d.name}: external dependency `frobnicator` is declared in the "
                      "brief but has no matching [[doctor.checks]] row", err)
        (self.tmp / "pdca.toml").write_text(
            '[[doctor.checks]]\nid = "frobnicator"\ncmd = "exit 3"\n'
            'hint = "install it"\n', encoding="utf-8")
        err = self.reap("planner", [d])
        self.assertIn(f"{d.name}: external dependency `frobnicator` is registered but "
                      "absent on this host", err)
        self.assertNotIn("could not be read", err)

    def test_publisher_without_its_artifacts(self) -> None:
        d = self.bundle()
        err = self.reap("publisher", [d])
        self.assertIn("publisher", err)
        self.assertIn(f"{d.name}: commit-msg.txt is missing", err)
        self.assertIn(f"{d.name}: pr-description.md is missing", err)

    def test_act_that_named_no_entry(self) -> None:
        err = self.reap("act")
        self.assertIn("act session", err)
        self.assertIn("act-log", err)

    def test_each_undischarged_bundle_of_a_batch_is_named(self) -> None:
        done, open_ = self.bundle("7"), self.bundle("8")
        (done / leaves.SIGNOFF_DECISION).write_text("accept\n", encoding="utf-8")
        err = self.reap("signoff", [done, open_])
        self.assertIn(f"{open_.name}: {leaves.SIGNOFF_DECISION} is missing", err)
        self.assertNotIn(f"{done.name}:", err)

    def test_the_report_names_the_bundles_the_driver_registered(self) -> None:
        # The session can write its own scratch file; a leaf that empties the bundle
        # list there must not turn the report into a vaguer one.
        d = self.bundle()

        def rewrite(env: dict) -> None:
            Path(env[handoff.ENV_STATE]).write_text('{"bundles": []}', encoding="utf-8")

        err = self.reap("signoff", [d], during=rewrite)
        self.assertIn(f"{d.name}: {leaves.SIGNOFF_DECISION} is missing", err)

    def test_a_discharged_contract_prints_nothing(self) -> None:
        d = self.bundle()
        (d / leaves.SIGNOFF_DECISION).write_text(
            "iterate-do\nthe probe hid the cause; remove it\n", encoding="utf-8")
        self.assertEqual(self.reap("signoff", [d]), "")
        (d / "brief.md").write_text(_brief_text(deps="`frobnicator` (build)"),
                                    encoding="utf-8")
        (self.tmp / "pdca.toml").write_text(
            '[[doctor.checks]]\nid = "frobnicator"\ncmd = "true"\nhint = "install it"\n',
            encoding="utf-8")
        self.assertEqual(self.reap("planner", [d]), "")


# ----------------------------------------------------------------------------
# (c) Report only.
# ----------------------------------------------------------------------------
class ReportOnly(Base):
    def test_the_reap_changes_nothing(self) -> None:
        d = self.bundle()
        (d / "brief.md").write_text(_brief_text(criterion=""), encoding="utf-8")
        (d / leaves.SIGNOFF_DECISION).write_text("iterate-do\n", encoding="utf-8")
        (d / "SUMMARY.md").write_text("# SUMMARY\n", encoding="utf-8")
        sessions = (("planner", None), ("signoff", None), ("publisher", None),
                    ("act", None), ("planner", _BROKEN_TABLES[0][1]))
        for role, table in sessions:
            with self.subTest(role=role, doctor_table="broken" if table else "absent"):
                if table:
                    (self.tmp / "pdca.toml").write_bytes(table)
                before = _tree(self.tmp)
                err = self.reap(role, None if role == "act" else [d])
                self.assertTrue(err.strip(),
                                "fixture must leave the contract undischarged")
                self.assertEqual(_tree(self.tmp), before,
                                 "the reap wrote, moved or deleted a file")

    def test_a_check_that_fails_is_one_line_after_the_abandon_reason(self) -> None:
        # Real inputs no longer break the whole check (each bundle and the doctor table
        # are contained, see (d)), so the failure is injected: stop_problems raises. The
        # typed reason is still printed first, then ONE line, and nothing raises.
        d = self.bundle()
        state_files: list[Path] = []

        def abandon(env: dict) -> None:
            state_files.append(Path(env[handoff.ENV_STATE]))
            handoff.record_abandon(state_files[0], "out of time")

        with mock.patch.object(handoff, "stop_problems",
                               side_effect=RuntimeError("the check broke")):
            lines = self.reap("signoff", [d], during=abandon).strip().split("\n")
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("deliberately abandoned — out of time", lines[0])
        self.assertIn("could not check the signoff session's exit contract", lines[1])
        self.assertIn("RuntimeError: the check broke", lines[1])
        self.assertFalse(state_files[0].exists(),
                         "the scratch file must still be removed")

    def test_a_scratch_file_the_session_broke_raises_nothing(self) -> None:
        # The session can write its scratch file, or replace it. Neither may turn the
        # reap into an exception out of the context manager, and the bundles the driver
        # registered are still reported.
        d = self.bundle()

        def too_deep(env: dict) -> None:  # json.loads raises RecursionError on it
            Path(env[handoff.ENV_STATE]).write_text("[" * 100_000 + "]" * 100_000,
                                                    encoding="utf-8")

        def a_directory(env: dict) -> None:  # unlinking a directory raises OSError
            spath = Path(env[handoff.ENV_STATE])
            spath.unlink()
            spath.mkdir()

        cases = (("JSON nested too deep to parse", too_deep, None),
                 ("a directory in its place", a_directory,
                  "could not remove the signoff session's scratch file"))
        for label, during, note in cases:
            with self.subTest(scratch_file=label):
                err = self.reap("signoff", [d], during=during)
                self.assertIn(f"{d.name}: {leaves.SIGNOFF_DECISION} is missing", err)
                if note:
                    self.assertIn(note, err)

    def test_the_leaf_s_own_exception_passes_through_untouched(self) -> None:
        d = self.bundle()
        boom = RuntimeError("the leaf died")
        with self.assertRaises(RuntimeError) as caught:
            with redirect_stderr(io.StringIO()):
                with handoff.session(self.cfg, "signoff", [d]):
                    raise boom
        self.assertIs(caught.exception, boom)


# ----------------------------------------------------------------------------
# (d) The report hides nothing.
# ----------------------------------------------------------------------------
class ReportHidesNothing(Base):
    # (d)(1) An abandon never hides the list.
    def test_an_abandon_reason_is_reported_with_the_problems(self) -> None:
        # A batch sign-off stopped early on purpose: issue_7 got `iterate-do` with no
        # rationale, issue_8 got nothing, then the session ran `--abandon "out of
        # time"`. Nothing is blocked at the reap, so hiding the list would only keep it
        # from the human: the reason is printed first and every unmet item follows.
        hook = _load_hook()
        first, second = self.bundle("7"), self.bundle("8")
        (first / leaves.SIGNOFF_DECISION).write_text("iterate-do\n", encoding="utf-8")
        ran: list[tuple[int, str]] = []

        def abandon(env: dict) -> None:
            ran.append(self.hook_main(hook, env, "--abandon", "out of time"))

        err = self.reap("signoff", [first, second], during=abandon)
        self.assertEqual(ran[0][0], 0, ran)  # the typed reason was recorded
        reason = "handoff: the signoff session was deliberately abandoned — out of time"
        self.assertIn(reason, err)
        for item in (f"{first.name}: decision 'iterate-do' has no rationale",
                     f"{second.name}: {leaves.SIGNOFF_DECISION} is missing"):
            with self.subTest(item=item):
                self.assertIn(item, err)
                self.assertLess(err.index(reason), err.index(item),
                                "the abandon reason comes first, then the list")

    def test_an_abandon_with_nothing_unmet_prints_only_the_reason(self) -> None:
        d = self.bundle()
        (d / leaves.SIGNOFF_DECISION).write_text("accept\n", encoding="utf-8")

        def abandon(env: dict) -> None:
            handoff.record_abandon(Path(env[handoff.ENV_STATE]), "stopped after all")

        err = self.reap("signoff", [d], during=abandon)
        self.assertEqual(err.splitlines(), ["handoff: the signoff session was "
                                            "deliberately abandoned — stopped after all"])

    # (d)(2) One "was it abandoned?" test.
    def test_a_blank_abandon_value_is_no_reason(self) -> None:
        # The session can write its own scratch file, so a blank `abandoned` can land
        # there without --abandon (which refuses a blank reason). A blank value is no
        # reason: no abandon line is printed, and the problem list is printed as always.
        d = self.bundle()
        for blank in ("", " ", "\n", " \t\n"):
            with self.subTest(abandoned=blank):
                def write_blank(env: dict, blank: str = blank) -> None:
                    p = Path(env[handoff.ENV_STATE])
                    state = json.loads(p.read_text(encoding="utf-8"))
                    p.write_text(json.dumps({**state, "abandoned": blank}),
                                 encoding="utf-8")

                err = self.reap("signoff", [d], during=write_blank)
                self.assertIn(f"{d.name}: {leaves.SIGNOFF_DECISION} is missing", err)
                self.assertNotIn("deliberately abandoned", err)

    # (d)(3) One bundle's failure never hides another's.
    def test_a_bundle_that_cannot_be_checked_does_not_hide_the_others(self) -> None:
        # A batch where one bundle's check RAISES and another bundle has a problem of
        # the kind only this report catches. The raising bundle is named as
        # could-not-check and the other bundle's problem is still reported, whichever
        # of the two the driver registered first. The failures are real ones: the
        # checks read these artifacts as UTF-8 text files, so a file saved in another
        # encoding, or a directory in the file's place, makes them raise.
        cases = (
            # role, artifact, the broken bundle's artifact (bytes; None = a directory
            # in its place), the other bundle's artifact, the problem it must report
            ("signoff", leaves.SIGNOFF_DECISION,
             "iterate-do\nthe café probe hid the cause\n".encode("cp1252"),
             "iterate-do\n", "decision 'iterate-do' has no rationale"),
            ("signoff", leaves.SIGNOFF_DECISION, None,
             "iterate-do\n", "decision 'iterate-do' has no rationale"),
            ("planner", "brief.md",
             _AUTHORED_BRIEF.replace("something", "a café").encode("cp1252"),
             _brief_text(criterion=""), "brief.md field 'success criterion' is empty"),
        )
        for i, (role, artifact, raw, other_text, problem) in enumerate(cases):
            how = "a directory" if raw is None else "not UTF-8"
            with self.subTest(role=role, broken_artifact=how):
                broken = self.bundle(f"{10 * i + 7}")
                other = self.bundle(f"{10 * i + 8}")
                if raw is None:
                    (broken / artifact).mkdir()
                else:
                    (broken / artifact).write_bytes(raw)
                (other / artifact).write_text(other_text, encoding="utf-8")
                with self.assertRaises(Exception) as caught:  # the check really raises
                    handoff.check_bundle(role, broken, self.cfg)
                error = type(caught.exception).__name__
                for order in ([broken, other], [other, broken]):
                    err = self.reap(role, order)
                    self.assertIn(f"{other.name}: {problem}", err)
                    self.assertIn(f"{broken.name}: could not check ({error}: ", err)
                    self.assertNotIn("could not check the", err)  # not the whole check

    # (d)(4) A broken [[doctor.checks]] table is reported, and hides nothing.
    def _three_briefs(self) -> tuple[Path, Path, Path]:
        """Briefs with an empty Success criterion, an empty Repo + branch target, and
        none missing; two of them declare a dependency the table would be asked about."""
        no_criterion, no_target, clean = (self.bundle(i) for i in ("7", "8", "9"))
        (no_criterion / "brief.md").write_text(
            _brief_text(criterion="", deps="`frobnicator` (build)"), encoding="utf-8")
        (no_target / "brief.md").write_text(_brief_text(target=""), encoding="utf-8")
        (clean / "brief.md").write_text(_brief_text(deps="`frobnicator` (build)"),
                                        encoding="utf-8")
        return no_criterion, no_target, clean

    def assert_one_table_line_and_every_brief_checked(self, error: str) -> None:
        no_criterion, no_target, clean = briefs = self._three_briefs()
        err = self.reap("planner", list(briefs))
        table = [line for line in err.split("\n") if "[[doctor.checks]]" in line]
        self.assertEqual(len(table), 1, err)
        self.assertIn(str(self.tmp / "pdca.toml"), table[0])
        self.assertIn(f"({error}: ", table[0])
        self.assertIn("dependency clause was not checked", table[0])
        # Every check that does not need the table still ran, for every brief, and is
        # reported next to that one line...
        items = _items(err)
        self.assertEqual(len(items), 3, err)
        self.assertTrue(items[1].startswith(
            f"{no_criterion.name}: brief.md field 'success criterion' is empty"), err)
        self.assertTrue(items[2].startswith(
            f"{no_target.name}: brief.md field 'repo + branch target' is empty"), err)
        # ...and nothing claims more went unchecked than the dependency clause.
        self.assertNotIn("could not check", err)
        self.assertNotIn("frobnicator", err)

    def test_a_broken_doctor_table_is_one_line_and_every_brief_is_still_checked(
            self) -> None:
        for label, raw in _BROKEN_TABLES:
            with self.subTest(pdca_toml=label):
                (self.tmp / "pdca.toml").write_bytes(raw)
                clean = self._three_briefs()[2]
                # The fixture really breaks the read the dependency clause does — and
                # /handoff, which checks the whole contract, meets that failure exactly
                # as it did before the reap learned to contain it.
                with self.assertRaises(Exception) as caught:
                    handoff.check_planner(clean, self.cfg)
                error = type(caught.exception)
                with self.assertRaises(error), redirect_stdout(io.StringIO()):
                    handoff.run_check(self.cfg, clean.name,
                                      environ={handoff.ENV_ROLE: "planner"})
                self.assert_one_table_line_and_every_brief_checked(error.__name__)

    def test_a_pdca_toml_that_does_not_parse_is_a_broken_table_too(self) -> None:
        # The dependency clause's read does not raise on it: it falls back to the rows
        # loaded when the run started (none in this Config), and /handoff still checks
        # against those, as before. The reap must not pass that off as a check of the
        # table the session left behind.
        (self.tmp / "pdca.toml").write_bytes(_UNPARSEABLE_TOML)
        with self.assertRaises(tomllib.TOMLDecodeError):
            tomllib.loads(_UNPARSEABLE_TOML.decode("utf-8"))
        clean = self._three_briefs()[2]
        out = io.StringIO()
        with redirect_stdout(out):
            rc = handoff.run_check(self.cfg, clean.name,
                                   environ={handoff.ENV_ROLE: "planner"})
        self.assertEqual(rc, 1, out.getvalue())
        self.assertIn("`frobnicator` is declared in the brief but has no matching",
                      out.getvalue())
        self.assert_one_table_line_and_every_brief_checked("TOMLDecodeError")

    def test_a_broken_doctor_table_is_reported_at_every_planner_reap(self) -> None:
        # Whether the driver registered bundles or not, and whether any brief exists or
        # not: a batch that rightly left every issue UNPLANNED must still hear about it.
        (self.tmp / "pdca.toml").write_bytes(_BROKEN_TABLES[0][1])
        unplanned = [self.bundle("7"), self.bundle("8")]
        briefed = self.bundle("9")
        (briefed / "brief.md").write_text(_AUTHORED_BRIEF, encoding="utf-8")

        def named_its_work(env: dict) -> None:
            handoff.record_pass(Path(env[handoff.ENV_STATE]), briefed.name)

        sessions = (
            # what the session was, its bundles, session kwargs, the leaf, other items
            ("an id-seeded batch that left every issue UNPLANNED", unplanned,
             {"require_artifact": False}, None, []),
            ("a single Plan whose brief is discharged", [briefed], {}, None, []),
            ("a CSV batch that named its work through /handoff", None, {},
             named_its_work, []),
            ("a CSV batch that named nothing", None, {}, None,
             ["the planner session registered no bundle set at spawn"]),
        )
        for label, bundles, kw, during, others in sessions:
            with self.subTest(session=label):
                err = self.reap("planner", bundles, during=during, **kw)
                items = _items(err)
                self.assertEqual(len(items), 1 + len(others), err)
                self.assertIn("[[doctor.checks]] table could not be read", items[0])
                self.assertIn("dependency clause was not checked", items[0])
                for expected, item in zip(others, items[1:]):
                    self.assertIn(expected, item)
                self.assertNotIn("could not check", err)


# ----------------------------------------------------------------------------
# (e) Nothing the session wrote is printed raw.
# ----------------------------------------------------------------------------
class NothingPrintedRaw(Base):
    # ESC[8m conceals the terminal text that follows, U+202E reverses it, and a newline
    # followed by "  - " would forge a report item.
    FORGED = "out of time\x1b[8m\n  - issue_7: all clear\N{RIGHT-TO-LEFT OVERRIDE}"

    def test_an_abandon_reason_cannot_hide_the_lines_after_it(self) -> None:
        hook = _load_hook()
        first, second = self.bundle("7"), self.bundle("8")
        (first / leaves.SIGNOFF_DECISION).write_text("iterate-do\n", encoding="utf-8")
        ran: list[tuple[int, str]] = []

        def abandon(env: dict) -> None:
            ran.append(self.hook_main(hook, env, "--abandon", self.FORGED))

        err = self.reap("signoff", [first, second], during=abandon)
        self.assertEqual(ran[0][0], 0, ran)
        lines = err.split("\n")
        self.assertEqual(lines[0], "handoff: the signoff session was deliberately "
                         "abandoned — out of time\\x1b[8m\\n  - "
                         "issue_7: all clear\\u202e")
        self.assertNotIn("  - issue_7: all clear", lines)  # no forged item
        self.assertEqual([item.split(" — ")[0] for item in _items(err)],
                         [f"{first.name}: decision 'iterate-do' has no rationale below "
                          "the token",
                          f"{second.name}: {leaves.SIGNOFF_DECISION} is missing"])

    def test_a_dependency_token_quoted_from_a_brief_is_escaped(self) -> None:
        d = self.bundle()
        (d / "brief.md").write_text(_brief_text(deps="`frob\x1b[8m` (build)"),
                                    encoding="utf-8")
        err = self.reap("planner", [d])
        self.assertIn(f"{d.name}: external dependency `frob" + r"\x1b[8m` is declared",
                      err)


# ----------------------------------------------------------------------------
# (g) The text the model reads promises no enforcement.
# ----------------------------------------------------------------------------
# The claim, in the words #331 used and in others: a hook or guard enforces, blocks or
# requires the contract, or the turn/session may (not) end on it.
_ENFORCEMENT_CLAIM = re.compile(
    r"\bstop[\s-]*hook"
    r"|\b(?:hook|guard)\b[^.;]*?\b(?:enforc|block|requir|re-?check|verif|allow|let)\w*"
    r"|\benforc\w*[^.;]*?\b(?:turn|session) end"
    r"|\b(?:turn|session) (?:may|can|cannot|can't|will)(?: not| now)? end\b"
    r"|\bescape hatch\b",
    re.IGNORECASE)

# What #331 told the model, one line per surface, then the same claim reworded. Every one
# must trip the rule, so the rule cannot quietly stop matching what it exists to catch.
_RETIRED_CLAIMS = (
    "with its detect cmd passing; the Stop hook enforces it.",
    "the Stop hook re-checks every briefed bundle before the session may end.",
    "how the session names its work; the Stop hook requires them).",
    "and the Stop hook blocks the session ending on a missing/malformed decision.",
    "the Stop hook checks every listed bundle before the session may end, and a "
    "deliberate early stop is recorded via the --abandon escape hatch it names.",
    "the Stop hook enforces the same contract when the session ends.",
    "The Stop hook enforces this same contract when the\nsession ends;",
    "handoff_guard: abandonment recorded — the session may now end; the driver will "
    "report the reason",
    "a hook blocks the turn from ending until brief.md exists",
    "the guard will not let you finish without a decision",
    "the contract is enforced when the session ends",
    "the session cannot end until /handoff passes",
)


class ModelFacingTextPromisesNoEnforcement(Base):
    def _texts(self) -> dict[str, str]:
        """Everything the model is told about the exit contract: the rendered /handoff
        command body, each contract leaf's prompt, and the hook's own replies."""
        d = self.bundle()
        (d / "brief.md").write_text(_AUTHORED_BRIEF, encoding="utf-8")
        texts = {
            "the /handoff command body": COMMAND.read_text(encoding="utf-8"),
            "_plan_prompt": leaves._plan_prompt(self.cfg, None, d),
            "_plan_batch_prompt (ids)": leaves._plan_batch_prompt(self.cfg, None, ["7"]),
            "_plan_batch_prompt (CSV)": leaves._plan_batch_prompt(self.cfg, None, None),
            "_signoff_prompt": leaves._signoff_prompt(d),
            "_signoff_batch_prompt": leaves._signoff_batch_prompt([d]),
            "_publish_prompt": leaves._publish_prompt(d, self.cfg),
            "_act_prompt": leaves._act_prompt(self.cfg, "2026-09-16", bundles=[]),
        }
        hook = _load_hook()
        with redirect_stderr(io.StringIO()), \
                handoff.session(self.cfg, "signoff", [d]) as env:
            texts["handoff_guard.py --check (FAIL)"] = \
                self.hook_main(hook, env, "--check", d.name)[1]
            texts["handoff_guard.py --abandon"] = \
                self.hook_main(hook, env, "--abandon", "out of time")[1]
        return texts

    def test_the_rule_catches_every_retired_claim(self) -> None:
        for claim in _RETIRED_CLAIMS:
            with self.subTest(claim=claim):
                self.assertRegex(" ".join(claim.split()), _ENFORCEMENT_CLAIM)

    def test_no_text_the_model_reads_says_a_hook_enforces_the_contract(self) -> None:
        for name, text in self._texts().items():
            with self.subTest(text=name):
                flat = " ".join(text.split())
                self.assertTrue(flat, f"{name} is empty — nothing was checked")
                found = _ENFORCEMENT_CLAIM.search(flat)
                if found:
                    self.fail(f"{name} still tells the model the exit contract is "
                              "enforced: …"
                              f"{flat[max(0, found.start() - 60):found.end() + 60]}…")


# ----------------------------------------------------------------------------
# (h) /handoff (--check) and --abandon keep working.
# ----------------------------------------------------------------------------
class SelfCheckAndAbandonUnchanged(Base):
    def test_check_verifies_one_named_id(self) -> None:
        hook, d = _load_hook(), self.bundle()
        with redirect_stderr(io.StringIO()):
            with handoff.session(self.cfg, "signoff", [d]) as env:
                rc, out = self.hook_main(hook, env, "--check", d.name)
                self.assertEqual((rc, "FAIL" in out), (1, True), out)
                (d / leaves.SIGNOFF_DECISION).write_text("accept\n", encoding="utf-8")
                rc, out = self.hook_main(hook, env, "--check", d.name)
                self.assertEqual((rc, "PASS" in out), (0, True), out)
                rc, _out = self.hook_main(hook, env, "--check")  # ids are required
                self.assertEqual(rc, 2)

    def test_abandon_records_a_typed_reason_the_reap_reports(self) -> None:
        hook, d = _load_hook(), self.bundle()
        results: list[tuple[int, str]] = []

        def abandon(env: dict) -> None:
            results.append(self.hook_main(hook, env, "--abandon", ""))  # needs a reason
            results.append(self.hook_main(hook, env, "--abandon", "no decision today"))

        before = _tree(d)
        err = self.reap("signoff", [d], during=abandon)
        self.assertEqual([rc for rc, _ in results], [2, 0], results)
        self.assertIn("no decision today", err)
        self.assertEqual(_tree(d), before, "--abandon writes nothing into the bundle")


if __name__ == "__main__":
    unittest.main()
