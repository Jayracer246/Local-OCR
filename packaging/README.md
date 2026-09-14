# Packaging Local OCR for offline install

Produces a standalone build of the app for Windows, macOS, or Linux —
no separate Python install needed on the target machine — with a copy of
Ollama's own CLI bundled inside as a fallback for a machine that doesn't
already have Ollama.

## How the Ollama bundling works

The app never drives Ollama's own GUI installer, silently or otherwise —
Ollama's own docs describe a separate standalone CLI archive specifically
[for embedding Ollama in another application](https://github.com/ollama/ollama/blob/main/docs/windows.mdx#standalone-cli):
that's what gets bundled here.

At startup (`ollama_bootstrap.ensure_ollama_running()`, called from
`main.py`), the app:

1. Checks whether something already answers at `http://localhost:11434`
   and identifies itself as Ollama — the exact same check
   (`verify_ollama_endpoint`) the app already runs before ever sending a
   document. If so, it does nothing. **An existing Ollama install, and
   its models, are always preferred and never touched.**
2. Otherwise, if this is a packaged build with a vendored binary, spawns
   it (`ollama serve`) as a background process and waits up to 15 seconds
   for it to come up. If nothing was bundled, or it doesn't come up in
   time, the app opens anyway — OCR will show the same "No response from
   the Ollama server" error it always has until one is available.

Running from source (`python main.py`) always skips straight to step 1 —
there's nothing bundled to fall back to.

The bundled server uses Ollama's own default model directory
(`~/.ollama` / `%USERPROFILE%\.ollama`), so if the user later installs
Ollama for real, or already has models pulled from a previous install, it
picks up the exact same models — nothing is duplicated.

## Build once per OS

A PyInstaller spec is portable; a *build* is not — it embeds that OS's
Python, Tcl/Tk, and tkinterdnd2's native extension for that platform.
There is no reliable way to cross-build a Windows or macOS GUI app from
Linux, so this has to run on each target OS separately.

| OS | Script | Tested by the author? |
|---|---|---|
| Linux | `packaging/build_linux.sh` | Yes — built, extracted fresh, and run end to end. |
| Windows | `packaging/build_windows.ps1` | No Windows machine available. Written against documented Ollama release assets and PyInstaller's CLI; please report back what breaks. |
| macOS | `packaging/build_macos.sh` | No Mac available. Same caveat. |

Each script:

1. Downloads that OS's standalone Ollama release asset from
   [github.com/ollama/ollama/releases](https://github.com/ollama/ollama/releases)
   into `packaging/vendor/ollama/` (gitignored — several hundred MB to
   over a gigabyte, never committed).
2. Runs `pyinstaller packaging/app.spec`, which bundles the app, its
   Python dependencies, and that vendored binary into one directory
   (`dist/LocalOCR/` or `dist/Local OCR.app`).
3. Zips/tars/dmg's the result into something you hand to another machine.

### Prerequisites (any OS)

```
python3 -m venv .venv
source .venv/bin/activate        # .venv\Scripts\Activate.ps1 on Windows
pip install -r requirements.lock
pip install pyinstaller
```

Then run that OS's script from the repo root.

### GPU support in the bundled Ollama

The base Windows/Linux downloads include NVIDIA + CPU support already —
confirmed on this machine's Linux build, which correctly detected an
RTX 3060 Ti via CUDA with no system CUDA toolkit installed (Ollama
bundles the CUDA runtime libraries it needs, not just relying on the
driver). AMD needs an extra download on Windows/Linux (mentioned in each
build script's output); macOS's Metal support is built into the one
`ollama-darwin.tgz` binary.

### Unsigned builds

Neither script code-signs anything, so:

- **Windows**: first launch triggers a SmartScreen "Windows protected
  your PC" warning (More info → Run anyway).
- **macOS**: Gatekeeper refuses to open it normally; right-click → Open
  (once) or allow it via System Settings → Privacy & Security.

Proper signing removes both, but needs a paid code-signing certificate
(Windows) or an Apple Developer Program membership (macOS) — a real cost
and process worth deciding on deliberately rather than defaulting into.

### Optional: a real Windows installer

`packaging/build_windows.ps1` produces a zip. If you'd rather hand out an
installer with a Start Menu entry and an uninstaller,
`packaging/windows_installer.iss` wraps `dist\LocalOCR\` with
[Inno Setup](https://jrsoftware.org/isinfo.php) — also unverified here
for the same reason (no Windows machine), but written against documented
Inno Setup syntax:

```
iscc packaging\windows_installer.iss
```

## What's deliberately out of scope

- **Model weights are never bundled.** They're tens of GB, change often,
  and the point of bundling just the Ollama runtime is that `ollama pull`
  works the same offline-or-not way it always has once Ollama is up —
  pulling a model is a separate, deliberate step for whoever sets up the
  offline machine, same as today.
- **No app icon yet** — `packaging/app.spec` has `icon=None`; drop an
  `.ico`/`.icns` into `packaging/` and point the spec at it whenever one
  exists.
