<#
.SYNOPSIS
    Scan the built Windows release artifacts with the local Microsoft
    Defender engine and record structured, timestamped evidence.

.DESCRIPTION
    Phase 5 (code signing, SmartScreen and antivirus) of the public
    release plan requires an antivirus scan pipeline for every release.
    This script covers the entirely local, no-public-upload half of that
    pipeline: it locates the installed Microsoft Defender platform,
    scans each of the four dist/ release artifacts (GUI EXE, CLI EXE,
    installer EXE, portable ZIP) with MpCmdRun.exe, and writes a
    machine-readable JSON evidence file plus a human-readable summary to
    test-evidence/av-scan-<timestamp>/.

    This script deliberately does NOT upload anything to a public
    multi-engine scanning service (e.g. VirusTotal) - per the governing
    release plan, that requires explicit owner authorization (HUMAN GATE
    4, item 4) and is documented as a manual step in
    docs/release/RELEASING.md instead of being
    automated here.

.PARAMETER DistDir
    Directory containing the built release artifacts (default: dist/,
    relative to the repo root - the output of scripts/build_windows.ps1).

.EXAMPLE
    pwsh scripts\run_local_av_scan.ps1
    Scans the current dist/ build and writes evidence under
    test-evidence/av-scan-<timestamp>/.
#>

[CmdletBinding()]
param(
    [string]$DistDir = $(Join-Path (Split-Path -Parent $PSScriptRoot) 'dist')
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot

function Find-MpCmdRun {
    $platformDir = Join-Path $env:ProgramData 'Microsoft\Windows Defender\Platform'
    if (-not (Test-Path $platformDir)) {
        throw "Microsoft Defender platform directory not found at $platformDir - is Defender installed/enabled?"
    }
    $latest = Get-ChildItem -Path $platformDir -Directory |
        Sort-Object Name -Descending | Select-Object -First 1
    if (-not $latest) {
        throw "No Microsoft Defender platform version found under $platformDir"
    }
    $mpcmd = Join-Path $latest.FullName 'MpCmdRun.exe'
    if (-not (Test-Path $mpcmd)) {
        throw "MpCmdRun.exe not found at $mpcmd"
    }
    return $mpcmd
}

$MpCmdRun = Find-MpCmdRun
Write-Host "==> Using $MpCmdRun" -ForegroundColor Cyan

$status = Get-MpComputerStatus
if (-not $status.AntivirusEnabled) {
    throw "Microsoft Defender antivirus is disabled on this machine - scan results would not be meaningful."
}
Write-Host "    Signature version : $($status.AntivirusSignatureVersion)"
Write-Host "    Signature date    : $($status.AntivirusSignatureLastUpdated)"

$targets = @(
    @{ Label = 'gui-exe';       Path = Join-Path $DistDir 'bananaflow\bananaflow.exe' },
    @{ Label = 'cli-exe';       Path = Join-Path $DistDir 'bananaflow\bananaflow-cli.exe' },
    @{ Label = 'installer-exe'; Path = (Get-ChildItem -Path $DistDir -Filter 'BananaFlow-*-windows-x64-setup.exe' -File -ErrorAction SilentlyContinue | Select-Object -First 1).FullName },
    @{ Label = 'portable-zip';  Path = (Get-ChildItem -Path $DistDir -Filter 'BananaFlow-*-windows-x64-portable.zip' -File -ErrorAction SilentlyContinue | Select-Object -First 1).FullName }
)

$results = @()
foreach ($t in $targets) {
    if (-not $t.Path -or -not (Test-Path $t.Path)) {
        Write-Host "==> [$($t.Label)] MISSING - skipped (run scripts\build_windows.ps1 first)" -ForegroundColor Yellow
        $results += [ordered]@{
            label   = $t.Label
            path    = $t.Path
            present = $false
        }
        continue
    }

    Write-Host "==> [$($t.Label)] Scanning $($t.Path)" -ForegroundColor Cyan
    $output = & $MpCmdRun -Scan -ScanType 3 -File $t.Path -DisableRemediation 2>&1
    $exitCode = $LASTEXITCODE
    $threatsFound = ($output -join "`n") -notmatch 'found no threats'

    $hash = (Get-FileHash -Path $t.Path -Algorithm SHA256).Hash

    $entry = [ordered]@{
        label        = $t.Label
        path         = $t.Path
        present      = $true
        sha256       = $hash
        exitCode     = $exitCode
        threatsFound = $threatsFound
        rawOutput    = ($output -join "`n")
    }
    $results += $entry

    if ($threatsFound -or $exitCode -ne 0) {
        Write-Host "    RESULT: FLAGGED (exit $exitCode)" -ForegroundColor Red
    } else {
        Write-Host "    RESULT: clean" -ForegroundColor Green
    }
}

$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$evidenceDir = Join-Path $RepoRoot "test-evidence\av-scan-$timestamp"
New-Item -ItemType Directory -Path $evidenceDir -Force | Out-Null

$evidence = [ordered]@{
    scannedAt              = (Get-Date).ToString('o')
    defenderSignatureVersion = $status.AntivirusSignatureVersion
    defenderSignatureDate    = $status.AntivirusSignatureLastUpdated.ToString('o')
    results                = $results
}
$evidenceJsonPath = Join-Path $evidenceDir 'av-scan-results.json'
$evidence | ConvertTo-Json -Depth 6 | Set-Content -Path $evidenceJsonPath -Encoding utf8
Write-Host "`n==> Evidence written to $evidenceJsonPath" -ForegroundColor Cyan

$anyFlagged = $results | Where-Object { $_.present -and ($_.threatsFound -or $_.exitCode -ne 0) }
$anyMissing = $results | Where-Object { -not $_.present }

if ($anyFlagged) {
    Write-Host "`nFAIL: one or more artifacts were flagged by the local Defender scan." -ForegroundColor Red
    exit 1
}
if ($anyMissing) {
    Write-Host "`nINCOMPLETE: one or more expected artifacts were missing (see above)." -ForegroundColor Yellow
    exit 2
}
Write-Host "`nPASS: all release artifacts scanned clean." -ForegroundColor Green
exit 0
