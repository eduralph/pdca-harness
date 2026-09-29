"""No dead attempt's artifact is harvested as a live attempt's work (issue #541).

A retried reviewer / advisory leaf runs every attempt in the SAME sandbox, and all three
harvest sites used to end with the same hand-copied `if produced.exists(): copy2(...)`.
That test knows nothing about WHICH attempt wrote the file — so a truncated verdict an
attempt left behind before it died transiently was copied out as the output of the attempt
that exited 0 afterwards, and the bundle reached sign-off carrying a dead attempt's text as
its review. "No evidence" must never be filed as a verdict.

The three sites now share ONE owner (`leaves._LeafHarvest`), which is told of each death as
it happens: the dying attempt's file comes OFF the path and into the bundle's `*.error.log`
(preserved, not deleted — and bounded, since that log is tracked), so what is harvested
afterwards is the live attempt's own work or nothing at all. When the file cannot be
withdrawn, the path is un-owned: the leaf still gets every attempt the shipped stop rule
gives it, and the harvest refuses rather than filing a file it cannot attribute.

The refusal is settled by the residue's filesystem IDENTITY, not by its bytes, which is what
keeps it from destroying a LIVE verdict: an attempt that writes over a residue this harness
could not unlink — in place, byte-identically, or atomically over one it could not even read
— has produced its own verdict, and that file is filed. Only "the same file, unchanged" is
refused.

Same root cause, second face: an artifact was CLASSIFIED — placeholder or real verdict? —
with no notion of who produced it either, by matching a `pdca:leaf-status` marker anywhere in
its text. So a real report that merely QUOTES a marker (every advisory review of this harness
does) was read as a placeholder, and all three readers of that answer acted on it: §6
relabelled its findings and forced them HUMAN, stripping the `[impl]` routing (#264); the
size signal counted the round ambiguous; the plan-advisory findings were discarded. Evidence
that existed was turned back into "no evidence" — the same invariant, aimed the other way.

What settles it now is a positive completion trailer the LEAF writes as the artifact's last
non-blank line: an artifact its author closed is real whatever it quotes, and a dying
attempt's half-written report is very unlikely to have closed itself — it would need to be cut
off exactly on a line that quotes the trailer. Absence classifies nothing — a leaf that never
emits it, and every artifact in the back catalogue, behaves exactly as before.

The harvest legs drive the REAL site entry points with a stub "leaf" that is a Python
interpreter (the `template/tests/test_leaf_resilience.py` harness), so the production spawn,
the retry loop, the sandbox and the harvest all run; the classification legs drive the three
production readers directly. Offline, no model, no network. Run from `template/`:
    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from pdca_harness import assemble, autoiterate, leaves, size_signal, state
from pdca_harness.config import Config, LeafConfig

# A claude-family leaf so the stream path engages (the only path that yields the "did a
# session start" signal, i.e. transient-vs-substantive). argv is a python interpreter
# running this inline script; `_invoke` appends --output-format/--verbose (ignored) and
# feeds the prompt on stdin. The script counts its own invocations into $PDCA_TEST_CNT so a
# test can assert the retry count, and behaves per attempt:
#   * the first attempt writes $PDCA_TEST_DEAD to the artifact path — a truncated verdict —
#     optionally padded with $PDCA_TEST_BULK × $PDCA_TEST_REPEAT and closed by
#     $PDCA_TEST_END, so a leg can build a residue far too large to quote whole; then,
#     with $PDCA_TEST_UNREADABLE, makes that file unreadable, and/or with $PDCA_TEST_LOCK
#     makes its own cwd read-only so the harness cannot UNLINK what it left (a residue that
#     survives its author — note the file itself stays writable, which is the whole point of
#     the overwrite legs below);
#   * the first $PDCA_TEST_DEATHS attempts then die at invocation (stderr only, no stdout →
#     no session started → the transient-infra signal);
#   * any later attempt exits 0, writing $PDCA_TEST_LIVE (empty ⇒ it writes nothing at all)
#     — in place, or via a temp file + os.replace under $PDCA_TEST_ATOMIC, which is how a
#     careful leaf writes over a file it cannot open for reading — and then prints its
#     stream: one bare event, or the file $PDCA_TEST_STREAM names (pinned vendor bytes).
# Every attempt also copies the prompt it was fed on stdin to $PDCA_TEST_PROMPT when set, so
# a leg can assert on the prompt the site ACTUALLY composed, not on a constant beside it.
_SCRIPT = """
import os, sys
art = os.environ["PDCA_TEST_ART"]
cnt = os.environ["PDCA_TEST_CNT"]
n = len(open(cnt).read()) if os.path.exists(cnt) else 0
open(cnt, "a").write("x")
if os.environ.get("PDCA_TEST_PROMPT"):
    open(os.environ["PDCA_TEST_PROMPT"], "w").write(sys.stdin.read())
if n == 0 and os.environ.get("PDCA_TEST_DEAD"):
    with open(art, "w") as fh:
        fh.write(os.environ["PDCA_TEST_DEAD"])
        fh.write(os.environ.get("PDCA_TEST_BULK", "")
                 * int(os.environ.get("PDCA_TEST_REPEAT") or 0))
        fh.write(os.environ.get("PDCA_TEST_END", ""))
    if os.environ.get("PDCA_TEST_UNREADABLE"):
        os.chmod(art, 0o000)
    if os.environ.get("PDCA_TEST_LOCK"):
        os.chmod(".", 0o500)
if n < int(os.environ["PDCA_TEST_DEATHS"]):
    sys.stderr.write("overloaded_error 529\\n")
    sys.exit(1)
live = os.environ.get("PDCA_TEST_LIVE")
if live:
    if os.environ.get("PDCA_TEST_ATOMIC"):
        with open(art + ".tmp", "w") as fh:
            fh.write(live)
        os.replace(art + ".tmp", art)
    else:
        open(art, "w").write(live)
stream = os.environ.get("PDCA_TEST_STREAM")
sys.stdout.write(open(stream).read() if stream else '{"type": "assistant"}\\n')
"""

# What the dying attempt leaves at the artifact path, and what a live one writes.
_DEAD_MARK = "DEAD-ATTEMPT-VERDICT-b3f1"
_DEAD_TEXT = f"| Correctness | PASS | {_DEAD_MARK} — cut off mid-table\n"
_LIVE_TEXT = "# Review\n\n- NEEDS-HUMAN — the LIVE attempt's own verdict\n"
# …and the ends of a residue too big to quote whole (the bounded-quote leg).
_TAIL_MARK = "LAST-LINE-OF-THE-RESIDUE-9c2"
_BULK_LINE = "| T3 | PASS | one more row of a very long verdict table |\n"
_BULK_TIMES = 20_000

_ADVERSARY = "adversary"
_ANTAGONIST = "antagonist"
_SPEC = {"role": "refute it"}
# A project rubric rule, so a leg can see where the rubric landed in a composed prompt.
_RUBRIC_MARK = "RUBRIC-RULE-5e1"
# Pinned vendor bytes #526 ships (tests/fixtures/README.md) — read, never added to.
_FIXTURES = Path(__file__).resolve().parent / "fixtures"

# The three harvest sites, each named by the artifact it files and the error log that must
# hold what a dead attempt left. Asserted as one table on purpose: a fix or a test that
# lands at two of the three leaves the third wrong, which is the whole shape of this issue.
_SITES = (
    ("review", "check-review.md", state.REVIEW_ERROR_LOG),
    ("advisory", f"check-advisory-{_ADVERSARY}.md", f"check-advisory-{_ADVERSARY}.error.log"),
    ("plan-advisory", f"plan-advisory-{_ANTAGONIST}.md",
     f"plan-advisory-{_ANTAGONIST}.error.log"),
)

_NEEDS_ROOTLESS = "root ignores the read-only directory / unreadable file this leg needs"

# The completion trailer and a quoted status marker, spelled out HERE rather than read off
# `assemble`. Two reasons, both load-bearing:
#   * this module is imported by the C4 gate with every production hunk REVERTED, and a
#     module-level reference to a symbol the patch adds makes that leg a load failure
#     (UNVERIFIABLE) instead of a red — so the new names appear only inside test bodies;
#   * the exact bytes are the contract between the harness's prompts and its reader, so a
#     test that re-derived them from production could not catch either side drifting. One leg
#     below asserts production's own constant is spelled exactly this way.
# The quoted token MUST be one the reader already recognises: quote an unknown one and every
# leg here passes pre-fix (an unknown token is not labelled either way), which is precisely
# how an earlier round shipped a green test that proved nothing.
_TRAILER = "<!-- pdca:leaf-complete -->"
_QUOTED = "<!-- pdca:leaf-status infra-empty -->"
# A clean review body: every verdict PASS, no NEEDS-HUMAN row, no FAIL cell — so the size
# signal's answer turns on the classification alone.
_CLEAN_TABLE = ("| Item | Verdict | Basis |\n"
                "|------|---------|-------|\n"
                "| C4 — fix verified | PASS | re-ran the bundle test red→green |\n")
# …and a genuine harness placeholder: the marker, and NO trailer, because the leaf never
# closed one. The shape criterion 4b protects — it must keep classifying as it always has.
_PLACEHOLDER = (f"# Advisory review — NOT COMPLETED\n\n{_QUOTED}\n\n"
                "- NEEDS-HUMAN — re-run the Check reviewer; this bundle has no review.\n")


def _probe(body: str, *, closed: bool) -> str:
    """A REAL leaf report that quotes a recognised status marker inside a fenced block.

    The SAME text in both columns of the red→green: ``closed`` only appends the trailer. The
    artifact is constant, so what flips its classification is the trailer alone — the base
    ignores it, the fix honours it.
    """
    text = ("# Advisory review — the leaf-status machinery\n"
            "\n"
            "The placeholder this harness writes opens with:\n"
            "\n"
            "```\n"
            f"{_QUOTED}\n"
            "```\n"
            "\n"
            f"{body}")
    return f"{text}\n{_TRAILER}\n" if closed else text


def _rootless() -> bool:
    return not (hasattr(os, "geteuid") and os.geteuid() == 0)


def _leaf() -> LeafConfig:
    return LeafConfig(mode="command", family="claude",
                      argv=[sys.executable, "-c", _SCRIPT], interactive=False)


def _stub_config(root: Path) -> Config:
    return Config(
        root=root,
        bundle_root=root / "results",
        process_dir=root / "process",
        templates_dir=root / "templates",
        default_branch="main",
        tracker_system="github",
        tracker_url="",
        issue_id_example="#1",
        builder=LeafConfig(mode="stub", family="claude"),
        reviewer=_leaf(),
    )


class _NoBackoff:
    """`leaves.time` with the retry backoff's wall clock removed — and nothing else.

    The three sites take the shipped attempt budget and backoff schedule; only the sleeping
    is skipped, so every leg exercises the real stop rule at test speed. Everything else
    (`monotonic`, `strftime`, the memory telemetry's clock) is the real `time`.
    """

    def sleep(self, _seconds: float) -> None:
        return None

    def __getattr__(self, name: str):
        return getattr(time, name)


class AttemptHarvest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _stub_config(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- driving the real sites ---------------------------------------------------------

    def _bundle(self, name: str) -> Path:
        d = self.cfg.bundle(name)
        d.mkdir(parents=True, exist_ok=True)
        (d / "brief.md").write_text("- **Slug:** harvest\n", encoding="utf-8")
        (d / "patch.diff").write_text("--- a\n+++ b\n", encoding="utf-8")
        (d / "check-gates.json").write_text('{"overall": "pass", "rows": []}\n',
                                            encoding="utf-8")
        return d

    def _counter(self, site: str) -> Path:
        return self.tmp / f"{site}.attempts"

    def _runs(self, site: str) -> int:
        p = self._counter(site)
        return len(p.read_text(encoding="utf-8")) if p.exists() else 0

    def _drive(self, site: str, d: Path, artifact: str, *, deaths: int = 1,
               dead: str = _DEAD_TEXT, live: str = "", lock: bool = False,
               unreadable: bool = False, atomic: bool = False,
               bulk: str = "", repeat: int = 0, end: str = "",
               prompt: Path | None = None, stream: Path | None = None) -> None:
        """Run ONE harvest site end to end against the stub leaf."""
        leaf = _leaf()
        self.cfg.reviewer = leaf
        runners = {
            "review": lambda: leaves._run_review_sandboxed(d, self.cfg),
            "advisory": lambda: leaves._run_advisory_sandboxed(
                d, self.cfg, leaf, _SPEC, _ADVERSARY),
            "plan-advisory": lambda: leaves._run_plan_advisory_sandboxed(
                d, self.cfg, leaf, _SPEC, _ANTAGONIST),
        }
        env = {"PDCA_TEST_CNT": str(self._counter(site)), "PDCA_TEST_ART": artifact,
               "PDCA_TEST_DEAD": dead, "PDCA_TEST_LIVE": live,
               "PDCA_TEST_DEATHS": str(deaths), "PDCA_TEST_LOCK": "1" if lock else "",
               "PDCA_TEST_UNREADABLE": "1" if unreadable else "",
               "PDCA_TEST_ATOMIC": "1" if atomic else "",
               "PDCA_TEST_BULK": bulk, "PDCA_TEST_REPEAT": str(repeat),
               "PDCA_TEST_END": end, "PDCA_TEST_PROMPT": str(prompt or ""),
               "PDCA_TEST_STREAM": str(stream or "")}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(leaves, "time", _NoBackoff()), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            runners[site]()

    # -- criterion 1 + 2: the dead attempt's file is refused, and preserved --------------

    def test_no_site_files_a_dead_attempts_artifact_as_a_live_ones(self) -> None:
        # The issue itself, at ALL THREE sites: attempt 1 leaves a truncated verdict and
        # dies transiently; attempt 2 exits 0 having written nothing. What the bundle must
        # NOT end up with is attempt 1's text filed as attempt 2's output.
        for site, artifact, error_log in _SITES:
            with self.subTest(site=site):
                d = self._bundle(site)
                self._drive(site, d, artifact)
                filed = (d / artifact).read_text(encoding="utf-8")
                self.assertNotIn(_DEAD_MARK, filed,
                                 f"{site}: a dead attempt's artifact was filed as the live "
                                 "attempt's output")
                self.assertIn("NOT COMPLETED", filed)   # degraded, as if nothing was written
                self.assertEqual(self._runs(site), 2)   # …and the live attempt really ran
                # Criterion 2: preserved, not merely deleted — a real (if truncated) verdict
                # must survive in the bundle while the operator is told none was produced.
                self.assertIn(_DEAD_MARK, (d / error_log).read_text(encoding="utf-8"),
                              f"{site}: the dead attempt's text was destroyed")

    def test_the_preserved_account_never_reads_as_a_leaf_that_spent_its_attempts(self) -> None:
        # The preserved record carries NO settlement marker (#540): the leaf did not spend
        # its attempts, so every reader must treat the log exactly as it treats an absent
        # one — the remedy here is re-running the leaf, not retiring it.
        d = self._bundle("review")
        self._drive("review", d, "check-review.md")
        log = d / state.REVIEW_ERROR_LOG
        self.assertFalse(state.leaf_ran_and_failed(log))
        self.assertIn("exited 0", log.read_text(encoding="utf-8"))

    def test_a_preserved_residue_is_bounded_where_it_lands_in_the_bundle(self) -> None:
        # The bundle's `*.error.log` is a TRACKED file and a residue is a whole review
        # artifact, so the quote is capped head/tail with an elision line — the same bound
        # the stderr tail beside it has always had (progress.py's deque(maxlen=200)).
        # Unbounded, a multi-MB artifact that dies transiently commits multi-MB logs.
        d = self._bundle("review")
        self._drive("review", d, "check-review.md",
                    bulk=_BULK_LINE, repeat=_BULK_TIMES, end=f"| END | {_TAIL_MARK} |\n")
        residue = len(_DEAD_TEXT) + len(_BULK_LINE) * _BULK_TIMES   # ~940 KB
        log = d / state.REVIEW_ERROR_LOG
        size = log.stat().st_size
        self.assertLess(size, residue // 10,
                        f"the residue ({residue} bytes) was quoted essentially whole into a "
                        f"tracked bundle file ({size} bytes)")
        text = log.read_text(encoding="utf-8")
        self.assertIn(_DEAD_MARK, text)      # the head of the residue survived…
        self.assertIn(_TAIL_MARK, text)      # …and so did its tail, where it was cut off
        self.assertIn("elided", text)        # …with the drop declared, not silent
        # …and no half-written sibling left beside it (`_replace_record`'s temp).
        self.assertFalse((d / f".{state.REVIEW_ERROR_LOG}.partial").exists())

    @unittest.skipUnless(_rootless(), _NEEDS_ROOTLESS)
    def test_an_unwithdrawable_residue_is_accounted_for_once_not_once_per_attempt(self) -> None:
        # A residue the harness could not unlink is STILL at the artifact path when the next
        # attempt dies — and that attempt never wrote it. Quoting it again under attempt 2's
        # name, and again under attempt 3's, is "no notion of which attempt wrote it" one
        # level down, inside the fix for it (and re-reads + re-digests a whole artifact per
        # attempt). It is accounted for ONCE, by the attempt that actually left it.
        d = self._bundle("review")
        self._drive("review", d, "check-review.md", deaths=3, lock=True)
        log = (d / state.REVIEW_ERROR_LOG).read_text(encoding="utf-8")
        self.assertEqual(self._runs("review"), 3)
        self.assertEqual(log.count(_DEAD_MARK), 1,
                         "the same residue was quoted once per attempt, each time as THAT "
                         f"attempt's work:\n{log}")
        # …and the attempts that left nothing say so, rather than going silent about a file
        # that is still sitting on the path.
        self.assertIn("nothing new", log)

    # -- criterion 3: the live attempt's own work is harvested exactly as today ----------

    def test_the_live_attempts_own_artifact_is_harvested_at_every_site(self) -> None:
        for site, artifact, error_log in _SITES:
            with self.subTest(site=site):
                d = self._bundle(site)
                self._drive(site, d, artifact, live=_LIVE_TEXT)
                self.assertEqual((d / artifact).read_text(encoding="utf-8"), _LIVE_TEXT)
                # A leaf that SUCCEEDED still leaves no error log behind (#540): there is a
                # verdict in the bundle, so there is nothing to explain.
                self.assertFalse((d / error_log).exists())

    def test_a_leaf_that_exits_0_writing_nothing_still_degrades_as_today(self) -> None:
        for site, artifact, error_log in _SITES:
            with self.subTest(site=site):
                d = self._bundle(site)
                self._drive(site, d, artifact, deaths=0, dead="")
                filed = (d / artifact).read_text(encoding="utf-8")
                self.assertIn("NOT COMPLETED", filed)
                self.assertEqual(assemble.leaf_status(filed), assemble.LEAF_STATUS_HUMAN)
                self.assertEqual(self._runs(site), 1)
                self.assertFalse((d / error_log).exists())

    @unittest.skipUnless(_rootless(), _NEEDS_ROOTLESS)
    def test_a_live_verdict_written_over_an_unwithdrawable_residue_is_still_filed(self) -> None:
        # The refusal must not destroy a LIVE verdict. `unlink()` needs write permission on
        # the DIRECTORY; `open(path, "w")` needs it only on the FILE — so in exactly the
        # scenario the refusal leg below builds (the dying attempt chmods its own sandbox
        # read-only), the next attempt can still overwrite the residue with its complete
        # review. What is at the path is then no longer the file the harness failed to
        # remove, so it is the live attempt's work and is filed.
        for site, artifact, error_log in _SITES:
            with self.subTest(site=site):
                d = self._bundle(site)
                self._drive(site, d, artifact, lock=True, live=_LIVE_TEXT)
                filed = (d / artifact).read_text(encoding="utf-8")
                self.assertEqual(filed, _LIVE_TEXT,
                                 f"{site}: the LIVE attempt's own verdict was refused and "
                                 "died with the sandbox")
                self.assertNotIn(_DEAD_MARK, filed)
                self.assertEqual(self._runs(site), 2)
                self.assertFalse((d / error_log).exists())   # a leaf that succeeded (#540)

    @unittest.skipUnless(_rootless(), _NEEDS_ROOTLESS)
    def test_a_live_verdict_identical_to_the_residue_is_still_filed(self) -> None:
        # …and the case CONTENT cannot settle: a deterministic leaf (a command-mode leaf
        # re-running the same analysis) writes byte-for-byte what its dead attempt wrote.
        # Comparing bytes reads that as "nobody rewrote it" and discards a verdict the live
        # attempt did produce; the file's identity moved, so the harvest sees the rewrite.
        d = self._bundle("review")
        self._drive("review", d, "check-review.md", lock=True, live=_DEAD_TEXT)
        filed = (d / "check-review.md")
        self.assertTrue(filed.exists(), "the live attempt's verdict was never filed")
        self.assertEqual(filed.read_text(encoding="utf-8"), _DEAD_TEXT,
                         "a live verdict was discarded for being identical to the residue "
                         "the previous attempt left")
        self.assertNotIn("NOT COMPLETED", filed.read_text(encoding="utf-8"))
        self.assertEqual(self._runs("review"), 2)

    @unittest.skipUnless(_rootless(), _NEEDS_ROOTLESS)
    def test_a_live_verdict_replacing_an_unreadable_residue_is_still_filed(self) -> None:
        # …and the case content cannot settle from the other side: the residue could not be
        # READ at all, so there are no bytes to compare against. Its IDENTITY is still
        # knowable, so an attempt that replaces it atomically (temp file + os.replace — how
        # a careful leaf writes over a file it cannot open) is recognised as having written
        # there, and its complete review is filed rather than dying with the sandbox.
        for site, artifact, error_log in _SITES:
            with self.subTest(site=site):
                d = self._bundle(site)
                self._drive(site, d, artifact, unreadable=True, atomic=True,
                            live=_LIVE_TEXT)
                self.assertEqual((d / artifact).read_text(encoding="utf-8"), _LIVE_TEXT,
                                 f"{site}: a live verdict was refused because the residue "
                                 "it replaced could not be read")
                self.assertEqual(self._runs(site), 2)
                self.assertFalse((d / error_log).exists())   # a leaf that succeeded (#540)

    # -- criterion 5: a residue that cannot be withdrawn is refused, never fatal ---------

    @unittest.skipUnless(_rootless(), _NEEDS_ROOTLESS)
    def test_an_unreadable_residue_is_refused_when_nothing_replaced_it(self) -> None:
        # The other side of the leg above, and the proof that its fixture really does
        # injure the read: the same unreadable residue, with the live attempt writing
        # NOTHING over it, must be refused — a file whose bytes this harness could never
        # see is the one thing it certainly cannot attribute to the attempt that exited 0.
        # (It is also not deleted: what could not be quoted must not be destroyed either.)
        d = self._bundle("review")
        self._drive("review", d, "check-review.md", unreadable=True)
        filed = (d / "check-review.md").read_text(encoding="utf-8")
        self.assertIn("NOT COMPLETED", filed)
        self.assertNotIn(_DEAD_MARK, filed)
        self.assertEqual(self._runs("review"), 2)
        self.assertIn("could not be read",
                      (d / state.REVIEW_ERROR_LOG).read_text(encoding="utf-8"))

    @unittest.skipUnless(_rootless(), _NEEDS_ROOTLESS)
    def test_a_residue_that_cannot_be_withdrawn_is_refused_at_every_site(self) -> None:
        # The dying attempt makes its own sandbox read-only, so the harness cannot take its
        # file off the path, and the attempt that exits 0 writes nothing over it. The state
        # is carried FORWARD: the run is not ended, and the harvest refuses on the success
        # branch rather than filing a file it cannot attribute to the attempt that exited 0.
        for site, artifact, error_log in _SITES:
            with self.subTest(site=site):
                d = self._bundle(site)
                self._drive(site, d, artifact, lock=True)
                filed = (d / artifact).read_text(encoding="utf-8")
                self.assertNotIn(_DEAD_MARK, filed,
                                 f"{site}: an un-withdrawable residue was filed anyway")
                self.assertIn("NOT COMPLETED", filed)
                self.assertEqual(self._runs(site), 2)   # the live attempt still ran
                self.assertIn(_DEAD_MARK, (d / error_log).read_text(encoding="utf-8"))

    @unittest.skipUnless(_rootless(), _NEEDS_ROOTLESS)
    def test_an_unwithdrawable_residue_does_not_narrow_the_retry_contract(self) -> None:
        # The mechanical check the shipped contract is measured by
        # (test_leaf_resilience.py:62): the attempt budget is the stop rule's alone. A
        # residue the harness cannot withdraw must cost the leaf no attempt.
        d = self._bundle("review")
        self._drive("review", d, "check-review.md", deaths=3, lock=True)
        self.assertEqual(self._runs("review"), 3, "an un-withdrawable residue cost the "
                                                  "leaf attempts")
        log = (d / state.REVIEW_ERROR_LOG)
        self.assertTrue(state.leaf_ran_and_failed(log))   # spent its attempts → settled
        self.assertIn(_DEAD_MARK, log.read_text(encoding="utf-8"))

    # -- criterion 6: one implementation, three users ------------------------------------

    def test_all_three_sites_go_through_the_one_owner(self) -> None:
        # The point of the split: the harvest is not hand-copied per site any more. Driven,
        # not read off the source — each site runs with the owner swapped for a recording
        # subclass, so a site that grows its own copy again records nothing and fails here.
        seen: list[str] = []
        owner = leaves._LeafHarvest

        class _Recording(owner):
            def __init__(self, **kw) -> None:
                seen.append(kw["dest"].name)
                super().__init__(**kw)

        for site, artifact, _log in _SITES:
            with self.subTest(site=site), mock.patch.object(leaves, "_LeafHarvest", _Recording):
                self._drive(site, self._bundle(site), artifact, deaths=0, dead="",
                            live=_LIVE_TEXT)
        self.assertEqual(seen, [artifact for _s, artifact, _l in _SITES])

    # -- criterion 4: the leaf-status label tells the truth ------------------------------

    @unittest.skipUnless(_rootless(), _NEEDS_ROOTLESS)
    def test_the_label_never_says_a_run_that_exited_0_did_not_run(self) -> None:
        d = self._bundle("review")
        self._drive("review", d, "check-review.md", lock=True)
        items = assemble.collect_needs_human(d, self.cfg)
        rows = [i for i in items if "leaf" in i.text]
        self.assertTrue(rows, "the refused harvest reached §6 with no leaf-status row")
        for item in rows:
            self.assertFalse(item.text.startswith("leaf did not run"),
                             f"§6 says a run whose last attempt exited 0 did not run: "
                             f"{item.text}")
            self.assertEqual(item.kind, assemble.HUMAN)  # never auto-iterated (#264)
        # §6 is deliberately BLUNT here: the un-owned shape shares the `human-empty` token
        # with "ran and produced nothing" rather than growing the status table by one, which
        # is the table the trailer redesign exists to stop growing. Removing the FALSEHOOD
        # ("leaf did not run" about a run that did) was the point; the precision given up in
        # the row is one file-open away, in the placeholder itself, where a human reads it —
        # so that is where it is asserted.
        artifact = (d / "check-review.md").read_text(encoding="utf-8")
        self.assertIn("exited 0", artifact)
        self.assertIn("un-owned artifact", artifact)

    def test_every_leaf_status_the_harness_can_write_has_a_label(self) -> None:
        # The twin-blindness shape one module over: a status added in `leaves` with no label
        # in `assemble` would silently drop the HUMAN forcing for that placeholder. This is
        # the whole guard for an unlabelled status — `_items_from_artifact` deliberately
        # does NOT guess at one it does not recognise (see below).
        statuses = {v for k, v in vars(assemble).items() if k.startswith("LEAF_STATUS_")}
        self.assertEqual(statuses - set(assemble._LEAF_STATUS_LABEL), set())

    def test_an_artifact_that_quotes_an_unknown_status_early_keeps_its_findings(self) -> None:
        # An advisory leaf reviewing THIS harness quotes leaf-status markers — in a finding,
        # and verbatim in a fenced block that can open anywhere, including line 5. Such an
        # artifact is a real verdict: relabelling it "leaf produced no verdict" denies
        # findings that exist and strips their `[impl]` routing (#264), which is what decides
        # whether a rebuild can address them. Position in the file settles nothing, so the
        # unrecognised token settles nothing either.
        d = self._bundle("review")
        (d / "check-review.md").write_text("All advisory items PASS.\n", encoding="utf-8")
        (d / f"check-advisory-{_ADVERSARY}.md").write_text(
            "# Advisory review — adversary\n"           # 1
            "\n"                                        # 2
            "The placeholder it writes reads:\n"        # 3
            "\n"                                        # 4
            "```\n"                                     # 5
            "<!-- pdca:leaf-status some-future-status -->\n"   # 6
            "```\n"                                     # 7
            "\n"
            "- NEEDS-HUMAN [impl] — off-by-one at foo.py:12\n"
            "- NEEDS-HUMAN [impl] — the marker above is emitted by a status no reader "
            "knows\n", encoding="utf-8")
        items = assemble.collect_needs_human(d, self.cfg)
        found = [i for i in items if "off-by-one" in i.text]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].kind, assemble.IMPL,
                         "a real finding lost its [impl] routing to a quoted marker")
        self.assertNotIn("no verdict", found[0].text)
        # …and the artifact is still routable: two builder-fixable findings, nothing else,
        # is exactly the shape auto-iterate exists for (#264).
        self.assertTrue(autoiterate.eligible(items))

    def test_a_placeholder_with_an_unknown_status_is_still_never_auto_iterated(self) -> None:
        # The other half: an unrecognised token is not labelled, and nothing is lost by
        # that. A placeholder's own items are unmarked prose, so they are HUMAN on their own
        # (#264) and the bundle still halts for the human — the marker is not what protects
        # this, and guessing at an unknown one only ever mislabels real artifacts.
        d = self._bundle("review")
        (d / "check-review.md").write_text("All advisory items PASS.\n", encoding="utf-8")
        (d / f"check-advisory-{_ADVERSARY}.md").write_text(
            "# Advisory review — adversary — NOT COMPLETED\n\n"
            "<!-- pdca:leaf-status some-future-status -->\n\n"
            "- NEEDS-HUMAN — the leaf left no findings\n", encoding="utf-8")
        items = assemble.collect_needs_human(d, self.cfg)
        rows = [i for i in items if "no findings" in i.text]
        self.assertEqual([i.kind for i in rows], [assemble.HUMAN])
        self.assertFalse(autoiterate.eligible(items))

    # -- criterion 4a/4b/4c/4e: WHO closed the artifact settles whether it is real --------

    def test_a_closed_artifact_is_real_at_every_reader_whatever_it_quotes(self) -> None:
        # The round's red→green, at all three readers of `assemble.leaf_status` — converted
        # together because they share that one function, not one by one (a fix that lands at
        # two of three sites is the twin blindness this whole slice exists to remove).
        # The artifact is a real report that QUOTES a recognised marker in a fenced block and
        # closes itself with the trailer; the base reads the quote and calls it a placeholder.
        d = self._bundle("closed")
        # (1) `assemble._items_from_artifact` — the §6 parser. A placeholder's items are
        #     relabelled and forced HUMAN, which on a real artifact denies findings that
        #     exist and strips the `[impl]` routing that decides whether Do can fix them.
        items = assemble._items_from_artifact(
            _probe("- NEEDS-HUMAN [impl] — off-by-one at foo.py:12\n", closed=True))
        self.assertEqual([i.kind for i in items], [assemble.IMPL],
                         "a closed artifact's finding lost its [impl] routing to a quote")
        self.assertEqual(items[0].text, "off-by-one at foo.py:12")
        # (2) `size_signal._review_drove_the_iterate` — a clean all-PASS review with no
        #     NEEDS-HUMAN and no FAIL cell drove nothing; only a placeholder is "ambiguous,
        #     count the round", because then nothing reviewed the attempt at all.
        (d / "check-review.md").write_text(_probe(_CLEAN_TABLE, closed=True), encoding="utf-8")
        self.assertFalse(size_signal._review_drove_the_iterate(d),
                         "a closed review was counted as a round nothing reviewed")
        # (3) `leaves._plan_findings` — a placeholder's NEEDS-HUMAN line reports
        #     infrastructure, not the brief, so it is skipped; a real review's is a finding.
        (d / f"plan-advisory-{_ANTAGONIST}.md").write_text(
            _probe("- NEEDS-HUMAN — the success criterion is unfalsifiable\n", closed=True),
            encoding="utf-8")
        self.assertEqual(leaves._plan_findings(d), 1,
                         "a closed plan advisory's finding was discarded as a placeholder's")
        # …and the completion record the same walk feeds (#526) does not call it a leaf that
        # never reviewed the brief.
        self.assertEqual(leaves._plan_not_completed(d), {})

    def test_a_closed_advisory_keeps_its_impl_routing_into_section_6(self) -> None:
        # The same defect where it is actually paid for: §6 and the auto-iterate decision.
        # An advisory reviewing THIS harness must quote status markers to describe them, and
        # the base answered by relabelling every one of its findings "leaf did not run" and
        # forcing them HUMAN — destroying real findings, and with them the #264 routing.
        d = self._bundle("section6")
        (d / "check-review.md").write_text("All advisory items PASS.\n", encoding="utf-8")
        (d / f"check-advisory-{_ADVERSARY}.md").write_text(
            _probe("- NEEDS-HUMAN [impl] — off-by-one at foo.py:12\n"
                   "- NEEDS-HUMAN [impl] — the marker above is quoted, not emitted\n",
                   closed=True), encoding="utf-8")
        items = assemble.collect_needs_human(d, self.cfg)
        found = [i for i in items if "off-by-one" in i.text]
        self.assertEqual([i.kind for i in found], [assemble.IMPL])
        self.assertNotIn("did not run", found[0].text)
        # …two builder-fixable findings and nothing else is exactly the shape auto-iterate
        # exists for; as a "placeholder" the round instead halts for a human who has nothing
        # to decide.
        self.assertTrue(autoiterate.eligible(items))

    def test_the_harness_note_on_a_review_delivered_without_bash_keeps_it_closed(self) -> None:
        # The one place the harness writes into a leaf's artifact after the leaf is done:
        # #526's note that Bash was unavailable, added to a plan review delivered while the
        # vendor sandbox could not start (driven here on #526's pinned vendor stream).
        # Appended BELOW the leaf's trailer, the note would un-close the review, and a closed
        # review quoting a marker would be read as a placeholder again — its finding dropped,
        # its leaf recorded as not completed. The note goes above; the trailer stays last.
        d = self._bundle("bash-down")
        artifact = f"plan-advisory-{_ANTAGONIST}.md"
        self._drive("plan-advisory", d, artifact, deaths=0, dead="",
                    live=_probe("- NEEDS-HUMAN — the success criterion is unfalsifiable\n",
                                closed=True),
                    stream=_FIXTURES / "claude_sandbox_cannot_start.stream.jsonl")
        text = (d / artifact).read_text(encoding="utf-8")
        self.assertIn("Bash was unavailable to this reviewer", text)   # #526's note is kept…
        last = next((ln.strip() for ln in reversed(text.splitlines()) if ln.strip()), "")
        self.assertEqual(last, _TRAILER, "the harness's note un-closed the leaf's review")
        self.assertEqual(assemble.leaf_status(text), "")                # …and it reads real
        self.assertEqual(leaves._plan_findings(d), 1)
        self.assertEqual(leaves._plan_not_completed(d), {})

    def test_the_trailer_counts_only_as_the_artifacts_last_non_blank_line(self) -> None:
        # WHERE it is, is the whole mechanism. Two designs died here already: matching a
        # marker anywhere (holed by any report that quotes one) and an 8-line header window
        # (holed by a fenced block opening at line 5). An anchored, whole-line test at the
        # END is what a quotation cannot forge, and what a truncated attempt is very unlikely
        # to have written — it would need to be cut off exactly on a line quoting the trailer.
        real = f"a real verdict\n\n{_QUOTED}\n{_TRAILER}\n"   # marker as 2nd-to-last line
        self.assertEqual(assemble.leaf_status(real), "")
        self.assertEqual(assemble.leaf_status(f"{real}\n   \n\n"), "")   # trailing blanks
        self.assertEqual(assemble.leaf_status(f"x\n\n{_QUOTED}\n  {_TRAILER}\t\n"), "")
        # …and the two ways it must NOT trip, so that quoting the trailer cannot do what
        # quoting the marker used to: inside a fence, the fence's closing line is the last
        # one; on a line of prose, the match is whole-line after strip, never a substring.
        fenced = (f"# Advisory review — NOT COMPLETED\n\n{_QUOTED}\n\n"
                  f"A leaf closes its artifact with:\n\n```\n{_TRAILER}\n```\n")
        self.assertEqual(assemble.leaf_status(fenced), assemble.LEAF_STATUS_INFRA)
        inline = f"# NOT COMPLETED\n\n{_QUOTED}\n\nsee {_TRAILER} for the rule\n"
        self.assertEqual(assemble.leaf_status(inline), assemble.LEAF_STATUS_INFRA)

    def test_an_artifact_without_the_trailer_classifies_exactly_as_today(self) -> None:
        # 4b — the guard that makes absence INERT. Leaves are arbitrary commands: a
        # third-party one cannot be compelled to emit the trailer and must never be read as
        # permanently failing, a model leaf that finishes and forgets it must not become a
        # failure, and no artifact in the back catalogue carries it at all. So an artifact
        # without the trailer must behave byte-for-byte as it did before this landed —
        # INCLUDING the pre-existing mis-read of a quoted marker below, which only the
        # trailer's PRESENCE is allowed to fix. (Green pre-fix by construction: it asserts
        # the base's behaviour, and is here to catch a regression, not to prove the defect.)
        d = self._bundle("open")
        items = assemble._items_from_artifact(
            _probe("- NEEDS-HUMAN [impl] — off-by-one at foo.py:12\n", closed=False))
        self.assertEqual([i.kind for i in items], [assemble.HUMAN])
        self.assertTrue(items[0].text.startswith("leaf did not run"))
        (d / "check-review.md").write_text(_probe(_CLEAN_TABLE, closed=False),
                                           encoding="utf-8")
        self.assertTrue(size_signal._review_drove_the_iterate(d))
        (d / f"plan-advisory-{_ANTAGONIST}.md").write_text(
            _probe("- NEEDS-HUMAN — the success criterion is unfalsifiable\n", closed=False),
            encoding="utf-8")
        self.assertEqual(leaves._plan_findings(d), 0)
        self.assertEqual(leaves._plan_not_completed(d), {_ANTAGONIST: assemble.LEAF_STATUS_INFRA})
        # …and the shape that has no trailer BECAUSE its leaf never closed one — the
        # harness's own placeholder — is still classified exactly as #278 classified it.
        self.assertEqual(assemble.leaf_status(_PLACEHOLDER), assemble.LEAF_STATUS_INFRA)

    def test_the_recognised_status_set_does_not_grow(self) -> None:
        # 4c — the mechanical expression of the redesign (a guard, green pre-fix). The
        # trailer decides whether an artifact is REAL, so the token table only ever explains
        # WHY one is not, and #541 does not grow it: rounds 2–4 of #541 each patched the
        # marker instead (unknown tokens, a header window, a fourth token) and each re-opened
        # the same hole, the last by self-triggering on any artifact quoting its new token.
        # The un-owned shape therefore shares `human-empty` — true of it, and no machine
        # reader needs to tell it from "ran and produced nothing" — and states its specifics
        # in the placeholder's prose. The set pinned is exactly the one the base already had:
        # #278's three plus #526's `sandbox-empty`, which a machine consumer does act on (the
        # plan-advisory benefit record and its §10 line name the host as the cause). A fifth
        # token is a decision someone must take deliberately; changing this leg is how they
        # take it.
        expected = {"infra-empty", "startup-empty", "sandbox-empty", "human-empty"}
        attrs = {v for k, v in vars(assemble).items() if k.startswith("LEAF_STATUS_")}
        self.assertEqual(attrs, expected)
        self.assertEqual(set(assemble._LEAF_STATUS_LABEL), expected)
        self.assertEqual(len(assemble._LEAF_STATUS_LABEL), len(expected))

    def test_every_instructable_leaf_closes_its_artifact_and_no_placeholder_does(self) -> None:
        # 4e — the write half, asserted rather than eyeballed, and as ONE loop over each set
        # of three sites: a change that reaches two of three leaves the third silently unable
        # to earn a real-artifact reading forever. Every harness-authored prompt ENDS with the
        # instruction; every stub (a leaf that really did finish, offline) emits it; no
        # placeholder does — they exist BECAUSE no leaf closed one, and #278's contract that
        # a placeholder can never smuggle in `[impl]` rests on that.
        self.assertEqual(assemble.LEAF_COMPLETE_TRAILER, _TRAILER)   # one spelling, pinned
        # (1) The prompt each site ACTUALLY hands its leaf — captured off the stub leaf's
        #     stdin, with a project rubric configured — ends with the closing instruction.
        #     The rubric is appended where the review and advisory prompts are composed, so
        #     an instruction built into a prompt constant reads fine on its own and is still
        #     not the leaf's last word: "Nothing may follow it.", then a rubric. Checking the
        #     constant alone passes either way; only the composed prompt tells them apart.
        for site, artifact, _log in _SITES:
            with self.subTest(prompt=site):
                d = self._bundle(f"prompt-{site}")
                (d / "rubric-snapshot.md").write_text(f"- {_RUBRIC_MARK}: no bare unwrap()\n",
                                                      encoding="utf-8")
                seen = self.tmp / f"{site}.prompt"
                self._drive(site, d, artifact, deaths=0, dead="", live=_LIVE_TEXT,
                            prompt=seen)
                prompt = seen.read_text(encoding="utf-8")
                last = prompt.rstrip().rsplit("\n", 1)[-1]
                self.assertIn(_TRAILER, last,
                              f"{site}: the prompt does not END with the closing instruction "
                              f"— its last line is {last[-160:]!r}")
                self.assertIn(artifact, last)                    # …naming this site's file
                self.assertTrue(last.endswith("Nothing may follow it."), last[-160:])
                if site != "plan-advisory":   # the two sites the project rubric is fed to
                    self.assertIn(_RUBRIC_MARK, prompt, f"{site}: no rubric reached the leaf")
                    self.assertLess(prompt.rindex(_RUBRIC_MARK), prompt.rindex(_TRAILER))
        # (2) The stubs close their artifacts; the placeholders never do.
        d = self._bundle("write-sites")
        for name, write, artifact, closes in (
            ("review", lambda: leaves._stub_review(d, self.cfg), "check-review.md", True),
            ("advisory", lambda: leaves._stub_advisory(d, _SPEC, _ADVERSARY),
             f"check-advisory-{_ADVERSARY}.md", True),
            ("plan-advisory", lambda: leaves._stub_plan_advisory(d, _SPEC, _ANTAGONIST),
             f"plan-advisory-{_ANTAGONIST}.md", True),
            ("review", lambda: leaves._review_unavailable(d, "no verdict table"),
             "check-review.md", False),
            ("advisory", lambda: leaves._advisory_unavailable(d, _ADVERSARY, "no findings"),
             f"check-advisory-{_ADVERSARY}.md", False),
            ("plan-advisory",
             lambda: leaves._plan_advisory_unavailable(d, _ANTAGONIST, "no findings"),
             f"plan-advisory-{_ANTAGONIST}.md", False),
        ):
            with self.subTest(site=name, closed=closes):
                with redirect_stderr(io.StringIO()):
                    write()
                text = (d / artifact).read_text(encoding="utf-8")
                last = next((ln.strip() for ln in reversed(text.splitlines()) if ln.strip()),
                            "")
                self.assertEqual(last == _TRAILER, closes,
                                 f"{name}: last non-blank line is {last!r}")
                # …and the reading that follows from it, so the two halves cannot drift.
                self.assertEqual(assemble.leaf_status(text) == "", closes)


if __name__ == "__main__":
    unittest.main()
