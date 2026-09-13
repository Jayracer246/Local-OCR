"""GUI smoke tests for the GPU mode/index controls in the Settings tab.

These tests create a real ``LocalOCRApp`` window (withdrawn) so widget state
can be inspected. They are skipped automatically when no display is available
(CI, headless containers).
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
import config
import settings as user_settings


class TestGpuSettingsGui(unittest.TestCase):
    def setUp(self):
        self._messagebox_patcher = mock.patch.object(app_module, "messagebox")
        self._messagebox_patcher.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings_path = Path(self.tmp.name) / "settings.json"
        self._settings_patcher = mock.patch.object(
            app_module.user_settings, "settings_path",
            return_value=self.settings_path)
        self._settings_patcher.start()
        try:
            self.app = app_module.LocalOCRApp()
            self.app.withdraw()
        except Exception:
            self._messagebox_patcher.stop()
            self._settings_patcher.stop()
            self.skipTest("No display available — skipping GUI smoke tests.")

    def tearDown(self):
        try:
            self.app.destroy()
        except Exception:
            pass
        self._messagebox_patcher.stop()
        self._settings_patcher.stop()

    def test_defaults_to_auto_with_index_disabled(self):
        self.assertEqual(self.app.gpu_mode_segment.get(), "Auto")
        self.assertEqual(self.app.gpu_index_entry.cget("state"), "disabled")
        self.assertEqual(self.app._gpu_mode(), config.GPU_MODE_AUTO)

    def test_switching_to_gpu_enables_the_index_field(self):
        self.app.gpu_mode_segment.set("GPU")
        self.app._on_gpu_mode_change()
        self.assertEqual(self.app.gpu_index_entry.cget("state"), "normal")
        self.assertEqual(self.app._gpu_mode(), config.GPU_MODE_GPU)

    def test_switching_back_to_auto_disables_the_index_field(self):
        self.app.gpu_mode_segment.set("GPU")
        self.app._on_gpu_mode_change()
        self.app.gpu_mode_segment.set("Auto")
        self.app._on_gpu_mode_change()
        self.assertEqual(self.app.gpu_index_entry.cget("state"), "disabled")

    def test_gpu_index_parsing(self):
        self.app.gpu_mode_segment.set("GPU")
        self.app._on_gpu_mode_change()
        for text, expected in (
            ("", None), ("3", 3), ("not a number", None),
            ("-1", None), (str(config.MAX_GPU_INDEX + 1), None),
        ):
            with self.subTest(text=text):
                self.app.gpu_index_entry.delete(0, "end")
                self.app.gpu_index_entry.insert(0, text)
                self.assertEqual(self.app._gpu_index(), expected)

    def test_persist_saves_gpu_mode_and_index(self):
        self.app.gpu_mode_segment.set("GPU")
        self.app._on_gpu_mode_change()
        self.app.gpu_index_entry.delete(0, "end")
        self.app.gpu_index_entry.insert(0, "2")
        self.app._persist()
        saved = user_settings.load(self.settings_path)
        self.assertEqual(saved["gpu_mode"], "gpu")
        self.assertEqual(saved["gpu_index"], 2)

    def test_busy_state_disables_gpu_controls_and_restore_reverts(self):
        self.app.gpu_mode_segment.set("GPU")
        self.app._on_gpu_mode_change()
        self.app._apply_ocr_busy_state()
        self.assertEqual(self.app.gpu_mode_segment.cget("state"), "disabled")
        self.assertEqual(self.app.gpu_index_entry.cget("state"), "disabled")
        self.app._restore_idle()
        self.assertEqual(self.app.gpu_mode_segment.cget("state"), "normal")
        # Still in GPU mode after the run, so the index field re-enables.
        self.assertEqual(self.app.gpu_index_entry.cget("state"), "normal")

    def test_restore_idle_after_auto_mode_keeps_index_disabled(self):
        self.app._apply_ocr_busy_state()
        self.app._restore_idle()
        self.assertEqual(self.app.gpu_index_entry.cget("state"), "disabled")

    def test_start_ocr_ignores_index_outside_gpu_mode(self):
        """An index typed in while in Auto/CPU mode must not leak into the
        request — gpu_index only means anything in GPU mode."""
        self.app.gpu_mode_segment.set("CPU only")
        self.app._on_gpu_mode_change()
        pdf = Path(self.tmp.name) / "a.pdf"
        pdf.write_bytes(b"%PDF-1.7 stub")
        self.app._accept_paths([pdf])
        self.app.model_combobox.set("some-model")
        captured = {}

        def fake_thread(target, args, daemon):
            captured["requests"] = args[0]
            return mock.MagicMock()

        with mock.patch.object(app_module.threading, "Thread",
                                side_effect=fake_thread):
            self.app.start_ocr()
        request = captured["requests"][0]
        self.assertEqual(request.gpu_mode, config.GPU_MODE_CPU)
        self.assertIsNone(request.gpu_index)

    def test_start_ocr_carries_gpu_mode_and_index_into_every_request(self):
        self.app.gpu_mode_segment.set("GPU")
        self.app._on_gpu_mode_change()
        self.app.gpu_index_entry.delete(0, "end")
        self.app.gpu_index_entry.insert(0, "1")
        a = Path(self.tmp.name) / "a.pdf"
        b = Path(self.tmp.name) / "b.pdf"
        a.write_bytes(b"%PDF-1.7 stub")
        b.write_bytes(b"%PDF-1.7 stub")
        self.app._accept_paths([a, b])
        self.app.model_combobox.set("some-model")
        captured = {}

        def fake_thread(target, args, daemon):
            captured["requests"] = args[0]
            return mock.MagicMock()

        with mock.patch.object(app_module.threading, "Thread",
                                side_effect=fake_thread):
            self.app.start_ocr()
        for request in captured["requests"]:
            self.assertEqual(request.gpu_mode, config.GPU_MODE_GPU)
            self.assertEqual(request.gpu_index, 1)


if __name__ == "__main__":
    unittest.main()
