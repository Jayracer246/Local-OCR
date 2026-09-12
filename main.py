"""Entry point for the Local OCR application."""

import customtkinter as ctk

import settings as user_settings
import theme
from app import LocalOCRApp


def main() -> None:
    prefs = user_settings.load()
    # Light by default: the warm off-white ground is the point of the palette.
    ctk.set_appearance_mode(prefs.get("appearance", "light"))
    ctk.set_default_color_theme(str(theme.write_theme_file()))
    app = LocalOCRApp()
    app.mainloop()


if __name__ == "__main__":
    main()
