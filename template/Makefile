# Bootstrap + self-test only. The CYCLE is run through the console script, the
# cross-platform run interface (issue #85):
#
#   <cli> flow <id> [<id> …]   run the cycle for one issue (or several → batch)
#   <cli> flow … --rehearse    dry-run on stub leaves + stub gates (no Claude/Docker)
#   <cli>                      the status dashboard (bare invocation)
#   <cli> --help              every subcommand
#
# `<cli>` is this project's console script (named per pyproject [project.scripts] —
# the cli_name copier answer). Before `make install`, run it as
# `python -m pdca_harness.cli …` (PYTHONPATH=src from a source checkout).
#
# This Makefile scopes to what is genuinely per-platform — bootstrap — plus the dev
# self-test. On Windows use scripts/install.ps1 (GNU make isn't standard there).
#
#   make install    create .venv and install the console script (pip install -e .)
#   make setup      one-time: grant Claude read of the workspace (permissions)
#   make            full self-test: offline guards + a real driver cycle on stub leaves
#   make check      fast: driver tests only, offline (~1s)

PYTHON ?= python3
export PYTHONPATH := src
PDCA := $(PYTHON) -m pdca_harness.cli

.DEFAULT_GOAL := test
.PHONY: test check install install-check setup

# One-time permission setup so the interactive leaves don't prompt: grant Claude
# read of the whole workspace. Writes the machine-local .claude/settings.local.json
# (gitignored). Add your project's sibling repos / build dirs to that file as needed.
setup:
	@$(PYTHON) -c "import json, os; ws = os.path.dirname(os.getcwd()); \
json.dump({'permissions': {'allow': ['Read(' + ws + '/**)'], \
'additionalDirectories': [ws, '/tmp']}}, \
open('.claude/settings.local.json', 'w'), indent=2)"
	@echo "wrote .claude/settings.local.json — workspace read + /tmp (PERMISSIONS only)"
	@echo "(folder TRUST is separate — it lives in the GLOBAL ~/.claude.json, not here:"
	@echo " the first interactive '<cli> flow' asks once to TRUST this project — accept it.)"

# --- install every tool `flow` needs (issue #207) --------------------------
# Delegates to scripts/bootstrap-tools.sh: harness-universal tools (git, gh, a
# pip-capable venv with a get-pip.py fallback), the configured leaf backends, and
# the instance's own gate toolchain (bootstrap-tools.d/ + [install].extra_bootstrap).
# Idempotent — re-running is a no-op for tools already present. Name-agnostic: the
# console script is named per pyproject [project.scripts], so depend on a sentinel.
install: .venv/.installed
	@printf '\nInstalled. Run the cycle with the console script (see pyproject [project.scripts]) on .venv/bin/.\n'

# Depend on the config that drives the bootstrap too (pdca.toml — leaf families /
# [install].extra_bootstrap — and the drop-in hooks), so switching a leaf from stub to
# command or wiring project bootstrap AFTER the first install re-provisions rather than
# short-circuiting on a stale sentinel (issue #207 review). Idempotent — a re-run is a no-op.
.venv/.installed: pyproject.toml scripts/bootstrap-tools.sh pdca.toml $(wildcard scripts/bootstrap-tools.d/*.sh)
	PYTHON=$(PYTHON) ./scripts/bootstrap-tools.sh
	@touch $@

# Report every tool's status without installing anything; exit non-zero iff a
# REQUIRED tool is missing (a preflight for CI / a fresh host).
install-check:
	@PYTHON=$(PYTHON) ./scripts/bootstrap-tools.sh --check

# --- self-test -------------------------------------------------------------
# Depends on `check` so the cheap guards fail fast before the cycle.
test: check
	@echo "== full driver cycle on a throwaway bundle (stub leaves, offline) =="
	@rm -rf results/issue_selftest
	PDCA_LEAVES_MODE=stub $(PDCA) init-issue selftest --from-brief examples/toy/brief.md
	PDCA_LEAVES_MODE=stub $(PDCA) run selftest
	@printf '\n\xe2\x9c\x93 driver OK. Inspect:\n'
	@printf '    results/issue_selftest/check-gates.md   (gate outcomes)\n'
	@printf '    results/issue_selftest/SUMMARY.md       (assembled Check summary)\n'

check:
	$(PYTHON) -m unittest discover -s tests
