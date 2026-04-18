# RAG Tools — Dev Launcher
# Runs the service from the local source checkout on port 21421,
# fully isolated from the installed app on port 21420.
#
# Usage (from repo root or anywhere):
#   .\scripts\dev.ps1                    # start the dev service (foreground)
#   .\scripts\dev.ps1 -Port 21422        # custom port
#
# The service reads ./ragtools.toml and writes to ./data/ (repo-local).
# No Windows Startup task is registered from dev mode.

param(
    [int]$Port = 21421,
    [string]$Host = "127.0.0.1"
)

$ErrorActionPreference = "Stop"

# Resolve repo root (parent of this script's directory)
$RepoRoot = Split-Path $PSScriptRoot -Parent
Push-Location $RepoRoot

try {
    # Activate venv if present
    $venvActivate = Join-Path $RepoRoot ".venv\Scripts\Activate.ps1"
    if (Test-Path $venvActivate) {
        Write-Host "Activating venv: $venvActivate" -ForegroundColor DarkGray
        . $venvActivate
    } else {
        Write-Host "No .venv found at $venvActivate — using current Python." -ForegroundColor Yellow
    }

    # Show what we're about to do
    Write-Host ""
    Write-Host "=== RAG Tools DEV service ===" -ForegroundColor Cyan
    Write-Host "  Repo:   $RepoRoot"
    Write-Host "  Host:   $Host"
    Write-Host "  Port:   $Port  (installed app stays on 21420)"
    Write-Host "  Data:   $RepoRoot\data\"
    Write-Host "  Config: $RepoRoot\ragtools.toml"
    Write-Host "  Admin:  http://${Host}:${Port}"
    Write-Host ""

    # Run the service in the foreground. Ctrl+C stops it cleanly.
    python -m ragtools.service.run --host $Host --port $Port
}
finally {
    Pop-Location
}
