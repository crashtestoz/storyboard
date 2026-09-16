"""Best-effort description of the machine this server runs on.

Exists for two consumers: ``/api/info`` (and so ``sbv_info`` over MCP), where
it is just informational, and the Storyboard AD assistant, which needs
*something* concrete to reason from when asked how long a render will take
rather than guessing. Every probe degrades to a shorter description instead
of failing the request that asked for it — this is context, not a
precondition for anything to work.
"""

from __future__ import annotations

import functools
import os
import platform
import subprocess


def _sysctl(name: str) -> str:
    try:
        out = subprocess.run(
            ["sysctl", "-n", name], capture_output=True, text=True, timeout=2
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:  # noqa: BLE001 — hardware info is never worth failing over
        return ""


@functools.lru_cache(maxsize=1)
def describe_hardware() -> dict:
    """CPU/memory summary of this machine. Cached: it does not change at runtime."""
    system = platform.system()
    chip = ""
    memory_gb = 0

    if system == "Darwin":
        chip = _sysctl("machdep.cpu.brand_string") or _sysctl("hw.model")
        mem_bytes = _sysctl("hw.memsize")
        if mem_bytes.isdigit():
            memory_gb = round(int(mem_bytes) / (1024**3))

    cpu_cores = os.cpu_count() or 0
    parts = [
        chip,
        f"{memory_gb} GB RAM" if memory_gb else "",
        f"{cpu_cores} CPU cores" if cpu_cores else "",
    ]
    summary = ", ".join(p for p in parts if p) or f"{system} ({platform.machine()})"

    return {
        "os": system,
        "machine": platform.machine(),
        "chip": chip,
        "memoryGB": memory_gb,
        "cpuCores": cpu_cores,
        "summary": summary,
    }
