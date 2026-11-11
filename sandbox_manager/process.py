"""Is the Host process that started a Sandbox still alive? (orphan reclaim, SandboxManager.reclaim_orphans)

A PID alone is not enough: Windows reuses PIDs, so the process creation time is recorded with it.
"""
from __future__ import annotations

import ctypes
import sys

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5


def process_started(pid: int) -> int | None:
    """Creation time (FILETIME ticks) of a live process, or None if no such process is running.

    Raises OSError when the process exists but cannot be inspected: the caller must then treat it as
    alive, because stopping another live Host's Sandbox is worse than leaving an orphan.
    """
    if sys.platform != "win32":
        raise OSError("process_started is Windows only")
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)

    handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        err = ctypes.get_last_error()
        if err == _ERROR_ACCESS_DENIED:
            raise OSError(err, f"cannot inspect process {pid}")
        return None                                   # ERROR_INVALID_PARAMETER: no such process
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise OSError(ctypes.get_last_error(), f"cannot inspect process {pid}")
        if code.value != _STILL_ACTIVE:
            return None                               # exited; the handle only keeps its record around
        times = [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            raise OSError(ctypes.get_last_error(), f"cannot inspect process {pid}")
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        k32.CloseHandle(handle)
