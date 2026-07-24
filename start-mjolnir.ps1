#!/usr/bin/env pwsh
<#
.SYNOPSIS
  Start the Mjolnir voice client against a running prodeo-server.

.DESCRIPTION
  Activates the project virtualenv, locates the Piper voice model, wires up the
  MJOLNIR_* environment, and launches prodeo-mjolnir. All the fiddly bits from
  the README are handled here so day-to-day you just run:

      .\start-mjolnir.ps1

  The prodeo-server must already be running in another terminal.

.PARAMETER Token
  API token; must match the server's PRODEO_API_TOKEN. Defaults to the
  MJOLNIR_API_TOKEN / PRODEO_API_TOKEN env var if set, else "change-me".

.PARAMETER ServerUrl
  Base URL of the running server. Default http://127.0.0.1:8600.

.PARAMETER VoicePath
  Path to the Piper .onnx voice model. Auto-detected if omitted.

.EXAMPLE
  .\start-mjolnir.ps1
.EXAMPLE
  .\start-mjolnir.ps1 -Token difpat01
#>
[CmdletBinding()]
param(
    [string]$Token = $(
        if ($env:MJOLNIR_API_TOKEN) { $env:MJOLNIR_API_TOKEN }
        elseif ($env:PRODEO_API_TOKEN) { $env:PRODEO_API_TOKEN }
        else { "change-me" }
    ),
    [string]$ServerUrl = "http://127.0.0.1:8600",
    [string]$VoicePath = ""
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

# 1. Activate the project virtualenv (so `prodeo-mjolnir` is on PATH).
$activate = Join-Path $root ".venv\Scripts\Activate.ps1"
if (-not (Test-Path $activate)) {
    Write-Host "No virtualenv at $activate — run 'uv sync --all-groups' first." -ForegroundColor Red
    exit 1
}
& $activate

# 2. Locate the Piper voice model (explicit -VoicePath wins, else auto-detect).
if (-not $VoicePath) {
    $candidates = @(
        (Join-Path $env:USERPROFILE "piper-voices\en_GB-alan-medium.onnx"),
        (Join-Path $env:USERPROFILE ".local\share\prodeo-mjolnir\piper-voices\en_GB-alan-medium.onnx")
    )
    $VoicePath = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $VoicePath -or -not (Test-Path $VoicePath)) {
    Write-Host "Piper voice model not found. Download it once with:" -ForegroundColor Yellow
    Write-Host '    python -m piper.download_voices en_GB-alan-medium --data-dir "$env:USERPROFILE\piper-voices"'
    exit 1
}

# 3. Piper wants forward slashes inside the JSON config value.
$voiceJson = $VoicePath -replace '\\', '/'

# 4. Friendly heads-up if the server isn't up yet (non-fatal).
try {
    Invoke-WebRequest -Uri "$ServerUrl/api/health" -TimeoutSec 2 -UseBasicParsing | Out-Null
} catch {
    Write-Host "Note: $ServerUrl isn't responding yet — start prodeo-server in another terminal." -ForegroundColor Yellow
}

# 5. Configure and launch.
$env:MJOLNIR_SERVER_URL = $ServerUrl
$env:MJOLNIR_API_TOKEN = $Token
$env:MJOLNIR_ENGINES = '{"piper": {"voice_path": "' + $voiceJson + '"}}'

Write-Host "Starting Mjolnir -> $ServerUrl" -ForegroundColor Green
Write-Host "  voice: $VoicePath" -ForegroundColor DarkGray
prodeo-mjolnir
