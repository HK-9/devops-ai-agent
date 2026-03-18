<#
.SYNOPSIS
    One-time project setup for Windows. Installs make (via Chocolatey),
    creates a virtual environment, installs dependencies, and configures git hooks.

.DESCRIPTION
    Run this script once after cloning:
        powershell -ExecutionPolicy Bypass -File setup.ps1

    Requires: Python 3.12+, Git
    Installs: Chocolatey (if missing), make (if missing), project deps, pre-commit hooks

    For Linux/macOS users, run: bash setup.sh
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# -- Guard: Windows only ------------------------------------------------
if ($env:OS -ne "Windows_NT") {
    Write-Host "This script is for Windows only. On Linux/macOS run:" -ForegroundColor Red
    Write-Host "  bash setup.sh" -ForegroundColor Yellow
    exit 1
}

# -- Progress tracking --------------------------------------------------
$TOTAL_STEPS = 7
$CURRENT_STEP = 0

function Write-Step($msg) {
    $script:CURRENT_STEP++
    $pct = [math]::Round(($script:CURRENT_STEP / $TOTAL_STEPS) * 100)
    $filled = [math]::Floor($pct / 5)
    $empty  = 20 - $filled
    $bar = ("#" * $filled) + ("-" * $empty)
    Write-Host ""
    Write-Host ("  [{0}] {1}% -- Step {2}/{3}" -f $bar, $pct, $script:CURRENT_STEP, $TOTAL_STEPS) -ForegroundColor Magenta
    Write-Host ("  >>> " + $msg) -ForegroundColor Cyan
}

function Write-OK($msg)   { Write-Host "      [OK] $msg" -ForegroundColor Green }
function Write-Skip($msg) { Write-Host "      [SKIP] $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "      [ERROR] $msg" -ForegroundColor Red }

function Invoke-Verbose {
    <#
    .SYNOPSIS
        Run an external command with real-time stdout/stderr streaming.
        Throws on non-zero exit code.
    #>
    param(
        [Parameter(Mandatory)] [string]   $Label,
        [Parameter(Mandatory)] [string]   $Command,
        [Parameter(Mandatory)] [string[]] $Arguments
    )

    Write-Host ("      [{0}] Running: {1} {2}" -f $Label, $Command, ($Arguments -join ' ')) -ForegroundColor DarkGray

    $pinfo = New-Object System.Diagnostics.ProcessStartInfo
    $pinfo.FileName               = $Command
    $pinfo.Arguments              = $Arguments -join ' '
    $pinfo.RedirectStandardOutput = $true
    $pinfo.RedirectStandardError  = $true
    $pinfo.UseShellExecute        = $false
    $pinfo.CreateNoWindow         = $true
    $pinfo.WorkingDirectory       = $PSScriptRoot

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $pinfo

    # Stream stdout and stderr asynchronously
    $stdoutEvent = Register-ObjectEvent -InputObject $proc -EventName OutputDataReceived -Action {
        if ($null -ne $EventArgs.Data -and $EventArgs.Data -ne '') {
            $line = $EventArgs.Data
            # Show pip progress lines and key status lines
            if ($line -match 'Installing|Collecting|Downloading|Building|Using|Successfully|Requirement|Preparing') {
                [Console]::WriteLine("        $line")
            }
        }
    }
    $stderrEvent = Register-ObjectEvent -InputObject $proc -EventName ErrorDataReceived -Action {
        if ($null -ne $EventArgs.Data -and $EventArgs.Data -ne '') {
            $line = $EventArgs.Data
            if ($line -match 'WARNING|ERROR|error') {
                [Console]::ForegroundColor = 'Yellow'
                [Console]::WriteLine("        $line")
                [Console]::ResetColor()
            }
        }
    }

    $proc.Start() | Out-Null
    $proc.BeginOutputReadLine()
    $proc.BeginErrorReadLine()
    $proc.WaitForExit()

    Unregister-Event -SourceIdentifier $stdoutEvent.Name
    Unregister-Event -SourceIdentifier $stderrEvent.Name

    if ($proc.ExitCode -ne 0) {
        throw ("Command '{0} {1}' failed with exit code {2}" -f $Command, ($Arguments -join ' '), $proc.ExitCode)
    }
}

# -- 1. Check Python ----------------------------------------------------
Write-Step "Checking Python..."
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Err "Python not found. Install Python 3.12+ from https://python.org and re-run."
    exit 1
}
$pyVer = python --version 2>&1
Write-OK $pyVer

# -- 2. Check Git -------------------------------------------------------
Write-Step "Checking Git..."
$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) {
    Write-Err "Git not found. Install Git from https://git-scm.com and re-run."
    exit 1
}
$gitVer = git --version 2>&1
Write-OK $gitVer

# -- 3. Install Chocolatey + make (if missing) --------------------------
Write-Step "Checking Chocolatey and make..."
$choco = Get-Command choco -ErrorAction SilentlyContinue
if ($choco) {
    Write-Skip "Chocolatey already installed."
} else {
    Write-Host "      Installing Chocolatey (requires Admin)..." -ForegroundColor Yellow

    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
    if (-not $isAdmin) {
        Write-Err "Chocolatey install requires Admin. Re-run this script as Administrator."
        Write-Host "      Right-click PowerShell -> 'Run as Administrator' -> run setup.ps1 again." -ForegroundColor Yellow
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

$mk = Get-Command make -ErrorAction SilentlyContinue
if ($mk) {
    Write-Skip "make already installed."
} else {
    Write-Host "      Installing make via Chocolatey..." -ForegroundColor Yellow

    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
    if (-not $isAdmin) {
        Write-Err "Installing make requires Admin. Re-run this script as Administrator."
        exit 1
    }

    Invoke-Verbose -Label "choco" -Command "choco" -Arguments @("install", "make", "-y", "--verbose")

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

# -- 4. Create virtual environment --------------------------------------
Write-Step "Setting up virtual environment..."
$venvPath = Join-Path $PSScriptRoot ".venv"
if (Test-Path (Join-Path $venvPath "Scripts\python.exe")) {
    Write-Skip ".venv already exists."
} else {
    Write-Host "      Creating .venv..." -ForegroundColor DarkGray
    python -m venv $venvPath
    Write-OK "Created .venv"
}

# Activate
$activateScript = Join-Path $venvPath "Scripts\Activate.ps1"
. $activateScript
Write-OK "Activated .venv"

# -- 5. Upgrade pip -----------------------------------------------------
Write-Step "Upgrading pip..."
Invoke-Verbose -Label "pip" -Command "python" -Arguments @("-m", "pip", "install", "--upgrade", "pip", "--verbose")
Write-OK "pip upgraded."

# -- 6. Install project dependencies ------------------------------------
Write-Step "Installing project dependencies (dev + infra extras)..."
Write-Host "      This may take a few minutes -- streaming output below:" -ForegroundColor Yellow
Invoke-Verbose -Label "pip" -Command "pip" -Arguments @("install", "-e", ".[dev,infra]", "--verbose")
Write-OK "Dependencies installed (dev + infra extras)."

# -- 7. Install pre-commit hooks ----------------------------------------
Write-Step "Installing git hooks..."
Invoke-Verbose -Label "hooks" -Command "pre-commit" -Arguments @("install", "--hook-type", "pre-commit", "--hook-type", "commit-msg")
Write-OK "Pre-commit + commit-msg hooks installed."

# -- Done ---------------------------------------------------------------
Write-Host ""
Write-Host "  [####################] 100% -- All steps complete!" -ForegroundColor Magenta
Write-Host ""
Write-Host "  ============================================" -ForegroundColor Green
Write-Host "    Setup complete! You're ready to go.       " -ForegroundColor Green
Write-Host "  ============================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Activate venv:   .venv\Scripts\Activate.ps1"
Write-Host "  Available tasks: make help"
Write-Host "  Commit changes:  make commit"
Write-Host "  Run tests:       make test"
Write-Host ""
