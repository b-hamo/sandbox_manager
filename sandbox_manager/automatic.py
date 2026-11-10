"""Choose once under the machine WSB lock; never migrate a bound session.

The caller must explicitly permit app-window capabilities. Desktop-only work
must keep using SandboxManager: micro_VM is not a compatible desktop fallback.
"""
from .allocation import windows_sandbox_slot
from .errors import INVALID_ARGUMENT, RUNTIME_UNAVAILABLE, SandboxManagerError
from .manager import SandboxManager, PREPARED, _locked
from .micro_vm import MicroVmManager


class AutomaticVmManager(SandboxManager):
    fixed_vm_binding = True

    def __init__(self, root, *, package, vm_root, relay, allow_app_window=False, max_active=8,
                 startup_timeout_s=180, **kwargs):
        if not allow_app_window:
            raise SandboxManagerError(INVALID_ARGUMENT, "automatic allocation requires explicit app-window acceptance")
        super().__init__(root, **kwargs)
        self.micro = MicroVmManager(root, package=package, vm_root=vm_root, relay=relay, max_active=max_active,
                                    startup_timeout_s=startup_timeout_s, **kwargs)
        # Common prepare/watcher/cleanup and delegated calls serialize the same session.
        self.micro._locks, self.micro._locks_guard = self._locks, self._locks_guard
        self._selected = None
        self._listener_binding = None

    def listener_binding(self):
        """Plan reachable listeners before boot; start rechecks under the OS lock.

        This is not a VM reservation. If occupancy changes before start, refuse
        rather than booting WSB with loopback-only listeners or migrating a VM.
        """
        if self._selected is not None:
            raise SandboxManagerError(RUNTIME_UNAVAILABLE, "runtime already bound")
        with windows_sandbox_slot(wait_ms=10000):
            self._listener_binding = "MICRO_VM_APP_WINDOW" if self.wsb.running() else "WINDOWS_SANDBOX"
        if self._listener_binding == "WINDOWS_SANDBOX":
            return {"host": "0.0.0.0", "port": 17443, "upload_port": 17444}
        return {"host": "127.0.0.1", "port": 0, "upload_port": 0}

    @property
    def guest_input_root(self):
        return self.micro.guest_input_root if self._selected == "MICRO_VM_APP_WINDOW" else None

    def _backend(self, s):
        if s.runtime_type == "MICRO_VM_APP_WINDOW":
            return self.micro
        if s.runtime_type != "WINDOWS_SANDBOX":
            raise SandboxManagerError(RUNTIME_UNAVAILABLE, "unknown fixed runtime binding")
        return super()

    @_locked
    def start(self, s):
        self._need(s, PREPARED)
        if self._selected is not None or s.sandbox_id or s.micro_vm:
            raise SandboxManagerError(RUNTIME_UNAVAILABLE, "runtime already selected; automatic reassignment refused")
        # Contention/access/query failures propagate. Only positively observed WSB
        # occupancy selects micro_VM; a failed WSB start never triggers fallback.
        with windows_sandbox_slot(wait_ms=10000):
            occupied = self.wsb.running()
            selected = "MICRO_VM_APP_WINDOW" if occupied else "WINDOWS_SANDBOX"
            if self._listener_binding is not None and self._listener_binding != selected:
                raise SandboxManagerError(RUNTIME_UNAVAILABLE,
                                          "WSB occupancy changed after listener planning; no VM started")
            self._selected = selected
            s.runtime_type = self._selected
            self._event(s, "RUNTIME_SELECTED", runtime_type=self._selected, occupied_wsb=sorted(occupied))
            if not occupied:
                return self._start_reserved(s)
        return self.micro.start(s)

    def publish_bootstrap(self, s, host_cert_pem):
        return self._backend(s).publish_bootstrap(s, host_cert_pem)

    def prepare_apps(self, s, apps):
        return self._backend(s).prepare_apps(s, apps)

    def share_program(self, s, name):
        return self._backend(s).share_program(s, name)

    def is_running(self, s):
        return self._backend(s).is_running(s)

    def window_state(self, s):
        return self._backend(s).window_state(s)

    def restart_runner(self, s, generation):
        return self._backend(s).restart_runner(s, generation)

    def runner_exited(self, s):
        return self.micro.runner_exited(s) if s.runtime_type == "MICRO_VM_APP_WINDOW" else False

    def reset_sandbox(self, s, generation):
        raise SandboxManagerError(RUNTIME_UNAVAILABLE, "runtime binding is fixed until session termination")

    def stop(self, s, reason, *, emergency=False):
        return self._backend(s).stop(s, reason, emergency=emergency)
