"""PR-review triage (issue #316) — ingest a published PR's external review findings
into the Act ledger.

The outer improvement loop's highest-value signal is what EXTERNAL reviewers find on
published PRs — and today it is invisible to Act: the pipeline stops at the draft PR,
and the ledger only receives what a human remembers to transcribe. ``pdca triage <pr>``
closes that gap deterministically:

  (a) pull the PR's reviews + review comments via ``gh api`` (the same
      ``subprocess.run(["gh", …])`` pattern as publish/cleanup's gh machinery),
      following pagination to the last page — "every finding" includes the 101st;
  (b) classify each finding into one of four classes — BUG / CONVENTION / NOISE /
      TEST-GAP — by keyword heuristics, with each class's keyword list overridable
      from the instance rubric's class list (#314) where one is configured;
  (c) route by class: a BUG on a MERGED PR files a tracker issue (the one piece of
      tracker-side automation in scope, via split's existing gh machinery) whose body
      carries a carry-forward note the next cycle's Plan reads from the ticket;
      CONVENTION appends a candidate gate row / rubric line to the act log; NOISE a
      candidate rubric-exclusion entry; TEST-GAP a candidate test note. The act log
      is the CEILING: this command proposes — it never edits ``pdca.toml``, never
      files gate rows, never touches the rubric itself;
  (d) register EVERY finding through :func:`act.register_signals` under a class-keyed
      signal name, so :func:`act.recurrences` flags a class that reappears after its
      process delta was applied — the existing #149 ledger becomes the recurrence
      tracker for external findings too.

## The signal-name grammar (stable across runs — recurrence matching depends on it)

    codex-pr:<class-slug>[-<keyword-slug>]     e.g.  codex-pr:convention-docstring

``codex-pr:`` marks an external-PR-review finding; the slug is the finding's CLASS
plus the keyword that decided it. Both come from the keyword table (built-in or
rubric-supplied), never from the free comment text — so the same class of miss maps
to the same signal on every run, which is what lets ``recurrences()`` match a
reappearance. A finding no keyword reaches registers as ``codex-pr:unclassified``
(and is listed for the human, or for the config-gated model pass below).

## Classification is heuristic-first, deliberately

Deterministic, cheap, auditable. Precedence on a multi-class match is severity-first
(BUG > TEST-GAP > CONVENTION > NOISE): "nit: this crashes" is a bug someone softened,
and mis-filing a real bug as noise buries it, while the reverse only files a
too-serious candidate a human then downgrades. The optional single MODEL pass runs
only over the unclassified remainder and only when ``[triage].model_cmd`` is set
(off by default) — keyword-only is complete and useful on its own.

Re-running on the same PR is the normal case (findings arrive over days, after
publish): a re-run ingests only findings NOT yet in the PR's stored triage record
(``process/triage/pr-<repo>-<n>.json``), and never re-files a tracker issue for a
finding it already filed — tracker issues cannot be rolled back (the split rule).
The record doubles as the recovery journal: findings are written ``pending`` and
the flag clears only after the ledger write and act-log append complete, so a run
interrupted between the two (a held Act session, a crash) is FINISHED by the next
run — never silently dropped by the "no new findings" fast path.
"""

from __future__ import annotations

import datetime
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from . import act, publish, rubric, split
from .config import Config

#: Prefix marking an external-PR-review signal in the Act ledger (grammar above).
SIGNAL_PREFIX = "codex-pr"

#: The four routing classes — the engine contract, in severity-first precedence order.
#: The rubric can retune each class's KEYWORDS; the class set (and its routing) is fixed.
CLASSES = ("BUG", "TEST-GAP", "CONVENTION", "NOISE")

#: Built-in keyword table (case-insensitive, whole-word/phrase match). An instance
#: rubric (#314) overrides one class's keywords with a class-list line like
#: ``- BUG: crash, race, corrupt``; classes the rubric doesn't name keep these, and
#: instances without a rubric get exactly this table (see :func:`class_keywords`).
DEFAULT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "BUG": ("bug", "broken", "crash", "crashes", "incorrect", "wrong", "corrupt",
            "corrupts", "race", "leak", "regression", "off-by-one", "traceback",
            "exception", "error", "fails", "failure", "data loss", "silently"),
    "TEST-GAP": ("untested", "no test", "missing test", "test gap", "coverage",
                 "not covered", "add a test", "needs a test", "vacuous test"),
    "CONVENTION": ("convention", "style", "naming", "docstring", "typo",
                   "formatting", "lint", "house style", "prefer"),
    "NOISE": ("nit", "nitpick", "cosmetic", "non-blocking", "optional",
              "feel free"),
}


@dataclass
class Finding:
    """One external review finding — a review body or an inline review comment."""

    source: str          # "review" | "review-comment"
    author: str
    text: str
    url: str = ""
    path: str = ""       # inline comments only: the file commented on
    cls: str = ""        # one of CLASSES, or "" = unclassified
    keyword: str = ""    # the keyword that decided the class ("" for a model verdict)

    @property
    def signal(self) -> str:
        """The class-keyed ledger signal (grammar in the module docstring)."""
        if not self.cls:
            return f"{SIGNAL_PREFIX}:unclassified"
        slug = _slug(self.cls + (f" {self.keyword}" if self.keyword else ""))
        return f"{SIGNAL_PREFIX}:{slug}"


def _slug(s: str) -> str:
    """Slugify via publish's slugifier — one grammar for every harness-made name."""
    return publish._slugify(s)


# ----------------------------------------------------------------------------
# (a) pull — the gh subprocess pattern publish/cleanup use (cleanup.py:66).
# ----------------------------------------------------------------------------
def _gh(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def _api(path: str):
    """Parsed JSON from ``gh api <path>``, or ``None`` on any failure (gh error,
    unparseable output). The CALLER fails closed: a triage that silently ingested
    half a PR's findings would under-register signals — the one direction this
    command must not degrade in."""
    proc = _gh(["api", path])
    if proc.returncode != 0:
        if (proc.stderr or "").strip():
            print((proc.stderr or "").strip(), file=sys.stderr)
        return None
    try:
        return json.loads(proc.stdout or "")
    except ValueError:
        return None


_PER_PAGE = 100


def _api_list(path: str) -> list | None:
    """EVERY item of a list endpoint, following pagination: ``page=1, 2, …`` until a
    short page. One ``per_page=100`` fetch is not enough — a PR can carry more than
    100 reviews or comments, and a single-page pull would silently drop the rest,
    violating "register every finding" in the one direction this command must not
    degrade in. Explicit page numbers rather than ``gh api --paginate`` because that
    flag concatenates the pages' JSON arrays back-to-back (unparseable as one
    document) unless a recent gh adds ``--slurp`` — a version dependency this
    stdlib loop avoids. ``None`` on any failed or non-list page: the caller fails
    closed, a partial pull is no better than a failed one."""
    items: list = []
    page = 1
    while True:
        data = _api(f"{path}?per_page={_PER_PAGE}&page={page}")
        if not isinstance(data, list):
            return None
        items.extend(data)
        if len(data) < _PER_PAGE:
            return items
        page += 1


_PR_URL_RE = re.compile(r"https?://[^/\s]+/([\w.-]+/[\w.-]+)/pull/(\d+)")
_PR_SHORT_RE = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")


def parse_pr(pr: str, repo: str = "") -> tuple[str, str]:
    """``(owner/repo, number)`` from a PR URL, ``OWNER/REPO#N``, or a bare number
    plus an explicit ``repo``; ``("", "")`` when unresolvable. A bare number WITHOUT
    a repo is refused rather than guessed: gh's checkout-default repository could
    hold an unrelated same-numbered PR (the same fail-closed rule as cleanup's
    ``--repo`` derivation, #300)."""
    pr = (pr or "").strip()
    m = _PR_URL_RE.search(pr)
    if m:
        return m.group(1), m.group(2)
    m = _PR_SHORT_RE.match(pr)
    if m:
        return m.group(1), m.group(2)
    if pr.lstrip("#").isdigit() and repo.strip():
        return repo.strip(), pr.lstrip("#")
    return "", ""


def _findings(reviews, comments) -> list[Finding]:
    """Findings out of the two gh api payloads. A body-less review (a bare APPROVED /
    CHANGES_REQUESTED event) carries no finding text and is skipped; everything with
    text counts — filtering is the classifier's job, not the extractor's."""
    out: list[Finding] = []
    for r in reviews if isinstance(reviews, list) else []:
        if not isinstance(r, dict):
            continue
        body = str(r.get("body") or "").strip()
        if body:
            out.append(Finding(source="review", author=_login(r), text=body,
                               url=str(r.get("html_url") or "")))
    for c in comments if isinstance(comments, list) else []:
        if not isinstance(c, dict):
            continue
        body = str(c.get("body") or "").strip()
        if body:
            out.append(Finding(source="review-comment", author=_login(c), text=body,
                               url=str(c.get("html_url") or ""),
                               path=str(c.get("path") or "")))
    return out


def _login(item: dict) -> str:
    user = item.get("user")
    return str(user.get("login") or "") if isinstance(user, dict) else ""


# ----------------------------------------------------------------------------
# (b) classify — keyword heuristics, rubric-tunable, severity-first.
# ----------------------------------------------------------------------------
_CLASS_LINE_RE = re.compile(r"^\s*[-*]\s*([A-Za-z][\w -]*?)\s*:\s*(.+?)\s*$")


def class_keywords(rubric_text: str = "") -> dict[str, tuple[str, ...]]:
    """The per-class keyword table: built-ins, overridden per class from the instance
    rubric's class list where one is configured (#314). A rubric list line of the
    shape ``- BUG: crash, race, corrupt`` (class name, then comma-separated keywords)
    replaces THAT class's built-ins; classes the rubric doesn't name keep theirs, and
    unknown class names are ignored — the four-class routing is the engine contract.
    Instances without a rubric get exactly :data:`DEFAULT_KEYWORDS`."""
    table = dict(DEFAULT_KEYWORDS)
    for line in (rubric_text or "").splitlines():
        m = _CLASS_LINE_RE.match(line)
        if not m:
            continue
        cls = re.sub(r"[\s_]+", "-", m.group(1).strip().upper())
        if cls not in CLASSES:
            continue
        kws = tuple(k.strip().lower() for k in m.group(2).split(",") if k.strip())
        if kws:
            table[cls] = kws
    return table


def classify(f: Finding, table: dict[str, tuple[str, ...]]) -> None:
    """Assign ``f.cls``/``f.keyword`` — the first class (severity-first CLASSES
    order) with a whole-word keyword match, decided by its first matching keyword in
    table order. Deterministic on purpose: the winning keyword names the signal, and
    recurrence matching needs the same miss to map to the same signal every run."""
    text = f.text.lower()
    for cls in CLASSES:
        for kw in table.get(cls, ()):
            if re.search(r"(?<!\w)" + re.escape(kw) + r"(?!\w)", text):
                f.cls, f.keyword = cls, kw
                return


def _rubric_text(cfg: Config, repo_spec: str) -> str:
    """The configured rubric's text, read from the triaged PR's mapped checkout —
    fail-OPEN like :func:`rubric.load`: a rubric problem must never block triage
    (built-in keywords still classify). Triage is out-of-band (no bundle, no
    worktree), so it reads the checkout ``publish._checkout_path`` maps for the PR's
    own repository, through the same path-escape guard the rubric module uses."""
    rel = str(getattr(cfg, "rubric_file", "") or "").strip()
    if not rel:
        return ""
    try:
        target = publish._checkout_path(cfg, repo_spec)
        path = rubric._resolve(target, rel)
        if path is None or not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    section = str(getattr(cfg, "rubric_section", "") or "").strip()
    return rubric._section(text, section) if section else text


def _model_pass(cfg: Config, findings: list[Finding]) -> None:
    """The config-gated single MODEL pass over the unclassified remainder (#316
    scope: off by default; keyword-only must be complete without it).
    ``[triage].model_cmd`` receives the unclassified findings as a JSON list on
    stdin and must print a JSON list of class names in the same order; anything
    unparsable, missing, or outside the four classes is ignored — fail-open, the
    finding simply stays ``codex-pr:unclassified`` for the human."""
    cmd = str(getattr(cfg, "triage_model_cmd", "") or "").strip()
    rest = [f for f in findings if not f.cls]
    if not cmd or not rest:
        return
    payload = json.dumps([{"text": f.text, "classes": list(CLASSES)} for f in rest])
    try:
        proc = subprocess.run(cmd, shell=True, input=payload, capture_output=True,
                              text=True, timeout=600)
        verdicts = json.loads(proc.stdout or "")
    except (OSError, subprocess.SubprocessError, ValueError):
        return
    if not isinstance(verdicts, list):
        return
    for f, v in zip(rest, verdicts):
        cls = re.sub(r"[\s_]+", "-", str(v).strip().upper())
        if cls in CLASSES:
            f.cls = cls  # class-level signal; no deciding keyword


# ----------------------------------------------------------------------------
# The per-PR triage record — the signal HISTORY recurrence detection reads, and the
# dedupe that makes re-runs safe (no double-filed tracker issues, no duplicate
# registrations). One JSON file per PR under process/triage/.
# ----------------------------------------------------------------------------
def _records_dir(cfg: Config) -> Path:
    return cfg.process_dir / "triage"


def _record_path(cfg: Config, repo_spec: str, number: str) -> Path:
    return _records_dir(cfg) / f"pr-{_slug(repo_spec)}-{number}.json"


def _load_record(path: Path) -> dict:
    """The stored record, or ``{}`` — tolerant like :func:`act.load_ledger`."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _finding_of(p: dict) -> Finding:
    """A :class:`Finding` rebuilt from its stored record dict — the recovery path:
    a finding an interrupted run recorded (``pending``) but never registered/logged
    re-enters the register+log batch from the record, not from a fresh pull."""
    fields = ("source", "author", "text", "url", "path", "cls", "keyword")
    return Finding(**{k: str(p.get(k) or "") for k in fields})


def _entries(cfg: Config) -> list[act.ActEntry]:
    """Every stored triage finding as synthetic :class:`act.ActEntry` rows — the
    entries :func:`act.register_signals` and :func:`act.recurrences` read, exactly
    the shape frozen-bundle extracts feed them (#149/#316).

    One entry per (PR, ingest date), NOT one per record: recurrence compares
    ``e.date > applied``, so folding an old finding into a re-run's entry would
    advance its date past a delta applied in between and fabricate a recurrence.
    The bundle name is ``pr_<n>`` so ``recurred_in`` names the PR."""
    out: list[act.ActEntry] = []
    d = _records_dir(cfg)
    for p in sorted(d.glob("pr-*.json")) if d.is_dir() else []:
        rec = _load_record(p)
        by_date: dict[str, list[str]] = {}
        for f in rec.get("findings", []):
            if isinstance(f, dict) and f.get("signal"):
                by_date.setdefault(str(f.get("date") or ""), []).append(str(f["signal"]))
        num = str(rec.get("number") or p.stem)
        for dt, sigs in sorted(by_date.items()):
            out.append(act.ActEntry(bundle=Path(f"pr_{num}"), date=dt,
                                    act_candidates=sigs))
    return out


# ----------------------------------------------------------------------------
# (c) route — BUG files a tracker issue (merged PRs only); everything else appends
# candidates to the act log, which is the ceiling.
# ----------------------------------------------------------------------------
def _issue_body(f: Finding, repo_spec: str, number: str, pr_url: str, date: str) -> str:
    """The filed BUG issue's body — finding + provenance + the carry-forward note.
    The note rides IN the issue body on purpose: the tracker thread is the one input
    the next cycle's Plan actually reads (the notes fetch), whereas the originating
    bundle is frozen — appending to a COMPLETE bundle's brief would mutate a record
    the Act frontier fingerprints (#299)."""
    quoted = "\n".join("> " + line for line in f.text.strip().splitlines())
    where = f" on `{f.path}`" if f.path else ""
    return (
        f"An external reviewer found a BUG-class issue on the merged PR {pr_url} "
        f"(ingested {date} by `pdca triage`).\n\n"
        f"{quoted}\n\n"
        f"— {f.author or 'unknown'}, via {f.source}{where}"
        + (f" ({f.url})" if f.url else "") + "\n\n"
        "## Carry-forward (for the next cycle's Plan)\n\n"
        "The PR merged with this finding unaddressed, so the defect is live on the "
        f"target's base. It is registered in the Act ledger as `{f.signal}`; the "
        "fixing cycle should cite the originating PR, and once a process delta "
        "lands, mark the signal applied (`act resolve`) so a recurrence is flagged.\n"
    )


def _excerpt(text: str, n: int = 90) -> str:
    one = re.sub(r"\s+", " ", text.strip())
    return one if len(one) <= n else one[: n - 1].rstrip() + "…"


def _entry_text(repo_spec: str, number: str, pr_url: str, merged: bool, date: str,
                batch: list[tuple[Finding, str]], recovered: int, bug_note: str,
                added: list[str], recs: list[dict]) -> str:
    """The act-log entry for one triage run: findings, per-class routed candidates
    (proposals only — the human applies them), registered signals, recurrences.
    ``batch`` pairs each finding with its filed issue number ("" = none); it holds
    the run's NEW findings plus the first ``recovered`` entries an interrupted
    earlier run recorded but never registered/logged (their issue numbers ride in
    from the record — credited here, never re-filed)."""
    head = (f"# PR-review triage — {date} — {repo_spec}#{number} "
            f"({'merged' if merged else 'open'}) — {len(batch)} finding(s)")
    if recovered:
        head += f" ({recovered} recovered from an interrupted run)"
    lines = [
        head,
        "",
        f"PR: {pr_url}",
        "",
        "## Findings (class-keyed signals)",
    ]
    for f, _ in batch:
        who = f.author or "unknown"
        lines.append(f"- [{f.cls or 'UNCLASSIFIED'}] {f.signal} — "
                     f"{_excerpt(f.text)} ({who}, {f.source})")
    lines += ["", "## Routed  (candidates only — the human applies each; this "
                  "command never edits pdca.toml or the rubric)"]
    for f, issue in batch:
        if f.cls == "BUG":
            if issue:
                lines.append(f"- BUG → filed tracker issue #{issue} "
                             f"(carry-forward note in its body): {_excerpt(f.text)}")
            else:
                lines.append(f"- BUG → candidate tracker issue (NOT filed: "
                             f"{bug_note or 'unknown'}): {_excerpt(f.text)}")
        elif f.cls == "CONVENTION":
            lines.append(f"- CONVENTION → candidate gate row / rubric line: enforce "
                         f"\"{f.keyword or f.signal}\" ([[gates.checks]] row or a "
                         f"rubric convention line) — {_excerpt(f.text)}")
        elif f.cls == "NOISE":
            lines.append(f"- NOISE → candidate rubric-exclusion entry: declare "
                         f"\"{f.keyword or f.signal}\" a rejected finding class in "
                         f"the rubric — {_excerpt(f.text)}")
        elif f.cls == "TEST-GAP":
            lines.append(f"- TEST-GAP → candidate test: cover "
                         f"\"{f.keyword or f.signal}\" — {_excerpt(f.text)}")
        else:
            lines.append(f"- UNCLASSIFIED → needs a human (or set "
                         f"[triage].model_cmd): {_excerpt(f.text)}")
    lines += ["", "## Registered signals (act-ledger.json)"]
    lines += [f"- {raw}" for raw in added] or ["- (all already tracked)"]
    if recs:
        lines += ["", "## ⚠ Recurrences (class reappeared after its delta was applied)"]
        lines += [f"- {r['signal']} — applied {r['applied']}, recurred in "
                  + ", ".join(r["recurred_in"]) for r in recs]
    return "\n".join(lines)


# ----------------------------------------------------------------------------
def run(cfg: Config, pr: str, *, repo: str = "", date: str = "") -> int:
    """``pdca triage <pr>`` — pull, classify, route, register. Returns a process
    code: 0 done (including "nothing new"), 1 failed (unreachable PR, a filing
    error, a held Act session), 2 usage. A held-session 1 is recoverable by design:
    the findings are already recorded ``pending``, and the prescribed re-run
    registers and logs them even though the pull finds nothing new."""
    date = date or datetime.date.today().isoformat()
    repo_spec, number = parse_pr(pr, repo)
    if not number:
        print("triage: cannot resolve the PR — pass a URL "
              "(https://github.com/OWNER/REPO/pull/N), OWNER/REPO#N, or a bare "
              "number WITH --repo OWNER/REPO (guessing gh's default repository "
              "could triage an unrelated same-numbered PR)", file=sys.stderr)
        return 2
    meta = _api(f"repos/{repo_spec}/pulls/{number}")
    if not isinstance(meta, dict):
        print(f"triage: could not read PR {repo_spec}#{number} via `gh api` — "
              "nothing ingested; fix gh/auth and re-run", file=sys.stderr)
        return 1
    reviews = _api_list(f"repos/{repo_spec}/pulls/{number}/reviews")
    comments = _api_list(f"repos/{repo_spec}/pulls/{number}/comments")
    if reviews is None or comments is None:
        # Fail CLOSED: half an ingest silently under-registers signals.
        print(f"triage: could not pull the reviews/comments of {repo_spec}#{number} "
              "— nothing ingested; fix gh/auth and re-run", file=sys.stderr)
        return 1

    findings = _findings(reviews, comments)
    table = class_keywords(_rubric_text(cfg, repo_spec))
    for f in findings:
        classify(f, table)
    _model_pass(cfg, findings)  # config-gated; off by default

    # Re-runs ingest only what is NEW since the stored record — findings arrive over
    # days, and an already-filed BUG issue must never be filed twice (irreversible,
    # the split rule). Recorded findings still marked ``pending`` were filed and
    # written by a run that never finished registering (a held Act lock, a crash
    # between the record write and the ledger write): they re-enter the register+log
    # batch below, so the "no new findings" fast path can NEVER strand them — a
    # re-run after an interrupted run finishes its job instead of exiting 0 with an
    # empty ledger.
    rec_path = _record_path(cfg, repo_spec, number)
    rec = _load_record(rec_path)
    prior = rec.get("findings", []) if isinstance(rec.get("findings"), list) else []
    seen = {(str(p.get("source") or ""), str(p.get("url") or ""), str(p.get("text") or ""))
            for p in prior if isinstance(p, dict)}
    new = [f for f in findings if (f.source, f.url, f.text) not in seen]
    pending = [p for p in prior if isinstance(p, dict) and p.get("pending")]
    if not new and not pending:
        if prior:
            print(f"triage: {repo_spec}#{number} — no new findings "
                  f"({len(prior)} already ingested and registered)")
        else:
            print(f"triage: no review findings on {repo_spec}#{number} "
                  "— nothing to ingest")
        return 0

    merged = bool(meta.get("merged") or meta.get("merged_at"))
    pr_url = str(meta.get("html_url") or f"https://github.com/{repo_spec}/pull/{number}")

    # Route BUG first — filing is the irreversible step, so it happens before the
    # record write that makes it non-repeatable (mirrors split's file-then-record
    # order: the record is what guarantees a re-run cannot duplicate the issue).
    # NEW findings only: a pending finding's filing decision was already made at its
    # own ingest (issue number in the record, or deliberately not filed) — recovery
    # finishes registration and logging, it never re-decides filing (the same
    # never-retry-blind rule as the SplitError branch below).
    filed: dict[int, str] = {}
    bug_note, failures = "", 0
    bugs = [i for i, f in enumerate(new) if f.cls == "BUG"]
    if bugs and not merged:
        bug_note = ("PR not merged — the fix can still land on the open PR; "
                    "candidate kept in the act log")
    elif bugs:
        can, repo_or_why = split.can_file(cfg)
        if not can:
            bug_note = repo_or_why  # degrade loudly to a logged candidate, exit 0
        else:
            for i in bugs:
                try:
                    filed[i] = split._create_issue(
                        repo_or_why,
                        f"[pr-triage] BUG finding on {repo_spec}#{number}: "
                        + _excerpt(new[i].text, 60),
                        _issue_body(new[i], repo_spec, number, pr_url, date),
                        "", cfg.root)
                except split.SplitError as exc:
                    bug_note = str(exc)  # incl. UncertainFiling — never retry blind
                    failures += 1

    # Durably record what was ingested/filed BEFORE registering, marked ``pending``:
    # a crash (or a held Act lock) after this point cannot re-file an issue, and the
    # flag is what makes the self-heal REAL — a finding stays pending until the
    # ledger write and act-log append complete, so the next run's batch picks it up
    # instead of the "no new findings" fast path skipping registration forever.
    payload = [{**asdict(f), "signal": f.signal, "date": date,
                "issue": filed.get(i, ""), "pending": True}
               for i, f in enumerate(new)]
    if new:
        _records_dir(cfg).mkdir(parents=True, exist_ok=True)
        rec_path.write_text(json.dumps(
            {"repo": repo_spec, "number": number, "url": pr_url, "merged": merged,
             "date": date, "bug_note": bug_note, "findings": prior + payload},
            indent=2) + "\n", encoding="utf-8")
    elif not bug_note:
        bug_note = str(rec.get("bug_note") or "")  # why a pending BUG wasn't filed

    # The register/log batch: recorded-but-unregistered findings from an interrupted
    # run first (their filed issue numbers ride in from the record — credited below,
    # never re-filed), then this run's new findings.
    batch = [(_finding_of(p), str(p.get("issue") or "")) for p in pending]
    batch += [(f, filed.get(i, "")) for i, f in enumerate(new)]

    # (d) register + log, under the shared Act session lock like `act log --append`
    # (#299): a triage overlapping a flow's auto-Act must not interleave ledger and
    # act-log writes with the review in progress.
    with act.act_session(cfg) as held:
        if not held:
            print("triage: another Act session is running (a flow's auto-Act or a "
                  "concurrent append) — the findings are recorded in "
                  f"{rec_path}; re-run to register them when it finishes",
                  file=sys.stderr)
            return 1
        entries = _entries(cfg)  # full triage history, incl. the record just written
        # Every finding registers on FIRST sight (min_count=1): an external finding
        # already cost a shipped defect plus a review round — the recurring-only
        # threshold exists for SUMMARY chatter, not for external findings.
        added = act.register_signals(cfg, entries, date, min_count=1)
        recs = [r for r in act.recurrences(cfg, entries)
                if str(r.get("signal", "")).startswith(SIGNAL_PREFIX)]
        log = act.append_entry(cfg, _entry_text(
            repo_spec, number, pr_url, merged, date, batch, len(pending), bug_note,
            added, recs))
        # Ledger + log are durable — NOW clear the pending flags (last on purpose:
        # a crash anywhere above leaves them set and the next run finishes the job;
        # the worst residue of a crash between append and here is one duplicate,
        # visible log entry — never a silent loss).
        done = _load_record(rec_path)
        for p in done.get("findings", []) if isinstance(done.get("findings"), list) else []:
            if isinstance(p, dict):
                p.pop("pending", None)
        rec_path.write_text(json.dumps(done, indent=2) + "\n", encoding="utf-8")

    counts = {c: sum(1 for f, _ in batch if f.cls == c) for c in CLASSES}
    unclassified = sum(1 for f, _ in batch if not f.cls)
    summary = ", ".join(f"{n} {c}" for c, n in counts.items() if n)
    if unclassified:
        summary = ", ".join(s for s in (summary, f"{unclassified} unclassified") if s)
    print(f"triage: {repo_spec}#{number} ({'merged' if merged else 'open'}) — "
          f"{len(new)} new finding(s)"
          + (f" + {len(pending)} recovered" if pending else "")
          + f": {summary or 'none'}")
    for f, issue in batch:
        route = (f"filed tracker issue #{issue}" if issue else "act-log candidate")
        print(f"  [{f.cls or 'UNCLASSIFIED'}] {f.signal} → {route}")
    if bug_note:
        print(f"  note: {bug_note}", file=sys.stderr)
    if added:
        print(f"  registered {len(added)} new signal(s) in the Act ledger")
    for r in recs:
        print(f"  ⚠ {r['signal']} recurred after its delta was applied "
              f"({r['applied']}) — in " + ", ".join(r["recurred_in"]))
    print(f"appended entry to {log}")
    return 1 if failures else 0
