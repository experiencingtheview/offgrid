# OffGrid first-run diagnostics for Windows.
#
# Run this from an extracted OffGrid folder when setup does not open. It needs
# no Python. It writes launcher output, SmartScreen/Mark-of-the-Web state, and
# the setup-page result into %TEMP%, then prints a zip to send with a report.
[CmdletBinding()]
param(
    [string]$Root = "",
    [int]$SetupPort = 8099,
    [int]$TimeoutSeconds = 300,
    [string]$DownloadedZip = "",
    [switch]$Unblock
)

$ErrorActionPreference = "Stop"

if (-not $Root) {
    $Root = Split-Path -Parent $PSScriptRoot
}
$Root = [IO.Path]::GetFullPath($Root)
$Bat = Join-Path $Root "OffGrid.bat"
if (-not (Test-Path -LiteralPath $Bat -PathType Leaf)) {
    throw "OffGrid.bat was not found at $Bat. Extract the whole zip and run this script from its scripts folder."
}
if ($DownloadedZip) {
    $DownloadedZip = [IO.Path]::GetFullPath($DownloadedZip)
}

$TempRoot = if ($env:TEMP) { $env:TEMP } else { [IO.Path]::GetTempPath() }
$OutDir = Join-Path $TempRoot ("offgrid-diagnostics-" + (Get-Date -Format "yyyyMMdd-HHmmss") + "-" + $PID)
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$Summary = Join-Path $OutDir "summary.txt"
$Stdout = Join-Path $OutDir "launcher.stdout.log"
$Stderr = Join-Path $OutDir "launcher.stderr.log"
$StatusFile = Join-Path $OutDir "status.json"
$PageFile = Join-Path $OutDir "setup-page.html"
$Zip = $OutDir + ".zip"
$Process = $null
$script:ExitCode = 0

function Add-Line {
    param([string]$Text)
    Write-Host $Text
    Add-Content -LiteralPath $script:Summary -Value $Text -Encoding UTF8
}

function Get-RegValue {
    param([string]$Path, [string]$Name)
    try {
        $item = Get-ItemProperty -LiteralPath $Path -Name $Name -ErrorAction Stop
        $property = $item.PSObject.Properties[$Name]
        if ($null -eq $property) {
            return "(not set)"
        }
        return [string]$property.Value
    } catch {
        return "(not set)"
    }
}

function Get-ZoneState {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return "missing"
    }
    try {
        $null = Get-Item -LiteralPath $Path -Stream Zone.Identifier -ErrorAction Stop
    } catch {
        return "not blocked"
    }
    try {
        $info = [string](Get-Content -LiteralPath $Path -Stream Zone.Identifier -Raw -ErrorAction Stop)
        $info = ($info -replace "`r?`n", "; ").Trim()
        return "BLOCKED ($info)"
    } catch {
        return "BLOCKED (stream present; could not read details)"
    }
}

function Show-ZoneState {
    param([string]$Path)
    Add-Line ("  " + (Get-ZoneState -Path $Path) + ": " + $Path)
}

function Get-SetupHealth {
    try {
        return Invoke-RestMethod -Uri "http://127.0.0.1:$SetupPort/health" -TimeoutSec 2 -ErrorAction Stop
    } catch {
        return $null
    }
}

function Stop-OffGridProcess {
    param($Process)
    if ($null -eq $Process -or $Process.HasExited) {
        return
    }
    try {
        & "$env:SystemRoot\System32\taskkill.exe" /PID $Process.Id /T /F 2>$null | Out-Null
        $Process.WaitForExit(5000) | Out-Null
    } catch {
        # The process may have exited between the check and taskkill.
    }
}

try {
    Add-Line "OffGrid Windows first-run diagnostics"
    Add-Line ("Created: " + (Get-Date -Format o))
    Add-Line ("Root: " + $Root)
    Add-Line ("Windows: " + [Environment]::OSVersion.VersionString)
    Add-Line ("Architecture: " + $env:PROCESSOR_ARCHITECTURE)
    Add-Line ("64-bit OS: " + [Environment]::Is64BitOperatingSystem)
    Add-Line ("PowerShell: " + $PSVersionTable.PSVersion.ToString())
    try {
        $os = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop
        Add-Line ("Windows caption: " + $os.Caption + " " + $os.Version + " build " + $os.BuildNumber)
    } catch {
        Add-Line ("Windows caption: unavailable (" + $_.Exception.Message + ")")
    }
    foreach ($name in @("py", "python", "python3")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($null -eq $command) {
            Add-Line ("Python command " + $name + ": not found")
        } else {
            Add-Line ("Python command " + $name + ": " + $command.Source)
        }
    }

    Add-Line ""
    Add-Line "SmartScreen / Smart App Control"
    Add-Line ("  SmartScreenEnabled (system): " + (Get-RegValue "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer" "SmartScreenEnabled"))
    Add-Line ("  SmartScreenEnabled (user): " + (Get-RegValue "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer" "SmartScreenEnabled"))
    Add-Line ("  ShellSmartScreenLevel: " + (Get-RegValue "HKLM:\SOFTWARE\Policies\Microsoft\Windows\System" "ShellSmartScreenLevel"))
    Add-Line ("  EnableSmartScreen policy: " + (Get-RegValue "HKLM:\SOFTWARE\Policies\Microsoft\Windows\System" "EnableSmartScreen"))
    Add-Line ("  Smart App Control state: " + (Get-RegValue "HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy" "VerifiedAndReputablePolicyState"))
    Add-Line "  (Smart App Control values: 0=off, 1=enforced, 2=evaluation; not set=not configured)"

    Add-Line ""
    Add-Line "Mark-of-the-Web"
    $Files = @(
        (Join-Path $Root "OffGrid.bat"),
        (Join-Path $Root "launch\offgrid.bat"),
        (Join-Path $Root "launch\bootstrap.ps1"),
        (Join-Path $Root "scripts\windows_diagnostics.ps1")
    )
    if ($DownloadedZip) {
        $Files = @($DownloadedZip) + $Files
    } else {
        Add-Line "  Downloaded zip: not supplied (use -DownloadedZip if you still have it)"
    }
    foreach ($path in $Files) {
        Show-ZoneState $path
    }

    if ($Unblock) {
        Add-Line ""
        Add-Line "Unblock requested; removing only the Mark-of-the-Web"
        foreach ($path in $Files) {
            if (Test-Path -LiteralPath $path -PathType Leaf) {
                try {
                    Unblock-File -LiteralPath $path -ErrorAction Stop
                    Add-Line ("  Unblocked: " + $path)
                } catch {
                    Add-Line ("  Could not unblock " + $path + ": " + $_.Exception.Message)
                }
            }
        }
        try {
            Get-ChildItem -LiteralPath $Root -Recurse -File -ErrorAction SilentlyContinue |
                ForEach-Object { Unblock-File -LiteralPath $_.FullName -ErrorAction SilentlyContinue }
            Add-Line "  Requested unblock for extracted files."
        } catch {
            Add-Line ("  Could not unblock extracted files: " + $_.Exception.Message)
        }
        Add-Line "  After unblock:"
        foreach ($path in $Files) {
            Show-ZoneState $path
        }
    }

    Add-Line ""
    Add-Line "One-click setup"
    $BaseUrl = "http://127.0.0.1:$SetupPort"
    $existing = Get-SetupHealth
    if ($existing -and $existing.app -eq "OffGrid setup") {
        Add-Line "  A setup page was already running; not starting another."
    } else {
        Add-Line "  Starting OffGrid.bat with no arguments..."
        $arguments = "/d /c call `"$Bat`""
        $oldPythonUnbuffered = $env:PYTHONUNBUFFERED
        $env:PYTHONUNBUFFERED = "1"
        try {
            $Process = Start-Process `
                -FilePath $env:ComSpec `
                -ArgumentList $arguments `
                -WorkingDirectory $Root `
                -RedirectStandardOutput $Stdout `
                -RedirectStandardError $Stderr `
                -WindowStyle Hidden `
                -PassThru
        } finally {
            if ($null -eq $oldPythonUnbuffered) {
                Remove-Item Env:PYTHONUNBUFFERED -ErrorAction SilentlyContinue
            } else {
                $env:PYTHONUNBUFFERED = $oldPythonUnbuffered
            }
        }

        $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
        $health = $null
        $reportedOpen = $false
        $reportedReady = $false
        $studioUrl = ""
        $failureMarker = ""
        while ((Get-Date) -lt $deadline) {
            if ($Process.HasExited) {
                break
            }
            $health = Get-SetupHealth
            if ($health) {
                break
            }
            $launcherText = ""
            try {
                $launcherText = Get-Content -LiteralPath $Stdout -Raw -ErrorAction Stop
            } catch {
                # stdout.log may not exist for the first second.
            }
            if ($launcherText -match "OffGrid is ready\.") {
                $reportedReady = $true
                if ($launcherText -match "Open:\s*(http://127\.0\.0\.1:\d+)") {
                    $studioUrl = $Matches[1].TrimEnd([char]"/")
                }
                break
            }
            if ($launcherText -match "http://127\.0\.0\.1:(\d+)/") {
                $reportedPort = [int]$Matches[1]
                if ($reportedPort -ne $SetupPort) {
                    $SetupPort = $reportedPort
                    $BaseUrl = "http://127.0.0.1:$SetupPort"
                    $health = Get-SetupHealth
                    if ($health) {
                        break
                    }
                }
            }
            if ($launcherText -match "OffGrid setup is open in your browser") {
                $reportedOpen = $true
                break
            }
            if ($launcherText -match "could not set up its private Python runtime") {
                $failureMarker = "private Python setup failed"
                break
            }
            if ($launcherText -match "could not start or is older than 3\.9") {
                $failureMarker = "private Python runtime is broken"
                break
            }
            if ($launcherText -match "OffGrid exited with code|Press any key to close this window") {
                $failureMarker = "launcher stopped at its error/pause prompt"
                break
            }
            Start-Sleep -Seconds 2
        }

        if ($health) {
            Add-Line ("  Setup page answered on " + $BaseUrl)
        } elseif ($reportedReady) {
            if ($studioUrl) {
                $BaseUrl = $studioUrl
                Add-Line ("  OffGrid reported ready; app page: " + $studioUrl)
            } else {
                Add-Line "  OffGrid reported ready."
            }
        } elseif ($reportedOpen) {
            Add-Line "  Launcher reported the setup page was open, but its health endpoint did not answer."
        } elseif ($failureMarker) {
            Add-Line ("  Launcher reported a failure: " + $failureMarker + " (see launcher stdout).")
        } elseif ($Process.HasExited) {
            Add-Line ("  Launcher exited before the setup page opened (exit code " + $Process.ExitCode + ").")
        } else {
            Add-Line ("  Setup page did not answer within " + $TimeoutSeconds + " seconds.")
        }
    }

    try {
        $setup = Invoke-RestMethod -Uri "$BaseUrl/api/status" -TimeoutSec 10 -ErrorAction Stop
        $setup | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $StatusFile -Encoding UTF8
        Add-Line ("  Setup stage: " + $setup.stage)
        Add-Line ("  Setup message: " + $setup.message)
        if ($setup.error) {
            Add-Line ("  Setup error: " + $setup.error)
        }
    } catch {
        Add-Line ("  Status endpoint did not answer: " + $_.Exception.Message)
    }

    try {
        $page = Invoke-WebRequest -UseBasicParsing -Uri "$BaseUrl/" -TimeoutSec 10 -ErrorAction Stop
        Set-Content -LiteralPath $PageFile -Value $page.Content -Encoding UTF8
        Add-Line ("  Setup page: HTTP " + $page.StatusCode)
    } catch {
        Add-Line ("  Setup page fetch failed: " + $_.Exception.Message)
    }

    $RuntimePython = Join-Path $Root "runtime\python\python.exe"
    if (Test-Path -LiteralPath $RuntimePython -PathType Leaf) {
        Add-Line "  Private runtime: present"
        try {
            $version = (& $RuntimePython -c "import sys; print(sys.version.split()[0])" 2>&1 | Out-String).Trim()
            if ($LASTEXITCODE -eq 0) {
                Add-Line ("  Private runtime version: " + $version)
            } else {
                Add-Line ("  Private runtime did not start: " + $version)
            }
        } catch {
            Add-Line ("  Private runtime did not start: " + $_.Exception.Message)
        }
    } else {
        Add-Line "  Private runtime: not present"
    }

    Add-Line ("  Launcher stdout: " + $Stdout)
    Add-Line ("  Launcher stderr: " + $Stderr)
} catch {
    Add-Line ("Diagnostic failed: " + $_.Exception.Message)
    $script:ExitCode = 1
} finally {
    Stop-OffGridProcess -Process $Process
    Add-Line ""
    Add-Line ("Logs: " + $OutDir)
    try {
        if (Get-Command Compress-Archive -ErrorAction SilentlyContinue) {
            Compress-Archive -Path (Join-Path $OutDir "*") -DestinationPath $Zip -Force
            Write-Host ("Log bundle: " + $Zip)
        } else {
            Write-Host "Compress-Archive is unavailable; attach the files in the folder above."
        }
    } catch {
        Write-Host ("Could not create the log zip: " + $_.Exception.Message)
        Write-Host ("Attach the files in: " + $OutDir)
    }
}

exit $script:ExitCode
