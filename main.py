"""Entry point for the Local OCR application."""

import customtkinter as ctk

import ollama_bootstrap
import settings as user_settings
import theme
from app import LocalOCRApp


def main() -> None:
    prefs = user_settings.load()
    # Best-effort, packaged builds only — see ollama_bootstrap's docstring.
    # Never allowed to stop the app from opening even if something here
    # goes wrong in a way that module's own soft-failure handling missed.
    try:
        ollama_bootstrap.ensure_ollama_running()
    except Exception:
        pass
    # Light by default: the warm off-white ground is the point of the palette.
    ctk.set_appearance_mode(prefs.get("appearance", "light"))
    ctk.set_default_color_theme(str(theme.write_theme_file()))
    app = LocalOCRApp()
    app.mainloop()


if __name__ == "__main__":
    main()
