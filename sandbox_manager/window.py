"""Keep the Sandbox window from taking over the user's screen (배주한 요청 2026-10-12).

`wsb connect` must open the Sandbox window (the Guest logs on only then, and the Runner starts at
logon), and the new window takes the foreground about a second later, cutting off whatever the user
was typing in. What was tried on the JH PC (2026-10-12, `wsb exec` screen check in the Guest):

    leave it                      Sandbox holds the foreground       Guest screen drawn
    minimize it                   still focused                      Guest screen goes dark: the Agent
                                                                     would see nothing (remote-session
                                                                     output stops) -> never do this
    push it to the bottom only    still focused                      drawn
    bottom + give focus back      Sandbox had it ~0.1 s, then the    drawn  <- this
                                  user's window again (60 s watched)

So: remember the user's foreground window before `connect`; for a short while afterwards, whenever a
Sandbox window takes the foreground, put it at the bottom of the z-order without activating it and
hand the foreground back. No key is pressed: our thread joins the Sandbox window's input queue for a
moment, which Windows allows to pass the foreground on. The window stays open (never minimized), so
the Guest screen keeps being drawn for the Runner. Bounded in time and count, so a user who clicks
the Sandbox window on purpose gets to keep it.
"""
from __future__ import annotations

import sys
import threading
import time

WATCH_S = 20.0          # the window appeared ~1 s after connect and took focus once; logon follows
MAX_GIVE_BACKS = 5      # the real Host run saw two grabs, 0.2 s apart
POLL_S = 0.05
PROCESS_HINT = "windowssandbox"     # WindowsSandboxRemoteSession.exe, WindowsSandboxClient.exe


def _api():
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    u32.GetForegroundWindow.restype = wintypes.HWND
    u32.SetForegroundWindow.argtypes = [wintypes.HWND]
    u32.IsWindow.argtypes = [wintypes.HWND]
    u32.IsWindowVisible.argtypes = [wintypes.HWND]
    u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_uint]
    return ctypes, wintypes, u32, k32


def foreground():
    """The user's current foreground window (None off Windows or when there is none)."""
    if sys.platform != "win32":
        return None
    return _api()[2].GetForegroundWindow() or None


def _sandbox_window(hwnd) -> bool:
    ctypes, wintypes, u32, k32 = _api()
    pid = wintypes.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = k32.OpenProcess(0x1000, False, pid.value)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        buf = ctypes.create_unicode_buffer(512)
        n = wintypes.DWORD(512)
        if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return False
        return PROCESS_HINT in buf.value.rsplit("\\", 1)[-1].lower()
    finally:
        k32.CloseHandle(h)


def give_back(sandbox_hwnd, user_hwnd) -> bool:
    """Sandbox window to the bottom (not activated, not minimized), foreground back to the user's window."""
    ctypes, wintypes, u32, k32 = _api()
    HWND_BOTTOM, SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 1, 0x1, 0x2, 0x10
    u32.SetWindowPos(sandbox_hwnd, HWND_BOTTOM, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
    me = k32.GetCurrentThreadId()
    theirs = u32.GetWindowThreadProcessId(sandbox_hwnd, None)
    attached = bool(u32.AttachThreadInput(me, theirs, True))
    try:
        if u32.SetForegroundWindow(user_hwnd):
            return True
    finally:
        if attached:
            u32.AttachThreadInput(me, theirs, False)
    # Refused (seen on the second grab, ~0.2 s after the first, in the real Host run): a lone Alt tap
    # counts as user input for the foreground lock. It goes to the Sandbox window, i.e. into the Guest,
    # before the Runner is ready, so it disturbs nothing.
    VK_MENU, KEYEVENTF_KEYUP = 0x12, 0x2
    u32.keybd_event(VK_MENU, 0, 0, 0)
    u32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    return bool(u32.SetForegroundWindow(user_hwnd))


def keep_behind(user_hwnd, *, watch_s: float = WATCH_S, max_give_backs: int = MAX_GIVE_BACKS,
                log=None) -> threading.Thread | None:
    """Start watching in the background; returns the thread (None when there is nothing to do)."""
    if sys.platform != "win32" or not user_hwnd:
        return None

    def run():
        u32 = _api()[2]
        end, given = time.monotonic() + watch_s, 0
        while time.monotonic() < end and given < max_give_backs:
            fg = u32.GetForegroundWindow()
            if fg and fg != user_hwnd and _sandbox_window(fg):
                if not u32.IsWindow(user_hwnd):
                    return                                   # the user's window is gone: leave it
                ok = give_back(fg, user_hwnd)
                given += 1
                if log:
                    log(f"Sandbox window took the foreground; moved behind, focus given back ({'ok' if ok else 'refused'})")
            time.sleep(POLL_S)

    t = threading.Thread(target=run, name="sandbox-window-behind", daemon=True)
    t.start()
    return t
