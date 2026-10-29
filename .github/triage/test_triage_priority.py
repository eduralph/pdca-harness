"""Tests for triage_priority.py: everything after the model call, offline.

Run: python -I -m unittest discover -s .github/triage
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import types
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import triage_priority as tp  # noqa: E402

CONFIG = tp.load_config()
AREAS = tp.area_names(CONFIG)
NO_FLAGS = {f: False for f in tp.FLAGS}


def verdict(**over) -> dict:
    v = {
        "type": "bug", "severity": "major", "reach": "common", "areas": ["records"],
        "flags": dict(NO_FLAGS), "review_priority": "none", "unsure": False, "missing": "",
        "secret_in_issue": False, "vulnerability_report": False,
        "reasons": {"type": "The driver reads a finished bundle's state wrongly.",
                    "severity": "A bundle is misread as unplanned and its cycle cannot advance.",
                    "reach": "Any bundle that finished a normal cycle hits it.",
                    "area": "The code that derives bundle state from its files.",
                    "overrides": "None apply."},
    }
    v.update(over)
    return v


def issue(**over) -> tp.Issue:
    i = tp.Issue(number=7, title="Finished bundles show as unplanned",
                 body="Steps: run one cycle to the end, then run the status command.", labels=set(), assignees=[])
    for k, val in over.items():
        setattr(i, k, val)
    return i


def plan(i: tp.Issue | None = None, **v) -> tp.Plan:
    return tp.build_plan(i or issue(), tp.validate(verdict(**v), AREAS), CONFIG,
                         model=tp.MODEL, usage="1 in, 1 out", truncated=False)


def prio(severity, reach, flags=None, review="none", labels=()):
    return tp.compute_priority(severity, reach, flags or NO_FLAGS, review, set(labels))


class Table(unittest.TestCase):
    def test_matches_adr_0017(self) -> None:
        expected = {
            ("critical", "common"): 0, ("critical", "edge"): 1, ("critical", "adversarial"): 1,
            ("major", "common"): 1, ("major", "edge"): 2, ("major", "adversarial"): 2,
            ("minor", "common"): 2, ("minor", "edge"): 3, ("minor", "adversarial"): 3,
            ("trivial", "common"): 3, ("trivial", "edge"): 3, ("trivial", "adversarial"): 3,
        }
        for (sev, reach), p in expected.items():
            with self.subTest(sev=sev, reach=reach):
                self.assertEqual(prio(sev, reach)[0], p)

    def test_security_lifts_adversarial_major_to_p1(self) -> None:
        p, base, applied = prio(
            "major", "adversarial", dict(NO_FLAGS, security=True))
        self.assertEqual((p, base), (1, 2))
        self.assertIn("security", applied[0])

    def test_decision_integrity_lifts_edge_minor_to_p1(self) -> None:
        flags = dict(NO_FLAGS, decision_integrity=True)
        self.assertEqual(prio("minor", "edge", flags)[0], 1)

    def test_decision_integrity_lifts_edge_major_to_p1(self) -> None:
        # ADR-0017 section 6: the reviewer's vendor is not checked (major / edge) is P1.
        p, base, notes = prio("major", "edge", dict(NO_FLAGS, decision_integrity=True))
        self.assertEqual((p, base), (1, 2))
        self.assertIn("decision-integrity", notes[0])

    def test_security_lifts_adversarial_critical_to_p1(self) -> None:
        self.assertEqual(prio("critical", "adversarial", dict(NO_FLAGS, security=True))[0], 1)

    def test_no_release_gating_floor(self) -> None:
        # The repository has no release-gating label, so the label is not a floor here.
        self.assertEqual(prio("trivial", "common", labels={"release-gating"})[0], 3)

    def test_no_label_is_a_floor(self) -> None:
        # ADR-0017 section 3 lists no label override.
        self.assertEqual(tp.LABEL_FLOORS, {})
        self.assertEqual(prio("trivial", "edge", labels={"accessibility", "security"})[0], 3)

    def test_review_priority_is_kept(self) -> None:
        # ADR-0017 section 3: a minor/edge deferred finding is P3 by the table, P2 from review.
        p, base, notes = prio("minor", "edge", review="P2")
        self.assertEqual((p, base), (2, 3))
        self.assertIn("deferred review", notes[0])

    def test_review_priority_can_lower_the_table(self) -> None:
        self.assertEqual(prio("major", "common", review="P2")[0], 2)

    def test_review_priority_that_agrees_adds_no_note(self) -> None:
        self.assertEqual(prio("major", "common", review="P1"), (1, 1, []))

    def test_floors_apply_after_the_review_priority(self) -> None:
        p, _, notes = prio("major", "adversarial", dict(NO_FLAGS, decision_integrity=True), review="P2")
        self.assertEqual(p, 1)
        self.assertEqual(len(notes), 1)  # the review agrees with the table; only the floor
        p, _, notes = prio("minor", "common", dict(NO_FLAGS, security=True), review="P3")
        self.assertEqual(p, 1)
        self.assertEqual(len(notes), 2)  # the review lowers it to P3, the floor lifts it to P1

    def test_adr_0017_section_6_examples(self) -> None:
        di, sec = dict(NO_FLAGS, decision_integrity=True), dict(NO_FLAGS, security=True)
        cases = [
            ("critical", "edge", di, 1),         # the sign-off session can edit section 9
            ("critical", "common", NO_FLAGS, 0),  # a leaf that never started shows 0 findings
            ("critical", "edge", sec, 1),         # the builder is not confined to its worktree
            ("critical", "adversarial", sec, 1),  # hostile issue text steers the builder
            ("major", "edge", di, 1),             # the reviewer's vendor is not checked
            ("major", "edge", NO_FLAGS, 2),       # copier update fails with local patches
            ("minor", "common", NO_FLAGS, 2),     # a hard-coded confidence in SUMMARY
        ]
        for sev, reach, flags, want in cases:
            with self.subTest(sev=sev, reach=reach, flags=flags):
                self.assertEqual(prio(sev, reach, flags)[0], want)

    def test_flags_match_adr_0017(self) -> None:
        self.assertEqual(tp.FLAGS, ("security", "decision_integrity"))
        self.assertEqual(tp.FLOORS["security"][0], 1)
        self.assertEqual(tp.FLOORS["decision_integrity"][0], 1)

    def test_floor_never_lowers(self) -> None:
        p, _, applied = prio("critical", "common", dict(NO_FLAGS, security=True))
        self.assertEqual((p, applied), (0, []))

    def test_every_flag_has_a_floor(self) -> None:
        self.assertLessEqual(set(tp.FLAGS), set(tp.FLOORS))


class Validate(unittest.TestCase):
    def test_rejects_unknown_values(self) -> None:
        for bad in ({"severity": "blocker"}, {"reach": "rare"}, {"type": "chore"},
                    {"areas": ["kernel"]}, {"review_priority": "P4"}):
            with self.subTest(bad=bad), self.assertRaises(tp.InvalidVerdict):
                tp.validate(verdict(**bad), AREAS)

    def test_rejects_missing_fields(self) -> None:
        for field in ("flags", "review_priority", "secret_in_issue", "vulnerability_report"):
            v = verdict()
            del v[field]
            with self.subTest(field=field), self.assertRaises(tp.InvalidVerdict):
                tp.validate(v, AREAS)

    def test_at_most_two_areas(self) -> None:
        got = tp.validate(verdict(areas=["records", "machine", "gates"]), AREAS)["areas"]
        self.assertEqual(got, ["records", "machine"])

    def test_no_area_is_allowed(self) -> None:
        self.assertEqual(tp.validate(verdict(areas=[]), AREAS)["areas"], [])

    def test_types_match_adr_0017(self) -> None:
        # No `research` type here: research questions are enhancements (prompt.md).
        self.assertEqual(tp.TYPES, ("bug", "enhancement", "documentation"))
        with self.assertRaises(tp.InvalidVerdict):
            tp.validate(verdict(type="research"), AREAS)

    def test_schema_enums_match_config(self) -> None:
        schema = tp.verdict_schema(AREAS)
        self.assertEqual(schema["properties"]["areas"]["items"]["enum"], AREAS)
        self.assertEqual(set(schema["required"]), set(schema["properties"]))


class Echo(unittest.TestCase):
    BODY = ("The builder leaf in worktree my-target-repo fails on runner ci-7.internal.example "
            "with token ghp_4XQ7EXAMPLE and the log line: gate reported unverifiable at round 48213.")

    def test_withholds_a_reason_that_quotes_the_issue(self) -> None:
        r = tp.scrub("The log line gate reported unverifiable at round shows the failure.", self.BODY)
        self.assertIn("withheld", r)

    def test_withholds_a_reason_that_repeats_a_number(self) -> None:
        self.assertIn("withheld", tp.scrub("The gate at round 48213 fails.", self.BODY))

    def test_withholds_a_single_token_secret(self) -> None:
        for reason in ("The token ghp_4XQ7EXAMPLE is rejected by the tracker.",
                       "The runner ci-7.internal.example cannot start the leaf.",
                       "The worktree my-target-repo is left dirty."):
            with self.subTest(reason=reason):
                self.assertIn("withheld", tp.scrub(reason, self.BODY))

    def test_hyphenated_words_are_not_identifiers(self) -> None:
        body = "A half-finished bundle is shown to a read-only reviewer."
        r = "A half-finished bundle reaches a read-only reviewer leaf."
        self.assertEqual(tp.scrub(r, body), r)

    def test_keeps_a_general_reason(self) -> None:
        r = "A builder leaf fails before the gates run."
        self.assertEqual(tp.scrub(r, self.BODY), r)

    def test_neutralises_mentions_and_markup(self) -> None:
        r = tp.scrub("ping @eduralph <img src=x> | `code` **bold**", "")
        self.assertNotIn("@e", r)
        for ch in "<>|`*":
            self.assertNotIn(ch, r)

    def test_one_word_is_not_a_reason(self) -> None:
        for bare in ("major", "bug", "", "area records"):
            with self.subTest(bare=bare):
                self.assertEqual(tp.scrub(bare, ""), "(no reason given)")

    def test_schema_describes_each_reason(self) -> None:
        props = tp.verdict_schema(AREAS)["properties"]["reasons"]["properties"]
        for key, spec in props.items():
            with self.subTest(reason=key):
                self.assertIn("sentence", spec["description"])

    def test_schema_names_this_project(self) -> None:
        props = tp.verdict_schema(AREAS)["properties"]["reasons"]["properties"]
        self.assertIn("which part of pdca-harness it is in", props["area"]["description"])
        self.assertIn("a bug, an enhancement or a documentation change", props["type"]["description"])

    def test_caps_length(self) -> None:
        self.assertLessEqual(len(tp.scrub("word " * 100, "")), tp.MAX_REASON)


class SecurityNotes(unittest.TestCase):
    def test_vulnerability_report_is_labelled_but_not_restated(self) -> None:
        p = plan(severity="critical", reach="adversarial", vulnerability_report=True,
                 flags=dict(NO_FLAGS, security=True))
        self.assertIn("priority:P1", p.add_labels)
        self.assertIn("report it privately as SECURITY.md describes", p.comment)
        self.assertIn("private advisory", p.comment)
        for reason in verdict()["reasons"].values():
            self.assertNotIn(reason, p.comment)

    def test_vulnerability_report_hides_what_is_missing(self) -> None:
        p = plan(vulnerability_report=True, unsure=True, missing="Which prompt text triggers the escape.")
        self.assertNotIn("prompt text", p.comment)

    def test_secret_note_asks_to_rotate(self) -> None:
        self.assertIn("rotate", plan(secret_in_issue=True).comment)

    def test_no_notes_on_an_ordinary_issue(self) -> None:
        c = plan().comment
        self.assertNotIn("privately", c)
        self.assertNotIn("SECURITY.md", c)
        self.assertNotIn("rotate", c)


class Skip(unittest.TestCase):
    def test_skips(self) -> None:
        cases = {
            "already has a priority": issue(labels={"priority:P2"}),
            "is a pull request": issue(is_pr=True),
            "is closed": issue(state="closed"),
            "was opened by a bot": issue(author_is_bot=True),
            "was already triaged": issue(triage_count=1),
        }
        for why, i in cases.items():
            with self.subTest(why=why):
                self.assertEqual(tp.skip_reason(i), why)

    def test_needs_triage_is_retried_on_an_edit(self) -> None:
        self.assertIsNone(tp.skip_reason(issue(triage_count=1, labels={"needs-triage"}), retry=True))

    def test_backfill_and_reopen_never_retry(self) -> None:
        # A back-fill batch used to pick a needs-triage issue up again and comment again.
        self.assertEqual(tp.skip_reason(issue(triage_count=1, labels={"needs-triage"})),
                         "was already triaged")

    def test_edit_does_not_retry_a_placed_issue(self) -> None:
        self.assertEqual(tp.skip_reason(issue(triage_count=1), retry=True), "was already triaged")

    def test_retries_are_capped(self) -> None:
        i = issue(triage_count=tp.MAX_RETRIES + 1, labels={"needs-triage"})
        self.assertEqual(tp.skip_reason(i, retry=True), "was retried too often")
        i = issue(triage_count=tp.MAX_RETRIES, labels={"needs-triage"})
        self.assertIsNone(tp.skip_reason(i, retry=True))

    def test_only_the_workflow_comments_count(self) -> None:
        bot, person = {"login": tp.BOT_LOGIN}, {"login": "someone"}
        comments = [{"body": tp.MARKER + " x", "user": bot},
                    {"body": "copied: " + tp.MARKER, "user": person},
                    {"body": "no marker", "user": bot},
                    {"body": None, "user": None}]
        self.assertEqual(tp.triage_comments(comments), 1)

    def test_fresh_issue_is_triaged(self) -> None:
        self.assertIsNone(tp.skip_reason(issue()))


class Backfill(unittest.TestCase):
    """The back-fill limit counts only issues that will be triaged (Codex P1 on #944)."""

    def test_already_triaged_issues_do_not_fill_the_batch(self) -> None:
        listed = [{"number": n, "labels": []} for n in range(1, 8)]
        listed.append({"number": 8, "labels": [{"name": "priority:P2"}]})
        # 1-3 already had their one pass and stay at needs-triage; 4-7 are fresh.
        seen = {n: issue(number=n, triage_count=1, labels={"needs-triage"}) for n in (1, 2, 3)}
        old_gh, old_fetch = tp.gh, tp.fetch_issue
        tp.gh = lambda *args, **kw: json.dumps(listed)
        tp.fetch_issue = lambda repo, n: seen.get(n) or issue(number=n)
        try:
            self.assertEqual(tp.open_untriaged("o/r", 2), [4, 5])
            self.assertEqual(tp.open_untriaged("o/r", 20), [4, 5, 6, 7])
        finally:
            tp.gh, tp.fetch_issue = old_gh, old_fetch


class ClearsNeedsTriage(unittest.TestCase):
    def test_a_placed_priority_clears_needs_triage(self) -> None:
        p = plan(i=issue(labels={"needs-triage"}))
        self.assertEqual(p.remove_labels, ["needs-triage"])
        self.assertTrue(any(l.startswith("priority:") for l in p.add_labels))

    def test_still_unsure_keeps_it(self) -> None:
        p = plan(i=issue(labels={"needs-triage"}), unsure=True, missing="Which version ran.")
        self.assertEqual(p.remove_labels, [])

    def test_nothing_removed_on_a_fresh_issue(self) -> None:
        self.assertEqual(plan().remove_labels, [])


class BuildPlan(unittest.TestCase):
    def test_labels_and_priority(self) -> None:
        p = plan()
        self.assertEqual(p.number, 7)
        self.assertEqual(p.add_labels, ["bug", "area:records", "sev:major", "reach:common", "priority:P1"])
        self.assertEqual(p.assignee, "eduralph")
        self.assertTrue(p.comment.startswith(tp.MARKER))
        self.assertIn("major × common is P1", p.comment)

    def test_label_descriptions_fit_github_limit(self) -> None:
        for group in ("severity", "reach", "area", "priority", "other"):
            for name, spec in CONFIG[group].items():
                with self.subTest(label=name):
                    self.assertLessEqual(len(spec["description"]), 100)

    def test_every_label_it_adds_exists_in_config(self) -> None:
        known = {n for g in ("severity", "reach", "area", "priority", "other") for n in CONFIG[g]}
        known |= set(tp.TYPES)
        p = plan(i=issue(), flags=dict(NO_FLAGS, security=True), areas=["leaves", "gates"])
        self.assertLessEqual(set(p.add_labels), known)
        self.assertLessEqual(set(p.add_labels), tp.allowed_labels(CONFIG))

    def test_every_type_gets_a_priority(self) -> None:
        # ADR-0017 section 4: severity, reach and priority for every type, not only bugs.
        for kind in tp.TYPES:
            with self.subTest(kind=kind):
                p = plan(type=kind, severity="minor", reach="common")
                self.assertIn(kind, p.add_labels)
                self.assertIn("priority:P2", p.add_labels)
                self.assertNotIn("no priority", p.comment)

    def test_existing_non_bug_label_still_gets_a_priority(self) -> None:
        p = plan(i=issue(labels={"documentation"}), severity="trivial", reach="common")
        self.assertIn("priority:P3", p.add_labels)

    def test_keeps_existing_type_and_assignee(self) -> None:
        p = plan(i=issue(labels={"enhancement"}, assignees=["someone"]))
        self.assertNotIn("bug", p.add_labels)
        self.assertIsNone(p.assignee)
        self.assertIn("It reads more like bug", p.comment)

    def test_no_type_note_when_the_label_agrees(self) -> None:
        self.assertNotIn("reads more like", plan(i=issue(labels={"bug"})).comment)

    def test_unsure_sets_needs_triage_and_no_priority(self) -> None:
        p = plan(unsure=True, missing="Which model CLI the builder ran.")
        self.assertIn("needs-triage", p.add_labels)
        self.assertFalse(any(l.startswith(("priority:", "sev:", "reach:")) for l in p.add_labels))

    def test_override_is_explained(self) -> None:
        p = plan(severity="critical", reach="edge", flags=dict(NO_FLAGS, decision_integrity=True))
        self.assertIn("priority:P1", p.add_labels)
        p = plan(severity="major", reach="adversarial", flags=dict(NO_FLAGS, security=True))
        self.assertIn("priority:P1", p.add_labels)
        self.assertIn("Then: security bug (at least P1)", p.comment)

    def test_review_priority_is_explained(self) -> None:
        p = plan(severity="minor", reach="edge", review_priority="P2")
        self.assertIn("priority:P2", p.add_labels)
        self.assertIn("kept the deferred review's P2", p.comment)

    def test_never_touches_the_milestone(self) -> None:
        self.assertFalse(hasattr(plan(), "milestone"))

    def test_input_is_capped_and_says_so(self) -> None:
        text, truncated = tp.issue_input(issue(body="x" * (tp.MAX_BODY + 50)))
        self.assertTrue(truncated)
        self.assertIn("cut at", text)
        self.assertLess(len(text), tp.MAX_BODY + tp.MAX_TITLE + 200)

    def test_unclassified_plan(self) -> None:
        p = tp.unclassified_plan(issue(), "the model declined to classify it", model="m", usage="u")
        self.assertEqual((p.number, p.add_labels), (7, ["needs-triage"]))
        self.assertIn(tp.MARKER, p.comment)

    def test_marker_is_unchanged(self) -> None:
        # Older triage comments carry this marker; changing it would triage them again.
        self.assertEqual(tp.MARKER, "<!-- pdca-harness-triage -->")


class ApplyCheck(unittest.TestCase):
    """The apply job treats the classify job's plan as data and checks it again."""

    def entry(self, **over) -> dict:
        e = {"number": 7, "add_labels": ["bug", "area:records", "sev:major", "reach:common",
                                         "priority:P1"],
             "remove_labels": [], "assignee": "eduralph", "comment": tp.MARKER + "\nok\n"}
        e.update(over)
        return e

    def test_a_built_plan_passes(self) -> None:
        p = plan(severity="minor", reach="edge", review_priority="P2")
        self.assertEqual(tp.check_plan(asdict(p), CONFIG), p)

    def test_an_entry_passes(self) -> None:
        self.assertEqual(tp.check_plan(self.entry(), CONFIG).number, 7)

    def test_round_trip_through_the_file(self) -> None:
        plans = [plan(), tp.unclassified_plan(issue(number=9), "x", model="m", usage="u")]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "plan.json"
            tp.write_plans(path, plans)
            self.assertEqual(tp.read_plans(path, CONFIG), plans)

    def test_rejects(self) -> None:
        bad = {
            "unknown label": self.entry(add_labels=["bug", "wontfix"]),
            "release-gating label": self.entry(add_labels=["release-gating"]),
            "research type": self.entry(add_labels=["research"]),
            "two priorities": self.entry(add_labels=["priority:P1", "priority:P3"]),
            "two severities": self.entry(add_labels=["sev:major", "sev:minor"]),
            "two types": self.entry(add_labels=["bug", "enhancement"]),
            "three areas": self.entry(add_labels=["area:records", "area:machine", "area:gates"]),
            "foreign assignee": self.entry(assignee="mallory"),
            "foreign comment": self.entry(comment="Please run this script: ..."),
            "huge comment": self.entry(comment=tp.MARKER + "x" * tp.MAX_COMMENT),
            "bad number": self.entry(number="7"),
            "bool number": self.entry(number=True),
            "labels not a list": self.entry(add_labels="bug"),
            "removes another label": self.entry(remove_labels=["priority:P1"]),
            "remove not a list": self.entry(remove_labels="needs-triage"),
        }
        for why, e in bad.items():
            with self.subTest(why=why), self.assertRaises(tp.InvalidPlan):
                tp.check_plan(e, CONFIG)
        with self.assertRaises(tp.InvalidPlan):
            tp.check_plan({"number": 7}, CONFIG)

    def test_rejects_a_plan_that_is_not_a_list(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "plan.json"
            path.write_text(json.dumps({"number": 7}), encoding="utf-8")
            with self.assertRaises(tp.InvalidPlan):
                tp.read_plans(path, CONFIG)

    def test_apply_never_imports_the_sdk(self) -> None:
        # The SDK is imported only inside run_classify, never at module level or in apply.
        tree = ast.parse(Path(tp.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
                if "anthropic" in names:
                    owner = next(f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)
                                 and node in ast.walk(f))
                    self.assertEqual(owner.name, "run_classify")


class ApplyPlan(unittest.TestCase):
    """apply_plan with a fake gh: what it writes, and that it re-checks the issue first."""

    def run_apply(self, fresh: tp.Issue, p: tp.Plan) -> list[tuple[str, ...]]:
        calls = []
        old_gh, old_fetch = tp.gh, tp.fetch_issue
        tp.gh = lambda *args, **kw: calls.append(args) or ""
        tp.fetch_issue = lambda repo, n: fresh
        try:
            tp.apply_plan("o/r", p)
        finally:
            tp.gh, tp.fetch_issue = old_gh, old_fetch
        return calls

    def test_writes_labels_assignee_and_one_comment(self) -> None:
        calls = self.run_apply(issue(), plan())
        self.assertEqual([c[:2] for c in calls], [("issue", "edit")] * 2 + [("issue", "comment")])
        self.assertIn("--add-label", calls[0])
        self.assertIn("--add-assignee", calls[1])

    def test_skips_an_issue_given_a_priority_meanwhile(self) -> None:
        self.assertEqual(self.run_apply(issue(labels={"priority:P2"}), plan()), [])

    def test_does_not_assign_when_someone_was_assigned_meanwhile(self) -> None:
        calls = self.run_apply(issue(assignees=["someone"]), plan())
        self.assertFalse(any("--add-assignee" in c for c in calls))


class LoadPrompt(unittest.TestCase):
    def write(self, text: str) -> Path:
        d = Path(tempfile.mkdtemp())
        (d / "prompt.md").write_text(text, encoding="utf-8")
        return d / "prompt.md"

    def test_refuses_template_text(self) -> None:
        for text in ("You triage. PROJECT SUMMARY here.", "- `core`: EXAMPLE, replace: x."):
            with self.subTest(text=text), self.assertRaises(SystemExit):
                tp.load_prompt(self.write(text))

    def test_strips_maintainer_notes(self) -> None:
        p = tp.load_prompt(self.write("Rules.\n<!-- note\n  for maintainers -->\n- `a`: b.\n"))
        self.assertEqual(p, "Rules.\n- `a`: b.\n")

    def test_the_real_prompt_loads_unchanged(self) -> None:
        # pdca-harness's prompt.md has no template text and no maintainer notes.
        real = tp.HERE / "prompt.md"
        self.assertEqual(tp.load_prompt(real), real.read_text(encoding="utf-8"))

    def test_the_real_prompt_names_every_verdict_field(self) -> None:
        text = (tp.HERE / "prompt.md").read_text(encoding="utf-8")
        for name in (*tp.FLAGS, "review_priority", "unsure", "missing", "secret_in_issue",
                     "vulnerability_report"):
            with self.subTest(field=name):
                self.assertIn(f"`{name}`", text)
        self.assertIn("changed the impact", text)

    def test_the_real_labels_have_no_template_area(self) -> None:
        self.assertFalse(any("EXAMPLE" in spec["description"] for spec in CONFIG["area"].values()))


if __name__ == "__main__":
    unittest.main()


class PlanHandoff(unittest.TestCase):
    """A plan survives a later failure and is never applied twice (Codex P2s on the
    two-job triage)."""

    def setUp(self) -> None:
        self.saved = {n: getattr(tp, n) for n in ("gh", "fetch_issue", "open_untriaged",
                                                   "classify_issue", "load_config",
                                                   "load_prompt", "ensure_labels")}
        self.addCleanup(lambda: [setattr(tp, n, f) for n, f in self.saved.items()])
        self.calls: list[tuple] = []
        tp.gh = lambda *args, **kw: self.calls.append(args) or ""

    def test_a_replayed_plan_is_skipped(self) -> None:
        p = plan()  # classified with no triage comment on the issue
        tp.fetch_issue = lambda repo, n: issue(triage_count=1, labels={"needs-triage"})
        tp.apply_plan("o/r", p)
        self.assertEqual(self.calls, [])

    def test_an_unchanged_issue_is_applied(self) -> None:
        tp.fetch_issue = lambda repo, n: issue()
        tp.apply_plan("o/r", plan())
        self.assertIn("comment", [c[1] for c in self.calls])

    def test_a_retry_after_an_edit_is_applied(self) -> None:
        p = tp.unclassified_plan(issue(), "x", model="m", usage="u")
        p.triage_count = 1
        tp.fetch_issue = lambda repo, n: issue(triage_count=1, labels={"needs-triage"})
        tp.apply_plan("o/r", p)
        self.assertIn("comment", [c[1] for c in self.calls])

    def test_the_count_round_trips_and_is_checked(self) -> None:
        from dataclasses import asdict
        p = plan()
        p.triage_count = 2
        self.assertEqual(tp.check_plan(asdict(p), CONFIG).triage_count, 2)
        e = asdict(p)
        del e["triage_count"]
        self.assertEqual(tp.check_plan(e, CONFIG).triage_count, 0)
        for bad in (-1, True, "1"):
            with self.subTest(bad=bad), self.assertRaises(tp.InvalidPlan):
                tp.check_plan({**asdict(p), "triage_count": bad}, CONFIG)

    def test_a_later_failure_keeps_the_plans_already_made(self) -> None:
        sdk = types.ModuleType("anthropic")
        sdk.Anthropic = lambda **kw: None
        sdk.RateLimitError = sdk.APIStatusError = sdk.APIConnectionError = type(
            "SDKError", (Exception,), {})
        sys.modules["anthropic"] = sdk
        self.addCleanup(sys.modules.pop, "anthropic", None)
        config = json.loads(json.dumps(CONFIG))
        for spec in config["area"].values():
            spec["description"] = "An area."
        tp.load_config = lambda *a, **kw: config
        tp.load_prompt = lambda *a, **kw: "rules"
        tp.open_untriaged = lambda repo, limit: [1, 2]

        def fetch(repo, n):
            if n == 2:
                raise subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502")
            return issue(number=n)
        tp.fetch_issue = fetch
        tp.classify_issue = lambda *a: (
            tp.Result(tp.validate(verdict(), tp.area_names(config)), None, "m", "u"), False)
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "plan.json"
            args = argparse.Namespace(repo="o/r", all=True, issue=None, limit=20, retry=False,
                                      dry_run=False, out=str(out))
            with self.assertRaises(subprocess.CalledProcessError):
                tp.run_classify(args)
            self.assertEqual([p.number for p in tp.read_plans(out, CONFIG)], [1])
