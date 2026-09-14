"""GUI smoke tests for the deterministic progress bar.

These tests create a real ``LocalOCRApp`` window (withdrawn) so that
``CTkProgressBar.get()`` returns the actual widget state. They are skipped
automatically when no display is available (CI, headless containers).
"""

import unittest

import app as app_module


class TestProgressBarGui(unittest.TestCase):
    def setUp(self):
        try:
            self.app = app_module.LocalOCRApp()
            self.app.withdraw()
        except Exception:
            self.skipTest("No display available — skipping GUI smoke tests.")

    def tearDown(self):
        try:
            self.app.destroy()
        except Exception:
            pass

    # -- helpers ----------------------------------------------------------

    def _progress(self, phase, current, total):
        self.app.on_progress(
            {"phase": phase, "current": current, "total": total}
        )

    # -- tests ------------------------------------------------------------

    def test_render_phase_fraction(self):
        """render phase: fraction = 0.2 * current / total."""
        self._progress("render", 1, 4)
        self.assertEqual(self.app.progress.cget("mode"), "determinate")
        self.assertAlmostEqual(self.app.progress.get(), 0.2 * 1 / 4)

        self._progress("render", 3, 4)
        self.assertAlmostEqual(self.app.progress.get(), 0.2 * 3 / 4)

    def test_render_phase_label(self):
        self._progress("render", 2, 12)
        self.assertEqual(self.app.status_label.cget("text"), "Page 2 of 12")

    def test_ocr_phase_after_render(self):
        """ocr phase following a render phase: 0.2 + 0.8 * current / total."""
        self._progress("render", 3, 3)  # mark render phase as seen
        self._progress("ocr", 1, 3)
        self.assertEqual(self.app.progress.cget("mode"), "determinate")
        self.assertAlmostEqual(self.app.progress.get(), 0.2 + 0.8 * 1 / 3)

        self._progress("ocr", 3, 12)
        self.assertAlmostEqual(self.app.progress.get(), 0.2 + 0.8 * 3 / 12)

    def test_ocr_phase_without_render(self):
        """ocr phase without prior render (image): fraction = current / total."""
        self._progress("ocr", 1, 1)
        self.assertEqual(self.app.progress.cget("mode"), "determinate")
        self.assertAlmostEqual(self.app.progress.get(), 1.0)

        self._progress("ocr", 2, 5)
        self.assertAlmostEqual(self.app.progress.get(), 2 / 5)

    def test_ocr_phase_label(self):
        self._progress("ocr", 3, 12)
        self.assertEqual(self.app.status_label.cget("text"), "Page 3 of 12")

    def test_first_progress_stops_indeterminate(self):
        """_apply_ocr_busy_state starts indeterminate; first on_progress stops it."""
        self.app._apply_ocr_busy_state()
        self._progress("render", 1, 3)
        self.assertEqual(self.app.progress.cget("mode"), "determinate")

    def test_restore_idle_resets_to_indeterminate(self):
        self._progress("ocr", 2, 3)
        self.assertEqual(self.app.progress.cget("mode"), "determinate")
        self.app._restore_idle()
        self.assertEqual(self.app.progress.cget("mode"), "indeterminate")
        self.assertEqual(self.app.progress.get(), 0)
        self.assertEqual(self.app.status_label.cget("text"), "")

    def test_busy_state_collapses_the_drop_zone(self):
        self.assertNotEqual(self.app.dropzone.grid_info(), {})
        self.app._apply_ocr_busy_state()
        self.assertEqual(self.app.dropzone.grid_info(), {})

    def test_restore_idle_brings_back_the_drop_zone(self):
        self.app._apply_ocr_busy_state()
        self.assertEqual(self.app.dropzone.grid_info(), {})
        self.app._restore_idle()
        self.assertNotEqual(self.app.dropzone.grid_info(), {})

    def test_busy_state_resets_render_phase_flag(self):
        self._progress("render", 1, 2)  # sets _render_phase_seen = True
        self.app._restore_idle()
        self.app._apply_ocr_busy_state()
        # Without a render event, ocr should use current/total, not the
        # 0.2 + 0.8 * current/total formula.
        self._progress("ocr", 1, 4)
        self.assertAlmostEqual(self.app.progress.get(), 1 / 4)


if __name__ == "__main__":
    unittest.main()