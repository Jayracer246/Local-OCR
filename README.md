# Local OCR

A local, privacy-focused desktop application that converts PDFs and images to
structured Markdown using Vision-Language models served by
[Ollama](https://ollama.com).

**Nothing leaves your machine.** This is enforced, not just intended: the app
will only connect to an Ollama server on loopback (`localhost` / `127.0.0.1` /
`::1`), and HTTP redirects are disabled so nothing listening on that port can
bounce your page images somewhere else. There is no configuration that makes
it talk to another host.

## Requirements

- Python 3.10 or newer with Tkinter support (macOS, Linux, or Windows).
  Drag and drop needs `tkinterdnd2`, which is installed with the other
  dependencies; if its Tcl extension will not load on your system the app
  still runs and only the drop target is missing.
  Both Tk 8.6 and Tk 9.0 work with the pinned customtkinter version
  (customtkinter 6.x; older 5.2.x renders blank windows under Tk 9.0 on
  macOS).
- A running Ollama server **on this machine**. The app never starts,
  installs, or pulls anything itself.
- A vision-capable model installed on that server.

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.lock     # pinned, reproducible
```

`requirements.txt` holds the supported version ranges; `requirements.lock`
holds the exact set that CI tests and audits. The version floors are security
floors — `pip-audit -r requirements.txt` runs on every push and weekly, so
please do not lower them.

To regenerate the lockfile after changing `requirements.txt`:

```bash
python3 -m venv .lockenv
.lockenv/bin/pip install -r requirements.txt
.lockenv/bin/pip freeze > requirements.lock
```

## Running

```bash
python main.py
```

## Setting up Ollama

The app requires an Ollama server that is already running. To see which
models a server has installed:

```bash
ollama list
```

To install a vision-capable model:

```bash
ollama pull <model-tag>
```

The model suggestions shown in the app (`gemma4:12b`, `qwen3.6:27b`) are
examples only — they are not guaranteed to exist on your server and are never
pulled automatically. Use `Refresh Models` to list what your server actually
has, or type any model tag manually.

### Server URL

Keep the default `http://localhost:11434`. If your Ollama listens on a
different port, or behind a reverse proxy on loopback, change the port or add
the path prefix — `http://127.0.0.1:11500` and `http://localhost/ollama` are
both fine.

**Remote servers are not supported by design.** A LAN or internet address
(`http://192.168.1.50:11434`, `https://ollama.example.com`) is rejected with
an explanatory message. Ollama has no built-in authentication and the API is
plain HTTP, so pointing this app at a remote server would put every page
image of every document you scan on the wire, unencrypted and unauthenticated
— which is exactly the property this app exists to avoid. If you need remote
inference, use a tool that was designed for it.

## Usage

1. **Drop files or a folder onto the window**, or use `Choose files` /
   `Choose folder`. Multiple documents can be queued at once; dropping a
   folder picks up every supported file in it. Anything that is not a PDF,
   PNG, JPEG or WebP is skipped and listed in the Log.
2. Confirm the server URL, pick or type a model tag, and choose a PDF DPI
   (100/150/200/300; higher is sharper but slower — DPI only affects PDFs).
3. `Start OCR`. Each PDF page is rendered and sent to the model in order.

Page images exist only in memory. A PDF page is rendered when the model is
ready for it and released as soon as its text comes back, so nothing is
written to disk except the final Markdown — there is no scratch directory, and
a crash or force-quit cannot strand readable images of your document in the
system temp folder. It also means memory use does not grow with page count: a
thousand-page PDF costs the same as a one-page one.

While a job runs you can follow it in several places:

- A **progress bar** with a page counter (`Page 3 / 12`) advances one step per
  page as each is rendered and recognized.
- A **preview panel** displays a thumbnail of the page currently being read.
- The **Result** tab fills with the recognized Markdown live, token by token,
  as the model streams it. A `Copy` button copies the full text to the
  clipboard.
- The **Log** tab keeps the status messages.
- The **Review** tab pairs each finished page's image with its text
  side by side, with `◀` / `▶` navigation for spot-checking quality.

While a job is running, `Start OCR` becomes `Cancel`. Cancelling stops after
the current page and writes no further output.

With several documents queued, progress reads `File 3 of 7 · page 4 of 11`.
**One bad document does not end the run** — a corrupt PDF is recorded in the
Log and the batch moves on, because losing forty finished documents to a
malformed forty-first would be the wrong trade. The summary at the end says
how many succeeded.

Model, quality, output folder, appearance and window size are remembered
between launches, in `~/.config/local-ocr/settings.json` (XDG-respecting;
`~/Library/Application Support` on macOS, `%APPDATA%` on Windows). The file
holds preferences only — **the server address is never stored there**, so no
edit to it can point the app at a different host.

On success the app switches to the Result tab and shows a dialog with `Open`
(open the `.md` in your default app), `Show in Finder` (or `Open Folder` off
macOS), and `OK`. On error it switches to the Log tab.

### Output

The result is saved as UTF-8 Markdown with the `_extracted.md` suffix —
`/docs/report.pdf` becomes `report_extracted.md`.

By default it goes **next to the input file**. The `Save to` row changes that:
pick any writable folder and results go there instead, which is what you want
when the document itself lives somewhere that syncs to a cloud. `Reset`
returns to the default.

If the output file already exists you are asked before it is overwritten;
declining leaves it untouched. The file is written atomically, so a failed run
never leaves a partial result, and with owner-only permissions (`0600`).

Anything in the recognized text that would make a Markdown previewer fetch a
remote URL on open — an `![image](http://…)` reference, an `<img>` or
`<iframe>` tag — is defused before saving. The URL stays fully visible and
clickable; it just no longer loads by itself. This matters because the text is
whatever the *model* produced from the page, and a crafted document can steer
that; opening the result should not phone anyone. The log says so whenever
something was changed.

## What stays local, and what doesn't

The app itself sends nothing off your machine. Enforced, not just intended:
the server must be loopback, redirects are disabled, and proxy environment
variables are ignored (httpx would otherwise route even `localhost` through
`HTTP_PROXY`). Page images are never written to disk — they are rendered in
memory and released as each page finishes — so a crash cannot strand them in
your temp folder either.

Three things sit outside that boundary. None is a defect in this app, but for
a tool whose whole point is staying local they are worth stating plainly:

- **The `Copy` button** puts the recognized text on your system clipboard.
  macOS syncs the clipboard to your other devices via Universal Clipboard,
  Windows Cloud Clipboard syncs to a Microsoft account when enabled, and
  clipboard-manager utilities keep searchable history on disk.
- **The output file lands next to the input by default.** If you scan a
  document that lives in Dropbox, OneDrive, iCloud Drive, Google Drive or a
  Syncthing folder, the `.md` is created in that same folder and your sync
  client will upload it. File permissions do not stop this — the sync client
  runs as you. Use **Save to → Change...** to send results somewhere else.
- **Ollama and the model.** This app hands pages to Ollama and its
  responsibility ends there. Before the first page it checks that the process
  on the port answers `/api/version` the way Ollama does, so an obvious
  impostor fails loudly — but that is a sanity check, not authentication.

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| `connection refused` | Ollama is not running, or the URL/port is wrong. Start Ollama (`ollama serve` or the desktop app) and verify the URL. |
| Timeout | The model is too slow for the page. Try a smaller model or a lower DPI. |
| `only connects to an Ollama server running on this machine` | You entered a non-local address. Local OCR is loopback-only by design — see [Server URL](#server-url). |
| `did not identify itself as Ollama` | Something other than Ollama is listening on that port. Check the port, and what else is running on it. |
| `its contents are not a PNG, JPEG or WebP image` | The file's extension does not match what it actually is. The real format is what gets decoded, so the app goes by content, not by name. |
| `above the … megapixel limit` | The page is enormous at this DPI. The message suggests a DPI that fits; a lower one also renders faster. |
| `above the 2000-page limit` | Split the PDF and run the parts separately. |
| Drag and drop does nothing | `tkinterdnd2`'s Tcl extension did not load. Use `Choose files` / `Choose folder` instead; everything else works. |
| Settings not remembered | The config file could not be written. Check permissions on `~/.config/local-ocr/`. |
| `model not found` | The tag is not installed on that server. Check `ollama list` and `ollama pull <tag>` on the server. Models are never pulled automatically. |
| Empty or garbage output / "returned no text" | The selected model has no vision support. Choose a vision-capable model. |
