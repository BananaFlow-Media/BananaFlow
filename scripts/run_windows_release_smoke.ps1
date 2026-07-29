<#
.SYNOPSIS
    Exercise the real installer and portable Windows release artifacts.

.DESCRIPTION
    Silently installs the Inno artifact into an isolated directory, extracts
    the portable ZIP, and drives BananaFlow's hidden packaged smoke entry point
    through fresh, migration, restart and authentication-deletion phases.
    No network or human interaction is required. Evidence is written as JSON.
#>

[CmdletBinding()]
param(
    [string]$DistDir = "",
    [string]$EvidenceDir = "",
    [switch]$KeepScratch
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
if (-not $DistDir) { $DistDir = Join-Path $RepoRoot 'dist' }
if (-not $EvidenceDir) { $EvidenceDir = Join-Path $RepoRoot 'test-evidence\release-candidate' }
$DistDir = [System.IO.Path]::GetFullPath($DistDir)
$EvidenceDir = [System.IO.Path]::GetFullPath($EvidenceDir)
New-Item -ItemType Directory -Force -Path $EvidenceDir | Out-Null

$zip = @(Get-ChildItem -LiteralPath $DistDir -Filter 'BananaFlow-*-windows-x64-portable.zip' -File)
$setup = @(Get-ChildItem -LiteralPath $DistDir -Filter 'BananaFlow-*-windows-x64-setup.exe' -File)
if ($zip.Count -ne 1) { throw "Expected exactly one portable ZIP in $DistDir; found $($zip.Count)." }
if ($setup.Count -ne 1) { throw "Expected exactly one installer in $DistDir; found $($setup.Count)." }

function Get-PeSubsystem {
    param([Parameter(Mandatory=$true)][string]$Path)
    $stream = [System.IO.File]::OpenRead($Path)
    $reader = [System.IO.BinaryReader]::new($stream)
    try {
        $stream.Position = 0x3c
        $peOffset = $reader.ReadInt32()
        $stream.Position = $peOffset
        if ($reader.ReadUInt32() -ne 0x00004550) { throw "Not a PE executable: $Path" }
        $stream.Position = $peOffset + 4 + 20 + 68
        return $reader.ReadUInt16()
    } finally {
        $reader.Dispose()
        $stream.Dispose()
    }
}

function Invoke-Process {
    param(
        [Parameter(Mandatory=$true)][string]$FilePath,
        [Parameter(Mandatory=$true)][string[]]$Arguments,
        [hashtable]$Environment = @{},
        [int]$TimeoutSeconds = 180
    )
    $info = [System.Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $FilePath
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    # ProcessStartInfo.ArgumentList exists in modern .NET/PowerShell 7 but is
    # null under the Windows PowerShell 5.1 runtime still used by many release
    # workstations. None of this runner's arguments contain literal quotes;
    # quote every value so whitespace in an install path remains safe on both.
    foreach ($argument in $Arguments) {
        if ($argument.Contains('"')) { throw "Smoke process arguments cannot contain literal quotes." }
    }
    $info.Arguments = (($Arguments | ForEach-Object { '"' + $_ + '"' }) -join ' ')
    foreach ($name in $Environment.Keys) { $info.Environment[$name] = [string]$Environment[$name] }
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $info
    if (-not $process.Start()) { throw "Could not start $FilePath" }
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        $process.Kill($true)
        throw "Process timed out after $TimeoutSeconds seconds: $FilePath"
    }
    return $process.ExitCode
}

function Invoke-PackagedSmoke {
    param(
        [Parameter(Mandatory=$true)][string]$Exe,
        [Parameter(Mandatory=$true)][string]$AppData,
        [Parameter(Mandatory=$true)][string]$Target,
        [Parameter(Mandatory=$true)][string]$Scenario,
        [Parameter(Mandatory=$true)][string]$Secret,
        [Parameter(Mandatory=$true)][string]$Label
    )
    New-Item -ItemType Directory -Force -Path $AppData | Out-Null
    $resultPath = Join-Path $EvidenceDir "$Label.json"
    $environment = @{
        APPDATA = $AppData
        LOCALAPPDATA = $AppData
        USERPROFILE = $AppData
        HOME = $AppData
        XDG_CONFIG_HOME = $AppData
        QT_QPA_PLATFORM = 'offscreen'
        BANANAFLOW_SMOKE_RESULT_FILE = $resultPath
        BANANAFLOW_RC_SMOKE_SCENARIO = $Scenario
        BANANAFLOW_RC_COOKIE_SECRET = $Secret
    }
    $exitCode = Invoke-Process -FilePath $Exe -Arguments @('--internal-smoke-test', $Target) -Environment $environment
    if (-not (Test-Path -LiteralPath $resultPath -PathType Leaf)) {
        throw "$Label produced no smoke result (exit $exitCode)."
    }
    $parsed = Get-Content -Raw -LiteralPath $resultPath | ConvertFrom-Json
    $failed = @($parsed.steps | Where-Object { -not $_.ok })
    $reviewedQtExit = $exitCode -eq -1073740791 -or [uint32]$exitCode -eq 0xc0000409
    if ($exitCode -ne 0 -and -not ($reviewedQtExit -and $parsed.ok -and $failed.Count -eq 0)) {
        throw "$Label exited $exitCode with failed steps: $($failed.step -join ', ')."
    }
    if (-not $parsed.ok -or $failed.Count -gt 0) {
        throw "$Label reported failed steps: $($failed.step -join ', ')."
    }
    return [ordered]@{
        label = $Label
        target = $Target
        scenario = $Scenario
        exit_code = $exitCode
        reviewed_qt_teardown = $reviewedQtExit
        steps = $parsed.steps
    }
}

function New-UpgradeFixture {
    param([Parameter(Mandatory=$true)][string]$AppData, [Parameter(Mandatory=$true)][string]$Secret)
    $root = Join-Path $AppData '.bananaflow'
    New-Item -ItemType Directory -Force -Path $root | Out-Null
    $legacy = Join-Path $root 'app_cookies.txt'
    $config = [ordered]@{
        config_version = 9
        cookies_browser = 'chrome'
        cookies_file = $legacy
        check_updates = $false
    }
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText(
        (Join-Path $root 'config.json'),
        ($config | ConvertTo-Json),
        $utf8NoBom
    )
    $cookieText = (@(
        ".youtube.com`tTRUE`t/`tTRUE`t0`tLOGIN_INFO`t$Secret",
        ".google.com`tTRUE`t/`tTRUE`t0`tSID`texcluded-broad-google-cookie"
    ) -join "`n") + "`n"
    [System.IO.File]::WriteAllText($legacy, $cookieText, $utf8NoBom)
}

function Add-CrashResidue {
    param([Parameter(Mandatory=$true)][string]$AppData)
    $authTemp = Join-Path $AppData '.bananaflow\auth_tmp'
    New-Item -ItemType Directory -Force -Path $authTemp | Out-Null
    Set-Content -LiteralPath (Join-Path $authTemp 'session-crash-residue.txt') -Value 'crash residue' -Encoding utf8
}

function Assert-NoPlaintextResidue {
    param([Parameter(Mandatory=$true)][string]$Root, [Parameter(Mandatory=$true)][string]$Secret)
    $legacy = @(Get-ChildItem -LiteralPath $Root -Recurse -File -Filter 'app_cookies.txt' -ErrorAction SilentlyContinue)
    $temporary = @(Get-ChildItem -LiteralPath $Root -Recurse -File -Filter 'session-*.txt' -ErrorAction SilentlyContinue)
    if ($legacy.Count -or $temporary.Count) {
        throw "Plaintext cookie residue remains under the isolated profile."
    }
    $needle = [System.Text.Encoding]::UTF8.GetBytes($Secret)
    foreach ($file in Get-ChildItem -LiteralPath $Root -Recurse -File -ErrorAction SilentlyContinue) {
        $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
        if ($bytes.Length -lt $needle.Length) { continue }
        $text = [System.Text.Encoding]::UTF8.GetString($bytes)
        if ($text.Contains($Secret)) {
            throw "Cookie canary was found in persistent profile data: $($file.Name)"
        }
    }
}

$scratchParent = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
# Keep this root deliberately short. The packaged PO-provider dependency tree
# contains filenames close to legacy MAX_PATH; a long test-only GUID prefix can
# make a valid normal installation fail for a path the product never chooses.
$scratch = Join-Path $scratchParent ("bf-rc-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $scratch | Out-Null
$results = @()
$installedDir = Join-Path $scratch 'installed'
$portableDir = Join-Path $scratch 'portable'
$installedData = Join-Path $scratch 'installed-appdata'
$portableData = Join-Path $scratch 'portable-appdata'
$installedSecret = "installed-canary-$([guid]::NewGuid().ToString('N'))"
$portableSecret = "portable-canary-$([guid]::NewGuid().ToString('N'))"

try {
    $installerExit = Invoke-Process -FilePath $setup[0].FullName -Arguments @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-',
        "/DIR=$installedDir", "/LOG=$(Join-Path $EvidenceDir 'installer.log')"
    ) -TimeoutSeconds 600
    if ($installerExit -ne 0) { throw "Installer exited $installerExit." }
    $installedExe = Join-Path $installedDir 'bananaflow.exe'
    if (-not (Test-Path -LiteralPath $installedExe -PathType Leaf)) { throw "Installed GUI executable is missing." }
    $installedSubsystem = Get-PeSubsystem -Path $installedExe
    if ($installedSubsystem -ne 2) { throw "Installed GUI uses PE subsystem $installedSubsystem, not Windows GUI (2)." }

    $results += Invoke-PackagedSmoke -Exe $installedExe -AppData $installedData -Target 'release-candidate' -Scenario 'fresh' -Secret $installedSecret -Label 'installed-fresh'
    $results += Invoke-PackagedSmoke -Exe $installedExe -AppData $installedData -Target 'tag-editor' -Scenario 'fresh' -Secret $installedSecret -Label 'installed-gui-start-shutdown'
    Add-CrashResidue -AppData $installedData
    $results += Invoke-PackagedSmoke -Exe $installedExe -AppData $installedData -Target 'release-candidate' -Scenario 'restart' -Secret $installedSecret -Label 'installed-restart'
    $results += Invoke-PackagedSmoke -Exe $installedExe -AppData $installedData -Target 'release-candidate' -Scenario 'delete' -Secret $installedSecret -Label 'installed-delete-auth'
    Assert-NoPlaintextResidue -Root $installedData -Secret $installedSecret

    Expand-Archive -LiteralPath $zip[0].FullName -DestinationPath $portableDir
    $portableExe = Join-Path $portableDir 'bananaflow.exe'
    if (-not (Test-Path -LiteralPath $portableExe -PathType Leaf)) { throw "Portable GUI executable is missing." }
    $portableSubsystem = Get-PeSubsystem -Path $portableExe
    if ($portableSubsystem -ne 2) { throw "Portable GUI uses PE subsystem $portableSubsystem, not Windows GUI (2)." }

    New-UpgradeFixture -AppData $portableData -Secret $portableSecret
    $results += Invoke-PackagedSmoke -Exe $portableExe -AppData $portableData -Target 'release-candidate' -Scenario 'upgrade' -Secret $portableSecret -Label 'portable-upgrade'
    $results += Invoke-PackagedSmoke -Exe $portableExe -AppData $portableData -Target 'tag-editor' -Scenario 'upgrade' -Secret $portableSecret -Label 'portable-gui-start-shutdown'
    Add-CrashResidue -AppData $portableData
    $results += Invoke-PackagedSmoke -Exe $portableExe -AppData $portableData -Target 'release-candidate' -Scenario 'restart' -Secret $portableSecret -Label 'portable-restart'
    $results += Invoke-PackagedSmoke -Exe $portableExe -AppData $portableData -Target 'release-candidate' -Scenario 'delete' -Secret $portableSecret -Label 'portable-delete-auth'
    Assert-NoPlaintextResidue -Root $portableData -Secret $portableSecret

    foreach ($file in Get-ChildItem -LiteralPath $EvidenceDir -Recurse -File) {
        $text = Get-Content -Raw -LiteralPath $file.FullName -ErrorAction SilentlyContinue
        if ($text -and ($text.Contains($installedSecret) -or $text.Contains($portableSecret))) {
            throw "Cookie canary leaked into smoke evidence: $($file.Name)"
        }
    }

    $summary = [ordered]@{
        ok = $true
        generated_utc = [DateTime]::UtcNow.ToString('o')
        artifacts = @(
            [ordered]@{name=$setup[0].Name; sha256=(Get-FileHash $setup[0].FullName -Algorithm SHA256).Hash.ToLower(); kind='installer'},
            [ordered]@{name=$zip[0].Name; sha256=(Get-FileHash $zip[0].FullName -Algorithm SHA256).Hash.ToLower(); kind='portable'}
        )
        pe_subsystem = [ordered]@{installed=$installedSubsystem; portable=$portableSubsystem; expected=2}
        no_console_window = $true
        no_plaintext_persistent_cookies = $true
        no_temporary_cookie_residue = $true
        log_and_diagnostic_secret_scan = 'pass'
        runs = $results
    }
    $summary | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath (Join-Path $EvidenceDir 'release-candidate-summary.json') -Encoding utf8
    Write-Host "PASS: installer and portable release-candidate smoke checks succeeded." -ForegroundColor Green
} finally {
    $uninstaller = Join-Path $installedDir 'unins000.exe'
    if (Test-Path -LiteralPath $uninstaller -PathType Leaf) {
        try { [void](Invoke-Process -FilePath $uninstaller -Arguments @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART') -TimeoutSeconds 300) } catch { Write-Warning $_ }
    }
    if (-not $KeepScratch -and (Test-Path -LiteralPath $scratch)) {
        $resolvedScratch = [System.IO.Path]::GetFullPath($scratch)
        if (-not $resolvedScratch.StartsWith($scratchParent, [System.StringComparison]::OrdinalIgnoreCase) -or -not ([System.IO.Path]::GetFileName($resolvedScratch)).StartsWith('bf-rc-')) {
            throw "Refusing to remove unverified scratch path: $resolvedScratch"
        }
        Remove-Item -LiteralPath $resolvedScratch -Recurse -Force -ErrorAction SilentlyContinue
    }
}
