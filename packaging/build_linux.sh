#!/usr/bin/env bash
# Builds the Local OCR Linux package, including a vendored Ollama binary
# for a machine that has no Ollama already installed.
#
# Run from the repo root:
#   source .venv/bin/activate  (or any venv with requirements.lock + pyinstaller)
#   bash packaging/build_linux.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

OLLAMA_VERSION="${OLLAMA_VERSION:-v0.34.0}"
VENDOR_DIR="packaging/vendor/ollama"

echo "== Fetching Ollama ${OLLAMA_VERSION} (linux-amd64) =="
rm -rf "$VENDOR_DIR"
mkdir -p "$VENDOR_DIR"
tmpfile="$(mktemp)"
curl -fsSL -o "$tmpfile" \
  "https://github.com/ollama/ollama/releases/download/${OLLAMA_VERSION}/ollama-linux-amd64.tar.zst"
tar --zstd -xf "$tmpfile" -C "$VENDOR_DIR"
rm -f "$tmpfile"

echo "== Building with PyInstaller =="
pyinstaller packaging/app.spec --distpath dist --workpath build --noconfirm

echo "== Packaging =="
( cd dist && tar -czf LocalOCR-linux-x86_64.tar.gz LocalOCR )

echo ""
echo "Done: dist/LocalOCR-linux-x86_64.tar.gz"
echo "Extract it anywhere and run ./LocalOCR/LocalOCR to start the app."
