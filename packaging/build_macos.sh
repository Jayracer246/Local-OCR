#!/usr/bin/env bash
# Builds the Local OCR macOS package, including a vendored Ollama binary
# for a machine that has no Ollama already installed.
#
# Run from the repo root:
#   python3 -m venv .venv
#   source .venv/bin/activate
#   pip install -r requirements.lock
#   pip install pyinstaller
#   bash packaging/build_macos.sh
#
# NOTE ON PROVENANCE: this script was written and reasoned through against
# Ollama's documented release assets and PyInstaller's documented CLI, but
# was authored and only tested on Linux — there is no Mac in that loop.
# The fetch URL and `pyinstaller` invocation are the well-established
# parts; hdiutil/.dmg packaging is standard macOS tooling but genuinely
# unverified here. Please report back what broke.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

OLLAMA_VERSION="${OLLAMA_VERSION:-v0.34.0}"
VENDOR_DIR="packaging/vendor/ollama"

echo "== Fetching Ollama ${OLLAMA_VERSION} (darwin) =="
rm -rf "$VENDOR_DIR"
mkdir -p "$VENDOR_DIR"
tmpfile="$(mktemp)"
curl -fsSL -o "$tmpfile" \
  "https://github.com/ollama/ollama/releases/download/${OLLAMA_VERSION}/ollama-darwin.tgz"
tar -xzf "$tmpfile" -C "$VENDOR_DIR"
rm -f "$tmpfile"

echo "== Building with PyInstaller =="
pyinstaller packaging/app.spec --distpath dist --workpath build --noconfirm

echo "== Packaging into a .dmg =="
hdiutil create -volname "Local OCR" -srcfolder "dist/Local OCR.app" \
  -ov -format UDZO "dist/LocalOCR-macos.dmg"

echo ""
echo "Done: dist/LocalOCR-macos.dmg"
echo ""
echo "This build is unsigned and not notarized, so Gatekeeper will refuse"
echo "to open it normally on another Mac. The offline-friendly workaround:"
echo "  1. Copy 'Local OCR.app' out of the mounted dmg."
echo "  2. Right-click it -> Open -> Open (once), instead of double-clicking."
echo "  or: System Settings -> Privacy & Security -> 'Open Anyway' after"
echo "  the first blocked attempt."
echo "Proper code signing + notarization removes this prompt entirely, but"
echo "needs an Apple Developer Program membership (paid, separate from this"
echo "script) — a decision worth making deliberately, not defaulting into."
