"""Reserve Windows host memory for other applications."""

import ctypes
import os

import psutil

JOB_COMMIT_GIB = 100
STARTUP_COMMIT_GIB = JOB_COMMIT_GIB + 48


def require_memory_headroom(*, available_gib: int, commit_gib: int) -> None:
    available = psutil.virtual_memory().available / 2**30
    if os.name != "nt":
        if available < available_gib:
            raise MemoryError(f"Insufficient physical RAM headroom (required {available_gib} GiB)")
        return

    class PerformanceInformation(ctypes.Structure):
        _fields_ = (
            [("cb", ctypes.c_ulong)]
            + [
                (name, ctypes.c_size_t)
                for name in (
                    "CommitTotal",
                    "CommitLimit",
                    "CommitPeak",
                    "PhysicalTotal",
                    "PhysicalAvailable",
                    "SystemCache",
                    "KernelTotal",
                    "KernelPaged",
                    "KernelNonpaged",
                    "PageSize",
                )
            ]
            + [(name, ctypes.c_ulong) for name in ("HandleCount", "ProcessCount", "ThreadCount")]
        )

    information = PerformanceInformation()
    information.cb = ctypes.sizeof(information)
    if not ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(information), information.cb):
        raise MemoryError("Cannot establish Windows memory headroom; keep Strata paused")
    available = psutil.virtual_memory().available / 2**30
    commit = (information.CommitLimit - information.CommitTotal) * information.PageSize / 2**30
    if available < available_gib or commit < commit_gib:
        raise MemoryError(
            f"Strata paused to protect other applications: available RAM {available:.1f} GiB (required {available_gib}), "
            f"free commit {commit:.1f} GiB (required {commit_gib})"
        )
