#!/usr/bin/env pwsh
<#
.SYNOPSIS
  Start the Mjolnir voice client against a running prodeo-server.

.DESCRIPTION
  Activates the project virtualenv, locates the Piper voice model, brings up
  Mjolnir's LLM brain (Ollama — installed, started, and model pulled on first
  run), wires up the MJOLNIR_* environment, and launches prodeo-mjolnir. All
  the fiddly bits from the README are handled here so day-to-day you just run:

      .\start-mjolnir.ps1

  The prodeo-server must already be running in another terminal.

.PARAMETER Token
  API token; must match the server's PRODEO_API_TOKEN. Defaults to the
  MJOLNIR_API_TOKEN / PRODEO_API_TOKEN env var if set, else "change-me".

.PARAMETER ServerUrl
  Base URL of the running server. Default http://127.0.0.1:8600.

.PARAMETER VoicePath
  Path to the Piper .onnx voice model. Auto-detected if omitted.

.PARAMETER Model
  Ollama model for the LLM brain. Must match MJOLNIR_LLM_MODEL (default
  llama3.1:8b).

.PARAMETER ModelsPath
  Where Ollama stores its (multi-GB) models. Optional: on a first install the
  script *asks* interactively where to put them, so you normally don't need
  this. Pass it to answer non-interactively (automation) or to override. The
  chosen path is created if needed and persisted to your user OLLAMA_MODELS so
  Ollama's background service uses it too. The Ollama program itself still
  installs to C: (winget's fixed location).

.PARAMETER NoOllama
  Skip the Ollama install/start/pull step (run on the deterministic grammar
  only — no natural-language understanding).

.PARAMETER NoCudaCheck
  Skip the CUDA runtime preflight. The check is advisory only: without a
  usable CUDA 12 + cuDNN 9, speech-to-text runs on the CPU instead of the GPU.

.EXAMPLE
  .\start-mjolnir.ps1
.EXAMPLE
  .\start-mjolnir.ps1 -Token difpat01
.EXAMPLE
  .\start-mjolnir.ps1 -ModelsPath F:\ollama-models
#>
[CmdletBinding()]
param(
    [string]$Token = $(
        if ($env:MJOLNIR_API_TOKEN) { $env:MJOLNIR_API_TOKEN }
        elseif ($env:PRODEO_API_TOKEN) { $env:PRODEO_API_TOKEN }
        else { "change-me" }
    ),
    [string]$ServerUrl = "http://127.0.0.1:8600",
    [string]$VoicePath = "",
    [string]$Model = "llama3.1:8b",
    [string]$ModelsPath = "",
    [switch]$NoOllama,
    [switch]$NoCudaCheck
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

# 5. CUDA preflight. Advisory only — without it speech-to-text quietly runs on
#    the CPU, which is exactly the failure this check exists to surface.
#    CTranslate2 (the engine under faster-whisper) links cuBLAS from CUDA *12*
#    and dlopens the cuDNN 9 backends; its wheel bundles only the cudnn64_9
#    dispatcher, not the backends. CUDA is installed machine-wide on purpose:
#    pip's nvidia-* wheels land in .venv, where the next `uv sync` deletes them
#    as undeclared. Mirrors _cuda_runtime_dirs() in prodeo_stt_fasterwhisper.
function Get-CudaRuntimeDir {
    param([string]$VenvRoot = $root)
    $dirs = @()
    # CUDA_PATH itself is skipped: it points at whichever toolkit was installed
    # last, routinely an older major that has no cublas64_12.dll.
    Get-ChildItem env: |
        Where-Object { $_.Name -like 'CUDA_PATH_V12_*' -or $_.Name -like 'CUDA_PATH_V13_*' } |
        ForEach-Object { $dirs += (Join-Path $_.Value 'bin') }
    $toolkit = Join-Path $env:ProgramFiles 'NVIDIA GPU Computing Toolkit\CUDA'
    $dirs += @(Get-ChildItem $toolkit -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^v1[23]\.' } | ForEach-Object { Join-Path $_.FullName 'bin' })
    # cuDNN 9 ships as its own installer with its own tree, DLLs one level
    # deeper under a CUDA-major folder (bin\12.9); older layouts use bin\.
    $cudnn = Join-Path $env:ProgramFiles 'NVIDIA\CUDNN'
    $dirs += @(Get-ChildItem $cudnn -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like 'v9.*' } | ForEach-Object {
            $bin = Join-Path $_.FullName 'bin'
            @(Get-ChildItem $bin -Directory -ErrorAction SilentlyContinue |
                ForEach-Object { $_.FullName }) + @($bin)
        })
    if ($env:CUDNN_PATH) { $dirs += (Join-Path $env:CUDNN_PATH 'bin') }
    # Last resort: the pip wheels, if someone installed them into this venv.
    $dirs += @(Get-ChildItem (Join-Path $VenvRoot '.venv\Lib\site-packages\nvidia') `
            -Directory -ErrorAction SilentlyContinue | ForEach-Object { Join-Path $_.FullName 'bin' })
    return @($dirs | Where-Object { Test-Path $_ } | Select-Object -Unique)
}

if (-not $NoCudaCheck) {
    try {
        $hasNvidiaGpu = [bool](Get-Command nvidia-smi -ErrorAction SilentlyContinue) -or
            [bool](@(Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue |
                    Where-Object { $_.Name -match 'NVIDIA' }))
        if ($hasNvidiaGpu) {
            $cudaDirs = Get-CudaRuntimeDir
            $missing = @('cublas64_12.dll', 'cudnn_ops64_9.dll') | Where-Object {
                $dll = $_
                -not ($cudaDirs | Where-Object { Test-Path (Join-Path $_ $dll) })
            }
            if ($missing) {
                Write-Host ""
                Write-Host "NVIDIA GPU found, but the CUDA runtime is incomplete — missing $($missing -join ', ')." -ForegroundColor Yellow
                Write-Host "Speech-to-text will fall back to the CPU (slower) until it is installed." -ForegroundColor Yellow
                Write-Host "Install once, machine-wide (each installer asks where to put itself):"
                Write-Host "    winget install --id Nvidia.CUDA -e --version 12.9"
                Write-Host "      ^ CUDA Toolkit 12.x. The winget default is 13.x, which does NOT work:"
                Write-Host "        CTranslate2 links cublas64_12.dll." -ForegroundColor DarkGray
                Write-Host "    cuDNN 9: https://developer.nvidia.com/cudnn-downloads"
                Write-Host "Then open a new shell so the updated PATH is picked up." -ForegroundColor DarkGray
                Write-Host "(Don't pip-install nvidia-* into .venv — the next 'uv sync' removes it.)" -ForegroundColor DarkGray
                Write-Host ""
            } else {
                Write-Host "CUDA runtime ready (GPU speech-to-text)." -ForegroundColor Green
            }
        }
    } catch {
        Write-Host "CUDA check skipped ($($_.Exception.Message))." -ForegroundColor DarkGray
    }
}

# 6. Bring up Mjolnir's LLM brain (Ollama). Non-fatal: any snag just means the
#    deterministic grammar handles commands and natural language is skipped.
if (-not $NoOllama) {
    try {
        # 127.0.0.1, not localhost: Ollama binds IPv4 only, but localhost
        # resolves to ::1 first and the fallback costs ~2s here - longer than
        # the 1s probe timeout below, so "is it up?" could never succeed.
        $ollamaUrl = "http://127.0.0.1:11434"

        # Keep the model resident so an idle assistant never cold-loads (an
        # 18s reload would blow the router's per-command budget). Persist it so
        # Ollama's background service uses it too.
        if (-not $env:OLLAMA_KEEP_ALIVE) {
            $env:OLLAMA_KEEP_ALIVE = "-1"
            [Environment]::SetEnvironmentVariable("OLLAMA_KEEP_ALIVE", "-1", "User")
        }

        # 6a. Decide where the (multi-GB) model is stored. On a first install we
        #     ask, so nothing lands on the system drive by surprise. -ModelsPath
        #     (or a previously chosen OLLAMA_MODELS) skips the question.
        $ollamaInstalled = [bool](Get-Command ollama -ErrorAction SilentlyContinue) `
            -or (Test-Path (Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"))
        $chosenPath = $ModelsPath
        if (-not $chosenPath -and -not $ollamaInstalled -and -not $env:OLLAMA_MODELS) {
            $default = Join-Path $env:USERPROFILE ".ollama\models"
            if ([Environment]::UserInteractive -and -not [Console]::IsInputRedirected) {
                Write-Host ""
                Write-Host "Ollama (Mjolnir's LLM brain) needs to be installed." -ForegroundColor Yellow
                $answer = Read-Host "Where would you like to install the model (~4 GB)? [Enter for $default]"
                if (-not [string]::IsNullOrWhiteSpace($answer)) { $chosenPath = $answer.Trim('"').Trim() }
            }
        }
        if ($chosenPath) {
            New-Item -ItemType Directory -Force -Path $chosenPath | Out-Null
            $env:OLLAMA_MODELS = $chosenPath
            [Environment]::SetEnvironmentVariable("OLLAMA_MODELS", $chosenPath, "User")
            Write-Host "Ollama models -> $chosenPath" -ForegroundColor Cyan
        }

        # 6b. Make sure the ollama CLI is available — install once if not.
        if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
            $ollamaBin = Join-Path $env:LOCALAPPDATA "Programs\Ollama"
            if (Test-Path (Join-Path $ollamaBin "ollama.exe")) {
                $env:PATH = "$ollamaBin;$env:PATH"   # installed, just not on PATH yet
            } else {
                Write-Host "Ollama not found — installing it once via winget..." -ForegroundColor Yellow
                winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements
                if (Test-Path $ollamaBin) { $env:PATH = "$ollamaBin;$env:PATH" }
            }
        }

        # 6c. Wait for the server to answer. On Windows the installer runs Ollama
        #     as a background app that can take several seconds to come up, so
        #     give it grace before launching our own `serve`, and wait up to 60s.
        $ollamaUp = $false
        $startedServe = $false
        for ($i = 0; $i -lt 120; $i++) {
            try { Invoke-WebRequest "$ollamaUrl/api/tags" -TimeoutSec 1 -UseBasicParsing | Out-Null; $ollamaUp = $true; break } catch {}
            if (-not $startedServe -and $i -ge 6 -and (Get-Command ollama -ErrorAction SilentlyContinue)) {
                Write-Host "Starting Ollama..." -ForegroundColor Cyan
                Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden
                $startedServe = $true
            }
            Start-Sleep -Milliseconds 500
        }

        # 6d. Pull the model on first run (several GB), then it's cached.
        if ($ollamaUp) {
            $installed = (& ollama list | Out-String)
            if ($installed -notmatch [regex]::Escape($Model)) {
                Write-Host "Pulling $Model (first run, several GB — this is a one-time download)..." -ForegroundColor Yellow
                & ollama pull $Model
            }
            Write-Host "Ollama ready ($Model)." -ForegroundColor Green
        } else {
            Write-Host "Ollama didn't come up — Mjolnir will use the basic grammar only." -ForegroundColor Yellow
        }
    } catch {
        Write-Host "Ollama setup skipped ($($_.Exception.Message)) — grammar-only understanding." -ForegroundColor Yellow
    }
}

# 7. Configure and launch.
$env:MJOLNIR_SERVER_URL = $ServerUrl
$env:MJOLNIR_API_TOKEN = $Token
$env:MJOLNIR_LLM_MODEL = $Model
# Warm local inference is ~3s; give the router headroom over its 4s default so
# a slightly-slow classification isn't clipped to a grammar fallback.
$env:MJOLNIR_LLM_ROUTER_TIMEOUT_S = "10"
$env:MJOLNIR_ENGINES = '{"piper": {"voice_path": "' + $voiceJson + '"}}'

Write-Host "Starting Mjolnir -> $ServerUrl" -ForegroundColor Green
Write-Host "  voice: $VoicePath" -ForegroundColor DarkGray
prodeo-mjolnir
