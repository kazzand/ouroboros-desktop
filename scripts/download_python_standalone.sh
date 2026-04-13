#!/bin/bash
set -e

# Downloads python-build-standalone for macOS (arm64 + x86_64)
# Run from repo root: bash scripts/download_python_standalone.sh

RELEASE="20260211"
PY_VERSION="3.10.19"
DEST="python-standalone"

ARCH=$(uname -m)
OS=$(uname -s)
if [ "$OS" = "Darwin" ] && [ "$ARCH" = "arm64" ]; then
    PLATFORM="aarch64-apple-darwin"
elif [ "$OS" = "Darwin" ] && [ "$ARCH" = "x86_64" ]; then
    PLATFORM="x86_64-apple-darwin"
elif [ "$OS" = "Linux" ] && [ "$ARCH" = "x86_64" ]; then
    PLATFORM="x86_64-unknown-linux-gnu"
elif [ "$OS" = "Linux" ] && { [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; }; then
    PLATFORM="aarch64-unknown-linux-gnu"
else
    echo "Unsupported platform: ${OS}/${ARCH}"
    exit 1
fi

FILENAME="cpython-${PY_VERSION}+${RELEASE}-${PLATFORM}-install_only_stripped.tar.gz"
URL="https://github.com/astral-sh/python-build-standalone/releases/download/${RELEASE}/${FILENAME}"

echo "=== Downloading Python ${PY_VERSION} for ${PLATFORM} ==="
echo "URL: ${URL}"

rm -rf "$DEST" _python_tmp
mkdir -p _python_tmp

curl -L --progress-bar "$URL" | tar xz -C _python_tmp
mv _python_tmp/python "$DEST"
rm -rf _python_tmp

echo ""
echo "=== Installing agent dependencies ==="
"${DEST}/bin/pip3" install --quiet -r requirements.txt

echo ""
echo "=== Done ==="
echo "Python: ${DEST}/bin/python3"
"${DEST}/bin/python3" --version
