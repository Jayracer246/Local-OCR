# Builds the Local OCR Windows package, including a vendored Ollama binary
# for a machine that has no Ollama already installed.
#
# Run from the repo root in PowerShell:
#   python -m venv .venv
#   .venv\Scripts\Activate.ps1
#   pip install -r requirements.lock
#   pip install pyinstaller
#   .\packaging\build_windows.ps1
#
# NOTE ON PROVENANCE: this script was written and reasoned through against
# Ollama's documented release assets and PyInstaller's documented CLI, but
# was authored and only tested on Linux — there is no Windows machine in
# that loop. The fetch URL, extraction, and `pyinstaller` invocation are
# the well-established parts; if anything here errors, it is far more
# likely a PowerShell quoting/execution-policy detail than the approach
# itself. Please report back what broke.

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$OllamaVersion = if ($env:OLLAMA_VERSION) { $env:OLLAMA_VERSION } else { "v0.34.0" }
$VendorDir = Join-Path $RepoRoot "packaging\vendor\ollama"

Write-Host "== Fetching Ollama $OllamaVersion (windows-amd64) =="
if (Test-Path $VendorDir) { Remove-Item -Recurse -Force $VendorDir }
New-Item -ItemType Directory -Force -Path $VendorDir | Out-Null
$zipPath = Join-Path $env:TEMP "ollama-windows-amd64.zip"
Invoke-WebRequest -Uri "https://github.com/ollama/ollama/releases/download/$OllamaVersion/ollama-windows-amd64.zip" -OutFile $zipPath
Expand-Archive -Path $zipPath -DestinationPath $VendorDir -Force
Remove-Item $zipPath

Write-Host ""
Write-Host "If this machine (or the ones you're targeting) has an AMD GPU,"
Write-Host "also fetch ollama-windows-amd64-rocm.zip from the same release"
Write-Host "and extract it into $VendorDir, merging with what's already"
Write-Host "there, before continuing — the base zip only carries NVIDIA/CPU"
Write-Host "support."
Write-Host ""

Write-Host "== Building with PyInstaller =="
pyinstaller packaging\app.spec --distpath dist --workpath build --noconfirm

Write-Host "== Packaging =="
Compress-Archive -Path "dist\LocalOCR" -DestinationPath "dist\LocalOCR-windows-x64.zip" -Force

Write-Host ""
Write-Host "Done: dist\LocalOCR-windows-x64.zip"
Write-Host "Extract it anywhere and run LocalOCR.exe to start the app."
Write-Host ""
Write-Host "This build is unsigned, so first launch will likely trigger a"
Write-Host "'Windows protected your PC' SmartScreen warning. Click 'More"
Write-Host "info' -> 'Run anyway'. Code-signing removes this but needs a"
Write-Host "paid code-signing certificate, which is a separate decision."
Write-Host ""
Write-Host "Optional: packaging\windows_installer.iss builds a proper"
Write-Host "installer (Start Menu shortcut, uninstaller) via Inno Setup"
Write-Host "(https://jrsoftware.org/isinfo.php) if you want that instead"
Write-Host "of handing out a zip. Run: iscc packaging\windows_installer.iss"
