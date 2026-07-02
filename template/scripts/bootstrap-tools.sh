#!/usr/bin/env bash
# scripts/bootstrap-tools.sh (issue #207) — bootstrap every tool `flow` needs, idempotently,
# on a machine of unknown state. `make install` delegates here; re-running is a no-op for
# tools already present. `--check` reports status and installs nothing (exit non-zero iff a
# REQUIRED tool is missing).
#
# Generic (this file ships verbatim from the copier template — it hardcodes NO project gate
# toolchain) but provisions the harness-universal tools automatically and gives the instance
# a hook for its own toolchain. Three tiers:
#   1. harness-universal — git, gh, a pip-capable venv (ensurepip, get-pip.py fallback);
#   2. configured leaf backends — the CLIs the rendered pdca.toml's [leaves.*].family use;
#   3. project gate toolchain — an instance-owned hook: scripts/bootstrap-tools.d/*.sh and
#      [install].extra_bootstrap in pdca.toml (empty in the pristine template).
#
# Policy: system packages (git, gh, python3-venv) install via `sudo apt-get` when apt + sudo
# are available, else the exact command is printed and a REQUIRED tool exits non-zero (never
# silently skipped). Leaf CLIs use their official user-space installers. A missing REQUIRED
# tool fails; a missing OPTIONAL tool (a non-gating advisory leaf) warns and continues — the
# same "advisory never blocks" contract as Check.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
TOML="$ROOT/pdca.toml"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

req_missing=0
opt_missing=0

have() { command -v "$1" >/dev/null 2>&1; }
say()  { printf '%-9s %-28s %s\n' "$1" "$2" "${3:-}"; }
miss() { [ "$1" = 1 ] && req_missing=1 || opt_missing=1; }  # $1 = required(1/0)

# ensure_system NAME APT_PKG REQUIRED — a system tool via apt, else print the command.
ensure_system() {
  local name="$1" pkg="$2" required="$3"
  if have "$name"; then say OK "$name"; return; fi
  if [ "$CHECK_ONLY" = 1 ]; then say MISSING "$name" "sudo apt-get install -y $pkg"; miss "$required"; return; fi
  if have apt-get && have sudo; then
    sudo apt-get install -y "$pkg" >/dev/null 2>&1 || true
    if have "$name"; then say INSTALLED "$name"; else say FAILED "$name" "apt did not provide '$pkg'"; miss "$required"; fi
  else
    say MISSING "$name" "run: sudo apt-get install -y $pkg   (no apt/sudo detected)"; miss "$required"
  fi
}

# --- tier 1: harness-universal ------------------------------------------------
echo "== tier 1: harness-universal =="
if have "$PYTHON"; then say OK "$PYTHON"; else say MISSING "$PYTHON" "install Python >= 3.11"; miss 1; fi

# A pip-capable venv: probe ensurepip in THIS interpreter (a clean Debian/Ubuntu lacks it,
# so `python -m venv` fails). Install python3-venv where we can; keep get-pip.py as the
# fallback so the console-script install works even on a pip-less stdlib.
if "$PYTHON" -c "import ensurepip" >/dev/null 2>&1; then
  say OK "python venv (ensurepip)"
else
  if have apt-get && have sudo && [ "$CHECK_ONLY" = 0 ]; then
    sudo apt-get install -y python3-venv >/dev/null 2>&1 || true
  fi
  if "$PYTHON" -c "import ensurepip" >/dev/null 2>&1; then
    say INSTALLED "python venv (ensurepip)"
  else
    # Not fatal: the venv step below builds --without-pip and bootstraps pip via get-pip.py.
    say WARN "python venv (ensurepip)" "get-pip.py fallback will be used (sudo apt-get install -y python3-venv to fix)"
  fi
fi

ensure_system git git 1
ensure_system gh gh 1
if [ "$CHECK_ONLY" = 0 ] && have gh; then
  gh auth status >/dev/null 2>&1 || say WARN "gh auth" "run 'gh auth login' (publish/merge need it)"
fi

# --- venv + console script ----------------------------------------------------
if [ "$CHECK_ONLY" = 0 ]; then
  echo
  echo "== console script (.venv) =="
  if [ ! -d "$ROOT/.venv" ]; then
    if "$PYTHON" -c "import ensurepip" >/dev/null 2>&1; then
      "$PYTHON" -m venv "$ROOT/.venv"
    else
      # pip-less stdlib: build the venv without pip, then bootstrap pip via get-pip.py.
      "$PYTHON" -m venv --without-pip "$ROOT/.venv"
      curl -fsSL https://bootstrap.pypa.io/get-pip.py | "$ROOT/.venv/bin/python"
    fi
  fi
  "$ROOT/.venv/bin/pip" install -q -e "$ROOT"
  say INSTALLED "console script" "(.venv/bin — see pyproject [project.scripts])"
fi

# --- tier 2: configured leaf backends ----------------------------------------
echo
echo "== tier 2: leaf backends (from pdca.toml) =="
# Every distinct [leaves.*].family in the rendered config (builder, reviewer, advisory,
# variants, escalation). The builder's family is REQUIRED (Do can't run without it); the
# rest are OPTIONAL (a non-gating advisory leaf never blocks). Only configured backends are
# installed — a claude-only render never fetches codex.
leaf_families() {
  [ -f "$TOML" ] || return 0
  grep -oE 'family[[:space:]]*=[[:space:]]*"[^"]+"' "$TOML" | grep -oE '"[^"]+"' | tr -d '"' | sort -u
}
builder_family() {
  [ -f "$TOML" ] || return 0
  sed -n '/^\[leaves\.builder\]/,/^\[/p' "$TOML" \
    | grep -oE 'family[[:space:]]*=[[:space:]]*"[^"]+"' | head -1 | grep -oE '"[^"]+"' | tr -d '"'
}

# family → the CLI binary it spawns.
family_bin() { case "$1" in claude) echo claude;; codex) echo codex;; gemini) echo gemini;; *) echo "$1";; esac; }
# family → its official user-space installer (empty ⇒ no known auto-installer).
family_install() {
  case "$1" in
    claude) echo "curl -fsSL https://claude.ai/install.sh | bash";;
    *) echo "";;  # codex / gemini: install command is family-specific — printed as a hint
  esac
}

BUILDER_FAM="$(builder_family || true)"
FAMILIES="$(leaf_families || true)"
if [ -z "$FAMILIES" ]; then
  say OK "all leaves are stubs" "no model CLI needed (offline mode)"
fi
for fam in $FAMILIES; do
  bin="$(family_bin "$fam")"
  required=0; [ "$fam" = "$BUILDER_FAM" ] && required=1
  if have "$bin"; then say OK "$bin (family=$fam)"; continue; fi
  installer="$(family_install "$fam")"
  if [ "$CHECK_ONLY" = 1 ]; then
    say MISSING "$bin (family=$fam)" "${installer:-install '$bin' for the $fam backend}"; miss "$required"; continue
  fi
  if [ -n "$installer" ]; then
    sh -c "$installer" || true
    if have "$bin"; then say INSTALLED "$bin (family=$fam)"; else say FAILED "$bin (family=$fam)" "installer did not provide it"; miss "$required"; fi
  else
    say MISSING "$bin (family=$fam)" "no auto-installer — install '$bin' for the $fam backend"; miss "$required"
  fi
done

# --- tier 3: project gate toolchain (instance-owned hook) --------------------
echo
echo "== tier 3: project toolchain (instance hook) =="
ran_hook=0
# 3a. drop-in dir: scripts/bootstrap-tools.d/*.sh, sourced in order (composable).
if [ -d "$ROOT/scripts/bootstrap-tools.d" ]; then
  for hook in "$ROOT"/scripts/bootstrap-tools.d/*.sh; do
    [ -e "$hook" ] || continue
    ran_hook=1
    say RUN "$(basename "$hook")"
    [ "$CHECK_ONLY" = 0 ] && CHECK_ONLY="$CHECK_ONLY" PYTHON="$PYTHON" bash "$hook"
  done
fi
# 3b. [install].extra_bootstrap in pdca.toml — one command the instance owns.
extra_bootstrap() {
  [ -f "$TOML" ] || return 0
  sed -n '/^\[install\]/,/^\[/p' "$TOML" \
    | grep -oE 'extra_bootstrap[[:space:]]*=[[:space:]]*"[^"]*"' | head -1 \
    | sed -E 's/.*=[[:space:]]*"([^"]*)"/\1/'
}
EXTRA="$(extra_bootstrap || true)"
if [ -n "$EXTRA" ]; then
  ran_hook=1
  say RUN "[install].extra_bootstrap" "$EXTRA"
  [ "$CHECK_ONLY" = 0 ] && ( cd "$ROOT" && sh -c "$EXTRA" )
fi
[ "$ran_hook" = 0 ] && say OK "no project hook" "add scripts/bootstrap-tools.d/*.sh or [install].extra_bootstrap"

# --- summary ------------------------------------------------------------------
echo
if [ "$req_missing" != 0 ]; then
  echo "bootstrap: REQUIRED tools missing — fix the lines above (exit 1)."
  exit 1
fi
if [ "$opt_missing" != 0 ]; then
  echo "bootstrap: required tools OK — some optional pieces need attention (see above)."
else
  echo "bootstrap: all tools present."
fi
exit 0
