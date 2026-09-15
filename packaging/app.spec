# PyInstaller spec for Local OCR.
#
# Run from the repo root with:  pyinstaller packaging/app.spec
#
# This produces a one-directory build (dist/LocalOCR/) rather than a single
# file: tkinterdnd2 loads a native Tcl extension at runtime, and onefile's
# unpack-to-a-temp-dir-on-every-launch model has a known history of
# fighting that lookup. One directory you can zip/copy/wrap in an
# installer is simpler to reason about, and startup is instant.
#
# Must be run separately on each target OS (Windows, macOS, Linux) — a
# spec file is portable, a *build* is not: it embeds that OS's Python,
# Tcl/Tk, and the native tkinterdnd2 extension for that platform.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

# Repo root: this spec lives in packaging/, the app modules live one level up.
ROOT = Path(SPECPATH).parent

datas = []
datas += collect_data_files("customtkinter")  # icons/fonts/themes it draws chrome from
datas += collect_data_files("tkinterdnd2")     # the tkdnd/<platform>/ native extension

# A vendored Ollama binary (and, on Windows, its accompanying GPU DLLs), if
# the build script for this OS fetched one into packaging/vendor/ollama/
# (see build_linux.sh / build_windows.ps1 / build_macos.sh). Optional:
# building without it just means ollama_bootstrap.ensure_ollama_running()
# has nothing to fall back to, and the app behaves exactly as it does when
# run from source. A whole directory (possibly several files, not just the
# one executable) needs Tree(), not a datas tuple — datas pairs individual
# files with a destination.
VENDOR_OLLAMA = ROOT / "packaging" / "vendor" / "ollama"
ollama_tree = Tree(str(VENDOR_OLLAMA), prefix="ollama_bin") if VENDOR_OLLAMA.is_dir() else []

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LocalOCR",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # GUI app: no terminal window
    disable_windowed_traceback=False,
    argv_emulation=(sys.platform == "darwin"),
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # .ico on Windows only — macOS gets its icon via BUNDLE() below instead,
    # and a Linux ELF binary has nowhere to embed one (the .desktop entry's
    # Icon= is what matters there).
    icon=(str(ROOT / "packaging" / "icon.ico") if sys.platform.startswith("win") else None),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    ollama_tree,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LocalOCR",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Local OCR.app",
        icon=str(ROOT / "packaging" / "icon.icns"),
        bundle_identifier="com.local-ocr.app",
        info_plist={
            "NSHighResolutionCapable": "True",
            "CFBundleShortVersionString": "1.0.0",
        },
    )
