"""Persisted user preferences.

Tk-free so it can be tested headlessly. Everything here fails soft: a
missing, unreadable or corrupt settings file must leave the app starting
normally with defaults, never crashing or refusing to run.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import config

APP_DIR_NAME = "local-ocr"
FILE_NAME = "settings.json"

DEFAULTS: dict = {
    "model": "",
    "dpi": config.DEFAULT_DPI,
    "output_dir": None,
    "appearance": "light",
    "window": None,          # "WxH" geometry, e.g. "960x720"
    "recursive": False,      # recurse into subfolders when a folder is dropped
    "gpu_mode": config.GPU_MODE_AUTO,   # "auto" | "gpu" | "cpu"
    "gpu_index": None,       # optional int; only consulted when gpu_mode == "gpu"
}


def config_dir() -> Path:
    """Per-platform configuration directory, honouring XDG on Linux."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_DIR_NAME


def settings_path() -> Path:
    return config_dir() / FILE_NAME


def _sanitize(raw: dict) -> dict:
    """Coerce a loaded file into something safe to act on.

    The settings file is ordinary user-writable data, so nothing in it is
    trusted: every value is range-checked and anything unrecognised is
    dropped back to its default. The Ollama URL is deliberately NOT stored,
    so no edit to this file can aim the app at a different host — the
    loopback lock stays the only thing that decides that.
    """
    clean = dict(DEFAULTS)
    if not isinstance(raw, dict):
        return clean

    model = raw.get("model")
    if isinstance(model, str) and len(model) <= 200:
        clean["model"] = model.strip()

    dpi = raw.get("dpi")
    if isinstance(dpi, int) and dpi in config.DPI_OPTIONS:
        clean["dpi"] = dpi

    output_dir = raw.get("output_dir")
    if isinstance(output_dir, str) and output_dir:
        candidate = Path(output_dir).expanduser()
        # Only restore it if it still exists and is writable; a folder that
        # has since been deleted or unmounted silently reverts to "beside
        # the input" rather than failing at the end of a long job.
        if candidate.is_dir() and os.access(candidate, os.W_OK | os.X_OK):
            clean["output_dir"] = str(candidate)

    appearance = raw.get("appearance")
    if appearance in ("light", "dark", "system"):
        clean["appearance"] = appearance

    window = raw.get("window")
    if isinstance(window, str) and len(window) <= 24:
        parts = window.lower().split("x")
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            width, height = (int(p) for p in parts)
            if 400 <= width <= 8000 and 300 <= height <= 8000:
                clean["window"] = f"{width}x{height}"

    if isinstance(raw.get("recursive"), bool):
        clean["recursive"] = raw["recursive"]

    gpu_mode = raw.get("gpu_mode")
    if gpu_mode in config.GPU_MODE_OPTIONS:
        clean["gpu_mode"] = gpu_mode

    gpu_index = raw.get("gpu_index")
    # bool is an int subclass — exclude it explicitly so a stray `true`
    # in the file doesn't silently become GPU index 1.
    if (isinstance(gpu_index, int) and not isinstance(gpu_index, bool)
            and 0 <= gpu_index <= config.MAX_GPU_INDEX):
        clean["gpu_index"] = gpu_index

    return clean


def load(path: Path | None = None) -> dict:
    """Read settings, returning defaults for anything missing or invalid."""
    target = path or settings_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        # Missing, unreadable, or not JSON — all the same outcome.
        return dict(DEFAULTS)
    return _sanitize(raw)


def save(values: dict, path: Path | None = None) -> bool:
    """Write settings atomically. Returns False instead of raising.

    A failure to persist preferences is a nuisance, not an error worth
    interrupting anyone over, so this never propagates an exception.
    """
    target = path or settings_path()
    payload = _sanitize(values)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=target.parent,
            prefix=".settings_",
            suffix=".tmp",
        ) as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            temp_path = Path(handle.name)
        os.replace(temp_path, target)
        return True
    except Exception:
        try:
            temp_path.unlink()           # type: ignore[possibly-undefined]
        except Exception:
            pass
        return False
