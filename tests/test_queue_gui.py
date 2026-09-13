"""GUI smoke tests for the document queue: add, remove, clear, and how a
batch's outcome affects it afterwards.

These tests create a real ``LocalOCRApp`` window (withdrawn) so widget state
can be inspected. They are skipped automatically when no display is available
(CI, headless containers).
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module


class TestQueueGui(unittest.TestCase):
    def setUp(self):
        self._messagebox_patcher = mock.patch.object(app_module, "messagebox")
        self._messagebox_patcher.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
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

    def _pdf(self, name):
        path = self.dir / name
        path.write_bytes(b"%PDF-1.7 stub")
        return path

    # -- tests --------------------------------------------------------

    def test_multiple_accept_calls_append_rather_than_replace(self):
        """This is the actual "no way to build a queue" bug: selecting or
        dropping more files used to replace the list instead of adding to it."""
        a, b, c = self._pdf("a.pdf"), self._pdf("b.pdf"), self._pdf("c.pdf")
        self.app._accept_paths([a])
        self.app._accept_paths([b, c])
        self.assertEqual(
            [p.name for p in self.app.selected_paths], ["a.pdf", "b.pdf", "c.pdf"])
        self.assertEqual(len(self.app._queue_rows), 3)
        self.assertEqual(self.app.queue_count_label.cget("text"), "3 documents queued")

    def test_singular_count_label(self):
        self.app._accept_paths([self._pdf("a.pdf")])
        self.assertEqual(self.app.queue_count_label.cget("text"), "1 document queued")

    def test_dropping_the_same_file_twice_does_not_duplicate(self):
        a = self._pdf("a.pdf")
        self.app._accept_paths([a])
        self.app._accept_paths([a])
        self.assertEqual(len(self.app.selected_paths), 1)
        self.assertEqual(len(self.app._queue_rows), 1)

    def test_remove_one_leaves_the_others_queued(self):
        a, b = self._pdf("a.pdf"), self._pdf("b.pdf")
        self.app._accept_paths([a, b])
        self.app.remove_from_queue(a.resolve())
        self.assertEqual([p.name for p in self.app.selected_paths], ["b.pdf"])
        self.assertNotIn(a.resolve(), self.app._queue_rows)

    def test_clear_all_empties_the_queue_and_view(self):
        """This is the "no way to clear the current doc" bug."""
        self.app._accept_paths([self._pdf("a.pdf"), self._pdf("b.pdf")])
        self.app.clear_queue()
        self.assertEqual(self.app.selected_paths, [])
        self.assertEqual(self.app._queue_rows, {})
        self.assertEqual(self.app.queue_count_label.cget("text"), "")

    def test_empty_queue_shows_placeholder_not_the_list(self):
        self.assertEqual(self.app.queue_empty_label.cget("text"),
                          "Nothing selected yet")
        self.assertEqual(self.app.queue_scroll.grid_info(), {})

    def test_nonempty_queue_shows_list_not_the_placeholder(self):
        self.app._accept_paths([self._pdf("a.pdf")])
        self.assertEqual(self.app.queue_empty_label.grid_info(), {})
        self.assertNotEqual(self.app.queue_scroll.grid_info(), {})

    def test_busy_state_blocks_accept_remove_and_clear(self):
        a = self._pdf("a.pdf")
        self.app._accept_paths([a])
        self.app.operation_state = app_module.OperationState.PROCESSING_OCR
        self.app._accept_paths([self._pdf("b.pdf")])
        self.app.remove_from_queue(a.resolve())
        self.app.clear_queue()
        self.assertEqual([p.name for p in self.app.selected_paths], ["a.pdf"])

    def test_full_success_clears_the_queue(self):
        self.app._accept_paths([self._pdf("a.pdf")])
        self.app.on_batch_finished(
            {"completed": [str(self.dir / "a_extracted.md")], "failures": []})
        self.assertEqual(self.app.selected_paths, [])

    def test_partial_failure_leaves_the_queue_untouched(self):
        """Losing the whole queue because one file failed would be worse
        than doing nothing — the user can inspect the log and retry."""
        self.app._accept_paths([self._pdf("a.pdf"), self._pdf("b.pdf")])
        self.app.on_batch_finished({
            "completed": [str(self.dir / "a_extracted.md")],
            "failures": [(str(self.dir / "b.pdf"), "boom")],
        })
        self.assertEqual(len(self.app.selected_paths), 2)


if __name__ == "__main__":
    unittest.main()
