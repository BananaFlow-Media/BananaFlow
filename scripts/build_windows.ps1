<#
.SYNOPSIS
    Build the BananaFlow Windows release (portable ZIP + Inno Setup input folder).

.DESCRIPTION
    1. Verifies python is on PATH.
    2. (Optional) requires ffmpeg.exe and ffprobe.exe to already be staged
       in packaging/ffmpeg/ when -RequireBundledFfmpeg is set.
    3. Runs the isolated test gate (scripts/run_isolated_tests.py) unless
       -SkipTests is passed.
    4. Regenerates packaging/version_info.txt from version.py.
    5. Runs PyInstaller against packaging/bananaflow.spec.
    6. Produces:
         dist/bananaflow/                               - one-folder portable build (input for Inno Setup)
         dist/bananaflow-<version>-windows-portable.zip - portable ZIP for direct distribution
         dist/SHA256SUMS.txt                        - SHA-256 checksums for the ZIP
         test-evidence/                             - per-file JUnit XML, logs, JSON aggregate
    7. Prints a short summary with sizes and exact next-step commands.

.PARAMETER RequireBundledFfmpeg
    Fail the build unless packaging/ffmpeg/ contains both ffmpeg.exe and
    ffprobe.exe before running PyInstaller. This script does not download
    FFmpeg automatically.

.PARAMETER SkipTests
    Skip the test gate that normally happens before packaging. Intended for a
    workflow that has ALREADY run the exact same gate
    (scripts/run_isolated_tests.py) in an earlier step — release-windows.yml
    does this so the suite is not executed twice. It is not a way to ship
    untested code.

.NOTES
    A staged JS runtime (packaging/runtime/deno.exe — run
    scripts/fetch_deno_runtime.ps1 first) is ALWAYS required: the public
    Windows package promises an out-of-the-box PO Token Provider stack,
    and that stack needs the bundled Deno. There is deliberately no
    opt-out switch.

.EXAMPLE
    pwsh scripts/build_windows.ps1
    Builds with whatever is already in packaging/ffmpeg/ (or no FFmpeg at all).

.EXAMPLE
    pwsh scripts/build_windows.ps1 -RequireBundledFfmpeg
    Requires staged FFmpeg binaries, then builds.

.NOTES
    Run from the repository root or from the scripts/ folder; the
    script normalises the working directory either way.
#>

[CmdletBinding()]
param(
    [switch]$RequireBundledFfmpeg,
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'

function Get-BuildRelatedProcesses {
    param(
        [Parameter(Mandatory = $true)]
        [int]$RootProcessId,

        [Parameter(Mandatory = $true)]
        [string]$RepoRoot
    )

    $all = @(Get-CimInstance Win32_Process)
    $descendantIds = New-Object 'System.Collections.Generic.HashSet[int]'
    $queue = New-Object 'System.Collections.Generic.Queue[int]'
    $queue.Enqueue($RootProcessId)

    while ($queue.Count -gt 0) {
        $parentId = $queue.Dequeue()
        foreach ($proc in $all | Where-Object { [int]$_.ParentProcessId -eq $parentId }) {
            $processId = [int]$proc.ProcessId
            if ($descendantIds.Add($processId)) {
                $queue.Enqueue($processId)
            }
        }
    }

    $targets = @()
    foreach ($proc in $all) {
        $processId = [int]$proc.ProcessId
        if ($processId -eq $PID) {
            continue
        }

        $name = ([string]$proc.Name).ToLowerInvariant()
        $cmd = [string]$proc.CommandLine
        $isDescendant = $descendantIds.Contains($processId)
        $isRepoRelated = $cmd -and $cmd.Contains($RepoRoot)
        $isBuildTool = $name -in @('node.exe', 'playwright.exe', 'pyinstaller.exe') -or (
            $name -in @('python.exe', 'python3.exe', 'py.exe') -and
            $cmd -match '(?i)pyinstaller|playwright'
        )

        if ($isBuildTool -and ($isDescendant -or $isRepoRelated)) {
            $targets += $proc
        }
    }

    return $targets
}

function Stop-BuildRelatedProcesses {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepoRoot
    )

    $targets = @(Get-BuildRelatedProcesses -RootProcessId $PID -RepoRoot $RepoRoot)
    foreach ($proc in $targets) {
        $processId = [int]$proc.ProcessId
        $name = [string]$proc.Name
        Write-Host "    Stopping leftover build child process: $name (PID $processId)" -ForegroundColor Yellow
        try {
            Stop-Process -Id $processId -Force -ErrorAction Stop
        } catch {
            Write-Warning "Could not stop $name (PID $processId): $($_.Exception.Message)"
        }
    }
}

function Get-LockedFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    foreach ($file in Get-ChildItem -Path $Path -Recurse -File) {
        $stream = $null
        try {
            $stream = [System.IO.File]::Open(
                $file.FullName,
                [System.IO.FileMode]::Open,
                [System.IO.FileAccess]::Read,
                [System.IO.FileShare]::Read
            )
        } catch [System.IO.IOException] {
            return $file.FullName
        } finally {
            if ($stream) {
                $stream.Dispose()
            }
        }
    }

    return $null
}

function New-PortableZip {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SourceDir,

        [Parameter(Mandatory = $true)]
        [string]$ZipPath,

        [Parameter(Mandatory = $true)]
        [string]$RepoRoot
    )

    Add-Type -AssemblyName System.IO.Compression.FileSystem

    $maxAttempts = 5
    for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
        Stop-BuildRelatedProcesses -RepoRoot $RepoRoot

        $locked = Get-LockedFile -Path $SourceDir
        if ($locked) {
            Write-Warning "File is still locked before ZIP attempt ${attempt}/${maxAttempts}: $locked"
            Start-Sleep -Seconds $attempt
            continue
        }

        if (Test-Path $ZipPath) {
            Remove-Item -Force $ZipPath
        }

        try {
            [System.IO.Compression.ZipFile]::CreateFromDirectory(
                $SourceDir,
                $ZipPath,
                [System.IO.Compression.CompressionLevel]::Fastest,
                $false
            )
            return
        } catch [System.IO.IOException] {
            Write-Warning "ZIP attempt ${attempt}/${maxAttempts} failed: $($_.Exception.Message)"
            if (Test-Path $ZipPath) {
                Remove-Item -Force $ZipPath -ErrorAction SilentlyContinue
            }
            Start-Sleep -Seconds $attempt
        }
    }

    throw "Failed to create ZIP after $maxAttempts attempts. A build or Playwright file is still locked under $SourceDir."
}

# Normalise working directory to the repo root.
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = Split-Path -Parent $ScriptDir
Set-Location $RepoRoot

Write-Host "==> BananaFlow Windows build" -ForegroundColor Cyan
Write-Host "    Repo root: $RepoRoot"

# Pre-flight: Python on PATH.
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    throw "python is not on PATH. Install Python 3.10+ and re-run."
}
$pyVer = (& python --version) -replace '^Python\s+', ''
Write-Host "    Python: $pyVer ($($py.Source))"

# Read the canonical version so we can name the output ZIP correctly.
$AppVersion = (& python -c "from version import FULL_VERSION; print(FULL_VERSION)").Trim()
Write-Host "    App version: $AppVersion"

# Optional: require staged FFmpeg binaries.
$FfmpegDir = Join-Path $RepoRoot 'packaging\ffmpeg'

if ($RequireBundledFfmpeg) {
    Write-Host "==> Requiring bundled FFmpeg from $FfmpegDir" -ForegroundColor Cyan
    $RequiredFfmpegFiles = @('ffmpeg.exe', 'ffprobe.exe')
    $MissingFfmpegFiles = @()

    foreach ($name in $RequiredFfmpegFiles) {
        $path = Join-Path $FfmpegDir $name
        if (-not (Test-Path -Path $path -PathType Leaf)) {
            $MissingFfmpegFiles += $path
        }
    }

    if ($MissingFfmpegFiles.Count -gt 0) {
        $NewLine = [Environment]::NewLine
        $MissingList = (($MissingFfmpegFiles | ForEach-Object { "  - $_" }) -join $NewLine)
        $Message = "Required bundled FFmpeg files are missing:$NewLine$MissingList$NewLine$NewLine"
        $Message += "Place LGPL-licensed Windows FFmpeg binaries exactly here:$NewLine"
        $Message += "  $FfmpegDir\ffmpeg.exe$NewLine"
        $Message += "  $FfmpegDir\ffprobe.exe$NewLine$NewLine"
        $Message += "This script does not auto-download FFmpeg because the release must not accidentally bundle a GPL build."
        throw $Message
    }

    Write-Host "    Found ffmpeg.exe and ffprobe.exe."
    Write-Host "    Build will fail if PyInstaller does not copy both next to bananaflow.exe."
} else {
    if (Test-Path (Join-Path $FfmpegDir 'ffmpeg.exe')) {
        Write-Host "    Bundling FFmpeg from $FfmpegDir"
    } else {
        Write-Host "    No FFmpeg in $FfmpegDir - building without bundled FFmpeg." -ForegroundColor Yellow
        Write-Host "    Users will need ffmpeg on their PATH or installed system-wide."
    }
}

# Require a staged JS runtime (Deno) for out-of-the-box PO Token Provider
# and signature-solving support on machines with no runtime installed.
$RuntimeDir = Join-Path $RepoRoot 'packaging\runtime'
$DenoPath = Join-Path $RuntimeDir 'deno.exe'

Write-Host "==> Requiring bundled JS runtime from $RuntimeDir" -ForegroundColor Cyan
if (-not (Test-Path -Path $DenoPath -PathType Leaf)) {
    throw "Required bundled runtime is missing: $DenoPath$([Environment]::NewLine)" +
          "Run scripts\fetch_deno_runtime.ps1 first to stage it."
}
Write-Host "    Found deno.exe."

# Run the test gate unless explicitly skipped.
#
# This uses scripts/run_isolated_tests.py, NOT `pytest tests/`. Running the
# whole suite in one process dies natively at 0xC0000005 partway through on
# Windows without printing a summary — accumulated Qt state, not a broken test
# — so the old gate here could never pass, and reported a native crash as
# "unit tests failed" (finding F-16). The isolated runner gives every file its
# own interpreter, reads the real child exit code, and fails truthfully on a
# logical failure, a collection error, a timeout, a missing result, or any
# native exit it has no reviewed classification for.
if (-not $SkipTests) {
    Write-Host "==> Running isolated test gate (fresh process per file)" -ForegroundColor Cyan
    & python scripts\run_isolated_tests.py --evidence-dir test-evidence
    if ($LASTEXITCODE -ne 0) {
        throw "Isolated test gate failed. See the failures above and the evidence in test-evidence\isolated_results.json."
    }
}

# Ensure build deps are present.
Write-Host "==> Installing build dependencies" -ForegroundColor Cyan
& python -m pip install --upgrade pip pyinstaller | Out-Null
& python -m pip install -r requirements.txt | Out-Null
& python -m pip install "bgutil-ytdlp-pot-provider==1.3.1" | Out-Null

# Stage the PO Token Provider plugin and Deno script backend so PyInstaller
# bundles the full official provider path next to the EXE. The public Windows
# build requires this stack; source installs can still use the po-token extra.
Write-Host "==> Staging PO Token Provider plugin and Deno script backend" -ForegroundColor Cyan
& python packaging\stage_pot_provider.py
if ($LASTEXITCODE -ne 0) {
    throw "PO Token Provider staging failed. The public Windows build requires plugin + Deno script backend."
}

# Regenerate VS_VERSIONINFO from version.py.
Write-Host "==> Regenerating version_info.txt" -ForegroundColor Cyan
& python packaging\generate_version_info.py

# Clean previous build outputs.
Write-Host "==> Cleaning previous build/ and dist/" -ForegroundColor Cyan
if (Test-Path build) { Remove-Item -Recurse -Force build }
if (Test-Path dist)  { Remove-Item -Recurse -Force dist }

# PyInstaller.
Write-Host "==> Running PyInstaller" -ForegroundColor Cyan
& python -m PyInstaller --noconfirm --clean packaging\bananaflow.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed. See output above."
}
Stop-BuildRelatedProcesses -RepoRoot $RepoRoot

$DistDir = Join-Path $RepoRoot 'dist\bananaflow'
if (-not (Test-Path $DistDir)) {
    throw "Expected $DistDir to exist after PyInstaller run."
}

if ($RequireBundledFfmpeg) {
    # PyInstaller 6.x's default one-folder layout collects bundled
    # binaries into an executable-adjacent "_internal" folder rather
    # than dropping them next to bananaflow.exe itself (the
    # --contents-directory default) — bananaflow.spec's
    # binaries=[(ffmpeg_path, '.')] entry actually lands here, not at
    # the top level. utils.paths.get_bundled_ffmpeg_dir() checks both
    # locations (via sys._MEIPASS) so the running app finds it either
    # way; this check must accept either location too, or a real
    # PyInstaller 6.x build always fails here even though FFmpeg is
    # genuinely bundled and will genuinely be found at runtime.
    $FfmpegSearchDirs = @($DistDir, (Join-Path $DistDir '_internal'))
    $FoundFfmpegDir = $null
    foreach ($dir in $FfmpegSearchDirs) {
        $hasAll = $true
        foreach ($name in @('ffmpeg.exe', 'ffprobe.exe')) {
            if (-not (Test-Path -Path (Join-Path $dir $name) -PathType Leaf)) {
                $hasAll = $false
                break
            }
        }
        if ($hasAll) {
            $FoundFfmpegDir = $dir
            break
        }
    }

    if (-not $FoundFfmpegDir) {
        $NewLine = [Environment]::NewLine
        $SearchedList = (($FfmpegSearchDirs | ForEach-Object { "  - $_\ffmpeg.exe / ffprobe.exe" }) -join $NewLine)
        throw "PyInstaller did not copy the required bundled FFmpeg files to any known location:$NewLine$SearchedList"
    }

    Write-Host "    Bundled FFmpeg confirmed in $FoundFfmpegDir."
}

# Release compliance docs live next to the portable EXEs so the ZIP and
# installer input folder carry the same license/source notice bundle.
Write-Host "==> Copying license/source notice docs into portable folder" -ForegroundColor Cyan
$ReleaseDocs = @(
    'LICENSE',
    'LICENSES.md',
    'NOTICE',
    'SOURCE_OFFER.md',
    'CONTRIBUTING.md',
    'THIRD_PARTY_NOTICES.md',
    'README.md'
)
foreach ($name in $ReleaseDocs) {
    $src = Join-Path $RepoRoot $name
    if (-not (Test-Path -Path $src -PathType Leaf)) {
        throw "Required release notice doc is missing: $src"
    }
    Copy-Item -Path $src -Destination (Join-Path $DistDir $name) -Force
}

# Portable ZIP.
$ZipName = "bananaflow-$AppVersion-windows-portable.zip"
$ZipPath = Join-Path $RepoRoot "dist\$ZipName"
Write-Host "==> Creating portable ZIP: $ZipName" -ForegroundColor Cyan
New-PortableZip -SourceDir $DistDir -ZipPath $ZipPath -RepoRoot $RepoRoot

# SHA-256 checksums.
$ChecksumPath = Join-Path $RepoRoot 'dist\SHA256SUMS.txt'
Write-Host "==> Writing SHA-256 checksums" -ForegroundColor Cyan
$hashes = @()
foreach ($f in Get-ChildItem -Path (Join-Path $RepoRoot 'dist') -File `
                              -Filter 'bananaflow-*' ) {
    $h = (Get-FileHash $f.FullName -Algorithm SHA256).Hash.ToLower()
    $hashes += "$h  $($f.Name)"
}
$hashes | Set-Content -Path $ChecksumPath -Encoding utf8

# SBOM (CycloneDX JSON + human-readable inventory). Generated from this
# exact build's installed dependency set, so package versions in the SBOM
# match the artifact rather than whatever happens to be pinned in
# THIRD_PARTY_NOTICES.md at review time.
Write-Host "==> Generating SBOM" -ForegroundColor Cyan
& python scripts\generate_sbom.py --out-dir dist
if ($LASTEXITCODE -ne 0) {
    throw "SBOM generation failed."
}

# Summary.
$DistSizeMB = [math]::Round(((Get-ChildItem -Recurse $DistDir | Measure-Object Length -Sum).Sum / 1MB), 1)
$ZipSizeMB  = [math]::Round((Get-Item $ZipPath).Length / 1MB, 1)

Write-Host ""
Write-Host "==> Build complete" -ForegroundColor Green
Write-Host "    App version : $AppVersion"
Write-Host "    Portable    : $DistDir"
Write-Host "                  $($DistSizeMB) MB on disk"
Write-Host "    ZIP         : $ZipPath"
Write-Host "                  $($ZipSizeMB) MB"
Write-Host "    Checksums   : $ChecksumPath"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  Smoke-test     : & '$DistDir\bananaflow.exe'"
Write-Host "  CLI smoke      : & '$DistDir\bananaflow-cli.exe' --version"
Write-Host "  Inno installer : iscc packaging\bananaflow.iss"
Write-Host ""
