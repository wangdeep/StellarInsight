#Requires -Version 5.1
<#
.SYNOPSIS
    Builds StellarInsight.exe and the Windows installer in one shot.

.DESCRIPTION
    1. Checks prerequisites (Python, Inno Setup)
    2. Installs / upgrades all Python dependencies
    3. Runs PyInstaller  ->  dist\StellarInsight.exe
    4. Runs Inno Setup   ->  installer\StellarInsight_Setup.exe

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\build.ps1
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Info  { param($m) Write-Host "  $m"          -ForegroundColor Cyan   }
function Ok    { param($m) Write-Host "  [OK]  $m"    -ForegroundColor Green  }
function Warn  { param($m) Write-Host "  [!!]  $m"    -ForegroundColor Yellow }
function Fail  {
    param($m)
    Write-Host ""
    Write-Host "  [FAIL]  $m" -ForegroundColor Red
    Write-Host ""
    exit 1
}
function Head  { param($m) Write-Host "`n===  $m  ===" -ForegroundColor Magenta }

Set-Location $PSScriptRoot

Head "Stellar Insight -- Windows Build"
Info "Working directory: $PSScriptRoot"

# ---------------------------------------------------------------------------
Head "Step 1 / 4 -- Checking Python"

$py = $null
foreach ($candidate in @("python", "python3", "py")) {
    try {
        $ver = & $candidate --version 2>&1
        if ($ver -match "Python 3\.(\d+)") {
            $minor = [int]$Matches[1]
            if ($minor -lt 10) {
                Warn "Python 3.$minor found -- 3.10+ recommended"
                continue
            }
            $py = $candidate
            Ok "$ver  ($candidate)"
            break
        }
    } catch { }
}
if (-not $py) {
    Fail "Python 3.10+ not found. Install from https://python.org and add to PATH."
}

# ---------------------------------------------------------------------------
Head "Step 2 / 4 -- Installing / upgrading dependencies"

$deps = @(
    "pyinstaller>=6.0",
    "pywebview>=5.0.0",
    "pystray>=0.19.0",
    "pillow>=10.0.0",
    "fastapi>=0.110.0",
    "uvicorn[standard]>=0.29.0",
    "jinja2>=3.1.0",
    "python-multipart>=0.0.9",
    "itsdangerous>=2.1.0",
    "starlette>=0.36.0",
    "aiohttp>=3.9.0",
    "httpx>=0.27.0",
    "websockets>=12.0",
    "cryptography>=42.0.0"
)

Info "Upgrading pip..."
& $py -m pip install --upgrade --quiet pip
if ($LASTEXITCODE -ne 0) { Fail "pip upgrade failed." }

Info "Installing packages..."
& $py -m pip install --upgrade --quiet $deps
if ($LASTEXITCODE -ne 0) { Fail "Dependency install failed. See output above." }
Ok "All dependencies installed"

# ---------------------------------------------------------------------------
Head "Step 3 / 4 -- Building executable with PyInstaller"

if (-not (Test-Path "xylon_eve.spec")) {
    Fail "xylon_eve.spec not found. Run this script from inside the xylon_eve\ folder."
}

foreach ($dir in @("build", "dist")) {
    if (Test-Path $dir) {
        Info "Removing old $dir\"
        Remove-Item $dir -Recurse -Force
    }
}

Info "Running PyInstaller (this takes a minute)..."
& $py -m PyInstaller xylon_eve.spec --clean --noconfirm
if ($LASTEXITCODE -ne 0) { Fail "PyInstaller failed -- see output above." }

$exe = Join-Path $PSScriptRoot "dist\StellarInsight.exe"
if (-not (Test-Path $exe)) {
    Fail "Expected dist\StellarInsight.exe was not produced."
}

$sizeMB = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Ok "dist\StellarInsight.exe  ($($sizeMB) MB)"

# ---------------------------------------------------------------------------
Head "Step 4 / 4 -- Building installer with Inno Setup"

$iscc = $null
$isccCandidates = @(
    "iscc",
    "${env:ProgramFiles(x86)}\Inno Setup 6\iscc.exe",
    "${env:ProgramFiles}\Inno Setup 6\iscc.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 5\iscc.exe",
    "${env:ProgramFiles}\Inno Setup 5\iscc.exe"
)

foreach ($c in $isccCandidates) {
    if (Test-Path $c -ErrorAction SilentlyContinue) {
        $iscc = $c
        break
    }
    try {
        $null = & $c /? 2>&1
        if ($LASTEXITCODE -eq 0) { $iscc = $c; break }
    } catch { }
}

if (-not $iscc) {
    Warn "Inno Setup not found -- skipping installer step."
    Warn "To build the installer:"
    Warn "  1. Install Inno Setup 6: https://jrsoftware.org/isdl.php"
    Warn "  2. Run this script again, or run:  iscc installer.iss"
} else {
    if (-not (Test-Path "installer.iss")) {
        Fail "installer.iss not found."
    }

    if (-not (Test-Path "installer")) {
        New-Item -ItemType Directory "installer" | Out-Null
    }

    Info "Running Inno Setup compiler..."
    & $iscc installer.iss
    if ($LASTEXITCODE -ne 0) { Fail "Inno Setup failed -- see output above." }

    $setup = Join-Path $PSScriptRoot "installer\StellarInsight_Setup.exe"
    if (-not (Test-Path $setup)) {
        Fail "Expected installer\StellarInsight_Setup.exe was not produced."
    }

    $sizeMB2 = [math]::Round((Get-Item $setup).Length / 1MB, 1)
    Ok "installer\StellarInsight_Setup.exe  ($($sizeMB2) MB)"
}

# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "========================================================" -ForegroundColor Green
Write-Host "  Build complete!" -ForegroundColor Green
Write-Host ""
Write-Host "  Standalone exe  ->  dist\StellarInsight.exe" -ForegroundColor White
if ($iscc) {
    Write-Host "  Installer       ->  installer\StellarInsight_Setup.exe" -ForegroundColor White
}
Write-Host "========================================================" -ForegroundColor Green
Write-Host ""
