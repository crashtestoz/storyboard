"""Best-effort description of the machine this server runs on.

Exists for two consumers: ``/api/info`` (and so ``sbv_info`` over MCP), where
it is just informational, and the Storyboard AD assistant, which needs
*something* concrete to reason from when asked how long a render will take
rather than guessing. Every probe degrades to a shorter description instead
of failing the request that asked for it — this is context, not a
precondition for anything to work.
"""

from __future__ import annotations

import ctypes
import functools
import os
import platform
import re
import subprocess
import threading
import time


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


# --------------------------------------------------------------------------- #
# Live load (CPU / GPU / memory %), for the Details card
# --------------------------------------------------------------------------- #
#
# macOS only, and without psutil or sudo: CPU from the Mach kernel's tick
# counters, memory from vm_stat, GPU from the accelerator's own
# PerformanceStatistics in ioreg (the same "Device Utilization %" Activity
# Monitor's GPU History reads). Each probe is ~10-20 ms. Any that fails is
# reported as None rather than failing the request.

_cpu_prev: tuple[float, list[int]] | None = None
_cpu_lock = threading.Lock()


def _cpu_ticks() -> list[int] | None:
    try:
        lib = ctypes.CDLL("/usr/lib/libSystem.dylib")
        lib.mach_host_self.restype = ctypes.c_uint
        ticks = (ctypes.c_uint * 4)()          # user, system, idle, nice
        count = ctypes.c_uint(4)
        if lib.host_statistics(lib.mach_host_self(), 3, ticks, ctypes.byref(count)) != 0:
            return None                        # 3 = HOST_CPU_LOAD_INFO
        return list(ticks)
    except Exception:  # noqa: BLE001
        return None


def _cpu_percent() -> float | None:
    """Busy share of all cores since the previous call — the poll interval.
    With no recent previous sample, measures over a short window instead."""
    global _cpu_prev
    with _cpu_lock:
        now, ticks = time.monotonic(), _cpu_ticks()
        if ticks is None:
            return None
        if _cpu_prev is None or now - _cpu_prev[0] > 10:
            time.sleep(0.25)
            first, now, ticks = ticks, time.monotonic(), _cpu_ticks()
            if ticks is None:
                return None
        else:
            first = _cpu_prev[1]
        _cpu_prev = (now, ticks)
    # The counters are 32-bit and wrap (idle reaches 2**32 in weeks of uptime).
    delta = [(b - a) % 2**32 for a, b in zip(first, ticks)]
    total = sum(delta)
    return round(100 * (total - delta[2]) / total, 1) if total > 0 else None


def _memory() -> tuple[float, float] | None:
    """(used GB, total GB), "used" as Activity Monitor's Memory Used counts
    it: app memory (anonymous minus purgeable) + wired + compressed."""
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=2).stdout
        page = int(re.search(r"page size of (\d+) bytes", out).group(1))
        pages = {k.strip(): int(v) for k, v in re.findall(r"^(.+?):\s+(\d+)\.", out, re.M)}
        used = (pages["Anonymous pages"] - pages.get("Pages purgeable", 0)
                + pages["Pages wired down"] + pages.get("Pages occupied by compressor", 0)) * page
        total = int(_sysctl("hw.memsize"))
        return used / 1024**3, total / 1024**3
    except Exception:  # noqa: BLE001
        return None


def _gpu_percent() -> float | None:
    try:
        out = subprocess.run(["ioreg", "-r", "-d", "1", "-c", "IOAccelerator"],
                             capture_output=True, text=True, timeout=2).stdout
        found = re.findall(r'"Device Utilization %"=(\d+)', out)
        return float(max(map(int, found))) if found else None
    except Exception:  # noqa: BLE001
        return None


_power_snaps: list[dict[str, int]] = []   # the last two distinct readings
_power_lock = threading.Lock()


def _power_watts() -> float | None:
    """Whole-machine power draw in watts, from the power controller's own
    telemetry (ioreg AppleSmartBattery → PowerTelemetryData) — present on
    Apple silicon desktops as well as laptops, and readable without sudo,
    unlike powermetrics. DC power into the logic board plus the power
    supply's estimated conversion loss, i.e. roughly what the wall sees.

    The controller refreshes this about once a minute, each refresh adding a
    minute of per-second samples to accumulating counters. So the honest
    figure is the average between the last two refreshes — a 1-minute mean
    that changes once a minute — with the instantaneous reading as the
    fallback until two refreshes have been seen. On battery, battery power.
    """
    try:
        out = subprocess.run(["ioreg", "-rn", "AppleSmartBattery"],
                             capture_output=True, text=True, timeout=2).stdout
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r'"PowerTelemetryData" = \{([^}]*)\}', out)
    if not m:
        return None
    t = {k: int(v) for k, v in re.findall(r'"(\w+)"=(\d+)', m.group(1))}
    if t.get("SystemPowerIn", 0) == 0 and t.get("BatteryPower", 0) > 0:
        return round(t["BatteryPower"] / 1000, 1)
    with _power_lock:
        if not _power_snaps or t.get("SystemPowerInAccumulatorCount") != \
                _power_snaps[-1].get("SystemPowerInAccumulatorCount"):
            _power_snaps.append(t)
            del _power_snaps[:-2]
        snaps = list(_power_snaps)

    def average(total: str, count: str) -> float | None:
        if len(snaps) < 2:
            return None
        dn = snaps[1].get(count, 0) - snaps[0].get(count, 0)
        return (snaps[1].get(total, 0) - snaps[0].get(total, 0)) / dn if dn > 0 else None

    system = average("AccumulatedSystemPowerIn", "SystemPowerInAccumulatorCount")
    loss = average("AccumulatedAdapterEfficiencyLoss", "AdapterEfficiencyLossAccumulatorCount")
    if system is None:
        system, loss = t.get("SystemPowerIn"), t.get("AdapterEfficiencyLoss", 0)
    if not system:
        return None
    return round((system + (loss or 0)) / 1000, 1)


def system_load() -> dict:
    """Current CPU, GPU and memory use (percent) and power draw (watts); any
    reading that is unavailable is None."""
    if platform.system() != "Darwin":
        return {"cpu": None, "gpu": None, "memory": None, "powerW": None}
    mem = _memory()
    return {
        "cpu": _cpu_percent(),
        "gpu": _gpu_percent(),
        "memory": round(100 * mem[0] / mem[1], 1) if mem else None,
        "memoryUsedGB": round(mem[0], 1) if mem else None,
        "memoryTotalGB": round(mem[1]) if mem else None,
        "powerW": _power_watts(),
    }
