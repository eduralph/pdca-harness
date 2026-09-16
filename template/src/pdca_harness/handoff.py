"""The interactive leaves' checked exit contract (issue #331).

Today the driver's entire completion signal for an interactive leaf is process exit —
``leaves._invoke`` runs ``subprocess.run(argv + [seed], ...)`` with no ``check=`` and
captures nothing — so "the human pressed Ctrl-D" and "the leaf discharged its contract"
are the same event, and a malformed/absent artifact is discovered later, far from the
cause. This module gives each interactive leaf a *checked* boundary:

* :func:`run_check` — the ``/handoff <issue_id>`` verdict: verify the CURRENT leaf's
  contract for ONE named id and report PASS/FAIL. Ids are REQUIRED — there is no scan
  mode (prototype finding, getwyrd/wyrd-pdca#166: a scan judges old bundles against a
  contract that postdates them; a named id only ever judges what this session worked).
* :func:`stop_problems` — the session-end verdict, judged when the driver REAPS the
  leaf's process (:func:`session`) and reported to the human on stderr
  (:func:`report_at_reap`). It re-reads the artifacts of every bundle the driver
  registered; where the driver registered none (the CSV/default batch planner, Act) it
  requires a passing ``/handoff`` instead. The report never blocks, never reopens the
  session and never changes bundle state; a deliberate abandon (:func:`record_abandon`)
  is printed first and hides nothing. It is not a turn-end check (issue #534): #331 ran
  it as a Claude Code ``Stop`` hook, but Stop fires every time the agent finishes a
  TURN, and a Stop hook's exit 2 sends its text back to the model instead of handing
  the turn to the human, so a leaf that asked the human a question could never reach
  them.
* :func:`session` — the driver-side registration: env for the spawned leaf naming its
  role and a session-state scratch file (the act-log baseline where authorship must be
  distinguished, the abandon channel, the record of passed ``/handoff`` runs). The
  scratch file lives OUTSIDE the bundle: the gate's verdict is exit status + report,
  never a bundle artifact (prototype finding — no ``handoff.json``).

Which contract applies is derived from the RENDER — the ``interactive = true`` leaves
and their ``agent`` names in ``pdca.toml`` (:func:`contracts`) — not from a hardcoded
leaf list. Per-field brief checks go through :func:`brief.whole_field`, because the
measured corpus (85 bundles) writes multi-line values the line-based
``brief.parse_fields`` reads as empty; and only the fields every template mandates AND
the corpus actually satisfies are required (``Test file`` is legitimately empty in 7
bundles, ``Falsifiability`` absent in 52/85 — neither is required here).
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import sys
import tempfile
import tomllib
from pathlib import Path

from .config import Config, LeafConfig

#: Env var naming the role of the currently running interactive leaf (driver-set).
ENV_ROLE = "PDCA_HANDOFF_ROLE"
#: Env var pointing at the session-state scratch file (driver-created, driver-removed).
ENV_STATE = "PDCA_HANDOFF_STATE"
#: Scratch-file prefix — dot-prefixed and gitignored, like the seed spill (#313).
STATE_PREFIX = ".pdca-handoff-"


# ----------------------------------------------------------------------------
# Which contract applies — derived from the render, not hardcoded.
# ----------------------------------------------------------------------------
def interactive_roles(cfg: Config) -> dict[str, str]:
    """``{role: agent name}`` for every leaf the RENDER marks ``interactive = true``.

    Introspected from the ``Config`` dataclass fields whose value is a
    :class:`LeafConfig`, so an instance that renders a leaf non-interactive sheds its
    contract with no code change (issue #331 criterion f).
    """
    out: dict[str, str] = {}
    for f in dataclasses.fields(cfg):
        v = getattr(cfg, f.name, None)
        if isinstance(v, LeafConfig) and v.interactive:
            out[f.name] = v.agent or f.name
    return out


def contracts(cfg: Config) -> dict[str, str]:
    """The subset of :func:`interactive_roles` that has a defined exit contract.

    The contract *checks* are per-role code (below); whether one is ACTIVE derives from
    the render. An interactive leaf without a defined contract (e.g. the splitter) is
    simply unchecked — never blocked on a contract it does not have.
    """
    checkable = set(_BUNDLE_CONTRACTS) | {"act"}
    return {r: a for r, a in interactive_roles(cfg).items() if r in checkable}


# ----------------------------------------------------------------------------
# The per-role contract checks. Each returns a list of problems; empty ⇒ PASS.
# ----------------------------------------------------------------------------
def _unfilled(value: str) -> bool:
    """A field value that is absent or still the template's ``<…>`` placeholder.

    ``whole_field`` returns values raw, so a multi-line placeholder arrives whole here —
    a leading ``<`` is the template's, never an authored value's."""
    v = value.strip()
    return not v or v.startswith("<")


def check_planner(d: Path, cfg: Config, *, allow_absent: bool = False,
                  dependencies: bool = True) -> list[str]:
    """The Plan exit contract: an AUTHORED ``brief.md`` whose declared external
    dependencies are registered AND present (#333/#340 — the same probe the
    pre-dispatch guard runs, so the two verdicts cannot drift apart).

    ``allow_absent`` is the id-seeded batch wrinkle: the batch prompt documents "leave
    it UNPLANNED (write no brief.md) and say why" as a legitimate outcome, so the
    session-end check (:func:`stop_problems`, reported when the driver reaps the
    session) passes a wholly-absent brief — a brief that EXISTS malformed never does.

    ``dependencies=False`` skips the dependency clause and nothing else. Only the reap
    passes it, when the ``[[doctor.checks]]`` table the clause reads cannot be read
    (:func:`stop_problems` reports that once for the session); ``/handoff`` always
    checks the whole contract.
    """
    from . import brief as _brief  # local: keep this module import-light for the hook
    from . import doctor as _doctor
    bp = d / "brief.md"
    if not bp.exists():
        if allow_absent:
            return []
        return ["brief.md is missing — the Plan contract is an authored brief"]
    if _brief.is_placeholder(bp):
        return ["brief.md is still an unfilled template copy (Slug is a placeholder) — "
                "author it or remove it"]
    problems: list[str] = []
    # The fields EVERY brief template mandates and the measured corpus satisfies —
    # read via whole_field (multi-line values, #336), never line-based parse_fields.
    for label in ("slug", "success criterion", "repo + branch target"):
        if _unfilled(_brief.whole_field(bp, label)):
            problems.append(f"brief.md field '{label}' is empty or an unfilled "
                            "placeholder — it is required by every brief template")
    # The dependency clause (#331 layer over #333/#340): every backticked token must
    # name a registered [[doctor.checks]] row whose detect cmd exits 0; an annotated
    # `(no-check: …)` token yields no token at all and is exempt by construction.
    if dependencies:
        problems += _doctor.unregistered_dependencies(bp, cfg)
        problems += _doctor.failing_dependencies(bp, cfg)
    return problems


def check_signoff(d: Path, cfg: Config) -> list[str]:
    """The sign-off exit contract: ``signoff-decision`` carries one valid token, plus a
    rationale below it for every non-accept decision (that rationale IS the session
    carry-forward the driver captures — see :data:`state.SESSION_CARRY`)."""
    from . import leaves as _leaves  # local: leaves imports this module at top level
    p = d / _leaves.SIGNOFF_DECISION
    tokens = ", ".join(sorted(_leaves.VALID_DECISIONS))
    if not p.exists():
        return [f"{_leaves.SIGNOFF_DECISION} is missing — write the agreed decision "
                f"(one of: {tokens}) as its first line"]
    token = _leaves.signoff_decision(d)
    if not token:
        first = (p.read_text(encoding="utf-8").splitlines() or [""])[0].strip()
        return [f"{_leaves.SIGNOFF_DECISION} first line {first!r} is not a valid "
                f"decision token (one of: {tokens})"]
    if token != "accept" and not _leaves.signoff_rationale(d):
        return [f"decision '{token}' has no rationale below the token — write WHY "
                "(why rejected / what to change, or why discontinued); the driver "
                "carries it into the next attempt's brief"]
    return []


def check_publisher(d: Path, cfg: Config) -> list[str]:
    """The publish exit contract: both contribution artifacts exist, non-empty, and
    pass the instance's own deterministic lint (``cli.contribution_problems`` — the
    same rules as the T4 ``contribcheck`` gate, reused rather than re-declared)."""
    from . import cli as _cli
    from . import publish as _publish
    problems: list[str] = []
    for name in (_publish.COMMIT_MSG, _publish.PR_BODY):
        p = d / name
        if not p.is_file() or not p.read_text(encoding="utf-8").strip():
            problems.append(f"{name} is missing or empty — the publish contract is "
                            "exactly these two artifacts")
    if not problems:
        problems += _cli.contribution_problems(d)
    return problems


def check_act(cfg: Config, entry: str, baseline: dict) -> list[str]:
    """The Act exit contract: the session NAMES the act-log entry it wrote.

    ``entry`` is the id the session hands ``/handoff`` (the entry's date). ``baseline``
    is the driver's session-start snapshot of ``process/act-log.md`` — supplied by the
    driver because an end-of-session command structurally cannot take one — and is what
    distinguishes an entry THIS session wrote from one that predates it.
    """
    if not entry.strip():
        return ["an entry id is required — run `/handoff <entry-date>` naming the "
                "act-log entry this session wrote (there is no scan mode)"]
    log = cfg.process_dir / "act-log.md"
    try:
        text = log.read_text(encoding="utf-8") if log.exists() else ""
    except OSError:
        text = ""
    if entry not in text:
        return [f"process/act-log.md has no entry containing '{entry}' — append the "
                "dated entry (even a 'no delta warranted' one) and name it here"]
    if baseline:
        if _sha(text) == baseline.get("act_log_sha"):
            return ["process/act-log.md is unchanged since this session started — the "
                    f"entry '{entry}' predates the session; append THIS session's entry"]
        prev_len = baseline.get("act_log_len")
        if isinstance(prev_len, int) and 0 <= prev_len <= len(text) \
                and entry not in text[prev_len:]:
            return [f"'{entry}' appears only in act-log text that predates this session "
                    "— name the entry THIS session appended"]
    return []


_BUNDLE_CONTRACTS = {
    "planner": check_planner,
    "signoff": check_signoff,
    "publisher": check_publisher,
}


def check_bundle(role: str, d: Path, cfg: Config, **kw) -> list[str]:
    """Dispatch the bundle-scoped contract for ``role``; unknown role ⇒ a problem
    naming it (never a silent pass for a contract that was asked for by name)."""
    fn = _BUNDLE_CONTRACTS.get(role)
    if fn is None:
        return [f"no exit contract is defined for role '{role}'"]
    return fn(d, cfg, **kw) if role == "planner" else fn(d, cfg)


# ----------------------------------------------------------------------------
# Session state — the driver-owned channel (env + a scratch file OUTSIDE the bundle).
# ----------------------------------------------------------------------------
def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_json(path: Path | None) -> dict:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_state(environ: dict | None = None) -> dict:
    """The current session's state dict, from :data:`ENV_STATE` — ``{}`` if none."""
    env = environ if environ is not None else os.environ
    raw = env.get(ENV_STATE, "")
    return _read_json(Path(raw)) if raw else {}


def _update_state(path: Path, **fields) -> None:
    data = _read_json(path)
    data.update(fields)
    try:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass  # best-effort — the artifact checks stand on their own


def record_pass(path: Path, ident: str) -> None:
    """Record a passed ``/handoff <ident>`` — how the session NAMES its work where the
    driver could not register a bundle set at spawn (CSV-batch Plan, Act)."""
    data = _read_json(path)
    passed = list(data.get("passed") or [])
    if ident not in passed:
        passed.append(ident)
    _update_state(path, passed=passed)


def record_abandon(path: Path, reason: str) -> None:
    """The deliberate abandon: a TYPED reason why the session stops with its contract
    unmet, recorded in the driver's session channel (never the bundle). When the driver
    reaps the session it prints the reason first and then everything
    :func:`stop_problems` finds. Nothing blocks at the session end, so an abandon
    explains the gap to the human and never hides it (#534)."""
    _update_state(path, abandoned=reason.strip() or "(no reason given)")


def _abandon_reason(state: dict) -> str:
    """The abandon reason recorded in ``state``, stripped; ``""`` means not abandoned.

    This is the ONE "was the session abandoned" test (:func:`report_at_reap` uses it),
    and all it decides is whether the reap prints an abandon line. It never decides
    whether the problem list is printed: :func:`stop_problems` does not look at
    ``abandoned`` at all. The session can write its scratch file itself, so a blank
    ``abandoned`` can get there without :func:`record_abandon`; a blank value is no
    reason and prints no abandon line.
    """
    return str(state.get("abandoned") or "").strip()


def _error_text(exc: Exception) -> str:
    """``Type: message`` on one line: how the reap names a check that raised — one
    bundle's (:func:`stop_problems`), the ``[[doctor.checks]]`` table
    (:func:`_doctor_table_problem`), or the whole check (:func:`report_at_reap`).
    """
    return " ".join(f"{type(exc).__name__}: {exc}".split())


def _printable(text: str) -> str:
    """``text`` with every non-printable character escaped: ``\\x1b``, ``\\n``,
    ``\\u202e`` (#534).

    The reap prints text a session controls — its abandon reason, and problem items that
    quote a brief (a dependency token) or an error message. Printed raw, a terminal
    escape such as ``ESC[8m`` (conceal) could hide every line after it, and a newline
    could forge a report line. :meth:`str.isprintable` decides: control and format
    characters and every separator but the space are escaped.
    """
    return "".join(c if c.isprintable() else c.encode("unicode_escape").decode("ascii")
                   for c in text)


def _emit(lines: list[str]) -> None:
    """Print the reap's ``lines`` to stderr, each through :func:`_printable`. Never
    raises: a stderr that cannot be written leaves nowhere to report anything."""
    try:
        for line in lines:
            print(_printable(line), file=sys.stderr)
    except Exception:  # noqa: BLE001 — the report must never break the leaf
        pass


def _doctor_table_problem(cfg: Config) -> str:
    """One report item when the ``[[doctor.checks]]`` table cannot be read, else ``""``.

    The planner contract's dependency clause reads that table from ``pdca.toml`` as it
    is NOW (:func:`doctor.registered_ids`), and the session may have edited it. The reap
    reads it the same way, once, so :func:`stop_problems` knows whether the clause can
    run: the item names the file, the table and the error, and says the clause was not
    checked — nothing more is skipped. A file that exists but does not parse counts as
    unreadable too: ``registered_ids`` would quietly fall back to the rows loaded when
    the run started, and a check against those is not a check of the table the session
    left behind.
    """
    from . import doctor as _doctor  # local: keep this module import-light for the hook
    toml = cfg.root / "pdca.toml"
    try:
        if toml.exists():
            tomllib.loads(toml.read_text(encoding="utf-8"))
        _doctor.registered_ids(cfg)
    except Exception as exc:  # noqa: BLE001 — any failure: the clause cannot run
        return (f"{toml}: the [[doctor.checks]] table could not be read "
                f"({_error_text(exc)}), so the dependency clause was not checked — no "
                "brief's External dependencies were matched to a registered row or "
                "probed")
    return ""


@contextlib.contextmanager
def session(cfg: Config, role: str, bundles: list[Path] | None = None, *,
            require_artifact: bool = True):
    """Driver-side registration for one interactive leaf session (issue #331 e).

    Yields the env to merge into the spawn: the role and a session-state scratch file
    (created in ``cfg.root`` with the gitignored :data:`STATE_PREFIX`, removed on exit).
    Captures the session-start act-log baseline for the act role. Yields ``{}`` — no
    contract — when the render does not mark the leaf interactive (criterion f), and on
    ANY setup failure (a checked exit contract must never break the leaf it checks).

    On exit, which is the driver reaping the leaf's process, the contract is judged and
    the verdict reported to the human (:func:`report_at_reap`, issue #534).
    """
    leaf = getattr(cfg, role, None)
    if not isinstance(leaf, LeafConfig) or not leaf.interactive:
        yield {}
        return
    path: Path | None = None
    try:
        baseline: dict = {}
        if role == "act":
            log = cfg.process_dir / "act-log.md"
            text = log.read_text(encoding="utf-8") if log.exists() else ""
            baseline = {"act_log_len": len(text), "act_log_sha": _sha(text)}
        registered = {
            "role": role,
            "bundles": [str(b) for b in (bundles or [])],
            "require_artifact": bool(require_artifact),
            "baseline": baseline,
        }
        fh = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=cfg.root,
            prefix=STATE_PREFIX, suffix=".json", delete=False)
        with fh:
            json.dump({**registered, "passed": []}, fh, indent=2)
        path = Path(fh.name)
        env = {ENV_ROLE: role, ENV_STATE: str(path)}
    except OSError as exc:
        print(f"handoff: could not register the {role} session state ({exc}) — the "
              "exit contract is unchecked for this session", file=sys.stderr)
        yield {}
        return
    try:
        yield env
    finally:
        # The reap (#534); nothing in it may raise out of this context manager. The
        # session writes `passed` / `abandoned` into the scratch file; what the driver
        # registered is taken from here, so the report names the bundles the driver
        # actually handed the leaf even if the file was rewritten or lost.
        try:
            state = {**_read_json(path), **registered}
        except Exception:  # noqa: BLE001 — e.g. JSON nested too deep to parse
            state = dict(registered)
        report_at_reap(cfg, role, state)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            _emit([f"handoff: could not remove the {role} session's scratch file "
                   f"({_error_text(exc)})"])


def report_at_reap(cfg: Config, role: str, state: dict) -> None:
    """Report, never enforce, a session's exit contract when the driver reaps it (#534).

    Printed to the human on stderr, in this order: the typed abandon reason, if one was
    recorded (:func:`_abandon_reason`); then everything :func:`stop_problems` finds,
    under one header naming the role. An abandon hides none of it: nothing is blocked
    here, so hiding it would only keep it from the human. Nothing found and no abandon
    ⇒ nothing printed.

    It covers what :func:`stop_problems` covers and no more: the artifacts of every
    bundle the driver registered for the session; where the driver registered none (the
    CSV/default batch planner, Act), whether the session named its work through a
    passing ``/handoff`` — the artifacts such a session wrote are not re-read; and, for
    a planner session, whether the ``[[doctor.checks]]`` table can be read.

    Every printed line goes through :func:`_printable`, because the reason and many
    items quote text the session wrote, and a raw terminal escape in it could hide the
    lines that follow.

    REPORT ONLY: it writes no bundle file, moves or deletes no artifact, does not reopen
    the session, and never raises, because a checked exit contract must never break the
    leaf it checks. A check that fails for one bundle is that bundle's item; a failure
    outside any bundle's check is one line, after the abandon reason.

    The absent-artifact cases are also handled by the driver after the session (no
    brief, no decision, no publish artifacts). What only this report catches is an
    artifact that is present but malformed: a brief whose Success criterion is empty, an
    iterate/discontinue decision with no rationale.
    """
    lines: list[str] = []
    try:
        reason = _abandon_reason(state)
        if reason:
            lines.append(f"handoff: the {role} session was deliberately abandoned — "
                         f"{reason}")
        problems = stop_problems(cfg, role, state)
        if problems:
            lines.append(f"handoff: the {role} session ended; checking its exit contract "
                         "found (a report only, the driver carries on as usual):")
            lines += [f"  - {p}" for p in problems]
    except Exception as exc:  # noqa: BLE001 — the report must never break the leaf
        lines.append(f"handoff: could not check the {role} session's exit contract "
                     f"({_error_text(exc)}) — no contract item was reported")
    _emit(lines)


# ----------------------------------------------------------------------------
# The two verdicts: /handoff (the session's self-check) and the reap report.
# ----------------------------------------------------------------------------
def resolve_bundle(cfg: Config, ident: str) -> Path:
    """``issue_331`` / ``331`` → the bundle dir, matching ``cfg.bundle`` keying."""
    return cfg.bundle(str(ident).strip().removeprefix("issue_"))


def run_check(cfg: Config, ident: str, *, role: str | None = None,
              environ: dict | None = None) -> int:
    """The ``/handoff <id>`` verdict: verify the current leaf's contract for ONE id.

    PASS ⇒ 0, FAIL ⇒ 1, no active contract ⇒ 2. The verdict is exit status + report —
    nothing is written into the bundle. A pass is recorded in the session state file
    (when one is registered), which is how a session whose work set the driver could
    not know at spawn names what it did.
    """
    env = environ if environ is not None else dict(os.environ)
    role = (role or env.get(ENV_ROLE) or "").strip()
    if not role:
        print("handoff: no leaf contract is active in this session "
              f"({ENV_ROLE} unset) — nothing to verify", file=sys.stderr)
        return 2
    active = contracts(cfg)
    if role not in active:
        print(f"handoff: the render defines no exit contract for role '{role}' "
              "(not interactive, or no contract exists) — nothing to verify",
              file=sys.stderr)
        return 2
    state = load_state(env)
    if role == "act":
        problems = check_act(cfg, ident, state.get("baseline") or {})
        label = f"handoff({role}) {ident}"
    else:
        d = resolve_bundle(cfg, ident)
        problems = check_bundle(role, d, cfg)
        label = f"handoff({role}) {d.name}"
    if problems:
        print(f"{label}: FAIL — the {role} exit contract is not discharged:")
        for p in problems:
            print(f"  - {p}")
        print("Fix the items above and run /handoff again. To deliberately abandon "
              "instead: python3 .claude/hooks/handoff_guard.py --abandon \"<why>\"")
        return 1
    print(f"{label}: PASS — the {role} exit contract is discharged")
    raw = env.get(ENV_STATE, "")
    if raw:
        record_pass(Path(raw), ident)
    return 0


def stop_problems(cfg: Config, role: str, state: dict) -> list[str]:
    """The session-end verdict: what the driver can still find unmet or unchecked in
    this session's exit contract (empty ⇒ nothing to report). The driver reports it
    when it reaps the session (:func:`report_at_reap`); nothing blocks on it. The name
    is historical: the #331 Stop hook that consumed it is retired (#534).

    What it covers, and no more:

    * **Every bundle the driver registered** at spawn (a single Plan, sign-off or
      publish bundle; each bundle of an id-seeded batch Plan or a batch sign-off): its
      ARTIFACTS are re-read — the contract is the artifacts, so a session that
      discharged them without ever typing ``/handoff`` is discharged too. A bundle whose
      check raises (a decision file that is not UTF-8, a directory where a file should
      be) is listed as ``<bundle>: could not check (<error>)`` and every other bundle is
      still checked.
    * **No registered bundle set** (the CSV/default batch planner, which picks its
      issues mid-session; Act): the session must have named its work through a passing
      ``/handoff``. The artifacts such a session wrote are not re-read here.
    * **Every planner session**, bundles registered or not, briefs present or not: the
      ``[[doctor.checks]]`` table the dependency clause reads is read first
      (:func:`_doctor_table_problem`). If it cannot be read, that is ONE item for the
      whole session, and every brief is still checked without the dependency clause.

    ``abandoned`` is not read here: an abandon changes nothing in this list, and
    :func:`report_at_reap` prints the typed reason above it, never in place of it.
    """
    if role not in contracts(cfg):
        return []
    if role == "act":
        if state.get("passed"):
            return []
        return ["the act session has not verified its exit contract — append the dated "
                "act-log entry (even a 'no delta warranted' one) and run "
                "`/handoff <entry-date>` naming it"]
    out: list[str] = []
    table_problem = _doctor_table_problem(cfg) if role == "planner" else ""
    if table_problem:
        out.append(table_problem)
    bundles = [Path(b) for b in (state.get("bundles") or [])]
    if bundles:
        kw: dict = {}
        if role == "planner":
            kw = {"allow_absent": not state.get("require_artifact", True),
                  "dependencies": not table_problem}
        for d in bundles:
            try:
                found = check_bundle(role, d, cfg, **kw)
            except Exception as exc:  # noqa: BLE001 — one bundle must not hide the rest
                found = [f"could not check ({_error_text(exc)})"]
            out += [f"{d.name}: {p}" for p in found]
        return out
    if state.get("passed"):
        return out
    return out + [f"the {role} session registered no bundle set at spawn and verified "
                  "none — run `/handoff issue_<id>` for each bundle this session worked"]
