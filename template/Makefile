# Front door for the harness — everything you need is a `make` target. No
# PYTHONPATH, no aliases, no remembering subcommands.
#
#   make flow ID=123                       run the cycle for one issue
#   make flow CSV="path/to/issues.csv"     batch: one Plan session may brief
#                                          SEVERAL issues, all built unattended
#                                          then signed off cheap-first via the queue
#                   Runs Plan → Do → Check → sign-off → publish (on an accept it
#                   opens a DRAFT PR; NO_PUBLISH=1 skips that). Plan/sign-off/publish
#                   open Claude in your terminal — use a real terminal. Optional:
#                   ACT=1 also runs the cross-cycle Act review; BY=<name> overrides
#                   the §9 attribution (defaults to the project author).
#   make batch IDS="123 456" [PLAN=1] [CSV=…]
#                   drive issues that are ALREADY briefed through the full cycle
#                   (Do → Check → sign-off → publish → Act) — no Plan beat. Skips
#                   ids with no brief or already complete. PLAN=1 (or CSV=…) adds a
#                   Plan pre-pass that briefs the UNPLANNED ids in one shared session
#                   first. NOACT=1 stops after sign-off; BY=<name> sets the §9 attribution.
#   make rehearse ID=123 [CSV=…]
#                   dry-run the SAME control flow with stub leaves + stub gates —
#                   no Claude, no live gates (publish dry-runs too), instant.
#   make publish ID=123 [DRY=1]
#                   re-publish an already-accepted bundle as a draft PR (the flow
#                   does this on accept). DRY=1 prints the git/gh plan without pushing.
#   make status     list every bundle and its state.
#   make cli ARGS="signoff 123 --accept"
#                   run any driver subcommand without the source-path boilerplate.
#
#   make            full self-test: offline guards + a real driver cycle on stub
#                   leaves (no model, no live gates).
#   make check      fast: driver tests only, offline (~1s).
#   make setup      one-time: grant Claude read of the workspace so the interactive
#                   leaves don't prompt per file/dir (add your project's sibling
#                   repos to .claude/settings.local.json as needed).
#   make install    create .venv with the real console script (named per pyproject
#                   [project.scripts]) (optional — the targets above work without it).

PYTHON ?= python3
export PYTHONPATH := src
PDCA := $(PYTHON) -m pdca_harness.cli

.DEFAULT_GOAL := test
.PHONY: test check flow batch rehearse publish status cli install setup

# --- the cycle -------------------------------------------------------------
# Live, continuous, Claude-driven. Give ID for one issue, or just CSV for a batch
# Plan session that may brief several issues (built all, then signed off cheap-first).
# On an accept the flow publishes a draft PR; NO_PUBLISH=1 stops at COMPLETE.
flow:
	@test -n "$(ID)$(CSV)" || { echo 'usage: make flow ID=<issue-id> [CSV="<path>"] [ACT=1] [NO_PUBLISH=1] [BY=<name>]'; echo '   or: make flow CSV="<path>" [...]   (batch: Plan briefs several)'; exit 2; }
	$(PDCA) flow $(ID) $(if $(CSV),--from-csv "$(CSV)") $(if $(NO_PUBLISH),--no-publish) $(if $(ACT),--act) $(if $(BY),--by "$(BY)")

# Drive already-briefed issues through the full cycle, no Plan beat (Do → Check →
# sign-off → publish → Act). NOACT=1 stops after sign-off; BY=<name> sets §9.
batch:
	@test -n "$(IDS)" || { echo 'usage: make batch IDS="<id> <id> ..." [PLAN=1] [CSV="<path>"] [NOACT=1] [BY=<name>]'; exit 2; }
	$(PDCA) batch $(IDS) $(if $(PLAN),--plan) $(if $(CSV),--from-csv "$(CSV)") $(if $(NOACT),--no-act) $(if $(BY),--by "$(BY)")

# Re-publish an accepted bundle as a draft PR (the flow does this on accept).
publish:
	@test -n "$(ID)" || { echo 'usage: make publish ID=<issue-id> [DRY=1] [BY=<name>]'; exit 2; }
	$(PDCA) publish $(ID) $(if $(DRY),--dry-run) $(if $(BY),--by "$(BY)")

# Same control flow with stub leaves + stub gates (no Claude / TTY / live gates), in
# an ISOLATED throwaway bundle root so it never touches the real results/ a live run uses.
rehearse:
	@test -n "$(ID)$(CSV)" || { echo 'usage: make rehearse ID=<issue-id> [CSV="<path>"]'; exit 2; }
	@rm -rf .rehearse
	PDCA_BUNDLE_ROOT=.rehearse PDCA_LEAVES_MODE=stub PDCA_GATES_MODE=stub $(PDCA) flow $(ID) $(if $(CSV),--from-csv "$(CSV)")
	@printf '(rehearsal bundles in ./.rehearse — throwaway; real runs use results/)\n'

status:
	@$(PDCA) status

# Escape hatch for any other subcommand: make cli ARGS="queue"
cli:
	$(PDCA) $(ARGS)

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
	@echo " the first interactive 'make flow' asks once to TRUST this project — accept it.)"

# --- optional real install (venv console script) ---------------------------
# Name-agnostic: the console script is named per pyproject [project.scripts]
# (the cli_name copier choice), so depend on a sentinel, not a fixed script path.
install: .venv/.installed
	@printf '\nInstalled. The console script (see pyproject [project.scripts]) is on .venv/bin/; or keep using `make flow …`.\n'

.venv/.installed: pyproject.toml
	$(PYTHON) -m venv .venv
	.venv/bin/pip install -q -e .
	@touch $@

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
