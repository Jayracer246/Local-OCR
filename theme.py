"""Visual theme: palette, fonts, and the generated customtkinter theme file.

Kept free of application logic so it can be imported by tooling and tested
without constructing the app.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

# --------------------------------------------------------------- palette

# Anthropic's brand palette. The three that carry the identity are the warm
# off-white ground, the near-black ink, and the muted terracotta accent —
# get those right and the rest follows.
INK = "#141413"
CREAM = "#faf9f5"
MID_GRAY = "#b0aea5"
LIGHT_GRAY = "#e8e6dc"
ACCENT = "#d97757"
# Dark mode uses a deeper coral. Cream text on the light coral only reaches
# 2.96:1, which fails AA; at #ad5539 it is 4.82:1. Measured, not guessed.
ACCENT_DARK = "#ad5539"
ACCENT_PAIR = (ACCENT, ACCENT_DARK)
BLUE = "#6a9bcc"
GREEN = "#788c5d"

# Derived shades. customtkinter takes (light_mode, dark_mode) pairs for every
# colour, so each role below is defined once for both.
ACCENT_HOVER = ("#c96846", "#c1613f")
ACCENT_DIM = ("#efd9d0", "#4a2f26")

SURFACE = (CREAM, INK)                    # window ground
CARD = ("#ffffff", "#1e1e1c")             # raised grouping
CARD_BORDER = (LIGHT_GRAY, "#302f2c")
FIELD = ("#ffffff", "#26251f")
FIELD_BORDER = ("#dcd9cc", "#3a3833")

TEXT = (INK, CREAM)
TEXT_MUTED = ("#6b6a63", MID_GRAY)
# Ink on the light coral is 5.90:1 and cream on the dark coral is 4.82:1;
# white on either fails or barely scrapes large-text only.
TEXT_ON_ACCENT = (INK, CREAM)
TEXT_DISABLED = (MID_GRAY, "#5c5a52")

TRACK = (LIGHT_GRAY, "#2b2a26")           # progress bar trough
DROP_ACTIVE = ACCENT_DIM                  # drag-hover wash

# Corner radii. Claude's surfaces are rounded but restrained — large enough
# to read as soft, small enough not to look like a toy.
RADIUS_CARD = 12
RADIUS_CONTROL = 8
RADIUS_PILL = 999

# ----------------------------------------------------------------- fonts

# Ordered preferences. The first family Tk actually knows about wins, so a
# machine without Poppins or Lora installed still gets something deliberate
# rather than Tk's default bitmap font.
HEADING_STACK = ("Poppins", "Inter", "Cantarell", "Ubuntu", "DejaVu Sans",
                 "Liberation Sans", "Helvetica", "Arial")
BODY_STACK = ("Lora", "Georgia", "Noto Serif", "DejaVu Serif", "Times")
UI_STACK = ("Inter", "Cantarell", "Ubuntu", "Segoe UI", "Helvetica Neue",
            "DejaVu Sans", "Liberation Sans", "Arial")
MONO_STACK = ("JetBrains Mono", "Fira Mono", "Ubuntu Mono", "DejaVu Sans Mono",
              "Menlo", "Consolas", "Courier New")

_resolved: dict[str, str] = {}


def resolve_font(stack: tuple[str, ...], available: set[str] | None = None) -> str:
    """Return the first family in `stack` the system actually has.

    `available` is injectable so this is testable without a display. When
    omitted it is read from Tk, which must already have a root window.
    Falls back to the last entry in the stack rather than raising: a missing
    font should make the app look plainer, never stop it starting.
    """
    if available is None:
        try:
            import tkinter.font as tkfont

            available = {name.strip() for name in tkfont.families()}
        except Exception:
            return stack[-1]
    lowered = {name.lower(): name for name in available}
    for candidate in stack:
        if candidate.lower() in lowered:
            return candidate
    return stack[-1]


def fonts() -> dict[str, str]:
    """Resolve every font role once, after the Tk root exists."""
    global _resolved
    if not _resolved:
        _resolved = {
            "heading": resolve_font(HEADING_STACK),
            "body": resolve_font(BODY_STACK),
            "ui": resolve_font(UI_STACK),
            "mono": resolve_font(MONO_STACK),
        }
    return _resolved


# Type scale. Deliberately few steps — the hierarchy comes from weight and
# colour more than from size.
SIZE_TITLE = 22
SIZE_HEADING = 15
SIZE_BODY = 13
SIZE_SMALL = 11
SIZE_MONO = 12


# ------------------------------------------------------- generated theme


def build_theme() -> dict:
    """The customtkinter theme dict for this palette."""
    return {
        "CTk": {"fg_color": list(SURFACE)},
        "CTkToplevel": {"fg_color": list(SURFACE)},
        "CTkFrame": {
            "corner_radius": RADIUS_CARD,
            # 0 by default: transparent utility frames are used everywhere for
            # layout, and a themed border would draw a stray box around each
            # one. Cards opt into a border explicitly.
            "border_width": 0,
            "fg_color": list(CARD),
            "top_fg_color": list(CARD),
            "border_color": list(CARD_BORDER),
        },
        "CTkButton": {
            "corner_radius": RADIUS_CONTROL,
            "border_width": 0,
            "fg_color": list(ACCENT_PAIR),
            "hover_color": list(ACCENT_HOVER),
            "border_color": list(CARD_BORDER),
            "text_color": list(TEXT_ON_ACCENT),
            "text_color_disabled": list(TEXT_DISABLED),
        },
        "CTkLabel": {
            "corner_radius": 0,
            "border_width": 0,
            "fg_color": "transparent",
            "border_color": list(CARD_BORDER),
            "text_color": list(TEXT),
        },
        "CTkEntry": {
            "corner_radius": RADIUS_CONTROL,
            "border_width": 1,
            "fg_color": list(FIELD),
            "border_color": list(FIELD_BORDER),
            "text_color": list(TEXT),
            "placeholder_text_color": list(TEXT_MUTED),
        },
        "CTkCheckBox": {
            "corner_radius": 4,
            "border_width": 2,
            "fg_color": list(ACCENT_PAIR),
            "border_color": list(FIELD_BORDER),
            "hover_color": list(ACCENT_HOVER),
            "checkmark_color": list(TEXT_ON_ACCENT),
            "text_color": list(TEXT),
            "text_color_disabled": list(TEXT_DISABLED),
        },
        "CTkSwitch": {
            "corner_radius": RADIUS_PILL,
            "border_width": 0,
            "button_length": 0,
            "fg_color": list(TRACK),
            "progress_color": list(ACCENT_PAIR),
            "button_color": ["#ffffff", "#ffffff"],
            "button_hover_color": [LIGHT_GRAY, LIGHT_GRAY],
            "text_color": list(TEXT),
            "text_color_disabled": list(TEXT_DISABLED),
        },
        "CTkRadioButton": {
            "corner_radius": RADIUS_PILL,
            "border_width_checked": 5,
            "border_width_unchecked": 2,
            "fg_color": list(ACCENT_PAIR),
            "border_color": list(FIELD_BORDER),
            "hover_color": list(ACCENT_HOVER),
            "text_color": list(TEXT),
            "text_color_disabled": list(TEXT_DISABLED),
        },
        "CTkProgressBar": {
            "corner_radius": RADIUS_PILL,
            "border_width": 0,
            "fg_color": list(TRACK),
            "progress_color": list(ACCENT_PAIR),
            "border_color": list(CARD_BORDER),
        },
        "CTkSlider": {
            "corner_radius": RADIUS_PILL,
            "button_corner_radius": RADIUS_PILL,
            "border_width": 6,
            "button_length": 0,
            "fg_color": list(TRACK),
            "progress_color": list(ACCENT_PAIR),
            "button_color": list(ACCENT_PAIR),
            "button_hover_color": list(ACCENT_HOVER),
        },
        "CTkOptionMenu": {
            "corner_radius": RADIUS_CONTROL,
            "fg_color": list(FIELD),
            "button_color": list(FIELD_BORDER),
            "button_hover_color": list(ACCENT_DIM),
            "text_color": list(TEXT),
            "text_color_disabled": list(TEXT_DISABLED),
        },
        "CTkComboBox": {
            "corner_radius": RADIUS_CONTROL,
            "border_width": 1,
            "fg_color": list(FIELD),
            "border_color": list(FIELD_BORDER),
            "button_color": list(FIELD_BORDER),
            "button_hover_color": list(ACCENT_DIM),
            "text_color": list(TEXT),
            "text_color_disabled": list(TEXT_DISABLED),
        },
        "CTkScrollbar": {
            "corner_radius": RADIUS_PILL,
            "border_spacing": 4,
            "fg_color": "transparent",
            "button_color": list(TRACK),
            "button_hover_color": [MID_GRAY, MID_GRAY],
        },
        "CTkSegmentedButton": {
            "corner_radius": RADIUS_CONTROL,
            "border_width": 1,
            "fg_color": list(TRACK),
            "selected_color": list(ACCENT_PAIR),
            "selected_hover_color": list(ACCENT_HOVER),
            "unselected_color": list(TRACK),
            "unselected_hover_color": list(ACCENT_DIM),
            "text_color": list(TEXT),
            "text_color_disabled": list(TEXT_DISABLED),
        },
        "CTkTextbox": {
            "corner_radius": RADIUS_CONTROL,
            "border_width": 1,
            "fg_color": list(FIELD),
            "border_color": list(FIELD_BORDER),
            "text_color": list(TEXT),
            "scrollbar_button_color": list(TRACK),
            "scrollbar_button_hover_color": [MID_GRAY, MID_GRAY],
        },
        "CTkScrollableFrame": {"label_fg_color": list(CARD)},
        "DropdownMenu": {
            "fg_color": list(FIELD),
            "hover_color": list(ACCENT_DIM),
            "text_color": list(TEXT),
        },
        "CTkFont": {
            "macOS": {"family": "Helvetica Neue", "size": SIZE_BODY, "weight": "normal"},
            "Windows": {"family": "Segoe UI", "size": SIZE_BODY, "weight": "normal"},
            "Linux": {"family": "Sans", "size": SIZE_BODY, "weight": "normal"},
        },
    }


def write_theme_file(directory: Path | None = None) -> Path:
    """Write the theme JSON where customtkinter can load it.

    customtkinter only accepts a *path*, so the generated dict has to land on
    disk. It goes to a temp file rather than into the repo: it is derived
    entirely from the constants above, so committing it would just be a copy
    that can drift out of step with them.
    """
    target_dir = directory or Path(tempfile.mkdtemp(prefix="localocr_theme_"))
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / "claude.json"
    path.write_text(json.dumps(build_theme(), indent=2), encoding="utf-8")
    return path
