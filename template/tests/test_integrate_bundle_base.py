"""A bundle's own base: exactly the fetched base plus a chosen set of bundles (#723).

``integrate.fold`` could only fold onto the run's one line per target: it names its
destination itself (``integration_branch``) and every accepted bundle of the target lands
there. #463's dependency-scoped bases need a branch holding exactly the bundles one bundle
builds on. These cases pin ``integrate.build_bundle_base``: what the branch holds, its name,
how it is pushed, that every safeguard of the run's fold applies to it (a clean merge, a
gone branch judged as the fold judges it, no revert brought in by a member's branch), that
a rewritten carried member stops it (#633), and that it never holds work outside the set.

Work outside the set is judged by each member's cut point, as recorded when it was
published: its ``stack-base-tip`` when it has a ``stack-base`` record, nothing for a member
never stacked. A member's own work is each non-merge commit its head reaches that neither
the fetched base nor its cut point does. A result holding any other non-merge commit above
the base is refused, naming the commit, the member that brings it, and each excluded bundle
whose head holds it, or saying no bundle of the run owns it. Rebuilding or moving any ref
(the run's line, an excluded bundle's branch) cannot change that: no integration ref on
origin is read. A member whose cut point cannot be trusted is refused: a stack base with no
tip, a tip its branch does not hold, an ``Onto branch`` record, or one published stacked
with no stack base (its ``publish.json`` says ``stacked-pr``, or its brief ``Stacks on:``).
A bundle outside the set cut from the run's line never makes a member's commits its own,
and work merged into a member's branch by hand (an update from the base, a topic branch)
is the member's. Each refusal comes before anything is pushed.

Real git: the ``StackFoldGit`` fixture of tests/test_integrate_stack_bases.py (a bare
``origin``, a primary checkout and a ``human`` clone), loaded by path and used as a helper
object, as tests/test_integrate_revert_via_branch.py does. It is never subclassed, which
would re-run that suite's tests here. Cut points are recorded with
``publish.write_stack_base``. Only modules are imported, and the new entry points are
reached inside each test body, so with the production change reverted these cases still
load and run, and fail.
    cd template && PYTHONPATH=src python3 -m unittest tests.test_integrate_bundle_base
"""

from __future__ import annotations

import importlib.util
import io
import json
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from pdca_harness import integrate, publish

_SPEC = importlib.util.spec_from_file_location(
    "stack_bases_fixture", Path(__file__).with_name("test_integrate_stack_bases.py"))
sb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sb)

# The bundles the run was asked to drive: what scopes the run's line (#591).
BATCH = ["A", "B", "C", "D", "E", "M", "O", "P", "Q", "X", "Y", "Z", "12"]


class BundleBase(unittest.TestCase):

    def setUp(self) -> None:
        self.fx = sb.StackFoldGit()          # the fixture's real-git setUp/tearDown and helpers
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.cfg = self.fx.cfg
        self.line = integrate.integration_branch(self.cfg, "main", BATCH)

    # -- helpers -------------------------------------------------------------------------

    def _build(self, d: Path, members: list[Path], *, carried=None, excluded=None):
        """``integrate.build_bundle_base``, looked up here, so a tree without it fails this
        case, not the module's import."""
        return integrate.build_bundle_base(self.cfg, d, members, batch=BATCH,
                                           carried=carried or {}, excluded=excluded or {})

    def _name(self, iid: str) -> str:
        """The base branch of bundle ``iid`` (a plain id: nothing for the name to escape)."""
        return f"{self.line}-b{iid}"

    def _refused(self, d: Path, members: list[Path], **kw) -> tuple[str, list[str]]:
        """The build of ``d``'s base raises ``IntegrationError`` and pushes nothing: no
        ``git push`` ran and origin has no base for ``d``. Returns the message and every git
        step the build ran through ``integrate._git``."""
        steps: list[str] = []
        real = integrate._git

        def spy(repo: Path, *args: str) -> int:
            steps.append(args[0])
            return real(repo, *args)

        with mock.patch.object(integrate, "_git", spy), redirect_stderr(io.StringIO()), \
                self.assertRaises(integrate.IntegrationError) as ctx:
            self._build(d, members, **kw)
        self.assertNotIn("push", steps, "the build pushed")
        self.assertFalse(self.fx._has(self._name(d.name.removeprefix("issue_"))),
                         "a refused base reached origin")
        return str(ctx.exception), steps

    def _dest(self, iid: str) -> Path:
        """A bundle a base is built for (it needs only its brief's target)."""
        d = self.cfg.bundle(iid)
        sb._brief(d)
        return d

    def _publish2(self, iid: str, first: dict[str, str], second: dict[str, str]) -> Path:
        """Publish ``fix/<iid>`` off ``origin/main`` with TWO commits: ``first``, then
        ``second`` on top."""
        d = self.fx._publish(iid, first)
        self.fx._push_from_human(second, to=f"fix/{iid}", off=f"origin/fix/{iid}")
        return d

    def _commits(self, rng: str) -> list[str]:
        return sb._git(self.fx.origin, "rev-list", rng).split()

    def _on_human(self, *steps: tuple[str, ...]) -> None:
        """Run git ``steps`` in the ``human`` clone, after fetching origin."""
        sb._git(self.fx.human, "fetch", "-q", "origin")
        for step in steps:
            sb._git(self.fx.human, *step)

    def _merge_into(self, branch: str, rev: str) -> None:
        """``branch``'s author merges ``rev`` into it (``--no-ff``) and pushes it."""
        self._on_human(("checkout", "-q", "-B", branch, f"origin/{branch}"),
                       ("merge", "-q", "--no-ff", "--no-edit", rev),
                       ("push", "-q", "origin", branch))

    def _wave0(self, carried: dict[str, str] | None = None) -> tuple[Path, Path, str]:
        """Wave 0 = A, Z (unrelated, two commits each on their own files), folded onto the
        run's line by its first fold; ``carried`` gets the run's record of the heads the line
        took. Returns (A, Z, the line tip)."""
        a = self._publish2("A", {"a1.txt": "a1\n"}, {"a2.txt": "a2\n"})
        z = self._publish2("Z", {"z1.txt": "z1\n"}, {"z2.txt": "z2\n"})
        integrate.fold(self.cfg, [a, z], batch=BATCH, folded_this_run={},
                       **({} if carried is None else {"carried": carried}))
        return a, z, self.fx._tip(self.line)

    def _x_on_the_line(self) -> tuple[str, str]:
        """X publishes x1 and the run's first fold puts it on the line. Returns (x1, the
        line tip L1: a line commit holding X's work)."""
        x = self.fx._publish("X", {"x.txt": "x\n"})
        integrate.fold(self.cfg, [x], batch=BATCH, folded_this_run={})
        return self.fx._tip("fix/X"), self.fx._tip(self.line)

    def _p_cut_from_the_line(self) -> Path:
        """P's branch cut from the run's line as it is now (publish records it
        ``stacked-pr``). No cut point is recorded here."""
        return self.fx._publish("P", {"p.txt": "p\n"}, cut_from=f"origin/{self.line}")

    def _rebuild_the_line_without_x(self) -> str:
        """A later fresh fold of the same batch replaces the line with one holding Z only
        (the run's first fold force-pushes). Returns its tip."""
        z = self.fx._publish("Z", {"z.txt": "z\n"})
        integrate.fold(self.cfg, [z], batch=BATCH, folded_this_run={})
        return self.fx._tip(self.line)

    def _assert_a_only(self, branch: str, tip: str, *, count: int) -> None:
        """B's base is on origin at ``tip``, holds every commit of A's branch above main
        (``count`` of them) and nothing of C's or Z's."""
        self.assertEqual(self.fx._tip(branch), tip, "the base was not pushed to origin")
        a_commits = self._commits("refs/heads/main..refs/heads/fix/A")
        self.assertEqual(len(a_commits), count)
        for c in a_commits:
            self.assertTrue(self.fx._ancestor(c, tip), f"A's {c} is missing")
        self.assertFalse(self.fx._ancestor(self.fx._tip("fix/C"), tip), "C's work leaked in")
        for c in self._commits("refs/heads/main..refs/heads/fix/Z"):
            self.assertFalse(self.fx._ancestor(c, tip), f"Z's {c} leaked in")

    # -- (1) exact contents ----------------------------------------------------------------

    def test_a_bundle_base_holds_exactly_the_fetched_base_and_its_members(self) -> None:
        record: dict[str, str] = {}
        a, z, line_tip = self._wave0(record)
        kept = dict(record)
        moved = self.fx._push_from_human({"later.txt": "later\n"}, to="main")  # main moves on
        branch, tip = self._build(self._dest("B"), [a], carried=record,
                                  excluded={"issue_Z": self.fx._tip("fix/Z")})
        self.assertEqual(branch, self._name("B"))
        self.assertEqual(self.fx._tip(branch), tip, "the base was not pushed to origin")
        a_commits = self._commits("refs/heads/main..refs/heads/fix/A")
        z_commits = self._commits("refs/heads/main..refs/heads/fix/Z")
        self.assertEqual((len(a_commits), len(z_commits)), (2, 2))
        for c in a_commits:
            self.assertTrue(self.fx._ancestor(c, tip), f"A's {c} is missing")
        for c in z_commits:
            self.assertFalse(self.fx._ancestor(c, tip), f"Z's {c} leaked in")
        # Started from origin/main as fetched (after it moved), not from the line's base.
        self.assertEqual(sb._git(self.fx.origin, "rev-parse", f"{tip}^1").strip(), moved)
        self.assertEqual(self.fx._tip(self.line), line_tip, "the run's line moved")
        self.assertEqual(record, kept, "the run's record of its line changed")
        # The run's next fold still continues its own line from the tip it recorded.
        integrate.fold(self.cfg, [a, z], batch=BATCH,
                       folded_this_run={sb.TARGET: line_tip}, carried=record)
        self.assertEqual(self.fx._tip(self.line), line_tip)

    # -- (2) name ----------------------------------------------------------------------------

    def test_the_name_is_the_line_then_b_and_the_flattened_id(self) -> None:
        a, _z, _t = self._wave0()
        expect = f"pdca-integration/main-r{integrate.batch_key(BATCH)}-b12"
        branch, _tip = self._build(self._dest("12"), [a])
        self.assertEqual(branch, expect)
        self.assertEqual(branch, integrate.integration_branch(self.cfg, "main", BATCH) + "-b"
                         + integrate._flatten_base("12"))
        self.assertTrue(self.fx._has(expect))

        def name(base: str, batch: list[str] | None, iid: str) -> str:
            return integrate.bundle_base_branch(self.cfg, base, batch, iid)

        self.assertEqual(name("main", BATCH, "issue_12"), expect)
        ids = [name("main", BATCH, i) for i in ("12", "1-2", "1/2")]
        self.assertEqual(ids, [self.line + "-b12", self.line + "-b1-h2", self.line + "-b1-s2"])
        self.assertEqual(len(set(ids)), 3, ids)
        for n in ids:
            self.assertNotIn("/", n.removeprefix("pdca-integration/"), n)
            self.assertFalse(n.startswith(self.line + "/") or self.line.startswith(n + "/"))
        # `-b` is never in a flattened base, so the name decodes to one (base, batch, id),
        # with a batch or without one.
        for base in ("main", "release-b2", "a-/b", "-b"):
            self.assertNotIn("-b", integrate._flatten_base(base), base)
        pairs = [(("a-b", None, "c"), ("a", None, "b-c")),
                 (("main-b1", BATCH, "2"), ("main", BATCH, "1-b2")),
                 (("main", BATCH, "1"), ("main", None, "1"))]
        for one, other in pairs:
            self.assertNotEqual(name(*one), name(*other), (one, other))

    # -- (3) push rule -----------------------------------------------------------------------

    def test_creating_pushes_plainly_and_building_again_starts_fresh_and_forces(self) -> None:
        a, _z, _t = self._wave0()
        b = self._dest("B")
        first = self.fx._pushes(lambda: self._build(b, [a]))
        self.assertEqual(len(first), 1, first)
        self.assertEqual([x for x in first[0] if x.startswith("--force")], [], first[0])
        self.assertEqual([x for x in first[0] if x.startswith("+")], [], first[0])
        # origin's copy moved on since (an earlier build, of other members): built again, the
        # base starts from origin/main, not from that copy, so the push is forced.
        old = self.fx._push_from_human({"old.txt": "old\n"}, to=self._name("B"), force=True)
        again = self.fx._pushes(lambda: self._build(b, [a]))
        self.assertEqual(len(again), 1, again)
        self.assertTrue([x for x in again[0] if x.startswith("--force")], again[0])
        tip = self.fx._tip(self._name("B"))
        self.assertFalse(self.fx._ancestor(old, tip), "built on origin's old copy")
        self.assertTrue(self.fx._ancestor(self.fx._tip("fix/A"), tip))

    def test_a_refused_force_push_names_the_branch_and_the_remedy(self) -> None:
        a, _z, _t = self._wave0()
        name = self._name("B")
        stale = self.fx._push_from_human({"stale.txt": "s\n"}, to=name)
        sb._git(self.fx.origin, "config", "receive.denyNonFastForwards", "true")
        with self.assertRaises(integrate.IntegrationError) as ctx:
            self._build(self._dest("B"), [a])
        msg = str(ctx.exception)
        for part in (f"could not push {name} to origin", f"delete {name} there",
                     "allow force-pushes on pdca-integration/*"):
            self.assertIn(part, msg)
        self.assertEqual(self.fx._tip(name), stale)

    # -- (4) every fold safeguard ------------------------------------------------------------

    def test_two_members_that_clash_are_both_named(self) -> None:
        a = self.fx._publish("A", {"c.txt": "from a\n"})
        y = self.fx._publish("Y", {"c.txt": "from y\n"})
        msg, _steps = self._refused(self._dest("D"), [a, y])
        for part in ("issue_Y's branch origin/fix/Y does not merge cleanly onto "
                     f"{self._name('D')} after issue_A (conflicts in c.txt)",
                     "issue_Y and issue_A change the same lines"):
            self.assertIn(part, msg)

    def test_a_clash_with_the_base_names_it_and_says_rebuild_not_declare_or_reorder(
            self) -> None:
        # Alone, and after an unrelated member merged first (judged by a trial merge then).
        a = self.fx._publish("A", {"base.txt": "a's base\n"})
        z = self.fx._publish("Z", {"z.txt": "z\n"})
        self.fx._push_from_human({"base.txt": "main moved\n"}, to="main")
        for members in ([a], [z, a]):
            msg, _steps = self._refused(self._dest("B"), members)
            for part in ("issue_A's branch origin/fix/A does not merge cleanly onto origin/main "
                         "itself (conflicts in base.txt)",
                         "Rebuild issue_A's branch on the new origin/main"):
                self.assertIn(part, msg)
            self.assertNotIn("declare the conflict", msg)
            self.assertNotIn("re-order", msg)

    def test_a_gone_member_not_known_merged_stops_the_build_as_it_stops_the_fold(
            self) -> None:
        a = self.fx._publish("A", {"a.txt": "a\n"})
        self.fx._delete("fix/A")
        gone = "issue_A's published branch origin/fix/A does not resolve after fetching"
        with sb._merged_heads({}):
            msg, _steps = self._refused(self._dest("B"), [a])
            with redirect_stderr(io.StringIO()), \
                    self.assertRaises(integrate.IntegrationError) as run:
                integrate.fold(self.cfg, [a], batch=BATCH, folded_this_run={})
        self.assertIn(gone, msg)
        self.assertIn(gone, str(run.exception))

    def test_a_gone_member_whose_pr_merged_into_the_base_is_skipped_as_the_fold_skips_it(
            self) -> None:
        a = self.fx._publish("A", {"a.txt": "a\n"})
        y = self.fx._publish("Y", {"y.txt": "y\n"})
        head = self.fx._tip("fix/A")
        self.fx._merge_pr("A")
        self.fx._delete("fix/A")
        err = io.StringIO()
        with sb._merged_heads({"A": head}), redirect_stderr(err):
            branch, tip = self._build(self._dest("B"), [a, y])
            integrate.fold(self.cfg, [a, y], batch=BATCH, folded_this_run={})
        self.assertEqual(self.fx._tip(branch), tip)
        self.assertTrue(self.fx._ancestor(head, tip), "A's merged work is missing")
        self.assertTrue(self.fx._ancestor(self.fx._tip("fix/Y"), tip))
        for where in (branch, self.line):
            self.assertIn(f"issue_A's branch origin/fix/A is gone and its PR merged at "
                          f"{head[:12]} — {where} already carries that commit; skipped.",
                          err.getvalue())

    def test_a_member_that_brings_in_a_revert_of_an_earlier_members_work_stops_the_build(
            self) -> None:
        # #623 / #673. A adds a.txt. Another branch on origin, `next`, merges A's PR and
        # then reverts it, and B's author updates fix/B from `next` (a merge commit on
        # fix/B), which brings A's commit and the revert along. main has neither, so the
        # base built fresh from it gets a.txt from A, and B's merge would take it off again
        # by a change none of B's own commits makes. The build stops and pushes nothing, and
        # so does the run's fresh fold of the same two: one check, worded for each branch.
        sb._git(self.fx.origin, "branch", "next", "main")
        a = self.fx._publish("A", {"a.txt": "a\n"})
        b = self.fx._publish("B", {"b.txt": "b\n"})
        self.fx._merge_pr("A", into="next")
        self._on_human(("checkout", "-q", "-B", "next", "origin/next"),
                       ("revert", "--no-edit", "-m", "1", "HEAD"),
                       ("push", "-q", "origin", "next"))
        self._merge_into("fix/B", "origin/next")
        self.assertTrue(self.fx._ancestor(self.fx._tip("fix/A"), self.fx._tip("fix/B")))
        self.assertFalse(self.fx._ancestor(self.fx._tip("fix/A"), self.fx._tip("main")))
        msg, _steps = self._refused(self._dest("D"), [a, b])
        for part in (f"merging issue_B's branch origin/fix/B would remove issue_A's work from "
                     f"{self._name('D')}: a.txt (issue_A) would go back",
                     "not one of issue_B's own commits"):
            self.assertIn(part, msg)
        with redirect_stderr(io.StringIO()), \
                self.assertRaises(integrate.IntegrationError) as run:
            integrate.fold(self.cfg, [a, b], batch=BATCH, folded_this_run={})
        self.assertIn(f"merging issue_B's branch origin/fix/B would remove issue_A's work "
                      f"from {self.line}: a.txt (issue_A) would go back", str(run.exception))
        self.assertFalse(self.fx._has(self.line))

    # -- (5) rewritten carried member ------------------------------------------------------

    def test_a_rewritten_carried_member_stops_the_build_before_any_merge(self) -> None:
        record: dict[str, str] = {}
        a, _z, _t = self._wave0(record)
        took = record["issue_A"]
        self.assertEqual(took, self.fx._tip("fix/A"))
        self.fx._push_from_human({"a1.txt": "rewritten\n"}, to="fix/A", force=True)
        for carried in (record, {"A": took}):        # a bundle's name or its id
            msg, steps = self._refused(self._dest("B"), [a], carried=carried)
            for part in ("issue_A's branch origin/fix/A was rewritten", took[:12],
                         "Nothing was merged or pushed"):
                self.assertIn(part, msg)
            self.assertNotIn("merge", steps, "the build merged before it refused")

    # -- (6) no foreign work: ownership by recorded cut point ------------------------------

    def test_6a_a_member_cut_from_a_line_commit_holding_excluded_work_is_refused(
            self) -> None:
        x1, l1 = self._x_on_the_line()
        p = self._p_cut_from_the_line()
        publish.write_stack_base(p, self.line, l1)
        msg, _steps = self._refused(self._dest("D"), [p], excluded={"issue_X": x1})
        for part in (f"would hold {x1[:12]},",
                     f"issue_P's branch origin/fix/P brings it in from below {l1[:12]}",
                     "It is in the published head of issue_X", "Nothing was pushed"):
            self.assertIn(part, msg)
        # `excluded` only names the culprit; it never decides. Without it, the same build is
        # refused all the same.
        msg, _steps = self._refused(self._dest("D"), [p], excluded={})
        self.assertIn(f"would hold {x1[:12]},", msg)
        self.assertIn("No bundle of the run owns it", msg)

    def test_6b_rebuilding_the_line_without_the_excluded_work_changes_nothing(self) -> None:
        x1, l1 = self._x_on_the_line()
        p = self._p_cut_from_the_line()
        publish.write_stack_base(p, self.line, l1)
        p_head = self.fx._tip("fix/P")
        self._rebuild_the_line_without_x()
        self.assertFalse(self.fx._ancestor(x1, self.fx._tip(self.line)), "X is on the line")
        self.assertEqual(publish.read_stack_base_tip(p), l1)
        self.assertEqual(self.fx._tip("fix/P"), p_head)
        msg, _steps = self._refused(self._dest("D"), [p], excluded={"issue_X": x1})
        for part in (f"would hold {x1[:12]},", "issue_P's branch origin/fix/P brings it in",
                     "It is in the published head of issue_X"):
            self.assertIn(part, msg)

    def test_6c_an_excluded_branch_fast_forwarded_through_a_member_still_owns_x1(
            self) -> None:
        # X publishes x1, P is cut from x1 (its recorded tip), and then X's branch is
        # fast-forwarded to P's head and gains x2: X's head now holds P's commit too.
        self.fx._publish("X", {"x1.txt": "x1\n"})
        x1 = self.fx._tip("fix/X")
        p = self.fx._publish("P", {"p.txt": "p\n"}, cut_from="origin/fix/X")
        publish.write_stack_base(p, self.line, x1)
        x2 = self.fx._push_from_human({"x2.txt": "x2\n"}, to="fix/X", off="origin/fix/P")
        self.assertEqual(self.fx._tip("fix/X"), x2)
        self.assertTrue(self.fx._ancestor(self.fx._tip("fix/P"), x2))
        msg, _steps = self._refused(self._dest("D"), [p], excluded={"issue_X": x2})
        for part in (f"would hold {x1[:12]},",
                     f"issue_P's branch origin/fix/P brings it in from below {x1[:12]}",
                     "It is in the published head of issue_X"):
            self.assertIn(part, msg)
        self.assertNotIn("more commit", msg)          # P's own commit is not counted

    def test_6d_work_below_a_cut_point_no_bundle_of_the_run_published_is_refused(
            self) -> None:
        k = self.fx._push_from_human({"k.txt": "k\n"}, to="stray")   # no bundle's
        p = self.fx._publish("P", {"p.txt": "p\n"}, cut_from="origin/stray")
        publish.write_stack_base(p, self.line, k)
        self.fx._publish("X", {"x.txt": "x\n"})
        msg, _steps = self._refused(self._dest("D"), [p],
                                    excluded={"issue_X": self.fx._tip("fix/X")})
        for part in (f"would hold {k[:12]},", "issue_P's branch origin/fix/P brings it in",
                     "No bundle of the run owns it"):
            self.assertIn(part, msg)
        self.assertNotIn("issue_X", msg)

    def test_6e_i_a_stack_base_with_no_tip_is_refused(self) -> None:
        x1, _l1 = self._x_on_the_line()
        p = self._p_cut_from_the_line()
        publish.write_stack_base(p, self.line)       # the record, with no tip
        self.assertEqual(publish.read_stack_base_tip(p), "")   # what the tip alone shows
        msg, _steps = self._refused(self._dest("D"), [p], excluded={"issue_X": x1})
        for part in (f"issue_P has a recorded stack base ({self.line}) but no recorded tip",
                     "Nothing was merged or pushed", "Re-publish issue_P"):
            self.assertIn(part, msg)

    def test_6e_ii_a_recorded_tip_its_branch_does_not_hold_is_refused(self) -> None:
        # P was cut from L1, which holds X. A later run's fresh line, without X, is then
        # recorded as P's stack base, and P is not published again: its recorded tip is not
        # an ancestor of its head, and below it X's commit would pass for P's own.
        x1, _l1 = self._x_on_the_line()
        p = self._p_cut_from_the_line()
        l2 = self._rebuild_the_line_without_x()
        publish.write_stack_base(p, self.line, l2)
        self.assertFalse(self.fx._ancestor(l2, self.fx._tip("fix/P")))
        msg, _steps = self._refused(self._dest("D"), [p], excluded={"issue_X": x1})
        for part in (f"issue_P's recorded cut point {l2[:12]}",
                     "is not in its branch origin/fix/P", "Re-publish issue_P"):
            self.assertIn(part, msg)

    def test_6e_iii_an_onto_branch_member_is_refused(self) -> None:
        # O was committed onto X's PR branch (`Onto branch`): its branch is X's.
        self.fx._publish("X", {"x.txt": "x\n"})
        o1 = self.fx._push_from_human({"o.txt": "o\n"}, to="fix/X", off="origin/fix/X")
        o = self.cfg.bundle("O")
        sb._brief(o, extra="- **Onto branch:** origin/fix/X\n")
        (o / "patch.diff").write_text(sb._new_file_patch("o.txt", "o"), encoding="utf-8")
        (o / "publish.json").write_text(json.dumps(
            {"mode": "stacked", "branch": "fix/X", "base": "origin/fix/X", "repo": "org/repo"}),
            encoding="utf-8")
        self.assertEqual(integrate._published_ref(o), ("origin", "origin/fix/X", True))
        msg, _steps = self._refused(self._dest("D"), [o], excluded={"issue_X": o1})
        for part in ("issue_O was published as an `Onto branch` commit on another PR's branch "
                     "origin/fix/X", "Nothing was merged or pushed", "Re-publish issue_O"):
            self.assertIn(part, msg)

    def test_6e_iv_a_a_member_published_stacked_whose_record_was_cleared_is_refused(
            self) -> None:
        x1, l1 = self._x_on_the_line()
        p = self._p_cut_from_the_line()
        publish.write_stack_base(p, self.line, l1)
        publish.clear_stack_base(p)                  # as a later run's flow clears it
        self.assertEqual(publish.read_stack_base(p), "")
        rec = json.loads((p / "publish.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["mode"], "stacked-pr")
        msg, _steps = self._refused(self._dest("D"), [p], excluded={"issue_X": x1})
        for part in ('issue_P was published stacked (its publish.json records "mode": '
                     '"stacked-pr") but has no stack-base record',
                     "Nothing was merged or pushed", "Re-publish issue_P"):
            self.assertIn(part, msg)

    def test_6e_iv_b_a_legacy_stacks_on_member_is_refused(self) -> None:
        # P's brief declares `Stacks on: X` and its branch is cut from X's; there is no
        # stack-base. Its publish.json is a plain new-pr one here (older, or written by
        # hand), so only the brief tells it was stacked.
        self.fx._publish("X", {"x.txt": "x\n"})
        x1 = self.fx._tip("fix/X")
        p = self.fx._publish("P", {"p.txt": "p\n"}, cut_from="origin/fix/X")
        sb._brief(p, extra="- **Stacks on:** X\n")
        rec = json.loads((p / "publish.json").read_text(encoding="utf-8"))
        rec["mode"] = "new-pr"
        (p / "publish.json").write_text(json.dumps(rec), encoding="utf-8")
        msg, _steps = self._refused(self._dest("D"), [p], excluded={"issue_X": x1})
        for part in ("issue_P was published stacked (its brief declares `Stacks on: X`) but "
                     "has no stack-base record", "Nothing was merged or pushed",
                     "Re-publish issue_P"):
            self.assertIn(part, msg)
        self.assertNotIn("stacked-pr", msg)

    def test_6f_an_excluded_bundle_cut_from_the_line_never_claims_a_members_work(
            self) -> None:
        a, _z, line_tip = self._wave0()
        c = self.fx._publish("C", {"c.txt": "c\n"}, cut_from=f"origin/{self.line}")
        publish.write_stack_base(c, self.line, line_tip)
        self.assertTrue(self.fx._ancestor(self.fx._tip("fix/A"), self.fx._tip("fix/C")))
        excluded = {"issue_C": self.fx._tip("fix/C"), "issue_Z": self.fx._tip("fix/Z")}
        b = self._dest("B")
        branch, tip = self._build(b, [a], excluded=excluded)
        self._assert_a_only(branch, tip, count=2)
        self.fx._push_from_human({"a3.txt": "a3\n"}, to="fix/A", off="origin/fix/A")
        branch, tip = self._build(b, [a], excluded=excluded)
        self._assert_a_only(branch, tip, count=3)

    def test_6g_an_update_from_the_base_and_a_topic_merged_by_hand_are_the_members_own(
            self) -> None:
        a = self.fx._publish("A", {"a1.txt": "a1\n"})
        moved = self.fx._push_from_human({"m.txt": "m\n"}, to="main")
        self.fx._update_branch("A")                  # origin/main merged into fix/A
        topic = self.fx._push_from_human({"t.txt": "t\n"}, to="topic-a")   # no bundle's
        self._merge_into("fix/A", "origin/topic-a")
        branch, tip = self._build(self._dest("B"), [a])
        self.assertEqual(self.fx._tip(branch), tip)
        for c in (self.fx._tip("fix/A"), topic, moved):
            self.assertTrue(self.fx._ancestor(c, tip), c)

    def test_6h_a_member_cut_from_a_base_of_another_members_work_is_built_on(self) -> None:
        # Q is a wave-0 member. M's own base is built from [Q], and M is cut from it, with
        # that base's tip recorded as M's cut point. D builds on both.
        q = self.fx._publish("Q", {"q.txt": "q\n"})
        m_base, m_tip = self._build(self._dest("M"), [q])
        m = self.fx._publish("M", {"m.txt": "m\n"}, cut_from=f"origin/{m_base}")
        publish.write_stack_base(m, m_base, m_tip)
        branch, tip = self._build(self._dest("D"), [q, m])
        self.assertEqual(self.fx._tip(branch), tip)
        for iid in ("Q", "M"):
            self.assertTrue(self.fx._ancestor(self.fx._tip(f"fix/{iid}"), tip), iid)

    def test_6h_without_the_member_whose_work_is_below_the_cut_point_it_is_refused(
            self) -> None:
        # The same M, built on without Q: Q's commit is below M's cut point, so it is not M's.
        q = self.fx._publish("Q", {"q.txt": "q\n"})
        q1 = self.fx._tip("fix/Q")
        m_base, m_tip = self._build(self._dest("M"), [q])
        m = self.fx._publish("M", {"m.txt": "m\n"}, cut_from=f"origin/{m_base}")
        publish.write_stack_base(m, m_base, m_tip)
        msg, _steps = self._refused(self._dest("E"), [m], excluded={"issue_Q": q1})
        for part in (f"would hold {q1[:12]},",
                     f"issue_M's branch origin/fix/M brings it in from below {m_tip[:12]}",
                     "It is in the published head of issue_Q"):
            self.assertIn(part, msg)

    # -- what the caller names ---------------------------------------------------------------

    def test_an_excluded_head_this_clone_lacks_never_decides_and_is_said_unchecked(
            self) -> None:
        a, _z, _t = self._wave0()
        branch, tip = self._build(self._dest("B"), [a], excluded={"issue_Y": sb.ABSENT})
        self.assertEqual(self.fx._tip(branch), tip)
        x1, l1 = self._x_on_the_line()
        p = self._p_cut_from_the_line()
        publish.write_stack_base(p, self.line, l1)
        msg, _steps = self._refused(self._dest("D"), [p], excluded={"issue_Y": sb.ABSENT})
        self.assertIn(f"would hold {x1[:12]},", msg)
        self.assertIn("this clone does not have the published head of issue_Y", msg)

    def test_a_member_also_listed_as_excluded_is_a_member(self) -> None:
        a, _z, _t = self._wave0()
        branch, tip = self._build(self._dest("B"), [a],
                                  excluded={"A": self.fx._tip("fix/A"),
                                            "Z": self.fx._tip("fix/Z")})
        self.assertEqual(self.fx._tip(branch), tip)
        self.assertTrue(self.fx._ancestor(self.fx._tip("fix/A"), tip))
        self.assertFalse(self.fx._ancestor(self.fx._tip("fix/Z"), tip))

    def test_a_member_with_a_patch_but_no_target_stops_the_build_before_any_git_step(
            self) -> None:
        a = self.fx._publish("A", {"a.txt": "a\n"})
        t = self.cfg.bundle("T")
        t.mkdir(parents=True)
        (t / "brief.md").write_text("- **Slug:** t\n", encoding="utf-8")
        (t / "patch.diff").write_text(sb._new_file_patch("t.txt", "t"), encoding="utf-8")
        msg, steps = self._refused(self._dest("B"), [a, t])
        self.assertIn("issue_T has a patch but no usable target", msg)
        self.assertEqual(steps, [], "the build ran git before it refused")


if __name__ == "__main__":
    unittest.main()
