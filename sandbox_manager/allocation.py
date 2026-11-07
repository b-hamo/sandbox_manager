"""Machine-wide Windows Sandbox start coordination, independent of Host roots.

Hold the OS mutex from the occupancy check until the started VM is recorded.
An abandoned mutex permits inspection, never reclamation of the previous VM.
"""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os

from .errors import RUNTIME_UNAVAILABLE, SandboxManagerError

MUTEX_NAME = r"Global\SCRP.WindowsSandbox.Start.v1"


@contextmanager
def windows_sandbox_slot():
    if os.name != "nt":
        raise SandboxManagerError(RUNTIME_UNAVAILABLE, "Windows Sandbox allocation requires Windows")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateMutexW
    create.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    create.restype = wintypes.HANDLE
    wait = kernel.WaitForSingleObject
    wait.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    wait.restype = wintypes.DWORD
    release = kernel.ReleaseMutex
    release.argtypes = (wintypes.HANDLE,)
    release.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    handle = create(None, False, MUTEX_NAME)
    if not handle:
        raise SandboxManagerError(RUNTIME_UNAVAILABLE,
                                  f"cannot open Sandbox allocation mutex: WinError {ctypes.get_last_error()}")
    acquired = False
    try:
        result = wait(handle, 0)
        # WAIT_ABANDONED: process died, but its VM may still be running. The
        # caller must query WSB under this lock and preserve any occupied VM.
        acquired = result in (0, 0x80)
        if not acquired:
            detail = "another Host is starting Windows Sandbox" if result == 0x102 else (
                f"Sandbox allocation wait failed: WinError {ctypes.get_last_error()}")
            raise SandboxManagerError(RUNTIME_UNAVAILABLE, detail)
        yield
    finally:
        if acquired:
            release(handle)
        close(handle)
