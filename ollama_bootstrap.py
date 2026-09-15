"""Best-effort startup of a bundled Ollama server, for the packaged build.

Only relevant when frozen (PyInstaller): running from source with
``python main.py`` has nothing bundled, so this no-ops immediately. When
packaged, a copy of Ollama's own standalone CLI binary — fetched from
Ollama's official GitHub releases at *build* time, never at runtime; see
packaging/README.md — ships inside the app purely as a fallback for a
machine that has no Ollama already installed. That is the integration
path Ollama's own docs describe for embedding it in another application,
rather than driving their separate GUI installer.

This never touches an existing Ollama installation: if something is
already answering at the configured URL and identifies itself as Ollama
(the same verify_ollama_endpoint() check the rest of the app runs before
ever sending it a document), the bundled binary is never even looked at.
Every failure mode here is soft — no bundled binary, a spawn that fails,
a server that never comes up in time — because the alternative (raising
during startup) would turn an optional convenience into a way to make
the whole app fail to open. Worst case, OCR reports the same "No response
from the Ollama server" error it always has, and the user starts one
themselves.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import config
import ocr_service

# Seconds to wait for a freshly spawned server to start answering, and how
# often to check while waiting.
STARTUP_TIMEOUT = 15
POLL_INTERVAL = 0.5


def _bundled_binary_dir() -> "Path | None":
    """Where a bundled ollama/ollama.exe would live, if this is a frozen
    build. None when running from source — there is nothing to bundle."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(getattr(sys, "_MEIPASS", "")) / "ollama_bin"


def _binary_path() -> "Path | None":
    """Find the vendored binary under ollama_bin/, wherever it landed.

    Ollama's own release archives don't put the executable at the top
    level — the real layout is bin/ollama[.exe] next to a lib/ollama/
    directory of shared libraries the binary loads via a relative path at
    startup (confirmed by actually running one: it found its libs and
    listed CUDA devices with no extra environment setup). Searching
    rather than hardcoding that path means a future Ollama release
    changing its archive layout doesn't silently break this.
    """
    directory = _bundled_binary_dir()
    if directory is None or not directory.is_dir():
        return None
    name = "ollama.exe" if sys.platform.startswith("win") else "ollama"
    matches = sorted(directory.rglob(name))
    return matches[0] if matches else None


def _already_running(url: str) -> bool:
    try:
        client = ocr_service.make_client(url, config.ENDPOINT_VERIFY_TIMEOUT)
        ocr_service.verify_ollama_endpoint(client)
    except Exception:
        return False
    return True


def ensure_ollama_running(url: str = config.DEFAULT_OLLAMA_URL) -> None:
    """Spawn the bundled Ollama binary if nothing is answering yet."""
    if _already_running(url):
        return
    binary = _binary_path()
    if binary is None:
        return

    popen_kwargs = {}
    if sys.platform.startswith("win"):
        # Otherwise a console window flashes open behind the GUI.
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        subprocess.Popen(
            [str(binary), "serve"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, **popen_kwargs,
        )
    except OSError:
        return

    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if _already_running(url):
            return
        time.sleep(POLL_INTERVAL)
