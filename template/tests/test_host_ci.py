"""Host-only CI parity gate (`[gates] host_ci`, issue #311) — offline slice.

The blind spot this closes: an instance's only CI-parity gates delegate to the host's
runner, and the T4 publish gate runs with ``cwd=cfg.root`` against the tree BEFORE
``patch.diff`` is applied (`publish._t4_passes`) — so a host CI job outside that runner
(a spell-checker like `typos`, a docs lint) structurally cannot see content that arrives
in the patch. Result: Check green, PR opens red on a required status (observed four
times in the wyrd instance).

The success criterion, asserted here against stub commands:
  (a) a declared command that exits non-zero AGAINST THE TREE THE PUSH WOULD PUBLISH
      blocks publish — no branch is pushed, no PR is opened — and the failure is
      recorded with the command named. "Non-zero" is literal: exit 77 and a
      hand-marked non-gating row block too (iteration-1 C3 — no unblessed carve-outs);
  (b) a command that exits 0 leaves publish behaviour unchanged;
  (c) an instance that declares nothing is byte-identical to today.

Iteration-1 C5 is covered by the real-git class: the publish-leg gate must FETCH and
PIN the exact base commit the push will build on — a warm no-fetch reconstruction
certified a stale base while the push fetched afterward, so the gate could pass a tree
other than the one pushed. Red on pre-fix `main`: publish consulted only `_t4_passes`
and pushed regardless of any declared host-CI command. The git/gh subprocesses are
stubbed the way this suite already stubs them (`test_publish_slice`); the real-git
class uses a toy bare origin + clone — no Claude, no network.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess as sp
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pdca_harness import gates, publish, signoff, worktree
from pdca_harness.config import Config, LeafConfig, _normalize_host_ci

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


def _cfg(root: Path) -> Config:
    """Stub leaves, no configured gates, generic publish defaults (own-repo remotes)."""
    return Config(
        root=root,
        bundle_root=root / "results",
        process_dir=root / "process",
        templates_dir=TEMPLATES,
        default_branch="main",
        tracker_system="github",
        tracker_url="https://example.org/issues",
        issue_id_example="1",
        builder=LeafConfig(mode="stub"),
        reviewer=LeafConfig(mode="stub"),
        planner=LeafConfig(mode="stub", interactive=True),
        signoff=LeafConfig(mode="stub", interactive=True),
        publisher=LeafConfig(mode="stub", interactive=True),
        act=LeafConfig(mode="stub", interactive=True),
        gates_checks=[],
        base_remote="origin",
        # Hermetic: pin the toy target inside this test's tmp root (see
        # test_publish_slice for why the sibling convention must not be relied on here).
        repo_checkouts={"example-org/example-repo": str(root / "example-repo")},
    )


_FIX_BRIEF = (
    "- **Slug:** my-fix\n"
    "- **Repo + branch target:** example-org/example-repo @ main\n"
)
_STACK_BRIEF = _FIX_BRIEF + "- **Onto branch:** origin/feature/x\n"


def _bundle(cfg: Config, issue_id: str, *, brief_body: str = _FIX_BRIEF) -> Path:
    """An accepted (COMPLETE) bundle with a non-empty patch — publish's precondition."""
    d = cfg.bundle(issue_id)
    d.mkdir(parents=True)
    (d / "brief.md").write_text(brief_body, encoding="utf-8")
    (d / "patch.diff").write_text("diff --git a/x b/x\n", encoding="utf-8")
    (d / "check-gates.json").write_text("{}", encoding="utf-8")
    shutil.copyfile(TEMPLATES / "SUMMARY.md.tpl", d / "SUMMARY.md")
    signoff.record(d / "SUMMARY.md", action="accept", by="Tester", date="2026-07-31")
    return d


class HostCiConfig(unittest.TestCase):
    """`[gates] host_ci` parses into gate-check-shaped rows; absent ⇒ empty (criterion c)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _load(self, toml: str) -> Config:
        (self.tmp / "pdca.toml").write_text(toml, encoding="utf-8")
        return Config.load(self.tmp)

    def test_bare_string_and_table_rows_normalize(self) -> None:
        cfg = self._load(
            "[gates]\n"
            'host_ci = ["typos", { id = "docs", cmd = "./lint", tier = "T2" }]\n')
        self.assertEqual(cfg.host_ci_checks, [
            {"cmd": "typos", "gating": True, "id": "host-ci-0", "tier": "T4",
             "scope": "bundle", "label": "host CI: typos"},
            {"id": "docs", "cmd": "./lint", "gating": True, "tier": "T2",
             "scope": "bundle", "label": "host CI: ./lint"},
        ])

    def test_gating_false_is_forced_true_loudly(self) -> None:
        # Iteration-1 C3: a `gating = false` row published despite a failing command.
        # The key is now a contract, not a default — forced true, ignored loudly.
        err = io.StringIO()
        with redirect_stderr(err):
            cfg = self._load('[gates]\nhost_ci = [{ cmd = "typos", gating = false }]\n')
        self.assertTrue(cfg.host_ci_checks[0]["gating"])
        self.assertIn("gating", err.getvalue())

    def test_absent_key_is_empty(self) -> None:
        # Criterion (c) at the config layer: nothing declared ⇒ nothing runs anywhere.
        cfg = self._load("[gates]\nchecks = []\n")
        self.assertEqual(cfg.host_ci_checks, [])

    def test_commandless_entry_is_dropped_loudly(self) -> None:
        # The #338 lesson: `subprocess.run("")` exits 0, so an empty cmd would pass
        # vacuously. A row with neither cmd nor subcmd is dropped with a warning.
        err = io.StringIO()
        with redirect_stderr(err):
            cfg = self._load('[gates]\nhost_ci = [{ id = "oops" }, "typos"]\n')
        self.assertEqual(len(cfg.host_ci_checks), 1)
        self.assertEqual(cfg.host_ci_checks[0]["cmd"], "typos")
        self.assertIn("host_ci", err.getvalue())

    def test_gates_stub_mode_empties_host_ci(self) -> None:
        # PDCA_GATES_MODE=stub must silence host CI too (offline rehearse determinism).
        with mock.patch.dict(os.environ, {"PDCA_GATES_MODE": "stub"}):
            cfg = self._load('[gates]\nhost_ci = ["typos"]\n')
        self.assertEqual(cfg.host_ci_checks, [])


class HostCiPublishGate(unittest.TestCase):
    """The pre-push leg: `publish` runs the declared commands against the pinned
    base + patch.diff tree and refuses to push on ANY non-pass — the seam `_t4_passes`
    (pre-apply, cwd=root) structurally cannot cover."""

    SHA = "a" * 40  # the pinned base commit `_pinned_base` is stubbed to resolve

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = _cfg(self.tmp)
        # The tree for_publish hands back in these offline tests (a real dir, no git).
        self.tree = self.tmp / "pushed-tree"
        self.tree.mkdir()
        self.calls: list[list[str]] = []

    def _fake_run(self, cmd, *a, **k):
        self.calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def _publish(self, issue_id: str, *, pinned=None, for_publish=None,
                 existing_pr: str | None = None):
        """Real (non-dry) publish with git/gh stubbed and the pin + tree controlled."""
        pb = pinned if pinned is not None else mock.MagicMock(return_value=(self.SHA, ""))
        fp = for_publish if for_publish is not None else mock.MagicMock(
            return_value=self.tree)
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch.object(publish, "_check_repo", return_value=0))
            st.enter_context(mock.patch.object(publish.subprocess, "run",
                                               side_effect=self._fake_run))
            st.enter_context(mock.patch.object(publish, "_pinned_base", pb))
            st.enter_context(mock.patch.object(publish.worktree, "for_publish", fp))
            st.enter_context(mock.patch.object(publish.worktree, "overflow_remove",
                                               mock.MagicMock()))
            if existing_pr is not None:
                st.enter_context(mock.patch.object(publish, "_existing_pr",
                                                   return_value=existing_pr))
            st.enter_context(redirect_stdout(buf))
            st.enter_context(redirect_stderr(err))
            rc = publish.publish(self.cfg, issue_id, by="Tester", today="2026-07-31")
        return rc, err.getvalue(), pb, fp

    def test_failing_command_blocks_publish_and_names_it(self) -> None:
        # Criterion (a): non-zero ⇒ no push, no PR, the command named + recorded with
        # the pinned base it was judged against.
        d = _bundle(self.cfg, "A1")
        self.cfg.host_ci_checks = _normalize_host_ci(["exit 1"])
        rc, err, _, _ = self._publish("A1")
        self.assertEqual(rc, 1)
        self.assertFalse(any("push" in c for c in self.calls))          # nothing pushed
        self.assertFalse(any(c[:3] == ["gh", "pr", "create"] for c in self.calls))
        self.assertIn("exit 1", err)                                    # command named
        self.assertIn("host-ci-0", err)
        record = json.loads((d / "host-ci.json").read_text(encoding="utf-8"))
        self.assertEqual(record["overall"], "fail")
        self.assertEqual(record["base"], self.SHA)                      # judged base pinned
        self.assertIn("exit 1", json.dumps(record))                     # recorded too
        self.assertFalse((d / "publish.json").exists())

    def test_exit_77_blocks_publish_too(self) -> None:
        # Iteration-1 C3: exit 77 is a CHECK gate's "cannot decide" channel (→ §6),
        # but criterion (a) is literal — a command that exits non-zero blocks publish.
        # The host's CI will fail the PR on it regardless of what a carve-out believed.
        _bundle(self.cfg, "U1")
        self.cfg.host_ci_checks = _normalize_host_ci(["exit 77"])
        rc, err, _, _ = self._publish("U1")
        self.assertEqual(rc, 1)
        self.assertFalse(any("push" in c for c in self.calls))
        self.assertIn("exit 77", err)

    def test_hand_marked_non_gating_row_still_blocks(self) -> None:
        # Iteration-1 C3, belt-and-braces: even a row that BYPASSES the normalizer's
        # forced gating (hand-built config) must block — the publish leg does not
        # consult `gating` at all.
        _bundle(self.cfg, "G1")
        self.cfg.host_ci_checks = [{"id": "adv", "cmd": "exit 1", "gating": False,
                                    "tier": "T4", "scope": "bundle",
                                    "label": "host CI: exit 1"}]
        rc, _err, _, _ = self._publish("G1")
        self.assertEqual(rc, 1)
        self.assertFalse(any("push" in c for c in self.calls))

    def test_passing_command_leaves_publish_unchanged(self) -> None:
        # Criterion (b): exit 0 ⇒ the push + PR proceed exactly as today, a stale
        # failure record from an earlier refused attempt is cleared, and the record
        # names the certified base commit (== the commit the push built on).
        d = _bundle(self.cfg, "B1")
        (d / "host-ci.json").write_text("{}", encoding="utf-8")         # stale record
        self.cfg.host_ci_checks = _normalize_host_ci(["true"])
        rc, _err, _, _ = self._publish("B1")
        self.assertEqual(rc, 0)
        self.assertTrue(any("push" in c for c in self.calls))
        self.assertTrue(any(c[:3] == ["gh", "pr", "create"] for c in self.calls))
        record = json.loads((d / "publish.json").read_text(encoding="utf-8"))
        self.assertEqual(record["host_ci_base"], self.SHA)              # certified == pushed
        self.assertFalse((d / "host-ci.json").exists())                 # stale record gone

    def test_undeclared_is_byte_identical(self) -> None:
        # Criterion (c): nothing declared ⇒ no fetch/pin, no worktree work, no record,
        # publish as today.
        d = _bundle(self.cfg, "C1")
        rc, _err, pb, fp = self._publish("C1")                          # host_ci_checks == []
        self.assertEqual(rc, 0)
        pb.assert_not_called()                                          # zero extra work
        fp.assert_not_called()
        self.assertTrue(any("push" in c for c in self.calls))
        self.assertFalse((d / "host-ci.json").exists())
        record = json.loads((d / "publish.json").read_text(encoding="utf-8"))
        self.assertNotIn("host_ci_base", record)

    def test_unpinnable_base_fails_closed(self) -> None:
        # The base the push would build on cannot be fetched/resolved ⇒ refuse — never
        # certify some other tree, never push content the declared CI never saw.
        d = _bundle(self.cfg, "N1")
        self.cfg.host_ci_checks = _normalize_host_ci(["true"])
        rc, err, _, _ = self._publish(
            "N1", pinned=mock.MagicMock(return_value=("", "`git fetch origin` failed")))
        self.assertEqual(rc, 1)
        self.assertFalse(any("push" in c for c in self.calls))
        self.assertIn("pin", err)
        self.assertTrue((d / "host-ci.json").exists())

    def test_worktree_error_fails_closed(self) -> None:
        # A patch that no longer applies onto the fetched base raises WorktreeError —
        # refuse, never push a tree that cannot be shown to match base + patch.diff.
        _bundle(self.cfg, "N2")
        self.cfg.host_ci_checks = _normalize_host_ci(["true"])
        boom = mock.MagicMock(side_effect=worktree.WorktreeError("patch did not apply"))
        rc, err, _, _ = self._publish("N2", for_publish=boom)
        self.assertEqual(rc, 1)
        self.assertFalse(any("push" in c for c in self.calls))
        self.assertIn("patch did not apply", err)

    def test_stacked_onto_branch_path_is_gated_too(self) -> None:
        # The `Onto branch` path pushes as well — the gate must sit before its push.
        _bundle(self.cfg, "S1", brief_body=_STACK_BRIEF)
        self.cfg.host_ci_checks = _normalize_host_ci(["exit 1"])
        rc, _err, _, _ = self._publish(
            "S1", existing_pr="https://example.org/pr/1")
        self.assertEqual(rc, 1)
        # Refused before any mutating git step ran (the mocked runner records all).
        self.assertFalse(any("push" in c or "apply" in c or "commit" in c
                             for c in self.calls))


class HostCiCheckLeg(unittest.TestCase):
    """The Check leg: host-CI rows land in the gates matrix (→ check-gates / SUMMARY §6
    via the existing gating-fail routing), honest-unverifiable without a patched tree."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = _cfg(self.tmp)

    def _gate_bundle(self, iid: str) -> Path:
        d = self.cfg.bundle(iid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text(_FIX_BRIEF, encoding="utf-8")
        (d / "patch.diff").write_text("", encoding="utf-8")
        return d

    def test_rows_are_unverifiable_without_a_patched_tree(self) -> None:
        # No git target here ⇒ no reconstructable tree: the row defers to the human
        # (§6) instead of running somewhere wrong — overall stays non-fail.
        d = self._gate_bundle("G1")
        self.cfg.host_ci_checks = _normalize_host_ci(["true"])
        with redirect_stderr(io.StringIO()):
            result = gates.run_gates(d, self.cfg)
        row = next(r for r in result["rows"] if r["rule_id"] == "host-ci-0")
        self.assertEqual(row["result"], "unverifiable")
        self.assertEqual(row["element"], "T4")
        self.assertTrue(row["gating"])
        self.assertEqual(result["overall"], "pass")   # unverifiable ≠ fail (→ §6 instead)

    def test_no_bundle_skips_host_ci(self) -> None:
        # The CI working-tree re-gate has no patch context — and the host's own CI runs
        # these jobs there for real. No host-CI row appears.
        self.cfg.host_ci_checks = _normalize_host_ci(["true"])
        with redirect_stderr(io.StringIO()):
            result = gates.run_working_tree(self.cfg)
        self.assertFalse(any(r["rule_id"].startswith("host-ci") for r in result["rows"]))

    def test_undeclared_keeps_the_stub_matrix(self) -> None:
        # Criterion (c) at Check: with nothing configured at all, the offline stub
        # matrix is exactly what it is today.
        d = self._gate_bundle("G2")
        result = gates.run_gates(d, self.cfg)
        self.assertEqual(result["overall"], "pass")
        self.assertTrue(all(r["rule_id"].endswith("-stub") or r["rule_id"] == ""
                            for r in result["rows"]))


class HostCiPatchedTree(unittest.TestCase):
    """Real-git leg: the publish gate fetches, PINS the exact base commit the push will
    build on, runs the command against that base + patch.diff, and builds the pushed
    branch on the same commit — so a check that only fails when the patch's (or the
    advanced base's) content is present proves both the pre-apply blindness (wyrd) and
    the stale-base certification (iteration-1 C5) are closed."""

    MARKER = "teh_typo_marker"
    _PATCH = (
        "diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n"
        "@@ -1 +1,2 @@\n base\n+" + MARKER + "\n"
    )
    _BENIGN_PATCH = (
        "diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n"
        "@@ -1 +1,2 @@\n base\n+benign\n"
    )

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = _cfg(self.tmp)
        self.repo = self.tmp / "checkout"
        origin = self.tmp / "origin.git"
        sp.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        sp.run(["git", "clone", "-q", str(origin), str(self.repo)], check=True)
        run = lambda *a: sp.run(["git", "-C", str(self.repo), *a], check=True,
                                capture_output=True)
        run("config", "user.email", "t@example.com")
        run("config", "user.name", "T")
        run("config", "commit.gpgsign", "false")
        (self.repo / "file.txt").write_text("base\n", encoding="utf-8")
        run("add", "-A"); run("commit", "-q", "-m", "base")
        run("branch", "-M", "main"); run("push", "-q", "-u", "origin", "main")
        self.cfg.repo_checkouts = {"example-org/example-repo": str(self.repo)}
        self.d = _bundle(self.cfg, "RG")
        (self.d / "patch.diff").write_text(self._PATCH, encoding="utf-8")

    def _pushed_heads(self) -> str:
        return sp.run(["git", "-C", str(self.repo), "ls-remote", "--heads", "origin"],
                      capture_output=True, text=True).stdout

    def _advance_origin(self, fname: str, content: str) -> str:
        """Advance the REMOTE main by one commit the local checkout has NOT fetched
        (a second clone commits + pushes); returns the new tip SHA."""
        side = self.tmp / ("side-" + fname.replace(".", "-"))
        sp.run(["git", "clone", "-q", "-b", "main", str(self.tmp / "origin.git"),
                str(side)], check=True)
        run = lambda *a: sp.run(["git", "-C", str(side), *a], check=True,
                                capture_output=True)
        run("config", "user.email", "t@example.com")
        run("config", "user.name", "T")
        run("config", "commit.gpgsign", "false")
        (side / fname).write_text(content, encoding="utf-8")
        run("add", "-A"); run("commit", "-q", "-m", f"advance: {fname}")
        run("push", "-q", "origin", "main")
        return sp.run(["git", "-C", str(side), "rev-parse", "HEAD"],
                      capture_output=True, text=True).stdout.strip()

    def test_typo_arriving_in_the_patch_blocks_the_push(self) -> None:
        # The wyrd shape end-to-end: the "typo" exists ONLY in patch.diff, so a check
        # run pre-apply (the old T4 seam) passes — only a run against the patched tree
        # can catch it. Publish must refuse with nothing pushed.
        check = f"! grep -rq {self.MARKER} ."
        self.cfg.host_ci_checks = _normalize_host_ci([check])
        # Sanity: the same command is GREEN against the unpatched checkout — the exact
        # blindness of the pre-apply seam.
        self.assertEqual(sp.run(check, shell=True, cwd=self.repo).returncode, 0)
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            rc = publish.publish(self.cfg, "RG", open_pr=False, by="T", today="2026-07-31")
        self.assertEqual(rc, 1, buf.getvalue())
        self.assertNotIn("fix/RG-my-fix", self._pushed_heads())   # no branch pushed
        record = json.loads((self.d / "host-ci.json").read_text(encoding="utf-8"))
        self.assertIn(self.MARKER, json.dumps(record))            # command named
        base = sp.run(["git", "-C", str(self.tmp / "origin.git"), "rev-parse", "main"],
                      capture_output=True, text=True).stdout.strip()
        self.assertEqual(record["base"], base)                    # …and the judged base
        self.assertFalse((self.d / "publish.json").exists())

    def test_base_advanced_since_check_blocks_the_push(self) -> None:
        # Iteration-1 C5: the remote base advances AFTER the fix was built/Checked; the
        # local checkout has not fetched, so its origin/main is stale. A warm no-fetch
        # reconstruction certified stale base + patch (green) while the push then
        # fetched and built the branch on the ADVANCED base whose content fails the
        # declared command — gate green, pushed tree red. The gate must fetch + pin
        # the commit the push will use, judge THAT tree, and refuse.
        advanced = self._advance_origin("typos.txt", self.MARKER + "\n")
        (self.d / "patch.diff").write_text(self._BENIGN_PATCH, encoding="utf-8")
        check = f"! grep -rq {self.MARKER} ."
        self.cfg.host_ci_checks = _normalize_host_ci([check])
        # Sanity — the stale-base blindness: the command is GREEN in the unfetched
        # checkout (the fault exists only on the advanced remote base).
        self.assertEqual(sp.run(check, shell=True, cwd=self.repo).returncode, 0)
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            rc = publish.publish(self.cfg, "RG", open_pr=False, by="T", today="2026-07-31")
        self.assertEqual(rc, 1, buf.getvalue())
        self.assertNotIn("fix/RG-my-fix", self._pushed_heads())   # nothing pushed
        record = json.loads((self.d / "host-ci.json").read_text(encoding="utf-8"))
        self.assertEqual(record["base"], advanced)                # judged the FETCHED base
        self.assertIn(self.MARKER, json.dumps(record))

    def test_push_builds_on_the_certified_commit_not_a_later_base(self) -> None:
        # The PIN: the base advances BETWEEN the gate's certification and the push's
        # own fetch. Without the pin, `checkout -B` re-resolves origin/main at push
        # time and publishes a tree the gate never saw (content-red here); with it,
        # the pushed branch is built on exactly the certified commit.
        (self.d / "patch.diff").write_text(self._BENIGN_PATCH, encoding="utf-8")
        self.cfg.host_ci_checks = _normalize_host_ci([f"! grep -rq {self.MARKER} ."])
        real = worktree.for_publish
        seen: dict[str, str] = {}

        def certify_then_advance(d, primary, base_commit):
            seen["sha"] = base_commit
            tree = real(d, primary, base_commit)
            self._advance_origin("late.txt", self.MARKER + "\n")  # after certification
            return tree

        buf = io.StringIO()
        with mock.patch.object(publish.worktree, "for_publish",
                               side_effect=certify_then_advance), \
             redirect_stdout(buf), redirect_stderr(buf):
            rc = publish.publish(self.cfg, "RG", open_pr=False, by="T", today="2026-07-31")
        self.assertEqual(rc, 0, buf.getvalue())
        origin = str(self.tmp / "origin.git")
        parent = sp.run(["git", "-C", origin, "rev-parse", "fix/RG-my-fix^"],
                        capture_output=True, text=True).stdout.strip()
        self.assertEqual(parent, seen["sha"])                     # built on the certified commit…
        files = sp.run(["git", "-C", origin, "ls-tree", "-r", "--name-only",
                        "fix/RG-my-fix"], capture_output=True, text=True).stdout
        self.assertNotIn("late.txt", files)                       # …not on the later base
        pj = json.loads((self.d / "publish.json").read_text(encoding="utf-8"))
        self.assertEqual(pj["host_ci_base"], seen["sha"])

    def test_clean_patch_passes_and_publishes(self) -> None:
        # Criterion (b) with the real mechanics: a green host CI leaves the push
        # intact, and the pushed commit's parent IS the certified base commit.
        self.cfg.host_ci_checks = _normalize_host_ci(["! grep -rq no_such_marker ."])
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            rc = publish.publish(self.cfg, "RG", open_pr=False, by="T", today="2026-07-31")
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertIn("fix/RG-my-fix", self._pushed_heads())      # pushed as today
        self.assertFalse((self.d / "host-ci.json").exists())
        origin = str(self.tmp / "origin.git")
        base = sp.run(["git", "-C", origin, "rev-parse", "main"],
                      capture_output=True, text=True).stdout.strip()
        parent = sp.run(["git", "-C", origin, "rev-parse", "fix/RG-my-fix^"],
                        capture_output=True, text=True).stdout.strip()
        pj = json.loads((self.d / "publish.json").read_text(encoding="utf-8"))
        self.assertEqual(pj["host_ci_base"], base)
        self.assertEqual(parent, base)                            # certified == pushed

    def test_check_gate_row_runs_in_the_patched_worktree(self) -> None:
        # The Check leg runs FROM the reconstructed worktree: the command sees the
        # patched content and its cwd is the lane tree, not cfg.root / the primary.
        self.cfg.host_ci_checks = _normalize_host_ci(
            [f'grep -q {self.MARKER} file.txt && pwd > "$PDCA_BUNDLE/hostci-cwd"'])
        with redirect_stderr(io.StringIO()):
            result = gates.run_gates(self.d, self.cfg)
        row = next(r for r in result["rows"] if r["rule_id"] == "host-ci-0")
        self.assertEqual(row["result"], "pass", row)
        ran_in = Path((self.d / "hostci-cwd").read_text(encoding="utf-8").strip())
        self.assertEqual(ran_in.resolve(), (self.tmp / "checkout.pdca-wt").resolve())

    def test_check_gate_red_fails_overall(self) -> None:
        # A red host-CI row is GATING: overall fails, and the #166 routing then lands
        # it in SUMMARY §6 where sign-off must see it.
        self.cfg.host_ci_checks = _normalize_host_ci(["false"])
        with redirect_stderr(io.StringIO()):
            result = gates.run_gates(self.d, self.cfg)
        row = next(r for r in result["rows"] if r["rule_id"] == "host-ci-0")
        self.assertEqual(row["result"], "fail")
        self.assertEqual(result["overall"], "fail")


if __name__ == "__main__":
    unittest.main()
