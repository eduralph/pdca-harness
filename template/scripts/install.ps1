# Windows bootstrap for the PDCA harness (issue #85) — the equivalent of `make
# install` + `make setup` for hosts where GNU make isn't standard. Creates a venv,
# installs the console script (pip install -e .), and writes the Claude read-permission
# overlay. After this, run the cycle through the console script: `<cli> flow <id> …`
# (named per pyproject [project.scripts]).
#
# Usage (from the project root):  pwsh -File scripts/install.ps1
$ErrorActionPreference = "Stop"

$python = if ($env:PYTHON) { $env:PYTHON } else { "python" }

Write-Host "Creating .venv and installing the console script (pip install -e .)…"
& $python -m venv .venv
& .\.venv\Scripts\pip.exe install -q -e .

# Permissions overlay: grant Claude read of the whole workspace (the parent dir) + a
# temp dir, so the interactive leaves don't prompt per file. Machine-local, gitignored.
Write-Host "Writing .claude/settings.local.json (workspace read permissions)…"
$ws = Split-Path -Parent (Get-Location).Path
$settings = @{
    permissions = @{
        allow                 = @("Read($ws/**)")
        additionalDirectories = @($ws, $env:TEMP)
    }
}
New-Item -ItemType Directory -Force -Path ".claude" | Out-Null
$settings | ConvertTo-Json -Depth 5 | Set-Content -Path ".claude/settings.local.json" -Encoding utf8

Write-Host ""
Write-Host "Installed. Run the cycle with the console script on .venv\Scripts\ (see pyproject [project.scripts])."
Write-Host "(folder TRUST is separate — the first interactive 'flow' asks once to TRUST this project; accept it.)"
