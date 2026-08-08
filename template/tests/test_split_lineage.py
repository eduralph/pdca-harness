"""`split-lineage.json` — the on-disk provenance record for `pdca split --accept`
(issue #456).

Before this, `materialise` wrote only a child's `brief.md` and the "Child slice of #N"
breadcrumb went solely into the filed tracker issue's body — nothing on disk
distinguished a split child from a fresh oversized brief. This module proves the missing
provenance: each child bundle records its `parent` / `siblings` / `depth`, the parent
bundle gains `children` *without losing its own edges*, one tolerant reader returns
either or `None` — for EVERY way a record can be unreadable, including bytes that are
not UTF-8 and a `depth` that is not a number, checked through `accept` itself and not
only through the reader — and the whole thing rides inside the accept's existing
transactional discipline.

Per the brief: import the MODULE, never the new symbols, so a red run (production hunks
reverted) fails with a real `AttributeError` on `split.<name>` rather than an
`ImportError` that would exit the verifier as PDCA-UNVERIFIABLE.
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from pdca_harness import split, state
from pdca_harness.config import Config, LeafConfig

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


def _proposal(*children: str, version: int = 1) -> str:
    body = f"<!-- pdca:split-proposal v{version} -->\n# Split proposal\n\n"
    for i, child in enumerate(children, 1):
        body += (f"<!-- pdca:child child-{i} -->\n{child}\n"
                 f"<!-- pdca:end child-{i} -->\n\n")
    return body


_ONE = "- **Slug:** first\n- **Defect / goal:** a\n"
_TWO = "- **Slug:** second\n- **Defect / goal:** b\n"
_THREE = "- **Slug:** third\n- **Defect / goal:** c\n"


class LineageBase(unittest.TestCase):
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

    def _seed_child_record(self, **edges) -> bytes:
        """Make the parent itself a split child, and return the exact bytes on disk."""
        record = {"version": 1, "id": "500", **edges}
        path = self.parent / split.LINEAGE
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        return path.read_bytes()


class LineageOnAccept(LineageBase):
    # -- item 1: each child names its parent, siblings, depth --------------------------

    def test_each_child_records_parent_siblings_and_depth(self) -> None:
        self._write(_proposal(_ONE, _TWO, _THREE))
        created = split.accept(self.parent, ["601", "602", "603"], self.cfg)

        rec_601 = split.read_lineage(created[0])
        self.assertIsNotNone(rec_601, "child 601 has no readable lineage record")
        self.assertEqual(rec_601["parent"], "500")
        self.assertEqual(sorted(rec_601["siblings"]), ["602", "603"])
        self.assertEqual(rec_601["depth"], 1)

        rec_602 = split.read_lineage(created[1])
        self.assertEqual(sorted(rec_602["siblings"]), ["601", "603"])

    # -- item 2: the parent names its children -----------------------------------------

    def test_parent_records_its_children(self) -> None:
        self._write(_proposal(_ONE, _TWO))
        split.accept(self.parent, ["601", "602"], self.cfg)
        rec = split.read_lineage(self.parent)
        self.assertIsNotNone(rec, "parent has no readable lineage record")
        self.assertEqual(rec["children"], ["601", "602"])

    # -- item 3: the mixed-role case — THE defect this slice exists to prevent ----------

    def test_a_parent_that_is_itself_a_child_keeps_both_edges(self) -> None:
        """A `role`-discriminated record overwrote the child record with a parent one and
        kept only `depth`, so a depth-1 bundle silently lost its sibling set. Asserting
        that `depth` survived blesses the loss; this asserts `parent` and `siblings`
        survive too, alongside the new `children` edge."""
        self._seed_child_record(parent="100", siblings=["501", "502"], depth=1)
        self._write(_proposal(_ONE, _TWO))

        split.accept(self.parent, ["601", "602"], self.cfg)

        rec = split.read_lineage(self.parent)
        self.assertEqual(rec["parent"], "100",
                         "the parent's OWN parent edge did not survive")
        self.assertEqual(rec["siblings"], ["501", "502"],
                         "the parent's OWN sibling edge did not survive")
        self.assertEqual(rec["depth"], 1, "the parent's OWN depth did not survive")
        self.assertEqual(rec["children"], ["601", "602"],
                         "the new children edge was not recorded")

    def test_depth_accumulates_through_a_mixed_role_parent(self) -> None:
        """A grandchild's depth is the parent's depth + 1, not a flat 1 — recursion depth
        is recorded at the moment it is known, without anyone counting."""
        self._seed_child_record(parent="100", siblings=[], depth=1)
        self._write(_proposal(_ONE, _TWO))
        created = split.accept(self.parent, ["601", "602"], self.cfg)
        self.assertEqual(split.read_lineage(created[0])["depth"], 2)

    # -- item 4: the tolerant reader ---------------------------------------------------

    def test_reader_returns_none_when_absent(self) -> None:
        self.assertIsNone(split.read_lineage(self.parent))

    def test_reader_returns_none_when_unreadable(self) -> None:
        # A directory in the record's place: read_text() raises OSError, not a parse error.
        (self.parent / split.LINEAGE).mkdir()
        self.assertIsNone(split.read_lineage(self.parent))

    def test_reader_returns_none_when_malformed(self) -> None:
        (self.parent / split.LINEAGE).write_text("{not json", encoding="utf-8")
        self.assertIsNone(split.read_lineage(self.parent))

    def test_reader_returns_none_when_not_an_object(self) -> None:
        (self.parent / split.LINEAGE).write_text("[1, 2, 3]", encoding="utf-8")
        self.assertIsNone(split.read_lineage(self.parent))

    def test_reader_returns_none_on_wrong_version(self) -> None:
        (self.parent / split.LINEAGE).write_text(
            json.dumps({"version": 99, "id": "500", "children": ["601"]}),
            encoding="utf-8")
        self.assertIsNone(split.read_lineage(self.parent))

    def test_reader_returns_none_on_bytes_that_are_not_utf8(self) -> None:
        """Not a hypothetical: a truncated write, a `latin-1` hand-edit or a half-synced
        file leaves bytes that are not UTF-8, and the decode raises `UnicodeDecodeError`
        from the READ — not from `json.loads` — so a handler written for `OSError` on the
        read and `ValueError` on the parse catches neither, and the reader that promised to
        abstain crashes its caller instead."""
        (self.parent / split.LINEAGE).write_bytes(b'{"version": 1, "id": "\xff\xfe500"}')
        self.assertIsNone(split.read_lineage(self.parent))

    def test_reader_returns_none_on_a_pathologically_nested_payload(self) -> None:
        """The other type an enumerated handler misses: deep nesting exhausts the parser's
        stack and raises `RecursionError`, which is a `RuntimeError` — not a `ValueError`,
        so "malformed JSON" as a list of expected exception types does not cover it. The
        contract is total abstention, and this is what makes the total catch load-bearing
        rather than decorative."""
        (self.parent / split.LINEAGE).write_text("[" * 60000 + "]" * 60000,
                                                 encoding="utf-8")
        self.assertIsNone(split.read_lineage(self.parent))

    def test_reader_never_raises_on_any_of_the_above(self) -> None:
        """The reader must abstain, never throw into a beat."""
        lineage_path = self.parent / split.LINEAGE

        def use_absent() -> None:
            pass

        def use_directory() -> None:
            lineage_path.mkdir()

        def use_malformed() -> None:
            lineage_path.write_text("{bad", encoding="utf-8")

        def use_non_utf8_bytes() -> None:
            lineage_path.write_bytes(b'\xff\xfe{"version": 1}')

        def use_deep_nesting() -> None:
            lineage_path.write_text("[" * 60000 + "]" * 60000, encoding="utf-8")

        for setup in (use_absent, use_directory, use_malformed,
                      use_non_utf8_bytes, use_deep_nesting):
            with self.subTest(setup=setup.__name__):
                if lineage_path.is_dir():
                    shutil.rmtree(lineage_path)
                elif lineage_path.exists():
                    lineage_path.unlink()
                setup()
                try:
                    split.read_lineage(self.parent)
                except Exception as exc:  # pragma: no cover - the assertion IS the point
                    self.fail(f"read_lineage raised {exc!r} instead of returning None")

    # -- item 4, from the PRODUCTION consumer's side ------------------------------------
    #
    # "The reader returns None" is only half the contract; the half that matters is
    # "…and every consumer behaves exactly as today". `accept` is the one consumer this
    # slice ships, so these drive a corrupt record through the real `split.accept` rather
    # than through the reader alone — a probe on the reader can be satisfied while the
    # beat still dies one frame up.

    def test_accept_survives_a_parent_record_that_is_not_utf8(self) -> None:
        """The corrupt record reaches `accept` through `materialise` and the merge. If the
        reader throws, the exception escapes the split — the parent keeps a record it can
        never replace, and no amount of retrying gets past it. Abstaining, the accept
        completes and REPLACES the unreadable bytes with a valid record: an unparseable
        file carries no edges any consumer could have read, so there is nothing to merge
        and nothing to preserve, and refusing the split over it would hand a corrupt hint
        the power to block the beat — the same failure the tolerant reader exists to
        prevent, one level up. (A record that cannot be READ AT ALL is different: `accept`
        refuses that up front, because it could not restore it on rollback.)"""
        (self.parent / split.LINEAGE).write_bytes(b'{"version": 1, "id": "\xff\xfe500"}')
        self._write(_proposal(_ONE, _TWO))

        created = split.accept(self.parent, ["601", "602"], self.cfg)

        rec = split.read_lineage(self.parent)
        self.assertIsNotNone(rec, "the parent was left without a readable record")
        self.assertEqual(rec["children"], ["601", "602"])
        self.assertEqual(split.read_lineage(created[0])["depth"], 1)
        self.assertTrue((self.parent / state.CLOSE_MARKER).exists(),
                        "the accept did not complete")

    def test_accept_survives_a_parent_whose_recorded_depth_is_not_a_number(self) -> None:
        """Tolerating the FILE but not its VALUES only moves the throw one line down:
        `{"depth": "one"}` parses fine, so the reader hands it back and `depth + 1` raises
        `TypeError` from inside `accept`. The child's depth falls back to 1 (unknown, not
        guessed), and the parent's own record is copied through verbatim rather than
        silently rewritten to something this run invented."""
        self._seed_child_record(parent="100", siblings=["501"], depth="one")
        self._write(_proposal(_ONE, _TWO))

        created = split.accept(self.parent, ["601", "602"], self.cfg)

        self.assertEqual(split.read_lineage(created[0])["depth"], 1)
        rec = split.read_lineage(self.parent)
        self.assertEqual(rec["parent"], "100")
        self.assertEqual(rec["siblings"], ["501"])
        self.assertEqual(rec["depth"], "one",
                         "the operator's own value was rewritten rather than left alone")
        self.assertEqual(rec["children"], ["601", "602"])

    # -- item 5: lineage is provenance, not attempt output ------------------------------

    def test_lineage_filename_is_not_in_downstream_of_brief(self) -> None:
        self.assertNotIn(split.LINEAGE, state.DOWNSTREAM_OF_BRIEF,
                         "the lineage record must survive iterate-plan's archive of a "
                         "rejected attempt — it is provenance, not attempt output")

    def test_the_record_survives_the_archive_accept_itself_performs(self) -> None:
        """`accept` archives the parent's abandoned attempt (everything in
        DOWNSTREAM_OF_BRIEF) before writing its own record. A lineage file already there —
        the parent is itself a split child — must come through that archive untouched, not
        be swept into `iteration-v1/`."""
        self._seed_child_record(parent="100", siblings=["501"], depth=1)
        (self.parent / "patch.diff").write_text("--- a\n+++ b\n", encoding="utf-8")
        self._write(_proposal(_ONE, _TWO))

        split.accept(self.parent, ["601", "602"], self.cfg)

        self.assertTrue((self.parent / "iteration-v1" / "patch.diff").exists(),
                        "the abandoned attempt was not archived — fixture is not exercising "
                        "the archive path")
        self.assertFalse((self.parent / "iteration-v1" / split.LINEAGE).exists(),
                         "the lineage record was archived away with the attempt")
        rec = split.read_lineage(self.parent)
        self.assertEqual(rec["parent"], "100")
        self.assertEqual(rec["children"], ["601", "602"])


class LineageIsTransactional(LineageBase):
    """Item 6 — the record rides inside `accept`'s existing all-or-nothing discipline."""

    @staticmethod
    def _write_text_failing_on(name: str, *, land_first: bool = False):
        """A `Path.write_text` that raises for one file — a full disk, mid-accept.

        ``land_first`` writes the bytes and *then* raises, the case where the file that
        broke the accept is nonetheless on disk.
        """
        real = Path.write_text

        def failing(self, *a, **kw):
            if self.name == name:
                if land_first:
                    real(self, *a, **kw)
                raise OSError(f"simulated disk-full while writing {name}")
            return real(self, *a, **kw)

        return failing

    def test_materialise_writes_the_record_into_staging_not_in_place(self) -> None:
        """Staging IS the transaction. Asserting on the finished bundles cannot tell
        "staged, then moved" from "written straight into the instance" — the end state is
        identical — so this calls the production writer directly and looks at where the
        bytes actually land: beside the brief, under `staging`, with nothing in the
        instance until `accept` moves it."""
        self._write(_proposal(_ONE, _TWO))
        children = split.parse((self.parent / split.PROPOSAL).read_text(encoding="utf-8"))
        staging = self.tmp / "staging"

        staged = split.materialise(children, ["601", "602"], self.cfg, staging,
                                   parent=self.parent)

        for d in staged:
            self.assertEqual(d.parent, staging, "a child was staged outside staging")
            self.assertTrue((d / split.LINEAGE).is_file(),
                            "the record was not staged beside the brief")
        self.assertFalse(self.cfg.bundle("601").exists(),
                         "materialise wrote into the instance instead of staging")

    def test_child_lineage_is_staged_then_moved_with_the_brief(self) -> None:
        """The record is written into `.split-staging` and moved atomically with
        `brief.md`, so a bundle whose brief never landed can never have a record."""
        self._write(_proposal(_ONE, _TWO))
        created = split.accept(self.parent, ["601", "602"], self.cfg)
        self.assertFalse((self.parent / ".split-staging").exists(),
                         "staging directory was not cleaned up")
        for d in created:
            self.assertTrue((d / split.LINEAGE).exists())
            self.assertTrue((d / "brief.md").exists())

    def test_the_parents_record_is_written_before_the_close_marker(self) -> None:
        """`CLOSE_MARKER` is what makes the parent terminal, so the record goes first and
        a failure between the two leaves the parent un-marked. Here the marker write
        fails: no marker, and no record naming children that were just rolled back."""
        self._write(_proposal(_ONE, _TWO))
        with mock.patch.object(
                Path, "write_text", self._write_text_failing_on(state.CLOSE_MARKER)):
            with self.assertRaises(OSError):
                split.accept(self.parent, ["601", "602"], self.cfg)
        self.assertFalse((self.parent / state.CLOSE_MARKER).exists())
        self.assertIsNone(split.read_lineage(self.parent),
                          "a record naming children that were rolled back was left behind")

    def test_a_failed_accept_restores_the_parents_prior_lineage_bytes(self) -> None:
        """A write that fails after the merge but before `CLOSE_MARKER` must leave the
        parent's PRIOR record byte-for-byte — not the merged one, not a half-written one."""
        prior_bytes = self._seed_child_record(parent="100", siblings=["501"], depth=1)
        self._write(_proposal(_ONE, _TWO))

        with mock.patch.object(
                Path, "write_text", self._write_text_failing_on("build-notes.md")):
            with self.assertRaises(OSError):
                split.accept(self.parent, ["601", "602"], self.cfg)

        self.assertEqual((self.parent / split.LINEAGE).read_bytes(), prior_bytes,
                         "the parent's prior lineage record was not restored")
        self.assertFalse((self.parent / state.CLOSE_MARKER).exists(),
                         "the parent was left marked terminal after a failed accept")
        self.assertFalse(self.cfg.bundle("601").exists(),
                         "a child bundle was left behind after a failed accept")
        self.assertFalse(self.cfg.bundle("602").exists())

    def test_a_failed_accept_with_no_prior_record_leaves_none_behind(self) -> None:
        self._write(_proposal(_ONE, _TWO))
        with mock.patch.object(
                Path, "write_text", self._write_text_failing_on("build-notes.md")):
            with self.assertRaises(OSError):
                split.accept(self.parent, ["601", "602"], self.cfg)
        self.assertFalse((self.parent / split.LINEAGE).exists(),
                         "a lineage record was left behind by a failed FIRST accept")

    def test_an_unreadable_prior_record_refuses_before_anything_is_written(self) -> None:
        """The snapshot the restore above depends on is taken in the PRE-WRITE phase, with
        the proposal and the ids — a record that cannot be read cannot be restored, so the
        accept refuses while refusing is still free.

        Read after the children were moved (the shape this replaced), the same directory
        raised `IsADirectoryError` from the gap between `accept`'s two protected regions:
        it escaped with the children materialised and the parent left open, rolling back
        nothing. The `shutil.move` spy proves the stronger property directly — nothing was
        even staged — and `SplitError` keeps the CLI on its documented recovery path,
        which prints the already-filed child issue ids and the exact retry."""
        (self.parent / split.LINEAGE).mkdir()   # read_bytes() -> IsADirectoryError
        self._write(_proposal(_ONE, _TWO))

        with mock.patch("shutil.move", wraps=shutil.move) as moved:
            with self.assertRaises(split.SplitError):
                split.accept(self.parent, ["601", "602"], self.cfg)

        moved.assert_not_called()
        self.assertFalse(self.cfg.bundle("601").exists(), "a child bundle was created")
        self.assertFalse(self.cfg.bundle("602").exists(), "a child bundle was created")
        self.assertFalse((self.parent / state.CLOSE_MARKER).exists(),
                         "the parent was left marked terminal by a refused accept")
        self.assertFalse((self.parent / "build-notes.md").exists(),
                         "the split breadcrumb was written by a refused accept")
        self.assertFalse((self.parent / ".split-staging").exists())
        self.assertIsNone(split.read_lineage(self.parent))
        self.assertTrue((self.parent / split.LINEAGE).is_dir(),
                        "the refused accept did not leave the parent as it found it")

    def test_a_restore_that_itself_fails_stays_out_of_the_way(self) -> None:
        """The rollback handler must stay total. If putting the record back fails too, the
        exception the operator sees is still the one that broke the accept, and the marker
        cleanup after it still runs — otherwise "children rolled back" and "parent still
        terminal" could coexist, the one pairing the write order exists to prevent. A
        restore that cannot complete is named on stderr instead, like `_rollback`'s."""
        self._seed_child_record(parent="100", siblings=["501"], depth=1)
        self._write(_proposal(_ONE, _TWO))

        def failing_write_bytes(self, *a, **kw):
            raise OSError("simulated disk-full while restoring")

        err = io.StringIO()
        with mock.patch.object(Path, "write_bytes", failing_write_bytes), \
                mock.patch.object(Path, "write_text",
                                  self._write_text_failing_on(state.CLOSE_MARKER,
                                                              land_first=True)), \
                redirect_stderr(err):
            with self.assertRaises(OSError) as caught:
                split.accept(self.parent, ["601", "602"], self.cfg)

        self.assertIn(state.CLOSE_MARKER, str(caught.exception),
                      "the restore's own failure masked the real cause")
        self.assertFalse((self.parent / state.CLOSE_MARKER).exists(),
                         "the marker cleanup was skipped because the restore raised")
        self.assertFalse(self.cfg.bundle("601").exists())
        self.assertIn(split.LINEAGE, err.getvalue(),
                      "a restore that could not complete said nothing")


if __name__ == "__main__":
    unittest.main()
