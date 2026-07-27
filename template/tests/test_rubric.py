"""The target repo's review rubric reaches all three model leaves (issue #314).

The asymmetry this removes: the builder generates without ever seeing the criteria the
reviewer applies, so convention violations ship and come back as findings — a guaranteed
review round for something the builder could have fixed before emitting.

Three consumers, not two: builder, Check reviewer, AND adversary. Feeding only two would
just move the asymmetry from builder-vs-reviewer to reviewer-vs-adversary.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pdca_harness import leaves, rubric
from pdca_harness.config import Config, LeafConfig

_RUBRIC = ("# Review rubric & protocol\n\n"
           "- Never use `unwrap()` in library code.\n"
           "- REJECTED as noise: naming bikesheds, import ordering.\n")


class RubricLoading(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.target = self.tmp / "target"
        (self.target / "docs").mkdir(parents=True)
        self.d = self.tmp / "results" / "issue_1"
        self.d.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cfg(self, rubric_file: str = "", section: str = "") -> Config:
        cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process", templates_dir=self.tmp / "templates",
            default_branch="main", tracker_system="github", tracker_url="",
            issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"),
        )
        cfg.rubric_file = rubric_file
        cfg.rubric_section = section
        return cfg

    def _with_target(self, cfg):
        """Pin target resolution — the brief's repo target is publish's business, not this
        module's, and resolving it for real would drag a git checkout into the test."""
        return mock.patch.object(leaves.rubric_mod, "worktree", None), cfg

    def _load(self, cfg) -> str:
        with mock.patch("pdca_harness.worktree._target", return_value=(self.target, "main")):
            return rubric.load(self.d, cfg)

    # -- unset is byte-identical -------------------------------------------------------

    def test_unset_yields_nothing(self) -> None:
        self.assertEqual(self._load(self._cfg()), "")
        self.assertEqual(rubric.for_builder(self.d, self._cfg()), "")
        self.assertEqual(rubric.for_reviewer(self.d, self._cfg()), "")

    # -- reading ------------------------------------------------------------------------

    def test_whole_file(self) -> None:
        (self.target / "RUBRIC.md").write_text(_RUBRIC, encoding="utf-8")
        self.assertIn("unwrap()", self._load(self._cfg("RUBRIC.md")))

    def test_one_section_of_a_larger_file(self) -> None:
        """The common shape: the rubric is a heading inside the host's AGENTS.md, and
        feeding the whole file would bury it in unrelated project context."""
        (self.target / "AGENTS.md").write_text(
            "# Project\n\nunrelated context\n\n" + _RUBRIC + "\n# Build\n\nmore prose\n",
            encoding="utf-8")
        text = self._load(self._cfg("AGENTS.md", "Review rubric"))
        self.assertIn("unwrap()", text)
        self.assertNotIn("unrelated context", text)
        self.assertNotIn("more prose", text, "the section ran past its next sibling heading")

    # -- fail-open ----------------------------------------------------------------------

    def test_missing_file_degrades_to_nothing(self) -> None:
        """A broken rubric path must never stop a build."""
        self.assertEqual(self._load(self._cfg("nope.md")), "")

    def test_missing_section_degrades_to_nothing(self) -> None:
        (self.target / "AGENTS.md").write_text("# Project\n\nprose\n", encoding="utf-8")
        self.assertEqual(self._load(self._cfg("AGENTS.md", "Review rubric")), "")

    def test_paths_escaping_the_target_are_refused(self) -> None:
        (self.tmp / "secret.md").write_text("not yours\n", encoding="utf-8")
        for rel in ("../secret.md", "docs/../../secret.md", str(self.tmp / "secret.md")):
            with self.subTest(rel=rel):
                self.assertEqual(self._load(self._cfg(rel)), "",
                                 f"{rel} resolved outside the target checkout")

    # -- the snapshot -------------------------------------------------------------------

    def test_first_read_snapshots_into_the_bundle(self) -> None:
        (self.target / "RUBRIC.md").write_text(_RUBRIC, encoding="utf-8")
        self._load(self._cfg("RUBRIC.md"))
        self.assertTrue((self.d / rubric.SNAPSHOT).exists())

    def test_later_readers_get_the_snapshot_not_the_moved_target(self) -> None:
        """"One artifact, both sides, no drift" is not achieved by re-reading a live file.

        The builder reads at Do and the reviewers at Check; the target can change in
        between — including because of this very cycle — and each leaf would then be
        judged against a different contract.
        """
        (self.target / "RUBRIC.md").write_text(_RUBRIC, encoding="utf-8")
        first = self._load(self._cfg("RUBRIC.md"))
        (self.target / "RUBRIC.md").write_text("# Review rubric\n\nTOTALLY DIFFERENT\n",
                                               encoding="utf-8")
        self.assertEqual(self._load(self._cfg("RUBRIC.md")), first,
                         "a later leaf saw a different rubric than the builder did")

    def test_snapshot_is_archived_with_its_attempt(self) -> None:
        """An iterate must re-snapshot: a rubric that changed between attempts SHOULD
        apply to the next one."""
        from pdca_harness import driver
        self.assertIn(rubric.SNAPSHOT, driver.DOWNSTREAM_OF_BRIEF)

    # -- the three consumers ------------------------------------------------------------

    def test_builder_block_demands_self_review(self) -> None:
        """Seeing the criteria is not the point; applying them before emitting is."""
        (self.target / "RUBRIC.md").write_text(_RUBRIC, encoding="utf-8")
        with mock.patch("pdca_harness.worktree._target", return_value=(self.target, "main")):
            block = rubric.for_builder(self.d, self._cfg("RUBRIC.md"))
        self.assertIn("re-read your own diff", block)
        self.assertIn("unwrap()", block)

    def test_reviewer_block_names_the_rejected_classes(self) -> None:
        (self.target / "RUBRIC.md").write_text(_RUBRIC, encoding="utf-8")
        with mock.patch("pdca_harness.worktree._target", return_value=(self.target, "main")):
            block = rubric.for_reviewer(self.d, self._cfg("RUBRIC.md"))
        self.assertIn("do not raise them", block)
        self.assertIn("REJECTED as noise", block)

    def test_all_three_leaves_receive_the_same_bytes(self) -> None:
        """One artifact, three consumers — the property the issue is named for."""
        (self.target / "RUBRIC.md").write_text(_RUBRIC, encoding="utf-8")
        cfg = self._cfg("RUBRIC.md")
        with mock.patch("pdca_harness.worktree._target", return_value=(self.target, "main")):
            builder = rubric.for_builder(self.d, cfg)
            reviewer = rubric.for_reviewer(self.d, cfg)
            adversary = leaves._advisory_prompt({}, "adversary", rubric.for_reviewer(self.d, cfg))
        for name, block in (("builder", builder), ("reviewer", reviewer),
                            ("adversary", adversary)):
            with self.subTest(leaf=name):
                self.assertIn("unwrap()", block, f"the {name} prompt carries no rubric")

    def test_prompts_are_unchanged_when_unset(self) -> None:
        """Byte-identical with no rubric configured — the property every instance that
        never sets the key depends on."""
        cfg = self._cfg()
        self.assertEqual(leaves._advisory_prompt({}, "adversary", ""),
                         leaves._advisory_prompt({}, "adversary"))
        self.assertNotIn("standing review rubric", leaves._build_prompt(self.d, cfg))




class ReviewFixes(unittest.TestCase):
    """Regressions from the codex review of #352."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.target = self.tmp / "target"
        self.target.mkdir(parents=True)
        self.d = self.tmp / "results" / "issue_1"
        self.d.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cfg(self, rel="RUBRIC.md", section=""):
        cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process", templates_dir=self.tmp / "templates",
            default_branch="main", tracker_system="github", tracker_url="",
            issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"))
        cfg.rubric_file, cfg.rubric_section = rel, section
        return cfg

    def _load(self, cfg):
        with mock.patch("pdca_harness.rubric._target_root", return_value=self.target):
            return rubric.load(self.d, cfg)

    def test_non_utf8_rubric_fails_open(self) -> None:
        """UnicodeDecodeError is not an OSError — it would have aborted the Do beat."""
        (self.target / "RUBRIC.md").write_bytes(b"\xff\xfe not utf-8 \x00")
        self.assertEqual(self._load(self._cfg()), "")

    def test_a_fenced_example_heading_is_not_mistaken_for_the_section(self) -> None:
        (self.target / "AGENTS.md").write_text(
            "# Project\n\n```md\n## Review rubric\n\nEXAMPLE ONLY\n```\n\n"
            "## Review rubric\n\nTHE REAL RULES\n", encoding="utf-8")
        text = self._load(self._cfg("AGENTS.md", "Review rubric"))
        self.assertIn("THE REAL RULES", text)
        self.assertNotIn("EXAMPLE ONLY", text)

    def test_an_absent_rubric_is_snapshotted_too(self) -> None:
        """Otherwise the drift window stays open the other way: the builder finds nothing,
        the target then GAINS the file, and the reviewer is handed rules the builder never
        saw."""
        self.assertEqual(self._load(self._cfg()), "")
        self.assertTrue((self.d / rubric.SNAPSHOT).exists(),
                        "the empty outcome was not pinned for later leaves")
        (self.target / "RUBRIC.md").write_text("# R\n\nappeared later\n", encoding="utf-8")
        self.assertEqual(self._load(self._cfg()), "",
                         "a later leaf picked up a rubric the builder never saw")


class SecondReviewFixes(unittest.TestCase):
    """Regressions from the second codex pass on #352."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.target = self.tmp / "target"
        self.target.mkdir(parents=True)
        self.d = self.tmp / "results" / "issue_1"
        self.d.mkdir(parents=True)
        (self.d / "brief.md").write_text("- **Slug:** s\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cfg(self, rel="", section=""):
        cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process", templates_dir=self.tmp / "templates",
            default_branch="main", tracker_system="github", tracker_url="",
            issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"))
        cfg.rubric_file, cfg.rubric_section = rel, section
        return cfg

    def _load(self, cfg):
        with mock.patch("pdca_harness.rubric._target_root", return_value=self.target):
            return rubric.load(self.d, cfg)

    def test_a_backtick_fence_is_not_closed_by_a_tilde_line(self) -> None:
        """A ``` example quoting a ~~~ line would close the fence early, exposing the
        example's heading and returning the sample instead of the real rubric."""
        (self.target / "A.md").write_text(
            "# P\n\n```md\n~~~\n## Review rubric\n\nEXAMPLE ONLY\n```\n\n"
            "## Review rubric\n\nTHE REAL RULES\n", encoding="utf-8")
        text = self._load(self._cfg("A.md", "Review rubric"))
        self.assertIn("THE REAL RULES", text)
        self.assertNotIn("EXAMPLE ONLY", text)

    def test_an_indented_heading_is_still_a_heading(self) -> None:
        """One to three spaces is valid ATX Markdown; a stricter anchor degraded the
        section to no rubric at all."""
        (self.target / "A.md").write_text(
            "# P\n\n  ## Review rubric\n\n- INDENTED RULE\n", encoding="utf-8")
        self.assertIn("INDENTED RULE", self._load(self._cfg("A.md", "Review rubric")))

    def test_the_unconfigured_outcome_is_pinned_too(self) -> None:
        """Unset at Do, set before Check: the reviewer would otherwise apply a rubric the
        builder never received. Config changes take effect on the NEXT attempt."""
        self.assertEqual(self._load(self._cfg()), "")
        self.assertTrue((self.d / rubric.SNAPSHOT).exists())
        (self.target / "R.md").write_text("# R\n\nturned on later\n", encoding="utf-8")
        self.assertEqual(self._load(self._cfg("R.md")), "",
                         "a mid-cycle config change reached a later leaf")

    def test_a_brief_with_no_target_reads_no_rubric(self) -> None:
        """`_checkout_path(cfg, "")` resolves to cfg.root.parent, so an empty repo spec
        would read a rubric out of an unrelated sibling directory."""
        from pdca_harness import rubric as r
        with mock.patch("pdca_harness.worktree.path", return_value=None), \
             mock.patch("pdca_harness.worktree._target", return_value=None), \
             mock.patch("pdca_harness.publish._resolve_target", return_value=("", "", "")):
            self.assertIsNone(r._target_root(self.d, self._cfg("R.md")))

    def test_a_lane_owned_by_another_bundle_is_not_used(self) -> None:
        """An overflow gate can leave a lane preserved for a different bundle; `path()`
        returns it because the directory exists."""
        from pdca_harness import rubric as r
        lane = self.tmp / "lane"
        lane.mkdir()
        with mock.patch("pdca_harness.worktree.path", return_value=lane), \
             mock.patch("pdca_harness.worktree.owner_of", return_value="issue_999"), \
             mock.patch("pdca_harness.worktree._target", return_value=(self.target, "main")):
            self.assertEqual(r._target_root(self.d, self._cfg("R.md")), self.target)

    def test_the_builder_prompt_and_rubric_do_not_run_together(self) -> None:
        """`for_builder()` has no trailing whitespace, so prefixing it glued its last rule
        onto `You are the Do builder…` — merging two instructions into one line."""
        (self.target / "R.md").write_text("# R\n\n- LAST RULE\n", encoding="utf-8")
        with mock.patch("pdca_harness.rubric._target_root", return_value=self.target):
            prompt = leaves._build_prompt(self.d, self._cfg("R.md"))
        self.assertNotIn("LAST RULEYou are", prompt)
        self.assertTrue(prompt.startswith("You are the Do builder"),
                        "the task prompt should frame the work; the rubric constrains it")
        self.assertIn("LAST RULE", prompt)


class ThirdReviewFixes(unittest.TestCase):
    """Round three on #352."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.target = self.tmp / "target"
        self.target.mkdir(parents=True)
        self.d = self.tmp / "results" / "issue_1"
        self.d.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cfg(self, rel="A.md", section="Review rubric"):
        cfg = Config(
            root=self.tmp, bundle_root=self.tmp / "results",
            process_dir=self.tmp / "process", templates_dir=self.tmp / "templates",
            default_branch="main", tracker_system="github", tracker_url="",
            issue_id_example="#1",
            builder=LeafConfig(mode="stub"), reviewer=LeafConfig(mode="stub"))
        cfg.rubric_file, cfg.rubric_section = rel, section
        return cfg

    def _load(self, cfg):
        with mock.patch("pdca_harness.rubric._target_root", return_value=self.target):
            return rubric.load(self.d, cfg)

    def test_an_info_string_does_not_close_a_fence(self) -> None:
        """A closer may carry only trailing whitespace: ```python inside an open backtick
        fence is a nested example's info string, not a close."""
        (self.target / "A.md").write_text(
            "# P\n\n````md\n```python\n## Review rubric\n\nEXAMPLE ONLY\n````\n\n"
            "## Review rubric\n\nTHE REAL RULES\n", encoding="utf-8")
        text = self._load(self._cfg())
        self.assertIn("THE REAL RULES", text)
        self.assertNotIn("EXAMPLE ONLY", text)

    def test_the_section_name_must_match_from_the_start(self) -> None:
        """A substring test selects `## Historical review rubric` for a configured
        `Review rubric`, handing every leaf an obsolete section while the real one is
        never read."""
        (self.target / "A.md").write_text(
            "# P\n\n## Historical review rubric\n\nOBSOLETE\n\n"
            "## Review rubric & protocol\n\nTHE REAL RULES\n", encoding="utf-8")
        text = self._load(self._cfg())
        self.assertIn("THE REAL RULES", text)
        self.assertNotIn("OBSOLETE", text)

    def test_a_prefix_of_the_heading_still_matches(self) -> None:
        """The real-world shape: the configured name omits a trailing qualifier."""
        (self.target / "A.md").write_text(
            "# P\n\n## Review rubric & protocol\n\nRULES\n", encoding="utf-8")
        self.assertIn("RULES", self._load(self._cfg()))

    def test_a_stale_lane_is_not_used_when_setup_fell_back(self) -> None:
        """`worktree.ensure()` can fail AFTER creating the lane and leave its owner stamp,
        so `_do_build_command` runs in place while an ownership check would still prefer
        the lane. The builder passes what ensure() actually returned — including None."""
        lane = self.tmp / "lane"
        lane.mkdir()
        with mock.patch("pdca_harness.worktree.path", return_value=lane), \
             mock.patch("pdca_harness.worktree.owner_of", return_value="issue_1"), \
             mock.patch("pdca_harness.worktree._target",
                        return_value=(self.target, "main")):
            # The builder path: ensure() returned None, so the lane must be ignored.
            self.assertEqual(
                rubric._target_root(self.d, self._cfg(), None), self.target)
            # …and a live worktree wins outright.
            self.assertEqual(
                rubric._target_root(self.d, self._cfg(), lane), lane)


class FourthReviewFixes(unittest.TestCase):
    """Round four on #352 — both findings in `_section`, as in rounds two and three."""

    def test_a_commented_out_section_is_not_selected(self) -> None:
        """A commented-out draft is a realistic thing to find in an AGENTS.md, and its
        heading is not structural Markdown. Selecting it hands every leaf rules the author
        had explicitly switched off."""
        text = ("# P\n\n<!--\n## Review rubric\n\nCOMMENTED DRAFT\n-->\n\n"
                "## Review rubric\n\nTHE REAL RULES\n")
        got = rubric._section(text, "Review rubric")
        self.assertIn("THE REAL RULES", got)
        self.assertNotIn("COMMENTED DRAFT", got)

    def test_a_single_line_comment_does_not_open_a_block(self) -> None:
        text = "# P\n\n<!-- ## Review rubric -->\n\n## Review rubric\n\nREAL\n"
        self.assertIn("REAL", rubric._section(text, "Review rubric"))

    def test_a_comment_marker_inside_a_fence_is_inert(self) -> None:
        """Fence state is checked first, so a `<!--` quoted in an example cannot swallow
        the rest of the file."""
        text = "# P\n\n```md\n<!--\n```\n\n## Review rubric\n\nREAL\n"
        self.assertIn("REAL", rubric._section(text, "Review rubric"))

    def test_setext_headings_are_deliberately_unsupported(self) -> None:
        """ATX only, by decision rather than oversight — see `_section`'s docstring.

        The safe direction: an unrecognised section yields NO rubric and a warning naming
        Setext, rather than a partial or wrong one. A rubric the author did not intend is
        worse than none.
        """
        text = "# P\n\nReview rubric\n-------------\n\nSETEXT RULES\n"
        self.assertEqual(rubric._section(text, "Review rubric"), "")


if __name__ == "__main__":
    unittest.main()
