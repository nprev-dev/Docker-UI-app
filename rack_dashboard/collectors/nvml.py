"""NVIDIA cards, read through the driver's own library (NVML) instead of by starting nvidia-smi.

nvidia-smi is a front end to this same library. Starting it costs about 25 ms of processor time
for the readings and 80 ms for the list of programs; asking the library takes a fraction of a
millisecond. The answers have the same shape as hardware.parse_gpus and parse_gpu_apps give.

Whenever the library is missing or says no, NvmlError is raised and the caller falls back to
nvidia-smi, which also explains in its own words what is wrong.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import byref, c_uint, c_ulonglong, c_void_p
from typing import Any, Callable

SUCCESS = 0
NOT_SUPPORTED = 3
INSUFFICIENT_SIZE = 7
# What the library puts where it will not tell a program's memory (Windows desktops, some containers).
UNKNOWN_SIZE = 0xFFFFFFFFFFFFFFFF
TEMPERATURE_GPU = 0
NAME_SIZE = 96
# After a failed attempt to open the library, it is not tried again for this long.
RETRY_AFTER = 60.0


class NvmlError(Exception):
    pass


class Memory(ctypes.Structure):
    _fields_ = [("total", c_ulonglong), ("free", c_ulonglong), ("used", c_ulonglong)]


class MemoryV2(ctypes.Structure):
    # Version 2 leaves out what the driver keeps for itself, as nvidia-smi does when it says "used".
    _fields_ = [("version", c_uint), ("total", c_ulonglong), ("reserved", c_ulonglong), ("free", c_ulonglong), ("used", c_ulonglong)]


class Utilization(ctypes.Structure):
    _fields_ = [("gpu", c_uint), ("memory", c_uint)]


class ProcessV1(ctypes.Structure):
    _fields_ = [("pid", c_uint), ("usedGpuMemory", c_ulonglong)]


class ProcessV2(ctypes.Structure):
    _fields_ = [("pid", c_uint), ("usedGpuMemory", c_ulonglong), ("gpuInstanceId", c_uint), ("computeInstanceId", c_uint)]


# Newest first. Each version of the call expects its own size of entry.
PROCESS_CALLS = [("_v3", ProcessV2), ("_v2", ProcessV2), ("", ProcessV1)]


def load_library(platform: str = sys.platform, environ: Any = os.environ) -> Any:
    if platform == "win32":
        system = os.path.join(environ.get("SystemRoot", r"C:\Windows"), "System32", "nvml.dll")
        older = os.path.join(environ.get("ProgramFiles", r"C:\Program Files"), "NVIDIA Corporation", "NVSMI", "nvml.dll")
        for path in (system, older):
            if os.path.exists(path):
                return ctypes.CDLL(path)
        raise OSError("nvml.dll not found")
    if platform == "darwin":
        raise OSError("NVIDIA's library does not exist on macOS")
    return ctypes.CDLL("libnvidia-ml.so.1")


class Nvml:
    def __init__(self, load: Callable[[], Any] = load_library, monotonic: Callable[[], float] = time.monotonic):
        self._load = load
        self._monotonic = monotonic
        self._lib: Any = None
        self._failed_at: float | None = None

    def _library(self) -> Any:
        if self._lib is not None:
            return self._lib
        if self._failed_at is not None and self._monotonic() - self._failed_at < RETRY_AFTER:
            raise NvmlError("library not available")
        try:
            lib = self._load()
            if lib.nvmlInit_v2() != SUCCESS:
                raise NvmlError("the driver did not start the library")
        except (OSError, AttributeError, NvmlError) as exc:
            self._failed_at = self._monotonic()
            raise NvmlError(str(exc)) from None
        self._lib = lib
        return lib

    def _forget(self) -> None:
        """After a failed call: the next one opens the library afresh (a driver may have been updated)."""
        lib, self._lib = self._lib, None
        if lib is not None:
            try:
                lib.nvmlShutdown()
            except (OSError, AttributeError):
                pass

    def _cards(self, lib: Any) -> list[c_void_p]:
        count = c_uint(0)
        if lib.nvmlDeviceGetCount_v2(byref(count)) != SUCCESS:
            raise NvmlError("could not count the cards")
        handles = []
        for index in range(count.value):
            handle = c_void_p()
            if lib.nvmlDeviceGetHandleByIndex_v2(index, byref(handle)) != SUCCESS:
                raise NvmlError(f"could not reach card {index}")
            handles.append(handle)
        return handles

    def readings(self) -> list[dict]:
        lib = self._library()
        try:
            return [self._reading(lib, handle) for handle in self._cards(lib)]
        except NvmlError:
            self._forget()
            raise

    def _reading(self, lib: Any, handle: c_void_p) -> dict:
        name = ctypes.create_string_buffer(NAME_SIZE)
        if lib.nvmlDeviceGetName(handle, name, NAME_SIZE) != SUCCESS:
            raise NvmlError("the card did not give its name")

        def number(call: str, *args: Any) -> float | None:
            value = c_uint(0)
            return float(value.value) if getattr(lib, call)(handle, *args, byref(value)) == SUCCESS else None

        total, used = self._memory(lib, handle)
        load = Utilization()
        watts, limit = number("nvmlDeviceGetPowerUsage"), number("nvmlDeviceGetEnforcedPowerLimit")
        return {
            "name": name.value.decode(errors="replace").strip(),
            "mem_total": total,
            "mem_used": used,
            "load": float(load.gpu) if lib.nvmlDeviceGetUtilizationRates(handle, byref(load)) == SUCCESS else None,
            "temp": number("nvmlDeviceGetTemperature", TEMPERATURE_GPU),
            "fan": number("nvmlDeviceGetFanSpeed"),
            # The library counts in milliwatts.
            "watts": None if watts is None else watts / 1000,
            "limit_watts": None if limit is None else limit / 1000,
        }

    def _memory(self, lib: Any, handle: c_void_p) -> tuple[int | None, int | None]:
        if hasattr(lib, "nvmlDeviceGetMemoryInfo_v2"):
            memory = MemoryV2(version=ctypes.sizeof(MemoryV2) | (2 << 24))
            if lib.nvmlDeviceGetMemoryInfo_v2(handle, byref(memory)) == SUCCESS:
                return memory.total, memory.used
        memory = Memory()
        if lib.nvmlDeviceGetMemoryInfo(handle, byref(memory)) == SUCCESS:
            return memory.total, memory.used
        return None, None

    def programs(self) -> dict | None:
        """How many programs hold memory on the cards, and how much between them."""
        lib = self._library()
        try:
            count, sizes = 0, []
            for handle in self._cards(lib):
                total, _ = self._memory(lib, handle)
                # A program that both computes and draws is listed under both; it counts once.
                held: dict[int, int | None] = {}
                for kind in ("Compute", "Graphics"):
                    for pid, size in self._processes(lib, handle, f"nvmlDeviceGet{kind}RunningProcesses"):
                        if held.get(pid) is None:
                            # A size beyond the card's memory can only be a misread entry.
                            held[pid] = None if size == UNKNOWN_SIZE or (total and size > total) else size
                count += len(held)
                sizes += [size for size in held.values() if size is not None]
        except NvmlError:
            self._forget()
            raise
        return {"count": count, "mem": sum(sizes) if sizes or not count else None}

    def _processes(self, lib: Any, handle: c_void_p, call: str) -> list[tuple[int, int]]:
        for suffix, entry in PROCESS_CALLS:
            function = getattr(lib, call + suffix, None)
            if function is None:
                continue
            wanted = c_uint(0)
            answer = function(handle, byref(wanted), None)
            if answer == SUCCESS:
                return []
            if answer != INSUFFICIENT_SIZE:
                raise NvmlError(f"{call} said {answer}")
            # Room for programs that start between the two calls.
            room = c_uint(wanted.value + 8)
            entries = (entry * room.value)()
            if function(handle, byref(room), entries) != SUCCESS:
                raise NvmlError(f"{call} said no on the second call")
            return [(entries[index].pid, entries[index].usedGpuMemory) for index in range(room.value)]
        raise NvmlError(f"this driver has no {call}")
