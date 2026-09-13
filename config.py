"""Configuration constants for the Local OCR application."""

# The host is locked to loopback — see ocr_service.normalize_ollama_url.
# Only the port and any local path prefix are user-adjustable. This is what
# makes "nothing leaves your machine" an enforced property rather than a
# documented intention.
DEFAULT_OLLAMA_URL = "http://localhost:11434"

# Suggestions only. These tags are never assumed to exist on the server and
# are never pulled automatically.
EXAMPLE_MODELS = ["gemma4:12b", "qwen3.6:27b"]

DPI_OPTIONS = [100, 150, 200, 300]
DEFAULT_DPI = 150

# Plain-language guidance shown in the Settings tab so "150 DPI" means
# something to someone who has never had a reason to think about scan
# resolution. (label, one-line description of when to reach for it).
DPI_GUIDE = [
    (100, "Fastest",
     "Large, clean, typed text where speed matters more than fine detail — "
     "quick previews or a first pass over a big batch."),
    (150, "Balanced — the default",
     "Most everyday scans: contracts, letters, reports, typed pages in "
     "normal print size."),
    (200, "Sharper",
     "Smaller print, denser tables, or a scan that isn't perfectly clean."),
    (300, "Highest fidelity",
     "Tiny text, faint or low-contrast originals, or handwriting — anything "
     "where getting it right matters more than getting it fast. Slower, "
     "and uses more memory per page."),
]

# GPU handling. Values are what the app stores/sends; labels are what the
# Settings tab shows. Ollama accepts these as ordinary per-request options
# (see ocr_service.build_ollama_options) — no server restart needed to
# change them.
GPU_MODE_AUTO = "auto"
GPU_MODE_GPU = "gpu"
GPU_MODE_CPU = "cpu"
GPU_MODE_OPTIONS = [GPU_MODE_AUTO, GPU_MODE_GPU, GPU_MODE_CPU]
GPU_MODE_LABELS = {
    GPU_MODE_AUTO: "Auto",
    GPU_MODE_GPU: "GPU",
    GPU_MODE_CPU: "CPU only",
}
# Sane bound for a manually-entered GPU index — nothing plausible exceeds
# this, so anything past it is a typo rather than a real card.
MAX_GPU_INDEX = 15

PDF_EXTENSIONS = frozenset({".pdf"})
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})
SUPPORTED_EXTENSIONS = PDF_EXTENSIONS | IMAGE_EXTENSIONS

# The formats an input image is actually allowed to BE, as opposed to what
# its filename claims. Pillow dispatches on magic bytes, not on the
# extension, so without this a file named .png that is really a PSD, GD,
# FITS or JPEG2000 would be handed to that format's decoder — historically
# the richest source of memory-corruption CVEs in the library. Keep this in
# step with IMAGE_EXTENSIONS.
ALLOWED_IMAGE_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})

# Upper bound on the pixels in a single decoded image. Guards against
# decompression bombs: a few KB of compressed data can describe a canvas
# large enough to exhaust memory. 150 MP comfortably clears an A0 sheet at
# 300 DPI (~139 MP), the largest thing anyone plausibly scans.
MAX_IMAGE_PIXELS = 150_000_000

# Reject absurd PDFs up front rather than discovering them halfway through.
# A 2000-page job at even 10s per page already runs for over five hours, so
# anything past this is a mistake rather than an intention.
MAX_PDF_PAGES = 2000

# Same pixel ceiling, applied to a PDF page before it is rasterized. The PDF
# format permits pages up to 200x200 inches, which at 300 DPI is 3.6 billion
# pixels — a ~500-byte file that asks for ~10 GB. Checking page.rect first
# means the allocation is never attempted.
MAX_PAGE_PIXELS = MAX_IMAGE_PIXELS

# Seconds. Probe of /api/version used to confirm something Ollama-shaped is
# listening before any page image is sent to it.
ENDPOINT_VERIFY_TIMEOUT = 10

# Seconds. Model listing should fail fast.
MODEL_LIST_TIMEOUT = 10
# Seconds of "silence" between stream chunks before a page request is
# considered stuck. OCR runs with stream=True, so the httpx timeout applies
# to the pauses between chunks rather than to the whole response — better
# than a single overall timeout (a long page won't hit it).
OCR_STREAM_IDLE_TIMEOUT = 120

# Milliseconds — throttling of live stream UI updates so Tk doesn't choke
# on a flood of tiny inserts.
STREAM_UI_FLUSH_MS = 100

# Pixels — longest side of the page preview thumbnail. Large enough to stay
# crisp if a bigger side-by-side view reuses the same bytes later; the live
# preview just displays it scaled down.
THUMBNAIL_MAX_SIDE = 900

# Milliseconds between main-thread drains of the worker event queue.
UI_POLL_INTERVAL_MS = 50

SYSTEM_PROMPT = (
    "Convert this image into Markdown text format. Your task is to perform "
    "high-accuracy Optical Character Recognition (OCR). Preserve the "
    "document's structure as accurately as possible: headers, lists, and "
    "tables. Do not add any greetings, explanations, or "
    "introductory/concluding remarks. Output only the raw recognized text."
)

USER_PROMPT = "Recognize this document page."
