"""Leaf spawn memory bound (issue #420) — stdlib unittest, no systemd, no network.

Every leaf subprocess used to be spawned with no resource bound of any kind, so one
leaf's build footprint could take the whole run down *unattributably* (systemd-oomd
kills the cgroup, not the process — the driver simply vanishes). `_invoke` now wraps
the spawn in the configured bound. What this pins, on the argv actually spawned:

  1. headless path — a configured bound wraps the leaf's argv;
  2. interactive path — same wrapping, and the leaf still inherits the terminal
     (a seeded REPL that loses its tty would be a regression, not a fix);
  3. no bound configured (the default) — byte-for-byte today's argv;
  4. bound configured, host facility absent — byte-for-byte today's argv, and the
     leaf still runs: a documented no-op, never a hard failure.

…and, because the knob is documented as "any `[leaves.*]` table takes `memory_max`",
that the per-leaf override reaches the ARRAY-form leaf tables too — `[[leaves.advisory]]`,
`[[leaves.plan_advisory]]` and the builder variants/escalations, which are built from raw
spec dicts in `leaves.py` rather than by `Config.leaf()` and so are exactly where a new
per-leaf key gets silently dropped (`ArrayFormLeafTables` below).

The spawn is stubbed in every case (the argv is recorded, nothing is executed) and
the host facility is stubbed by patching the availability probe, so this runs
offline on any host, with or without systemd. Run with:
    cd template && PYTHONPATH=src python -m unittest tests.test_leaf_memory_cap
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from pdca_harness import leaves, progress
from pdca_harness.config import Config, LeafConfig

_BOUND = "8G"
_LEAF_ARGV = ["fake-vendor-cli", "-p", "--flag"]
_MISSING = object()


class SimpleCompleted:
    """Stand-in for subprocess.CompletedProcess on the stubbed interactive spawn."""

    returncode = 0


class StubSubprocess:
    """`leaves.subprocess` with only ``run`` replaced; everything else is the real module.

    Swapping the module reference INSIDE `leaves` keeps the stub local to the unit under
    test — `setattr(subprocess, "run", …)` would rebind the stdlib for every other module
    in the interpreter for the duration of the test, and this suite runs inside a
    1500-test `unittest discover`.
    """

    def __init__(self, run) -> None:
        self.run = run

    def __getattr__(self, name: str):
        return getattr(subprocess, name)


def _cfg(root: Path, **kw) -> Config:
    """A Config built exactly as the existing suites build one (test_families.py:22).

    Deliberately NOT `Config(leaf_memory_max=…)`: the bound is `setattr`'d by the tests
    that want one, so this module still constructs a Config — and still reaches the real
    spawn path — on a tree where the field does not exist yet. A constructor kwarg would
    raise TypeError there, which exits non-zero and *looks* red while proving only that a
    symbol is missing.
    """
    return Config(
        root=root,
        bundle_root=root / "results",
        process_dir=root / "process",
        templates_dir=root / "templates",
        default_branch="main",
        tracker_system="github",
        tracker_url="",
        issue_id_example="1",
        builder=LeafConfig(mode="stub"),
        reviewer=LeafConfig(mode="stub"),
        **kw,
    )


class _SpawnRecorder(unittest.TestCase):
    """Base: record what `_invoke` would spawn, execute nothing, enforce nothing."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.spawned: dict = {}
        self.spawns: list[list[str]] = []

        # Stub BOTH spawn shapes: record the argv (and the interactive kwargs), run nothing.
        def fake_run(argv, **kw):  # the interactive branch — leaves.subprocess.run
            self._record(argv, kw)
            return SimpleCompleted()

        def fake_heartbeat(argv, **kw):  # the headless branch
            self._record(argv, kw)
            return 0, "", True

        self._patch(leaves, "subprocess", StubSubprocess(fake_run))
        self._patch(progress, "run_with_heartbeat", fake_heartbeat)
        # Default: pretend the host CAN enforce a bound. The absent-facility test flips it.
        self._patch(leaves, "_memory_cap_supported", lambda argv: True)
        # The facility decision is resolved once per bound per PROCESS (#420 review), so a
        # cached answer from an earlier test would mask the probe this one installs.
        getattr(leaves, "_MEMORY_CAP_DECISION", {}).clear()
        self.addCleanup(getattr(leaves, "_MEMORY_CAP_DECISION", {}).clear)

    def _record(self, argv, kw) -> None:
        self.spawned["argv"] = list(argv)
        self.spawned["kw"] = dict(kw)
        self.spawns.append(list(argv))

    def _patch(self, obj, name: str, value) -> None:
        """Install a stub, tolerating a symbol that does not exist.

        Tolerating it is the point: with the production hunks reverted there is no
        `_memory_cap_supported` to patch, and an AttributeError here would exit
        non-zero while proving only that a symbol is missing. The tests must reach the
        real spawn path on that tree and fail on the argv COMPARISON instead.
        """
        original = getattr(obj, name, _MISSING)
        setattr(obj, name, value)
        if original is _MISSING:
            self.addCleanup(lambda: delattr(obj, name))
        else:
            self.addCleanup(setattr, obj, name, original)

    def _leaf(self, *, interactive: bool = False, **kw) -> LeafConfig:
        return LeafConfig(mode="command", family="generic", argv=list(_LEAF_ARGV),
                          interactive=interactive, **kw)

    def assertWrapped(self, argv: list[str], bound: str, tail: list[str]) -> None:
        """`argv` is `tail` wrapped in a scope capped at `bound` — the whole claim."""
        self.assertNotEqual(argv[:len(tail)], tail,
                            "leaf spawned unwrapped — the bound is not applied")
        self.assertEqual(argv[0], "systemd-run")
        self.assertIn(f"MemoryMax={bound}", argv)
        self.assertIn("--", argv)
        self.assertEqual(argv[argv.index("--") + 1:], tail,
                         "the leaf's own command line must survive the wrapper intact")


class LeafMemoryCap(_SpawnRecorder):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = _cfg(self.tmp)

    def _invoke(self, leaf: LeafConfig) -> list[str]:
        leaves._invoke(leaf, self.tmp, "TASK-PROMPT", cfg=self.cfg)
        return self.spawned["argv"]

    # -- 1. headless: the bound wraps the leaf's argv ------------------------------
    def test_headless_spawn_is_wrapped_in_the_configured_bound(self) -> None:
        setattr(self.cfg, "leaf_memory_max", _BOUND)
        self.assertWrapped(self._invoke(self._leaf()), _BOUND, _LEAF_ARGV)

    # -- 2. interactive: wrapped too, and it keeps the terminal --------------------
    def test_interactive_spawn_is_wrapped_and_keeps_the_terminal(self) -> None:
        setattr(self.cfg, "leaf_memory_max", _BOUND)
        argv = self._invoke(self._leaf(interactive=True))
        # The REPL's argv: the leaf's command line plus its seed positional, in order.
        self.assertWrapped(argv, _BOUND, [*_LEAF_ARGV, "TASK-PROMPT"])
        # A wrapper that took the tty away (a --pty/service unit, a piped stdio) would
        # break the REPL the human types into: stdio stays inherited, as today.
        for stream in ("stdin", "stdout", "stderr", "capture_output"):
            self.assertIsNone(self.spawned["kw"].get(stream),
                              f"interactive leaf lost its inherited {stream}")

    # -- 3. unset (the default): byte-for-byte today's argv ------------------------
    def test_no_bound_configured_spawns_todays_argv_unchanged(self) -> None:
        for interactive in (False, True):
            with self.subTest(interactive=interactive):
                self.spawned.clear()
                argv = self._invoke(self._leaf(interactive=interactive))
                expected = [*_LEAF_ARGV, "TASK-PROMPT"] if interactive else list(_LEAF_ARGV)
                self.assertEqual(argv, expected)

    # -- 4. bound set, facility absent: no-op, and the leaf still runs -------------
    def test_bound_without_the_host_facility_is_a_noop_not_a_failure(self) -> None:
        setattr(self.cfg, "leaf_memory_max", _BOUND)
        self._patch(leaves, "_memory_cap_supported", lambda argv: False)
        for interactive in (False, True):
            with self.subTest(interactive=interactive):
                self.spawned.clear()
                err = io.StringIO()
                with redirect_stderr(err):
                    argv = self._invoke(self._leaf(interactive=interactive))  # no raise
                expected = [*_LEAF_ARGV, "TASK-PROMPT"] if interactive else list(_LEAF_ARGV)
                self.assertEqual(argv, expected)
                # Degraded, but never silently: the run says the bound is not in force.
                # (Only the first spawn probes — the decision is the run's, not the spawn's;
                # `getvalue()` is checked on the leg that resolved it.)
                if not interactive:
                    self.assertIn(_BOUND, err.getvalue())

    # -- the per-leaf override + its opt-out ---------------------------------------
    def test_per_leaf_memory_max_overrides_the_driver_bound(self) -> None:
        setattr(self.cfg, "leaf_memory_max", _BOUND)
        leaf = self._leaf()
        setattr(leaf, "memory_max", "2G")
        argv = self._invoke(leaf)
        self.assertIn("MemoryMax=2G", argv)
        self.assertNotIn(f"MemoryMax={_BOUND}", argv)

    def test_per_leaf_off_opts_out_of_the_driver_bound(self) -> None:
        setattr(self.cfg, "leaf_memory_max", _BOUND)
        leaf = self._leaf()
        setattr(leaf, "memory_max", "off")
        self.assertEqual(self._invoke(leaf), list(_LEAF_ARGV))

    def test_a_host_rejecting_the_rich_properties_still_gets_a_hard_cap(self) -> None:
        # An older systemd (or one without swap accounting) rejects the richest property
        # set; degrading all the way to "unbounded" there would give up the actual cap.
        setattr(self.cfg, "leaf_memory_max", _BOUND)
        self._patch(leaves, "_memory_cap_supported",
                    lambda argv: not any("ManagedOOM" in a or "MemorySwapMax" in a
                                         for a in argv))
        argv = self._invoke(self._leaf())
        self.assertIn(f"MemoryMax={_BOUND}", argv)
        self.assertFalse([a for a in argv if "ManagedOOM" in a or "MemorySwapMax" in a])
        self.assertEqual(argv[argv.index("--") + 1:], _LEAF_ARGV)

    def test_the_host_facility_is_probed_once_per_run_not_once_per_leaf(self) -> None:
        """A per-spawn probe pays a subprocess per leaf and — worse — lets a transient
        hiccup unbound ONE leaf while its siblings stay capped: a run half-bounded is
        exactly the unattributable state the cap exists to remove."""
        setattr(self.cfg, "leaf_memory_max", _BOUND)
        probes: list[list[str]] = []
        self._patch(leaves, "_memory_cap_supported",
                    lambda argv: (probes.append(list(argv)), True)[1])
        for _ in range(4):
            self.assertWrapped(self._invoke(self._leaf()), _BOUND, _LEAF_ARGV)
        self.assertEqual(len(probes), 1, f"probed once per spawn: {len(probes)} probes")

    def test_a_none_cfg_never_crashes_the_spawn(self) -> None:
        # `_invoke`'s cfg is `Config | None`; a bound-less spawn must still work.
        leaves._invoke(self._leaf(), self.tmp, "TASK-PROMPT")
        self.assertEqual(self.spawned["argv"], list(_LEAF_ARGV))


class ArrayFormLeafTables(_SpawnRecorder):
    """`memory_max` on the ARRAY-form leaf tables — the ones built from spec dicts.

    `Config.leaf()` builds the six NAMED `[leaves.*]` tables; `[[leaves.advisory]]`,
    `[[leaves.plan_advisory]]`, `[[leaves.builder_variant]]`, `[[leaves.builder_escalation]]`
    and `[[leaves.sizer_escalation]]` are instead built from raw spec dicts inside
    `leaves.py`, so a per-leaf key added to the named path alone is *silently dropped*
    for them — a documented knob that does nothing. The advisory pool is the case that
    matters most: it is what a run fans out CONCURRENTLY, i.e. the multiplier behind the
    incident this issue is about.
    """

    def _bundle(self, iid: str) -> Path:
        d = self.tmp / "results" / f"issue_{iid}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "brief.md").write_text(f"- **Slug:** {iid}\n- **Defect:** x.\n",
                                    encoding="utf-8")
        return d

    def _spec(self, leaf_id: str, **kw) -> dict:
        return {"id": leaf_id, "mode": "command", "family": "generic",
                "argv": list(_LEAF_ARGV), **kw}

    # -- [[leaves.advisory]] --------------------------------------------------------
    def test_advisory_leaf_honours_its_own_memory_max(self) -> None:
        cfg = _cfg(self.tmp, advisory_leaves=[self._spec("rev", memory_max="2G")])
        setattr(cfg, "leaf_memory_max", _BOUND)
        leaves.run_advisory_leaves(self._bundle("A1"), cfg)
        self.assertWrapped(self.spawned["argv"], "2G", _LEAF_ARGV)

    def test_advisory_leaf_can_opt_out_with_off(self) -> None:
        cfg = _cfg(self.tmp, advisory_leaves=[self._spec("rev", memory_max="off")])
        setattr(cfg, "leaf_memory_max", _BOUND)
        leaves.run_advisory_leaves(self._bundle("A2"), cfg)
        self.assertEqual(self.spawned["argv"], list(_LEAF_ARGV))

    def test_advisory_leaf_without_a_key_still_inherits_the_driver_bound(self) -> None:
        cfg = _cfg(self.tmp, advisory_leaves=[self._spec("rev")])
        setattr(cfg, "leaf_memory_max", _BOUND)
        leaves.run_advisory_leaves(self._bundle("A3"), cfg)
        self.assertWrapped(self.spawned["argv"], _BOUND, _LEAF_ARGV)

    def test_an_unparseable_advisory_bound_is_validated_not_passed_through(self) -> None:
        """Validated by the same `config.memory_max_value` as the named tables: a
        nonsense value degrades to "inherit" with a note, never into a systemd property
        that would make every advisory spawn fail to start."""
        cfg = _cfg(self.tmp, advisory_leaves=[self._spec("rev", memory_max="eight gigs")])
        setattr(cfg, "leaf_memory_max", _BOUND)
        err = io.StringIO()
        with redirect_stderr(err):
            leaves.run_advisory_leaves(self._bundle("A4"), cfg)
        self.assertWrapped(self.spawned["argv"], _BOUND, _LEAF_ARGV)
        self.assertIn("memory_max", err.getvalue())

    # -- [[leaves.plan_advisory]] ---------------------------------------------------
    def test_plan_advisory_leaf_honours_its_own_memory_max(self) -> None:
        cfg = _cfg(self.tmp, plan_advisory_leaves=[self._spec("plan-rev", memory_max="3G")])
        setattr(cfg, "leaf_memory_max", _BOUND)
        leaves.run_plan_advisory(self._bundle("P1"), cfg)
        self.assertWrapped(self.spawned["argv"], "3G", _LEAF_ARGV)

    def test_plan_advisory_leaf_can_opt_out_with_off(self) -> None:
        cfg = _cfg(self.tmp, plan_advisory_leaves=[self._spec("plan-rev", memory_max="off")])
        setattr(cfg, "leaf_memory_max", _BOUND)
        leaves.run_plan_advisory(self._bundle("P2"), cfg)
        self.assertEqual(self.spawned["argv"], list(_LEAF_ARGV))

    # -- [[leaves.builder_variant]] / [[leaves.builder_escalation]] -----------------
    def _variant_cfg(self, *, base_bound: str, spec: dict) -> tuple[Config, Path]:
        builder = self._leaf()
        setattr(builder, "memory_max", base_bound)
        cfg = _cfg(self.tmp, builder_variants=[spec])
        cfg.builder = builder
        d = self._bundle("V")
        (d / "brief.md").write_text("- **Slug:** v\n- **Difficulty:** high\n",
                                    encoding="utf-8")
        return cfg, d

    def test_a_builder_variant_inherits_the_base_leafs_bound(self) -> None:
        """A variant is the same appetite as the leaf it varies (it usually differs only
        in `argv`/model), so losing the base leaf's cap is how the hungriest builder ends
        up the one unbounded."""
        cfg, d = self._variant_cfg(
            base_bound="2G",
            spec={"argv": ["variant-cli"], "when": {"field": "difficulty",
                                                    "substring": "high"}})
        leaf = leaves.select_builder(d, cfg, 1)
        leaves._invoke(leaf, self.tmp, "TASK-PROMPT", cfg=cfg)
        self.assertWrapped(self.spawned["argv"], "2G", ["variant-cli"])

    def test_a_builder_variant_inherits_the_base_leafs_off_opt_out(self) -> None:
        cfg, d = self._variant_cfg(
            base_bound="off",
            spec={"argv": ["variant-cli"], "when": {"field": "difficulty",
                                                    "substring": "high"}})
        setattr(cfg, "leaf_memory_max", _BOUND)
        leaf = leaves.select_builder(d, cfg, 1)
        leaves._invoke(leaf, self.tmp, "TASK-PROMPT", cfg=cfg)
        self.assertEqual(self.spawned["argv"], ["variant-cli"])

    def test_a_builder_variants_own_memory_max_wins(self) -> None:
        cfg, d = self._variant_cfg(
            base_bound="2G",
            spec={"argv": ["variant-cli"], "memory_max": "6G",
                  "when": {"field": "difficulty", "substring": "high"}})
        leaf = leaves.select_builder(d, cfg, 1)
        leaves._invoke(leaf, self.tmp, "TASK-PROMPT", cfg=cfg)
        self.assertWrapped(self.spawned["argv"], "6G", ["variant-cli"])

    def test_a_builder_escalation_inherits_the_base_leafs_bound(self) -> None:
        builder = self._leaf()
        setattr(builder, "memory_max", "2G")
        cfg = _cfg(self.tmp, builder_escalation=[{"min_iteration": 2,
                                                  "argv": ["escalated-cli"]}])
        cfg.builder = builder
        leaf = leaves.select_builder(self._bundle("E"), cfg, 2)
        leaves._invoke(leaf, self.tmp, "TASK-PROMPT", cfg=cfg)
        self.assertWrapped(self.spawned["argv"], "2G", ["escalated-cli"])


class ConfigParsing(unittest.TestCase):
    """The public config surface: [driver].leaf_memory_max + [leaves.*].memory_max."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _load(self, extra: str = "", builder_extra: str = "") -> Config:
        (self.tmp / "pdca.toml").write_text(
            '[project]\ndefault_branch = "main"\n'
            '[leaves.builder]\nmode = "stub"\n' + builder_extra +
            '[leaves.reviewer]\nmode = "stub"\n' + extra, encoding="utf-8")
        return Config.load(self.tmp)

    def test_unset_is_unbounded(self) -> None:
        cfg = self._load("")
        self.assertEqual(getattr(cfg, "leaf_memory_max", ""), "")
        self.assertEqual(getattr(cfg.builder, "memory_max", ""), "")

    def test_driver_and_per_leaf_keys_are_parsed(self) -> None:
        cfg = self._load('[driver]\nleaf_memory_max = "8G"\n')
        self.assertEqual(getattr(cfg, "leaf_memory_max", ""), "8G")
        cfg = self._load(builder_extra='memory_max = "2G"\n')
        self.assertEqual(getattr(cfg.builder, "memory_max", ""), "2G")

    def test_a_nonsense_bound_degrades_to_unbounded_with_a_note(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            cfg = self._load('[driver]\nleaf_memory_max = "eight gigs"\n')
        # Fail-safe direction: today's behaviour, never a guessed number — a WRONG cap
        # kills a run exactly as dead as no cap. And it says so rather than degrading
        # silently (config.py's sweep_worktrees note is the peer).
        self.assertEqual(getattr(cfg, "leaf_memory_max", ""), "")
        self.assertIn("leaf_memory_max", err.getvalue())


if __name__ == "__main__":
    unittest.main()
