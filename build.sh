#!/usr/bin/env bash
# Build Ouroboros for macOS — creates a .app bundle and .dmg
# Usage: bash build.sh
#
# Optional env vars for signing/notarization:
#   DEVELOPER_ID_APPLICATION  — Apple Developer ID (e.g. "Developer ID Application: Name (TEAMID)")
#   APPLE_TEAM_ID             — Apple Team ID
#   APPLE_ID                  — Apple ID email
#   APPLE_ID_PASSWORD         — App-specific password

set -euo pipefail

VERSION=$(cat VERSION)
APP_NAME="Ouroboros"
ENTRY="server.py"
DIST_DIR="dist"
BUILD_DIR="build"
DMG_NAME="${APP_NAME}-${VERSION}-macos.dmg"

echo "=== Building ${APP_NAME} v${VERSION} for macOS ==="

# Clean previous builds
rm -rf "${DIST_DIR}/${APP_NAME}.app" "${DIST_DIR}/${DMG_NAME}" "${BUILD_DIR}"

# Run PyInstaller
pyinstaller \
    --name "${APP_NAME}" \
    --windowed \
    --icon assets/icon.icns \
    --add-data "ouroboros:ouroboros" \
    --add-data "supervisor:supervisor" \
    --add-data "web:web" \
    --add-data "prompts:prompts" \
    --add-data "docs:docs" \
    --add-data "assets:assets" \
    --add-data "BIBLE.md:." \
    --add-data "README.md:." \
    --add-data "VERSION:." \
    --add-data "pyproject.toml:." \
    --hidden-import starlette \
    --hidden-import starlette.applications \
    --hidden-import starlette.routing \
    --hidden-import starlette.requests \
    --hidden-import starlette.responses \
    --hidden-import starlette.websockets \
    --hidden-import starlette.staticfiles \
    --hidden-import starlette.middleware \
    --hidden-import uvicorn \
    --hidden-import uvicorn.logging \
    --hidden-import uvicorn.loops \
    --hidden-import uvicorn.loops.auto \
    --hidden-import uvicorn.protocols \
    --hidden-import uvicorn.protocols.http \
    --hidden-import uvicorn.protocols.http.auto \
    --hidden-import uvicorn.protocols.websockets \
    --hidden-import uvicorn.protocols.websockets.auto \
    --hidden-import uvicorn.lifespan \
    --hidden-import uvicorn.lifespan.on \
    --hidden-import websockets \
    --hidden-import dulwich \
    --hidden-import huggingface_hub \
    --hidden-import multiprocessing \
    --noconfirm \
    --clean \
    "${ENTRY}"

echo "=== PyInstaller build complete ==="

# ── Code signing (optional) ──
if [[ -n "${DEVELOPER_ID_APPLICATION:-}" ]]; then
    echo "=== Signing app bundle ==="
    codesign --force --deep --options runtime \
        --sign "${DEVELOPER_ID_APPLICATION}" \
        "${DIST_DIR}/${APP_NAME}.app"
    echo "Signing complete."
else
    echo "Skipping code signing (DEVELOPER_ID_APPLICATION not set)."
fi

# ── Create DMG ──
echo "=== Creating DMG ==="
hdiutil create -volname "${APP_NAME}" \
    -srcfolder "${DIST_DIR}/${APP_NAME}.app" \
    -ov -format UDZO \
    "${DIST_DIR}/${DMG_NAME}"

echo "=== DMG created: ${DIST_DIR}/${DMG_NAME} ==="

# ── Notarization (optional) ──
if [[ -n "${APPLE_ID:-}" && -n "${APPLE_ID_PASSWORD:-}" && -n "${APPLE_TEAM_ID:-}" ]]; then
    echo "=== Notarizing DMG ==="
    xcrun notarytool submit "${DIST_DIR}/${DMG_NAME}" \
        --apple-id "${APPLE_ID}" \
        --password "${APPLE_ID_PASSWORD}" \
        --team-id "${APPLE_TEAM_ID}" \
        --wait

    echo "=== Stapling notarization ticket ==="
    xcrun stapler staple "${DIST_DIR}/${DMG_NAME}"
    echo "Notarization complete."
else
    echo "Skipping notarization (APPLE_ID / APPLE_ID_PASSWORD / APPLE_TEAM_ID not set)."
fi

echo "=== macOS build finished: ${DIST_DIR}/${DMG_NAME} ==="
