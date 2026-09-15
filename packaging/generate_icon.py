"""Generates the Nigma Local OCR app icon: a big white "N" cut into an
orange background with a drop shadow, and an "OCR" wordmark in white with
a black outline underneath.

Not part of the build — this is a one-off design tool, run by hand
whenever the icon needs to change. It writes packaging/icon.png (512,
general purpose / Linux .desktop Icon=), icon.ico (Windows, multi-res),
and icon.icns (macOS) — all three package builds already point at these.

    pip install pillow   # already in requirements.lock if you have the venv
    python packaging/generate_icon.py
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT_DIR = Path(__file__).parent

SIZE = 1024
ACCENT_TOP = (223, 137, 100)   # a touch lighter than theme.ACCENT, for gradient top
ACCENT_DARK = (173, 85, 57)    # theme.ACCENT_DARK  #ad5539
INK = (20, 20, 19)             # theme.INK  #141413
WHITE = (255, 255, 255)

CORNER_RADIUS = int(SIZE * 0.19)
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
ICNS_SIZES = [16, 32, 64, 128, 256, 512, 1024]

# A bold sans is all this needs — DejaVu Sans ships on virtually every
# Linux desktop and is already one of this app's own documented font
# fallbacks (see theme.py's HEADING_STACK). Add another candidate here if
# regenerating on a machine that doesn't have it.
BOLD_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",  # macOS fallback
    "C:\\Windows\\Fonts\\arialbd.ttf",                     # Windows fallback
]


def _find_bold_font() -> str:
    for candidate in BOLD_FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    raise FileNotFoundError(
        "No bold font found among: " + ", ".join(BOLD_FONT_CANDIDATES) +
        " — add the path to a bold TTF on this machine to BOLD_FONT_CANDIDATES."
    )


def _vertical_gradient(size: int, top: tuple, bottom: tuple) -> Image.Image:
    grad = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / (size - 1)
        grad.putpixel((0, y), tuple(
            round(top[c] + (bottom[c] - top[c]) * t) for c in range(3)
        ))
    return grad.resize((size, size))


def _rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def _clip_to_badge(image: Image.Image, mask: Image.Image) -> None:
    """Re-applies the rounded-badge mask to an image's alpha channel — used
    after drawing anything that might have bled past the badge edge (the
    oversized N's "cutting through" overflow, or OCR's stroke outline)."""
    image.putalpha(Image.composite(
        image.split()[3], Image.new("L", image.size, 0), mask))


def build_master() -> Image.Image:
    font_path = _find_bold_font()
    mask = _rounded_mask(SIZE, CORNER_RADIUS)

    base = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    base.paste(_vertical_gradient(SIZE, ACCENT_TOP, ACCENT_DARK), (0, 0), mask)

    # Subtle inner vignette for depth.
    vignette = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(vignette).ellipse(
        [-SIZE * 0.3, -SIZE * 0.3, SIZE * 1.3, SIZE * 1.3], fill=40)
    vignette = vignette.filter(ImageFilter.GaussianBlur(SIZE * 0.12))
    dark_overlay = Image.new("RGBA", (SIZE, SIZE), ACCENT_DARK + (0,))
    dark_overlay.putalpha(vignette.point(lambda p: int(p * 0.5)))
    base = Image.alpha_composite(base, dark_overlay)

    # ---- big N, with a drop shadow behind it ----
    n_font = ImageFont.truetype(font_path, int(SIZE * 0.95))
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    bbox = probe.textbbox((0, 0), "N", font=n_font)
    n_w, n_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    n_x = (SIZE - n_w) / 2 - bbox[0]
    n_y = SIZE * 0.36 - n_h / 2 - bbox[1]

    shadow_offset = int(SIZE * 0.018)
    shadow_layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(shadow_layer).text(
        (n_x + shadow_offset, n_y + shadow_offset * 1.6), "N",
        font=n_font, fill=(0, 0, 0, 150))
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(SIZE * 0.014))
    base = Image.alpha_composite(base, shadow_layer)

    n_layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(n_layer).text((n_x, n_y), "N", font=n_font, fill=WHITE + (255,))
    base = Image.alpha_composite(base, n_layer)
    _clip_to_badge(base, mask)

    # ---- OCR wordmark: white fill, black outline ----
    ocr_font = ImageFont.truetype(font_path, int(SIZE * 0.165))
    obbox = probe.textbbox((0, 0), "OCR", font=ocr_font)
    o_w, o_h = obbox[2] - obbox[0], obbox[3] - obbox[1]
    o_x = (SIZE - o_w) / 2 - obbox[0]
    o_y = SIZE * 0.855 - o_h / 2 - obbox[1]

    ocr_layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(ocr_layer).text(
        (o_x, o_y), "OCR", font=ocr_font, fill=WHITE + (255,),
        stroke_width=max(2, int(SIZE * 0.013)), stroke_fill=INK + (255,))
    base = Image.alpha_composite(base, ocr_layer)
    _clip_to_badge(base, mask)

    return base


def main() -> None:
    master = build_master()
    master.resize((512, 512), Image.LANCZOS).save(OUT_DIR / "icon.png")
    master.save(OUT_DIR / "icon.ico", sizes=[(s, s) for s in ICO_SIZES])
    master.save(OUT_DIR / "icon.icns", sizes=[(s, s) for s in ICNS_SIZES])
    print(f"Wrote icon.png, icon.ico, icon.icns to {OUT_DIR}")


if __name__ == "__main__":
    main()
