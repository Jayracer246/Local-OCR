"""GUI smoke tests for the current-page preview panel.

These tests create a real ``LocalOCRApp`` window (withdrawn) so widget state
can be inspected. They are skipped automatically when no display is available
(CI, headless containers).
"""

import io
import unittest
from unittest import mock

from PIL import Image

import app as app_module


def png_bytes(size=(120, 90), color=(200, 100, 50)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


class TestPreviewGui(unittest.TestCase):
    def setUp(self):
        # on_ocr_success pops a modal dialog that would block the run.
        self._messagebox_patcher = mock.patch.object(app_module, "messagebox")
        self._messagebox_patcher.start()
        try:
            self.app = app_module.LocalOCRApp()
            self.app.withdraw()
        except Exception:
            self._messagebox_patcher.stop()
            self.skipTest("No display available — skipping GUI smoke tests.")

    def tearDown(self):
        try:
            self.app.destroy()
        except Exception:
            pass
        self._messagebox_patcher.stop()

    # -- tests ------------------------------------------------------------

    def test_on_page_image_sets_image_and_caption(self):
        self.app.on_page_image({"page": 2, "total": 5, "png": png_bytes()})
        self.assertIsNotNone(self.app._preview_image)
        self.assertEqual(self.app.preview_caption.cget("text"), "Page 2 of 5")

    def test_busy_state_clears_preview(self):
        self.app.on_page_image({"page": 1, "total": 1, "png": png_bytes()})
        self.assertIsNotNone(self.app._preview_image)
        self.app._apply_ocr_busy_state()
        self.assertIsNone(self.app._preview_image)
        self.assertEqual(self.app.preview_caption.cget("text"), "Page preview")

    def test_preview_survives_completion(self):
        """The last page's preview stays after success, until the next run."""
        self.app.on_page_image({"page": 3, "total": 3, "png": png_bytes()})
        with mock.patch.object(self.app, "_show_completion_dialog"):
            self.app.on_ocr_success("/tmp/fake.md")
        self.assertIsNotNone(self.app._preview_image)


if __name__ == "__main__":
    unittest.main()
