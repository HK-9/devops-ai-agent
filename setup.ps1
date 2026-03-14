<#
.SYNOPSIS
    One-time project setup for Windows. Installs make (via Chocolatey),
    creates a virtual environment, installs dependencies, and configures git hooks.

.DESCRIPTION
    Run this script once after cloning:
        powershell -ExecutionPolicy Bypass -File setup.ps1

    Requires: Python 3.12+, Git
    Installs: Chocolatey (if missing), make (if missing), project deps, pre-commit hooks
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── Guard: Windows only ───────────────────────────────────────────────
if ($env:OS -ne "Windows_NT") {
    Write-Host "This script is for Windows only. On Linux/macOS use:" -ForegroundColor Red
    Write-Host "  python -m venv .venv && source .venv/bin/activate && make install && make hooks" -ForegroundColor Yellow
    exit 1
}

function Write-Step($msg) { Write-Host "`n>>> $msg" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "    [OK] $msg" -ForegroundColor Green }
function Write-Skip($msg) { Write-Host "    [SKIP] $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "    [ERROR] $msg" -ForegroundColor Red }

# ── 1. Check Python ────────────────────────────────────────────────────
Write-Step "Checking Python..."
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Err "Python not found. Install Python 3.12+ from https://python.org and re-run."
    exit 1
}
$pyVer = python --version 2>&1
Write-OK $pyVer

# ── 2. Check Git ──────────────────────────────────────────────────────
Write-Step "Checking Git..."
$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) {
    Write-Err "Git not found. Install Git from https://git-scm.com and re-run."
    exit 1
}
$gitVer = git --version 2>&1
Write-OK $gitVer

# ── 3. Install Chocolatey (if missing) ────────────────────────────────
Write-Step "Checking Chocolatey..."
$choco = Get-Command choco -ErrorAction SilentlyContinue
if ($choco) {
    Write-Skip "Chocolatey already installed."
} else {
    Write-Host "    Installing Chocolatey (requires Admin)..." -ForegroundColor Yellow

    # Check if running as admin
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
    if (-not $isAdmin) {
        Write-Err "Chocolatey install requires Admin. Re-run this script as Administrator."
        Write-Host "    Right-click PowerShell -> 'Run as Administrator' -> run setup.ps1 again." -ForegroundColor Yellow
        exit 1
    }

    Set-ExecutionPolicy Bypass -Scope Process -Force
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
    Invoke-Expression ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))

    # Refresh PATH
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")

    $choco = Get-Command choco -ErrorAction SilentlyContinue
    if ($choco) {
        Write-OK "Chocolatey installed."
    } else {
        Write-Err "Chocolatey installation failed. Install manually: https://chocolatey.org/install"
        exit 1
    }
}

# ── 4. Install make (if missing) ─────────────────────────────────────
Write-Step "Checking make..."
$mk = Get-Command make -ErrorAction SilentlyContinue
if ($mk) {
    Write-Skip "make already installed."
} else {
    Write-Host "    Installing make via Chocolatey..." -ForegroundColor Yellow

    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
    if (-not $isAdmin) {
        Write-Err "Installing make requires Admin. Re-run this script as Administrator."
        exit 1
    }

    choco install make -y
    # Refresh PATH
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")

    $mk = Get-Command make -ErrorAction SilentlyContinue
    if ($mk) {
        Write-OK "make installed."
    } else {
        Write-Err "make installation failed. Try: choco install make -y"
        exit 1
    }
}

# ── 5. Create virtual environment ────────────────────────────────────
Write-Step "Setting up virtual environment..."
$venvPath = Join-Path $PSScriptRoot ".venv"
if (Test-Path (Join-Path $venvPath "Scripts\python.exe")) {
    Write-Skip ".venv already exists."
} else {
    python -m venv $venvPath
    Write-OK "Created .venv"
}

# Activate
$activateScript = Join-Path $venvPath "Scripts\Activate.ps1"
. $activateScript
Write-OK "Activated .venv"

# ── 6. Install project dependencies ──────────────────────────────────
Write-Step "Installing project dependencies..."
python -m pip install --upgrade pip --quiet
pip install -e ".[dev,infra]" --quiet
Write-OK "Dependencies installed (dev + infra extras)."

# ── 7. Install pre-commit hooks ──────────────────────────────────────
Write-Step "Installing git hooks..."
pre-commit install --hook-type pre-commit --hook-type commit-msg
Write-OK "Pre-commit + commit-msg hooks installed."

# ── 8. Summary ───────────────────────────────────────────────────────
Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "  Setup complete! You're ready to go.       " -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Activate venv:   .venv\Scripts\Activate.ps1"
Write-Host "  Available tasks: make help"
Write-Host "  Commit changes:  make commit"
Write-Host "  Run tests:       make test"
Write-Host ""
