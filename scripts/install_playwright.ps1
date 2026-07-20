<#
.SYNOPSIS
    Install Playwright Chromium so BananaFlow's optional features work.

.DESCRIPTION
    Run this once on a fresh install. Playwright downloads ~300 MB of
    browser binaries to %USERPROFILE%\AppData\Local\ms-playwright\.

    Features that need Playwright:
      * Channel and artist discography scraping (core/scraper.py)
      * Cookie sign-in wizard (core/cookie_wizard.py)
      * Universal stream extractor (core/universal_extractor.py)

    Plain YouTube / YT Music / Spotify single-track and playlist
    downloads do NOT require Playwright. The app degrades gracefully
    and the preflight check announces the missing component.

.EXAMPLE
    pwsh scripts\install_playwright.ps1
    From the repo root, with python on PATH.

.EXAMPLE
    & 'C:\Program Files\BananaFlow\scripts\install_playwright.ps1'
    From an installed copy. Uses the bundled Python if present.
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

Write-Host "==> Installing Playwright Chromium" -ForegroundColor Cyan

# Prefer the bundled Python next to bananaflow.exe; fall back to system.
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BundledPy = Join-Path $ScriptDir 'python.exe'

if (Test-Path $BundledPy) {
    $PyExe = $BundledPy
} else {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) {
        throw "python is not on PATH and no bundled Python found. Install Python 3.10+ first."
    }
    $PyExe = $cmd.Source
}

Write-Host "    Using Python: $PyExe"
& $PyExe -m playwright install chromium

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "==> Done. Playwright Chromium is now available." -ForegroundColor Green
    Write-Host "    Channel scraping, cookie wizard, and universal extractor"
    Write-Host "    will work on the next BananaFlow launch."
} else {
    throw "playwright install chromium failed (exit $LASTEXITCODE)."
}
