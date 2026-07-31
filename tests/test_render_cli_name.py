"""Template-repo test: a namespaced `cli_name` must reach every rendered command.

The console script is installed under the copier answer `cli_name`
(template/pyproject.toml.jinja [project.scripts]), and copier.yml explicitly
recommends namespacing it when several instances share a machine. A `.jinja`
source that quotes the default name literally (e.g. the CI re-gate's
`run: pdca gates --working-tree`, issue #375) breaks every such instance:
CI fails command-not-found on every PR. This renders with a namespaced answer
and asserts no file rendered from a template/**/*.jinja source still carries a
bare `pdca <subcommand>` invocation. Skips cleanly if copier isn't importable.
Run with: python -m unittest tests.test_render_cli_name
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# copier.yml keeps Copier's default answers filename; the answers-file template's
# own name is interpolated (template/{{ _copier_conf.answers_file }}.jinja), so it
# cannot be mapped by suffix-stripping alone.
ANSWERS_FILE = ".copier-answers.yml"

# The driver's subcommands. A bare `pdca <one of these>` in a rendered file is an
# invocation of the DEFAULT command name — dead under a namespaced render. The
# lookbehind and the mandatory whitespace keep `pdca.toml`, `pdca_harness`,
# `pdca-harness`, and the namespaced name itself (`pdca-nstest gates`) out.
BARE_INVOCATION = re.compile(
    r"(?<![\w{./-])pdca[ \t]+(?:"
    r"gates|flow|run|status|signoff|publish|doctor|contribcheck|split|try|act|sweep|queue"
    r")\b"
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

try:
    from copier import run_copy  # type: ignore

    HAVE_COPIER = True
except Exception:  # pragma: no cover - environment without copier
    HAVE_COPIER = False


@unittest.skipUnless(HAVE_COPIER, "copier not installed")
class RenderCliName(unittest.TestCase):
    def test_namespaced_cli_name_reaches_every_rendered_command(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            # Render from a tagged git copy of the working tree (the same harness
            # as test_render_and_run), with a NAMESPACED cli_name answer.
            src = tmp / "src"
            shutil.copytree(REPO, src, ignore=shutil.ignore_patterns(".git", "__pycache__"))
            _git(src, "init", "-q")
            _git(src, "add", "-A")
            _git(src, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x")
            _git(src, "tag", "v0test")
            out = tmp / "out"
            run_copy(
                str(src),
                str(out),
                data={
                    "project_name": "Render Test",
                    "tracker_url": "https://x/issues",
                    "cli_name": "pdca-nstest",
                },
                defaults=True,
                unsafe=True,
                quiet=True,
            )

            # (a) The CI merge re-gate invokes the CONFIGURED console script —
            # the functional breakage of #375 — and keeps no bare invocation.
            workflow = out / ".github" / "workflows" / "check-gates.yml"
            self.assertTrue(workflow.exists(), "rendered CI workflow missing")
            wf_text = workflow.read_text(encoding="utf-8")
            self.assertIn(
                "pdca-nstest gates --working-tree",
                wf_text,
                "CI re-gate does not invoke the configured cli_name",
            )
            self.assertIsNone(
                BARE_INVOCATION.search(wf_text),
                "CI workflow still invokes the default `pdca` command name",
            )

            # (b) The whole class stays caught: enumerate template/**/*.jinja in
            # the SOURCE tree, map each to its rendered path (strip the .jinja
            # suffix), and require no bare default-name invocation in any of them.
            scanned: list[str] = []
            offenders: list[str] = []
            for jinja_src in sorted((src / "template").rglob("*.jinja")):
                rel = jinja_src.relative_to(src / "template").as_posix()
                rel = rel[: -len(".jinja")]
                if "{{" in rel:
                    # Only the answers-file template has an interpolated name;
                    # anything else templated would silently escape the scan.
                    self.assertIn(
                        "_copier_conf.answers_file",
                        rel,
                        f"unmapped templated filename: {rel}",
                    )
                    rel = ANSWERS_FILE
                rendered = out / rel
                if not rendered.exists():
                    continue  # conditionally excluded by copier.yml _exclude
                scanned.append(rel)
                for lineno, line in enumerate(
                    rendered.read_text(encoding="utf-8").splitlines(), 1
                ):
                    if BARE_INVOCATION.search(line):
                        offenders.append(f"{rel}:{lineno}: {line.strip()}")

            # Guard against a vacuous pass: the load-bearing renders must be in scope.
            self.assertIn(".github/workflows/check-gates.yml", scanned)
            self.assertIn("pdca.toml", scanned)
            self.assertFalse(
                offenders,
                "bare `pdca <subcommand>` invocation(s) survive a namespaced "
                "render (source these from `{{ cli_name }}`):\n" + "\n".join(offenders),
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
