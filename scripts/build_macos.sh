#!/usr/bin/env bash
#
# build_macos.sh — Build the BananaFlow macOS release.
# ============================================================
# Produces:
#   dist/BananaFlow.app                         — the application bundle
#   dist/BananaFlow-v<version>-macos-arm64.dmg   — a drag-to-Applications DMG
#   dist/SHA256SUMS.txt                     — checksum for the DMG
#
# Steps:
#   1. Verify python3 is available.
#   2. Run the unit tests (skip with --skip-tests).
#   3. Install build deps + Playwright Chromium (bundled into the .app).
#   4. Stage pinned, SHA-256-verified LGPL FFmpeg into packaging/ffmpeg/
#      from verified upstream sources (or revalidate a matching build cache).
#   5. Build the .app with PyInstaller (packaging/bananaflow.spec).
#   6. Ad-hoc codesign the bundle (no Apple Developer ID required).
#   7. Wrap the .app in a DMG with an /Applications symlink.
#
# This project is NOT notarized (no paid Apple Developer account). On
# first launch users must right-click the app → Open, then confirm, to
# get past Gatekeeper. See the macOS section of README.md.
#
# Usage:
#   chmod +x scripts/build_macos.sh
#   ./scripts/build_macos.sh                 # full build
#   ./scripts/build_macos.sh --skip-tests    # faster dev iteration
#   ./scripts/build_macos.sh --no-ffmpeg     # ship without bundled FFmpeg
#
set -euo pipefail

SKIP_TESTS=0
BUNDLE_FFMPEG=1
for arg in "$@"; do
  case "$arg" in
    --skip-tests) SKIP_TESTS=1 ;;
    --no-ffmpeg)  BUNDLE_FFMPEG=0 ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done
export BANANAFLOW_BUNDLE_FFMPEG="$BUNDLE_FFMPEG"

# ── Resolve repo root (this script lives in scripts/) ──────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

echo "==> BananaFlow macOS build"
echo "    Repo root: $ROOT"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "ERROR: this script must run on macOS." >&2
  exit 1
fi

# ── Python ─────────────────────────────────────────────────────────────────
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 is not on PATH. Install Python 3.10+." >&2
  exit 1
fi
PY=python3
echo "    Python: $($PY --version) ($(command -v $PY))"

ARCH="$(uname -m)"   # arm64 on Apple Silicon
APP_VERSION="$($PY -c 'from version import FULL_VERSION; print(FULL_VERSION)')"
echo "    App version: $APP_VERSION"
echo "    Architecture: $ARCH"

# ── Build dependencies ─────────────────────────────────────────────────────
echo "==> Installing build dependencies"
$PY -m pip install --upgrade pip pyinstaller pytest pytest-mock >/dev/null
$PY -m pip install -r requirements.txt >/dev/null

# ── Playwright Chromium (bundled into the .app) ────────────────────────────
echo "==> Installing Playwright Chromium"
$PY -m pip install --upgrade playwright >/dev/null
$PY -m playwright install chromium

# ── FFmpeg (LGPL) staging ──────────────────────────────────────────────────
# Use the same pinned source build and verification as release/PR CI. A
# previously generated cache is reusable only with matching source/recipe and
# executable hashes, native architecture, LGPL license and codec smoke gates.
if [[ "$BUNDLE_FFMPEG" == "1" ]]; then
  $PY scripts/stage_ffmpeg.py --platform macos-arm64
else
  echo "==> --no-ffmpeg: building without bundled FFmpeg"
  echo "    Users will need ffmpeg on PATH (e.g. 'brew install ffmpeg')."
fi

# ── Tests ──────────────────────────────────────────────────────────────────
if [[ "$SKIP_TESTS" == "0" ]]; then
  echo "==> Running unit tests"
  QT_QPA_PLATFORM=offscreen $PY -m pytest tests/ -q --tb=line
else
  echo "==> Skipping tests (--skip-tests)"
fi

# ── Clean previous outputs ─────────────────────────────────────────────────
echo "==> Cleaning build/ and dist/"
rm -rf build dist

# ── PyInstaller ────────────────────────────────────────────────────────────
echo "==> Running PyInstaller"
$PY -m PyInstaller --noconfirm --clean packaging/bananaflow.spec

# Exact corresponding sources and recipe for the statically linked LGPL
# FFmpeg payload accompany the package, not just a pointer to upstream HEAD.
if [[ "$BUNDLE_FFMPEG" == "1" ]]; then
  cp packaging/ffmpeg-source-bundle.tar.xz dist/ffmpeg-source-bundle.tar.xz
fi

APP="$ROOT/dist/BananaFlow.app"
if [[ ! -d "$APP" ]]; then
  echo "ERROR: expected $APP to exist after PyInstaller." >&2
  exit 1
fi

$PY scripts/build_app_updater.py --destination "$APP/Contents/MacOS"
$PY scripts/generate_sbom.py --out-dir dist

# The spec deliberately excludes nested Chromium .apps from binary analysis.
# Keep local/PR builds identical to the release workflow's post-build copy.
CACHE="$HOME/Library/Caches/ms-playwright"
mkdir -p "$APP/Contents/Resources/ms-playwright"
for dir in "$CACHE"/chromium-* "$CACHE"/chromium_headless_shell-*; do
  [[ -d "$dir" ]] || continue
  cp -R "$dir" "$APP/Contents/Resources/ms-playwright/$(basename "$dir")"
done
if ls "$APP/Contents/Resources/ms-playwright/" | grep -q '^ffmpeg-'; then
  echo "ERROR: Playwright's unused recording FFmpeg was bundled." >&2
  exit 1
fi
if ! ls "$APP/Contents/Resources/ms-playwright/" | grep -q '^chromium-'; then
  echo "ERROR: bundled Chromium is missing." >&2
  exit 1
fi

# ── License / source notice bundle ─────────────────────────────────────────
# BananaFlow is GPL-3.0-or-later and bundles LGPL FFmpeg, Chromium, Deno and the
# bgutil provider, so the conveyed .app must carry the license texts and the
# written source offer. Mirrors scripts/build_windows.ps1's portable-folder
# copy, and must run before codesign so the signature covers them.
echo "==> Copying license/source notice docs into the bundle"
RESOURCES="$APP/Contents/Resources"
mkdir -p "$RESOURCES"
RELEASE_DOCS="LICENSE LICENSES.md NOTICE SOURCE_OFFER.md CONTRIBUTING.md THIRD_PARTY_NOTICES.md README.md"
for doc in $RELEASE_DOCS; do
  if [[ ! -f "$ROOT/$doc" ]]; then
    echo "ERROR: required release notice doc is missing: $doc" >&2
    exit 1
  fi
  cp "$ROOT/$doc" "$RESOURCES/$doc"
  echo "    staged $doc"
done

# ── Ad-hoc codesign ────────────────────────────────────────────────────────
# Ad-hoc (-) signing satisfies the arm64 hard requirement that all
# binaries carry *some* signature. It is NOT notarization — Gatekeeper
# still warns on first launch. If you later get a Developer ID, replace
# the identity below and add a notarization step.
echo "==> Ad-hoc codesigning the bundle"
for executable in bananaflow bananaflow-cli bananaflow-updater; do
  codesign --force --sign - "$APP/Contents/MacOS/$executable"
  codesign --verify --strict --verbose=2 "$APP/Contents/MacOS/$executable"
done

# ── DMG ────────────────────────────────────────────────────────────────────
DMG_NAME="BananaFlow-v$APP_VERSION-macos-$ARCH.dmg"
DMG_PATH="$ROOT/dist/$DMG_NAME"
echo "==> Creating DMG: $DMG_NAME"

STAGING="$(mktemp -d)"
cp -R "$APP" "$STAGING/"
ln -s /Applications "$STAGING/Applications"
# Also at the DMG root so the license terms and written source offer are
# visible on mount, not only after installing the .app.
for doc in LICENSE LICENSES.md NOTICE SOURCE_OFFER.md THIRD_PARTY_NOTICES.md; do
  cp "$ROOT/$doc" "$STAGING/$doc"
done
rm -f "$DMG_PATH"
hdiutil create \
  -volname "BananaFlow" \
  -srcfolder "$STAGING" \
  -ov -format UDZO \
  "$DMG_PATH"
hdiutil verify "$DMG_PATH"
rm -rf "$STAGING"

# ── Checksums ──────────────────────────────────────────────────────────────
echo "==> Writing SHA-256 checksum"
( cd "$ROOT/dist" && shasum -a 256 "$DMG_NAME" > SHA256SUMS.txt )
if [[ "$BUNDLE_FFMPEG" == "1" ]]; then
  ( cd "$ROOT/dist" && shasum -a 256 ffmpeg-source-bundle.tar.xz >> SHA256SUMS.txt )
fi

# ── Summary ────────────────────────────────────────────────────────────────
APP_SIZE="$(du -sh "$APP" | cut -f1)"
DMG_SIZE="$(du -sh "$DMG_PATH" | cut -f1)"
echo ""
echo "==> Build complete"
echo "    App version : $APP_VERSION ($ARCH)"
echo "    App bundle  : $APP  ($APP_SIZE)"
echo "    DMG         : $DMG_PATH  ($DMG_SIZE)"
echo "    Checksums   : $ROOT/dist/SHA256SUMS.txt"
echo ""
echo "Smoke test:"
echo "  open '$APP'"
echo "  '$APP/Contents/MacOS/bananaflow-cli' --version"
echo ""
echo "NOTE: this build is unsigned/un-notarized. First launch requires"
echo "      right-click → Open to bypass Gatekeeper."
