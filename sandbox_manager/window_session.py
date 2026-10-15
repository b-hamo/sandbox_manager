"""Observe only the client windows belonging to one `wsb --id` session.

Closing the remote-session window disconnects its UI; it need not stop the VM.
This module reports that intent. The Host owns termination and shared-file decisions.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
import threading
import time
import uuid

from . import window
from .process import process_started

CLIENTS = {"windowssandboxremotesession.exe", "windowssandboxclient.exe"}


def session_pids(rows, sandbox_id):
    """Match an exact --id argument, never a title or a process-name substring."""
    pattern = re.compile(r'(?:^|\s)--id(?:\s+|=)"?' + re.escape(sandbox_id) + r'"?(?=\s|$)', re.I)
    return {int(row["ProcessId"]) for row in rows
            if str(row.get("Name", "")).lower() in CLIENTS
            and pattern.search(row.get("CommandLine") or "")}


def client_pids(sandbox_id):
    script = ("$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
              "@(Get-CimInstance Win32_Process -Filter "
              "\"Name='WindowsSandboxRemoteSession.exe' OR Name='WindowsSandboxClient.exe'\" "
              "| Select-Object Name,ProcessId,CommandLine) | ConvertTo-Json -Compress")
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                            capture_output=True, timeout=5, check=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    rows = json.loads(result.stdout.decode("utf-8-sig") or "[]")
    return session_pids(rows if isinstance(rows, list) else [rows], sandbox_id)


def windows_for_pids(pids):
    """Include hidden/minimized top-level windows: visibility is not lifetime."""
    ctypes, wintypes, u32, _ = window._api()
    found = {}
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _):
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            found[int(hwnd)] = pid.value
        return True

    if not u32.EnumWindows(callback_type(visit), 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return found


class WindowSession:
    """A cancellable observer. MISSING is a grace period, CLOSED is sticky.

    Errors reset the absence timer. Never seeing a window is UNSEEN, not a close.
    Cached HWND/PID pairs keep normal polling cheap; after destruction rediscover
    the session's client to allow a replacement window within the grace period.
    """
    def __init__(self, sandbox_id, *, snapshot=None, clock=time.monotonic, grace_s=5.0, poll_s=0.5, log=None):
        self.sandbox_id = str(uuid.UUID(sandbox_id))
        self._snapshot = snapshot or self._windows
        self._clock, self._grace_s, self._poll_s = clock, grace_s, poll_s
        self._log = log or (lambda message: None)
        self._handles = {}
        self._births = {}
        self._seen, self._missing_at = False, None
        self.state = "UNSEEN"
        self._stop = threading.Event()
        self._thread = None

    def _windows(self):
        if self._handles:
            live_pids = {pid for pid, birth in self._births.items()
                         if birth is not None and process_started(pid) == birth}
            current = windows_for_pids(live_pids)
            if any(current.get(hwnd) == pid for hwnd, pid in self._handles.items()):
                self._handles = current
                return current
        self._handles = windows_for_pids(client_pids(self.sandbox_id))
        self._births = {pid: process_started(pid) for pid in set(self._handles.values())}
        return self._handles

    def sample(self):
        if self.state == "CLOSED":
            return self.state
        try:
            present = bool(self._snapshot())
        except Exception as exc:
            if self.state != "UNKNOWN":
                self._log(f"Sandbox window query failed; not treating it as a close: {exc}")
            self._missing_at, self.state = None, "UNKNOWN"
            return self.state
        if present:
            self._seen, self._missing_at, self.state = True, None, "OPEN"
        elif self._seen:
            now = self._clock()
            if self._missing_at is None:
                self._missing_at = now
            self.state = "CLOSED" if now - self._missing_at >= self._grace_s else "MISSING"
        else:
            self.state = "UNSEEN"
        return self.state

    def start(self):
        def run():
            while not self._stop.is_set():
                if self.sample() == "CLOSED":
                    self._log(f"Sandbox client window closed: {self.sandbox_id}")
                    return
                self._stop.wait(self._poll_s)
        if sys.platform == "win32":
            self._thread = threading.Thread(target=run, name="sandbox-window-session", daemon=True)
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=6)
