"""GUI smoke tests for the side-by-side Review tab.

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


class TestReviewGui(unittest.TestCase):
    def setUp(self):
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

    # -- helpers ----------------------------------------------------------

    def _feed(self, page, total, text, with_image=True):
        if with_image:
            self.app.on_page_image(
                {"page": page, "total": total, "png": png_bytes()}
            )
        self.app.on_page_text({"page": page, "total": total, "text": text})

    def _review_text(self):
        return self.app.review_text.get("1.0", "end-1c")

    def _label(self):
        return self.app.review_nav_label.cget("text")

    # -- tests ------------------------------------------------------------

    def test_first_page_shown_and_boundaries(self):
        self._feed(1, 3, "one")
        self.assertEqual(self._label(), "Page 1 of 3")
        self.assertEqual(self._review_text(), "one")
        # Only one page ready → both nav buttons disabled.
        self.assertEqual(self.app.review_prev_button.cget("state"), "disabled")
        self.assertEqual(self.app.review_next_button.cget("state"), "disabled")

    def test_navigation_across_pages(self):
        self._feed(1, 3, "one")
        self._feed(2, 3, "two")
        self._feed(3, 3, "three")
        # Still viewing page 1; next is now enabled, prev still disabled.
        self.assertEqual(self._label(), "Page 1 of 3")
        self.assertEqual(self.app.review_prev_button.cget("state"), "disabled")
        self.assertEqual(self.app.review_next_button.cget("state"), "normal")

        self.app.review_next()
        self.assertEqual(self._label(), "Page 2 of 3")
        self.assertEqual(self._review_text(), "two")
        self.assertEqual(self.app.review_prev_button.cget("state"), "normal")

        self.app.review_next()
        self.assertEqual(self._label(), "Page 3 of 3")
        self.assertEqual(self._review_text(), "three")
        self.assertEqual(self.app.review_next_button.cget("state"), "disabled")

        self.app.review_prev()
        self.assertEqual(self._label(), "Page 2 of 3")
        self.assertEqual(self._review_text(), "two")

    def test_navigation_does_not_overrun_boundaries(self):
        self._feed(1, 1, "only")
        self.app.review_prev()  # no-op at the left edge
        self.app.review_next()  # no-op at the right edge
        self.assertEqual(self._label(), "Page 1 of 1")
        self.assertEqual(self._review_text(), "only")

    def test_page_without_image_still_navigable(self):
        """A page whose thumbnail failed shows its text with a blank image."""
        self._feed(1, 1, "text only", with_image=False)
        self.assertEqual(self._label(), "Page 1 of 1")
        self.assertEqual(self._review_text(), "text only")

    def test_new_run_clears_review(self):
        self._feed(1, 2, "a")
        self._feed(2, 2, "b")
        self.app._apply_ocr_busy_state()
        self.assertEqual(self.app.review_pages, {})
        self.assertEqual(self._label(), "No pages yet")
        self.assertEqual(self._review_text(), "")
        self.assertEqual(self.app.review_prev_button.cget("state"), "disabled")
        self.assertEqual(self.app.review_next_button.cget("state"), "disabled")

    def test_image_cache_is_lru_bounded(self):
        for page in range(1, 9):
            self._feed(page, 8, f"p{page}")
            self.app.show_review_page(page - 1)  # visit to populate the cache
        self.assertLessEqual(
            len(self.app._review_image_cache), app_module.REVIEW_IMAGE_CACHE_SIZE
        )


if __name__ == "__main__":
    unittest.main()
