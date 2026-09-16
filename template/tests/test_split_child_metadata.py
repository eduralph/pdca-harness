"""Split children inherit the parent issue's milestone and labels (issue #467).

Pre-fix, `_create_issue` (`split.py:986-1012` on `main`) built `gh issue create` with
only `--repo`, `--title`, `--body` and `--parent` — `split.py` had no reference to
`milestone` or `label` anywhere, and `file_children` never read anything else off the
parent issue. A split therefore dropped the parent's release metadata: children landed
with no milestone and no labels, and the milestone count stopped matching the work in
both directions (observed on getwyrd/wyrd milestone 0.1 Alpha — 8 children of 3 parents
all filed `milestone=NONE`).

gh reads every `--label` value as CSV, so a label name only arrives intact if it is
encoded for that. The fake `gh` below therefore reads each child's `--label` values back
the way gh does (`_gh_label_field`, pinned to observed gh 2.100.0 behaviour by
`TheFakeGhReadsLabelsLikeGh`) and fails the call where gh would, and the label
assertions are on the names gh would SEE rather than on the raw argv strings alone.

Fixture mirrors `test_split.py:638-694`'s `FilingChildIssues` (a `github` tracker at
`https://github.com/acme/widgets`, parent bundle `issue_500`, `subprocess`/`shutil`
replaced on `pdca_harness.split` so no real `gh` is ever invoked), per this issue's
repro instruction.
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pdca_harness import split
from pdca_harness.config import Config, LeafConfig

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


def _proposal(*children: str, version: int = 1) -> str:
    body = f"<!-- pdca:split-proposal v{version} -->\n# Split proposal\n\n"
    for i, child in enumerate(children, 1):
        body += (f"<!-- pdca:child child-{i} -->\n{child}\n"
                 f"<!-- pdca:end child-{i} -->\n\n")
    return body


_ONE = "- **Slug:** first\n- **Defect / goal:** a\n"
_TWO_INDEP = "- **Slug:** second\n- **Defect / goal:** b\n"


def _gh_label_field(value: str) -> list[str]:
    """The label names gh takes from ONE `--label <value>` — a model of the external parser.

    gh registers `--label` with pflag's `StringSliceVarP`; pflag's `readAsCSV` hands every
    occurrence to Go's `encoding/csv` with default options (`,` separates fields, `"`
    quotes one, `""` inside quotes is a literal quote, no lazy quotes, no space trimming)
    and appends the first record's fields. Raises ValueError, with Go's own wording,
    wherever gh exits `invalid argument ... for "-l, --label" flag: parse error`. Line
    breaks are outside this model; no case here uses one.
    """
    if value == "":
        return []
    if "\n" in value or "\r" in value:
        raise AssertionError(f"{value!r}: line breaks are outside this model")
    fields: list[str] = []
    i = 0
    while True:
        if value.startswith('"', i):                   # quoted field
            parts: list[str] = []
            i += 1
            while True:
                j = value.find('"', i)
                if j < 0:
                    raise ValueError('extraneous or missing " in quoted-field')
                parts.append(value[i:j])
                i = j + 1
                if not value.startswith('"', i):       # the closing quote
                    break
                parts.append('"')                      # `""`: one literal quote
                i += 1
            fields.append("".join(parts))
            if i == len(value):
                return fields
            if value[i] != ",":
                raise ValueError('extraneous or missing " in quoted-field')
            i += 1
        else:                                          # bare field
            j = value.find(",", i)
            field = value[i:] if j < 0 else value[i:j]
            if '"' in field:
                raise ValueError('bare " in non-quoted-field')
            fields.append(field)
            if j < 0:
                return fields
            i = j + 1


def _gh_reads_labels(argv: list[str]) -> list[str]:
    """Every label name gh would take from a `gh issue create` argv, in order.

    Walked as flag/value pairs — every flag `_create_issue` passes takes exactly one value
    — so a title or body that happens to read `--label` is never mistaken for the flag.
    """
    assert argv[:3] == ["gh", "issue", "create"], argv
    assert len(argv) % 2 == 1, f"argv is not flag/value pairs: {argv!r}"
    names: list[str] = []
    for flag, value in zip(argv[3::2], argv[4::2]):
        assert flag.startswith("--"), f"{flag!r} is not a flag: {argv!r}"
        if flag == "--label":
            names += _gh_label_field(value)
    return names


def _label_values(argv: list[str]) -> list[str]:
    """The raw `--label` values, exactly as they appear in the argv."""
    return [value for flag, value in zip(argv[3::2], argv[4::2]) if flag == "--label"]


class TheFakeGhReadsLabelsLikeGh(unittest.TestCase):
    """Pins `_gh_label_field` to what gh 2.100.0 was OBSERVED doing, offline.

    Reads: `GH_BROWSER=echo gh issue list -R acme/widgets --label <value> --web` prints
    one `label:` search term per name gh read (`issue list` and `issue create` register
    `--label` the same way). Errors: `gh issue create --label <value> --help` exits 1 with
    the parse error before doing anything. If a gh release stops reading `--label` as
    CSV, re-run those two commands and update these rows.
    """

    def test_it_reads_each_value_as_gh_did(self) -> None:
        for value, names in (
                ("bug", ["bug"]),
                ("help wanted", ["help wanted"]),
                ("area,backend", ["area", "backend"]),
                ('"area,backend"', ["area,backend"]),
                ('"x,y",z', ["x,y", "z"]),
                ('"say ""hi"""', ['say "hi"']),
                ('"area, ""quoted"""', ['area, "quoted"']),
                ('""""', ['"'])):
            with self.subTest(value=value):
                self.assertEqual(_gh_label_field(value), names)

    def test_it_refuses_what_gh_refused(self) -> None:
        for value, why in (('say "hi"', 'bare " in non-quoted-field'),
                           ('area, "quoted"', 'bare " in non-quoted-field'),
                           ('"a', 'extraneous or missing " in quoted-field'),
                           ('"a"b', 'extraneous or missing " in quoted-field')):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, why):
                    _gh_label_field(value)


class ChildrenInheritParentMetadata(unittest.TestCase):
    """`gh issue view <parent> --json milestone,labels` feeds every child `gh issue
    create` — the lookup happens once, is best-effort, and invents nothing."""

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
        (self.parent / split.PROPOSAL).write_text(_proposal(_ONE, _TWO_INDEP),
                                                  encoding="utf-8")
        self.children = split.parse((self.parent / split.PROPOSAL).read_text(
            encoding="utf-8"))
        self.calls: list[list[str]] = []

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _patched(self, run):
        return mock.patch.multiple(
            "pdca_harness.split",
            subprocess=SimpleNamespace(run=run),
            shutil=SimpleNamespace(which=lambda _n: "/usr/bin/gh",
                                   rmtree=shutil.rmtree, move=shutil.move))

    def _gh(self, *, lookup_response: str = "{}", fail_lookup: bool = False,
            raise_lookup: BaseException | None = None):
        """A fake `gh` recording every call. `gh issue view` is answered from
        `lookup_response`/`fail_lookup`/`raise_lookup`. A `gh issue create` whose
        `--label` values gh could not parse exits 1 and files nothing, as gh does; every
        other one gets an issue URL, numbered 601, 602, ... in filing order."""
        created = {"n": 0}

        def run(cmd, capture_output=False, text=False, cwd=None):
            assert isinstance(cmd, list), f"argv must be a list, not {type(cmd).__name__}"
            assert capture_output is True and text is True
            self.calls.append(list(cmd))
            if cmd[:3] == ["gh", "issue", "view"]:
                if raise_lookup is not None:
                    raise raise_lookup
                if fail_lookup:
                    return SimpleNamespace(returncode=1, stdout="", stderr="gh: HTTP 500")
                return SimpleNamespace(returncode=0, stdout=lookup_response, stderr="")
            try:
                _gh_reads_labels(cmd)
            except ValueError as exc:
                return SimpleNamespace(
                    returncode=1, stdout="",
                    stderr=f'invalid argument for "-l, --label" flag: parse error: {exc}')
            created["n"] += 1
            url = f"https://github.com/acme/widgets/issues/{600 + created['n']}"
            return SimpleNamespace(returncode=0, stdout=url + "\n", stderr="")
        return run

    def _creates(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["gh", "issue", "create"]]

    def _views(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["gh", "issue", "view"]]

    def _file_under_labels(self, *names: str) -> list[str]:
        """File both children under a parent carrying ``names`` (and no milestone);
        return the child ids. A child gh refused fails the test, naming gh's error."""
        meta = json.dumps({"milestone": None, "labels": [{"name": n} for n in names]})
        with self._patched(self._gh(lookup_response=meta)):
            try:
                return split.file_children(self.parent, self.children, self.cfg)
            except split.SplitError as exc:
                self.fail(f"gh refused a child it should have filed: {exc}")

    # -- (a) + (b): milestone and labels reach EVERY child, one --label per name -----

    def test_milestone_and_labels_reach_every_child(self) -> None:
        meta = json.dumps({
            "milestone": {"number": 12, "title": "Milestone 0.60.0"},
            "labels": [{"name": "bug"}, {"name": "help wanted"}],
        })
        with self._patched(self._gh(lookup_response=meta)):
            ids = split.file_children(self.parent, self.children, self.cfg)
        self.assertEqual(ids, ["601", "602"])
        creates = self._creates()
        self.assertEqual(len(creates), 2)
        for call in creates:
            self.assertIn("--milestone", call)
            self.assertEqual(call[call.index("--milestone") + 1], "Milestone 0.60.0")
            # ONE `--label` per name, never comma-joined, and each read back by gh as
            # exactly the parent's name.
            self.assertEqual(call.count("--label"), 2)
            self.assertEqual(_gh_reads_labels(call), ["bug", "help wanted"])

    def test_the_milestone_title_goes_out_verbatim(self) -> None:
        """`--milestone` is a plain string flag in gh — no CSV — so a title holding a
        comma or a quote must NOT be quoted: gh would look up a milestone named with the
        quotes in it."""
        title = 'Milestone 0.60.0, "beta"'
        meta = json.dumps({"milestone": {"number": 12, "title": title}, "labels": []})
        with self._patched(self._gh(lookup_response=meta)):
            ids = split.file_children(self.parent, self.children, self.cfg)
        self.assertEqual(ids, ["601", "602"])
        for call in self._creates():
            self.assertIn("--milestone", call)
            self.assertEqual(call[call.index("--milestone") + 1], title)

    # -- (b), as gh reads it: a comma or a quote in a name stays ONE label -----------

    def test_a_comma_in_a_label_name_stays_one_label(self) -> None:
        """Passed raw, `--label area,backend` asks gh for two labels, `area` and
        `backend`: the child gets labels its parent never had, or gh fails on one that
        does not exist and child 1 aborts the whole split."""
        ids = self._file_under_labels("bug", "area,backend")
        self.assertEqual(ids, ["601", "602"])
        creates = self._creates()
        self.assertEqual(len(creates), 2)
        for call in creates:
            self.assertEqual(_gh_reads_labels(call), ["bug", "area,backend"])

    def test_a_double_quote_in_a_label_name_stays_one_label(self) -> None:
        """Passed raw, a `"` is a CSV parse error: gh exits 1 before filing anything, and
        since every child carries the same labels, the first child fails the split."""
        names = ('say "hi"', 'area, "quoted"', '"')
        ids = self._file_under_labels(*names)
        self.assertEqual(ids, ["601", "602"])
        creates = self._creates()
        self.assertEqual(len(creates), 2)
        for call in creates:
            self.assertEqual(_gh_reads_labels(call), list(names))

    def test_a_plain_label_name_goes_out_byte_for_byte(self) -> None:
        """Only a name gh would misread is encoded: `bug` and `help wanted` reach gh
        exactly as written, beside a name that did need quoting."""
        ids = self._file_under_labels("bug", "help wanted", "area,backend")
        self.assertEqual(ids, ["601", "602"])
        for call in self._creates():
            values = _label_values(call)
            self.assertEqual(len(values), 3, "not one --label per name")
            self.assertEqual(values[:2], ["bug", "help wanted"])

    # -- (c): a parent with neither invents no flag -----------------------------------

    def test_no_milestone_no_labels_matches_todays_argv(self) -> None:
        with self._patched(self._gh(lookup_response="{}")):
            ids = split.file_children(self.parent, self.children, self.cfg)
        self.assertEqual(ids, ["601", "602"])
        for call in self._creates():
            self.assertNotIn("--milestone", call)
            self.assertNotIn("--label", call)
            # Identical in SHAPE to the pre-fix argv — repo, title, body, parent, and
            # nothing else — so a parent with no metadata to inherit invents no flag.
            self.assertEqual(
                call,
                ["gh", "issue", "create", "--repo", "acme/widgets",
                 "--title", call[call.index("--title") + 1],
                 "--body", call[call.index("--body") + 1],
                 "--parent", "500"])

    def test_a_null_milestone_and_no_labels_invent_no_flag(self) -> None:
        """The shape gh prints for an issue with neither — `milestone` is `null`,
        `labels` is `[]` — as opposed to the keys being absent altogether (above)."""
        with self._patched(self._gh(
                lookup_response=json.dumps({"milestone": None, "labels": []}))):
            ids = split.file_children(self.parent, self.children, self.cfg)
        self.assertEqual(ids, ["601", "602"])
        for call in self._creates():
            self.assertNotIn("--milestone", call)
            self.assertNotIn("--label", call)

    # -- (d): looked up ONCE, before the filing loop ----------------------------------

    def test_the_lookup_happens_once_before_the_children(self) -> None:
        with self._patched(self._gh(lookup_response="{}")):
            split.file_children(self.parent, self.children, self.cfg)
        views = self._views()
        self.assertEqual(len(views), 1, "the parent was looked up more than once")
        self.assertEqual(self.calls[0][:3], ["gh", "issue", "view"],
                         "the lookup did not happen before the filing loop")
        self.assertEqual(self.calls[0][3], "500")
        self.assertIn("--repo", self.calls[0])
        self.assertEqual(self.calls[0][self.calls[0].index("--repo") + 1],
                         "acme/widgets")

    # -- (d): best-effort — a failing lookup still files every child, warns ONCE -----

    def test_a_non_zero_lookup_still_files_every_child_and_warns_once(self) -> None:
        err = io.StringIO()
        with self._patched(self._gh(fail_lookup=True)), redirect_stderr(err):
            ids = split.file_children(self.parent, self.children, self.cfg)
        self.assertEqual(ids, ["601", "602"], "a failed lookup must not stop filing")
        for call in self._creates():
            self.assertNotIn("--milestone", call)
            self.assertNotIn("--label", call)
        warning = err.getvalue()
        self.assertEqual(warning.count("\n"), 1, "expected exactly one warning line")
        self.assertIn("milestone", warning.lower())
        self.assertIn("500", warning)

    def test_a_raising_lookup_still_files_every_child_and_warns_once(self) -> None:
        err = io.StringIO()
        with self._patched(self._gh(raise_lookup=RuntimeError("network blip"))), \
             redirect_stderr(err):
            ids = split.file_children(self.parent, self.children, self.cfg)
        self.assertEqual(ids, ["601", "602"])
        for call in self._creates():
            self.assertNotIn("--milestone", call)
            self.assertNotIn("--label", call)
        self.assertEqual(err.getvalue().count("\n"), 1)

    def test_a_non_object_lookup_result_is_treated_as_unknown(self) -> None:
        """`gh` (or a shim) can print valid JSON that is not an object — `null`, `[]` —
        and an unguarded `.get` on it would crash the whole batch instead of the
        promised unknown-so-still-file (mirrors `sources.tracker_issue_reopened`,
        `sources.py:173-175`)."""
        for response in ("null", "[]"):
            with self.subTest(response=response):
                self.calls.clear()
                err = io.StringIO()
                with self._patched(self._gh(lookup_response=response)), \
                     redirect_stderr(err):
                    ids = split.file_children(self.parent, self.children, self.cfg)
                self.assertEqual(ids, ["601", "602"])
                for call in self._creates():
                    self.assertNotIn("--milestone", call)
                    self.assertNotIn("--label", call)
                self.assertEqual(err.getvalue().count("\n"), 1)

    def test_unparseable_lookup_output_is_treated_as_unknown(self) -> None:
        err = io.StringIO()
        with self._patched(self._gh(lookup_response="not json at all")), \
             redirect_stderr(err):
            ids = split.file_children(self.parent, self.children, self.cfg)
        self.assertEqual(ids, ["601", "602"])
        for call in self._creates():
            self.assertNotIn("--milestone", call)
            self.assertNotIn("--label", call)
        self.assertEqual(err.getvalue().count("\n"), 1)

    def test_ctrl_c_during_the_lookup_stays_an_interrupt(self) -> None:
        """Best-effort covers ERRORS, not the operator's Ctrl-C. Swallowing it would go
        on to file issues the tracker cannot take back after being told to stop; nothing
        is filed yet at that point, so letting it through loses nothing (the rule the
        filing loop already follows: an interrupt stays an interrupt)."""
        with self._patched(self._gh(raise_lookup=KeyboardInterrupt())), \
             redirect_stderr(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                split.file_children(self.parent, self.children, self.cfg)
        self.assertEqual(self._creates(), [], "a child was filed after Ctrl-C")

    # -- (e): the --ids path and a non-GitHub tracker do NO lookup --------------------

    def test_ids_path_does_no_lookup(self) -> None:
        def never(cmd, **kw):
            raise AssertionError("gh was invoked on the --ids path")
        with self._patched(never):
            created = split.accept(self.parent, ["601", "602"], self.cfg)
        self.assertEqual([d.name for d in created], ["issue_601", "issue_602"])

    def test_a_non_github_tracker_does_no_lookup(self) -> None:
        self.cfg.tracker_system = "gitlab"

        def never(cmd, **kw):
            raise AssertionError("gh was invoked against a non-GitHub tracker")
        with self._patched(never):
            with self.assertRaises(split.TrackerUnavailable):
                split.file_children(self.parent, self.children, self.cfg)

    # -- (f): can't pass by filing fewer children --------------------------------------

    def test_every_child_is_filed_AND_carries_the_metadata_together(self) -> None:
        """Both have to hold at once: a fix that inherited the metadata for only the
        first child, or silently dropped a child to make indices line up, would satisfy
        an argv-only check while still under-filing the batch."""
        meta = json.dumps({"milestone": {"title": "M"}, "labels": [{"name": "x"}]})
        with self._patched(self._gh(lookup_response=meta)):
            ids = split.file_children(self.parent, self.children, self.cfg)
        self.assertEqual(len(ids), len(self.children), "not every child was filed")
        creates = self._creates()
        self.assertEqual(len(creates), len(self.children))
        for call in creates:
            self.assertIn("--milestone", call)
            self.assertEqual(call[call.index("--milestone") + 1], "M")
            self.assertEqual(_gh_reads_labels(call), ["x"])


if __name__ == "__main__":
    unittest.main()
