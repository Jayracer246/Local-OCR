"""Tests for gpu.py. No real GPU or vendor tooling required."""

import subprocess
import unittest
from unittest import mock

import gpu


def _which(present: set):
    return lambda cmd: f"/usr/bin/{cmd}" if cmd in present else None


class TestDetectNvidia(unittest.TestCase):
    def test_two_cards_parsed(self):
        with mock.patch.object(gpu.shutil, "which", _which({"nvidia-smi"})), \
             mock.patch.object(gpu.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout="0, NVIDIA GeForce RTX 3080\n1, NVIDIA GeForce RTX 3060\n",
            )
            result = gpu.detect_gpus()
        self.assertEqual(result, [
            gpu.GPU(index=0, name="NVIDIA GeForce RTX 3080", vendor="nvidia"),
            gpu.GPU(index=1, name="NVIDIA GeForce RTX 3060", vendor="nvidia"),
        ])

    def test_malformed_line_skipped(self):
        with mock.patch.object(gpu.shutil, "which", _which({"nvidia-smi"})), \
             mock.patch.object(gpu.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout="not a valid line\n0, NVIDIA GeForce RTX 3080\n\n",
            )
            result = gpu.detect_gpus()
        self.assertEqual(result, [
            gpu.GPU(index=0, name="NVIDIA GeForce RTX 3080", vendor="nvidia"),
        ])

    def test_empty_output_falls_through(self):
        with mock.patch.object(gpu.shutil, "which", _which({"nvidia-smi"})), \
             mock.patch.object(gpu.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="",
            )
            result = gpu.detect_gpus()
        self.assertEqual(result, [])


class TestDetectAmd(unittest.TestCase):
    def test_falls_back_from_missing_nvidia(self):
        with mock.patch.object(gpu.shutil, "which", _which({"rocm-smi"})), \
             mock.patch.object(gpu.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=(
                    "========ROCm System Management Interface========\n"
                    "GPU[0]\t\t: Card series: \tRadeon RX 6800 XT\n"
                ),
            )
            result = gpu.detect_gpus()
        self.assertEqual(result, [
            gpu.GPU(index=0, name="Radeon RX 6800 XT", vendor="amd"),
        ])


class TestDetectApple(unittest.TestCase):
    def test_reports_single_integrated_gpu_on_macos(self):
        with mock.patch.object(gpu.sys, "platform", "darwin"), \
             mock.patch.object(gpu.shutil, "which", _which({"system_profiler"})), \
             mock.patch.object(gpu.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout="Graphics/Displays:\n    Chipset Model: Apple M2 Pro\n",
            )
            result = gpu.detect_gpus()
        self.assertEqual(result, [
            gpu.GPU(index=0, name="Apple M2 Pro", vendor="apple"),
        ])

    def test_never_probed_off_macos(self):
        with mock.patch.object(gpu.sys, "platform", "linux"), \
             mock.patch.object(gpu.shutil, "which") as which:
            result = gpu._detect_apple()
        which.assert_not_called()
        self.assertEqual(result, [])


class TestFailsSoft(unittest.TestCase):
    def test_no_tooling_at_all_returns_empty(self):
        with mock.patch.object(gpu.shutil, "which", _which(set())):
            self.assertEqual(gpu.detect_gpus(), [])

    def test_timeout_treated_as_not_found(self):
        with mock.patch.object(gpu.shutil, "which", _which({"nvidia-smi"})), \
             mock.patch.object(gpu.subprocess, "run") as run:
            run.side_effect = subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=5)
            self.assertEqual(gpu._detect_nvidia(), [])

    def test_nonzero_exit_treated_as_not_found(self):
        with mock.patch.object(gpu.shutil, "which", _which({"nvidia-smi"})), \
             mock.patch.object(gpu.subprocess, "run") as run:
            run.side_effect = subprocess.CalledProcessError(1, "nvidia-smi")
            self.assertEqual(gpu._detect_nvidia(), [])

    def test_first_successful_probe_wins(self):
        with mock.patch.object(gpu.shutil, "which", _which({"nvidia-smi", "rocm-smi"})), \
             mock.patch.object(gpu.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="0, NVIDIA GeForce RTX 3080\n",
            )
            result = gpu.detect_gpus()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].vendor, "nvidia")


if __name__ == "__main__":
    unittest.main()
