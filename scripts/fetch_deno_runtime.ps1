<#
.SYNOPSIS
    Stage a standalone Deno binary into packaging/runtime/ for bundling
    into the packaged EXE.

.DESCRIPTION
    Downloads the official Deno release zip for win-x64 from
    https://github.com/denoland/deno/releases and verifies it against a
    SHA-256 pinned in this script (below), not the .sha256sum file GitHub
    serves alongside the same release — a same-source checksum only
    proves the download wasn't corrupted in transit, not that the release
    itself is what it claims to be (issue #31). Extracts deno.exe into
    packaging/runtime/ — the folder
    core.runtime_components.activate_bundled_components() prepends to
    PATH at startup so a packaged EXE works on a clean machine with no JS
    runtime installed.

    Run this once before a release build that should bundle a runtime
    (mirrors scripts/install_playwright.ps1's role for Chromium). Not
    required for day-to-day development — Deno already on your PATH is
    used either way; this only affects what ships inside the EXE.

    packaging/runtime/ is gitignored (see .gitignore) — this script (or
    a manual equivalent) must be re-run on every machine that builds a
    release, exactly like packaging/ffmpeg/ staging.

.PARAMETER Version
    Deno release tag to fetch, without the leading 'v' (default: a
    known-good pinned version, deliberately not "latest" so every build
    uses a version this project has actually verified).

.EXAMPLE
    pwsh scripts\fetch_deno_runtime.ps1
    Stages the pinned default version.

.EXAMPLE
    pwsh scripts\fetch_deno_runtime.ps1 -Version 2.10.0
    Stages a specific newer Deno release instead.
#>

[CmdletBinding()]
param(
    [string]$Version = '2.9.1',

    # Verify and stage an already-downloaded archive instead of fetching one.
    # Same pin, same fail-closed check — this only replaces where the bytes
    # come from. Useful for an air-gapped build machine, for verifying a zip
    # you downloaded and inspected by hand, and for exercising the
    # verification itself without network access (see
    # tests/test_supply_chain_pinning_phase4.py).
    [string]$ArchivePath
)

$ErrorActionPreference = 'Stop'

# Pinned independently of the release itself (issue #31): Deno does not
# publish any signature or attestation separate from the release page, so
# a checksum fetched from the same release's .sha256sum asset only proves
# the download matches what that page currently serves, not that the page
# hasn't been tampered with. Each entry here was verified once, out of
# band, from the real published asset -- add a new entry (with the same
# care) before bumping -Version to a release not listed here; this
# script deliberately refuses to fall back to trusting a same-source
# checksum for an unpinned version.
$PinnedHashes = @{
    '2.9.1' = 'ab310b4232cca207d40ffa41867e93aaf9f893802bc76756e74f486a6b21b371'
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = Split-Path -Parent $ScriptDir
$RuntimeDir = Join-Path $RepoRoot 'packaging\runtime'

$AssetName = 'deno-x86_64-pc-windows-msvc.zip'
$BaseUrl   = "https://github.com/denoland/deno/releases/download/v$Version"
$ZipUrl    = "$BaseUrl/$AssetName"

if (-not $PinnedHashes.ContainsKey($Version)) {
    throw (
        "No pinned SHA-256 for Deno v$Version in this script. Establish the " +
        "real checksum without relying on the release page you are about to " +
        "download from -- e.g. build the same tag from source and hash the " +
        "result, compare against a copy already mirrored by a third party " +
        "(a distro package, an existing verified build machine), or check the " +
        "hash a previous release of this project pinned if the asset is " +
        "unchanged. Fetching v$Version's own .sha256sum asset is NOT such a " +
        "source: it ships from the same release and is rewritten by anyone " +
        "who can rewrite the zip. Add the verified value to `$PinnedHashes " +
        "before using this version."
    )
}
$ExpectedHash = $PinnedHashes[$Version]

New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null

if ($ArchivePath) {
    if (-not (Test-Path $ArchivePath)) {
        throw "Archive not found: $ArchivePath"
    }
    Write-Host "==> Verifying local Deno v$Version archive" -ForegroundColor Cyan
    Write-Host "    Source: $ArchivePath"
    $TmpZip = $ArchivePath
} else {
    Write-Host "==> Fetching Deno v$Version for bundling" -ForegroundColor Cyan
    Write-Host "    Source: $ZipUrl"
    $TmpZip = Join-Path $env:TEMP "deno-$Version-win-x64.zip"
    Invoke-WebRequest -Uri $ZipUrl -OutFile $TmpZip -UseBasicParsing
}

$ActualHash = (Get-FileHash -Path $TmpZip -Algorithm SHA256).Hash
if ($ActualHash.ToLower() -ne $ExpectedHash.ToLower()) {
    # Never delete an archive the caller supplied — only one we fetched.
    if (-not $ArchivePath) { Remove-Item -Force $TmpZip -ErrorAction SilentlyContinue }
    throw "SHA-256 mismatch for $AssetName`nExpected: $ExpectedHash`nActual:   $ActualHash"
}
Write-Host "    Checksum verified: $ActualHash"

Add-Type -AssemblyName System.IO.Compression.FileSystem
$TmpExtractDir = Join-Path $env:TEMP "deno-$Version-extract"
if (Test-Path $TmpExtractDir) { Remove-Item -Recurse -Force $TmpExtractDir }
[System.IO.Compression.ZipFile]::ExtractToDirectory($TmpZip, $TmpExtractDir)

$DenoExe = Join-Path $TmpExtractDir 'deno.exe'
if (-not (Test-Path $DenoExe)) {
    throw "deno.exe not found inside the downloaded archive."
}
Copy-Item -Force $DenoExe (Join-Path $RuntimeDir 'deno.exe')

if (-not $ArchivePath) { Remove-Item -Force $TmpZip -ErrorAction SilentlyContinue }
Remove-Item -Recurse -Force $TmpExtractDir -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "==> Done. Staged: $RuntimeDir\deno.exe" -ForegroundColor Green
Write-Host "    Run 'bananaflow-cli --doctor' after building to confirm the JS"
Write-Host "    runtime check reports 'bundled with BananaFlow'."
