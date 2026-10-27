#!/usr/bin/env python3
"""Check the #593 stack-mode workflow once on real GitHub (#625). Standard library only.

The offline suites test stack mode against a local bare ``origin`` and a stubbed ``gh``. This
tool runs the same workflow on GitHub: the pre-provisioned private sandbox repository
``eduralph/pdca-stack-sandbox``, real ``git``, real ``gh``, PRs merged on GitHub. The model
leaves are replaced by fixed patches, so it makes no model calls. Five subcommands share one
work directory:

    python3 tools/live/stack_mode_live.py preflight
    python3 tools/live/stack_mode_live.py setup    --workdir DIR
    python3 tools/live/stack_mode_live.py run      --workdir DIR
    python3 tools/live/stack_mode_live.py judge    --workdir DIR
    python3 tools/live/stack_mode_live.py teardown --workdir DIR [--keep]

All but ``judge`` take ``--repo`` (default ``eduralph/pdca-stack-sandbox``) and
``--token-file`` (default ``~/.config/pdca/sandbox-gh-token``).

Repo guard. ``preflight`` (which ``setup``, ``run`` and ``teardown`` run first) refuses, before
any network call, a token file that is missing or empty and a repository whose name is not
``pdca-stack-sandbox`` and does not contain ``sandbox`` (``eduralph/pdca-harness`` by name).
Then ``gh api repos/<repo>`` must report the repository private and pushable, and both
``allow_merge_commit`` and ``allow_squash_merge`` true. A setting GitHub does not report is a
warning (a merge it refuses later is INCOMPLETE), and so is a private repository of a personal
account, which may refuse the draft PRs publish opens. Every ``gh`` and ``git`` process the
tool or the driver starts gets ``GH_TOKEN`` set to the token file's token; an ambient
``GH_TOKEN`` / ``GITHUB_TOKEN`` or a stored ``gh`` login is never used, and ``git`` pushes
through ``gh auth git-credential``, set as the credential helper of the tool's own clone only.

``setup`` creates the run base ``live-<UTC stamp>`` from the sandbox's ``main`` and records it
in ``state.json`` (written before the branch is created, so ``teardown`` always finds it), then
clones the sandbox and writes the scenario's bundles. ``main``, the repository and its settings
are never changed.

``run`` drives the scenario through the real driver code: each run is ``flow.flow_ids``, the
call ``pdca flow <ids>`` makes, with only the model leaves replaced (``flow._drive_wave`` writes
a fixed patch and accepts, ``publish.draft_texts`` returns True); ``git``, ``gh`` and
``publish.publish`` are real. It merges PRs on the sandbox and records every observation, as
raw data, in ``observations.json``. ``judge`` reads only that file, evaluates the checks E0-E5,
writes ``report.md`` and ``report.json`` and exits 0 only if every check passes (1 when one
FAILs, 3 when one is INCOMPLETE). It needs no network, so it can be re-run on a saved
``observations.json``. ``teardown`` closes the run's open PRs and deletes the run's branches
(``--keep`` skips it).

A check is INCOMPLETE, never FAIL, when what it needs was not observed (a missing PR, a branch
delete not confirmed, a merge or ``update-branch`` GitHub refused, a PR file list ``run`` did
not see settle, a ``git merge-base --is-ancestor`` that exited with neither 0 nor 1). A file
list settles when two reads in a row agree and both are on the commits ``run`` expected: after
a push to the PR (or when it opened), the PR's ``baseRefOid`` is the run base tip and its head
is the head pushed; after the run base moved with no push to the PR, GitHub keeps the PR's
base commit and file list, so its head must be unchanged and its mergeability recomputed (a
test merge of that head into the run base tip). A FAIL is an observation that was complete
and disagreed with the expectation. A driver run is an observation, not a precondition: once
run 2's preconditions were observed, a run 2 that exits non-zero or raises is E3's FAIL.
``run`` exits 3 when any check would be INCOMPLETE.

Cost: about seven PRs and a dozen branches on the sandbox, about ten minutes (mostly waiting
for GitHub to recompute PR file lists), no model calls. ``CONTRIBUTING.md`` ("Checking stack
mode on real GitHub") has the steps.
"""

from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import traceback
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TOOL = "stack_mode_live"
HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parents[2]
TEMPLATE_SRC = REPO_ROOT / "template" / "src"
TEMPLATES = REPO_ROOT / "template" / "templates"

DEFAULT_REPO = "eduralph/pdca-stack-sandbox"
DEFAULT_TOKEN_FILE = "~/.config/pdca/sandbox-gh-token"
SANDBOX_NAME = "pdca-stack-sandbox"
REFUSED_REPOS = ("eduralph/pdca-harness",)
MAIN = "main"
BY = "pdca stack-mode live check"
EMAIL = "pdca-live-check@example.invalid"

# The scenario. Each id's fixed patch adds one file of its own; DEPS is its `Depends on`.
IDS = ("A", "B", "C", "D", "P", "Q", "S")
FILES = {iid: [f"stack-live/{iid.lower()}.txt"] for iid in IDS}
DEPS = {"B": "A, C", "D": "B", "Q": "P"}
RUNS = {
    "run1": ["A", "C", "B"],         # A and C independent (wave 0), B on both (wave 1)
    "run2": ["A", "B", "C", "D"],    # all four named, after A and C merged and lost their branches
    "run3": ["P", "Q"],              # the plain-chain control
    "run4": ["S"],                   # the squash shape
}
CHECK_IDS = ("E0", "E1", "E1b", "E2", "E3", "E4", "E5")
PASS, FAIL, INCOMPLETE = "PASS", "FAIL", "INCOMPLETE"
COMPLETE = "COMPLETE"            # state.COMPLETE (template/src/pdca_harness/state.py:27)
RECORDS = ("state.json", "observations.json", "report.json", "report.md")
# Dropped from the environment before GH_TOKEN is set: an ambient token or host is never used.
AMBIENT = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
           "GH_HOST", "GH_REPO")
POLL = 5.0                       # seconds between two reads of a PR's file list
# The polled PR file lists `run` reads after the run base moved with no push to the PR. GitHub
# moves a PR's baseRefOid and file list when the PR is pushed to (or opened), not when its base
# branch moves: on the sandbox, Q's PR kept its old base commit and P's file for 20+ minutes
# after P merged. So these settle on the PR's head unchanged and its mergeability recomputed
# against the run base tip, never on baseRefOid (:func:`read_off`). Every other polled list
# follows a push.
NO_PUSH = ("B_files_after_A_merge", "B_files_before_update", "Q_files_after_P_merge")
# One read of a PR: its file list, its base and head commits, its mergeability, and GitHub's
# test merge (the merge of the head into the base branch as it is now, made when GitHub
# computes mergeability) with its parents. `gh pr view --json potentialMergeCommit` gives the
# test merge's oid only, not its parents.
PR_READ = ("query($owner: String!, $name: String!, $number: Int!) { repository(owner: $owner, "
           "name: $name) { pullRequest(number: $number) { baseRefOid headRefOid mergeable "
           "potentialMergeCommit { oid parents(first: 2) { nodes { oid } } } "
           "files(first: 100) { nodes { path } } } } }")
MERGE_TRIES = 6
RC_FAIL, RC_REFUSED, RC_INCOMPLETE = 1, 2, 3


# -- the guard (no network) -----------------------------------------------------------------

def guard(repo: str, token_file: str) -> tuple[str, str]:
    """``(token, "")`` when ``repo`` may be acted on and ``token_file`` holds a token, else
    ``("", why)``. Reads one local file and nothing else: no ``gh``, no network."""
    spec = repo
    owner, _sep, name = spec.partition("/")
    if not owner or not name or "/" in name or any(c.isspace() for c in spec):
        return "", f"--repo must be OWNER/NAME, got {repo!r}"
    if spec.lower() in REFUSED_REPOS:
        return "", f"refusing {spec}: this tool never acts on the harness's own repository"
    if name.lower() != SANDBOX_NAME and "sandbox" not in name.lower():
        return "", (f"refusing {spec}: the repository must be {SANDBOX_NAME} or have "
                    f"'sandbox' in its name")
    path = Path(token_file).expanduser()
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return "", f"cannot read the token file {path}: {exc.strerror or exc}"
    if not token:
        return "", f"the token file {path} is empty"
    if any(c.isspace() for c in token):
        return "", f"the token file {path} must hold the token alone, on one line"
    return token, ""


def use_token(token: str) -> None:
    """Point every ``gh`` and ``git`` process this one starts, the driver's included, at the
    sandbox token and at nothing else. ``GH_TOKEN`` wins over a stored ``gh`` login; the other
    token variables, and a host or repo override, are dropped. ``GIT_CONFIG_GLOBAL`` is emptied
    so the user's global git config (an ``insteadOf`` to ssh, a credential helper, a hooks path)
    cannot route a fetch or push around the clone's own credential helper (:func:`_clone`).
    Nothing here writes the token anywhere."""
    for var in AMBIENT:
        os.environ.pop(var, None)
    os.environ.update({"GH_TOKEN": token, "GH_PROMPT_DISABLED": "1",
                       "GH_NO_UPDATE_NOTIFIER": "1", "GIT_TERMINAL_PROMPT": "0",
                       "GIT_CONFIG_GLOBAL": os.devnull})


def gh(*args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    """``gh args``. A missing or hung ``gh`` is an answer (exit 127 / 124), never a raise."""
    try:
        return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return subprocess.CompletedProcess(["gh", *args], 127, "", "gh: command not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(["gh", *args], 124, "",
                                           f"gh timed out after {timeout:g} s")


def _said(r: subprocess.CompletedProcess) -> str:
    """What a failed command said, on one line."""
    return " ".join(f"{r.stderr or ''} {r.stdout or ''}".split()) or f"exit {r.returncode}"


def preflight(repo: str, *, need_merge: bool) -> tuple[dict, list[str], list[str]]:
    """``(repository, refusals, warnings)`` from ``gh api repos/<repo>``. Refused: an answer
    for another repository (a rename or redirect), a repository that is not private, a token
    without push permission, and, with ``need_merge``, ``allow_merge_commit`` or
    ``allow_squash_merge`` false (the scenario uses both). A merge setting GitHub does not
    report (it may hide them from a token without admin rights) is a warning."""
    r = gh("api", f"repos/{repo}")
    if r.returncode != 0:
        return {}, [f"`gh api repos/{repo}` failed: {_said(r)}"], []
    try:
        info = json.loads(r.stdout or "{}")
    except ValueError:
        info = None
    if not isinstance(info, dict):
        return {}, [f"`gh api repos/{repo}` did not answer a repository object"], []
    refuse: list[str] = []
    warn: list[str] = []
    full = info.get("full_name")
    if not isinstance(full, str) or full.lower() != repo.lower():
        refuse.append(f"GitHub answered for {full!r}, not {repo} (renamed or redirected?)")
    if info.get("private") is not True:
        refuse.append(f"{repo} is not private (private: {json.dumps(info.get('private'))})")
    perms = info.get("permissions") if isinstance(info.get("permissions"), dict) else {}
    if perms.get("push") is not True:
        refuse.append(f"the token cannot push to {repo} "
                      f"(permissions.push: {json.dumps(perms.get('push'))})")
    for setting in ("allow_merge_commit", "allow_squash_merge"):
        value = info.get(setting)
        if value is False:
            (refuse if need_merge else warn).append(
                f"{repo} has {setting} = false; the scenario merges with a merge commit and "
                f"with a squash, so it needs both")
        elif value is not True:
            warn.append(f"GitHub did not report {setting} (a token without admin rights may "
                        f"not see it); a merge it refuses later makes the run INCOMPLETE")
    owner = info.get("owner") if isinstance(info.get("owner"), dict) else {}
    if need_merge and info.get("private") is True and owner.get("type") == "User":
        warn.append(f"{repo} is a private repository of a personal account, where GitHub may "
                    f"not offer draft pull requests (it lists them for private repositories "
                    f"on Team and Enterprise plans); publish opens every PR as a draft, so if "
                    f"GitHub refuses one, `run` opens no PR and ends INCOMPLETE")
    return info, refuse, warn


def _check_repo(step: str, repo: str, token: str, *, need_merge: bool) -> dict | None:
    """``use_token``, then ``preflight``, printed. The repository object, or None: refused."""
    use_token(token)
    info, refuse, warn = preflight(repo, need_merge=need_merge)
    for w in warn:
        print(f"{step}: warning: {w}", file=sys.stderr)
    for why in refuse:
        print(f"{step}: refused: {why}", file=sys.stderr)
    if refuse:
        return None
    print(f"{step}: {repo} is private and the token can push to it")
    return info


def _refuse(step: str, why: str) -> int:
    print(f"{step}: refused: {why}", file=sys.stderr)
    return RC_REFUSED


# -- shared ---------------------------------------------------------------------------------

def git(repo: Path, *args: str, check: bool = False,
        timeout: float = 600) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       timeout=timeout, stdin=subprocess.DEVNULL)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {_said(r)}")
    return r


def read_state(workdir: Path) -> dict | None:
    """The work directory's ``state.json`` when this tool wrote it, else None."""
    try:
        s = json.loads((workdir / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return s if isinstance(s, dict) and s.get("tool") == TOOL else None


def write_json(path: Path, data: dict) -> None:
    """Write ``data`` whole or not at all (a temp file renamed over ``path``)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _driver() -> SimpleNamespace:
    """The driver modules of this checkout (``template/src``), never an installed copy: the
    tool checks the code it ships beside. No bytecode is written into the checkout."""
    sys.dont_write_bytecode = True
    if str(TEMPLATE_SRC) not in sys.path:
        sys.path.insert(0, str(TEMPLATE_SRC))
    import pdca_harness  # noqa: PLC0415
    from pdca_harness import cli, flow, integrate, publish, signoff, state  # noqa: PLC0415
    from pdca_harness.config import Config, LeafConfig  # noqa: PLC0415
    where = Path(pdca_harness.__file__).resolve()
    if TEMPLATE_SRC.resolve() not in where.parents:
        raise RuntimeError(f"imported pdca_harness from {where}, not from {TEMPLATE_SRC}")
    return SimpleNamespace(cli=cli, flow=flow, integrate=integrate, publish=publish,
                           signoff=signoff, state=state, Config=Config, LeafConfig=LeafConfig)


def make_config(drv: SimpleNamespace, workdir: Path, repo: str, base: str):
    """The ``Config`` every step drives with: its own bundle root in the work directory, the
    tool's clone as the sandbox's checkout, ``origin`` as the base remote (own repo)."""
    project = workdir / "project"
    return drv.Config(
        root=project, bundle_root=project / "results", process_dir=project / "process",
        templates_dir=TEMPLATES, default_branch=base, tracker_system="github", tracker_url="",
        issue_id_example="#1", builder=drv.LeafConfig(mode="stub"),
        reviewer=drv.LeafConfig(mode="stub"),
        # "command", not "stub": a stub publisher turns every publish and fold into a dry
        # run (flow.py:659, :3079). It never runs: `run` writes the texts first.
        publisher=drv.LeafConfig(mode="command", interactive=True),
        base_remote="origin", repo_checkouts={repo: str(workdir / "checkout")}, author=BY)


def brief_text(iid: str, repo: str, base: str) -> str:
    """Bundle ``iid``'s brief: a Slug carrying the run base (so its branch is unique to the
    run), the run base as the target, and its `Depends on`."""
    lines = [f"# Stack-mode live check: bundle {iid} of {base}", "",
             f"- **Slug:** {base}-{iid.lower()}",
             f"- **Repo + branch target:** {repo} @ {base}"]
    if iid in DEPS:
        lines.append(f"- **Depends on:** {DEPS[iid]}")
    return "\n".join(lines) + "\n"


def teardown_command(workdir: Path, repo: str, token_file: str) -> str:
    return " ".join(shlex.quote(x) for x in (
        "python3", str(HERE), "teardown", "--workdir", str(workdir), "--repo", repo,
        "--token-file", token_file))


# -- preflight ------------------------------------------------------------------------------

def cmd_preflight(a: argparse.Namespace) -> int:
    """The guard and the repository check, printed. Writes nothing."""
    token, why = guard(a.repo, a.token_file)
    if why:
        return _refuse("preflight", why)
    if _check_repo("preflight", a.repo, token, need_merge=True) is None:
        return RC_REFUSED
    print(f"preflight: {a.repo} passes: setup may create a run base there")
    return 0


# -- setup ----------------------------------------------------------------------------------

def cmd_setup(a: argparse.Namespace) -> int:
    token, why = guard(a.repo, a.token_file)
    if why:
        return _refuse("setup", why)
    workdir = Path(a.workdir).expanduser().resolve()
    if workdir.exists() and (not workdir.is_dir() or any(workdir.iterdir())):
        return _refuse("setup", f"{workdir} is not an empty directory; use a fresh --workdir")
    info = _check_repo("setup", a.repo, token, need_merge=True)
    if info is None:
        return RC_REFUSED
    repo = str(info["full_name"])
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    base = f"live-{stamp}"
    workdir.mkdir(parents=True, exist_ok=True)
    state = {"tool": TOOL, "repo": repo, "run_base": base, "stamp": stamp,
             "default_branch": info.get("default_branch"), "base_created": False,
             "branches": [], "prs": {}, "ready": False}
    # Recorded BEFORE the branch exists: a create whose answer is lost on the way back still
    # leaves the branch to `teardown`, which reads a missing branch as already gone.
    write_json(workdir / "state.json", state)
    try:
        state["base_from"] = _main_sha(repo)
        r = gh("api", "-X", "POST", f"repos/{repo}/git/refs",
               "-f", f"ref=refs/heads/{base}", "-f", f"sha={state['base_from']}")
        if r.returncode != 0:
            raise RuntimeError(f"could not create {base} on {repo}: {_said(r)}")
        state["base_created"] = True
        write_json(workdir / "state.json", state)
        drv = _driver()
        cfg = make_config(drv, workdir, repo, base)
        state["line_prefix"] = drv.integrate.integration_branch(cfg, base)
        _clone(workdir / "checkout", repo, base, state["base_from"])
        state["branches"] = _write_bundles(drv, cfg, repo, base)
        state["ready"] = True
        write_json(workdir / "state.json", state)
    except Exception as exc:  # noqa: BLE001 — reported with how to clean up
        print(f"setup: did not finish: {type(exc).__name__}: {exc}\nsetup: remove what it "
              f"made with:\n  {teardown_command(workdir, repo, a.token_file)}",
              file=sys.stderr)
        return RC_FAIL
    print(f"setup: run base {base} on {repo} (from {MAIN} at {state['base_from'][:12]}); "
          f"clone {workdir / 'checkout'}; bundles {workdir / 'project' / 'results'}")
    print(f"setup: next: python3 {HERE} run --workdir {workdir}")
    return 0


def _main_sha(repo: str) -> str:
    r = gh("api", f"repos/{repo}/git/ref/heads/{MAIN}", "--jq", ".object.sha")
    sha = (r.stdout or "").strip()
    if r.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", sha):
        raise RuntimeError(f"could not read {repo}'s {MAIN}: {_said(r)}")
    return sha


def _clone(checkout: Path, repo: str, base: str, base_from: str) -> None:
    """The sandbox's checkout, on the run base. ``git`` authenticates through ``gh auth
    git-credential``, set here and nowhere else (the empty value first drops any helper a
    system config sets), so the token is in no remote URL and no file."""
    checkout.mkdir(parents=True)
    for args in (("init", "-q", "-b", base),
                 ("config", "credential.https://github.com.helper", ""),
                 ("config", "--add", "credential.https://github.com.helper",
                  "!gh auth git-credential"),
                 ("config", "user.name", BY), ("config", "user.email", EMAIL),
                 ("config", "commit.gpgsign", "false"),
                 ("remote", "add", "origin", f"https://github.com/{repo}.git"),
                 ("fetch", "-q", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}"),
                 ("checkout", "-q", "-B", base, f"origin/{base}")):
        git(checkout, *args, check=True)
    tip = git(checkout, "rev-parse", f"origin/{base}", check=True).stdout.strip()
    if tip != base_from:
        raise RuntimeError(f"origin/{base} is at {tip}, not at {base_from}")


def _write_bundles(drv: SimpleNamespace, cfg, repo: str, base: str) -> list[str]:
    """Write every scenario bundle's brief; return the PR branch the driver will push for
    each (``publish._branch_name``, publish.py:622-630), so `teardown` has them even when
    ``run`` stops before it reads them back."""
    branches = []
    for iid in IDS:
        d = cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text(brief_text(iid, repo, base), encoding="utf-8")
        branches.append(drv.publish._branch_name(cfg, d, drv.publish._resolve_target(d)[2]))
    cfg.process_dir.mkdir(parents=True, exist_ok=True)
    return branches


# -- run ------------------------------------------------------------------------------------

class _Tee(io.TextIOBase):
    """A stream that keeps what is written and passes it on: the run's stderr is recorded
    for ``judge`` and still shown while the flow runs."""

    def __init__(self, out) -> None:
        super().__init__()
        self.out, self.buf = out, io.StringIO()

    def write(self, s: str) -> int:
        self.buf.write(s)
        self.out.write(s)
        return len(s)

    def flush(self) -> None:
        self.out.flush()

    def getvalue(self) -> str:
        return self.buf.getvalue()


class Live:
    """Drives the scenario and records what GitHub shows. Only the model leaves are
    replaced, as the peer offline case does (template/tests/test_flow_same_wave_conflict.py:
    119-129, :153-157): ``flow._drive_wave`` writes the fixed patch and accepts, and
    ``publish.draft_texts`` returns True, the texts written first. ``subprocess.run``,
    ``flow._publish_bundle`` → ``publish.publish`` (flow.py:642-660), the folds and every
    ``gh`` read the driver makes are real."""

    def __init__(self, workdir: Path, state: dict, *, settle: float, settle_max: float) -> None:
        self.d = _driver()
        self.workdir, self.state = workdir, state
        state.setdefault("branches", [])
        state.setdefault("prs", {})
        self.repo, self.base = str(state["repo"]), str(state["run_base"])
        self.checkout = workdir / "checkout"
        self.cfg = make_config(self.d, workdir, self.repo, self.base)
        self.settle = settle
        self.settle_max = max(settle_max, settle + 2 * POLL)  # room for two reads
        self.today = datetime.date.today().isoformat()
        self.built: dict[str, dict] = {}   # bundle id → what it was built on, read at its build
        self.obs: dict = {
            "tool": TOOL, "schema": 1, "repo": self.repo, "run_base": self.base,
            "line_prefix": state.get("line_prefix"), "files": FILES,
            "started": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "runs": {}, "prs": {}, "merged": {}, "errors": [], "finished": False}

    # recording ---------------------------------------------------------------------------

    def save(self) -> None:
        write_json(self.workdir / "observations.json", self.obs)
        write_json(self.workdir / "state.json", self.state)

    def _remember(self, branch: str = "", iid: str = "", pr: dict | None = None) -> None:
        """Record a run branch or PR in ``state.json`` at once, for ``teardown``."""
        if branch and branch not in self.state["branches"]:
            self.state["branches"].append(branch)
        if iid and pr:
            self.state["prs"][iid] = {k: pr.get(k) for k in ("number", "url", "headRefName")}
        write_json(self.workdir / "state.json", self.state)

    # git and gh --------------------------------------------------------------------------

    def _fetch(self) -> None:
        """Fetch origin, pruned, tried three times; a fetch that keeps failing raises."""
        for attempt in range(3):
            r = git(self.checkout, "fetch", "-q", "--prune", "origin")
            if r.returncode == 0:
                return
            time.sleep(3 * (attempt + 1))
        raise RuntimeError(f"git fetch origin failed: {_said(r)}")

    def _record_lines(self) -> None:
        """Record every branch origin has under the run's integration prefix (the lines and
        per-bundle bases the folds pushed, integrate.py:140-160) in ``state.json``."""
        prefix = str(self.state.get("line_prefix") or "")
        r = git(self.checkout, "ls-remote", "--heads", "origin")
        for line in r.stdout.splitlines() if prefix and r.returncode == 0 else []:
            ref = line.split()[-1].removeprefix("refs/heads/")
            if ref.startswith(prefix):
                self._remember(branch=ref)

    def _tip(self) -> str:
        r = git(self.checkout, "rev-parse", "--verify", "--quiet", f"origin/{self.base}^{{commit}}")
        return r.stdout.strip() if r.returncode == 0 else ""

    def _ancestor(self, commit: str | None, of: str) -> dict:
        """``git merge-base --is-ancestor``: exit 0 → True, 1 → False, anything else (128 for
        a commit this clone lacks) → None, which ``judge`` reads as not observed."""
        if not commit or not of:
            return {"value": None, "rc": None}
        rc = git(self.checkout, "merge-base", "--is-ancestor", commit, of).returncode
        return {"value": True if rc == 0 else False if rc == 1 else None, "rc": rc}

    def _view(self, url: str, fields: str) -> dict | None:
        """``gh pr view url --json fields``, tried three times; None when it never answered."""
        for attempt in range(3):
            r = gh("pr", "view", url, "--json", fields)
            if r.returncode == 0:
                try:
                    info = json.loads(r.stdout or "{}")
                except ValueError:
                    info = None
                if isinstance(info, dict):
                    return info
            time.sleep(2 * (attempt + 1))
        return None

    def _pr_state(self, url: str) -> str:
        return str((self._view(url, "state") or {}).get("state") or "")

    # the leaves --------------------------------------------------------------------------

    def _patch(self, iid: str) -> str:
        return "".join(f"diff --git a/{name} b/{name}\nnew file mode 100644\n--- /dev/null\n"
                       f"+++ b/{name}\n@@ -0,0 +1 @@\n+{iid}\n" for name in FILES[iid])

    def _drive_wave(self, cfg, wave: list[Path], **_kw) -> int:
        """Build and accept each bundle, recording what it was built on, as the peer's
        ``_drive_wave`` does (test_flow_same_wave_conflict.py:119-129). Accepting writes what
        the fixture's ``_accept`` writes (test_integrate_stack_bases.py:102-106), then the
        two texts publish needs, so no publisher leaf runs."""
        for d in wave:
            iid = d.name.removeprefix("issue_")
            self.built[iid] = {"stack_base": self.d.publish.read_stack_base(d),
                               "tip": self.d.publish.read_stack_base_tip(d)}
            (d / "patch.diff").write_text(self._patch(iid), encoding="utf-8")
            (d / "check-gates.json").write_text("{}", encoding="utf-8")
            shutil.copyfile(TEMPLATES / "SUMMARY.md.tpl", d / "SUMMARY.md")
            self.d.signoff.record(d / "SUMMARY.md", action="accept", by=BY, date=self.today)
            (d / self.d.publish.COMMIT_MSG).write_text(
                f"live check {self.base}: add {', '.join(FILES[iid])}\n\n"
                f"Fixed patch of the pdca-harness stack-mode live check.\n", encoding="utf-8")
            (d / self.d.publish.PR_BODY).write_text(
                f"Fixed patch of the pdca-harness stack-mode live check, run {self.base}. "
                f"Opened by tools/live/stack_mode_live.py; its teardown closes it.\n",
                encoding="utf-8")
        return 1

    def _drive(self, name: str) -> dict:
        """One run of the flow, as ``pdca flow <ids>`` drives it: ``flow.flow_ids`` with the
        ids as asked for (cli.py:667-669). It skips an id an earlier run finished and hands
        the rest to ``flow._drive_and_act`` with every id as the batch (flow.py:3498-3501), so
        a finished id is a finished NAMED id, which the pre-wave carry judges
        (``flow._finished_named``, flow.py:1270; ``flow._carry_finished``, :1425). The exit
        code is ``cli._results_rc``'s (cli.py:707), as ``pdca flow`` reports it. A raise is
        recorded, not propagated."""
        ids = RUNS[name]
        print(f"run: {name} — pdca flow {' '.join(ids)}", file=sys.stderr)
        holds: dict[str, str] = {}
        results: dict = {}
        raised = ""
        err, out = _Tee(sys.stderr), _Tee(sys.stdout)
        start = time.monotonic()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(self.d.flow, "_drive_wave", self._drive_wave))
            stack.enter_context(mock.patch.object(self.d.publish, "draft_texts",
                                                  lambda *a, **k: True))
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            try:
                results = self.d.flow.flow_ids(self.cfg, list(ids), do_publish=True,
                                               do_act=False, by=BY, today=self.today,
                                               holds=holds)
            except (Exception, SystemExit):  # noqa: BLE001 — the run's outcome, recorded
                raised = traceback.format_exc()
        rec = {"ids": ids, "driven": True, "raised": raised,
               "rc": None if raised else self.d.cli._results_rc(results, holds=holds),
               "results": {str(k): str(v) for k, v in (results or {}).items()},
               "holds": dict(holds),
               "states": {iid: self.d.state.state(self.cfg.bundle(iid)) for iid in ids},
               "stderr": err.getvalue(), "stdout": out.getvalue(),
               "seconds": round(time.monotonic() - start, 1)}
        self.obs["runs"][name] = rec
        self._fetch()
        self._record_lines()
        for iid in ids:
            self._record_pr(iid)
        self.save()
        return rec

    def _record_pr(self, iid: str) -> None:
        """The PR ``iid``'s publish opened, as GitHub reports it, with the head the run pushed
        (read off origin)."""
        if iid in self.obs["prs"]:
            return
        rec = self.d.publish._publish_record(self.cfg.bundle(iid)) or {}
        branch, url = str(rec.get("branch") or ""), str(rec.get("pr_url") or "")
        if branch:
            self._remember(branch=branch)
        if not url:
            return
        view = self._view(url, "number,url,state,isDraft,baseRefName,headRefName,headRefOid")
        if view is None:
            self.obs["prs"][iid] = {"url": url, "headRefName": branch,
                                    "error": "gh pr view did not answer"}
            return
        ls = git(self.checkout, "ls-remote", "origin", f"refs/heads/{view.get('headRefName')}")
        view["pushed_head"] = (ls.stdout.split() or [""])[0] if ls.returncode == 0 else ""
        view["published"] = {k: rec.get(k) for k in ("branch", "base", "mode", "cut_from")}
        self.obs["prs"][iid] = view
        self._remember(branch=str(view.get("headRefName") or ""), iid=iid, pr=view)

    # GitHub actions ----------------------------------------------------------------------

    def _merge(self, iid: str, how: str) -> dict:
        """Mark ``iid``'s draft PR ready, merge it (``--merge`` / ``--squash``, pinned to the
        head read just before), confirm it reads MERGED, then delete its branch and confirm
        it is gone (:meth:`_delete_branch`). A merge GitHub refuses is recorded as refused:
        what needs it reads INCOMPLETE."""
        pr = self.obs["prs"].get(iid) or {}
        url = str(pr.get("url") or "")
        rec = self.obs["merged"][iid] = {"method": how, "merged": False, "refused": "",
                                         "branch": pr.get("headRefName"), "deleted": False}
        if not url:
            rec["refused"] = f"{iid} has no PR to merge"
            self.save()
            return rec
        print(f"run: merging {iid}'s PR {url} (--{how})", file=sys.stderr)
        for attempt in range(3):        # publish opens drafts, and a draft does not merge
            ready = gh("pr", "ready", url)
            if ready.returncode == 0:
                break
            time.sleep(3 * (attempt + 1))
        rec["ready"] = "ok" if ready.returncode == 0 else _said(ready)
        now = self._view(url, "isDraft,headRefOid") or {}
        if now.get("isDraft") is not False:
            rec["refused"] = f"`gh pr ready` did not take the PR out of draft: {rec['ready']}"
            self.save()
            return rec
        head = str(now.get("headRefOid") or "")
        said, st = "", ""
        for attempt in range(MERGE_TRIES):
            args = ["pr", "merge", url, f"--{how}"] + (["--match-head-commit", head] if head
                                                        else [])
            r = gh(*args)
            said = "" if r.returncode == 0 else _said(r)
            st = self._pr_state(url)
            if st == "MERGED":
                break
            if attempt + 1 < MERGE_TRIES:   # GitHub may still be computing mergeability
                time.sleep(5 * (attempt + 1))
        if st != "MERGED":
            rec["refused"] = f"reads {st or 'no state'} after `gh pr merge --{how}`: {said}"
            self.save()
            return rec
        rec.update(merged=True, merged_head=head)
        rec.update(self._delete_branch(str(pr.get("headRefName") or "")))
        self._fetch()
        rec["base_tip"] = self._tip()
        self.save()
        return rec

    def _delete_branch(self, branch: str) -> dict:
        """Delete ``branch`` on GitHub (the sandbox does not delete merged branches) and
        confirm with ``git ls-remote`` that origin no longer has it. A 422 "Reference does
        not exist" counts as gone."""
        if not branch:
            return {"deleted": False, "delete": "no branch recorded"}
        for attempt in range(3):
            r = gh("api", "-X", "DELETE", f"repos/{self.repo}/git/refs/heads/{branch}")
            if r.returncode == 0 or "Reference does not exist" in _said(r):
                break
            time.sleep(3 * (attempt + 1))
        if r.returncode == 0:
            how = "deleted"
        elif "Reference does not exist" in _said(r):
            how = "already gone"
        else:
            return {"deleted": False, "delete": f"refused: {_said(r)}"}
        ls = None
        for attempt in range(6):
            ls = git(self.checkout, "ls-remote", "--exit-code", "origin", f"refs/heads/{branch}")
            if ls.returncode == 2:           # --exit-code: no matching ref
                return {"deleted": True, "delete": how}
            time.sleep(2 * (attempt + 1))
        return {"deleted": False, "delete": f"{how}, but `git ls-remote` still answers "
                                            f"(rc {ls.returncode if ls else '?'})"}

    def _update_branch(self, iid: str) -> dict:
        """GitHub's "Update branch" (a merge of the base into the PR branch) on ``iid``'s PR,
        then wait for the PR head to move."""
        url = str(self.obs["prs"][iid]["url"])
        before = str((self._view(url, "headRefOid") or {}).get("headRefOid") or "")
        print(f"run: Update branch on {iid}'s PR {url}", file=sys.stderr)
        r = gh("pr", "update-branch", url)
        if r.returncode != 0:
            return {"ok": False, "refused": _said(r), "head_before": before}
        after = before
        for _ in range(30):
            after = str((self._view(url, "headRefOid") or {}).get("headRefOid") or "")
            if after and after != before:
                return {"ok": True, "head_before": before, "head_after": after}
            time.sleep(3)
        return {"ok": False, "head_before": before, "head_after": after,
                "refused": f"the PR head stayed at {before} for 90 s after gh pr update-branch"}

    def _poll(self, key: str, iid: str, head: str = "") -> dict:
        """Read ``iid``'s PR file list until GitHub has settled it; record it as ``obs[key]``.
        GitHub works a PR out again in the background after a push to it, so a read right
        after one can still be the old answer. After the run base moved with no push to the PR
        (``key`` in :data:`NO_PUSH`), GitHub keeps the PR's base commit and file list and
        recomputes only its mergeability. From ``settle`` seconds on, read every ``POLL``
        seconds (:meth:`_read_pr`) until two reads in a row agree and both are on the commits
        expected by :func:`read_off`'s rule for ``key`` (the run base tip just fetched, the
        PR's head), or until ``settle_max`` seconds have passed. The stop rule never looks at
        the files a check expects, so a wrong list is recorded as it is. Every read is kept.
        GitHub's compare of the base tip and the head (:meth:`_compare`) is recorded beside
        the reads."""
        pr = self.obs["prs"][iid]
        self._fetch()
        expect = {"baseRefOid": self._tip(),
                  "headRefOid": head or str(pr.get("pushed_head") or "")}
        no_push = key in NO_PUSH
        start = time.monotonic()
        time.sleep(self.settle)
        reads: list[dict] = []
        while True:
            reads.append({"at": round(time.monotonic() - start, 1),
                          **self._read_pr(pr.get("number"))})
            if (len(reads) >= 2 and not read_off(reads[-1], expect, no_push=no_push)
                    and not read_off(reads[-2], expect, no_push=no_push)
                    and reads[-1]["files"] == reads[-2]["files"]):
                break
            if time.monotonic() - start >= self.settle_max:
                break
            time.sleep(POLL)
        rec = self.obs[key] = {
            "reads": reads, "expect": expect,
            "synced": not read_off(reads[-1], expect, no_push=no_push),
            "seconds": round(time.monotonic() - start, 1), "window": self.settle_max,
            "compare": self._compare(expect["baseRefOid"], expect["headRefOid"])}
        self.save()
        return rec

    def _read_pr(self, number: object) -> dict:
        """One read of PR ``number`` (:data:`PR_READ`): its file list, ``baseRefOid``,
        ``headRefOid``, ``mergeable`` and ``potentialMergeCommit`` (GitHub's test merge, with
        its parents), or what ``gh`` said instead."""
        owner, _sep, name = self.repo.partition("/")
        r = gh("api", "graphql", "-f", f"query={PR_READ}", "-f", f"owner={owner}",
               "-f", f"name={name}", "-F", f"number={number}")
        try:
            data = json.loads(r.stdout or "{}") if r.returncode == 0 else None
        except ValueError:
            data = None
        pr = _obj(_obj(_obj(data, "data"), "repository"), "pullRequest")
        files = _obj(pr, "files").get("nodes")
        if not isinstance(files, list):
            return {"error": _said(r) if r.returncode != 0 else
                    f"no file list in the answer: {' '.join((r.stdout or '').split())[:300]}"}
        test = pr.get("potentialMergeCommit")
        parents = _obj(test, "parents").get("nodes")
        return {"files": sorted(str(f.get("path", "")) for f in files if isinstance(f, dict)),
                "baseRefOid": pr.get("baseRefOid"), "headRefOid": pr.get("headRefOid"),
                "mergeable": pr.get("mergeable"),
                "potentialMergeCommit": None if not isinstance(test, dict) else {
                    "oid": test.get("oid"),
                    "parents": [str(p.get("oid")) for p in parents if isinstance(p, dict)]
                    if isinstance(parents, list) else None}}

    def _compare(self, base: str, head: str) -> dict:
        """GitHub's compare of ``base...head`` (``gh api repos/<repo>/compare/<base>...<head>``:
        the change from the two commits' merge base to ``head``), which is what a PR from
        ``head`` would list if GitHub worked it out afresh against ``base``. Recorded beside
        the PR's own file list, not judged."""
        rec: dict = {"base": base, "head": head}
        if not base or not head:
            return {**rec, "error": "no base tip or head to compare"}
        r = gh("api", f"repos/{self.repo}/compare/{base}...{head}")
        try:
            info = json.loads(r.stdout or "{}") if r.returncode == 0 else None
        except ValueError:
            info = None
        if not isinstance(info, dict) or not isinstance(info.get("files"), list):
            return {**rec, "error": _said(r) if r.returncode != 0 else
                    "no file list in the answer"}
        return {**rec, "files": sorted(str(f.get("filename", "")) for f in info["files"]
                                       if isinstance(f, dict)),
                "merge_base": _obj(info, "merge_base_commit").get("sha"),
                "status": info.get("status"), "ahead_by": info.get("ahead_by"),
                "behind_by": info.get("behind_by")}

    def _after_merge(self, iid: str) -> dict:
        """``gh pr view <iid> --json state,headRefOid,mergeCommit`` after the merge and the
        branch delete, and whether the run base has each of the two commits."""
        url = str(self.obs["prs"][iid]["url"])
        view = self._view(url, "state,headRefOid,mergeCommit")
        if view is None:
            return {"view": None, "error": "gh pr view did not answer"}
        self._fetch()
        mc = view.get("mergeCommit")
        oid = mc.get("oid") if isinstance(mc, dict) else None
        ref = f"origin/{self.base}"
        return {"view": view, "base_tip": self._tip(),
                "on_base": {"headRefOid": self._ancestor(view.get("headRefOid"), ref),
                            "mergeCommit": self._ancestor(oid, ref)}}

    def _changes_on_base(self, *ids: str) -> dict:
        """Whether the run base holds each id's files with the content its patch wrote; None
        when git could not read the base."""
        ref = f"origin/{self.base}"
        out: dict = {}
        for iid in ids:
            seen = []
            for name in FILES[iid]:
                ls = git(self.checkout, "ls-tree", ref, "--", name)
                if ls.returncode != 0:
                    seen.append(None)
                elif not ls.stdout.strip():
                    seen.append(False)
                else:
                    show = git(self.checkout, "show", f"{ref}:{name}")
                    seen.append(None if show.returncode != 0 else show.stdout == f"{iid}\n")
            out[iid] = None if None in seen else all(seen)
        return out

    def _built_on(self, iid: str, *merged: str) -> dict:
        """What ``iid`` was built on (its stack-base tip, read when it was built; else the
        commit its PR branch was cut from, publish.json's ``cut_from``) and whether that
        commit holds each of ``merged``'s merged heads."""
        built = self.built.get(iid) or {}
        commit, source = str(built.get("tip") or ""), "stack-base tip"
        if not commit:
            rec = self.d.publish._publish_record(self.cfg.bundle(iid)) or {}
            commit, source = str(rec.get("cut_from") or ""), "publish.json cut_from"
        heads = {m: str((self.obs["merged"].get(m) or {}).get("merged_head") or "")
                 for m in merged}
        return {"commit": commit, "source": source if commit else "",
                "stack_base": built.get("stack_base") or "", "heads": heads,
                "holds": {m: self._ancestor(h, commit) for m, h in heads.items()}}

    # the scenario ------------------------------------------------------------------------

    def scenario(self) -> None:
        self.save()
        for what, step in (("run 1, A and C merged, B updated", self._run1),
                           ("run 2", self._run2), ("run 3, P merged", self._run3),
                           ("B merged", self._merge_b), ("run 4, S squash-merged", self._run4)):
            try:
                step()
            except Exception as exc:  # noqa: BLE001 — recorded; the next step may still run
                self.obs["errors"].append(f"{what}: {traceback.format_exc()}")
                print(f"run: {what} stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
            self.save()
        self.obs["finished"] = True
        self.save()

    def _run1(self) -> None:
        obs = self.obs
        why = run_problems(self._drive("run1"), RUNS["run1"], obs["prs"])
        if why:
            print(f"run: run1 did not finish ({'; '.join(why)}); what needs it reads "
                  f"INCOMPLETE", file=sys.stderr)
            return
        self._poll("B_files_opened", "B")
        a = self._merge("A", "merge")
        if a["merged"]:
            self._poll("B_files_after_A_merge", "B")
        c = self._merge("C", "merge")
        if a["merged"] and c["merged"]:
            self._poll("B_files_before_update", "B")
            upd = obs["B_update_branch"] = self._update_branch("B")
            self.save()
            if upd["ok"]:
                self._poll("B_files_after_update", "B", head=upd["head_after"])
        if a["merged"]:
            obs["A_after_merge"] = self._after_merge("A")

    def _run2(self) -> None:
        why = run2_preconditions(self.obs)
        if why:
            self.obs["runs"]["run2"] = {"ids": RUNS["run2"], "driven": False,
                                        "skipped": "; ".join(why)}
            print(f"run: run2 not driven: {'; '.join(why)}", file=sys.stderr)
            return
        self._drive("run2")
        self.obs["D_built_on"] = self._built_on("D", "A", "C")

    def _run3(self) -> None:
        obs = self.obs
        why = run_problems(self._drive("run3"), RUNS["run3"], obs["prs"])
        if why:
            print(f"run: run3 did not finish ({'; '.join(why)})", file=sys.stderr)
            return
        self._poll("Q_files_opened", "Q")
        if self._merge("P", "merge")["merged"]:
            self._poll("Q_files_after_P_merge", "Q")

    def _merge_b(self) -> None:
        merged = self.obs["merged"]
        if "B" not in self.obs["prs"] or not all(
                (merged.get(i) or {}).get("merged") for i in ("A", "C")):
            print("run: B not merged: it needs its PR and A's and C's merges", file=sys.stderr)
            return
        if self._merge("B", "merge")["merged"]:
            self.obs["after_B_merge"] = {"on_base": self._changes_on_base("A", "B", "C")}

    def _run4(self) -> None:
        why = run_problems(self._drive("run4"), RUNS["run4"], self.obs["prs"])
        if why:
            print(f"run: run4 did not finish ({'; '.join(why)})", file=sys.stderr)
            return
        if self._merge("S", "squash")["merged"]:
            self.obs["S_after_squash"] = self._after_merge("S")


def cmd_run(a: argparse.Namespace) -> int:
    token, why = guard(a.repo, a.token_file)
    if why:
        return _refuse("run", why)
    workdir = Path(a.workdir).expanduser().resolve()
    state = read_state(workdir)
    if state is None:
        return _refuse("run", f"no state.json from `setup` in {workdir}")
    if str(state.get("repo", "")).lower() != a.repo.lower():
        return _refuse("run", f"{workdir} was set up for {state.get('repo')}, not {a.repo}")
    if state.get("torn_down") or not state.get("ready"):
        return _refuse("run", f"the setup in {workdir} did not finish or was torn down; "
                              f"run `teardown`, then `setup` in a fresh --workdir")
    if (workdir / "observations.json").exists():
        return _refuse("run", f"{workdir} already holds a run; `teardown` it and `setup` a "
                              f"fresh --workdir")
    if _check_repo("run", a.repo, token, need_merge=True) is None:
        return RC_REFUSED
    live = Live(workdir, state, settle=a.settle, settle_max=a.settle_max)
    try:
        live.scenario()
    finally:
        live.save()
        print(f"run: observations written to {workdir / 'observations.json'}")
    checks, _info = judge(live.obs)
    missing = incomplete_reasons(checks)
    if missing:
        print("run: the observations are incomplete, so `judge` reports INCOMPLETE (this "
              "blocks accept; re-run in a fresh --workdir):", file=sys.stderr)
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
        return RC_INCOMPLETE
    print(f"run: every check has what it needs; next: python3 {HERE} judge --workdir {workdir}")
    return 0


# -- judge (offline) ------------------------------------------------------------------------

def _obj(d: object, key: str) -> dict:
    """``d[key]`` when it is an object, else {}: a hand-edited or truncated observations file
    reads as not observed, never as a crash."""
    v = d.get(key) if isinstance(d, dict) else None
    return v if isinstance(v, dict) else {}


def _last_line(text: object) -> str:
    lines = [x for x in str(text or "").strip().splitlines() if x.strip()]
    return lines[-1].strip() if lines else ""


def _tail(text: object, n: int = 15) -> list[str]:
    return [x.rstrip() for x in str(text or "").splitlines() if x.strip()][-n:]


def run_problems(rec: object, ids: list[str], prs: dict) -> list[str]:
    """Why a recorded run did not finish ``ids`` cleanly, or []: it was driven, raised
    nothing, exited 0 with no holds, and each id is COMPLETE with its PR read."""
    if not isinstance(rec, dict):
        return ["not recorded"]
    if not rec.get("driven"):
        return [f"not driven ({rec.get('skipped') or 'no reason recorded'})"]
    why = []
    if rec.get("raised"):
        why.append(f"raised {_last_line(rec['raised'])}")
    elif rec.get("rc") != 0:
        why.append(f"exit code {rec.get('rc')}")
    if rec.get("holds"):
        why.append(f"held {json.dumps(rec['holds'], sort_keys=True)}")
    states = _obj(rec, "states") or _obj(rec, "results")
    for iid in ids:
        if states.get(iid) != COMPLETE:
            why.append(f"{iid} is {states.get(iid) or 'not recorded'}")
        elif not isinstance(prs.get(iid), dict) or not prs[iid].get("url"):
            why.append(f"{iid} has no PR")
    return why


def merge_problems(merged: dict, iid: str, method: str, *, deleted: bool) -> list[str]:
    """What is missing for "``iid``'s PR merged with ``method``" (and, with ``deleted``, "its
    branch confirmed gone after"), or []."""
    m = merged.get(iid)
    if not isinstance(m, dict):
        return [f"{iid}'s merge was not recorded"]
    if m.get("merged") is not True:
        return [f"{iid}'s merge was refused: {m.get('refused') or 'not merged'}"]
    if m.get("method") != method:
        return [f"{iid} was merged with {m.get('method')!r}, not {method!r}"]
    if deleted and m.get("deleted") is not True:
        return [f"{iid}'s branch delete was not confirmed: {m.get('delete') or 'not recorded'}"]
    return []


def run2_preconditions(obs: dict) -> list[str]:
    """What run 2 needs observed first: A and C merged with merge commits, both branch deletes
    confirmed. With these observed, run 2's own outcome is E3's result."""
    merged = _obj(obs, "merged")
    return (merge_problems(merged, "A", "merge", deleted=True)
            + merge_problems(merged, "C", "merge", deleted=True))


# What a settled read of a polled PR file list shows besides its files (:func:`read_off`), by
# whether the run base moved with no push to the PR since (:data:`NO_PUSH`).
SETTLE_RULE = {
    False: "after a push to the PR: baseRefOid the run base tip, headRefOid the head pushed",
    True: "no push to the PR since the run base moved, so GitHub keeps its baseRefOid and file "
          "list: headRefOid unchanged, mergeable MERGEABLE, test merge on the run base tip and "
          "that head",
}


def _short(oid: object) -> str:
    return str(oid)[:12] if isinstance(oid, str) and oid else json.dumps(oid)


def read_off(rd: object, expect: dict, *, no_push: bool) -> list[str]:
    """Why one read of a polled PR file list is not on the commits ``run`` expected, or []
    when it is. ``run`` stops polling on this rule, and ``judge`` checks the recorded reads
    with it again. ``expect`` holds the run base tip ``run`` had just fetched and the PR's
    head. After a push to the PR, or when it opened, GitHub moves the PR's ``baseRefOid`` to
    the base tip, so the read must show that tip and the head. After the base moved with no
    push to the PR (``no_push``), GitHub keeps the PR's ``baseRefOid`` and file list, so the
    read must show the head unchanged and the mergeability recomputed against the base tip:
    ``mergeable`` MERGEABLE, and a test merge (``potentialMergeCommit``) whose parents are the
    base tip and the head."""
    if not isinstance(rd, dict) or not isinstance(rd.get("files"), list) or not rd["files"]:
        said = rd.get("error") if isinstance(rd, dict) else None
        return [f"has no file list ({said})" if said else "has no file list"]
    tip, head = str(expect.get("baseRefOid") or ""), str(expect.get("headRefOid") or "")
    if not tip or not head:
        return ["cannot be checked: `run` recorded no base tip or head to expect"]
    if not no_push:
        return [f"shows {k} {_short(rd.get(k))}, not {_short(v)}"
                for k, v in (("baseRefOid", tip), ("headRefOid", head)) if rd.get(k) != v]
    off = []
    if rd.get("headRefOid") != head:
        off.append(f"shows headRefOid {_short(rd.get('headRefOid'))}, not {_short(head)}")
    if rd.get("mergeable") != "MERGEABLE":
        off.append(f"shows mergeable {_short(rd.get('mergeable'))}, not MERGEABLE")
    test = rd.get("potentialMergeCommit")
    parents = test.get("parents") if isinstance(test, dict) else None
    if not isinstance(parents, list):
        off.append("shows no test merge (potentialMergeCommit)")
    elif sorted(str(p) for p in parents) != sorted((tip, head)):
        on = " + ".join(_short(p) for p in parents) or "no parents"
        off.append(f"shows its test merge on {on}, not on {_short(tip)} + {_short(head)}")
    return off


def settled_files(obs: dict, key: str, what: str) -> tuple[list[str] | None, str, dict]:
    """``(files, missing, seen)`` for the polled file list ``obs[key]``: the list the last two
    reads agree on, else None and why it is not observed. Agreeing is not enough: both reads
    must be on the commits ``run`` expected (``expect``: the run base tip it had just fetched,
    and the PR's head) by :func:`read_off`'s rule for ``key`` (:data:`NO_PUSH`), and ``run``
    must have recorded the poll as synced. A poll that ran out of time can end on two
    identical reads GitHub served before it caught up with a merge or an update, and those say
    nothing about the list after it. ``seen`` has the reads behind it, what the last one shows
    besides the files, and GitHub's compare of the base tip and the head, recorded beside."""
    rec = _obj(obs, key)
    reads = rec.get("reads") if isinstance(rec.get("reads"), list) else []
    lists = [r.get("files") if isinstance(r, dict) else None for r in reads]
    last = lists[-1] if lists else None
    expect = _obj(rec, "expect")
    no_push = key in NO_PUSH
    final = reads[-1] if reads and isinstance(reads[-1], dict) else {}
    seen = {"files": last, "compare": rec.get("compare"), "reads": len(reads),
            "seconds": rec.get("seconds"), "synced": rec.get("synced"),
            "expect": expect or None, "settle rule": SETTLE_RULE[no_push],
            "last read": {k: final.get(k) for k in ("baseRefOid", "headRefOid", "mergeable",
                                                    "potentialMergeCommit")} if final else None}
    if not reads:
        return None, f"{what} was not read", seen
    if (len(lists) < 2 or not isinstance(lists[-1], list) or not isinstance(lists[-2], list)
            or sorted(lists[-1]) != sorted(lists[-2])):
        return None, (f"{what} did not settle: the last two of {len(reads)} read(s) over "
                      f"{rec.get('seconds')} s are not one list"), seen
    want = {k: expect.get(k) for k in ("baseRefOid", "headRefOid")}
    unset = [k for k, v in want.items() if not isinstance(v, str) or not v]
    if unset:
        return None, (f"{what}: `run` recorded no {' or '.join(unset)} the PR should be on, "
                      f"so its reads cannot be checked"), seen
    off = [f"read {n + 1} of {len(reads)} {why}" for n in (len(reads) - 2, len(reads) - 1)
           for why in read_off(reads[n], want, no_push=no_push)]
    if off and no_push:
        return None, (f"{what}: the last two reads do not both show the PR's head unchanged "
                      f"and its mergeability recomputed against the run base tip "
                      f"({'; '.join(off)}), so GitHub may not have taken in the move of the "
                      f"base yet"), seen
    if off:
        return None, (f"{what}: the last two reads are not both on the commits `run` "
                      f"expected ({'; '.join(off)}), so the list may be GitHub's answer from "
                      f"before the merge or update"), seen
    if rec.get("synced") is not True:
        return None, (f"{what} did not settle: `run` recorded the poll as not synced "
                      f"(synced: {json.dumps(rec.get('synced'))}) after {len(reads)} read(s) "
                      f"over {rec.get('seconds')} s"), seen
    return sorted(str(f) for f in last), "", seen


def _ancestry(on: dict, key: str, what: str, present: bool) -> tuple[object, str]:
    """``(value, missing)`` for a recorded ``_ancestor`` answer: True / False, or None with
    why it is not observed (git exited with neither 0 nor 1)."""
    rec = on.get(key)
    value = rec.get("value") if isinstance(rec, dict) else None
    if present and value not in (True, False):
        rc = rec.get("rc") if isinstance(rec, dict) else None
        return None, f"whether the run base has {what}: git merge-base exited {rc}"
    return value, ""


def gone_lines(stderr: str, iid: str, branch: str, base: str) -> dict | None:
    """The line run 2's fold printed for ``iid``'s deleted branch when it went on: one of
    ``integrate._gone_branch``'s two success messages, matched whole
    (template/src/pdca_harness/integrate.py:1434-1436, the line already carries the merged
    head; :1509-1511, the merged work reaches the line through the base). None when neither
    names it. The errors at :1443, :1449, :1491 and :1503 open with the same words, so the
    shared opening alone never counts."""
    if not branch:
        return None
    head = (r"integrate: issue_" + re.escape(iid) + r"'s branch origin/" + re.escape(branch)
            + r" is gone and its PR merged at (?P<at>[0-9a-f]+)")
    skipped = re.compile(head + r" — (?P<line>\S+) already carries that commit; skipped\.")
    through = re.compile(head + r", which (?P<line>\S+) did not have — the merged work "
                         r"reaches (?P=line) through " + re.escape(f"origin/{base}") + r"\.")
    for raw in stderr.splitlines():
        text = raw.rstrip()
        for kind, rx in (("already carries it; skipped", skipped),
                         ("reaches the line through the base", through)):
            m = rx.fullmatch(text)
            if m:
                return {"message": kind, "merged at": m.group("at"), "line": m.group("line")}
    return None


def _result(missing: list[str], ok: bool) -> str:
    return INCOMPLETE if missing else PASS if ok else FAIL


def judge(obs: dict) -> tuple[list[dict], list[dict]]:
    """``(checks, info)`` for one observations document. Reads nothing else."""
    base = obs.get("run_base") if isinstance(obs.get("run_base"), str) else ""
    files = obs.get("files") if isinstance(obs.get("files"), dict) else FILES
    own = {iid: sorted(str(f) for f in files.get(iid, FILES[iid])) for iid in IDS}
    prs, runs, merged = _obj(obs, "prs"), _obj(obs, "runs"), _obj(obs, "merged")
    checks: list[dict] = []
    info: list[dict] = []

    def add(cid: str, result: str, expected: str, observed: dict, missing: list[str]) -> None:
        if missing:
            observed = {"not observed": missing, **observed}
        checks.append({"id": cid, "result": result, "expected": expected,
                       "observed": observed})

    # E0 — every PR the runs opened is against the run base. A, B, C, P, Q and S always need
    # a PR; D only when run 2 finished it (run 2's own outcome is E3's). One PR read against
    # another base is a FAIL whatever else is missing: that observation is complete.
    bases = {iid: pr.get("baseRefName") for iid, pr in prs.items()
             if iid in IDS and isinstance(pr, dict) and "baseRefName" in pr}
    wrong = {iid: b for iid, b in bases.items() if b != base} if base else {}
    d_done = _obj(_obj(runs, "run2"), "states").get("D") == COMPLETE
    missing = [] if base else ["the run base was not recorded"]
    missing += [f"{iid} has no PR read ({_outcome(runs, iid)})" for iid in IDS
                if iid not in bases and (iid != "D" or d_done)]
    add("E0", FAIL if wrong else _result(missing, True),
        f"every PR the runs opened has baseRefName {base!r} (the run base), never the "
        f"integration line or {MAIN}", {"baseRefName": bases, "wrong": wrong},
        [] if wrong else missing)

    for key, iid, note in (
            ("B_files_opened", "B opened", "B's PR files when it opened"),
            ("B_files_after_A_merge", "B after A", "B's PR files after A merged"),
            ("B_files_before_update", "B before Update branch",
             "B's PR files after A and C merged, before Update branch; reported, not judged: "
             "run 1's line joins two wave-0 branches, so it is no plain chain "
             "(docs/07-crosscutting.md:655-661)"),
            ("Q_files_opened", "Q opened", "Q's PR files before P merged")):
        if key in obs:
            _got, why, seen = settled_files(obs, key, note.split(";", 1)[0])
            info.append({"id": iid, "note": note,
                         "observed": {**seen, "not settled": why} if why else seen})

    # E1 — after A and C merged and Update branch, B's PR shows only B's own files.
    upd = _obj(obs, "B_update_branch")
    got, why, seen = settled_files(obs, "B_files_after_update", "B's PR files after Update "
                                                                "branch")
    missing = (run_problems(runs.get("run1"), RUNS["run1"], prs)
               + merge_problems(merged, "A", "merge", deleted=False)
               + merge_problems(merged, "C", "merge", deleted=False))
    if not missing and upd.get("ok") is not True:
        missing.append(f"Update branch on B's PR: {upd.get('refused') or 'not recorded'}")
    elif not missing and why:
        missing.append(why)
    add("E1", _result(missing, got == own["B"]),
        f"after A and C merge (merge commits) and Update branch, B's PR files are exactly "
        f"{own['B']}", {**seen, "Update branch": upd or None}, missing)

    # E1b — plain chain: after P merges, Q's PR shows only Q's own files, no Update branch.
    got, why, seen = settled_files(obs, "Q_files_after_P_merge", "Q's PR files after P merged")
    missing = (run_problems(runs.get("run3"), RUNS["run3"], prs)
               + merge_problems(merged, "P", "merge", deleted=False))
    if not missing and why:
        missing.append(why)
    add("E1b", _result(missing, got == own["Q"]),
        f"plain chain: after P merges (merge commit), with no Update branch, Q's PR files are "
        f"exactly {own['Q']}", seen, missing)

    # E2 — A after merge + branch delete: MERGED, the pushed head, a merge commit on the base.
    a = _obj(obs, "A_after_merge")
    view, on = _obj(a, "view"), _obj(a, "on_base")
    oid = _obj(view, "mergeCommit").get("oid")
    pushed = _obj(prs, "A").get("pushed_head")
    missing = (run_problems(runs.get("run1"), RUNS["run1"], prs)
               + merge_problems(merged, "A", "merge", deleted=True))
    if not view:
        missing.append(f"A's gh pr view after the delete: {a.get('error') or 'not read'}")
    if not pushed:
        missing.append("the head the run pushed for A was not read")
    mc_on, why = _ancestry(on, "mergeCommit", f"A's mergeCommit {str(oid)[:12]}", bool(oid))
    missing += [why] if why else []
    head_on, _why = _ancestry(on, "headRefOid", "A's headRefOid", False)
    add("E2", _result(missing, view.get("state") == "MERGED" and view.get("headRefOid") == pushed
                      and bool(oid) and mc_on is True),
        "A after merge (merge commit) + branch delete: state MERGED, headRefOid equal to the "
        "head the run pushed, mergeCommit.oid present and on the run base",
        {"state": view.get("state"), "headRefOid": view.get("headRefOid"),
         "pushed head": pushed, "mergeCommit.oid": oid, "mergeCommit on run base": mc_on,
         "headRefOid on run base": head_on}, missing)

    checks.append(_e3(obs, prs, runs, base))

    # E4 — S after squash + delete: merge commit on the base, PR head not on it.
    s = _obj(obs, "S_after_squash")
    view, on = _obj(s, "view"), _obj(s, "on_base")
    oid, head = _obj(view, "mergeCommit").get("oid"), view.get("headRefOid")
    missing = (run_problems(runs.get("run4"), RUNS["run4"], prs)
               + merge_problems(merged, "S", "squash", deleted=True))
    if not view:
        missing.append(f"S's gh pr view after the delete: {s.get('error') or 'not read'}")
    mc_on, why1 = _ancestry(on, "mergeCommit", f"S's mergeCommit {str(oid)[:12]}", bool(oid))
    head_on, why2 = _ancestry(on, "headRefOid", f"S's headRefOid {str(head)[:12]}", bool(head))
    missing += [w for w in (why1, why2) if w]
    add("E4", _result(missing, view.get("state") == "MERGED" and bool(oid) and bool(head)
                      and mc_on is True and head_on is False),
        "S after squash + branch delete: mergeCommit.oid on the run base, headRefOid NOT on it "
        "(the shape #626 assumes)",
        {"state": view.get("state"), "headRefOid": head, "headRefOid on run base": head_on,
         "mergeCommit.oid": oid, "mergeCommit on run base": mc_on}, missing)

    # E5 — after B merges, A's, B's and C's changes are all on the run base.
    landed = _obj(obs, "after_B_merge").get("on_base")
    landed = landed if isinstance(landed, dict) else {}
    missing = (run_problems(runs.get("run1"), RUNS["run1"], prs)
               + [m for i in ("A", "C", "B")
                  for m in merge_problems(merged, i, "merge", deleted=False)])
    if not missing:
        missing += [f"whether the run base has {i}'s change was not read" for i in "ABC"
                    if landed.get(i) not in (True, False)]
    add("E5", _result(missing, all(landed.get(i) is True for i in "ABC")),
        "after A, C, then B merge with merge commits, A's, B's and C's changes are all on the "
        "run base", {"on run base": landed}, missing)
    order = {cid: n for n, cid in enumerate(CHECK_IDS)}
    checks.sort(key=lambda c: order[c["id"]])
    return checks, info


def _outcome(runs: dict, iid: str) -> str:
    """What the last recorded run naming ``iid`` left it as, e.g. ``run1: PLANNED``."""
    for name in reversed(list(RUNS)):
        rec = runs.get(name)
        if iid in RUNS[name] and isinstance(rec, dict):
            if not rec.get("driven"):
                return f"{name} not driven"
            return f"{name}: {_obj(rec, 'states').get(iid) or 'not recorded'}"
    return "no run of it recorded"


def _e3(obs: dict, prs: dict, runs: dict, base: str) -> dict:
    """E3 — run 2 (A B C D named, after A and C merged and lost their branches) finishes:
    exit 0, no holds, D COMPLETE; the fold names A's and C's branches gone with one of
    ``_gone_branch``'s success messages; D is built on a commit holding A's and C's merged
    heads. INCOMPLETE only when run 2's preconditions were not observed; once they were, a run
    2 that raised or exited non-zero is the FAIL, with what it said."""
    expected = ("run 2 (A B C D named; A and C merged with merge commits, their branches "
                "deleted): exit 0, no holds, D COMPLETE; for A's and C's branches the fold "
                "prints a success line of integrate._gone_branch (integrate.py:1434-1436 or "
                ":1509-1511); D built on a commit holding A's and C's merged heads")
    missing = run2_preconditions(obs)
    rec = runs.get("run2")
    if not missing and not isinstance(rec, dict):
        missing = ["run2 was not recorded"]
    elif not missing and not rec.get("driven"):
        missing = [f"run2 was not driven: {rec.get('skipped') or 'no reason recorded'}"]
    if missing:
        return {"id": "E3", "result": INCOMPLETE, "expected": expected,
                "observed": {"not observed": missing}}
    err = str(rec.get("stderr") or "")
    branch = {i: str(_obj(prs, i).get("headRefName")
                     or _obj(_obj(obs, "merged"), i).get("branch") or "") for i in ("A", "C")}
    took = {i: gone_lines(err, i, branch[i], base) for i in ("A", "C")}
    not_carried = [x.strip() for x in err.splitlines() if re.match(
        r"flow: issue_\w+ \(finished in an earlier run\) is not carried", x.strip())]
    d_state = _obj(rec, "states").get("D") or _obj(rec, "results").get("D")
    fails = []
    if rec.get("raised"):
        fails.append(f"run2 raised {_last_line(rec['raised'])}")
    elif rec.get("rc") != 0:
        fails.append(f"run2 exited {rec.get('rc')}")
    if rec.get("holds"):
        fails.append(f"run2 held {json.dumps(rec['holds'], sort_keys=True)}")
    if d_state != COMPLETE:
        fails.append(f"D is {d_state or 'not recorded'}")
    fails += [f"no success line of _gone_branch for {i}'s branch origin/{branch[i]}"
              for i in ("A", "C") if not took[i]]
    built = _obj(obs, "D_built_on")
    holds = _obj(built, "holds")
    held = {i: (_obj(holds, i).get("value") if isinstance(holds.get(i), dict) else None)
            for i in ("A", "C")}
    unread = []
    if not fails:
        if not built.get("commit"):
            unread.append("what D was built on was not read")
        unread += [f"whether D's base {str(built.get('commit'))[:12]} holds {i}'s merged head: "
                   f"git merge-base exited {_obj(holds, i).get('rc')}"
                   for i in ("A", "C") if built.get("commit") and held[i] not in (True, False)]
        fails += [f"D's base {str(built.get('commit'))[:12]} lacks {i}'s merged head"
                  for i in ("A", "C") if held[i] is False]
    observed = {"rc": rec.get("rc"), "exception": _last_line(rec.get("raised")) or None,
                "holds": rec.get("holds") or {}, "D": d_state, "gone branch lines": took,
                "not carried": not_carried, "D built on": built.get("commit") or None,
                "D's base holds": held, "failed": fails,
                "stderr tail": _tail(err) + _tail(rec.get("raised"), 5)}
    result = FAIL if fails else INCOMPLETE if unread else PASS
    if unread:
        observed = {"not observed": unread, **observed}
    return {"id": "E3", "result": result, "expected": expected, "observed": observed}


def incomplete_reasons(checks: list[dict]) -> list[str]:
    return [f"{c['id']}: {m}" for c in checks if c["result"] == INCOMPLETE
            for m in _obj(c, "observed").get("not observed") or ["not observed"]]


def verdict(checks: list[dict]) -> str:
    """INCOMPLETE when any check is (it blocks accept); else FAIL when any fails; else PASS."""
    results = {c["result"] for c in checks}
    return INCOMPLETE if INCOMPLETE in results else FAIL if FAIL in results else PASS


def report_md(obs: dict, checks: list[dict], info: list[dict]) -> str:
    result = verdict(checks)
    lines = ["# Stack mode on real GitHub: live check report", "",
             f"- Repository: `{obs.get('repo', '?')}`, run base `{obs.get('run_base', '?')}`",
             f"- Started: {obs.get('started', '?')}",
             f"- Result: **{result}**"]
    if result == INCOMPLETE:
        lines.append("- Not everything a check needs was observed, so this run cannot close "
                     "#625: fix the cause and re-run in a fresh work directory. Missing:")
        lines += [f"  - {m}" for m in incomplete_reasons(checks)]
    elif result == FAIL:
        lines.append("- Each FAIL is a complete observation that disagrees with the "
                     "expectation: file it as its own bug.")
    lines += ["", "| Check | Result | Expected | Observed |", "|---|---|---|---|"]
    for c in checks:
        seen = json.dumps(c["observed"], sort_keys=True).replace("|", "\\|")
        lines.append(f"| {c['id']} | {c['result']} | {c['expected']} | `{seen}` |")
    lines += ["", "## Runs", ""]
    for name, ids in RUNS.items():
        rec = _obj(obs, "runs").get(name)
        if not isinstance(rec, dict):
            lines.append(f"- {name} ({' '.join(ids)}): not recorded")
        elif not rec.get("driven"):
            lines.append(f"- {name} ({' '.join(ids)}): not driven: {rec.get('skipped')}")
        else:
            lines.append(f"- {name} ({' '.join(ids)}): exit {rec.get('rc')}, "
                         f"holds {json.dumps(rec.get('holds') or {})}, "
                         f"states {json.dumps(rec.get('states') or {}, sort_keys=True)}"
                         + (f", raised `{_last_line(rec.get('raised'))}`"
                            if rec.get("raised") else ""))
    if info:
        lines += ["", "## Recorded, not judged", ""]
        lines += [f"- {i['id']} ({i['note']}): `{json.dumps(i['observed'])}`" for i in info]
    for e in obs.get("errors") or []:
        lines += ["", "- run error:", "", "```", str(e).rstrip(), "```"]
    return "\n".join(lines) + "\n"


def cmd_judge(a: argparse.Namespace) -> int:
    workdir = Path(a.workdir).expanduser().resolve()
    src = Path(a.observations).expanduser() if a.observations else workdir / "observations.json"
    try:
        obs = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"judge: cannot read {src}: {exc}", file=sys.stderr)
        return RC_REFUSED
    if not isinstance(obs, dict):
        print(f"judge: {src} is not an observations document", file=sys.stderr)
        return RC_REFUSED
    checks, info = judge(obs)
    result = verdict(checks)
    workdir.mkdir(parents=True, exist_ok=True)
    write_json(workdir / "report.json",
               {"tool": TOOL, "result": result, "repo": obs.get("repo"),
                "run_base": obs.get("run_base"), "checks": checks, "info": info,
                "incomplete": incomplete_reasons(checks), "errors": obs.get("errors") or []})
    (workdir / "report.md").write_text(report_md(obs, checks, info), encoding="utf-8")
    for c in checks:
        print(f"{c['id']}: {c['result']}")
    print(f"judge: {result}; report in {workdir / 'report.md'}")
    return 0 if result == PASS else RC_INCOMPLETE if result == INCOMPLETE else RC_FAIL


# -- teardown -------------------------------------------------------------------------------

def cmd_teardown(a: argparse.Namespace) -> int:
    """Close every still-open PR whose head is a run branch, then delete the run base, every
    branch ``state.json`` records and every remote branch under the run's integration prefix
    (``integrate.integration_branch(cfg, base)``, integrate.py:140-157: the lines and the
    per-bundle bases, :160, all start with it). Never ``main`` or the default branch, never
    the repository or its settings. Then remove what ``setup`` made locally (the clone, its
    integration worktrees, the bundle root); ``state.json``, the observations and the report
    stay. ``--keep`` changes nothing."""
    token, why = guard(a.repo, a.token_file)
    if why:
        return _refuse("teardown", why)
    workdir = Path(a.workdir).expanduser().resolve()
    state = read_state(workdir)
    if state is None:
        return _refuse("teardown", f"no state.json from `setup` in {workdir}")
    repo, base = str(state.get("repo") or ""), str(state.get("run_base") or "")
    if repo.lower() != a.repo.lower():
        return _refuse("teardown", f"{workdir} was set up for {repo}, not {a.repo}")
    if not base.startswith("live-") or base == MAIN:
        return _refuse("teardown", f"state.json names {base!r} as the run base, not a "
                                   f"live-<stamp> branch")
    recorded = [b for b in state.get("branches") or [] if isinstance(b, str) and b]
    prs = [p for p in (state.get("prs") or {}).values() if isinstance(p, dict)]
    if a.keep:
        print(f"teardown: --keep: leaving {base}, {len(recorded)} recorded branch(es) and "
              f"{len(prs)} PR(s) on {repo}, and {workdir}")
        return 0
    info = _check_repo("teardown", a.repo, token, need_merge=False)
    if info is None:
        return RC_REFUSED
    protected = {MAIN, str(info.get("default_branch") or MAIN)}
    failed: list[str] = []
    prefix = ""
    try:
        drv = _driver()
        prefix = drv.integrate.integration_branch(make_config(drv, workdir, repo, base), base)
    except Exception as exc:  # noqa: BLE001 — the recorded branches still go
        failed.append(f"could not work out the integration prefix: {exc}")
    listed: list[str] = []
    if prefix:
        r = gh("api", "--paginate", f"repos/{repo}/git/matching-refs/heads/{prefix}",
               "--jq", ".[].ref")
        if r.returncode != 0:
            failed.append(f"could not list the branches under {prefix}: {_said(r)}")
        listed = [x.strip().removeprefix("refs/heads/") for x in r.stdout.splitlines()
                  if x.strip().startswith(f"refs/heads/{prefix}")]
    branches = list(dict.fromkeys([base, *recorded, *listed]))
    branches = [b for b in branches if b not in protected]
    numbers = {p.get("number") for p in prs if isinstance(p.get("number"), int)}
    r = gh("pr", "list", "--repo", repo, "--state", "open", "--limit", "1000",
           "--json", "number,url,headRefName")
    try:
        open_prs = json.loads(r.stdout or "[]") if r.returncode == 0 else None
    except ValueError:
        open_prs = None
    if not isinstance(open_prs, list):
        failed.append(f"could not list the open PRs: {_said(r)}")
        open_prs = []
    for pr in open_prs:
        if not isinstance(pr, dict):
            continue
        if pr.get("headRefName") in branches or pr.get("number") in numbers:
            c = gh("pr", "close", str(pr.get("number")), "--repo", repo)
            print(f"teardown: close PR #{pr.get('number')} ({pr.get('headRefName')}): "
                  f"{'closed' if c.returncode == 0 else _said(c)}")
            if c.returncode != 0:
                failed.append(f"PR #{pr.get('number')}: {_said(c)}")
    gone = []
    for b in branches:
        d = gh("api", "-X", "DELETE", f"repos/{repo}/git/refs/heads/{b}")
        if d.returncode == 0:
            how = "deleted"
        elif "Reference does not exist" in _said(d):
            how = "already gone"
        else:
            how = f"NOT deleted: {_said(d)}"
            failed.append(f"branch {b}: {_said(d)}")
        gone.append({"branch": b, "result": how})
        print(f"teardown: {b}: {how}")
    # Only what `setup` and the driver made here: the clone, its integration worktrees and
    # their lock files (siblings named `checkout.pdca-…`, integrate.py:298-303, :324), and
    # the bundle root. Never anything else in the directory the user named.
    removed = []
    for p in sorted(workdir.iterdir()):
        if p.name in ("checkout", "project") or p.name.startswith("checkout.pdca-"):
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
            removed.append(p.name)
    state["torn_down"] = {"at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                          "branches": gone, "failed": failed, "removed": removed}
    write_json(workdir / "state.json", state)
    for f in failed:
        print(f"teardown: {f}", file=sys.stderr)
    print(f"teardown: {'done' if not failed else 'INCOMPLETE, run it again'}; kept "
          f"{', '.join(n for n in RECORDS if (workdir / n).exists())} in {workdir}")
    return RC_FAIL if failed else 0


# -- command line ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="stack_mode_live.py",
                                description=__doc__.split("\n", 1)[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def remote(sp: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sp.add_argument("--repo", default=DEFAULT_REPO,
                        help=f"the sandbox repository (default {DEFAULT_REPO})")
        sp.add_argument("--token-file", default=DEFAULT_TOKEN_FILE,
                        help=f"the sandbox token (default {DEFAULT_TOKEN_FILE})")
        return sp

    remote(sub.add_parser("preflight", help="check the token and the repository; write "
                                            "nothing")).set_defaults(fn=cmd_preflight)
    s = remote(sub.add_parser("setup", help="create the run base, the clone and the bundles"))
    s.add_argument("--workdir", required=True)
    s.set_defaults(fn=cmd_setup)
    r = remote(sub.add_parser("run", help="drive the scenario; write observations.json"))
    r.add_argument("--workdir", required=True)
    r.add_argument("--settle", type=float, default=10.0,
                   help="seconds before the first read of a PR's file list (default 10)")
    r.add_argument("--settle-max", type=float, default=180.0,
                   help="seconds to keep reading a PR's file list until two reads agree "
                        "(default 180)")
    r.set_defaults(fn=cmd_run)
    j = sub.add_parser("judge", help="evaluate observations.json; write report.md/json")
    j.add_argument("--workdir", required=True)
    j.add_argument("--observations", default="",
                   help="read this file instead of WORKDIR/observations.json")
    j.set_defaults(fn=cmd_judge)
    t = remote(sub.add_parser("teardown", help="close the run's PRs, delete its branches"))
    t.add_argument("--workdir", required=True)
    t.add_argument("--keep", action="store_true", help="change nothing")
    t.set_defaults(fn=cmd_teardown)
    a = p.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):          # keep step lines in order with stderr
        sys.stdout.reconfigure(line_buffering=True)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
