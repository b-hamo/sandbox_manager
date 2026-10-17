"""Keep the Sandbox window from taking over the user's screen (배주한 요청 2026-10-12).

`wsb connect` must open the Sandbox window (the Guest logs on only then, and the Runner starts at
logon), and that window keeps coming to the front: ~1 s after connect, again ~0.4 s later without
focus, and later whenever something opens inside the Guest (an installer, its UAC prompt, the
installed app: seen at +35 s and +190 s during a Codex install). What was tried on the JH PC
(2026-10-12, `wsb exec` screen check in the Guest):

    leave it                      Sandbox holds the foreground       Guest screen drawn
    minimize it                   still focused                      Guest screen goes dark: the Agent
                                                                     would see nothing (remote-session
                                                                     output stops) -> never do this
    push it to the bottom only    still focused                      drawn
    bottom + give focus back      ~0.1 s, then the user's window     drawn  <- this, for the whole session

So, for as long as the Sandbox window exists:
- it takes the foreground on its own -> push it to the bottom of the z-order (never minimized) and give
  the foreground back to the window the user had. No key is pressed if Windows allows the hand-over
  (our thread joins the Sandbox window's input queue for a moment); otherwise one Alt tap, which goes
  to the Sandbox window itself.
- it is drawn above the focused window without having the focus -> push it to the bottom.
- the USER brings it forward (clicks it, or picks it from the taskbar / Alt+Tab) -> leave it alone
  until they switch away again. See user_chose().
"""
from __future__ import annotations

import sys
import threading
import time

POLL_S = 0.05
APPEAR_S = 60.0          # stop if no Sandbox window shows up at all
GONE_S = 5.0             # stop once the Sandbox window has been gone this long
MAX_S = 12 * 3600.0      # hard stop
MAX_GIVE_BACKS = 200     # per session; a runaway loop ends here
CLICK_RECENT_S = 0.6
PROCESS_HINT = "windowssandbox"     # WindowsSandboxRemoteSession.exe, WindowsSandboxClient.exe
# Window classes the user goes through to switch windows on purpose.
SWITCHER_CLASSES = ("Shell_TrayWnd", "Shell_SecondaryTrayWnd", "MultitaskingViewFrame",
                    "XamlExplorerHostIslandWindow", "ForegroundStaging", "TaskSwitcherWnd")


def user_chose(prev_class: str, s_since_click_on_sandbox: float) -> bool:
    """Did the user bring the Sandbox forward on purpose (rather than the Sandbox taking it)?
    Only a mouse button actually pressed on the Sandbox window counts as a click: a cursor merely
    resting where the (large) window pops up must not."""
    if prev_class in SWITCHER_CLASSES:
        return True                                    # taskbar, Alt+Tab, Task View
    return s_since_click_on_sandbox <= CLICK_RECENT_S


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
    u32.GetWindow.restype = wintypes.HWND
    u32.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
    u32.GetAncestor.restype = wintypes.HWND
    u32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
    u32.WindowFromPoint.restype = wintypes.HWND
    u32.WindowFromPoint.argtypes = [wintypes.POINT]
    u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
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


def _sandbox_windows() -> list:
    """Visible top-level windows of the Sandbox client."""
    ctypes, wintypes, u32, k32 = _api()
    found = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _):
        if u32.IsWindowVisible(h) and _sandbox_window(h):
            found.append(h)
        return True
    u32.EnumWindows(proc(cb), 0)
    return found


def _above(a, b) -> bool:
    """True when window a is higher in the z-order than window b (drawn over it)."""
    u32 = _api()[2]
    GW_HWNDPREV = 3
    h = u32.GetWindow(b, GW_HWNDPREV)
    while h:
        if h == a:
            return True
        h = u32.GetWindow(h, GW_HWNDPREV)
    return False


def _class(hwnd) -> str:
    ctypes, wintypes, u32, k32 = _api()
    buf = ctypes.create_unicode_buffer(128)
    u32.GetClassNameW(hwnd, buf, 128)
    return buf.value


def _cursor_on(hwnds) -> bool:
    ctypes, wintypes, u32, k32 = _api()
    pt = wintypes.POINT()
    if not u32.GetCursorPos(ctypes.byref(pt)):
        return False
    h = u32.WindowFromPoint(pt)
    return bool(h) and u32.GetAncestor(h, 2) in hwnds        # GA_ROOT


def _mouse_down() -> bool:
    u32 = _api()[2]
    return any(u32.GetAsyncKeyState(vk) & 0x8000 for vk in (0x01, 0x02, 0x04))   # left, right, middle


def _to_bottom(hwnd) -> None:
    """Bottom of the z-order: not activated, not minimized (the Guest screen keeps being drawn)."""
    u32 = _api()[2]
    HWND_BOTTOM, SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 1, 0x1, 0x2, 0x10
    u32.SetWindowPos(hwnd, HWND_BOTTOM, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)


def give_back(sandbox_hwnd, user_hwnd) -> bool:
    """Sandbox window to the bottom (not activated, not minimized), foreground back to the user's window."""
    ctypes, wintypes, u32, k32 = _api()
    _to_bottom(sandbox_hwnd)
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
    # counts as input for the foreground lock. It goes to the Sandbox window, i.e. into the Guest.
    VK_MENU, KEYEVENTF_KEYUP = 0x12, 0x2
    u32.keybd_event(VK_MENU, 0, 0, 0)
    u32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    return bool(u32.SetForegroundWindow(user_hwnd))


def keep_behind(user_hwnd, *, log=None, poll_s: float = POLL_S) -> threading.Thread | None:
    """Watch in the background for as long as the Sandbox window exists; returns the thread."""
    if sys.platform != "win32" or not user_hwnd:
        return None

    def run():
        u32 = _api()[2]
        start = time.monotonic()
        last_user = user_hwnd                  # the window to give the foreground back to
        prev_user_class = None                 # class of the last non-Sandbox foreground window
        prev_was_sandbox, chosen, seen, gone_since, given, lowered = False, False, False, None, 0, 0
        last_click = -1e9                      # when a mouse button was last seen down on the Sandbox
        retry_at = 0.0
        while time.monotonic() - start < MAX_S and given < MAX_GIVE_BACKS:
            now = time.monotonic()
            hs = _sandbox_windows()
            if hs:
                seen, gone_since = True, None
            elif seen:
                gone_since = gone_since or now
                if now - gone_since > GONE_S:
                    return                                   # the Sandbox is closed
            elif now - start > APPEAR_S:
                return
            if hs and _mouse_down() and _cursor_on(hs):
                last_click = now
            fg = u32.GetForegroundWindow()
            # A window counts as the Sandbox's even before it is visible (it takes the foreground first).
            is_sandbox = bool(fg) and (fg in hs or _sandbox_window(fg))
            if is_sandbox:
                if not chosen and now >= retry_at:
                    switched = not prev_was_sandbox and prev_user_class is not None
                    if switched and user_chose(prev_user_class, now - last_click):
                        chosen = True                        # the user's own choice: leave it in front
                        if log:
                            log("Sandbox window brought forward by the user; left in front")
                    elif u32.IsWindow(last_user):
                        ok = give_back(fg, last_user)
                        given += 1
                        if log and (given <= 3 or given % 20 == 0):
                            log(f"Sandbox window took the foreground; moved behind, focus given back "
                                f"({'ok' if ok else 'refused'}, #{given})")
                        if not ok:
                            retry_at = now + 0.3             # refused: try again shortly, not every poll
            elif fg:
                chosen = False
                last_user = fg
                prev_user_class = _class(fg)
                for h in hs:
                    if _above(h, fg):
                        _to_bottom(h)
                        lowered += 1
                        if log and lowered == 1:
                            log("Sandbox window rose above the user's window (no focus); moved behind")
            prev_was_sandbox = is_sandbox
            time.sleep(poll_s)

    t = threading.Thread(target=run, name="sandbox-window-behind", daemon=True)
    t.start()
    return t
