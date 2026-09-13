"""Tests for settings persistence, batch OCR, and theme resolution.

Headless: nothing here needs a display.
"""

import json
import os
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import config
import ocr_service
import settings as user_settings
import theme
from ocr_service import OCRRequest, OCRServiceError


def stream_response(*deltas):
    return iter([SimpleNamespace(message=SimpleNamespace(content=d)) for d in deltas])


class TestSettingsRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "settings.json"

    def test_round_trip(self):
        out = Path(self.tmp.name) / "out"
        out.mkdir()
        values = {
            "model": "glm-ocr:latest", "dpi": 300, "output_dir": str(out),
            "appearance": "dark", "window": "1024x768", "recursive": True,
            "gpu_mode": "gpu", "gpu_index": 1,
        }
        self.assertTrue(user_settings.save(values, self.path))
        self.assertEqual(user_settings.load(self.path), values)

    def test_missing_file_gives_defaults(self):
        self.assertEqual(user_settings.load(self.path), user_settings.DEFAULTS)

    def test_corrupt_file_gives_defaults_rather_than_raising(self):
        for junk in ("", "{", "not json at all", "[1,2,3]", "null"):
            with self.subTest(content=junk[:12]):
                self.path.write_text(junk)
                self.assertEqual(user_settings.load(self.path),
                                 user_settings.DEFAULTS)

    def test_hostile_values_are_rejected_not_trusted(self):
        """The settings file is user-writable data, so nothing in it is trusted."""
        self.path.write_text(json.dumps({
            "dpi": 99999,                        # not an offered option
            "appearance": "rainbow",             # not a mode
            "window": "99999999x1; rm -rf /",    # not a geometry
            "output_dir": "/nonexistent/nope",   # gone since it was saved
            "model": "x" * 5000,                 # absurd length
            "recursive": "yes please",           # wrong type
            "gpu_mode": "quantum",               # not a real mode
            "gpu_index": 99999,                  # not a plausible card
        }))
        loaded = user_settings.load(self.path)
        self.assertEqual(loaded["dpi"], config.DEFAULT_DPI)
        self.assertEqual(loaded["appearance"], "light")
        self.assertIsNone(loaded["window"])
        self.assertIsNone(loaded["output_dir"])
        self.assertEqual(loaded["model"], "")
        self.assertIs(loaded["recursive"], False)
        self.assertEqual(loaded["gpu_mode"], config.GPU_MODE_AUTO)
        self.assertIsNone(loaded["gpu_index"])

    def test_gpu_settings_round_trip_and_reject_bad_values(self):
        for mode in config.GPU_MODE_OPTIONS:
            with self.subTest(mode=mode):
                user_settings.save({"gpu_mode": mode, "gpu_index": 2}, self.path)
                loaded = user_settings.load(self.path)
                self.assertEqual(loaded["gpu_mode"], mode)
                self.assertEqual(loaded["gpu_index"], 2)

        # A bool is technically an int in Python, but "true" is never a
        # sensible GPU index — it must not silently become index 1.
        self.path.write_text(json.dumps({"gpu_mode": "gpu", "gpu_index": True}))
        self.assertIsNone(user_settings.load(self.path)["gpu_index"])

        # Out of range and wrong type both fall back to None.
        for bad in (-1, config.MAX_GPU_INDEX + 1, "0", 3.5, None):
            with self.subTest(bad=bad):
                self.path.write_text(json.dumps({"gpu_mode": "gpu", "gpu_index": bad}))
                self.assertIsNone(user_settings.load(self.path)["gpu_index"])

    def test_no_server_url_is_ever_persisted(self):
        """Editing this file must not be able to redirect the app off-box."""
        self.path.write_text(json.dumps({
            "ollama_url": "http://evil.example.com:11434",
            "url": "http://evil.example.com:11434",
        }))
        loaded = user_settings.load(self.path)
        self.assertNotIn("ollama_url", loaded)
        self.assertNotIn("url", loaded)
        self.assertEqual(set(loaded), set(user_settings.DEFAULTS))

    def test_stale_output_dir_falls_back_to_default(self):
        gone = Path(self.tmp.name) / "was-a-usb-stick"
        gone.mkdir()
        user_settings.save({"output_dir": str(gone)}, self.path)
        gone.rmdir()                              # unmounted since
        self.assertIsNone(user_settings.load(self.path)["output_dir"])

    def test_save_failure_returns_false_instead_of_raising(self):
        unwritable = Path("/proc/nope/settings.json")
        self.assertFalse(user_settings.save({"dpi": 150}, unwritable))

    def test_config_dir_follows_xdg_on_linux(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/tmp/xdg-here"}):
            with mock.patch.object(user_settings.sys, "platform", "linux"):
                self.assertEqual(user_settings.config_dir(),
                                 Path("/tmp/xdg-here/local-ocr"))


class TestCollectInputs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _touch(self, relative):
        path = self.dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.7 stub" if path.suffix == ".pdf" else b"x")
        return path

    def test_folder_yields_supported_files_only(self):
        self._touch("a.pdf"); self._touch("b.png")
        self._touch("notes.txt"); self._touch("archive.zip")
        found = ocr_service.collect_inputs([self.dir])
        self.assertEqual([p.name for p in found], ["a.pdf", "b.png"])

    def test_not_recursive_by_default(self):
        self._touch("top.pdf"); self._touch("nested/deep.pdf")
        flat = ocr_service.collect_inputs([self.dir])
        deep = ocr_service.collect_inputs([self.dir], recursive=True)
        self.assertEqual([p.name for p in flat], ["top.pdf"])
        self.assertEqual([p.name for p in deep], ["deep.pdf", "top.pdf"])

    def test_duplicates_removed_when_folder_and_file_both_given(self):
        one = self._touch("one.pdf")
        found = ocr_service.collect_inputs([self.dir, one, one])
        self.assertEqual(len(found), 1)

    def test_symlinked_directory_loop_does_not_hang(self):
        self._touch("real.pdf")
        try:
            (self.dir / "loop").symlink_to(self.dir, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        found = ocr_service.collect_inputs([self.dir], recursive=True)
        self.assertEqual([p.name for p in found], ["real.pdf"])

    def test_missing_and_unsupported_paths_are_ignored(self):
        self.assertEqual(
            ocr_service.collect_inputs(
                [self.dir / "ghost.pdf", self._touch("notes.txt")]),
            [])

    def test_results_are_sorted_and_stable(self):
        for name in ("c.pdf", "A.png", "b.jpeg"):
            self._touch(name)
        names = [p.name for p in ocr_service.collect_inputs([self.dir])]
        self.assertEqual(names, sorted(names, key=str.lower))


class TestProcessBatch(unittest.TestCase):
    URL = "http://localhost:11434"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.sample = Path("examples/sample-5-page-pdf-a4-size.pdf")

    def _request(self, name):
        source = self.dir / name
        source.write_bytes(self.sample.read_bytes())
        return OCRRequest(
            input_path=source,
            output_path=ocr_service.build_output_path(source),
            ollama_url=self.URL, model="m", dpi=72)

    def _run(self, requests, chat_side_effect, cancel=None):
        events: queue.Queue = queue.Queue()
        with mock.patch.object(ocr_service.ollama, "Client") as client_cls:
            response = mock.MagicMock()
            response.status_code = 200
            response.json.return_value = {"version": "0.0.0-test"}
            client_cls.return_value._client.get.return_value = response
            client_cls.return_value.chat.side_effect = chat_side_effect
            outcome = ocr_service.process_batch(requests, events, cancel)
        drained = []
        while True:
            try:
                drained.append(events.get_nowait())
            except queue.Empty:
                break
        return outcome, drained

    def test_all_succeed(self):
        requests = [self._request(f"doc{i}.pdf") for i in range(3)]
        outcome, events = self._run(
            requests, lambda *a, **k: stream_response("page text"))
        self.assertEqual(len(outcome.completed), 3)
        self.assertEqual(outcome.failures, [])
        self.assertEqual(outcome.total, 3)
        for request in requests:
            self.assertTrue(request.output_path.exists())
        kinds = [k for k, _ in events]
        self.assertEqual(kinds.count("file_start"), 3)
        self.assertEqual(kinds.count("file_done"), 3)

    def test_one_bad_document_does_not_abort_the_rest(self):
        """Losing finished work because a later file is malformed is wrong."""
        requests = [self._request(f"doc{i}.pdf") for i in range(3)]
        requests[1].input_path.write_bytes(b"%PDF-1.7 truncated garbage")
        outcome, events = self._run(
            requests, lambda *a, **k: stream_response("ok"))
        self.assertEqual(len(outcome.completed), 2)
        self.assertEqual(len(outcome.failures), 1)
        self.assertEqual(outcome.failures[0][0].name, "doc1.pdf")
        self.assertIn("file_failed", [k for k, _ in events])
        # The third file still ran and still produced output.
        self.assertTrue(requests[2].output_path.exists())

    def test_cancel_stops_the_whole_batch(self):
        requests = [self._request(f"doc{i}.pdf") for i in range(5)]
        cancel = threading.Event()
        started = []

        def chat(*_a, **_kw):
            started.append(1)
            if len(started) == 2:
                cancel.set()
            return stream_response("text")

        with self.assertRaises(ocr_service.OCRCancelled):
            self._run(requests, chat, cancel)
        # Stopped early rather than grinding through all five.
        self.assertLess(len(started), 5)

    def test_file_events_carry_index_and_total(self):
        requests = [self._request(f"doc{i}.pdf") for i in range(2)]
        _outcome, events = self._run(
            requests, lambda *a, **k: stream_response("t"))
        starts = [p for k, p in events if k == "file_start"]
        self.assertEqual([(p["index"], p["total"]) for p in starts],
                         [(1, 2), (2, 2)])
        self.assertEqual([p["name"] for p in starts], ["doc0.pdf", "doc1.pdf"])

    def test_empty_batch_is_a_no_op(self):
        outcome, events = self._run([], lambda *a, **k: stream_response("t"))
        self.assertEqual(outcome.total, 0)
        self.assertEqual(events, [])


class TestThemeFonts(unittest.TestCase):
    def test_first_available_family_wins(self):
        available = {"DejaVu Sans", "Liberation Sans"}
        self.assertEqual(
            theme.resolve_font(("Poppins", "DejaVu Sans", "Arial"), available),
            "DejaVu Sans")

    def test_preferred_family_wins_when_present(self):
        self.assertEqual(
            theme.resolve_font(("Poppins", "DejaVu Sans"), {"Poppins", "DejaVu Sans"}),
            "Poppins")

    def test_matching_is_case_insensitive(self):
        self.assertEqual(
            theme.resolve_font(("Poppins",), {"poppins"}), "Poppins")

    def test_falls_back_to_last_entry_when_nothing_matches(self):
        """A machine with none of the preferred fonts still gets something."""
        self.assertEqual(
            theme.resolve_font(("Poppins", "Inter", "Arial"), set()), "Arial")

    def test_every_stack_has_a_plausible_last_resort(self):
        for stack in (theme.HEADING_STACK, theme.BODY_STACK,
                      theme.UI_STACK, theme.MONO_STACK):
            with self.subTest(stack=stack[0]):
                self.assertTrue(stack[-1])

    def test_theme_file_is_valid_json_with_every_widget_class(self):
        path = theme.write_theme_file()
        data = json.loads(path.read_text())
        for widget in ("CTk", "CTkButton", "CTkFrame", "CTkTextbox",
                       "CTkProgressBar", "CTkSegmentedButton", "CTkComboBox"):
            self.assertIn(widget, data)

    def test_accent_text_meets_wcag_aa_in_both_modes(self):
        """Measured, because white-on-coral silently failed at 3.12:1."""
        def luminance(value):
            channels = [int(value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
                      for c in channels]
            return (0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2])

        def ratio(a, b):
            la, lb = luminance(a), luminance(b)
            return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)

        light_text, dark_text = theme.TEXT_ON_ACCENT
        light_bg, dark_bg = theme.ACCENT_PAIR
        self.assertGreaterEqual(ratio(light_text, light_bg), 4.5)
        self.assertGreaterEqual(ratio(dark_text, dark_bg), 4.5)


if __name__ == "__main__":
    unittest.main()
