# Download a private Python runtime into this OffGrid folder on first launch.
# It is used only by OffGrid and is never installed system-wide.
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Runtime = Join-Path $Root "runtime\python"
$Python = Join-Path $Runtime "python.exe"
$DownloadDir = Join-Path $Root ".downloads"
$Archive = Join-Path $DownloadDir "python-runtime.tar.gz"
$Temporary = Join-Path $Root "runtime\python-bootstrap"

if (Test-Path $Python) {
    # A previous download may have been interrupted. Run it before trusting it.
    & $Python -c "import sys" 2>$null
    if ($LASTEXITCODE -eq 0) {
        exit 0
    }
}

$Machine = $env:PROCESSOR_ARCHITECTURE
if ($Machine -eq "ARM64") {
    $Triple = "aarch64-pc-windows-msvc"
} else {
    $Triple = "x86_64-pc-windows-msvc"
}

Write-Host "  OffGrid: first run needs a small Python runtime."
Write-Host "  It is stored inside this folder, not installed on the computer."
Write-Host "  Downloading a private Python runtime (about 65 MB, one time)..."

New-Item -ItemType Directory -Force -Path $DownloadDir, (Split-Path -Parent $Temporary) | Out-Null

# A stable, immutable python-build-standalone release. Pinning it avoids the
# anonymous GitHub API limits and keeps setup deterministic.
$Url = "https://github.com/astral-sh/python-build-standalone/releases/download/20261110/cpython-3.12.16%2B20261110-$Triple-install_only.tar.gz"

try {
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Archive -TimeoutSec 300
} catch {
    Write-Host ""
    Write-Host "OffGrid: the Python download failed. Check the internet connection and try again."
    exit 1
}

if (Test-Path $Temporary) {
    Remove-Item -Recurse -Force $Temporary
}
New-Item -ItemType Directory -Force -Path $Temporary | Out-Null

$Tar = Get-Command tar.exe -ErrorAction SilentlyContinue
if (-not $Tar) {
    Write-Host ""
    Write-Host "OffGrid: Windows could not unpack the Python runtime."
    Write-Host "Open Microsoft Store and update Windows, then run OffGrid again."
    exit 1
}

try {
    & $Tar.Source -xzf $Archive -C $Temporary
    $Extracted = Join-Path $Temporary "python\python.exe"
    if (-not (Test-Path $Extracted)) {
        throw "unexpected archive layout"
    }
    if (Test-Path $Runtime) {
        Remove-Item -Recurse -Force $Runtime
    }
    Move-Item (Join-Path $Temporary "python") $Runtime

    & $Python -c "import sys" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "runtime did not start"
    }
} catch {
    Write-Host ""
    Write-Host "OffGrid: the Python runtime could not be unpacked or started."
    exit 1
} finally {
    Remove-Item -Recurse -Force $Temporary -ErrorAction SilentlyContinue
    Remove-Item -Force $Archive -ErrorAction SilentlyContinue
}

Write-Host "  Python is ready. Starting OffGrid..."
exit 0
