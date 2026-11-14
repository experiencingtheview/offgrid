# Real Windows first-run smoke test.
#
# This intentionally makes every system Python candidate fail, then runs the
# public OffGrid.bat exactly as a double-click would. The launcher must download
# its private runtime and bring up the browser setup page on its own.
[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [int]$TimeoutSeconds = 600,
    [int]$SetupPort = 8099
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath($RepoRoot)
$Bat = Join-Path $RepoRoot "OffGrid.bat"
if (-not (Test-Path -LiteralPath $Bat -PathType Leaf)) {
    throw "OffGrid.bat was not found at $Bat"
}

$TempRoot = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [IO.Path]::GetTempPath() }
$LogDir = Join-Path $TempRoot "offgrid-easy-smoke"
$ShimDir = Join-Path $LogDir "no-python"
$StdoutLog = Join-Path $LogDir "stdout.log"
$StderrLog = Join-Path $LogDir "stderr.log"

Remove-Item -Recurse -Force -LiteralPath $LogDir -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $ShimDir | Out-Null

function Show-Logs {
    foreach ($path in @($StdoutLog, $StderrLog)) {
        Write-Host ""
        Write-Host "--- $path ---"
        if (Test-Path -LiteralPath $path) {
            Get-Content -LiteralPath $path -Tail 160 -ErrorAction SilentlyContinue
        }
    }
}

function Stop-OffGridProcess {
    param($Process)
    if ($null -eq $Process -or $Process.HasExited) {
        return
    }
    & "$env:SystemRoot\System32\taskkill.exe" /PID $Process.Id /T /F 2>$null | Out-Null
    $Process.WaitForExit(10000) | Out-Null
}

# Make the ordinary Python command names fail before PATH is searched further.
# The launcher probes py -3, python and python3 before it downloads its private
# runtime; a passing test therefore proves it did not silently use the runner's
# preinstalled Python.
$Shim = @'
@echo off
echo OffGrid smoke test: system Python is intentionally unavailable. 1>&2
exit /b 1
'@
foreach ($name in @("python", "python3", "py")) {
    Set-Content -LiteralPath (Join-Path $ShimDir "$name.bat") -Value $Shim -Encoding Ascii
}

$KeptPath = @(
    $env:PATH -split ";" |
        Where-Object { $_ -and $_ -notmatch "(?i)python|windowsapps" }
)
$env:PATH = $ShimDir + ";" + ($KeptPath -join ";")
Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONSTARTUP -ErrorAction SilentlyContinue
Remove-Item Env:VIRTUAL_ENV -ErrorAction SilentlyContinue
$env:OFFGRID_ENDPOINT = "http://127.0.0.1:1/v1"

foreach ($name in @("python", "python3", "py")) {
    $resolved = @(& "$env:SystemRoot\System32\where.exe" $name 2>$null)
    if ($LASTEXITCODE -eq 0 -and $resolved.Count -gt 0) {
        $first = [string]$resolved[0]
        if (-not $first.StartsWith($ShimDir, [StringComparison]::OrdinalIgnoreCase)) {
            throw "system $name is still first on PATH: $first"
        }
    }
    & $env:ComSpec /d /c "$name --version" 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        throw "the test shim for $name did not fail; system Python may still be reachable"
    }
}
& $env:ComSpec /d /c "py -3 -c `"import sys`"" 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    throw "the py launcher still found a system Python"
}

Write-Host "Python command names are hidden; launching OffGrid.bat with no arguments."
$Arguments = "/d /c call `"$Bat`""
$Process = $null
try {
    $Process = Start-Process `
        -FilePath $env:ComSpec `
        -ArgumentList $Arguments `
        -WorkingDirectory $RepoRoot `
        -RedirectStandardOutput $StdoutLog `
        -RedirectStandardError $StderrLog `
        -WindowStyle Hidden `
        -PassThru

    $BaseUrl = "http://127.0.0.1:$SetupPort"
    $Deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $Page = $null
    $LastRequestError = ""
    while ((Get-Date) -lt $Deadline) {
        if ($Process.HasExited) {
            throw "OffGrid.bat exited before the setup page opened (exit code $($Process.ExitCode))"
        }
        try {
            $candidate = Invoke-WebRequest -UseBasicParsing -Uri "$BaseUrl/" -TimeoutSec 5 -ErrorAction Stop
            if ($candidate.StatusCode -eq 200 -and $candidate.Content -match "OffGrid setup") {
                $Page = $candidate
                break
            }
        } catch {
            $LastRequestError = $_.Exception.Message
        }
        Start-Sleep -Seconds 2
    }
    if ($null -eq $Page) {
        throw "the setup page did not open at $BaseUrl within $TimeoutSeconds seconds ($LastRequestError)"
    }

    $Health = Invoke-RestMethod -Uri "$BaseUrl/health" -TimeoutSec 10 -ErrorAction Stop
    if ($Health.status -ne "ok" -or $Health.app -ne "OffGrid setup") {
        throw "the setup health endpoint returned an unexpected payload"
    }
    $Status = Invoke-RestMethod -Uri "$BaseUrl/api/status" -TimeoutSec 10 -ErrorAction Stop
    if ($Status.stage -notin @("needs-model", "error")) {
        throw "the setup page did not reach an initial setup state: $($Status.stage)"
    }
    foreach ($asset in @("/setup.css", "/setup.js")) {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "$BaseUrl$asset" -TimeoutSec 10 -ErrorAction Stop
        if ($response.StatusCode -ne 200 -or $response.Content.Length -eq 0) {
            throw "the setup page asset $asset did not load"
        }
    }

    $Output = ""
    if (Test-Path -LiteralPath $StdoutLog) {
        $Output += Get-Content -LiteralPath $StdoutLog -Raw -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $StderrLog) {
        $Output += "`n" + (Get-Content -LiteralPath $StderrLog -Raw -ErrorAction SilentlyContinue)
    }
    if ($Output -notmatch "(?i)private Python runtime") {
        throw "the launcher did not report downloading the private Python runtime"
    }
    if ($Output -notmatch "OffGrid setup is open in your browser") {
        throw "the launcher did not report the setup page as open"
    }

    $RuntimePython = Join-Path $RepoRoot "runtime\python\python.exe"
    if (-not (Test-Path -LiteralPath $RuntimePython -PathType Leaf)) {
        throw "the private Python runtime was not created at $RuntimePython"
    }
    $ReportedPython = (& $RuntimePython -c "import sys; print(sys.executable)" 2>&1 | Out-String).Trim()
    $PythonExit = $LASTEXITCODE
    if ($PythonExit -ne 0) {
        throw "the private Python runtime did not start (exit code $PythonExit)"
    }
    $ExpectedPython = [IO.Path]::GetFullPath($RuntimePython)
    if ([IO.Path]::GetFullPath($ReportedPython) -ne $ExpectedPython) {
        throw "the running interpreter was not the private runtime: $ReportedPython"
    }

    Write-Host ""
    Write-Host "Windows one-click setup smoke test passed."
    Write-Host "  setup page: $($Page.StatusCode) $BaseUrl"
    Write-Host "  setup state: $($Status.stage)"
    Write-Host "  private runtime: $ReportedPython"
} catch {
    Stop-OffGridProcess -Process $Process
    Write-Host ""
    Write-Host "Windows one-click setup smoke test failed: $($_.Exception.Message)"
    Show-Logs
    throw
} finally {
    Stop-OffGridProcess -Process $Process
}
