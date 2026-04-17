#Requires -Version 5.1
<#
.SYNOPSIS
    Builds StellarInsight.exe, the installer, and optionally publishes a GitHub Release.

.PARAMETER Release
    After building, bump the version, commit, tag, and publish to GitHub Releases.

.PARAMETER Version
    Explicit version string to use when releasing (e.g. "1.2.0").
    If omitted the patch number is auto-incremented.

.EXAMPLE
    # Normal build only
    powershell -ExecutionPolicy Bypass -File .\build.ps1

    # Build + publish release (auto-bumps patch: 1.0.0 -> 1.0.1)
    powershell -ExecutionPolicy Bypass -File .\build.ps1 -Release

    # Build + publish release with explicit version
    powershell -ExecutionPolicy Bypass -File .\build.ps1 -Release -Version "1.1.0"
#>
param(
    [switch]$Release,
    [string]$Version = ""
)

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
# Version management
# ---------------------------------------------------------------------------
$versionFile = Join-Path $PSScriptRoot "VERSION"
$currentVersion = (Get-Content $versionFile -Raw).Trim()

if ($Release) {
    if ($Version -ne "") {
        $newVersion = $Version.Trim()
    } else {
        # Auto-increment patch: 1.0.0 -> 1.0.1
        $parts = $currentVersion.Split('.')
        $parts[2] = [string]([int]$parts[2] + 1)
        $newVersion = $parts -join '.'
    }
    Info "Version: $currentVersion  ->  $newVersion"
    # Update VERSION file
    Set-Content $versionFile $newVersion
    # Update installer.iss
    (Get-Content "installer.iss" -Raw) -replace '#define AppVersion\s+"[^"]+"', "#define AppVersion      `"$newVersion`"" |
        Set-Content "installer.iss"
    # Update app.py
    (Get-Content "app.py" -Raw) -replace 'version="[0-9]+\.[0-9]+\.[0-9]+"', "version=`"$newVersion`"" |
        Set-Content "app.py"
    $currentVersion = $newVersion
} else {
    $newVersion = $currentVersion
    Info "Version: $currentVersion  (use -Release to publish)"
}

# ---------------------------------------------------------------------------
Head "Step 1 / 4 -- Checking Python"

$py = $null
foreach ($candidate in @("python", "python3", "py")) {
    try {
        $ver = & $candidate --version 2>&1
        if ($ver -match "Python 3\.(\d+)") {
            $minor = [int]$Matches[1]
            if ($minor -lt 10) { Warn "Python 3.$minor found -- 3.10+ recommended"; continue }
            $py = $candidate
            Ok "$ver  ($candidate)"
            break
        }
    } catch { }
}
if (-not $py) { Fail "Python 3.10+ not found. Install from https://python.org and add to PATH." }

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
    Fail "xylon_eve.spec not found. Run this script from inside the StellarInsight\ folder."
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
if (-not (Test-Path $exe)) { Fail "Expected dist\StellarInsight.exe was not produced." }

$sizeMB = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Ok "dist\StellarInsight.exe  ($($sizeMB) MB)"

# ---------------------------------------------------------------------------
Head "Step 4 / 4 -- Building installer with Inno Setup"

$iscc = $null
foreach ($c in @(
    "iscc",
    "${env:ProgramFiles(x86)}\Inno Setup 6\iscc.exe",
    "${env:ProgramFiles}\Inno Setup 6\iscc.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 5\iscc.exe",
    "${env:ProgramFiles}\Inno Setup 5\iscc.exe"
)) {
    if (Test-Path $c -ErrorAction SilentlyContinue) { $iscc = $c; break }
    try { $null = & $c /? 2>&1; if ($LASTEXITCODE -eq 0) { $iscc = $c; break } } catch { }
}

$setup = $null
if (-not $iscc) {
    Warn "Inno Setup not found -- skipping installer step."
    Warn "Install from https://jrsoftware.org/isdl.php then rebuild."
} else {
    if (-not (Test-Path "installer")) { New-Item -ItemType Directory "installer" | Out-Null }
    Info "Running Inno Setup compiler..."
    & $iscc installer.iss
    if ($LASTEXITCODE -ne 0) { Fail "Inno Setup failed -- see output above." }
    $setup = Join-Path $PSScriptRoot "installer\StellarInsight_Setup.exe"
    if (-not (Test-Path $setup)) { Fail "Expected installer\StellarInsight_Setup.exe was not produced." }
    $sizeMB2 = [math]::Round((Get-Item $setup).Length / 1MB, 1)
    Ok "installer\StellarInsight_Setup.exe  ($($sizeMB2) MB)"
}

# ---------------------------------------------------------------------------
# GitHub Release (only when -Release flag is set)
# ---------------------------------------------------------------------------
if ($Release) {
    Head "Step 5 / 5 -- Publishing GitHub Release v$newVersion"

    # Check gh CLI is available
    $gh = $null
    try { $null = & gh --version 2>&1; $gh = "gh" } catch { }
    if (-not $gh) { Fail "GitHub CLI (gh) not found. Install from https://cli.github.com then run: gh auth login" }

    if (-not $setup) { Fail "Installer was not built -- cannot publish release without it." }

    # Commit version bumps
    Info "Committing version bump..."
    & git add VERSION installer.iss app.py
    & git commit -m "chore: bump version to $newVersion"
    if ($LASTEXITCODE -ne 0) { Warn "Git commit failed -- maybe nothing changed?" }

    # Tag
    Info "Tagging v$newVersion..."
    & git tag -a "v$newVersion" -m "Release v$newVersion"
    if ($LASTEXITCODE -ne 0) { Fail "Git tag failed." }

    # Push commit + tag
    Info "Pushing to GitHub..."
    & git push
    & git push --tags
    if ($LASTEXITCODE -ne 0) { Fail "Git push failed." }

    # Create GitHub Release and upload installer
    Info "Creating GitHub Release..."
    & gh release create "v$newVersion" $setup `
        --title "Stellar Insight v$newVersion" `
        --notes "## Stellar Insight v$newVersion`n`nDownload and run ``StellarInsight_Setup.exe`` to install or update." `
        --latest
    if ($LASTEXITCODE -ne 0) { Fail "GitHub release creation failed." }

    Ok "GitHub Release v$newVersion published!"
    Info "https://github.com/wangdeep/StellarInsight/releases/tag/v$newVersion"
}

# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "========================================================" -ForegroundColor Green
Write-Host "  Build complete!  v$newVersion" -ForegroundColor Green
Write-Host ""
Write-Host "  Standalone exe  ->  dist\StellarInsight.exe" -ForegroundColor White
if ($setup) {
    Write-Host "  Installer       ->  installer\StellarInsight_Setup.exe" -ForegroundColor White
}
if ($Release) {
    Write-Host "  GitHub Release  ->  https://github.com/wangdeep/StellarInsight/releases" -ForegroundColor White
}
Write-Host "========================================================" -ForegroundColor Green
Write-Host ""
