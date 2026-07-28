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
from pathlib import Path

from pdca_harness import driver, leaves, split, state, waves
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
        `pdca split` session was told the opposite of its own role. Scanning only the
        role files missed it — the prompt actually sent to the model is built in code."""
        import inspect
        from pdca_harness import leaves
        for fn in (leaves._split_prompt, leaves._sizer_prompt):
            with self.subTest(prompt=fn.__name__):
                self.assertNotIn("call at sign-off", inspect.getsource(fn))
                self.assertNotIn("splitting is the human's call at sign-off",
                                 inspect.getsource(fn))

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


if __name__ == "__main__":
    unittest.main()
