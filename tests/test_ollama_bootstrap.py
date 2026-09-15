"""Tests for ollama_bootstrap. No real Ollama server or frozen build needed."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ollama_bootstrap


class TestBinaryPathSearch(unittest.TestCase):
    """Ollama's real release layout is bin/ollama[.exe] next to a
    lib/ollama/ directory of shared libraries, not a flat ollama_bin/ollama
    — _binary_path() has to find it wherever it landed."""

    def test_finds_binary_nested_under_bin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bin").mkdir()
            (root / "bin" / "ollama").write_text("")
            (root / "lib" / "ollama").mkdir(parents=True)
            (root / "lib" / "ollama" / "libggml.so").write_text("")
            with mock.patch.object(ollama_bootstrap, "_bundled_binary_dir",
                                    return_value=root):
                found = ollama_bootstrap._binary_path()
        self.assertEqual(found, root / "bin" / "ollama")

    def test_no_binary_dir_returns_none(self):
        with mock.patch.object(ollama_bootstrap, "_bundled_binary_dir",
                                return_value=None):
            self.assertIsNone(ollama_bootstrap._binary_path())

    def test_binary_dir_missing_on_disk_returns_none(self):
        with mock.patch.object(ollama_bootstrap, "_bundled_binary_dir",
                                return_value=Path("/no/such/directory")):
            self.assertIsNone(ollama_bootstrap._binary_path())

    def test_no_matching_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ollama_bootstrap, "_bundled_binary_dir",
                                    return_value=Path(tmp)):
                self.assertIsNone(ollama_bootstrap._binary_path())


class TestNotFrozen(unittest.TestCase):
    """Running from source: nothing to bundle, so this must no-op cleanly
    without even checking whether a server is already running."""

    def test_binary_dir_is_none(self):
        with mock.patch.object(ollama_bootstrap.sys, "frozen", False, create=True):
            self.assertIsNone(ollama_bootstrap._bundled_binary_dir())

    def test_ensure_running_never_spawns_anything(self):
        with mock.patch.object(ollama_bootstrap.sys, "frozen", False, create=True), \
             mock.patch.object(ollama_bootstrap, "_already_running",
                                return_value=False) as already_running, \
             mock.patch.object(subprocess, "Popen") as popen:
            ollama_bootstrap.ensure_ollama_running("http://localhost:11434")
        popen.assert_not_called()


class TestAlreadyRunning(unittest.TestCase):
    def test_does_not_spawn_when_server_already_answers(self):
        with mock.patch.object(ollama_bootstrap, "_already_running",
                                return_value=True), \
             mock.patch.object(ollama_bootstrap, "_binary_path") as binary_path, \
             mock.patch.object(subprocess, "Popen") as popen:
            ollama_bootstrap.ensure_ollama_running("http://localhost:11434")
        binary_path.assert_not_called()
        popen.assert_not_called()


class TestNoBundledBinary(unittest.TestCase):
    def test_does_nothing_when_frozen_but_no_binary_present(self):
        with mock.patch.object(ollama_bootstrap, "_already_running",
                                return_value=False), \
             mock.patch.object(ollama_bootstrap, "_binary_path",
                                return_value=None), \
             mock.patch.object(subprocess, "Popen") as popen:
            ollama_bootstrap.ensure_ollama_running("http://localhost:11434")
        popen.assert_not_called()


class TestSpawning(unittest.TestCase):
    def test_spawns_bundled_binary_and_returns_once_it_answers(self):
        calls = {"n": 0}

        def fake_already_running(url):
            calls["n"] += 1
            return calls["n"] > 1  # not running, then running after spawn

        with mock.patch.object(ollama_bootstrap, "_already_running",
                                side_effect=fake_already_running), \
             mock.patch.object(ollama_bootstrap, "_binary_path",
                                return_value=ollama_bootstrap.Path("/opt/ollama")), \
             mock.patch.object(subprocess, "Popen") as popen, \
             mock.patch.object(ollama_bootstrap.time, "sleep"):
            ollama_bootstrap.ensure_ollama_running("http://localhost:11434")

        popen.assert_called_once()
        args, kwargs = popen.call_args
        self.assertEqual(args[0], ["/opt/ollama", "serve"])
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)

    def test_gives_up_soft_if_server_never_comes_up(self):
        deadlines = iter([0, 100])  # first check now, second past the timeout

        with mock.patch.object(ollama_bootstrap, "_already_running",
                                return_value=False), \
             mock.patch.object(ollama_bootstrap, "_binary_path",
                                return_value=ollama_bootstrap.Path("/opt/ollama")), \
             mock.patch.object(subprocess, "Popen"), \
             mock.patch.object(ollama_bootstrap.time, "sleep"), \
             mock.patch.object(ollama_bootstrap.time, "monotonic",
                                side_effect=lambda: next(deadlines)):
            ollama_bootstrap.ensure_ollama_running("http://localhost:11434")
        # No exception raised is the assertion here — a server that never
        # comes up must not crash the app.

    def test_popen_failure_is_swallowed(self):
        with mock.patch.object(ollama_bootstrap, "_already_running",
                                return_value=False), \
             mock.patch.object(ollama_bootstrap, "_binary_path",
                                return_value=ollama_bootstrap.Path("/opt/ollama")), \
             mock.patch.object(subprocess, "Popen",
                                side_effect=OSError("no such file")):
            ollama_bootstrap.ensure_ollama_running("http://localhost:11434")

    def test_windows_spawn_suppresses_console_window(self):
        calls = {"n": 0}

        def fake_already_running(url):
            calls["n"] += 1
            return calls["n"] > 1

        with mock.patch.object(ollama_bootstrap.sys, "platform", "win32"), \
             mock.patch.object(ollama_bootstrap, "_already_running",
                                side_effect=fake_already_running), \
             mock.patch.object(ollama_bootstrap, "_binary_path",
                                return_value=ollama_bootstrap.Path(
                                    "C:\\ollama\\ollama.exe")), \
             mock.patch.object(subprocess, "Popen") as popen, \
             mock.patch.object(subprocess, "CREATE_NO_WINDOW", 0x08000000,
                                create=True), \
             mock.patch.object(ollama_bootstrap.time, "sleep"):
            ollama_bootstrap.ensure_ollama_running("http://localhost:11434")
        popen.assert_called_once()
        _, kwargs = popen.call_args
        self.assertEqual(kwargs["creationflags"], 0x08000000)


class TestAlreadyRunningCheck(unittest.TestCase):
    def test_uses_the_same_verification_as_the_rest_of_the_app(self):
        with mock.patch.object(ollama_bootstrap.ocr_service, "make_client") as make_client, \
             mock.patch.object(ollama_bootstrap.ocr_service,
                                "verify_ollama_endpoint") as verify:
            verify.return_value = "0.1.0"
            self.assertTrue(ollama_bootstrap._already_running("http://localhost:11434"))
        make_client.assert_called_once()
        verify.assert_called_once()

    def test_any_failure_means_not_running(self):
        with mock.patch.object(ollama_bootstrap.ocr_service, "make_client",
                                side_effect=RuntimeError("boom")):
            self.assertFalse(ollama_bootstrap._already_running("http://localhost:11434"))


if __name__ == "__main__":
    unittest.main()
