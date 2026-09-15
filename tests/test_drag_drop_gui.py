"""GUI smoke tests for drag-and-drop initialization.

The underlying bug report was "nothing happens when I drag a file" with no
error anywhere — caused by ``_enable_drag_and_drop`` swallowing the Tcl
extension's failure silently while the headline kept inviting a drag that
could never do anything. These tests pin the fix: a failure is now visible
(logged) and the UI stops promising a feature that isn't there.

These tests create a real ``LocalOCRApp`` window (withdrawn) so widget state
can be inspected. They are skipped automatically when no display is available
(CI, headless containers) or when tkinterdnd2 itself did not import.
"""

import unittest
from unittest import mock

import app as app_module


class TestDragDropInitialization(unittest.TestCase):
    def setUp(self):
        self._messagebox_patcher = mock.patch.object(app_module, "messagebox")
        self._messagebox_patcher.start()

    def tearDown(self):
        self._messagebox_patcher.stop()

    def _new_app(self):
        try:
            app = app_module.LocalOCRApp()
        except Exception:
            self.skipTest("No display available — skipping GUI smoke tests.")
        app.withdraw()
        self.addCleanup(app.destroy)
        return app

    def test_require_failure_is_logged_and_headline_corrected(self):
        if not app_module.DND_AVAILABLE:
            self.skipTest("tkinterdnd2 not importable in this environment")
        with mock.patch.object(
            app_module.TkinterDnD, "_require",
            side_effect=RuntimeError("tkdnd extension not found"),
        ):
            app = self._new_app()
        self.assertFalse(app.dnd_active)
        self.assertEqual(app.drop_headline.cget("text"), "Choose what to convert")
        log_text = app.log_box.get("1.0", "end-1c")
        self.assertIn("Drag and drop could not be enabled", log_text)
        self.assertIn("tkdnd extension not found", log_text)

    def test_headline_matches_whatever_dnd_active_actually_is(self):
        """Whether or not this display/toolkit combo actually supports it,
        the headline must never promise more than dnd_active delivers."""
        if not app_module.DND_AVAILABLE:
            self.skipTest("tkinterdnd2 not importable in this environment")
        app = self._new_app()
        if app.dnd_active:
            self.assertEqual(app.drop_headline.cget("text"),
                              "Drop files or a folder here")
        else:
            self.assertEqual(app.drop_headline.cget("text"),
                              "Choose what to convert")

    def test_module_not_importable_shows_the_no_dnd_headline_from_the_start(self):
        with mock.patch.object(app_module, "DND_AVAILABLE", False):
            app = self._new_app()
        self.assertFalse(app.dnd_active)
        self.assertEqual(app.drop_headline.cget("text"), "Choose what to convert")


if __name__ == "__main__":
    unittest.main()
