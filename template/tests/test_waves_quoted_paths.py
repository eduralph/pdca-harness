"""Offline tests for `waves.diff_files` on Git-quoted ``diff --git`` headers.

Git C-quotes a path that holds a non-ASCII byte (``core.quotePath``), a double quote, a
backslash or a control character: ``diff --git "a/caf\\303\\251.txt" "b/caf\\303\\251.txt"``.
`diff_files` must return the real repo-relative path for those headers too, so the advisory
same-wave overlap audit (`flow._audit_wave_overlap`) and the size backstop's ``patch_files``
count see every file a patch touches. Patches come from real ``git`` in a temp repo, with
quoting pinned on; no network. Run from the project root:
    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from pdca_harness import flow, waves

# A fixed identity and no global or system git config, so a host's own settings (its
# `core.quotePath`, signing, hooks) cannot change the patches these tests are built on.
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(repo: Path, *args: str) -> bytes:
    # Inherited GIT_* variables are dropped: a GIT_DIR set by a hook would point these
    # commands at the outer repository.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")} | _GIT_ENV
    return subprocess.run(["git", "-C", str(repo), *args], check=True, env=env,
                          capture_output=True).stdout


def _repo(root: Path, files: dict[str, str]) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for name, body in files.items():
        (repo / name).write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _diff(repo: Path, out: Path, *extra: str) -> Path:
    """Write ``git diff`` to ``out`` byte for byte. Quoting is pinned on, so a host with
    ``core.quotePath = false`` cannot hand the parser an unquoted header instead."""
    out.write_bytes(_git(repo, "-c", "core.quotePath=true", "diff", *extra))
    return out


class QuotedHeaderTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def _patch(self, text: str) -> Path:
        p = self.root / "hand.diff"
        p.write_text(text, encoding="utf-8")
        return p

    def _edit(self, repo: Path, path: str, bundle: str) -> Path:
        """A bundle dir whose ``patch.diff`` is a real ``git diff`` editing ``path``."""
        (repo / path).write_text("edited by " + bundle + "\n", encoding="utf-8")
        d = self.root / bundle
        d.mkdir()
        _diff(repo, d / "patch.diff")
        _git(repo, "checkout", "-q", "--", ".")
        return d

    # (a) the reproduction: an edit to café.txt
    def test_non_ascii_path(self) -> None:
        repo = _repo(self.root, {"café.txt": "x\n"})
        (repo / "café.txt").write_text("y\n", encoding="utf-8")
        patch = _diff(repo, self.root / "p.diff")
        self.assertIn(b'diff --git "a/caf\\303\\251.txt" "b/caf\\303\\251.txt"\n',
                      patch.read_bytes())
        self.assertEqual(waves.diff_files(patch), {"café.txt"})

    # (b) a double quote, a backslash, a tab and a newline each come back decoded
    def test_quote_backslash_tab_newline_paths(self) -> None:
        quoted = {'say"hi".txt': b'"a/say\\"hi\\".txt"',
                  "back\\slash.txt": b'"a/back\\\\slash.txt"',
                  "tab\there.txt": b'"a/tab\\there.txt"',
                  "new\nline.txt": b'"a/new\\nline.txt"'}
        repo = _repo(self.root, dict.fromkeys(quoted, "x\n"))
        for name in quoted:
            (repo / name).write_text("y\n", encoding="utf-8")
        patch = _diff(repo, self.root / "p.diff")
        for header in quoted.values():
            self.assertIn(header, patch.read_bytes())
        self.assertEqual(waves.diff_files(patch), set(quoted))

    def test_rest_of_git_escape_table(self) -> None:
        # \a \b \v \f \r, and octal for a control byte with no letter (\001, DEL \177).
        names = ["bell\a.txt", "bs\b.txt", "vt\v.txt", "ff\f.txt", "cr\r.txt",
                 "soh\x01.txt", "del\x7f.txt"]
        repo = _repo(self.root, dict.fromkeys(names, "x\n"))
        for name in names:
            (repo / name).write_text("y\n", encoding="utf-8")
        patch = _diff(repo, self.root / "p.diff")
        for header in (b'"a/bell\\a.txt"', b'"a/cr\\r.txt"', b'"a/soh\\001.txt"',
                       b'"a/del\\177.txt"'):
            self.assertIn(header, patch.read_bytes())
        self.assertEqual(waves.diff_files(patch), set(names))

    # (c) a rename between a quoted and an unquoted name returns both names
    def test_rename_plain_to_quoted(self) -> None:
        repo = _repo(self.root, {"plain.txt": "plain\n" * 5})
        _git(repo, "mv", "plain.txt", "café.txt")
        patch = _diff(repo, self.root / "p.diff", "--cached", "-M")
        self.assertIn(b'diff --git a/plain.txt "b/caf\\303\\251.txt"\n', patch.read_bytes())
        self.assertEqual(waves.diff_files(patch), {"plain.txt", "café.txt"})

    def test_rename_quoted_to_plain(self) -> None:
        repo = _repo(self.root, {"café.txt": "café\n" * 5})
        _git(repo, "mv", "café.txt", "plain.txt")
        patch = _diff(repo, self.root / "p.diff", "--cached", "-M")
        self.assertIn(b'diff --git "a/caf\\303\\251.txt" b/plain.txt\n', patch.read_bytes())
        self.assertEqual(waves.diff_files(patch), {"café.txt", "plain.txt"})

    def test_rename_with_spaces_next_to_a_quoted_side(self) -> None:
        # The quotes mark where each side ends, so a space in either name is not ambiguous.
        repo = _repo(self.root, {"my notes.txt": "notes\n" * 5, "naïve list.txt": "list\n" * 5})
        _git(repo, "mv", "my notes.txt", "café notes.txt")
        _git(repo, "mv", "naïve list.txt", "plain list.txt")
        patch = _diff(repo, self.root / "p.diff", "--cached", "-M")
        self.assertIn(b'diff --git a/my notes.txt "b/caf\\303\\251 notes.txt"\n',
                      patch.read_bytes())
        self.assertIn(b'diff --git "a/na\\303\\257ve list.txt" b/plain list.txt\n',
                      patch.read_bytes())
        self.assertEqual(waves.diff_files(patch), {"my notes.txt", "café notes.txt",
                                                   "naïve list.txt", "plain list.txt"})

    # (d) unquoted headers, /dev/null sides and a missing file behave as before
    def test_plain_modify_add_delete(self) -> None:
        repo = _repo(self.root, {"src.py": "x\n", "gone.py": "x\n"})
        (repo / "src.py").write_text("y\n", encoding="utf-8")
        (repo / "gone.py").unlink()
        (repo / "new.py").write_text("n\n", encoding="utf-8")
        _git(repo, "add", "-A")
        patch = _diff(repo, self.root / "p.diff", "--cached")
        text = patch.read_bytes()
        self.assertIn(b"--- /dev/null\n", text)
        self.assertIn(b"+++ /dev/null\n", text)
        self.assertNotIn(b'"', text)
        self.assertEqual(waves.diff_files(patch), {"src.py", "gone.py", "new.py"})

    def test_quote_inside_a_plain_name_reads_as_before(self) -> None:
        # Git never writes this (it quotes such a name), but no side opens with a quote,
        # so the plain reading applies, unchanged.
        p = self._patch('diff --git a/say"hi".txt b/say"hi".txt\n')
        self.assertEqual(waves.diff_files(p), {'say"hi".txt'})

    def test_missing_file_is_empty(self) -> None:
        self.assertEqual(waves.diff_files(self.root / "nope.diff"), set())

    # (e) the advisory overlap audit sees a quoted path two same-wave bundles share
    def test_overlap_audit_flags_shared_quoted_path(self) -> None:
        repo = _repo(self.root, {"café.txt": "x\n"})
        wave = [self._edit(repo, "café.txt", "issue_1"), self._edit(repo, "café.txt", "issue_2")]
        with redirect_stderr(io.StringIO()) as err:
            flow._audit_wave_overlap(wave)
        self.assertIn("issue_1 and issue_2 both touch café.txt", err.getvalue())
        self.assertIn("undeclared conflict", err.getvalue())

    def test_overlap_audit_keeps_names_apart_that_differ_in_escaped_bytes(self) -> None:
        # café (\303\251) and cafè (\303\250): a lossy decode would merge them into a
        # false overlap.
        repo = _repo(self.root, {"café.txt": "x\n", "cafè.txt": "x\n"})
        wave = [self._edit(repo, "café.txt", "issue_1"), self._edit(repo, "cafè.txt", "issue_2")]
        self.assertEqual(waves.diff_files(wave[0] / "patch.diff"), {"café.txt"})
        self.assertEqual(waves.diff_files(wave[1] / "patch.diff"), {"cafè.txt"})
        with redirect_stderr(io.StringIO()) as err:
            flow._audit_wave_overlap(wave)
        self.assertEqual(err.getvalue(), "")

    # (f) a malformed quoted header never raises; the docstring's choice is to skip it
    def test_malformed_quoted_header_is_skipped(self) -> None:
        bad = [
            'diff --git "a/x\\3" "b/x\\3"',               # short octal escape
            'diff --git "a/x\\q" "b/x\\q"',               # unknown escape
            'diff --git "a/x\\477" "b/x\\477"',           # octal escape above one byte
            'diff --git "a/x\\',                          # backslash at the end of the line
            'diff --git "a/unterminated b/unterminated',  # no closing quote
            'diff --git "a/x" "b/unterminated',           # second side never closes
            'diff --git a/x "b/x\\q"',                    # bad escape in a mixed header
            'diff --git "a/x\\" "b/x\\"',                 # the backslash eats the quote
            'diff --git "a/x""b/x"',                      # no space between the sides
            'diff --git "a/x" "b/x" trailing',            # text after the second side
            'diff --git a/x "c/x"',                       # wrong prefix inside the quotes
            'diff --git "a/" "b/"',                       # empty paths
            'diff --git "a/x" b/',                        # empty plain path after a quoted one
        ]
        for line in bad:
            with self.subTest(line=line):
                # Skipped means no path at all from that header, half-decoded or otherwise…
                self.assertEqual(waves.diff_files(self._patch(line + "\n")), set())
                # …and only that header: the next one still reads.
                both = self._patch(line + "\ndiff --git a/ok.txt b/ok.txt\n")
                self.assertEqual(waves.diff_files(both), {"ok.txt"})

    def test_octal_bytes_not_valid_utf8_decode_with_replacement(self) -> None:
        # (f) the docstring's other choice: a well-formed quoted name whose bytes are not
        # UTF-8 decodes with U+FFFD, so the file is still counted.
        name = os.fsdecode(b"x\xffy.txt")
        try:
            repo = _repo(self.root, {name: "x\n"})
        except (OSError, UnicodeError) as e:  # a filesystem that stores only UTF-8 names
            self.skipTest(f"cannot create a non-UTF-8 file name here: {e}")
        (repo / name).write_text("y\n", encoding="utf-8")
        patch = _diff(repo, self.root / "p.diff")
        self.assertIn(b'diff --git "a/x\\377y.txt" "b/x\\377y.txt"\n', patch.read_bytes())
        self.assertEqual(waves.diff_files(patch), {"x�y.txt"})


if __name__ == "__main__":
    unittest.main()
