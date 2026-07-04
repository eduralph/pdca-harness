"""Family profiles (vendor neutrality): registry resolution, pdca.toml overrides,
role-prompt injection, model/effort mapping, and the generic fake-vendor-CLI path
end-to-end (stdlib unittest, no model CLIs, no network).
"""

from __future__ import annotations

import io
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from pdca_harness import families, leaves
from pdca_harness.config import Config, LeafConfig


def _cfg(root: Path, **kw) -> Config:
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


class Registry(unittest.TestCase):
    def test_claude_builtin_reproduces_the_hardcoded_branches(self) -> None:
        p = families.resolve("claude")
        self.assertEqual(p.stream_argv, ("--output-format", "stream-json", "--verbose"))
        self.assertEqual(p.stream_format, "claude-stream-json")
        self.assertEqual(p.grounding_flag, "--add-dir")
        self.assertEqual(p.role_injection, "flag")
        self.assertEqual(p.agent_flag, "--agent")
        self.assertTrue(p.cwd_discovery)
        self.assertTrue(p.native_guard)

    def test_empty_and_generic_families_are_stdin_no_flags(self) -> None:
        for name in ("", "generic"):
            p = families.resolve(name)
            self.assertEqual(p.stream_argv, (), name)
            self.assertEqual(p.grounding_flag, "", name)
            self.assertFalse(p.cwd_discovery, name)
            self.assertFalse(p.native_guard, name)

    def test_codex_streams_via_json_and_confines_by_cwd(self) -> None:
        p = families.resolve("codex")
        self.assertEqual(p.stream_argv, ("--json",))          # `codex exec --json`
        self.assertEqual(p.stream_format, "codex-stream-json")
        self.assertEqual(p.model_flag, "-m")
        self.assertEqual(p.grounding_flag, "--add-dir")       # writable $PDCA_TARGET grant
        self.assertFalse(p.cwd_discovery)                     # confined to the worktree cwd
        self.assertFalse(p.native_guard)                      # driver `gh` shim, not a hook

    def test_unknown_family_falls_back_to_generic(self) -> None:
        # The ad-hoc families tests/instances already use ("local", "mid", "frontier")
        # must keep today's behavior: no vendor flags at all.
        p = families.resolve("frontier")
        self.assertEqual(p.stream_argv, ())
        self.assertFalse(p.cwd_discovery)

    def test_toml_override_extends_a_builtin_and_declares_a_new_family(self) -> None:
        overrides = {
            "codex": {"grounding_flag": "--cd"},
            "mycli": {"stream_argv": ["--json"], "cwd_discovery": True},
        }
        self.assertEqual(families.resolve("codex", overrides).grounding_flag, "--cd")
        mycli = families.resolve("mycli", overrides)
        self.assertEqual(mycli.stream_argv, ("--json",))  # list → tuple
        self.assertTrue(mycli.cwd_discovery)
        # Untouched fields keep the generic base.
        self.assertEqual(mycli.role_injection, "inline")

    def test_config_load_parses_families_tables(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "pdca.toml").write_text(
            '[project]\ndefault_branch = "main"\n'
            '[leaves.builder]\nmode = "stub"\nfamily = "mycli"\n'
            '[leaves.reviewer]\nmode = "stub"\n'
            '[families.mycli]\ngrounding_flag = "--dir"\n',
            encoding="utf-8",
        )
        cfg = Config.load(tmp)
        self.assertEqual(cfg.profile(cfg.builder).grounding_flag, "--dir")


class Frontmatter(unittest.TestCase):
    def test_strips_yaml_block(self) -> None:
        text = "---\nname: reviewer\ntools: Read\n---\nYou are the reviewer.\n"
        self.assertEqual(families.strip_frontmatter(text), "You are the reviewer.\n")

    def test_no_frontmatter_passes_through(self) -> None:
        self.assertEqual(families.strip_frontmatter("plain body\n"), "plain body\n")

    def test_unterminated_frontmatter_passes_through(self) -> None:
        text = "---\nname: broken\nno closing fence\n"
        self.assertEqual(families.strip_frontmatter(text), text)


class RoleInjection(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = _cfg(self.tmp)
        # The canonical, vendor-neutral body (source of truth) — no frontmatter.
        agents = self.tmp / "agents"
        agents.mkdir(parents=True)
        (agents / "reviewer.md").write_text("ROLE-SENTINEL body.\n", encoding="utf-8")

    def _legacy(self, text: str) -> None:
        """Write the legacy Claude-packaged file (frontmatter + body) an instance rendered
        before the canonical-body split would carry at .claude/agents/<name>.md."""
        legacy = self.tmp / ".claude" / "agents"
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "reviewer.md").write_text(text, encoding="utf-8")

    def test_flag_family_gets_agent_argv(self) -> None:
        leaf = LeafConfig(family="claude", agent="reviewer", argv=["claude", "-p"])
        argv, prefix = leaves._role_injection(self.cfg, leaf, families.resolve("claude"))
        self.assertEqual(argv, ["--agent", "reviewer"])
        self.assertEqual(prefix, "")

    def test_flag_already_in_argv_is_not_duplicated(self) -> None:
        leaf = LeafConfig(family="claude", agent="reviewer",
                          argv=["claude", "-p", "--agent", "reviewer"])
        argv, _ = leaves._role_injection(self.cfg, leaf, families.resolve("claude"))
        self.assertEqual(argv, [])

    def test_inline_family_gets_prompt_prefix_from_canonical_body(self) -> None:
        leaf = LeafConfig(family="codex", agent="reviewer", argv=["codex", "exec"])
        argv, prefix = leaves._role_injection(self.cfg, leaf, families.resolve("codex"))
        self.assertEqual(argv, [])
        self.assertIn("ROLE-SENTINEL", prefix)       # the agents/<name>.md body, inlined

    def test_inline_prefers_canonical_over_legacy(self) -> None:
        # Both present (a not-yet-cleaned instance): the canonical agents/ body wins.
        self._legacy("---\nname: reviewer\n---\nLEGACY-SENTINEL body.\n")
        leaf = LeafConfig(family="codex", agent="reviewer", argv=["codex", "exec"])
        _, prefix = leaves._role_injection(self.cfg, leaf, families.resolve("codex"))
        self.assertIn("ROLE-SENTINEL", prefix)
        self.assertNotIn("LEGACY-SENTINEL", prefix)

    def test_divergent_legacy_warns_it_is_shadowed(self) -> None:
        # #228: a pre-split instance customized the legacy .claude/agents file; the canonical
        # body now wins and would silently drop those edits. A divergent legacy must WARN so
        # the human migrates the customization rather than losing it unnoticed.
        self._legacy("---\nname: reviewer\n---\nCUSTOMIZED-BY-USER body.\n")
        leaf = LeafConfig(family="codex", agent="reviewer", argv=["codex", "exec"])
        err = io.StringIO()
        with redirect_stderr(err):
            _, prefix = leaves._role_injection(self.cfg, leaf, families.resolve("codex"))
        self.assertIn("ROLE-SENTINEL", prefix)                  # canonical still used
        self.assertIn("being ignored", err.getvalue())         # warned it's shadowed
        self.assertIn("migrate", err.getvalue())               # …and to migrate the edits
        self.assertIn("agents/reviewer.md", err.getvalue())

    def test_matching_legacy_is_silent(self) -> None:
        # A legacy file whose body MATCHES the canonical (the normal fresh-render case where
        # both are shipped) must NOT warn — nothing was customized, nothing is being lost.
        self._legacy("---\nname: reviewer\n---\nROLE-SENTINEL body.\n")
        leaf = LeafConfig(family="codex", agent="reviewer", argv=["codex", "exec"])
        err = io.StringIO()
        with redirect_stderr(err):
            leaves._role_injection(self.cfg, leaf, families.resolve("codex"))
        self.assertEqual(err.getvalue(), "")

    def test_inline_falls_back_to_legacy_claude_agents(self) -> None:
        # Back-compat: an instance rendered before the split has only .claude/agents/<name>.md;
        # inline injection reads it and strips the frontmatter.
        (self.tmp / "agents" / "reviewer.md").unlink()
        self._legacy("---\nname: reviewer\n---\nLEGACY-SENTINEL body.\n")
        leaf = LeafConfig(family="codex", agent="reviewer", argv=["codex", "exec"])
        _, prefix = leaves._role_injection(self.cfg, leaf, families.resolve("codex"))
        self.assertIn("LEGACY-SENTINEL", prefix)
        self.assertNotIn("name: reviewer", prefix)   # frontmatter stripped

    def test_no_agent_or_missing_file_degrades_to_nothing(self) -> None:
        no_agent = LeafConfig(family="codex", argv=["codex"])
        self.assertEqual(
            leaves._role_injection(self.cfg, no_agent, families.resolve("codex")),
            ([], ""))
        ghost = LeafConfig(family="codex", agent="no-such-role", argv=["codex"])
        self.assertEqual(
            leaves._role_injection(self.cfg, ghost, families.resolve("codex")),
            ([], ""))


class MappedArgv(unittest.TestCase):
    def test_model_and_effort_map_through_the_profile(self) -> None:
        leaf = LeafConfig(family="claude", model="opus", effort="high", argv=["claude", "-p"])
        extra = leaves._mapped_argv(leaf, families.resolve("claude"), list(leaf.argv))
        self.assertEqual(extra, ["--model", "opus", "--effort", "high"])

    def test_explicit_argv_wins(self) -> None:
        leaf = LeafConfig(family="claude", model="opus", effort="high",
                          argv=["claude", "-p", "--model", "sonnet", "--effort", "low"])
        extra = leaves._mapped_argv(leaf, families.resolve("claude"), list(leaf.argv))
        self.assertEqual(extra, [])

    def test_codex_effort_maps_to_config_pair(self) -> None:
        leaf = LeafConfig(family="codex", effort="high", argv=["codex", "exec"])
        extra = leaves._mapped_argv(leaf, families.resolve("codex"), list(leaf.argv))
        self.assertEqual(extra, ["-c", "model_reasoning_effort=high"])


class FakeVendorCliEndToEnd(unittest.TestCase):
    """The full generic-family headless path with a shell script standing in as the
    vendor CLI: prompt arrives on stdin (with the inlined role prompt), no vendor
    flags are appended, and the artifact lands in the leaf's cwd."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = _cfg(self.tmp)
        agents = self.tmp / "agents"                 # canonical body (source of truth)
        agents.mkdir(parents=True)
        (agents / "reviewer.md").write_text(
            "ROLE-SENTINEL: judge the patch.\n", encoding="utf-8")
        self.cli = self.tmp / "fake-vendor-cli.sh"
        self.cli.write_text(
            "#!/bin/sh\n"
            "cat > received-prompt.txt\n"           # the stdin prompt, verbatim
            'printf "%s\\n" "$@" > received-argv.txt\n',
            encoding="utf-8")
        self.cli.chmod(0o755)

    def test_generic_leaf_gets_inlined_role_prompt_and_no_vendor_flags(self) -> None:
        leaf = LeafConfig(mode="command", family="generic", agent="reviewer",
                          argv=[str(self.cli), "--vendor-arg"])
        workdir = self.tmp / "sandbox"
        workdir.mkdir()
        leaves._invoke(leaf, workdir, "TASK-PROMPT", stream_json=True, cfg=self.cfg)
        prompt = (workdir / "received-prompt.txt").read_text(encoding="utf-8")
        self.assertIn("ROLE-SENTINEL", prompt)      # role prompt inlined…
        self.assertIn("TASK-PROMPT", prompt)        # …ahead of the task prompt
        self.assertLess(prompt.index("ROLE-SENTINEL"), prompt.index("TASK-PROMPT"))
        argv = (workdir / "received-argv.txt").read_text(encoding="utf-8")
        self.assertIn("--vendor-arg", argv)
        for flag in ("--output-format", "--add-dir", "--agent"):
            self.assertNotIn(flag, argv)            # stream_json ignored: no stream flags


if __name__ == "__main__":
    unittest.main()
