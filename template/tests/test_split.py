"""The splitter leaf and `pdca split --accept` (issues #322 / #323).

Decomposing an oversized slice by hand is error-prone in exactly the place that matters:
the inter-child `Depends on:` / `Conflicts with:` fields, which are what make the wave
scheduler do the right thing. Fat-finger those and the children either serialise when they
could have run in parallel, or build blind on the same base and conflict at fold.

The doctrine the leaf inherits verbatim: **Do does not split — Do reports. Splitting is the
human's call at sign-off.**
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pdca_harness import cli, driver, leaves, split, state, waves
from pdca_harness.config import Config, LeafConfig

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


def _proposal(*children: str, version: int = 1) -> str:
    body = f"<!-- pdca:split-proposal v{version} -->\n# Split proposal\n\n"
    for i, child in enumerate(children, 1):
        body += (f"<!-- pdca:child child-{i} -->\n{child}\n"
                 f"<!-- pdca:end child-{i} -->\n\n")
    return body


_ONE = "- **Slug:** first\n- **Defect / goal:** a\n"
_TWO_DEP = "- **Slug:** second\n- **Defect / goal:** b\n- **Depends on:** child-1\n"
_TWO_INDEP = "- **Slug:** second\n- **Defect / goal:** b\n"


class Parsing(unittest.TestCase):
    def test_children_are_returned_in_document_order(self) -> None:
        """Order is load-bearing: `--accept` maps children to ids POSITIONALLY, so a
        parser that reordered them would silently mis-assign every id."""
        children = split.parse(_proposal(_ONE, _TWO_DEP))
        self.assertEqual([c.label for c in children], ["child-1", "child-2"])

    def test_a_child_body_may_contain_headings_and_fenced_code(self) -> None:
        """The reason the delimiters are HTML comments: a child body is a full draft brief,
        so anything that could appear INSIDE a child cannot mark its edge."""
        tricky = ("- **Slug:** tricky\n\n## Notes\n\n```md\n- **Slug:** not-a-child\n"
                  "<!-- pdca:end child-1 -->\n```\n")
        children = split.parse(_proposal(tricky))
        self.assertEqual(len(children), 1)
        self.assertIn("not-a-child", children[0].body)

    def test_an_unmarked_or_future_format_is_refused(self) -> None:
        for text, why in ((_proposal(_ONE).replace("<!-- pdca:split-proposal v1 -->", ""),
                           "no version marker"),
                          (_proposal(_ONE, version=99), "unsupported version"),
                          ("<!-- pdca:split-proposal v1 -->\nno children\n", "no children")):
            with self.subTest(case=why):
                with self.assertRaises(split.SplitError):
                    split.parse(text)

    def test_ordering_fields_are_read_but_placeholders_are_not(self) -> None:
        children = split.parse(_proposal(_ONE, _TWO_DEP))
        self.assertEqual(children[1].ordering("Depends on"), ["child-1"])
        placeholder = split.parse(_proposal("- **Slug:** s\n- **Depends on:** <id>\n"))
        self.assertEqual(placeholder[0].ordering("Depends on"), [])


class Accepting(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process", templates_dir=TEMPLATES,
            default_branch="main", tracker_system="github", tracker_url="",
            issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
        )
        self.parent = self.cfg.bundle("500")
        self.parent.mkdir(parents=True)
        (self.parent / "brief.md").write_text("- **Slug:** parent\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, text: str) -> None:
        (self.parent / split.PROPOSAL).write_text(text, encoding="utf-8")

    # -- the rewrite that makes the scheduler work -------------------------------------

    def test_labels_are_rewritten_to_real_ids_in_ordering_fields(self) -> None:
        """Asserted on the resulting FIELD VALUE, not merely that files were written —
        this is the step that makes `compute_waves` work on the output."""
        self._write(_proposal(_ONE, _TWO_DEP))
        created = split.accept(self.parent, ["601", "602"], self.cfg)
        body = (created[1] / "brief.md").read_text(encoding="utf-8")
        self.assertIn("- **Depends on:** 601", body)
        self.assertNotIn("child-1", body)

    def test_prose_mentioning_a_label_is_left_alone(self) -> None:
        """A blanket substitution would corrupt a child that explains its seam in prose."""
        self._write(_proposal("- **Slug:** s\n- **Defect / goal:** unlike child-2, this…\n",
                              _TWO_INDEP))
        created = split.accept(self.parent, ["601", "602"], self.cfg)
        self.assertIn("unlike child-2", (created[0] / "brief.md").read_text(encoding="utf-8"))

    # -- validation happens before any write -------------------------------------------

    def test_id_count_mismatch_is_refused_not_guessed(self) -> None:
        self._write(_proposal(_ONE, _TWO_DEP))
        with self.assertRaises(split.SplitError):
            split.accept(self.parent, ["601"], self.cfg)
        self.assertEqual(list(self.cfg.bundle_root.glob("issue_6*")), [],
                         "a child was created despite the refusal")

    def test_duplicate_ids_are_refused(self) -> None:
        self._write(_proposal(_ONE, _TWO_DEP))
        with self.assertRaises(split.SplitError):
            split.accept(self.parent, ["601", "601"], self.cfg)

    def test_colliding_with_an_existing_bundle_is_refused(self) -> None:
        self.cfg.bundle("601").mkdir(parents=True)
        self._write(_proposal(_ONE, _TWO_DEP))
        with self.assertRaises(split.SplitError):
            split.accept(self.parent, ["601", "602"], self.cfg)
        self.assertFalse((self.cfg.bundle("602")).exists(),
                         "a sibling was created before the collision was detected")

    def test_an_unresolvable_label_is_refused(self) -> None:
        self._write(_proposal(_ONE, "- **Slug:** s\n- **Depends on:** child-9\n"))
        with self.assertRaises(split.SplitError):
            split.accept(self.parent, ["601", "602"], self.cfg)

    def test_nothing_is_left_behind_on_failure(self) -> None:
        """A part-written accept is worse than either outcome: the human can neither re-run
        (the ids exist) nor proceed (the batch is incomplete)."""
        self._write(_proposal(_ONE, "- **Slug:** s\n- **Depends on:** child-9\n"))
        with self.assertRaises(split.SplitError):
            split.accept(self.parent, ["601", "602"], self.cfg)
        self.assertEqual(list(self.cfg.bundle_root.glob("issue_6*")), [])
        self.assertFalse((self.parent / ".split-staging").exists(), "staging left behind")

    # -- the parent ---------------------------------------------------------------------

    def test_the_parent_is_marked_split_and_takes_the_close_path(self) -> None:
        self._write(_proposal(_ONE, _TWO_INDEP))
        split.accept(self.parent, ["601", "602"], self.cfg)
        self.assertEqual((self.parent / state.CLOSE_MARKER).read_text().strip(), "split")
        self.assertEqual(driver._close_class(self.parent, self.cfg), "split")

    def test_an_ITERATED_parent_still_takes_the_close_path(self) -> None:
        """The realistic split parent: it failed an attempt BEFORE anyone concluded it was
        too large. `_close_class` excludes any bundle with an `iteration-v*` archive from
        the hint path, so a brief-hint rewrite alone would silently run a normal build."""
        (self.parent / "iteration-v1").mkdir()
        self._write(_proposal(_ONE, _TWO_INDEP))
        split.accept(self.parent, ["601", "602"], self.cfg)
        self.assertEqual(driver._close_class(self.parent, self.cfg), "split",
                         "an iterated split parent fell through to a real build")

    def test_reopening_a_split_parent_still_works(self) -> None:
        """The marker is in DOWNSTREAM_OF_BRIEF, so an iterate archives it and the next
        pass runs a real build — the close stays a decision, not a trap."""
        self.assertIn(state.CLOSE_MARKER, driver.DOWNSTREAM_OF_BRIEF)

    # -- the promise the whole feature rests on ------------------------------------------

    def test_round_trip_stub_proposal_to_scheduled_waves(self) -> None:
        """Offline, end to end: stub splitter → --accept → the wave plan.

        This is the proof that the parallel/stacked promise actually holds. Two dependent
        children must schedule as TWO waves; two independent ones as ONE. If the label→id
        rewrite were wrong, `compute_waves` would see dangling references and this is where
        it shows.
        """
        leaves.do_split(self.parent, self.cfg)          # stub writes a 2-child proposal
        created = split.accept(self.parent, ["601", "602"], self.cfg)
        self.assertEqual(len(waves.compute_waves(self.cfg, created)), 2,
                         "a declared dependency did not stack the children")

        other = self.cfg.bundle("700")
        other.mkdir(parents=True)
        (other / "brief.md").write_text(_proposal(_ONE, _TWO_INDEP), encoding="utf-8")
        (other / split.PROPOSAL).write_text(_proposal(_ONE, _TWO_INDEP), encoding="utf-8")
        indep = split.accept(other, ["801", "802"], self.cfg)
        self.assertEqual(len(waves.compute_waves(self.cfg, indep)), 1,
                         "independent children were serialised instead of parallelised")


class SplitterLeaf(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process", templates_dir=TEMPLATES,
            default_branch="main", tracker_system="github", tracker_url="",
            issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
        )
        self.d = self.cfg.bundle("500")
        self.d.mkdir(parents=True)
        (self.d / "brief.md").write_text("- **Slug:** parent\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_leaf_writes_exactly_one_file(self) -> None:
        """Asserted on the directory listing, not just the file's presence: "propose seams,
        never cut them" means no bundles, no branches, no edits to brief.md."""
        before = {p.name for p in self.d.iterdir()}
        self.assertEqual(leaves.do_split(self.d, self.cfg), 0)
        self.assertEqual({p.name for p in self.d.iterdir()} - before, {split.PROPOSAL})

    def test_a_bundle_with_no_brief_is_refused(self) -> None:
        (self.d / "brief.md").unlink()
        self.assertEqual(leaves.do_split(self.d, self.cfg), 1)

    def test_the_shipped_template_parses(self) -> None:
        """The template teaches the format, so it must BE the format — a template whose own
        delimiters did not parse would be discovered only by a real split."""
        children = split.parse((TEMPLATES / "split-proposal.md.tpl").read_text("utf-8"))
        self.assertEqual([c.label for c in children], ["child-1", "child-2"])




class ReviewFixes(unittest.TestCase):
    """Regressions from the codex review of #322/#323."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process", templates_dir=TEMPLATES,
            default_branch="main", tracker_system="github", tracker_url="",
            issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
        )
        self.parent = self.cfg.bundle("500")
        self.parent.mkdir(parents=True)
        (self.parent / "brief.md").write_text("- **Slug:** parent\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_fenced_end_marker_does_not_truncate_the_child(self) -> None:
        """The earlier test asserted only the content BEFORE the fake terminator, so it
        passed while every field after it was silently dropped."""
        body = ("- **Slug:** tricky\n\n```md\n<!-- pdca:end child-1 -->\n```\n"
                "- **Success criterion:** SURVIVES\n")
        children = split.parse(_proposal(body))
        self.assertEqual(len(children), 1)
        self.assertIn("SURVIVES", children[0].body,
                      "fields after a fenced end-marker were dropped")

    def test_a_mismatched_end_label_is_refused_not_skipped(self) -> None:
        text = ("<!-- pdca:split-proposal v1 -->\n"
                "<!-- pdca:child child-1 -->\n- **Slug:** a\n<!-- pdca:end child-1 -->\n"
                "<!-- pdca:child child-2 -->\n- **Slug:** b\n<!-- pdca:end child-9 -->\n")
        with self.assertRaises(split.SplitError):
            split.parse(text)

    def test_cyclic_dependencies_are_refused_before_writing(self) -> None:
        (self.parent / split.PROPOSAL).write_text(_proposal(
            "- **Slug:** a\n- **Depends on:** child-2\n",
            "- **Slug:** b\n- **Depends on:** child-1\n"), encoding="utf-8")
        with self.assertRaises(split.SplitError):
            split.accept(self.parent, ["601", "602"], self.cfg)
        self.assertEqual(list(self.cfg.bundle_root.glob("issue_6*")), [])

    def test_the_abandoned_attempt_is_archived(self) -> None:
        """A split is decided at sign-off, so the parent still carries the rejected
        attempt. Leaving patch.diff + SUMMARY.md live lets publish ship the very
        implementation the split exists to abandon."""
        (self.parent / "patch.diff").write_text("abandoned\n", encoding="utf-8")
        (self.parent / "SUMMARY.md").write_text("stale\n", encoding="utf-8")
        (self.parent / split.PROPOSAL).write_text(_proposal(_ONE, _TWO_INDEP),
                                                  encoding="utf-8")
        split.accept(self.parent, ["601", "602"], self.cfg)
        self.assertFalse((self.parent / "patch.diff").exists(),
                         "the abandoned patch is still live — publish could ship it")
        self.assertTrue(list(self.parent.glob("iteration-v*/patch.diff")),
                        "the attempt was destroyed rather than archived")

    def test_a_second_acceptance_is_refused(self) -> None:
        (self.parent / split.PROPOSAL).write_text(_proposal(_ONE, _TWO_INDEP),
                                                  encoding="utf-8")
        split.accept(self.parent, ["601", "602"], self.cfg)
        with self.assertRaises(split.SplitError):
            split.accept(self.parent, ["701", "702"], self.cfg)
        self.assertFalse(self.cfg.bundle("701").exists())

    def test_a_completed_id_is_refused(self) -> None:
        (self.cfg.bundle_root / "completed" / "issue_601").mkdir(parents=True)
        (self.parent / split.PROPOSAL).write_text(_proposal(_ONE, _TWO_INDEP),
                                                  encoding="utf-8")
        with self.assertRaises(split.SplitError):
            split.accept(self.parent, ["601", "602"], self.cfg)

    def test_a_frozen_bundle_is_not_splittable(self) -> None:
        (self.parent / "patch.diff").write_text("x", encoding="utf-8")
        (self.parent / "check-gates.json").write_text("[]", encoding="utf-8")
        (self.parent / "SUMMARY.md").write_text(
            "## 9. Sign-off\n\nOutcome: accepted\n", encoding="utf-8")
        if state.state(self.parent) == state.COMPLETE:
            self.assertEqual(leaves.do_split(self.parent, self.cfg), 1)

    def test_split_is_not_a_close_disposition_token(self) -> None:
        """`close_class` SUBSTRING-matches, so a generic "split" token would send
        `likely-fix — split parser failure` down the close fast path."""
        self.assertEqual(self.cfg.close_class("likely-fix — split parser failure"), "")
        self.assertEqual(self.cfg.close_class("split-brain repro"), "")

    def test_the_shipped_child_schema_can_publish(self) -> None:
        """A filled Slug alone makes state() call the child PLANNED, so flow skips Plan
        and sends it to Do — a child with no `Repo + branch target` builds fine and then
        has nowhere to publish."""
        tpl = (TEMPLATES / "split-proposal.md.tpl").read_text(encoding="utf-8")
        for child in split.parse(tpl):
            with self.subTest(child=child.label):
                self.assertIn("Repo + branch target", child.body)
                self.assertIn("External dependencies", child.body)


class FencedOrderingFields(unittest.TestCase):
    """The last deferred finding from the #354 review: fenced examples are content.

    A child body is a full draft brief and the format explicitly permits fenced code, so a
    child illustrating `- **Depends on:** child-2` in an example must not have that example
    treated as metadata. The reader and the rewriter share one fence-aware iterator — two
    different views of the same document is how a reviewed proposal materialises into
    something else.
    """

    def test_a_fenced_ordering_line_is_not_rewritten(self) -> None:
        body = ("- **Slug:** s\n\n```md\n- **Depends on:** child-2\n```\n"
                "- **Depends on:** child-2\n")
        out = split.rewrite_ordering(body, {"child-2": "642"})
        self.assertIn("```md\n- **Depends on:** child-2\n```", out,
                      "the fenced example was rewritten")
        self.assertIn("\n- **Depends on:** 642", out,
                      "the real ordering field was not rewritten")

    def test_a_fenced_ordering_line_is_not_read_as_a_reference(self) -> None:
        """Otherwise a well-formed proposal fails the unknown-label or cycle check on its
        own documentation."""
        body = "- **Slug:** s\n\n```md\n- **Depends on:** child-9\n```\n"
        self.assertEqual(split.Child("child-1", body).ordering("Depends on"), [])

    def test_a_proposal_documenting_the_format_still_accepts(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        cfg = Config(
            root=tmp, bundle_root=tmp / "results", process_dir=tmp / "process",
            templates_dir=TEMPLATES, default_branch="main", tracker_system="github",
            tracker_url="", issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"))
        parent = cfg.bundle("500")
        parent.mkdir(parents=True)
        (parent / "brief.md").write_text("- **Slug:** p\n", encoding="utf-8")
        (parent / split.PROPOSAL).write_text(_proposal(
            "- **Slug:** a\n\n```md\n- **Depends on:** child-9\n```\n",
            "- **Slug:** b\n- **Depends on:** child-1\n"), encoding="utf-8")
        created = split.accept(parent, ["601", "602"], cfg)
        self.assertEqual(len(created), 2)
        self.assertIn("- **Depends on:** 601",
                      (created[1] / "brief.md").read_text(encoding="utf-8"))
        shutil.rmtree(tmp, ignore_errors=True)


class TheSplitterReadsTheSizer(unittest.TestCase):
    """The splitter is the consumer that needs the sizer's answer most (#351 review).

    `_split_prompt` passed only the STRUCTURAL band and reasons — "difficulty=high;
    3 conflicts declared" — and dropped `proposed_seams` and `independent_outcomes`
    entirely. So an instance paid one model to find the seams and then paid a second to
    rediscover them, with the first answer sitting unread in the same bundle.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.d = self.tmp / "results" / "issue_1"
        self.d.mkdir(parents=True)
        (self.d / "brief.md").write_text("- **Slug:** s\n- **Difficulty:** high\n",
                                         encoding="utf-8")
        self.cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process", templates_dir=TEMPLATES,
            default_branch="main", tracker_system="github", tracker_url="",
            issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _verdict(self, **kw) -> None:
        """Stamped with the brief's digest — `_split_prompt` reads through
        `current_sizing`, so an unstamped verdict is treated as belonging to some other
        brief and correctly ignored."""
        import json
        from pdca_harness import leaves as _lv
        key = _lv._sizer_key(self.d, self.cfg, self.d / "brief.md")
        (self.d / "sizing.json").write_text(json.dumps({
            "band": "oversized",
            "independent_outcomes": ["parser rewrite", "renderer rewrite"],
            "proposed_seams": ["split at the parser/renderer boundary"],
            "brief_sha": key, **kw}), encoding="utf-8")

    def test_the_seams_and_outcomes_reach_the_prompt(self) -> None:
        self._verdict()
        prompt = leaves._split_prompt(self.d, self.cfg)
        self.assertIn("split at the parser/renderer boundary", prompt)
        self.assertIn("renderer rewrite", prompt)

    def test_the_prior_is_framed_as_a_starting_point(self) -> None:
        """A verdict presented as settled invites ratification. The splitter sees the
        brief and can disagree — and a reasoned disagreement is worth more than assent."""
        self._verdict()
        prompt = leaves._split_prompt(self.d, self.cfg)
        self.assertIn("STARTING POINT", prompt)
        self.assertIn("disagree", prompt)

    def test_no_verdict_leaves_the_prompt_unchanged(self) -> None:
        """The sizer is optional; a splitter run without one must read as it always did."""
        self.assertNotIn("sizer has already", leaves._split_prompt(self.d, self.cfg))

    def test_the_splitter_never_invokes_the_paid_leaf(self) -> None:
        """READ, not re-run: paying a second model to rediscover the first one's answer is
        the waste this fixes."""
        from unittest import mock
        self._verdict()
        with mock.patch.object(leaves, "run_sizer") as sizer:
            leaves._split_prompt(self.d, self.cfg)
        sizer.assert_not_called()


class TheDoctrineIsConsistent(unittest.TestCase):
    """Splitting is a PLAN activity, and every role prompt has to say the same thing.

    The sizer was corrected to "they decide at Plan" in an earlier round while the splitter
    still said "the human's call at sign-off" — two prompts in one feature disagreeing about
    when the decision is made. A split authors briefs, and authoring briefs is Plan's beat.
    """

    AGENTS = Path(__file__).resolve().parents[1] / "agents"

    def _text(self, name: str) -> str:
        """`<name>.md.jinja` in the template checkout, `<name>.md` in a rendered instance.

        This suite runs in BOTH — `tests/test_render_and_run` drives the generated
        project's own tests — and reading only the `.jinja` name passes locally while
        failing every render. Third occurrence of this shape after the `.gitignore` and
        `pdca.toml` assertions, which is why it is stated here rather than just fixed.
        """
        for candidate in (f"{name}.md.jinja", f"{name}.md"):
            path = self.AGENTS / candidate
            if path.is_file():
                return path.read_text(encoding="utf-8")
        raise AssertionError(f"no role prompt found for {name!r} in {self.AGENTS}")

    def test_no_role_prompt_places_the_split_at_sign_off(self) -> None:
        for role in ("splitter", "sizer"):
            with self.subTest(role=role):
                self.assertNotIn("call at sign-off", self._text(role))

    def test_no_RUNTIME_prompt_places_the_split_at_sign_off(self) -> None:
        """The role files were corrected and the task prompts were not, so every real
        `pdca split` session was told the opposite of its own role.

        This assertion has now failed to catch the same defect TWICE, each time for a
        different reason, so it is worth stating what it must do rather than only what it
        checks. First it scanned only the role files, and the prompt sent to the model is
        built in code. Then it scanned `inspect.getsource`, and the offending sentence was
        split across two adjacent string literals — `"…call at "` `"sign-off."` — so the
        substring was never present in the source text although it was present in every
        rendered prompt. The only text that means anything is the string the model
        receives, so this RENDERS each prompt and scans that.
        """
        from pdca_harness import leaves
        d = self._rendered_bundle()
        for fn in (leaves._split_prompt, leaves._sizer_prompt):
            with self.subTest(prompt=fn.__name__):
                rendered = fn(d, self._cfg)
                self.assertNotIn("call at sign-off", rendered)
                self.assertNotIn("human's call at sign-off", rendered)
                self.assertIn("PLAN", rendered)

    def _rendered_bundle(self) -> Path:
        import tempfile
        from types import SimpleNamespace
        root = Path(tempfile.mkdtemp())
        d = root / "results" / "issue_1"
        d.mkdir(parents=True)
        (d / "brief.md").write_text("- **Slug:** s\n- **Difficulty:** high\n",
                                    encoding="utf-8")
        self._cfg = SimpleNamespace(templates_dir=root / "templates", sizing={},
                                    root=root, tracker_system="github", tracker_url="")
        return d

    def test_the_splitter_says_the_split_is_authored_in_plan(self) -> None:
        self.assertIn("authored in PLAN", self._text("splitter"))

    def test_the_splitter_routes_a_late_discovery_through_iterate_plan(self) -> None:
        """Run after a build, the answer is not "split anyway" — it is to go back to Plan,
        because the children would inherit nothing from the attempt."""
        self.assertIn("iterate-plan", self._text("splitter"))

    def test_signoff_maps_too_big_to_iterate_plan(self) -> None:
        """`iterate-do` is the tempting wrong answer: the findings look
        implementation-shaped every round, which is how a bundle burns its whole iterate
        budget without converging."""
        text = self._text("signoff")
        self.assertIn("too big is `iterate-plan`", text)
        self.assertIn("Not `iterate-do`", text)
        self.assertIn("Not\n`discontinue`", text.replace("—", "—"))


class AcceptIsSafe(unittest.TestCase):
    """Pre-merge review of #354."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process", templates_dir=TEMPLATES,
            default_branch="main", tracker_system="github", tracker_url="",
            issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"))
        self.parent = self.cfg.bundle("500")
        self.parent.mkdir(parents=True)
        (self.parent / "brief.md").write_text("- **Slug:** p\n", encoding="utf-8")
        (self.parent / split.PROPOSAL).write_text(_proposal(_ONE, _TWO_INDEP),
                                                  encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_path_shaped_id_is_refused(self) -> None:
        """`cfg.bundle("x/foo")` is `results/issue_x/foo`, whose NAME is "foo" — so
        validation checked one path and the move installed to `results/foo`, nesting into a
        pre-existing directory, recording it as created, and rolling it back on a later
        failure. `rmtree` on something this command never made."""
        victim = self.cfg.bundle_root / "foo"
        victim.mkdir(parents=True)
        (victim / "keep.txt").write_text("pre-existing\n", encoding="utf-8")
        for bad in ("x/foo", "../escape", "a b"):
            with self.subTest(bad=bad):
                with self.assertRaises(split.SplitError):
                    split.accept(self.parent, [bad, "602"], self.cfg)
        self.assertTrue((victim / "keep.txt").exists(),
                        "rollback deleted a directory the command never created")

    def test_a_placeholder_does_not_hide_a_later_dependency(self) -> None:
        """`ordering()` returned [] at the first placeholder, so a real value below it
        passed validation unchecked while `rewrite_ordering` still rewrote it — and
        `parse_fields` keeps the FIRST field, so compute_waves saw no dependency and put
        both children in one wave."""
        body = "- **Slug:** b\n- **Depends on:** <child-N…>\n- **Depends on:** child-1\n"
        self.assertEqual(split.Child("child-2", body).ordering("Depends on"), ["child-1"])

    def test_a_bogus_label_below_a_placeholder_is_still_refused(self) -> None:
        (self.parent / split.PROPOSAL).write_text(_proposal(
            _ONE, "- **Slug:** b\n- **Depends on:** <id>\n- **Depends on:** child-9\n"),
            encoding="utf-8")
        with self.assertRaises(split.SplitError):
            split.accept(self.parent, ["601", "602"], self.cfg)

    def test_prefixed_ids_are_canonicalised(self) -> None:
        """`#601` comes from issue_id_example; `issue_601` comes from copying a bundle
        directory name. Both are the same tracker id, and `brief._id_list` strips either
        when reading a dependency — so leaving them raw created `issue_issue_601` while the
        rewritten `Depends on` resolved to `601`, and `pdca flow` aborted on an unresolved
        dependency AFTER the parent had been marked split."""
        from types import SimpleNamespace
        from pdca_harness import cli, leaves
        cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process", templates_dir=TEMPLATES,
            default_branch="main", tracker_system="github", tracker_url="",
            issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"))
        leaves.do_split(self.parent, cfg)
        rc = cli._split(cfg, SimpleNamespace(issue_id="500", accept=True,
                                             ids="issue_601, #602"))
        self.assertEqual(rc, 0)
        self.assertTrue(cfg.bundle("601").is_dir())
        self.assertTrue(cfg.bundle("602").is_dir())
        self.assertFalse((cfg.bundle_root / "issue_issue_601").exists())
        self.assertIn("- **Depends on:** 601",
                      (cfg.bundle("602") / "brief.md").read_text(encoding="utf-8"))

    def test_ids_the_scheduler_cannot_parse_are_refused(self) -> None:
        """`brief._id_list` treats a lowercase token with no digit as prose, so
        `--ids alpha,beta` would write `Depends on: alpha`, `compute_waves` would read no
        dependency, and the children would run in one wave — the ordering fields failing
        silently, which is the one outcome this feature exists to prevent."""
        from pdca_harness import brief as _brief
        self.assertEqual(_brief._id_list("alpha"), [])
        with self.assertRaises(split.SplitError):
            split.accept(self.parent, ["alpha", "beta"], self.cfg)
        self.assertEqual(list(self.cfg.bundle_root.glob("issue_alpha*")), [])

    def test_ids_the_scheduler_does_parse_are_accepted(self) -> None:
        """Validated by round-trip through `_id_list`, so the two cannot drift: anything
        it reads back unchanged is usable, whatever its shape."""
        for good in ("601", "MANT-1", "a1"):
            with self.subTest(issue_id=good):
                from pdca_harness import brief as _brief
                self.assertEqual(_brief._id_list(good), [good])

    def test_the_splitter_is_in_the_doctor_preflight(self) -> None:
        """`pdca split` spawns it like any other command leaf; omitting it let
        `--strict` pass while the split later died on an uninstalled CLI."""
        from pdca_harness import doctor
        cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process", templates_dir=TEMPLATES,
            default_branch="main", tracker_system="github", tracker_url="",
            issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"))
        cfg.splitter = LeafConfig(mode="command", family="claude", argv=["splitter-cli"])
        self.assertIn("splitter", doctor._command_leaves(cfg))


class FilingChildIssues(unittest.TestCase):
    """`--accept` without `--ids` files the child issues itself (#358).

    #323 left this to the human "to keep the tracker the source of truth". Inside an
    interactive Plan session the human is present and approving, and the friction was
    real: leave the session, file N issues by hand, come back with the numbers.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process", templates_dir=TEMPLATES,
            default_branch="main", tracker_system="github",
            tracker_url="https://github.com/acme/widgets",
            issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
        )
        self.parent = self.cfg.bundle("500")
        self.parent.mkdir(parents=True)
        (self.parent / "brief.md").write_text("- **Slug:** parent\n", encoding="utf-8")
        (self.parent / split.PROPOSAL).write_text(_proposal(_ONE, _TWO_DEP),
                                                  encoding="utf-8")
        self.calls: list[list[str]] = []

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _gh(self, numbers, *, fail_at=None, stdout=None):
        """A fake `gh` that records its argv and hands back issue URLs.

        It ASSERTS the calling convention rather than accepting anything: `cmd` must be a
        list (a string would be handed to a shell), and `capture_output`/`text` must both
        be true (without `text` the real `subprocess.run` returns bytes, and `.strip()` on
        bytes would silently never match the URL regex). Recording `list(cmd)` while
        ignoring the flags meant dropping either one still passed every test here.
        """
        def run(cmd, capture_output=False, text=False, cwd=None):
            assert isinstance(cmd, list), f"argv must be a list, not {type(cmd).__name__}"
            assert capture_output is True, "stdout must be captured to read the issue URL"
            assert text is True, "text=True is required or stdout arrives as bytes"
            self.calls.append(list(cmd))
            n = len(self.calls)
            if fail_at is not None and n == fail_at:
                return SimpleNamespace(returncode=1, stdout="", stderr="gh: HTTP 403")
            if stdout is not None:
                return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
            url = f"https://github.com/acme/widgets/issues/{numbers[n - 1]}"
            return SimpleNamespace(returncode=0, stdout=url + "\n", stderr="")
        return run

    def _patched(self, run):
        return mock.patch.multiple(
            "pdca_harness.split",
            subprocess=SimpleNamespace(run=run),
            shutil=SimpleNamespace(which=lambda _n: "/usr/bin/gh",
                                   rmtree=shutil.rmtree, move=shutil.move))

    def test_it_files_one_sub_issue_per_child_in_order(self) -> None:
        children = split.parse((self.parent / split.PROPOSAL).read_text(encoding="utf-8"))
        with self._patched(self._gh(["601", "602"])):
            ids = split.file_children(self.parent, children, self.cfg)
        self.assertEqual(ids, ["601", "602"])
        self.assertEqual(len(self.calls), 2)
        for call, child in zip(self.calls, split.parse(_proposal(_ONE, _TWO_DEP))):
            self.assertEqual(call[:3], ["gh", "issue", "create"])
            # The BODY, which nothing asserted: filing an issue whose body is empty (or
            # another child's) loses the slice the human is being asked to approve.
            self.assertIn("--body", call)
            body = call[call.index("--body") + 1]
            self.assertIn(child.body.strip().splitlines()[0], body)
            self.assertIn("#500", body, "the body does not name its parent")
            self.assertIn("--repo", call)
            self.assertEqual(call[call.index("--repo") + 1], "acme/widgets")
            # A REAL tracker relationship, not a convention in the body text — the parent
            # becomes an umbrella and each child gets its own PR.
            self.assertIn("--parent", call)
            self.assertEqual(call[call.index("--parent") + 1], "500")

    def test_the_title_comes_from_the_child_itself(self) -> None:
        """Read from the proposal rather than generated, so the tracker shows what the
        proposal actually says."""
        text = _proposal("# Extract the parser\n\n- **Slug:** parser\n")
        (self.parent / split.PROPOSAL).write_text(text, encoding="utf-8")
        children = split.parse(text)
        with self._patched(self._gh(["601"])):
            split.file_children(self.parent, children, self.cfg)
        call = self.calls[0]
        # Pinned to --title's POSITION. Membership in argv would also be satisfied if the
        # heading were passed as some other flag's value.
        self.assertEqual(call[call.index("--title") + 1], "Extract the parser")

    def test_a_child_with_no_heading_still_gets_a_title(self) -> None:
        """An untitled issue is worse than a dull one."""
        child = split.parse(_proposal("- **Defect / goal:** a\n"))[0]
        self.assertTrue(split.child_title(child, self.parent).strip())

    def test_a_partial_failure_names_every_issue_it_already_created(self) -> None:
        """Tracker issues cannot be rolled back. The failure this forbids is the SILENT
        one — issues created for children whose bundles never appeared, with nothing on
        screen naming them."""
        children = split.parse((self.parent / split.PROPOSAL).read_text(encoding="utf-8"))
        with self._patched(self._gh(["601", "602"], fail_at=2)):
            with self.assertRaises(split.SplitError) as caught:
                split.file_children(self.parent, children, self.cfg)
        msg = str(caught.exception)
        self.assertIn("#601", msg)
        self.assertIn("Filed 1 of 2", msg)
        self.assertIn("cannot be rolled back", msg)
        self.assertIn("--ids 601,<id>", msg)

    def test_an_unreadable_issue_url_is_reported_not_swallowed(self) -> None:
        """The issue may well have been created; the caller must be told a number it
        cannot name may now exist."""
        children = split.parse(_proposal(_ONE))
        with self._patched(self._gh([], stdout="created something\n")):
            with self.assertRaises(split.SplitError) as caught:
                split.file_children(self.parent, children, self.cfg)
        self.assertIn("no issue URL", str(caught.exception))


class TrackerUnavailableRequiresIds(unittest.TestCase):
    """Never a silent skip — a split that filed nothing and materialised nothing would
    look like a no-op."""

    def _cfg(self, root: Path, **over) -> Config:
        base = dict(tracker_system="github", tracker_url="https://github.com/acme/widgets")
        base.update(over)
        return Config(
            root=root, bundle_root=root / "results", process_dir=root / "process",
            templates_dir=TEMPLATES, default_branch="main", issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"), **base)

    def test_a_non_github_tracker_refuses(self) -> None:
        cfg = self._cfg(Path(tempfile.mkdtemp()), tracker_system="gitlab")
        ok, why = split.can_file(cfg)
        self.assertFalse(ok)
        self.assertIn("not GitHub", why)

    def test_an_unknown_repository_refuses(self) -> None:
        cfg = self._cfg(Path(tempfile.mkdtemp()), tracker_url="")
        ok, why = split.can_file(cfg)
        self.assertFalse(ok)
        self.assertIn("repository could not be determined", why)

    def test_a_missing_gh_refuses(self) -> None:
        cfg = self._cfg(Path(tempfile.mkdtemp()))
        with mock.patch("pdca_harness.split.shutil.which", return_value=None):
            ok, why = split.can_file(cfg)
        self.assertFalse(ok)
        self.assertIn("`gh` is not on PATH", why)

    def test_the_refusal_is_a_SplitError_subclass(self) -> None:
        """So every existing caller still handles it, while the CLI can still tell "your
        proposal is wrong" from "I cannot reach your tracker"."""
        self.assertTrue(issubclass(split.TrackerUnavailable, split.SplitError))


class EndToEndThroughWaves(unittest.TestCase):
    """The assertion 0.56 never made: proposal → accept → the children SCHEDULE.

    #322 was designed around `flow_batch` enumerating bundles AFTER `do_plan_batch`, so
    children created during Plan are picked up by the same run and scheduled with no new
    code. That property had never been exercised end to end.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process", templates_dir=TEMPLATES,
            default_branch="main", tracker_system="github",
            tracker_url="https://github.com/acme/widgets", issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
        )
        self.parent = self.cfg.bundle("500")
        self.parent.mkdir(parents=True)
        (self.parent / "brief.md").write_text("- **Slug:** parent\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _accept(self, proposal_text: str) -> list[str]:
        (self.parent / split.PROPOSAL).write_text(proposal_text, encoding="utf-8")
        ids = ["601", "602"]
        split.accept(self.parent, ids, self.cfg)
        return ids

    def _waves(self, ids: list[str]) -> list[list[str]]:
        got = waves.compute_waves(self.cfg, [self.cfg.bundle(i) for i in ids])
        return [sorted(d.name for d in wave) for wave in got]

    def test_independent_children_schedule_in_ONE_wave(self) -> None:
        ids = self._accept(_proposal(_ONE, _TWO_INDEP))
        self.assertEqual(self._waves(ids), [["issue_601", "issue_602"]])

    def test_a_dependent_child_schedules_in_TWO_waves_in_order(self) -> None:
        """The `Depends on:` rewrite is what makes this work — the proposal names
        `child-1`, and the materialised brief has to name `601`."""
        ids = self._accept(_proposal(_ONE, _TWO_DEP))
        self.assertEqual(self._waves(ids), [["issue_601"], ["issue_602"]])
        self.assertIn("601", (self.cfg.bundle("602") / "brief.md").read_text(
            encoding="utf-8"))

    def test_the_parent_is_terminal_and_never_builds(self) -> None:
        """Asserted on the parent's STATE, not on its absence from a wave list it was
        never in: `_waves(ids)` is handed the child bundles only, so the old assertion
        could not have failed however the parent was left."""
        from pdca_harness import driver as _driver
        self._accept(_proposal(_ONE, _TWO_DEP))
        self.assertTrue((self.parent / state.CLOSE_MARKER).exists())
        self.assertIn("issue_601", (self.parent / "build-notes.md").read_text(
            encoding="utf-8"))
        # The real guarantee. The parent is NOT a halted state — it is `BUILT`, and it
        # goes on to sign-off so the human confirms the split, which is the design. What
        # must never happen is the BUILDER running on it, and `_close_class` is what
        # routes `advance` past the builder. Asserting "terminal" instead was both wrong
        # about the design and, in its first form, unfalsifiable.
        self.assertEqual(_driver._close_class(self.parent, self.cfg), "split")
        # DRIVEN, not merely inspected. Asserting `patch.diff` is absent without ever
        # advancing the bundle was unfalsifiable — nothing had run that could have created
        # one. The parent is driven to a halt with the builder wired to fail the test.
        def must_not_run(*a, **k):
            raise AssertionError("the builder ran on a split parent")

        with mock.patch.object(_driver.leaves, "do_build", must_not_run), \
             mock.patch.object(_driver.leaves, "run_review", must_not_run), \
             redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            final = _driver.run_issue(self.parent, self.cfg)
        self.assertIn(final, ("AWAITING_SIGNOFF", "COMPLETE", "DISCONTINUED"))
        self.assertFalse((self.parent / "patch.diff").exists())


class CliFilesTheIssuesItself(unittest.TestCase):
    """`pdca split <id> --accept` with no `--ids` — the flow #358 exists to create."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process", templates_dir=TEMPLATES,
            default_branch="main", tracker_system="github",
            tracker_url="https://github.com/acme/widgets", issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"))
        self.parent = self.cfg.bundle("500")
        self.parent.mkdir(parents=True)
        (self.parent / "brief.md").write_text("- **Slug:** parent\n", encoding="utf-8")
        (self.parent / split.PROPOSAL).write_text(_proposal(_ONE, _TWO_DEP),
                                                  encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _args(self, ids: str = ""):
        return SimpleNamespace(issue_id="500", accept=True, ids=ids)

    def _run(self, filer):
        err = io.StringIO()
        with mock.patch("pdca_harness.split.file_children", filer), \
             redirect_stderr(err), redirect_stdout(io.StringIO()):
            rc = cli._split(self.cfg, self._args())
        return rc, err.getvalue()

    def test_no_ids_files_them_and_materialises_the_children(self) -> None:
        rc, err = self._run(lambda parent, children, cfg, **kw: ["601", "602"])
        self.assertEqual(rc, 0)
        self.assertTrue(self.cfg.bundle("601").is_dir())
        self.assertTrue(self.cfg.bundle("602").is_dir())
        self.assertIn("filed 2 child issue(s): #601, #602", err)
        self.assertIn("flow 601 602", err)

    def test_an_unreachable_tracker_refuses_and_names_ids(self) -> None:
        """Never a silent skip: a split that filed nothing and materialised nothing would
        look like a no-op."""
        def filer(parent, children, cfg, **kw):
            raise split.TrackerUnavailable("`gh` is not on PATH, so this cannot file "
                                           "the child issues for you")
        rc, err = self._run(filer)
        self.assertEqual(rc, 1)
        self.assertIn("gh` is not on PATH", err)
        self.assertIn("--accept --ids <id-1>,<id-2>", err)
        self.assertFalse(self.cfg.bundle("601").exists())
        self.assertFalse((self.parent / state.CLOSE_MARKER).exists())

    def test_a_malformed_proposal_is_refused_BEFORE_anything_is_filed(self) -> None:
        """A tracker issue cannot be rolled back, so creating three of them for a proposal
        that then fails to parse is the worst possible order."""
        (self.parent / split.PROPOSAL).write_text("no marker here\n", encoding="utf-8")
        called = []
        rc, err = self._run(lambda *a, **kw: called.append(a) or ["601"])
        self.assertEqual(rc, 1)
        self.assertEqual(called, [], "issues were filed for an unparseable proposal")
        self.assertIn("split-proposal", err)

    def test_a_failure_AFTER_filing_names_the_issues_it_cannot_withdraw(self) -> None:
        """The one failure this feature must not have: real issues orphaned with nothing
        on screen naming them."""
        (self.cfg.bundle("602")).mkdir(parents=True)   # collides during accept
        rc, err = self._run(lambda parent, children, cfg, **kw: ["601", "602"])
        self.assertEqual(rc, 1)
        self.assertIn("CANNOT be rolled back", err)
        self.assertIn("#601", err)
        self.assertIn("#602", err)
        self.assertIn("--accept --ids 601,602", err)

    def test_explicit_ids_never_reach_the_filer(self) -> None:
        """`--ids` stays the path for a human who already filed them."""
        called = []
        with mock.patch("pdca_harness.split.file_children",
                        lambda *a, **kw: called.append(a) or []), \
             redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            rc = cli._split(self.cfg, self._args(ids="601,602"))
        self.assertEqual(rc, 0)
        self.assertEqual(called, [])
        # BOTH, not just the first: silently dropping the second id still passed.
        self.assertTrue(self.cfg.bundle("601").is_dir())
        self.assertTrue(self.cfg.bundle("602").is_dir())
        self.assertIn("- **Depends on:** 601",
                      (self.cfg.bundle("602") / "brief.md").read_text(encoding="utf-8"))


class ThePlannerIsToldItOwnsTheSplit(unittest.TestCase):
    """#358's real subject. The planner role never mentioned splitting at all, so the
    beat that owns the decision was the one beat never told about it."""

    AGENTS = Path(__file__).resolve().parents[1] / "agents"

    def _role(self, name: str) -> str:
        for candidate in (f"{name}.md.jinja", f"{name}.md"):
            path = self.AGENTS / candidate
            if path.is_file():
                return path.read_text(encoding="utf-8")
        raise AssertionError(f"no role prompt for {name!r}")

    def test_the_planner_role_names_the_command_and_the_beat(self) -> None:
        text = self._role("planner")
        self.assertIn("pdca split", text)
        self.assertIn("--accept", text)
        self.assertIn("iterate-plan", text)

    def test_the_RUNTIME_plan_prompt_says_it_too(self) -> None:
        """Rendered, not scanned as source — see `TheDoctrineIsConsistent` for why that
        distinction has already cost this feature two missed defects."""
        d = self.cfg.bundle("500")
        prompt = leaves._plan_prompt(self.cfg, None, d)
        self.assertIn("pdca split 500", prompt)
        self.assertIn("--accept", prompt)
        self.assertIn("SPLIT IT IN THIS BEAT", prompt)

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process", templates_dir=TEMPLATES,
            default_branch="main", tracker_system="github", tracker_url="",
            issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_role_still_tells_the_human_to_file_issues_by_hand(self) -> None:
        """The friction #358 removes. If this string returns, the flow has regressed to
        "leave the session, file N issues, come back with the numbers"."""
        for role in ("planner", "splitter", "signoff"):
            with self.subTest(role=role):
                self.assertNotIn("file a tracker issue per child", self._role(role))
                self.assertNotIn("files a tracker\nissue per child", self._role(role))


class CodexReviewHardening(unittest.TestCase):
    """The paths the codex review of this PR was examining when its budget ran out."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process", templates_dir=TEMPLATES,
            default_branch="main", tracker_system="github",
            tracker_url="https://github.com/acme/widgets", issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"))
        self.parent = self.cfg.bundle("500")
        self.parent.mkdir(parents=True)
        self.calls: list[list[str]] = []

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _patched(self, run):
        return mock.patch.multiple(
            "pdca_harness.split",
            subprocess=SimpleNamespace(run=run),
            shutil=SimpleNamespace(which=lambda _n: "/usr/bin/gh",
                                   rmtree=shutil.rmtree, move=shutil.move))

    def _run_returning(self, stdout: str):
        def run(cmd, capture_output=False, text=False, cwd=None):
            self.calls.append(list(cmd))
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        return run

    def test_a_notice_printed_before_the_url_does_not_lose_the_number(self) -> None:
        """`gh` also emits notices and, in some configurations, a "Creating issue in
        owner/repo" preamble. Anchoring to the end of the WHOLE output failed on a call
        that had in fact created the issue — the worst case, since it is then reported as
        a number we cannot name."""
        children = split.parse(_proposal(_ONE))
        stdout = ("Creating issue in acme/widgets\n"
                  "Warning: 3 uncommitted changes\n"
                  "https://github.com/acme/widgets/issues/601\n")
        with self._patched(self._run_returning(stdout)):
            self.assertEqual(split.file_children(self.parent, children, self.cfg), ["601"])

    def test_no_url_at_all_is_still_reported(self) -> None:
        children = split.parse(_proposal(_ONE))
        with self._patched(self._run_returning("created something\n")):
            with self.assertRaises(split.SplitError) as caught:
                split.file_children(self.parent, children, self.cfg)
        self.assertIn("no issue URL", str(caught.exception))

    def test_an_unexpected_exception_still_names_what_was_filed(self) -> None:
        """`except Exception`, not `except SplitError`: anything escaping the loop would
        otherwise lose the list of issues already created — the one thing this function
        must never do."""
        children = split.parse(_proposal(_ONE, _TWO_DEP))
        state_ = {"n": 0}

        def run(cmd, capture_output=False, text=False, cwd=None):
            state_["n"] += 1
            if state_["n"] == 2:
                raise RuntimeError("something nobody predicted")
            return SimpleNamespace(
                returncode=0, stdout="https://github.com/acme/widgets/issues/601\n",
                stderr="")
        with self._patched(run):
            with self.assertRaises(split.SplitError) as caught:
                split.file_children(self.parent, children, self.cfg)
        msg = str(caught.exception)
        self.assertIn("#601", msg)
        self.assertIn("cannot be rolled back", msg)

    def test_a_non_numeric_parent_files_flat_and_SAYS_so(self) -> None:
        """The umbrella relationship is half the reason this exists, so producing a flat
        set of unrelated issues has to be announced rather than inferred."""
        parent = self.cfg.bundle("hotfix-alpha")
        parent.mkdir(parents=True)
        children = split.parse(_proposal(_ONE))
        err = io.StringIO()
        with self._patched(self._run_returning(
                "https://github.com/acme/widgets/issues/601\n")), redirect_stderr(err):
            split.file_children(parent, children, self.cfg)
        self.assertNotIn("--parent", self.calls[0])
        self.assertIn("NOT as sub-issues", err.getvalue())

    def test_the_command_is_an_argv_list_so_the_shell_never_sees_it(self) -> None:
        """Titles and bodies come from a MODEL-authored proposal file. Passed as a list
        with shell=False they are arguments, not script."""
        nasty = "- **Slug:** a\n\n# ; rm -rf / $(whoami) `id` && echo pwned\n"
        children = split.parse(_proposal(nasty))
        with self._patched(self._run_returning(
                "https://github.com/acme/widgets/issues/601\n")):
            split.file_children(self.parent, children, self.cfg)
        # `assertIsInstance(cmd, list)` here was vacuous — the fake recorded `list(cmd)`,
        # which coerces a string into a list of characters. The fake now ASSERTS the type
        # at the call, so a string command fails before it is recorded; what is left to
        # check here is that the payload travelled as ONE argv element rather than being
        # split or interpolated.
        cmd = self.calls[0]
        self.assertEqual(cmd[0], "gh")
        self.assertIn("; rm -rf / $(whoami) `id` && echo pwned", cmd,
                      "the payload is not a single argv element — it was split or the "
                      "shell would see it")


class CodexVerifyFixes(unittest.TestCase):
    """Findings from the verification pass."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process", templates_dir=TEMPLATES,
            default_branch="main", tracker_system="github",
            tracker_url="https://github.com/acme/widgets", issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"))
        self.parent = self.cfg.bundle("500")
        self.parent.mkdir(parents=True)
        (self.parent / "brief.md").write_text("- **Slug:** parent\n", encoding="utf-8")
        (self.parent / split.PROPOSAL).write_text(_proposal(_ONE, _TWO_DEP),
                                                  encoding="utf-8")
        self.filed: list[object] = []

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _accept(self, ids: str = ""):
        def filer(parent, children, cfg, **kw):
            self.filed.append(children)
            return ["601", "602"]
        err = io.StringIO()
        with mock.patch("pdca_harness.split.file_children", filer), \
             redirect_stderr(err), redirect_stdout(io.StringIO()):
            rc = cli._split(self.cfg, SimpleNamespace(issue_id="500", accept=True, ids=ids))
        return rc, err.getvalue()

    def test_a_SECOND_accept_files_nothing(self) -> None:
        """P1. Filing happened before `accept()`'s preconditions, so re-running a
        successful `--accept` created a whole second set of REAL sub-issues and only then
        discovered the parent was already split. Tracker issues cannot be withdrawn, so the
        order is the entire guarantee."""
        rc, _ = self._accept()
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.filed), 1)

        rc, err = self._accept()
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.filed), 1, "a second set of real issues was filed")
        self.assertIn("already marked", err)

    def test_a_cyclic_proposal_files_nothing(self) -> None:
        """Same order defect, different trigger: `validate` caught the cycle only after
        the children had been filed."""
        cyclic = _proposal("- **Slug:** a\n- **Depends on:** child-2\n",
                           "- **Slug:** b\n- **Depends on:** child-1\n")
        (self.parent / split.PROPOSAL).write_text(cyclic, encoding="utf-8")
        rc, err = self._accept()
        self.assertEqual(rc, 1)
        self.assertEqual(self.filed, [], "issues were filed for an unschedulable proposal")
        self.assertIn("cycle", err)

    def test_a_proposal_naming_an_unknown_sibling_files_nothing(self) -> None:
        bad = _proposal("- **Slug:** a\n- **Depends on:** child-9\n")
        (self.parent / split.PROPOSAL).write_text(bad, encoding="utf-8")
        rc, err = self._accept()
        self.assertEqual(rc, 1)
        self.assertEqual(self.filed, [])
        self.assertIn("not a child of this proposal", err)

    def test_accept_still_checks_everything_itself(self) -> None:
        """`preflight` is a pre-filing gate, not a replacement: `accept()` is reachable
        directly via `--ids` and from every test, and must never depend on a caller
        having run the checks."""
        cyclic = _proposal("- **Slug:** a\n- **Depends on:** child-2\n",
                           "- **Slug:** b\n- **Depends on:** child-1\n")
        (self.parent / split.PROPOSAL).write_text(cyclic, encoding="utf-8")
        with self.assertRaises(split.SplitError):
            split.accept(self.parent, ["601", "602"], self.cfg)

    def _prompts(self) -> dict[str, str]:
        role = Path(__file__).resolve().parents[1] / "agents" / "planner.md.jinja"
        if not role.is_file():
            role = Path(__file__).resolve().parents[1] / "agents" / "planner.md"
        return {"role": role.read_text(encoding="utf-8"),
                "runtime": leaves._plan_prompt(self.cfg, None, self.parent)}

    def _planned(self, iid: str) -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "brief.md").write_text(f"- **Slug:** s{iid}\n", encoding="utf-8")
        return d

    def _scheduled_by(self, fn, *args, **kwargs) -> set[str]:
        """The bundle names a flow entry point actually hands to the driver.

        Observed at `_drive_and_act`, which is where the set is finally decided — so this
        measures behaviour rather than the presence of a token in the source. The previous
        version scanned `inspect.getsource` for `_bundle_dirs(cfg)`, which is the same
        anti-pattern that let a stale doctrine survive in `_split_prompt` and two heading
        variants survive in `sizing`: source text is not behaviour, and a helper rename or
        an indirection would have kept it green while reversing what it asserts.
        """
        from pdca_harness import flow
        seen: set[str] = set()

        def capture(cfg, bundles, **kw):
            seen.update(b.name for b in bundles)
            return {}

        with mock.patch.object(flow, "_drive_and_act", capture), \
             mock.patch.object(flow.leaves, "do_plan_batch", lambda *a, **k: None), \
             redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            fn(*args, **kwargs)
        return seen

    def test_a_CSV_batch_picks_up_a_child_CREATED_DURING_the_plan_beat(self) -> None:
        """The promise both prompts make, and the ORDERING is the whole of it.

        Creating the child before the call proved only that `flow_batch` reads the disk —
        an implementation that enumerated BEFORE `do_plan_batch` would have passed too,
        while failing at the one thing claimed. The child is now created by the stubbed
        Plan beat itself, so the test fails unless enumeration genuinely follows it.
        """
        from pdca_harness import flow
        self._planned("500")
        seen: set[str] = set()

        def capture(cfg, bundles, **kw):
            seen.update(b.name for b in bundles)
            return {}

        def plan_creating_a_child(cfg, csv=None, **kw):
            self._planned("601")                  # `pdca split --accept` during Plan

        with mock.patch.object(flow, "_drive_and_act", capture), \
             mock.patch.object(flow.leaves, "do_plan_batch", plan_creating_a_child), \
             redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            flow.flow_batch(self.cfg, csv=None)
        self.assertIn("issue_601", seen,
                      "a child created during the Plan beat was not scheduled — either "
                      "enumeration moved before do_plan_batch, or it no longer reads disk")

    def test_an_explicit_id_LIST_does_not(self) -> None:
        """The correction that took three attempts. `pdca flow 500 501` reads as a batch
        and is not one: it iterates the ids it was handed, so a child created during its
        Plan beat is silently never built."""
        from pdca_harness import flow
        self._planned("500")
        self._planned("501")
        self._planned("601")                      # created during Plan, never named
        seen = self._scheduled_by(flow.flow_ids, self.cfg, ["500", "501"])
        self.assertEqual(seen, {"issue_500", "issue_501"})
        self.assertNotIn("issue_601", seen)

    def test_the_prompts_name_the_csv_batch_as_the_only_self_scheduling_shape(self) -> None:
        """P1 on the second attempt at this. The first said "the run continues into waves
        on its own" (false for every single-issue run); the correction said "a batch run
        (CSV, or several ids)", which is still false for `pdca flow 500 501` — that goes
        through `flow_ids` and iterates only the ids it was handed, so the children are
        silently never built."""
        for where, body in self._prompts().items():
            with self.subTest(where=where):
                low = body.lower()
                self.assertIn("csv", low)
                self.assertIn("pdca flow 500 501", low if where == "runtime" else low,
                              "the explicit-id-list case is the one that reads as a batch "
                              "and is not one — it has to be named, not implied")
                self.assertIn("pdca flow <child-ids>", body)

    def test_neither_prompt_calls_an_explicit_id_list_a_batch(self) -> None:
        for where, body in self._prompts().items():
            with self.subTest(where=where):
                self.assertNotIn("or several ids", body)


class CodexRound4(unittest.TestCase):
    """Round four."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process", templates_dir=TEMPLATES,
            default_branch="main", tracker_system="github",
            tracker_url="https://github.com/acme/widgets", issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"))
        self.parent = self.cfg.bundle("500")
        self.parent.mkdir(parents=True)
        (self.parent / "brief.md").write_text("- **Slug:** parent\n", encoding="utf-8")
        (self.parent / split.PROPOSAL).write_text(_proposal(_ONE, _TWO_DEP),
                                                  encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_failed_breadcrumb_leaves_the_parent_RECOVERABLE(self) -> None:
        """P1. `CLOSE_MARKER` is what makes the parent terminal and `_rollback` only
        removes child directories, so writing the marker BEFORE `build-notes.md` meant a
        failure on the breadcrumb deleted the children and left the parent marked `split`
        — tracker issues filed, every retry refused as "already marked", and no ordinary
        way back. The breadcrumb is written first now, and the marker is removed on the
        way out regardless."""
        real_write = Path.write_text

        def boom(self_, data, *a, **k):
            if self_.name == "build-notes.md":
                raise OSError(28, "No space left on device")
            return real_write(self_, data, *a, **k)

        with mock.patch.object(Path, "write_text", boom):
            with self.assertRaises(OSError):
                split.accept(self.parent, ["601", "602"], self.cfg)

        self.assertFalse((self.parent / state.CLOSE_MARKER).exists(),
                         "the parent stayed terminal with its children rolled back")
        self.assertFalse(self.cfg.bundle("601").exists())
        self.assertFalse(self.cfg.bundle("602").exists())
        # …and the documented retry actually works now.
        created = split.accept(self.parent, ["601", "602"], self.cfg)
        self.assertEqual(len(created), 2)
        self.assertTrue((self.parent / state.CLOSE_MARKER).exists())

    def test_the_recovery_command_uses_the_installed_program_name(self) -> None:
        """P2. A rendered project installs its own console script (`pdca-gramps`), which
        is why the CLI has `_prog()`. The one command printed for recovering already-filed
        tracker issues hard-coded `pdca` — guidance that does not exist there."""
        children = split.parse((self.parent / split.PROPOSAL).read_text(encoding="utf-8"))
        calls = {"n": 0}

        def run(cmd, capture_output=False, text=False, cwd=None):
            calls["n"] += 1
            if calls["n"] == 2:
                return SimpleNamespace(returncode=1, stdout="", stderr="gh: HTTP 403")
            return SimpleNamespace(
                returncode=0, stdout="https://github.com/acme/widgets/issues/601\n",
                stderr="")

        with mock.patch.multiple(
                "pdca_harness.split",
                subprocess=SimpleNamespace(run=run),
                shutil=SimpleNamespace(which=lambda _n: "/usr/bin/gh",
                                       rmtree=shutil.rmtree, move=shutil.move)):
            with self.assertRaises(split.SplitError) as caught:
                split.file_children(self.parent, children, self.cfg, prog="pdca-gramps")
        self.assertIn("pdca-gramps split 500 --accept --ids", str(caught.exception))

    def test_the_cli_passes_its_own_program_name_through(self) -> None:
        seen = {}

        def filer(parent, children, cfg, *, prog="pdca"):
            seen["prog"] = prog
            raise split.SplitError("stop here")

        with mock.patch("pdca_harness.split.file_children", filer), \
             mock.patch.object(cli.sys, "argv", ["pdca-gramps", "split"]), \
             redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            cli._split(self.cfg, SimpleNamespace(issue_id="500", accept=True, ids=""))
        self.assertEqual(seen["prog"], "pdca-gramps")


class TheWholeChainUnmocked(unittest.TestCase):
    """CLI -> preflight -> file_children -> _create_issue -> accept, with ONLY `gh` faked.

    Every other test of this flow patches `split.file_children`, so the filing code itself
    — argv construction, URL parsing, the parent link, the id handed to `accept` — was
    never exercised from the command the operator actually runs. A test that mocks the
    unit under test passes whether or not that unit works.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process", templates_dir=TEMPLATES,
            default_branch="main", tracker_system="github",
            tracker_url="https://github.com/acme/widgets", issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"))
        self.parent = self.cfg.bundle("500")
        self.parent.mkdir(parents=True)
        (self.parent / "brief.md").write_text("- **Slug:** parent\n", encoding="utf-8")
        (self.parent / split.PROPOSAL).write_text(_proposal(_ONE, _TWO_DEP),
                                                  encoding="utf-8")
        self.calls: list[list[str]] = []

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_gh(self, numbers):
        def run(cmd, capture_output=False, text=False, cwd=None):
            assert isinstance(cmd, list) and capture_output is True and text is True
            self.calls.append(list(cmd))
            n = numbers[len(self.calls) - 1]
            return SimpleNamespace(
                returncode=0, stderr="",
                # Realistic output: a preamble line, the URL alone, then a notice that
                # itself mentions a DIFFERENT issue.
                # The trailing notice ENDS with a different issue URL. That is what makes
                # this fixture load-bearing: a regex matching "line ending in /issues/N"
                # picks 999 here. Only a WHOLE-LINE URL match gets the right answer.
                stdout=(f"Creating issue in acme/widgets\n"
                        f"https://github.com/acme/widgets/issues/{n}\n"
                        f"Note: related to https://github.com/acme/widgets/issues/999\n"))
        return run

    def test_end_to_end_with_only_gh_faked(self) -> None:
        with mock.patch("pdca_harness.split.subprocess",
                        SimpleNamespace(run=self._fake_gh(["601", "602"]))), \
             mock.patch("pdca_harness.split.shutil.which", return_value="/usr/bin/gh"), \
             redirect_stderr(io.StringIO()) as err, redirect_stdout(io.StringIO()):
            rc = cli._split(self.cfg, SimpleNamespace(issue_id="500", accept=True, ids=""))

        self.assertEqual(rc, 0)
        # The number came from the URL LINE, not from the notice mentioning #999.
        self.assertTrue(self.cfg.bundle("601").is_dir())
        self.assertTrue(self.cfg.bundle("602").is_dir())
        self.assertFalse(self.cfg.bundle("999").exists(),
                         "the id was read from a notice rather than the created issue")
        # …the ordering field was rewritten to the REAL id…
        self.assertIn("- **Depends on:** 601",
                      (self.cfg.bundle("602") / "brief.md").read_text(encoding="utf-8"))
        # …each child was filed as a sub-issue of the parent…
        for call in self.calls:
            self.assertEqual(call[call.index("--parent") + 1], "500")
        # …and the parent is terminal with its breadcrumb.
        self.assertTrue((self.parent / state.CLOSE_MARKER).exists())
        self.assertIn("issue_601", (self.parent / "build-notes.md").read_text(
            encoding="utf-8"))
        self.assertIn("flow 601 602", err.getvalue())

    def test_the_children_then_schedule_as_waves(self) -> None:
        """The end of the chain #358 exists to create — asserted on bundles produced by
        the real filing path rather than hand-written ones."""
        with mock.patch("pdca_harness.split.subprocess",
                        SimpleNamespace(run=self._fake_gh(["601", "602"]))), \
             mock.patch("pdca_harness.split.shutil.which", return_value="/usr/bin/gh"), \
             redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            cli._split(self.cfg, SimpleNamespace(issue_id="500", accept=True, ids=""))
        got = waves.compute_waves(
            self.cfg, [self.cfg.bundle("601"), self.cfg.bundle("602")])
        self.assertEqual([sorted(d.name for d in w) for w in got],
                         [["issue_601"], ["issue_602"]])

    def test_a_gh_failure_midway_names_the_issue_it_already_filed(self) -> None:
        """The partial-failure path, also never run unmocked."""
        def run(cmd, capture_output=False, text=False, cwd=None):
            self.calls.append(list(cmd))
            if len(self.calls) == 2:
                return SimpleNamespace(returncode=1, stdout="", stderr="gh: HTTP 403")
            return SimpleNamespace(
                returncode=0, stderr="",
                stdout="https://github.com/acme/widgets/issues/601\n")

        with mock.patch("pdca_harness.split.subprocess", SimpleNamespace(run=run)), \
             mock.patch("pdca_harness.split.shutil.which", return_value="/usr/bin/gh"), \
             redirect_stderr(io.StringIO()) as err, redirect_stdout(io.StringIO()):
            rc = cli._split(self.cfg, SimpleNamespace(issue_id="500", accept=True, ids=""))
        self.assertEqual(rc, 1)
        out = err.getvalue()
        self.assertIn("#601", out)
        self.assertIn("cannot be rolled back", out)
        self.assertFalse((self.parent / state.CLOSE_MARKER).exists())
        self.assertFalse(self.cfg.bundle("601").exists(),
                         "a bundle was created for a batch that never completed filing")


class RollbackCoversAPartialMove(unittest.TestCase):
    """`shutil.move` is not atomic across filesystems: it can create the destination, copy
    part of the tree, and then raise. Recording `created` AFTER the move meant such a
    bundle was invisible to `_rollback` and left behind with its parent unmarked — after
    which every retry refused it as an existing bundle."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process", templates_dir=TEMPLATES,
            default_branch="main", tracker_system="github", tracker_url="",
            issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"))
        self.parent = self.cfg.bundle("500")
        self.parent.mkdir(parents=True)
        (self.parent / "brief.md").write_text("- **Slug:** parent\n", encoding="utf-8")
        (self.parent / split.PROPOSAL).write_text(_proposal(_ONE, _TWO_DEP),
                                                  encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_move_that_creates_the_destination_then_fails_is_rolled_back(self) -> None:
        real_move = shutil.move
        calls = {"n": 0}

        def half_move(src, dst, *a, **k):
            calls["n"] += 1
            if calls["n"] == 2:
                Path(dst).mkdir(parents=True, exist_ok=True)      # partially arrived…
                (Path(dst) / "brief.md").write_text("half\n", encoding="utf-8")
                raise OSError(28, "No space left on device")       # …then failed
            return real_move(src, dst, *a, **k)

        with mock.patch.object(split.shutil, "move", half_move):
            with self.assertRaises(OSError):
                split.accept(self.parent, ["601", "602"], self.cfg)

        self.assertFalse(self.cfg.bundle("601").exists(),
                         "the completed child was not rolled back")
        self.assertFalse(self.cfg.bundle("602").exists(),
                         "the HALF-MOVED child survived the rollback and now blocks "
                         "every retry as an existing bundle")
        self.assertFalse((self.parent / state.CLOSE_MARKER).exists())

    def test_a_rollback_that_cannot_delete_says_so(self) -> None:
        """`ignore_errors=True` alone left a bundle on disk and said nothing, so the
        printed retry failed on an "already exists" nobody could have anticipated."""
        err = io.StringIO()
        with mock.patch.object(split.shutil, "rmtree", lambda *a, **k: None), \
             redirect_stderr(err):
            split._rollback([self.parent])          # exists, and "cannot" be removed
        self.assertIn("could not remove", err.getvalue())
        self.assertIn(str(self.parent), err.getvalue())


class TheRealPathConsultsTheTracker(unittest.TestCase):
    """The refusal was tested only against `can_file` in isolation and against a mocked
    `file_children`. Neither proves the command an operator runs consults the tracker at
    all — delete the wiring and both keep passing, while a non-GitHub instance silently
    files nothing and reports success."""

    def _cfg(self, tmp: Path, **over) -> Config:
        base = dict(tracker_system="github",
                    tracker_url="https://github.com/acme/widgets")
        base.update(over)
        return Config(
            root=tmp, bundle_root=tmp / "results", process_dir=tmp / "process",
            templates_dir=TEMPLATES, default_branch="main", issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"), **base)

    def _bundle(self, cfg: Config) -> Path:
        parent = cfg.bundle("500")
        parent.mkdir(parents=True)
        (parent / "brief.md").write_text("- **Slug:** parent\n", encoding="utf-8")
        (parent / split.PROPOSAL).write_text(_proposal(_ONE, _TWO_DEP), encoding="utf-8")
        return parent

    def _run(self, cfg: Config, *, gh: bool = True) -> tuple[int, str]:
        ran = []

        def never(cmd, **kw):
            ran.append(cmd)
            raise AssertionError("gh was invoked despite an unusable tracker")

        err = io.StringIO()
        with mock.patch("pdca_harness.split.subprocess", SimpleNamespace(run=never)), \
             mock.patch("pdca_harness.split.shutil.which",
                        return_value="/usr/bin/gh" if gh else None), \
             redirect_stderr(err), redirect_stdout(io.StringIO()):
            rc = cli._split(cfg, SimpleNamespace(issue_id="500", accept=True, ids=""))
        self.assertEqual(ran, [])
        return rc, err.getvalue()

    def test_a_non_github_tracker_refuses_through_the_real_command(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            cfg = self._cfg(tmp, tracker_system="gitlab")
            parent = self._bundle(cfg)
            rc, err = self._run(cfg)
            self.assertEqual(rc, 1)
            self.assertIn("not GitHub", err)
            self.assertIn("--accept --ids", err)
            self.assertFalse(cfg.bundle("601").exists())
            self.assertFalse((parent / state.CLOSE_MARKER).exists(),
                             "the parent was marked split although nothing was created")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_missing_gh_refuses_through_the_real_command(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            cfg = self._cfg(tmp)
            self._bundle(cfg)
            rc, err = self._run(cfg, gh=False)
            self.assertEqual(rc, 1)
            self.assertIn("`gh` is not on PATH", err)
            self.assertFalse(cfg.bundle("601").exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_an_unknown_repository_refuses_through_the_real_command(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            cfg = self._cfg(tmp, tracker_url="")
            self._bundle(cfg)
            rc, err = self._run(cfg)
            self.assertEqual(rc, 1)
            self.assertIn("repository could not be determined", err)
            self.assertFalse(cfg.bundle("601").exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_atx_closing_hashes_are_not_part_of_the_title(self) -> None:
        """`# Extract the parser ##` is valid ATX and renders as "Extract the parser"
        everywhere else, so carrying the hashes through would put raw markup in the title
        of a real tracker item."""
        parent = Path(tempfile.mkdtemp())
        for heading, want in (("# Extract the parser ##", "Extract the parser"),
                              ("## Split the reader ######", "Split the reader"),
                              ("# Keep this # inside", "Keep this # inside"),
                              ("# Plain", "Plain")):
            with self.subTest(heading=heading):
                child = split.parse(_proposal(f"{heading}\n\n- **Slug:** s\n"))[0]
                self.assertEqual(split.child_title(child, parent), want)

    def test_a_blank_slug_still_yields_a_usable_title(self) -> None:
        """`.+?` matches a space, so `- **Slug:**` with nothing after it captured one,
        `.strip()` emptied it, and `child_title` returned "" — against its own docstring.
        `gh issue create --title ""` fails, aborting the batch for an untitled child."""
        tmp = Path(tempfile.mkdtemp())
        try:
            cfg = self._cfg(tmp)
            parent = cfg.bundle("500")
            parent.mkdir(parents=True)
            for body in ("- **Slug:**   \n", "- slug: \n", "- **Slug:** <fill me>\n",
                         "- **Defect / goal:** a\n"):
                with self.subTest(body=body.strip()):
                    child = split.parse(_proposal(body))[0]
                    title = split.child_title(child, parent)
                    self.assertTrue(title.strip(),
                                    f"empty title for {body!r} — gh would reject it")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class IrreversibleStateIsNeverLost(unittest.TestCase):
    """Two paths where real tracker issues existed and the operator was told otherwise."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process", templates_dir=TEMPLATES,
            default_branch="main", tracker_system="github",
            tracker_url="https://github.com/acme/widgets", issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"))
        self.parent = self.cfg.bundle("500")
        self.parent.mkdir(parents=True)
        (self.parent / "brief.md").write_text("- **Slug:** parent\n", encoding="utf-8")
        self.children = split.parse(_proposal(_ONE, _TWO_DEP))
        self.n = 0

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _with(self, run):
        return mock.patch.multiple(
            "pdca_harness.split",
            subprocess=SimpleNamespace(run=run),
            shutil=SimpleNamespace(which=lambda _n: "/usr/bin/gh",
                                   rmtree=shutil.rmtree, move=shutil.move))

    def test_a_zero_exit_with_no_url_warns_that_the_issue_MAY_exist(self) -> None:
        """`gh` succeeded; only its number is unreadable. Telling the operator to file
        that child by hand invites a duplicate against a tracker that can undo neither."""
        def run(cmd, capture_output=False, text=False, cwd=None):
            self.n += 1
            if self.n == 2:
                return SimpleNamespace(returncode=0, stdout="created ok\n", stderr="")
            return SimpleNamespace(
                returncode=0, stderr="",
                stdout="https://github.com/acme/widgets/issues/601\n")

        with self._with(run):
            with self.assertRaises(split.SplitError) as caught:
                split.file_children(self.parent, self.children, self.cfg)
        msg = str(caught.exception)
        self.assertIn("#601", msg)
        self.assertIn("MAY ALSO HAVE BEEN FILED", msg)
        self.assertIn("duplicate", msg)

    def test_an_ordinary_failure_does_NOT_claim_the_issue_may_exist(self) -> None:
        """The complement: a call that genuinely failed filed nothing, and hedging there
        would stop an operator from retrying something that is safe to retry."""
        def run(cmd, capture_output=False, text=False, cwd=None):
            self.n += 1
            if self.n == 2:
                return SimpleNamespace(returncode=1, stdout="", stderr="gh: HTTP 403")
            return SimpleNamespace(
                returncode=0, stderr="",
                stdout="https://github.com/acme/widgets/issues/601\n")

        with self._with(run):
            with self.assertRaises(split.SplitError) as caught:
                split.file_children(self.parent, self.children, self.cfg)
        msg = str(caught.exception)
        self.assertNotIn("MAY ALSO HAVE BEEN FILED", msg)
        # …but it does not claim certainty either. `gh` can time out or lose the response
        # after GitHub has already committed the issue, and this code cannot tell that
        # apart from an outright rejection.
        self.assertIn("check the tracker before filing it by hand", msg)

    def test_ctrl_c_still_reports_what_was_already_filed(self) -> None:
        """`KeyboardInterrupt` is not an `Exception`, so it walked past the handler and
        the irreversible numbers vanished with nothing on screen naming them."""
        def run(cmd, capture_output=False, text=False, cwd=None):
            self.n += 1
            if self.n == 2:
                raise KeyboardInterrupt
            return SimpleNamespace(
                returncode=0, stderr="",
                stdout="https://github.com/acme/widgets/issues/601\n")

        err = io.StringIO()
        with self._with(run), redirect_stderr(err):
            with self.assertRaises(KeyboardInterrupt):
                split.file_children(self.parent, self.children, self.cfg)
        out = err.getvalue()
        self.assertIn("#601", out)
        self.assertIn("cannot be rolled back", out)

    def test_ctrl_c_stays_an_interrupt(self) -> None:
        """Reported, then re-raised unchanged: converting it to a SplitError would make
        Ctrl-C look like an ordinary error the caller might handle and continue past."""
        def run(cmd, capture_output=False, text=False, cwd=None):
            raise KeyboardInterrupt

        with self._with(run), redirect_stderr(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                split.file_children(self.parent, self.children, self.cfg)


if __name__ == "__main__":
    unittest.main()
