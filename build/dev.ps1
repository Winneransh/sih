# Run the Tauri shell against the live dev servers — no freezing, fast reload.
#
# The backend must be started separately:
#     .\run.ps1
#
# The shell will not find a sidecar and falls back to port 8000, which is what
# you want while iterating.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
Push-Location src-tauri
cargo tauri dev
Pop-Location
