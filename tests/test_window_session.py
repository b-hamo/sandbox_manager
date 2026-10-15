import os
import sys
import unittest
from unittest.mock import patch

from sandbox_manager.window_session import WindowSession, session_pids, windows_for_pids
from test_manager import Base, FakeClock, SBX


class WindowLifetime(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.visible = {}
        self.monitor = WindowSession(SBX, snapshot=lambda: self.visible, clock=self.clock, grace_s=5)

    def test_unseen_is_not_closed(self):
        self.clock.sleep(100)
        self.assertEqual(self.monitor.sample(), "UNSEEN")

    def test_close_is_debounced_and_sticky(self):
        self.visible = {10: 20}
        self.assertEqual(self.monitor.sample(), "OPEN")
        self.visible = {}
        self.assertEqual(self.monitor.sample(), "MISSING")
        self.clock.sleep(4)
        self.assertEqual(self.monitor.sample(), "MISSING")
        self.clock.sleep(1)
        self.assertEqual(self.monitor.sample(), "CLOSED")
        self.visible = {11: 21}
        self.assertEqual(self.monitor.sample(), "CLOSED")

    def test_window_replacement_resets_grace(self):
        self.visible = {10: 20}
        self.monitor.sample()
        self.visible = {}
        self.monitor.sample()
        self.clock.sleep(4)
        self.visible = {11: 21}
        self.assertEqual(self.monitor.sample(), "OPEN")
        self.visible = {}
        self.assertEqual(self.monitor.sample(), "MISSING")
        self.clock.sleep(4)
        self.assertEqual(self.monitor.sample(), "MISSING")

    def test_query_failure_is_not_absence_and_restarts_grace(self):
        self.visible = {10: 20}
        self.monitor.sample()
        self.visible = {}
        self.monitor.sample()
        self.clock.sleep(10)
        with patch.object(self.monitor, "_snapshot", side_effect=OSError("denied")):
            self.assertEqual(self.monitor.sample(), "UNKNOWN")
        self.assertEqual(self.monitor.sample(), "MISSING")

    def test_exact_session_and_executable_match(self):
        rows = [dict(Name="WindowsSandboxRemoteSession.exe", ProcessId=1, CommandLine=f'client --id {SBX}'),
                dict(Name="WindowsSandboxClient.exe", ProcessId=2, CommandLine=f'client --id "{SBX.upper()}"'),
                dict(Name="WindowsSandboxRemoteSession.exe", ProcessId=3, CommandLine=f'client --id {SBX}0'),
                dict(Name="WindowsSandboxServer.exe", ProcessId=4, CommandLine=f'server --id {SBX}'),
                dict(Name="WindowsSandboxRemoteSession.exe", ProcessId=5, CommandLine=None),
                dict(Name="WindowsSandboxRemoteSession.exe", ProcessId=6, CommandLine='client --id 00000000-0000-0000-0000-000000000000')]
        self.assertEqual(session_pids(rows, SBX), {1, 2})

    def test_reused_handle_with_different_pid_is_rediscovered(self):
        self.monitor._handles = {10: 20}
        with patch('sandbox_manager.window_session.windows_for_pids', side_effect=[{10: 99}, {}]), \
             patch('sandbox_manager.window_session.client_pids', return_value=set()) as query:
            self.assertEqual(self.monitor._windows(), {})
            query.assert_called_once_with(SBX)

    def test_reused_pid_is_not_an_owned_window(self):
        self.monitor._handles, self.monitor._births = {10: 20}, {20: 100}
        with patch('sandbox_manager.window_session.process_started', return_value=200), \
             patch('sandbox_manager.window_session.windows_for_pids', return_value={}) as windows, \
             patch('sandbox_manager.window_session.client_pids', return_value=set()) as query:
            self.assertEqual(self.monitor._windows(), {})
            self.assertEqual(windows.call_args_list[0].args, (set(),))
            query.assert_called_once_with(SBX)

    def test_live_window_uses_cached_identity_without_process_query(self):
        self.monitor._handles, self.monitor._births = {10: 20}, {20: 100}
        with patch('sandbox_manager.window_session.process_started', return_value=100), \
             patch('sandbox_manager.window_session.windows_for_pids', return_value={10: 20}), \
             patch('sandbox_manager.window_session.client_pids') as query:
            self.assertEqual(self.monitor._windows(), {10: 20})
            query.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "Windows HWND lifetime test")
    def test_real_window_minimize_hide_restore_and_destroy(self):
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        u32.CreateWindowExW.restype = wintypes.HWND
        u32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
        u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u32.DestroyWindow.argtypes = [wintypes.HWND]
        hwnd = u32.CreateWindowExW(0x08000000, "STATIC", "SCRP window lifetime test", 0x00CF0000,
                                   0, 0, 100, 100, None, None, None, None)
        self.assertTrue(hwnd)
        self.monitor._snapshot = lambda: {h: p for h, p in windows_for_pids({os.getpid()}).items() if h == hwnd}
        try:
            for command in (4, 6, 0, 4):  # shown without activation, minimized, hidden, restored
                u32.ShowWindow(hwnd, command)
                self.clock.sleep(10)
                self.assertEqual(self.monitor.sample(), "OPEN")
        finally:
            u32.DestroyWindow(hwnd)
        self.assertEqual(self.monitor.sample(), "MISSING")
        self.clock.sleep(5)
        self.assertEqual(self.monitor.sample(), "CLOSED")


class ManagerWindow(Base):
    def test_start_stop_disposes_observer_without_foreground_management(self):
        from unittest.mock import Mock
        observer = Mock(state="OPEN")
        self.wsb.watch_window = Mock(return_value=observer)
        manager, session = self.started()
        self.wsb.watch_window.assert_called_once_with(SBX, log=manager.log)
        self.assertEqual(manager.window_state(session), "OPEN")
        observer.state = "CLOSED"
        self.assertEqual(manager.window_state(session), "CLOSED")
        manager.stop(session, "USER_STOP")
        observer.stop.assert_called_once()
        self.assertEqual(manager.window_state(session), "UNAVAILABLE")

    def test_reset_uses_new_observer_and_closing_refuses_recovery(self):
        from unittest.mock import Mock
        old, new = Mock(state="OPEN"), Mock(state="OPEN")
        self.wsb.watch_window = Mock(side_effect=[old, new])
        manager, session = self.started()
        old.state = "MISSING"
        self.assertCode("RUNTIME_UNAVAILABLE", manager.reset_sandbox, session, 2)
        self.assertEqual(len(self.wsb.started_xml), 1)
        old.state = "OPEN"
        manager.reset_sandbox(session, 2)
        old.stop.assert_called_once()
        self.assertEqual(manager.window_state(session), "OPEN")
        self.assertEqual(self.wsb.watch_window.call_count, 2)
        manager.stop(session, "USER_STOP")
        new.stop.assert_called_once()
