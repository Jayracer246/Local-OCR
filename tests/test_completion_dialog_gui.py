"""GUI smoke tests for the completion dialog and its file actions.

These tests create a real ``LocalOCRApp`` window (withdrawn) so widget state
can be inspected. They are skipped automatically when no display is available
(CI, headless containers).
"""

import unittest
from pathlib import Path
from unittest import mock

import customtkinter as ctk

import app as app_module
import ocr_service


class TestCompletionDialogGui(unittest.TestCase):
    def setUp(self):
        self._messagebox_patcher = mock.patch.object(app_module, "messagebox")
        self.messagebox = self._messagebox_patcher.start()
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

    def test_on_ocr_success_uses_dialog_not_messagebox(self):
        with mock.patch.object(self.app, "_show_completion_dialog") as dialog:
            self.app.on_ocr_success("/tmp/out.md")
        dialog.assert_called_once_with(["/tmp/out.md"])
        self.messagebox.showinfo.assert_not_called()

    def test_show_completion_dialog_builds_a_toplevel(self):
        before = self.app.winfo_children()
        self.app._show_completion_dialog(["/tmp/out.md"])
        toplevels = [
            w for w in self.app.winfo_children()
            if isinstance(w, ctk.CTkToplevel) and w not in before
        ]
        self.assertEqual(len(toplevels), 1)
        toplevels[0].destroy()

    def test_run_file_action_success_invokes_action(self):
        action = mock.Mock()
        self.app._run_file_action(action, Path("/tmp/out.md"))
        action.assert_called_once_with(Path("/tmp/out.md"))
        self.messagebox.showerror.assert_not_called()

    def test_run_file_action_failure_reports_error(self):
        def boom(_path):
            raise ocr_service.OCRServiceError("could not open")

        self.app._run_file_action(boom, Path("/tmp/out.md"))
        self.messagebox.showerror.assert_called_once()


if __name__ == "__main__":
    unittest.main()
