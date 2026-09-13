"""LocalOCRApp: all widgets, state, and main-thread UI behavior.

Threading contract: workers never touch Tk. They only put plain-value
events on self.event_queue; drain_ui_events() runs on the Tk main thread
via .after() and performs every GUI update.
"""

from __future__ import annotations

import bisect
import io
import queue
import sys
import threading
from collections import OrderedDict
from enum import Enum, auto
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image

import config
import ocr_service
import settings as user_settings
import theme
from ocr_service import OCRRequest

# Drag and drop is a nicety, not a requirement: if tkinterdnd2 is missing or
# its Tcl extension will not load on this machine, the app runs exactly as
# before and only the drop target is absent.
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _DND_BASE = TkinterDnD.DnDWrapper
    DND_AVAILABLE = True
except Exception:                                        # pragma: no cover
    DND_FILES = None

    class _DND_BASE:                                     # type: ignore[no-redef]
        pass

    DND_AVAILABLE = False


class OperationState(Enum):
    IDLE = auto()
    REFRESHING_MODELS = auto()
    PROCESSING_OCR = auto()


FILE_DIALOG_FILTERS = [
    ("Supported documents", "*.pdf *.png *.jpg *.jpeg *.webp"),
    ("PDF files", "*.pdf"),
    ("Images", "*.png *.jpg *.jpeg *.webp"),
    ("All files", "*.*"),
]

PAD = 18           # outer gutter
GAP = 12           # between cards
INNER = 16         # inside a card
PREVIEW_WIDTH = 210
REVIEW_IMAGE_WIDTH = 380
REVIEW_IMAGE_MAX_H = 520
REVIEW_IMAGE_CACHE_SIZE = 5
QUEUE_LIST_HEIGHT = 132   # px — the scrollable queue list before it scrolls


class LocalOCRApp(ctk.CTk, _DND_BASE):
    def __init__(self) -> None:
        super().__init__()

        self.prefs = user_settings.load()

        self.title("Local OCR")
        self.geometry(self.prefs.get("window") or "960x780")
        self.minsize(760, 620)

        self.operation_state = OperationState.IDLE
        self.closing = False
        # A real queue: keyed by resolved path so the same file dropped
        # twice doesn't duplicate, ordered by when it was added so the list
        # reads the way a queue should.
        self._queue: "OrderedDict[Path, Path]" = OrderedDict()
        self._queue_rows: dict[Path, ctk.CTkFrame] = {}
        self.event_queue: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self._render_phase_seen = False

        stored_output = self.prefs.get("output_dir")
        self.output_dir: Path | None = Path(stored_output) if stored_output else None

        self._stream_buffer = ""
        self._stream_flush_scheduled = False
        self._result_page = 0
        self._batch_index = 0
        self._batch_total = 0

        self.review_pages: dict[int, dict] = {}
        self._review_order: list[int] = []
        self._review_index = 0
        self._review_total = 0
        self._review_image_cache: "OrderedDict[int, ctk.CTkImage]" = OrderedDict()

        self._build_layout()
        self._enable_drag_and_drop()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(config.UI_POLL_INTERVAL_MS, self.drain_ui_events)

    @property
    def selected_paths(self) -> list[Path]:
        """The current queue, in the order documents were added to it."""
        return list(self._queue.values())

    # ------------------------------------------------------------- layout

    def _font(self, role: str, size: int, weight: str = "normal") -> ctk.CTkFont:
        return ctk.CTkFont(family=theme.fonts()[role], size=size, weight=weight)

    def _build_layout(self) -> None:
        self.configure(fg_color=theme.SURFACE)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # Everything lives inside one scrollable region instead of gridding
        # straight onto the window. Several rows below (the dropzone's
        # queue list especially) have grown taller than a fixed layout can
        # always guarantee space for; without this, shrinking the window —
        # or just having a shorter screen — pushed the Start button and the
        # tabs clean off the bottom with no way to reach them. A window
        # that's too short now gets a scrollbar instead of losing controls.
        self.root_scroll = ctk.CTkScrollableFrame(
            self, fg_color=theme.SURFACE, corner_radius=0)
        self.root_scroll.grid(row=0, column=0, sticky="nsew")
        self.root_scroll.grid_columnconfigure(0, weight=1)
        self.root_scroll.grid_rowconfigure(5, weight=1)  # tab area absorbs resize

        self._build_header()
        self._build_dropzone()
        self._build_settings()
        self._build_action()
        self._build_progress()
        self._build_panels()
        self._build_settings_window()

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self.root_scroll, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=PAD, pady=(PAD, GAP))
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header, text="Local OCR", anchor="w",
            font=self._font("heading", theme.SIZE_TITLE, "bold"),
            text_color=theme.TEXT,
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            header,
            text="Documents to Markdown, entirely on this machine.",
            anchor="w",
            font=self._font("body", theme.SIZE_BODY),
            text_color=theme.TEXT_MUTED,
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))

        self.settings_button = ctk.CTkButton(
            header, text="⚙ Settings", width=110, height=34,
            font=self._font("ui", theme.SIZE_SMALL),
            fg_color="transparent", border_width=1,
            border_color=theme.FIELD_BORDER, text_color=theme.TEXT,
            hover_color=theme.ACCENT_DIM,
            command=self.open_settings,
        )
        self.settings_button.grid(row=0, column=1, rowspan=2, sticky="e")

    def _build_dropzone(self) -> None:
        self.dropzone = ctk.CTkFrame(
            self.root_scroll, fg_color=theme.CARD, border_color=theme.CARD_BORDER,
            border_width=1, corner_radius=theme.RADIUS_CARD,
        )
        self.dropzone.grid(row=1, column=0, sticky="ew", padx=PAD, pady=(0, GAP))
        self.dropzone.grid_columnconfigure(0, weight=1)

        self.drop_headline = ctk.CTkLabel(
            self.dropzone,
            text=("Drop files or a folder here"
                  if DND_AVAILABLE else "Choose what to convert"),
            font=self._font("heading", theme.SIZE_HEADING, "bold"),
            text_color=theme.TEXT,
        )
        self.drop_headline.grid(row=0, column=0, pady=(INNER + 4, 2))

        self.drop_hint = ctk.CTkLabel(
            self.dropzone,
            text="PDF, PNG, JPEG or WebP — drop more anytime to add to the queue",
            font=self._font("body", theme.SIZE_SMALL),
            text_color=theme.TEXT_MUTED,
        )
        self.drop_hint.grid(row=1, column=0, pady=(0, INNER - 4))

        buttons = ctk.CTkFrame(self.dropzone, fg_color="transparent")
        buttons.grid(row=2, column=0, pady=(0, INNER - 4))
        self.select_button = ctk.CTkButton(
            buttons, text="Choose files", width=130, height=34,
            font=self._font("ui", theme.SIZE_BODY),
            command=self.select_file,
        )
        self.select_button.grid(row=0, column=0, padx=(0, 8))
        self.select_folder_button = ctk.CTkButton(
            buttons, text="Choose folder", width=130, height=34,
            font=self._font("ui", theme.SIZE_BODY),
            fg_color="transparent", border_width=1,
            border_color=theme.FIELD_BORDER, text_color=theme.TEXT,
            hover_color=theme.ACCENT_DIM,
            command=self.select_folder,
        )
        self.select_folder_button.grid(row=0, column=1)

        # ---- queue: header (count + clear) then the list itself ----
        queue_header = ctk.CTkFrame(self.dropzone, fg_color="transparent")
        queue_header.grid(row=3, column=0, sticky="ew", padx=INNER, pady=(0, 4))
        queue_header.grid_columnconfigure(0, weight=1)
        self.queue_count_label = ctk.CTkLabel(
            queue_header, text="", anchor="w",
            font=self._font("ui", theme.SIZE_SMALL), text_color=theme.TEXT_MUTED)
        self.queue_count_label.grid(row=0, column=0, sticky="w")
        self.queue_clear_button = ctk.CTkButton(
            queue_header, text="Clear all", width=72, height=24,
            font=self._font("ui", theme.SIZE_SMALL),
            fg_color="transparent", border_width=1,
            border_color=theme.FIELD_BORDER, text_color=theme.TEXT_MUTED,
            hover_color=theme.ACCENT_DIM, command=self.clear_queue,
        )
        self.queue_clear_button.grid(row=0, column=1, sticky="e")
        self.queue_clear_button.grid_remove()

        self.queue_empty_label = ctk.CTkLabel(
            self.dropzone, text="Nothing selected yet", anchor="center",
            font=self._font("ui", theme.SIZE_SMALL),
            text_color=theme.TEXT_MUTED, justify="center",
        )
        self.queue_empty_label.grid(row=4, column=0, sticky="ew",
                                    padx=INNER, pady=(0, INNER))

        self.queue_scroll = ctk.CTkScrollableFrame(
            self.dropzone, fg_color="transparent", height=QUEUE_LIST_HEIGHT,
            corner_radius=0)
        self.queue_scroll.grid_columnconfigure(0, weight=1)
        self.queue_scroll.grid(row=4, column=0, sticky="ew",
                               padx=INNER, pady=(0, INNER))
        self.queue_scroll.grid_remove()   # shown once something is queued

    def _build_settings(self) -> None:
        card = ctk.CTkFrame(
            self.root_scroll, fg_color=theme.CARD, border_color=theme.CARD_BORDER,
            border_width=1, corner_radius=theme.RADIUS_CARD,
        )
        card.grid(row=2, column=0, sticky="ew", padx=PAD, pady=(0, GAP))
        card.grid_columnconfigure(1, weight=1)

        label_font = self._font("ui", theme.SIZE_SMALL)
        value_font = self._font("ui", theme.SIZE_BODY)

        ctk.CTkLabel(card, text="QUALITY", font=label_font,
                     text_color=theme.TEXT_MUTED).grid(
            row=0, column=0, sticky="w", padx=(INNER, 8), pady=(INNER, 0))
        self.dpi_combobox = ctk.CTkComboBox(
            card, values=[f"{dpi} DPI" for dpi in config.DPI_OPTIONS],
            state="readonly", width=120, font=value_font, height=34,
            command=lambda _v: self._persist(),
        )
        self.dpi_combobox.set(f"{self.prefs.get('dpi', config.DEFAULT_DPI)} DPI")
        self.dpi_combobox.grid(row=1, column=0, sticky="w",
                               padx=(INNER, 8), pady=(2, INNER))

        ctk.CTkLabel(card, text="SAVE TO", font=label_font,
                     text_color=theme.TEXT_MUTED).grid(
            row=0, column=1, sticky="w", padx=(0, INNER), pady=(INNER, 0))
        destination = ctk.CTkFrame(card, fg_color="transparent")
        destination.grid(row=1, column=1, sticky="ew",
                         padx=(0, INNER), pady=(2, INNER))
        destination.grid_columnconfigure(0, weight=1)
        self.output_label = ctk.CTkLabel(
            destination, text="", anchor="w", font=value_font)
        self.output_label.grid(row=0, column=0, sticky="ew")
        self.output_choose_button = ctk.CTkButton(
            destination, text="Change", width=76, height=30,
            font=self._font("ui", theme.SIZE_SMALL),
            fg_color="transparent", border_width=1,
            border_color=theme.FIELD_BORDER, text_color=theme.TEXT,
            hover_color=theme.ACCENT_DIM, command=self.choose_output_dir)
        self.output_choose_button.grid(row=0, column=1, padx=(8, 0))
        self.output_reset_button = ctk.CTkButton(
            destination, text="Reset", width=62, height=30,
            font=self._font("ui", theme.SIZE_SMALL),
            fg_color="transparent", border_width=1,
            border_color=theme.FIELD_BORDER, text_color=theme.TEXT_MUTED,
            hover_color=theme.ACCENT_DIM, state="disabled",
            command=self.reset_output_dir)
        self.output_reset_button.grid(row=0, column=2, padx=(6, 0))
        self._update_output_label()

        # Server row: loopback-locked, so it is reference information rather
        # than a routine setting. Kept reachable, kept quiet.
        server = ctk.CTkFrame(card, fg_color="transparent")
        server.grid(row=2, column=0, columnspan=2, sticky="ew",
                    padx=INNER, pady=(0, INNER))
        server.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(server, text="SERVER", font=label_font,
                     text_color=theme.TEXT_MUTED).grid(row=0, column=0, sticky="w")
        self.url_entry = ctk.CTkEntry(
            server, font=self._font("mono", theme.SIZE_SMALL), height=30)
        self.url_entry.insert(0, config.DEFAULT_OLLAMA_URL)
        self.url_entry.grid(row=0, column=1, sticky="ew", padx=(10, 8))
        self.refresh_button = ctk.CTkButton(
            server, text="Refresh models", width=120, height=30,
            font=self._font("ui", theme.SIZE_SMALL),
            fg_color="transparent", border_width=1,
            border_color=theme.FIELD_BORDER, text_color=theme.TEXT,
            hover_color=theme.ACCENT_DIM, command=self.refresh_models)
        self.refresh_button.grid(row=0, column=2)
        self.url_hint = ctk.CTkLabel(
            server,
            text="This machine only — localhost or 127.0.0.1.",
            font=self._font("ui", theme.SIZE_SMALL),
            text_color=theme.TEXT_MUTED, anchor="w")
        self.url_hint.grid(row=1, column=1, columnspan=2, sticky="w",
                           padx=(10, 0), pady=(4, 0))

    def _build_action(self) -> None:
        self.start_button = ctk.CTkButton(
            self.root_scroll, text="Start OCR", height=50,
            corner_radius=theme.RADIUS_CONTROL,
            font=self._font("heading", theme.SIZE_HEADING, "bold"),
            command=self.start_ocr,
        )
        self.start_button.grid(row=3, column=0, sticky="ew",
                               padx=PAD, pady=(0, GAP))

    def _build_progress(self) -> None:
        self.status_frame = ctk.CTkFrame(self.root_scroll, fg_color="transparent")
        self.status_frame.grid_columnconfigure(0, weight=1)

        self.status_label = ctk.CTkLabel(
            self.status_frame, text="", anchor="w",
            font=self._font("ui", theme.SIZE_SMALL), text_color=theme.TEXT_MUTED)
        self.status_label.grid(row=0, column=0, sticky="w", pady=(0, 6))

        self.progress = ctk.CTkProgressBar(
            self.status_frame, mode="indeterminate", height=6)
        self.progress.grid(row=1, column=0, sticky="ew")
        self.progress.set(0)
        # Hidden while idle: an empty progress bar is visual noise.
        self.status_frame.grid(row=4, column=0, sticky="ew",
                               padx=PAD, pady=(0, GAP))
        self.status_frame.grid_remove()

    def _build_panels(self) -> None:
        self.bottom_frame = ctk.CTkFrame(self.root_scroll, fg_color="transparent")
        self.bottom_frame.grid(row=5, column=0, sticky="nsew",
                               padx=PAD, pady=(0, PAD))
        self.bottom_frame.grid_columnconfigure(0, weight=0)
        self.bottom_frame.grid_columnconfigure(1, weight=1)
        self.bottom_frame.grid_rowconfigure(0, weight=1)

        self.preview_panel = ctk.CTkFrame(
            self.bottom_frame, width=PREVIEW_WIDTH, fg_color=theme.CARD,
            border_color=theme.CARD_BORDER, border_width=1,
            corner_radius=theme.RADIUS_CARD)
        self.preview_panel.grid(row=0, column=0, sticky="ns", padx=(0, GAP))
        self.preview_panel.grid_propagate(False)
        self.preview_panel.grid_columnconfigure(0, weight=1)
        self.preview_panel.grid_rowconfigure(0, weight=1)
        self.preview_image_label = ctk.CTkLabel(self.preview_panel, text="")
        self.preview_image_label.grid(row=0, column=0, sticky="nsew",
                                      padx=10, pady=(10, 4))
        self.preview_caption = ctk.CTkLabel(
            self.preview_panel, text="Page preview",
            font=self._font("ui", theme.SIZE_SMALL),
            text_color=theme.TEXT_MUTED)
        self.preview_caption.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 10))
        self._preview_image = None

        self.tabview = ctk.CTkTabview(
            self.bottom_frame, fg_color=theme.CARD,
            segmented_button_selected_color=theme.ACCENT_PAIR,
            segmented_button_selected_hover_color=theme.ACCENT_HOVER,
            text_color=theme.TEXT,
            corner_radius=theme.RADIUS_CARD)
        self.tabview.add("Log")
        self.tabview.add("Result")
        self.tabview.add("Review")
        self.tabview.set("Log")
        self.tabview.grid(row=0, column=1, sticky="nsew")

        mono = self._font("mono", theme.SIZE_MONO)

        self.log_box = ctk.CTkTextbox(
            self.tabview.tab("Log"), font=mono, state="disabled", wrap="word",
            border_width=0, fg_color="transparent")
        self.log_box.pack(fill="both", expand=True)

        self.result_frame = self.tabview.tab("Result")
        self.copy_button = ctk.CTkButton(
            self.result_frame, text="Copy", width=72, height=28,
            font=self._font("ui", theme.SIZE_SMALL),
            fg_color="transparent", border_width=1,
            border_color=theme.FIELD_BORDER, text_color=theme.TEXT,
            hover_color=theme.ACCENT_DIM, command=self.copy_result)
        self.copy_button.pack(anchor="e", padx=(0, 4), pady=(4, 2))
        self.result_box = ctk.CTkTextbox(
            self.result_frame, font=mono, state="disabled", wrap="word",
            border_width=0, fg_color="transparent")
        self.result_box.pack(fill="both", expand=True)

        review = self.tabview.tab("Review")
        review.grid_columnconfigure(0, weight=0)
        review.grid_columnconfigure(1, weight=1)
        review.grid_rowconfigure(1, weight=1)

        nav = ctk.CTkFrame(review, fg_color="transparent")
        nav.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(4, 6))
        nav.grid_columnconfigure(1, weight=1)
        self.review_prev_button = ctk.CTkButton(
            nav, text="◀", width=40, height=28, state="disabled",
            fg_color="transparent", border_width=1,
            border_color=theme.FIELD_BORDER, text_color=theme.TEXT,
            hover_color=theme.ACCENT_DIM, command=self.review_prev)
        self.review_prev_button.grid(row=0, column=0, padx=(4, 8))
        self.review_nav_label = ctk.CTkLabel(
            nav, text="No pages yet", font=self._font("ui", theme.SIZE_SMALL),
            text_color=theme.TEXT_MUTED)
        self.review_nav_label.grid(row=0, column=1)
        self.review_next_button = ctk.CTkButton(
            nav, text="▶", width=40, height=28, state="disabled",
            fg_color="transparent", border_width=1,
            border_color=theme.FIELD_BORDER, text_color=theme.TEXT,
            hover_color=theme.ACCENT_DIM, command=self.review_next)
        self.review_next_button.grid(row=0, column=2, padx=(8, 4))

        self.review_image_label = ctk.CTkLabel(
            review, text="", width=REVIEW_IMAGE_WIDTH)
        self.review_image_label.grid(row=1, column=0, sticky="nsew",
                                     padx=(4, 8), pady=4)
        self.review_text = ctk.CTkTextbox(
            review, font=mono, state="disabled", wrap="word",
            border_width=0, fg_color="transparent")
        self.review_text.grid(row=1, column=1, sticky="nsew", pady=4)

    def _build_settings_window(self) -> None:
        """A separate, persistent window (opened from the ⚙ button in the
        header) rather than a tab: appearance, model, and GPU are one-off
        choices you set and forget, not something that needs to compete for
        space with Log/Result/Review while a job is running.

        Built once, up front, and hidden immediately — never destroyed —
        because start_ocr(), _persist() and friends read these widgets
        (model_combobox, appearance_toggle, gpu_mode_segment,
        gpu_index_entry) regardless of whether this window has ever been
        opened.
        """
        window = ctk.CTkToplevel(self)
        window.title("Settings")
        window.geometry("520x760")
        window.minsize(420, 480)
        window.configure(fg_color=theme.SURFACE)
        # Hide rather than destroy: closing the window must not invalidate
        # the widgets other methods rely on for the app's whole lifetime.
        window.protocol("WM_DELETE_WINDOW", window.withdraw)
        self.settings_window = window

        label_font = self._font("ui", theme.SIZE_SMALL)
        value_font = self._font("ui", theme.SIZE_BODY)

        scroll = ctk.CTkScrollableFrame(window, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=PAD, pady=PAD)
        scroll.grid_columnconfigure(0, weight=1)

        # ----------------------------------------------------- appearance
        ctk.CTkLabel(
            scroll, text="APPEARANCE", anchor="w",
            font=self._font("heading", theme.SIZE_HEADING, "bold"),
            text_color=theme.TEXT,
        ).grid(row=0, column=0, sticky="w", pady=(4, 6))
        self.appearance_toggle = ctk.CTkSegmentedButton(
            scroll, values=["Light", "Dark"], width=140,
            font=self._font("ui", theme.SIZE_SMALL),
            command=self._on_appearance_change,
        )
        self.appearance_toggle.set(
            "Dark" if self.prefs.get("appearance") == "dark" else "Light"
        )
        self.appearance_toggle.grid(row=1, column=0, sticky="w", pady=(0, 16))

        # ----------------------------------------------------------- model
        ctk.CTkLabel(
            scroll, text="MODEL", anchor="w",
            font=self._font("heading", theme.SIZE_HEADING, "bold"),
            text_color=theme.TEXT,
        ).grid(row=2, column=0, sticky="w", pady=(4, 6))
        self.model_combobox = ctk.CTkComboBox(
            scroll, values=list(config.EXAMPLE_MODELS), font=value_font,
            height=34, command=lambda _v: self._persist(),
        )
        self.model_combobox.set(self.prefs.get("model") or "")
        self.model_combobox.grid(row=3, column=0, sticky="ew", pady=(0, 16))

        # ---------------------------------------------------------- GPU
        ctk.CTkLabel(
            scroll, text="GPU", anchor="w",
            font=self._font("heading", theme.SIZE_HEADING, "bold"),
            text_color=theme.TEXT,
        ).grid(row=4, column=0, sticky="w", pady=(4, 2))
        ctk.CTkLabel(
            scroll, anchor="w", justify="left", wraplength=520,
            text=("Which processor Ollama should prefer for OCR. This is a "
                  "hint sent with every request, saved as your default — "
                  "Ollama still decides based on what's actually installed."),
            font=self._font("body", theme.SIZE_SMALL), text_color=theme.TEXT_MUTED,
        ).grid(row=5, column=0, sticky="w", pady=(0, 8))

        gpu_row = ctk.CTkFrame(scroll, fg_color="transparent")
        gpu_row.grid(row=6, column=0, sticky="w", pady=(0, 4))
        ctk.CTkLabel(gpu_row, text="MODE", font=label_font,
                     text_color=theme.TEXT_MUTED).grid(
            row=0, column=0, sticky="w", padx=(0, 8))
        self.gpu_mode_segment = ctk.CTkSegmentedButton(
            gpu_row, values=list(config.GPU_MODE_LABELS.values()),
            font=value_font, command=lambda _v: self._on_gpu_mode_change(),
        )
        self.gpu_mode_segment.set(
            config.GPU_MODE_LABELS.get(self.prefs.get("gpu_mode"), "Auto"))
        self.gpu_mode_segment.grid(row=0, column=1, sticky="w")

        index_row = ctk.CTkFrame(scroll, fg_color="transparent")
        index_row.grid(row=7, column=0, sticky="w", pady=(0, 12))
        ctk.CTkLabel(index_row, text="GPU INDEX", font=label_font,
                     text_color=theme.TEXT_MUTED).grid(
            row=0, column=0, sticky="w", padx=(0, 8))
        self.gpu_index_entry = ctk.CTkEntry(
            index_row, width=70, height=30, font=value_font,
            placeholder_text="0")
        stored_index = self.prefs.get("gpu_index")
        if stored_index is not None:
            self.gpu_index_entry.insert(0, str(stored_index))
        self.gpu_index_entry.bind("<FocusOut>", lambda _e: self._persist())
        self.gpu_index_entry.grid(row=0, column=1, sticky="w")
        ctk.CTkLabel(
            index_row,
            text=("Optional — only used in GPU mode, on a machine with more "
                  "than one card. Best-effort: Ollama does not guarantee it."),
            font=self._font("ui", theme.SIZE_SMALL), text_color=theme.TEXT_MUTED,
            anchor="w", justify="left", wraplength=440,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self._on_gpu_mode_change(persist=False)

        # ------------------------------------------------------ DPI guide
        ctk.CTkLabel(
            scroll, text="DPI QUALITY GUIDE", anchor="w",
            font=self._font("heading", theme.SIZE_HEADING, "bold"),
            text_color=theme.TEXT,
        ).grid(row=8, column=0, sticky="w", pady=(12, 2))
        ctk.CTkLabel(
            scroll, anchor="w", justify="left", wraplength=520,
            text=("DPI controls how sharp each page is rendered before it's "
                  "sent off for recognition. Higher DPI reads finer detail "
                  "but takes longer and uses more memory per page."),
            font=self._font("body", theme.SIZE_SMALL), text_color=theme.TEXT_MUTED,
        ).grid(row=9, column=0, sticky="w", pady=(0, 8))

        for offset, (dpi, label, description) in enumerate(config.DPI_GUIDE):
            card = ctk.CTkFrame(
                scroll, fg_color=theme.CARD, border_color=theme.CARD_BORDER,
                border_width=1, corner_radius=theme.RADIUS_CONTROL)
            card.grid(row=10 + offset, column=0, sticky="ew", pady=4)
            card.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(
                card, text=str(dpi), width=56,
                font=self._font("heading", theme.SIZE_HEADING, "bold"),
                text_color=theme.ACCENT,
            ).grid(row=0, column=0, rowspan=2, sticky="w",
                   padx=(INNER, 4), pady=(INNER - 4, INNER - 4))
            ctk.CTkLabel(
                card, text=label, anchor="w",
                font=self._font("ui", theme.SIZE_BODY, "bold"),
                text_color=theme.TEXT,
            ).grid(row=0, column=1, sticky="ew", padx=(0, INNER), pady=(INNER - 4, 0))
            ctk.CTkLabel(
                card, text=description, anchor="w", justify="left",
                wraplength=420,
                font=self._font("body", theme.SIZE_SMALL),
                text_color=theme.TEXT_MUTED,
            ).grid(row=1, column=1, sticky="ew", padx=(0, INNER), pady=(0, INNER - 4))

        window.withdraw()  # built eagerly so its widgets exist; hidden until asked for

    # ----------------------------------------------------------------- GPU

    def _on_gpu_mode_change(self, persist: bool = True) -> None:
        is_gpu = self.gpu_mode_segment.get() == config.GPU_MODE_LABELS[config.GPU_MODE_GPU]
        self.gpu_index_entry.configure(state="normal" if is_gpu else "disabled")
        if persist:
            self._persist()

    def _gpu_mode(self) -> str:
        label = self.gpu_mode_segment.get()
        for value, text in config.GPU_MODE_LABELS.items():
            if text == label:
                return value
        return config.GPU_MODE_AUTO

    def _gpu_index(self) -> "int | None":
        text = self.gpu_index_entry.get().strip()
        if not text:
            return None
        try:
            value = int(text)
        except ValueError:
            return None
        return value if 0 <= value <= config.MAX_GPU_INDEX else None

    def open_settings(self) -> None:
        """Show the settings window, raising it if it's already open."""
        self.settings_window.deiconify()
        self.settings_window.lift()
        self.settings_window.focus_force()

    # -------------------------------------------------------- drag & drop

    def _enable_drag_and_drop(self) -> None:
        self.dnd_active = False
        if not DND_AVAILABLE:
            return
        try:
            self.TkdndVersion = TkinterDnD._require(self)
            for widget in (self.dropzone, self.drop_headline, self.drop_hint,
                           self.queue_empty_label, self.queue_scroll):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop)
                widget.dnd_bind("<<DragEnter>>", self._on_drag_enter)
                widget.dnd_bind("<<DragLeave>>", self._on_drag_leave)
            self.dnd_active = True
        except Exception as exc:
            # The module imported fine (DND_AVAILABLE is about the Python
            # import succeeding), but the Tcl extension it needs did not
            # load into this Tk interpreter — different failure, same
            # underlying "no drop target" outcome. This used to be silently
            # swallowed, which left the headline inviting a drag that could
            # never do anything and gave no clue why. Now it's visible: the
            # invitation is withdrawn and the reason is logged, since a
            # blank Log tab was exactly what made this bug unreportable.
            self.drop_headline.configure(text="Choose what to convert")
            self.append_log(
                "[Info] Drag and drop could not be enabled on this system "
                f"({exc}). Use \"Choose files\" or \"Choose folder\" instead."
            )

    def _on_drag_enter(self, _event):
        if self.operation_state is OperationState.IDLE:
            self.dropzone.configure(fg_color=theme.DROP_ACTIVE,
                                    border_color=theme.ACCENT)

    def _on_drag_leave(self, _event):
        self.dropzone.configure(fg_color=theme.CARD,
                                border_color=theme.CARD_BORDER)

    def _on_drop(self, event):
        self._on_drag_leave(event)
        if self.operation_state is not OperationState.IDLE:
            return
        # Tk hands over a single string with brace-quoting around any path
        # containing spaces; splitlist is what understands that format.
        try:
            raw = self.tk.splitlist(event.data)
        except Exception:
            raw = [event.data]
        self._accept_paths([Path(item) for item in raw])

    # ----------------------------------------------------- file selection

    def _accept_paths(self, paths: list[Path]) -> None:
        """Validate a set of dropped or chosen paths and add them to the
        queue. Already-queued files are skipped rather than duplicated."""
        if self.operation_state is not OperationState.IDLE:
            return
        documents = ocr_service.collect_inputs(
            paths, recursive=bool(self.prefs.get("recursive")))
        if not documents:
            messagebox.showwarning(
                "Nothing usable",
                "No PDF, PNG, JPEG or WebP files were found in what you "
                "dropped.",
                parent=self)
            return

        accepted: list[Path] = []
        rejected: list[str] = []
        duplicates = 0
        for document in documents:
            if document.resolve() in self._queue:
                duplicates += 1
                continue
            try:
                ocr_service.validate_input_path(document)
            except ValueError as exc:
                rejected.append(f"{document.name}: {exc}")
            else:
                accepted.append(document)

        if not accepted:
            if rejected:
                messagebox.showwarning(
                    "Unsupported files", "\n\n".join(rejected[:5]), parent=self)
            elif duplicates:
                self.append_log(
                    f"[Info] {duplicates} file(s) were already in the queue.")
            return

        for document in accepted:
            self._add_to_queue(document)
        self._refresh_queue_view()

        if rejected:
            self.append_log(
                f"[Skipped] {len(rejected)} file(s) were not usable:")
            for line in rejected[:10]:
                self.append_log(f"          {line}")
        if duplicates:
            self.append_log(
                f"[Info] {duplicates} file(s) were already in the queue.")

    # ------------------------------------------------------------- queue

    def _add_to_queue(self, path: Path) -> None:
        key = path.resolve()
        self._queue[key] = path
        row = ctk.CTkFrame(self.queue_scroll, fg_color="transparent")
        row.grid_columnconfigure(0, weight=1)
        row.grid(row=len(self._queue_rows), column=0, sticky="ew", pady=2)
        ctk.CTkLabel(
            row, text=path.name, anchor="w",
            font=self._font("ui", theme.SIZE_SMALL), text_color=theme.TEXT,
        ).grid(row=0, column=0, sticky="ew", padx=(2, 8))
        ctk.CTkButton(
            row, text="✕", width=24, height=24,
            font=self._font("ui", theme.SIZE_SMALL),
            fg_color="transparent", border_width=0,
            text_color=theme.TEXT_MUTED, hover_color=theme.ACCENT_DIM,
            command=lambda k=key: self.remove_from_queue(k),
        ).grid(row=0, column=1, sticky="e")
        self._queue_rows[key] = row

    def remove_from_queue(self, key: Path) -> None:
        if self.operation_state is not OperationState.IDLE:
            return
        self._queue.pop(key, None)
        row = self._queue_rows.pop(key, None)
        if row is not None:
            row.destroy()
        for index, remaining_row in enumerate(self._queue_rows.values()):
            remaining_row.grid_configure(row=index)
        self._refresh_queue_view()

    def clear_queue(self) -> None:
        if self.operation_state is not OperationState.IDLE:
            return
        self._queue.clear()
        for row in self._queue_rows.values():
            row.destroy()
        self._queue_rows.clear()
        self._refresh_queue_view()

    def _refresh_queue_view(self) -> None:
        count = len(self._queue)
        if count == 0:
            self.queue_count_label.configure(text="")
            self.queue_clear_button.grid_remove()
            self.queue_scroll.grid_remove()
            self.queue_empty_label.grid()
        else:
            noun = "document" if count == 1 else "documents"
            self.queue_count_label.configure(text=f"{count} {noun} queued")
            self.queue_clear_button.grid()
            self.queue_empty_label.grid_remove()
            self.queue_scroll.grid()

    def select_file(self) -> None:
        if self.operation_state is not OperationState.IDLE:
            return
        names = filedialog.askopenfilenames(
            title="Select documents", filetypes=FILE_DIALOG_FILTERS, parent=self)
        if not names:
            return
        self._accept_paths([Path(name) for name in names])

    def select_folder(self) -> None:
        if self.operation_state is not OperationState.IDLE:
            return
        chosen = filedialog.askdirectory(
            title="Select a folder of documents", parent=self, mustexist=True)
        if not chosen:
            return
        self._accept_paths([Path(chosen)])

    # -------------------------------------------------------- preferences

    def _persist(self) -> None:
        user_settings.save({
            "model": self.model_combobox.get().strip(),
            "dpi": self._selected_dpi() or config.DEFAULT_DPI,
            "output_dir": str(self.output_dir) if self.output_dir else None,
            "appearance": "dark" if ctk.get_appearance_mode() == "Dark" else "light",
            "window": f"{self.winfo_width()}x{self.winfo_height()}",
            "recursive": bool(self.prefs.get("recursive")),
            "gpu_mode": self._gpu_mode(),
            "gpu_index": self._gpu_index(),
        })

    def _on_appearance_change(self, value: str) -> None:
        ctk.set_appearance_mode(value.lower())
        self._persist()

    def _selected_dpi(self) -> int | None:
        try:
            return int(self.dpi_combobox.get().split()[0])
        except (ValueError, IndexError):
            return None

    def _update_output_label(self) -> None:
        if self.output_dir is None:
            self.output_label.configure(text="Beside each input file",
                                        text_color=theme.TEXT_MUTED)
            self.output_reset_button.configure(state="disabled")
        else:
            self.output_label.configure(text=str(self.output_dir),
                                        text_color=theme.TEXT)
            self.output_reset_button.configure(state="normal")

    def choose_output_dir(self) -> None:
        if self.operation_state is not OperationState.IDLE:
            return
        chosen = filedialog.askdirectory(
            title="Save recognized text to", parent=self, mustexist=True)
        if not chosen:
            return
        path = Path(chosen)
        try:
            ocr_service.validate_output_dir(path)
        except ValueError as exc:
            messagebox.showerror("Cannot use that folder", str(exc), parent=self)
            return
        self.output_dir = path
        self._update_output_label()
        self._persist()

    def reset_output_dir(self) -> None:
        if self.operation_state is not OperationState.IDLE:
            return
        self.output_dir = None
        self._update_output_label()
        self._persist()

    # ---------------------------------------------------- event plumbing

    def drain_ui_events(self) -> None:
        if self.closing:
            return
        try:
            while True:
                try:
                    kind, payload = self.event_queue.get_nowait()
                except queue.Empty:
                    break
                self.handle_event(kind, payload)
        finally:
            self.after(config.UI_POLL_INTERVAL_MS, self.drain_ui_events)

    def handle_event(self, kind: str, payload) -> None:
        if kind == "log":
            self.append_log(payload)
        elif kind == "models_loaded":
            self.on_models_loaded(payload)
        elif kind == "refresh_error":
            self.on_refresh_error(payload)
        elif kind == "ocr_success":
            self.on_ocr_success(payload)
        elif kind == "progress":
            self.on_progress(payload)
        elif kind == "ocr_error":
            self.on_ocr_error(payload)
        elif kind == "ocr_cancelled":
            self.on_ocr_cancelled()
        elif kind == "page_text":
            self.on_page_text(payload)
        elif kind == "page_image":
            self.on_page_image(payload)
        elif kind == "stream_chunk":
            self.on_stream_chunk(payload)
        elif kind == "file_start":
            self.on_file_start(payload)
        elif kind == "file_done":
            self.on_file_done(payload)
        elif kind == "file_failed":
            self.on_file_failed(payload)
        elif kind == "batch_finished":
            self.on_batch_finished(payload)
        else:
            self.append_log(f"[Warn] Unhandled event kind: {kind!r}")

    def append_log(self, message: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", message + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def append_result(self, text: str) -> None:
        self.result_box.configure(state="normal")
        self.result_box.insert("end", text)
        self.result_box.see("end")
        self.result_box.configure(state="disabled")

    def _clear_result_panel(self) -> None:
        self.result_box.configure(state="normal")
        self.result_box.delete("1.0", "end")
        self.result_box.configure(state="disabled")
        self._stream_buffer = ""
        self._stream_flush_scheduled = False
        self._result_page = 0

    # ------------------------------------------------------- batch events

    def on_file_start(self, payload: dict) -> None:
        self._batch_index = payload["index"]
        self._batch_total = payload["total"]
        # Each document restarts the per-page view; the Result panel keeps
        # accumulating so a batch reads as one continuous transcript.
        self._result_page = 0
        self._review_total = 0
        if self._batch_total > 1:
            self.append_log(
                f"[File {payload['index']}/{payload['total']}] {payload['name']}")

    def on_file_done(self, payload: dict) -> None:
        if self._batch_total > 1:
            self.append_log(f"[Saved] {payload['output']}")

    def on_file_failed(self, payload: dict) -> None:
        self.append_log(f"[Failed] {payload['name']}: {payload['error']}")

    def on_batch_finished(self, payload: dict) -> None:
        self._flush_stream_buffer()
        self._restore_idle()
        completed = payload["completed"]
        failures = payload["failures"]
        total = len(completed) + len(failures)
        self.append_log(f"[Finished] {len(completed)} of {total} done.")
        if failures:
            self.tabview.set("Log")
            messagebox.showwarning(
                "Finished with problems",
                f"{len(completed)} document(s) converted.\n"
                f"{len(failures)} could not be processed — see the Log tab.",
                parent=self)
        elif completed:
            self.tabview.set("Result")
            self._show_completion_dialog(completed)
        if not failures:
            # Everything queued is done; leave the queue ready for the next
            # batch rather than making the user clear it themselves. A
            # partial failure leaves the queue untouched so nothing that
            # didn't finish disappears silently.
            self.clear_queue()

    # --------------------------------------------------------- page events

    def on_page_image(self, payload: dict) -> None:
        page = payload["page"]
        self._review_total = payload["total"]
        self.review_pages.setdefault(page, {"png": None, "text": None})["png"] = (
            payload["png"])

        image = Image.open(io.BytesIO(payload["png"]))
        width, height = image.size
        max_width = PREVIEW_WIDTH - 28
        scale = min(1.0, max_width / width)
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
        self._preview_image = ctk.CTkImage(
            light_image=image, dark_image=image, size=size)
        self.preview_image_label.configure(image=self._preview_image)
        self.preview_caption.configure(text=f"Page {page} of {payload['total']}")

    def _clear_preview(self) -> None:
        self.preview_image_label.configure(image=None)
        self._preview_image = None
        self.preview_caption.configure(text="Page preview")

    def on_progress(self, payload: dict) -> None:
        phase = payload["phase"]
        current = payload["current"]
        total = payload["total"]

        if phase == "render":
            self._render_phase_seen = True
            fraction = 0.2 * current / total
        else:
            fraction = (0.2 + 0.8 * current / total
                        if self._render_phase_seen else current / total)

        # In a batch, per-page progress is folded into this file's own slice
        # of the bar so it advances once across the whole run.
        if self._batch_total > 1:
            slice_size = 1.0 / self._batch_total
            fraction = (self._batch_index - 1) * slice_size + fraction * slice_size

        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress.set(fraction)

        if self._batch_total > 1:
            self.status_label.configure(
                text=f"File {self._batch_index} of {self._batch_total}  ·  "
                     f"page {current} of {total}")
        else:
            self.status_label.configure(text=f"Page {current} of {total}")

    def on_page_text(self, payload: dict) -> None:
        self._flush_stream_buffer()
        page = payload["page"]
        text = payload["text"]
        self._review_total = payload.get("total", self._review_total)
        self.review_pages.setdefault(page, {"png": None, "text": None})["text"] = text
        self._register_review_page(page)
        if page == self._result_page:
            return
        if self._result_page:
            self.append_result("\n\n")
        self._result_page = page
        self.append_result(text)

    def on_stream_chunk(self, payload: dict) -> None:
        page = payload["page"]
        if self._result_page and page != self._result_page:
            self._stream_buffer += "\n\n"
        self._result_page = page
        self._stream_buffer += payload["text"]
        if not self._stream_flush_scheduled:
            self._stream_flush_scheduled = True
            self.after(config.STREAM_UI_FLUSH_MS, self._flush_stream_buffer)

    def _flush_stream_buffer(self) -> None:
        if self._stream_buffer:
            self.append_result(self._stream_buffer)
            self._stream_buffer = ""
        self._stream_flush_scheduled = False

    def copy_result(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.result_box.get("1.0", "end-1c"))

    # -------------------------------------------------------- review tab

    def _clear_review(self) -> None:
        self.review_pages = {}
        self._review_order = []
        self._review_index = 0
        self._review_total = 0
        self._review_image_cache.clear()
        self.review_image_label.configure(image=None)
        self.review_text.configure(state="normal")
        self.review_text.delete("1.0", "end")
        self.review_text.configure(state="disabled")
        self.review_nav_label.configure(text="No pages yet")
        self.review_prev_button.configure(state="disabled")
        self.review_next_button.configure(state="disabled")

    def _register_review_page(self, page: int) -> None:
        if page in self._review_order:
            self._update_review_nav()
            return
        bisect.insort(self._review_order, page)
        if len(self._review_order) == 1:
            self.show_review_page(0)
        else:
            self._update_review_nav()

    def _review_image_for(self, page: int, png: bytes | None):
        if png is None:
            return None
        cached = self._review_image_cache.get(page)
        if cached is not None:
            self._review_image_cache.move_to_end(page)
            return cached
        image = Image.open(io.BytesIO(png))
        width, height = image.size
        scale = min(REVIEW_IMAGE_WIDTH / width, REVIEW_IMAGE_MAX_H / height, 1.0)
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
        ctk_image = ctk.CTkImage(light_image=image, dark_image=image, size=size)
        self._review_image_cache[page] = ctk_image
        while len(self._review_image_cache) > REVIEW_IMAGE_CACHE_SIZE:
            self._review_image_cache.popitem(last=False)
        return ctk_image

    def show_review_page(self, index: int) -> None:
        if not self._review_order:
            return
        index = max(0, min(index, len(self._review_order) - 1))
        self._review_index = index
        page = self._review_order[index]
        entry = self.review_pages.get(page, {})
        self.review_image_label.configure(
            image=self._review_image_for(page, entry.get("png")))
        self.review_text.configure(state="normal")
        self.review_text.delete("1.0", "end")
        self.review_text.insert("1.0", entry.get("text") or "")
        self.review_text.configure(state="disabled")
        self._update_review_nav()

    def _update_review_nav(self) -> None:
        ready = len(self._review_order)
        if ready == 0:
            self.review_nav_label.configure(text="No pages yet")
            self.review_prev_button.configure(state="disabled")
            self.review_next_button.configure(state="disabled")
            return
        page = self._review_order[self._review_index]
        document_total = self._review_total or ready
        self.review_nav_label.configure(text=f"Page {page} of {document_total}")
        self.review_prev_button.configure(
            state="normal" if self._review_index > 0 else "disabled")
        self.review_next_button.configure(
            state="normal" if self._review_index < ready - 1 else "disabled")

    def review_prev(self) -> None:
        self.show_review_page(self._review_index - 1)

    def review_next(self) -> None:
        self.show_review_page(self._review_index + 1)

    # ---------------------------------------------------- control states

    def _apply_refresh_busy_state(self) -> None:
        self.url_entry.configure(state="disabled")
        self.refresh_button.configure(state="disabled")
        self.start_button.configure(state="disabled")

    def _apply_ocr_busy_state(self) -> None:
        for widget in (self.select_button, self.select_folder_button,
                       self.url_entry, self.refresh_button, self.model_combobox,
                       self.output_choose_button, self.output_reset_button,
                       self.gpu_mode_segment):
            widget.configure(state="disabled")
        self.dpi_combobox.configure(state="disabled")
        self.gpu_index_entry.configure(state="disabled")
        self.start_button.configure(
            state="normal", text="Cancel", command=self.cancel_ocr)
        self._render_phase_seen = False
        self._clear_result_panel()
        self._clear_preview()
        self._clear_review()
        self.tabview.set("Log")
        self.status_label.configure(text="Starting…")
        self.status_frame.grid()
        self.progress.configure(mode="indeterminate")
        self.progress.start()

    def _restore_idle(self) -> None:
        for widget in (self.select_button, self.select_folder_button,
                       self.url_entry, self.refresh_button, self.model_combobox,
                       self.output_choose_button, self.gpu_mode_segment):
            widget.configure(state="normal")
        self.dpi_combobox.configure(state="readonly")
        self._on_gpu_mode_change(persist=False)  # re-sync gpu_index_entry state
        self.start_button.configure(
            state="normal", text="Start OCR", command=self.start_ocr)
        self.progress.stop()
        self.progress.configure(mode="indeterminate")
        self.progress.set(0)
        self.status_frame.grid_remove()
        self.status_label.configure(text="")
        self._render_phase_seen = False
        self._batch_index = 0
        self._batch_total = 0
        self._update_output_label()
        self.operation_state = OperationState.IDLE

    # ------------------------------------------------------ model refresh

    def refresh_models(self) -> None:
        if self.operation_state is not OperationState.IDLE:
            return
        try:
            url = ocr_service.normalize_ollama_url(self.url_entry.get())
        except ValueError as exc:
            messagebox.showerror("Ollama server URL", str(exc), parent=self)
            return
        self.operation_state = OperationState.REFRESHING_MODELS
        self._apply_refresh_busy_state()
        self.append_log(f"Refreshing model list from {url}...")
        threading.Thread(
            target=self._refresh_worker, args=(url,), daemon=True).start()

    def _refresh_worker(self, url: str) -> None:
        try:
            models = ocr_service.list_models(url)
        except Exception as exc:
            self.event_queue.put(("refresh_error", str(exc)))
        else:
            self.event_queue.put(("models_loaded", models))

    def on_models_loaded(self, models: list[str]) -> None:
        self._restore_idle()
        if not models:
            self.append_log(
                "No models found on the server; enter a model tag manually.")
            return
        typed = self.model_combobox.get().strip()
        self.model_combobox.configure(values=models)
        self.model_combobox.set(typed if typed else models[0])
        self.append_log(f"Found {len(models)} model(s).")
        self._persist()

    def on_refresh_error(self, message: str) -> None:
        self._restore_idle()
        self.append_log(f"[Error] {message}")
        messagebox.showerror("Model refresh failed", message, parent=self)

    # -------------------------------------------------------------- OCR

    def start_ocr(self) -> None:
        if self.operation_state is not OperationState.IDLE:
            return
        if not self.selected_paths:
            messagebox.showerror(
                "Nothing selected",
                "Choose or drop at least one document first.", parent=self)
            return

        for path in self.selected_paths:
            try:
                ocr_service.validate_input_path(path)
            except ValueError as exc:
                messagebox.showerror("Invalid file", str(exc), parent=self)
                return

        try:
            url = ocr_service.normalize_ollama_url(self.url_entry.get())
        except ValueError as exc:
            messagebox.showerror("Ollama server URL", str(exc), parent=self)
            return

        model = self.model_combobox.get().strip()
        if not model:
            messagebox.showerror(
                "No model", "Enter or select an Ollama model tag.", parent=self)
            return

        dpi = self._selected_dpi()
        if dpi not in config.DPI_OPTIONS:
            options = ", ".join(str(d) for d in config.DPI_OPTIONS)
            messagebox.showerror(
                "Invalid quality", f"DPI must be one of: {options}", parent=self)
            return

        if self.output_dir is not None:
            try:
                ocr_service.validate_output_dir(self.output_dir)
            except ValueError as exc:
                messagebox.showerror("Cannot use that folder", str(exc), parent=self)
                return

        gpu_mode = self._gpu_mode()
        gpu_index = self._gpu_index() if gpu_mode == config.GPU_MODE_GPU else None

        requests = [
            OCRRequest(
                input_path=path,
                output_path=ocr_service.build_output_path(path, self.output_dir),
                ollama_url=url, model=model, dpi=dpi,
                gpu_mode=gpu_mode, gpu_index=gpu_index,
            )
            for path in self.selected_paths
        ]

        existing = [r.output_path for r in requests if r.output_path.exists()]
        if existing:
            listed = "\n".join(str(p) for p in existing[:8])
            more = "" if len(existing) <= 8 else f"\n…and {len(existing) - 8} more"
            if not messagebox.askyesno(
                "Overwrite existing files?",
                f"{len(existing)} output file(s) already exist:\n\n{listed}{more}"
                "\n\nOverwrite them?", parent=self):
                return

        self.operation_state = OperationState.PROCESSING_OCR
        self.cancel_event = threading.Event()
        self._apply_ocr_busy_state()
        self._persist()
        self.append_log(
            f"[Start] {len(requests)} document(s) · model {model} · {dpi} DPI")
        threading.Thread(
            target=self._ocr_worker,
            args=(requests, self.cancel_event),
            daemon=True).start()

    def cancel_ocr(self) -> None:
        if self.operation_state is not OperationState.PROCESSING_OCR:
            return
        self.cancel_event.set()
        self.start_button.configure(state="disabled", text="Cancelling…")
        self.append_log("[Cancel] Stopping after the current page...")

    def _ocr_worker(self, requests: list[OCRRequest], cancel_event) -> None:
        cancelled = False
        outcome = None
        error: Exception | None = None
        try:
            outcome = ocr_service.process_batch(
                requests, self.event_queue, cancel_event)
        except ocr_service.OCRCancelled:
            cancelled = True
        except Exception as exc:
            error = exc
        if cancelled:
            self.event_queue.put(("ocr_cancelled", None))
        elif error is not None:
            self.event_queue.put(("ocr_error", str(error)))
        else:
            self.event_queue.put((
                "batch_finished",
                {"completed": [str(p) for p in outcome.completed],
                 "failures": [str(p) for p, _ in outcome.failures]},
            ))

    def on_ocr_success(self, saved_path: str) -> None:
        """Single-document terminal event; batches finish via batch_finished."""
        self._flush_stream_buffer()
        self._restore_idle()
        self.append_log(f"[Success] File saved: {saved_path}")
        self.tabview.set("Result")
        self._show_completion_dialog([saved_path])

    def on_ocr_cancelled(self) -> None:
        self._flush_stream_buffer()
        self._restore_idle()
        self.append_log("[Cancelled] Stopped by you. Nothing further was written.")
        self.tabview.set("Log")

    def on_ocr_error(self, message: str) -> None:
        self._flush_stream_buffer()
        self._restore_idle()
        self.append_log(f"[Error] {message}")
        self.tabview.set("Log")
        messagebox.showerror("OCR failed", message, parent=self)

    @staticmethod
    def _reveal_button_text() -> str:
        if sys.platform == "darwin":
            return "Show in Finder"
        if sys.platform.startswith("win"):
            return "Show in Explorer"
        return "Open folder"

    def _show_completion_dialog(self, saved_paths: list[str]) -> None:
        first = Path(saved_paths[0])
        dialog = ctk.CTkToplevel(self)
        dialog.title("Finished")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.configure(fg_color=theme.SURFACE)

        if len(saved_paths) == 1:
            headline, detail = "Saved", str(first)
        else:
            headline = f"{len(saved_paths)} documents saved"
            detail = f"in {first.parent}"

        ctk.CTkLabel(
            dialog, text=headline,
            font=self._font("heading", theme.SIZE_HEADING, "bold"),
        ).grid(row=0, column=0, columnspan=3, padx=PAD, pady=(PAD, 2), sticky="w")
        ctk.CTkLabel(
            dialog, text=detail, justify="left", wraplength=430,
            font=self._font("ui", theme.SIZE_SMALL), text_color=theme.TEXT_MUTED,
        ).grid(row=1, column=0, columnspan=3, padx=PAD, pady=(0, INNER), sticky="w")

        ctk.CTkButton(
            dialog, text="Open", width=104, height=34,
            font=self._font("ui", theme.SIZE_BODY),
            command=lambda: self._run_file_action(
                ocr_service.open_in_default_app, first),
        ).grid(row=2, column=0, padx=(PAD, 6), pady=(0, PAD))
        ctk.CTkButton(
            dialog, text=self._reveal_button_text(), width=132, height=34,
            font=self._font("ui", theme.SIZE_BODY),
            fg_color="transparent", border_width=1,
            border_color=theme.FIELD_BORDER, text_color=theme.TEXT,
            hover_color=theme.ACCENT_DIM,
            command=lambda: self._run_file_action(
                ocr_service.reveal_in_file_manager, first),
        ).grid(row=2, column=1, padx=6, pady=(0, PAD))
        ok_button = ctk.CTkButton(
            dialog, text="Done", width=84, height=34,
            font=self._font("ui", theme.SIZE_BODY),
            fg_color="transparent", border_width=1,
            border_color=theme.FIELD_BORDER, text_color=theme.TEXT_MUTED,
            hover_color=theme.ACCENT_DIM, command=dialog.destroy)
        ok_button.grid(row=2, column=2, padx=(6, PAD), pady=(0, PAD))

        dialog.after(50, dialog.lift)
        ok_button.focus_set()

    def _run_file_action(self, action, path: Path) -> None:
        try:
            action(path)
        except ocr_service.OCRServiceError as exc:
            self.append_log(f"[Error] {exc}")
            messagebox.showerror("Action failed", str(exc), parent=self)

    # ---------------------------------------------------------- shutdown

    def on_close(self) -> None:
        if self.closing or self.operation_state is OperationState.IDLE:
            self.closing = True
            self._persist()
            self.destroy()
            return
        if messagebox.askyesno(
            "Quit", "An operation is still running. Close anyway?", parent=self
        ):
            self.closing = True
            self.cancel_event.set()
            self._persist()
            self.destroy()
