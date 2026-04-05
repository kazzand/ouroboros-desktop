#!/usr/bin/env bash
# Build Ouroboros for Linux — creates a standalone binary in a tar.gz
# Usage: bash build_linux.sh

set -euo pipefail

VERSION=$(cat VERSION)
APP_NAME="Ouroboros"
ENTRY="server.py"
DIST_DIR="dist"
BUILD_DIR="build"
OUTPUT="${DIST_DIR}/Ouroboros-linux-x86_64.tar.gz"

echo "=== Building ${APP_NAME} v${VERSION} for Linux ==="

# Clean previous builds
rm -rf "${DIST_DIR}/${APP_NAME}" "${OUTPUT}" "${BUILD_DIR}"

# Run PyInstaller — onedir mode for Linux
pyinstaller \
    --name "${APP_NAME}" \
    --onedir \
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

# Package as tar.gz
echo "=== Creating archive ==="
cd "${DIST_DIR}"
tar -czf "Ouroboros-linux-x86_64.tar.gz" "${APP_NAME}/"
cd ..

echo "=== Linux build finished: ${OUTPUT} ==="
