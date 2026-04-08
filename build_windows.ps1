# Build Ouroboros for Windows — creates a standalone binary in a zip
# Usage: .\build_windows.ps1

$ErrorActionPreference = "Stop"

$VERSION = Get-Content VERSION -Raw
$VERSION = $VERSION.Trim()
$APP_NAME = "Ouroboros"
$ENTRY = "server.py"
$DIST_DIR = "dist"
$BUILD_DIR = "build"
$OUTPUT = "$DIST_DIR\Ouroboros-windows-x64.zip"

Write-Host "=== Building $APP_NAME v$VERSION for Windows ==="

# Clean previous builds
if (Test-Path "$DIST_DIR\$APP_NAME") { Remove-Item -Recurse -Force "$DIST_DIR\$APP_NAME" }
if (Test-Path $OUTPUT) { Remove-Item -Force $OUTPUT }
if (Test-Path $BUILD_DIR) { Remove-Item -Recurse -Force $BUILD_DIR }

# Run PyInstaller — onedir mode for Windows
pyinstaller `
    --name $APP_NAME `
    --onedir `
    --icon assets/icon.ico `
    --add-data "ouroboros;ouroboros" `
    --add-data "supervisor;supervisor" `
    --add-data "web;web" `
    --add-data "prompts;prompts" `
    --add-data "docs;docs" `
    --add-data "assets;assets" `
    --add-data "BIBLE.md;." `
    --add-data "README.md;." `
    --add-data "VERSION;." `
    --add-data "pyproject.toml;." `
    --hidden-import starlette `
    --hidden-import starlette.applications `
    --hidden-import starlette.routing `
    --hidden-import starlette.requests `
    --hidden-import starlette.responses `
    --hidden-import starlette.websockets `
    --hidden-import starlette.staticfiles `
    --hidden-import starlette.middleware `
    --hidden-import uvicorn `
    --hidden-import uvicorn.logging `
    --hidden-import uvicorn.loops `
    --hidden-import uvicorn.loops.auto `
    --hidden-import uvicorn.protocols `
    --hidden-import uvicorn.protocols.http `
    --hidden-import uvicorn.protocols.http.auto `
    --hidden-import uvicorn.protocols.websockets `
    --hidden-import uvicorn.protocols.websockets.auto `
    --hidden-import uvicorn.lifespan `
    --hidden-import uvicorn.lifespan.on `
    --hidden-import websockets `
    --hidden-import dulwich `
    --hidden-import huggingface_hub `
    --hidden-import multiprocessing `
    --noconfirm `
    --clean `
    $ENTRY

# Package as zip
Write-Host "=== Creating archive ==="
Compress-Archive -Path "$DIST_DIR\$APP_NAME" -DestinationPath $OUTPUT

Write-Host "=== Windows build finished: $OUTPUT ==="
