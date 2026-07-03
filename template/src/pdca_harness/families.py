"""Vendor family profiles — every model-CLI-specific behavior as DATA.

The driver reaches a model only by spawning a configured CLI (``LeafConfig.argv``);
what *differs* between vendors (how to ask for a live tool-use stream, how to grant
read access to an extra directory, how a role prompt is injected, whether the CLI
must run from the harness root to discover its agents/hooks) used to live as
``family == "claude"`` branches scattered through :mod:`leaves`. This module
replaces those branches with a declarative :class:`FamilyProfile` registry:

- Built-ins ship for ``claude``, ``codex``, ``gemini`` and ``generic``.
- ``generic`` (prompt on stdin, no special flags, env-only grounding) is also the
  fallback for ANY unknown or empty family name, so ad-hoc families ("local",
  "mid", …) keep the behavior they always had.
- An instance can override or extend any profile from ``pdca.toml`` via
  ``[families.<name>]`` tables — adding a vendor is config, not a driver change.

The registry is static data resolved at config time; no model ever chooses
control flow (the determinism contract, docs 01).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, fields, replace


@dataclass(frozen=True)
class FamilyProfile:
    """Capabilities of one vendor CLI family.

    Defaults describe the *generic* vendor: prompt on stdin, no flags, grounding
    via environment only (``$PDCA_TARGET`` / ``$PDCA_WORKTREE``), role prompt
    inlined into the task prompt, and the driver-level PATH-shim STOP guard.
    """

    name: str = "generic"
    # Flags appended for a live tool-use stream; empty ⇒ the CLI has none (the
    # heartbeat then has no "did a session start" signal — see LeafError.produced).
    stream_argv: tuple[str, ...] = ()
    # Which stream parser the flags produce; "claude-stream-json" is the only one
    # progress.py speaks today (a codex/gemini parser can register here later).
    stream_format: str = ""
    # The CLI's "grant read access to this extra directory" flag ("" ⇒ env-only).
    grounding_flag: str = ""
    # How a role prompt reaches the model: "flag" (pass ``agent_flag <name>``, the
    # CLI resolves the definition itself), "inline" (prepend the agent file's body
    # to the task prompt), or "none".
    role_injection: str = "inline"
    agent_flag: str = ""
    # True ⇒ the CLI discovers project agents/hooks by walking up from cwd, so a
    # builder must run FROM THE HARNESS ROOT and be grounded in its worktree via
    # grounding_flag; False ⇒ confine the builder by cwd in the worktree (#136).
    cwd_discovery: bool = False
    # True ⇒ the family enforces the STOP discipline itself (claude's PreToolUse
    # hook); False ⇒ the driver wraps the leaf with the `gh` PATH shim (guard.py).
    native_guard: bool = False
    # Optional model/effort mapping for the opt-in per-leaf `model` / `effort`
    # keys; explicit argv remains the escape hatch and always wins.
    model_flag: str = ""
    effort_argv: tuple[str, ...] = ()


BUILTIN: dict[str, FamilyProfile] = {
    "claude": FamilyProfile(
        name="claude",
        stream_argv=("--output-format", "stream-json", "--verbose"),
        stream_format="claude-stream-json",
        grounding_flag="--add-dir",
        role_injection="flag",
        agent_flag="--agent",
        cwd_discovery=True,
        native_guard=True,
        model_flag="--model",
        effort_argv=("--effort", "{effort}"),
    ),
    "codex": FamilyProfile(
        name="codex",
        stream_argv=("--json",),           # `codex exec --json` — JSONL event stream
        stream_format="codex-stream-json",  # parsed by progress.py for live tool-use
        grounding_flag="--add-dir",         # grant the sandboxed reviewer read+write to
                                            # $PDCA_TARGET (git stash/unstash, re-run tests)
        model_flag="-m",
        effort_argv=("-c", "model_reasoning_effort={effort}"),
    ),
    "gemini": FamilyProfile(
        name="gemini",
        grounding_flag="--include-directories",
        model_flag="-m",
    ),
    "generic": FamilyProfile(),
}

_warned: set[str] = set()


def resolve(name: str, overrides: dict[str, dict] | None = None) -> FamilyProfile:
    """The profile for family ``name``, with ``pdca.toml [families.*]`` overrides.

    Resolution: start from the built-in for ``name`` (or the ``generic`` built-in
    for an unknown/empty name — noted once on stderr so a typo'd family is visible
    but never fatal), then apply the instance's override table for that name, if
    any. Unknown override keys are ignored (forward compatibility)."""
    key = (name or "").strip().lower()
    base = BUILTIN.get(key)
    table = (overrides or {}).get(key, {})
    if base is None:
        base = replace(BUILTIN["generic"], name=key or "generic")
        if key and not table and key not in _warned:
            _warned.add(key)
            print(f"families: unknown family '{name}' — using the generic profile "
                  "(stdin prompt, no vendor flags); declare [families.{0}] in "
                  "pdca.toml to customize".format(key), file=sys.stderr)
    if not table:
        return base
    known = {f.name for f in fields(FamilyProfile)}
    kwargs = {}
    for k, v in table.items():
        if k not in known or k == "name":
            continue
        kwargs[k] = tuple(v) if isinstance(v, list) else v
    return replace(base, **kwargs)


def strip_frontmatter(text: str) -> str:
    """The body of an agent definition file, minus its YAML frontmatter block.

    ``.claude/agents/*.md`` carry claude-specific frontmatter (``tools:``,
    ``hooks:``) between two ``---`` lines; families that inline the role prompt
    want only the vendor-neutral markdown body below it."""
    if not text.startswith("---"):
        return text
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "".join(lines[i + 1:])
    return text
