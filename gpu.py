"""Local GPU detection for the Settings tab's card picker.

Tk-free so it can be tested headlessly, same rule as ocr_service.

Detection is best-effort and always fails soft: each vendor probe is only
attempted if its tool is on PATH, is given a short timeout, and any error
(missing tool, non-zero exit, timeout, garbled output) is swallowed and
treated as "nothing found" rather than raised. A machine with no supported
GPU tooling — or a CI container with none of this installed — just gets an
empty list, and the Settings tab falls back to manual index entry.

Every command below is a fixed argument list with no shell and no
user-supplied input, so there is nothing here for a crafted path or
filename to inject into.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

# Seconds. Each probe should fail fast rather than hang the detect worker.
DETECT_TIMEOUT = 5


@dataclass(frozen=True)
class GPU:
    index: int
    name: str
    vendor: str  # "nvidia" | "amd" | "apple"


def detect_gpus() -> list[GPU]:
    """Return whatever cards the first successful vendor probe finds.

    Tried in order: NVIDIA, then AMD, then Apple's integrated GPU. Stops at
    the first probe that finds anything — a machine has one GPU stack that
    matters to Ollama, not several to merge.
    """
    for probe in (_detect_nvidia, _detect_amd, _detect_apple):
        found = probe()
        if found:
            return found
    return []


def _run(cmd: list[str]) -> str | None:
    if shutil.which(cmd[0]) is None:
        return None
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=DETECT_TIMEOUT, check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return result.stdout


def _detect_nvidia() -> list[GPU]:
    output = _run(["nvidia-smi", "--query-gpu=index,name", "--format=csv,noheader"])
    if not output:
        return []
    gpus = []
    for line in output.splitlines():
        parts = [p.strip() for p in line.strip().split(",", 1)]
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        gpus.append(GPU(index=int(parts[0]), name=parts[1], vendor="nvidia"))
    return gpus


# rocm-smi's `--showproductname` prints one line per card like:
#   GPU[0]		: Card series: 	Radeon RX 6800 XT
_AMD_LINE_RE = re.compile(r"GPU\[(\d+)\]\s*:\s*Card series:\s*(.+)")


def _detect_amd() -> list[GPU]:
    output = _run(["rocm-smi", "--showproductname"])
    if not output:
        return []
    gpus = []
    for line in output.splitlines():
        match = _AMD_LINE_RE.search(line.strip())
        if match:
            gpus.append(GPU(
                index=int(match.group(1)), name=match.group(2).strip(),
                vendor="amd",
            ))
    return gpus


def _detect_apple() -> list[GPU]:
    # Apple Silicon's GPU is a single integrated part — there is no second
    # card to pick between, so this only ever reports index 0.
    if sys.platform != "darwin":
        return []
    output = _run(["system_profiler", "SPDisplaysDataType"])
    if not output:
        return []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("Chipset Model:"):
            return [GPU(index=0, name=line.split(":", 1)[1].strip(), vendor="apple")]
    return []
