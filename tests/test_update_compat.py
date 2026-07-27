"""Template-repo test: `copier update` from a prior release, not just a fresh render.

`test_render_and_run` renders the template from scratch. That is not the path any
existing instance takes — they run `copier update`, which **three-way merges** the new
template against edits the instance already made. `pdca.toml` is by design the file every
instance edits (gate rows, leaf argv, checkouts, doctor rows), so the least-tested path is
the one applied to the most-edited file. A change that renders cleanly and still breaks
every downstream instance is invisible without this (issue #342).

Why this cannot reuse `test_render_and_run`'s fixture: that one strips `.git`, makes a
single commit of the *current* tree and tags only `v0test`, so there is no prior release to
render from. Here the throwaway repo carries two commits — a genuine prior release tree
(`git archive <tag>`) and the current working tree — tagged so copier can move between them.

Skips cleanly when copier is missing, or when the checkout has no release tags (a shallow
CI clone: `actions/checkout` needs `fetch-depth: 0` for this to run).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

try:
    from copier import run_copy, run_update  # type: ignore

    HAVE_COPIER = True
except Exception:  # pragma: no cover - environment without copier
    HAVE_COPIER = False


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _git_out(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True).stdout


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", message)


def prior_release_ref() -> str | None:
    """The newest `vX.Y.Z` tag in this checkout, or None if there are none.

    DERIVED, never hardcoded: a pinned version would rot at every release. None means the
    caller should skip — a shallow clone has no tags, which is a missing precondition and
    not a failure.
    """
    try:
        out = _git_out(REPO, "tag", "--list", "v[0-9]*", "--sort=-v:refname")
    except subprocess.CalledProcessError:  # pragma: no cover - not a git checkout
        return None
    tags = [t for t in out.split() if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
    return tags[0] if tags else None


def build_two_ref_source(tmp: Path, prior: str) -> Path:
    """A throwaway template repo with the prior release at `v_old` and HEAD's working tree
    at `v_new`, so `run_copy(vcs_ref="v_old")` then `run_update(vcs_ref="v_new")` walks the
    same distance a real instance walks."""
    src = tmp / "src"
    src.mkdir()
    _git(src, "init", "-q")

    # Commit 1 — the genuine prior release tree, extracted from this repo's history.
    subprocess.run(f"git -C {REPO} archive {prior} | tar -x -C {src}",
                   shell=True, check=True, capture_output=True)
    _commit(src, "prior release")
    _git(src, "tag", "v_old")

    # Commit 2 — the current working tree (the change under test, committed or not).
    for child in src.iterdir():
        if child.name == ".git":
            continue
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    for child in REPO.iterdir():
        if child.name in (".git", "__pycache__", ".venv"):
            continue
        dst = src / child.name
        if child.is_dir():
            shutil.copytree(child, dst,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(child, dst)
    _commit(src, "current tree")
    _git(src, "tag", "v_new")
    return src


# The edits a real instance makes to pdca.toml. Each is a distinct merge shape: a changed
# scalar, an appended row inside an inline array-of-tables, and a whole new table.
_ADDED_GATE = ('  { id = "instance-extra", tier = "T4", '
               'label = "instance-owned contribution lint", '
               'cmd = "true", gating = true, scope = "repo" },')


def apply_instance_edits(out: Path) -> None:
    text = (out / "pdca.toml").read_text(encoding="utf-8")

    # A changed scalar the instance owns.
    text, n = re.subn(r"^lanes = \d+$", "lanes = 4", text, count=1, flags=re.M)
    assert n == 1, "no [driver].lanes line to edit — template shape changed"

    # A row appended to the shipped gates array, beside the T4 contribution row.
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if 'id = "T4-contribution"' in line:
            lines.insert(i + 1, _ADDED_GATE)
            break
    else:  # pragma: no cover - shipped row missing means the template moved
        raise AssertionError("shipped T4-contribution row not found")
    text = "\n".join(lines) + "\n"

    # A key added under a table the template already ships. (Adding a *new*
    # `[publisher.checkouts]` table would be duplicate TOML — the template ships it
    # empty-but-present precisely so an instance fills it in, which is the real shape.)
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "[publisher.checkouts]":
            lines.insert(i + 1, '"org/thing" = "../thing"')
            break
    else:  # pragma: no cover - shipped table missing means the template moved
        raise AssertionError("shipped [publisher.checkouts] table not found")
    (out / "pdca.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


_PROBE = r"""
import json, sys
sys.path.insert(0, "src")
from pathlib import Path
from pdca_harness.config import Config

cfg = Config.load(Path("."))
print(json.dumps({
    "lanes": cfg.lanes,
    "checkouts": dict(getattr(cfg, "repo_checkouts", {}) or {}),
    "gates": [
        {"id": c.get("id", ""), "tier": c.get("tier", ""),
         "scope": c.get("scope", ""), "at_publish": c.get("at_publish", None)}
        for c in cfg.gates_checks
    ],
    "leaves": {k: {"mode": getattr(v, "mode", None)}
               for k, v in vars(cfg).items() if hasattr(v, "mode") and hasattr(v, "argv")},
    "raw": Path("pdca.toml").read_text(encoding="utf-8"),
}))
"""


CONFLICT_MARKERS = ("<<<<<<<", "=======", ">>>>>>>")


def conflict_markers_in(out: Path) -> list[str]:
    """Lines of `pdca.toml` carrying git conflict markers after the update.

    Copier's update is a three-way merge, so a template edit on a line *adjacent* to one
    the instance owns does not fail loudly — it writes conflict markers into the file and
    exits 0. The instance then has syntactically invalid TOML and every `pdca` command
    dies at config load. Checked by name because the resulting `TOMLDecodeError: Invalid
    statement` says nothing about the cause.
    """
    text = (out / "pdca.toml").read_text(encoding="utf-8")
    return [ln for ln in text.splitlines() if ln.startswith(CONFLICT_MARKERS)]


def load_updated_config(out: Path) -> tuple[dict | None, str]:
    """`(config, error)` for the merged instance, loaded in-instance.

    Returns the error rather than raising so a *conflicted* merge is reported by the test
    that names that failure, instead of dying in `setUpClass` where every other assertion
    disappears with it. Asserting on the loaded config rather than on file text is the
    point: a merge that leaves parseable TOML but drops a row is exactly what this catches.
    """
    r = subprocess.run([sys.executable, "-c", _PROBE], cwd=out,
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None, r.stderr
    return json.loads(r.stdout), ""


def render_prior_edit_and_update(tmp: Path, prior: str) -> tuple[dict | None, str]:
    """The whole flow, returning the merged config. Shared so per-issue update assertions
    (e.g. #339's T4 publish-selection rule) reuse one fixture instead of rebuilding it."""
    src = build_two_ref_source(tmp, prior)
    out = tmp / "instance"
    run_copy(str(src), str(out),
             data={"project_name": "Update Test", "tracker_url": "https://x/issues"},
             vcs_ref="v_old", defaults=True, unsafe=True, quiet=True)
    # copier update refuses to run on a non-repo / dirty tree, and a real instance is
    # always a repo — so commit the render, then the edits, as an instance would.
    _git(out, "init", "-q")
    _commit(out, "rendered")
    apply_instance_edits(out)
    _commit(out, "instance edits")

    run_update(str(out), vcs_ref="v_new", defaults=True, unsafe=True,
               quiet=True, overwrite=True)
    return load_updated_config(out)


@unittest.skipUnless(HAVE_COPIER, "copier not installed")
class UpdateCompat(unittest.TestCase):
    """`copier update` from the previous release must not silently change an instance."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.prior = prior_release_ref()
        if cls.prior is None:  # pragma: no cover - shallow clone
            raise unittest.SkipTest(
                "no vX.Y.Z tags in this checkout (shallow clone? needs fetch-depth: 0)")
        cls.tmp = Path(tempfile.mkdtemp())
        cls.out = cls.tmp / "instance"
        cls.cfg, cls.load_error = render_prior_edit_and_update(cls.tmp, cls.prior)

    @classmethod
    def tearDownClass(cls) -> None:
        if getattr(cls, "tmp", None):
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def config(self) -> dict:
        """The merged config, or fail here with the conflicted-merge diagnosis.

        A conflicted merge breaks every config-shaped assertion at once; naming the cause
        beats five copies of `TOMLDecodeError: Invalid statement`.
        """
        if self.cfg is None:
            markers = conflict_markers_in(self.out)
            hint = ("\n\ncopier update left conflict markers in pdca.toml — a template "
                    "edit landed next to an instance-owned line:\n  "
                    + "\n  ".join(markers)) if markers else ""
            self.fail(f"merged instance config failed to load:\n{self.load_error}{hint}")
        return self.cfg

    def test_instance_edits_survive_the_merge(self) -> None:
        """The three merge shapes an instance actually produces."""
        self.assertEqual(self.config()["lanes"], 4, "edited scalar lost in the merge")
        self.assertIn("instance-extra", [g["id"] for g in self.config()["gates"]],
                      "instance-appended gate row lost in the merge")
        self.assertEqual(self.config()["checkouts"].get("org/thing"), "../thing",
                         "instance-added [publisher.checkouts] table lost in the merge")

    def test_shipped_contribution_gate_survives(self) -> None:
        """The shipped T4 contribution row must still be there, still bundle-scoped.

        #339 keys publish selection off `scope`, so losing or rescoping this row on update
        would silently stop gating every instance's contribution artifacts.
        """
        rows = [g for g in self.config()["gates"] if g["id"] == "T4-contribution"]
        self.assertEqual(len(rows), 1, "shipped T4-contribution row lost on update")
        self.assertEqual(rows[0]["scope"], "bundle",
                         "T4-contribution rescoped — publish selection depends on this")

    def test_no_model_work_is_newly_enabled(self) -> None:
        """An update must never switch on model-backed work an instance did not ask for.

        The guard #321 has to satisfy: a rendered `size_guard` default plus a configured
        sizer leaf would give an updating instance new artifacts, latency and model cost
        with no opt-in. Asserted on the merged TOML so it fails the moment such a default
        is introduced, not once someone notices the bill.
        """
        cfg = self.config()
        # Asserted on the PARSED config, not on raw text. The template may legitimately
        # SHIP a `[leaves.sizer]` table — #320 does, at `mode = "stub"` — and a raw-text
        # test would fail on the mere presence of a section that costs nothing. What must
        # never change on update is whether a model actually RUNS.
        for name, leaf in cfg["leaves"].items():
            with self.subTest(leaf=name):
                self.assertNotEqual(leaf.get("mode"), "command",
                                    f"update switched leaf '{name}' to a live model call")
        for line in cfg["raw"].splitlines():
            stripped = line.strip()
            if stripped.startswith("size_guard") and not stripped.startswith("#"):
                self.assertIn('"off"', stripped,
                              "update enabled size_guard without opt-in")

    def test_merge_leaves_no_conflict_markers(self) -> None:
        """A template edit next to an instance-owned line must not ship broken TOML.

        Copier's update merges three ways and exits 0 having written `<<<<<<<` into
        `pdca.toml`; the instance discovers it when the next `pdca` command dies at config
        load. Every 0.56 issue that edits `pdca.toml.jinja` — #314, #320, #321, #322,
        #323, #337, #339 — can trigger this by touching a line adjacent to one instances
        commonly own (`[driver] lanes`, leaf `argv`, the gates array).
        """
        markers = conflict_markers_in(self.out)
        self.assertEqual(markers, [],
                         "copier update left conflict markers in pdca.toml:\n  "
                         + "\n  ".join(markers))

    def test_merged_config_still_loads(self) -> None:
        """Parseable TOML *and* a Config the engine accepts — setUpClass would have raised
        otherwise, so this documents the guarantee rather than re-deriving it."""
        self.assertTrue(self.config()["gates"], "merged config has no gate rows at all")


if __name__ == "__main__":
    unittest.main()
