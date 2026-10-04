"""Sandbox Manager: the Windows Sandbox part of the Runtime/Lifecycle Manager (WBS 3.1~3.3, 4.8, 5.9).

The Host (b-hamo/host_control) calls this in its own process (SCRP: Broker and Runtime Manager are
in-process function calls). Order proven end to end on 2026-09-29 (poc/sandbox-launch/tools/e2e_real.py):

    Host                                        SandboxManager
    ----                                        --------------
                                           ->   prepare()           workspace + Runner package
                                           ->   start()             wsb start, window, Host address
                                                                    from the running Sandbox, firewall check
    certificate with SAN = s.host_address,
    register session, write bootstrap to
    s.bootstrap_path (--bootstrap-out)     ->   publish_bootstrap() certificate + address, ready marker LAST
    Runner connects, Startup Verification,
    READY                                  ->   mark_ready()        delete the spent token file
    TERMINATE / failure / kill             ->   stop()              wsb stop, confirmed by `wsb list`
                                           ->   cleanup()           package, bootstrap, .wsb removed

Recovery while the Host is alive (the Host decides when; at most max_restarts per session, both counted):
    Runner died, Sandbox fine              ->   restart_runner(s, generation+1)   old Runner ended in the Guest,
                                                                    start script waits for a new bootstrap
    Sandbox unresponsive                   ->   reset_sandbox(s, generation+1)    old Sandbox stopped (confirmed),
                                                                    a new one started; returns its Host address
    then, as above: new bootstrap (new token, the new generation) -> publish_bootstrap() -> READY -> mark_ready()
The Guest is touched only through `wsb exec` with one fixed command (config.restart_command()).

Why the address comes after start: after a reboot the Default Switch does not exist until the first
Sandbox starts, and it comes back on a different subnet. The Guest start script therefore waits for
the ready marker instead of taking the address on its command line.

If the Host process dies (Codex closed or killed) its Sandbox would keep running and every later
start() would fail with "already running". start() therefore first reclaims sessions whose owner
process is gone (reclaim_orphans). A Sandbox with no session record here is never touched.

READY is never decided here: only the Host, after its own checks, may say so (SCRP).
The workspace holds a live token between publish_bootstrap() and mark_ready(), so it must not be
synced (OneDrive); the Guest sees only the two read-only folders.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import ssl
import threading
import time
from dataclasses import asdict, dataclass, field
from functools import wraps
from pathlib import Path
from typing import Callable, TypeVar

from . import config
from .errors import (INVALID_ARGUMENT, RUNTIME_START_FAILED, RUNTIME_UNAVAILABLE,
                                    SandboxManagerError)
from .firewall import FIX_COMMAND, FirewallCheck
from .network import HostNetwork
from .process import process_started
from .wsb import WsbCli

RUNTIME_TYPE = "WINDOWS_SANDBOX"
TERMINATION_REASONS = ("TASK_COMPLETE", "USER_STOP", "SECURITY_VIOLATION", "TIMEOUT", "RUNTIME_ERROR")
ID_RE = re.compile(r"^[A-Z]{2,5}-[A-Za-z0-9]+(?:-[A-Za-z0-9]+){0,3}$")

PREPARED, STARTING, STARTED, RUNNING, RESTARTING, TERMINATED, FAILED = (
    "PREPARED", "STARTING", "STARTED", "RUNNING", "RESTARTING", "TERMINATED", "FAILED")
# A Sandbox may be up; stop() has not confirmed it gone. RESTARTING is live: a dead Runner is not a dead Host.
LIVE_STATES = (STARTING, STARTED, RUNNING, RESTARTING)


def default_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "SecureCUA" / "sandbox-manager"


F = TypeVar("F", bound=Callable)


def _locked(fn: F) -> F:
    """One call at a time per session (issue #3).

    The Host calls this class from worker threads (asyncio.to_thread). Right after READY a session
    can end, so mark_ready() and stop() may arrive together from two threads and interleave their
    state.json writes. Reentrant: start() calls stop() itself when it fails.
    A stop() that arrives while start() is still running waits for it; the Host does not do that
    (host_control lifecycle.py waits for start() first).
    """
    @wraps(fn)
    def wrapper(self: "SandboxManager", s: "SandboxSession", *args, **kwargs):
        with self._lock_for(s.session_id):
            return fn(self, s, *args, **kwargs)
    return wrapper  # type: ignore[return-value]


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class SandboxSession:
    session_id: str
    runtime_id: str
    generation: int
    dir: Path
    state: str = PREPARED
    runtime_type: str = RUNTIME_TYPE
    host_address: str | None = None
    sandbox_id: str | None = None
    guest_ip: str | None = None
    runner_sha256: str | None = None
    termination_reason: str | None = None
    owner: dict | None = None                    # {"pid", "started"} of the Host process that prepared it
    restarts: int = 0                            # restart_runner() + reset_sandbox() calls so far
    # Copies of user files the task needs: [{"name", "size", "sha256"}]. The source path is not kept:
    # it can be sensitive, and the Guest only ever sees the copy.
    input_files: list = field(default_factory=list)
    timings: dict = field(default_factory=dict)
    events: list = field(default_factory=list)

    @property
    def package_dir(self) -> Path:
        return self.dir / "package"

    @property
    def bootstrap_dir(self) -> Path:
        return self.dir / "bootstrap"

    @property
    def bootstrap_path(self) -> Path:
        """Where the Host must write bootstrap.json (host/sender.py --bootstrap-out)."""
        return self.bootstrap_dir / config.BOOTSTRAP_NAME

    @property
    def ready_path(self) -> Path:
        return self.bootstrap_dir / config.READY_NAME

    @property
    def wsb_path(self) -> Path:
        return self.dir / "sandbox.wsb"

    @property
    def input_dir(self) -> Path:
        return self.dir / "input"

    @property
    def guest_input_paths(self) -> list[str]:
        """Where the Guest sees each input file (read-only), to tell the Agent."""
        return [f"{config.GUEST_INPUT}\\{f['name']}" for f in self.input_files]

    def to_json(self) -> dict:
        d = asdict(self)
        d["dir"] = str(self.dir)
        return d


class SandboxManager:
    def __init__(self, root: Path | None = None, *, wsb: WsbCli | None = None, network: HostNetwork | None = None,
                 firewall: FirewallCheck | None = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 ip_wait_s: float = 120, stop_verify_s: float = 30, firewall_wait_s: float = 20,
                 process_started: Callable[[int], int | None] = process_started, max_restarts: int = 3,
                 log: Callable[[str], None] | None = None):
        self.root = (root or default_root()).resolve()
        onedrive = os.environ.get("OneDrive")
        if onedrive and str(self.root).lower().startswith(str(Path(onedrive).resolve()).lower()):
            # PoC finding: a synced folder would upload the live session token.
            raise SandboxManagerError(INVALID_ARGUMENT, f"workspace must not be inside OneDrive: {self.root}")
        self.wsb = wsb or WsbCli()
        self.net = network or HostNetwork()
        self.firewall = firewall or FirewallCheck()
        self.clock, self.sleep = clock, sleep
        # First start after a reboot took 51 s to get an address (it creates the switch).
        self.ip_wait_s, self.stop_verify_s = ip_wait_s, stop_verify_s
        # tools/install_firewall.ps1's watcher rebinds rules within ~5 s of the adapter appearing.
        self.firewall_wait_s = firewall_wait_s
        self.process_started = process_started
        # Safety cap per session (D-7). The Host's own policy decides when to restart; this only stops a loop.
        self.max_restarts = max_restarts
        self.log = log or (lambda msg: None)
        self._t0: dict[str, float] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, session_id: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(session_id, threading.RLock())

    # ------------------------------------------------------------------ bookkeeping
    def _save(self, s: SandboxSession) -> None:
        # A name no other thread or process uses, so two writers never share one temp file.
        tmp = s.dir / f"state.json.{os.getpid()}.{threading.get_ident()}.tmp"
        tmp.write_text(json.dumps(s.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(s.dir / "state.json")

    def _event(self, s: SandboxSession, kind: str, **fields) -> None:
        s.events.append({"at": _now(), "kind": kind, **fields})
        self._save(s)
        detail = " ".join(f"{k}={v}" for k, v in fields.items())
        self.log(f"[{s.session_id}] {kind} {detail}".rstrip())

    def _state(self, s: SandboxSession, new: str, **fields) -> None:
        old, s.state = s.state, new
        self._event(s, "STATE", **{"from": old, "to": new}, **fields)

    def _mark(self, s: SandboxSession, name: str) -> None:
        """Seconds since prepare() in this process (timings are for reports, not decisions)."""
        s.timings[name] = round(self.clock() - self._t0.setdefault(s.session_id, self.clock()), 2)

    def _need(self, s: SandboxSession, *states: str) -> None:
        if s.state not in states:
            raise SandboxManagerError(INVALID_ARGUMENT, f"session is {s.state}, expected {' or '.join(states)}")

    def load(self, session_id: str) -> SandboxSession:
        if not ID_RE.match(session_id):
            raise SandboxManagerError(INVALID_ARGUMENT, f"bad session id {session_id!r}")
        path = self.root / "sessions" / session_id / "state.json"
        if not path.exists():
            raise SandboxManagerError(INVALID_ARGUMENT, f"unknown session {session_id}")
        d = json.loads(path.read_text(encoding="utf-8"))
        d["dir"] = Path(d["dir"])
        return SandboxSession(**d)

    # ------------------------------------------------------------------ orphans
    def _me(self) -> dict:
        pid = os.getpid()
        try:
            started = self.process_started(pid)
        except OSError:
            started = None
        return {"pid": pid, "started": started}

    def _owner_alive(self, owner: dict | None) -> bool:
        """False only when the owner is surely gone. When in doubt, alive: never stop a live Host's Sandbox."""
        if not owner:
            return False                              # recorded before owners were (orphan from an older version)
        try:
            now = self.process_started(owner["pid"])
        except OSError:
            return True
        if now is None:
            return False
        return owner.get("started") is None or now == owner["started"]   # different start time: PID reused

    def reclaim_orphans(self) -> list[str]:
        """Stop and clean up Sandboxes whose Host process died before stop(). Returns their session IDs.

        start() calls this first. Only sessions recorded here are touched; a Sandbox without a record
        (started by hand, or by another tool) is left alone and start() refuses as before.
        """
        reclaimed = []
        sessions = self.root / "sessions"
        for path in sorted(sessions.glob("*/state.json")) if sessions.is_dir() else ():
            sid = path.parent.name
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                self.log(f"[{sid}] ORPHAN_SCAN_SKIPPED unreadable state.json: {exc}")
                continue
            if d.get("state") not in LIVE_STATES or self._owner_alive(d.get("owner")):
                continue
            with self._lock_for(sid):
                s = self.load(sid)                            # re-read under the lock
                if s.state not in LIVE_STATES or self._owner_alive(s.owner):
                    continue
                self._event(s, "ORPHAN_RECLAIMED", owner=s.owner, sandbox_id=s.sandbox_id)
                try:
                    self.stop(s, "RUNTIME_ERROR", emergency=True)
                except SandboxManagerError as exc:
                    self.log(f"[{sid}] ORPHAN_STOP_FAILED {exc.message}")   # still listed: start() will refuse
                    continue
                self.cleanup(s)
                reclaimed.append(sid)
        return reclaimed

    # ------------------------------------------------------------------ lifecycle
    def prepare(self, session_id: str, runtime_id: str, generation: int, runner_exe: Path, *,
                input_files: list | None = None) -> SandboxSession:
        """Create the session workspace with the Runner package. Starts nothing.

        input_files: user files the task needs. Each is COPIED into the workspace and shown to the
        Guest read-only under config.GUEST_INPUT (s.guest_input_paths); the original is never mapped.
        Whether a file may be handed over at all is the Host's policy decision; this only refuses
        what cannot be copied safely.
        """
        for value, what in ((session_id, "session_id"), (runtime_id, "runtime_id")):
            if not isinstance(value, str) or not ID_RE.match(value):
                raise SandboxManagerError(INVALID_ARGUMENT, f"bad {what} {value!r}")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise SandboxManagerError(INVALID_ARGUMENT, f"generation must be an integer >= 1, got {generation!r}")
        runner_exe = Path(runner_exe)
        if not runner_exe.is_file():
            raise SandboxManagerError(INVALID_ARGUMENT, f"Runner exe not found: {runner_exe}")
        sources = self._check_inputs(input_files or [])

        with self._lock_for(session_id):
            return self._prepare_locked(session_id, runtime_id, generation, runner_exe, sources)

    @staticmethod
    def _check_inputs(files: list) -> list[Path]:
        sources, names, total = [], set(), 0
        for f in files:
            p = Path(f)
            try:
                st = os.lstat(p)
            except OSError:
                raise SandboxManagerError(INVALID_ARGUMENT, f"input file not found: {p}") from None
            if p.is_symlink() or getattr(st, "st_file_attributes", 0) & config.FILE_ATTRIBUTE_REPARSE_POINT:
                raise SandboxManagerError(INVALID_ARGUMENT, f"input file is a link: {p}")
            if not p.is_file():
                raise SandboxManagerError(INVALID_ARGUMENT, f"input must be a regular file, not a folder: {p}")
            if p.name.lower() in names:                     # Windows names are case-insensitive
                raise SandboxManagerError(INVALID_ARGUMENT, f"two input files are both named {p.name!r}")
            if st.st_size > config.INPUT_FILE_MAX:
                raise SandboxManagerError(INVALID_ARGUMENT,
                                          f"input file {p.name!r} is {st.st_size} bytes (limit {config.INPUT_FILE_MAX})")
            total += st.st_size
            if total > config.INPUT_TOTAL_MAX:
                raise SandboxManagerError(INVALID_ARGUMENT, f"input files exceed {config.INPUT_TOTAL_MAX} bytes in total")
            names.add(p.name.lower())
            sources.append(p)
        return sources

    def _prepare_locked(self, session_id: str, runtime_id: str, generation: int, runner_exe: Path,
                        sources: list[Path]) -> SandboxSession:
        directory = self.root / "sessions" / session_id
        if directory.exists():
            raise SandboxManagerError(INVALID_ARGUMENT, f"session {session_id} already exists")
        s = SandboxSession(session_id, runtime_id, generation, directory, owner=self._me())
        s.package_dir.mkdir(parents=True)
        s.bootstrap_dir.mkdir()
        self._t0[session_id] = self.clock()

        shutil.copy2(runner_exe, s.package_dir / config.RUNNER_NAME)
        (s.package_dir / config.START_SCRIPT).write_text(config.START_PS1, encoding="utf-8-sig")
        (s.package_dir / config.RESTART_SCRIPT).write_text(config.RESTART_PS1, encoding="utf-8-sig")
        s.runner_sha256 = _sha256(s.package_dir / config.RUNNER_NAME)   # J-3: this session runs this exact build
        if sources:
            try:
                self._copy_inputs(s, sources)
            except (OSError, SandboxManagerError) as exc:
                shutil.rmtree(directory, ignore_errors=True)  # nothing half-prepared is left behind
                if isinstance(exc, SandboxManagerError):
                    raise
                raise SandboxManagerError(INVALID_ARGUMENT, f"could not copy input file: {exc}") from None
        self._event(s, "PREPARED", runner_sha256=s.runner_sha256)
        return s

    def _copy_inputs(self, s: SandboxSession, sources: list[Path]) -> None:
        s.input_dir.mkdir()
        total = 0
        for src in sources:
            dst = s.input_dir / src.name
            shutil.copyfile(src, dst)                         # a new file: no link back to the original
            size = dst.stat().st_size
            total += size
            if size > config.INPUT_FILE_MAX or total > config.INPUT_TOTAL_MAX:   # it grew after the check
                raise SandboxManagerError(INVALID_ARGUMENT, f"input file {src.name!r} grew past the size limit")
            s.input_files.append({"name": src.name, "size": size, "sha256": _sha256(dst)})
        config.check_contents(s.input_dir)
        self._event(s, "INPUT_FILES_COPIED", count=len(s.input_files), total_bytes=total,
                    names=[f["name"] for f in s.input_files])

    @_locked
    def start(self, s: SandboxSession) -> str:
        """Start the Sandbox and return the Host address the Runner must dial.

        The Runner cannot start yet: the Guest script waits for publish_bootstrap().
        """
        self._need(s, PREPARED)
        self.reclaim_orphans()
        running = self.wsb.running()
        if running:
            # One Windows Sandbox per PC (PoC). A second session needs another Runtime (미결 13).
            # Either a live Host owns it, or nobody here started it: refuse, never stop it.
            raise SandboxManagerError(RUNTIME_UNAVAILABLE, f"a Windows Sandbox is already running: {sorted(running)}"
                                      " (in use by another live Host, or not started by Sandbox Manager; close it first)")

        mappings = [config.Mapping(s.package_dir, config.GUEST_PACKAGE), config.Mapping(s.bootstrap_dir, config.GUEST_BOOTSTRAP)]
        if s.input_files:
            mappings.append(config.Mapping(s.input_dir, config.GUEST_INPUT))
        xml = config.build_wsb(mappings, config.logon_command(), workspace=s.dir)
        s.wsb_path.write_text(xml, encoding="utf-8")        # no secrets in it; kept until cleanup for audit
        self._state(s, STARTING)
        try:
            s.sandbox_id = self.wsb.start(xml)
            self._mark(s, "sandbox_started")
            self._event(s, "SANDBOX_STARTED", sandbox_id=s.sandbox_id)
            self.wsb.connect(s.sandbox_id)                   # LogonCommand waits for the window (PoC)

            deadline = self.clock() + self.ip_wait_s
            while not s.guest_ip and self.clock() < deadline:
                s.guest_ip = self.wsb.ip(s.sandbox_id)
                if not s.guest_ip:
                    self.sleep(2)
            if not s.guest_ip:
                raise SandboxManagerError(RUNTIME_START_FAILED, f"no Guest IPv4 within {self.ip_wait_s}s")
            toward = self.net.toward(s.guest_ip)
            switch = self.net.switch_ipv4()
            if toward != switch:
                # Only the switch path is covered by the Host firewall rules; a LAN route is not (NAT).
                raise SandboxManagerError(RUNTIME_START_FAILED,
                                          f"Guest is reached via {toward}, not the Sandbox switch address {switch}")
            s.host_address = config.check_ipv4(toward)
            self._mark(s, "network_confirmed")

            # The adapter exists only now after a reboot; give the rebind watcher time to catch up.
            deadline = self.clock() + self.firewall_wait_s
            problems = self.firewall.problems()
            while problems and self.clock() < deadline:
                self.sleep(2)
                problems = self.firewall.problems()
            if problems:
                raise SandboxManagerError(RUNTIME_UNAVAILABLE,
                                          "firewall not ready: " + "; ".join(problems)
                                          + f". Fix as administrator: {FIX_COMMAND} "
                                          "(or run tools/install_firewall.ps1 once so this is repaired automatically)")
            self._state(s, STARTED, guest_ip=s.guest_ip, host_address=s.host_address)
            return s.host_address
        except SandboxManagerError as exc:
            self._fail(s, exc)
            raise

    @_locked
    def publish_bootstrap(self, s: SandboxSession, host_cert_pem: str) -> None:
        """Hand the Guest what the Runner needs, after the Host wrote bootstrap.json. Marker goes last.

        Files are written directly: while a folder is mapped, the Host cannot rename inside it (PoC),
        so write-then-rename is impossible and the marker plays that role.
        Also the second half of restart_runner(): the Guest start script is waiting for this marker.
        """
        self._need(s, STARTED, RESTARTING)
        try:
            cert_der = ssl.PEM_cert_to_DER_cert(host_cert_pem)
        except (ValueError, TypeError):
            raise SandboxManagerError(INVALID_ARGUMENT, "host_cert_pem is not a PEM certificate") from None
        if "PRIVATE KEY" in host_cert_pem:
            raise SandboxManagerError(INVALID_ARGUMENT, "host_cert_pem contains a private key; pass the certificate only")
        if not s.bootstrap_path.is_file():
            raise SandboxManagerError(INVALID_ARGUMENT, f"Host has not written the bootstrap yet: {s.bootstrap_path}")
        try:
            json.loads(s.bootstrap_path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise SandboxManagerError(INVALID_ARGUMENT, "bootstrap.json is not complete JSON yet") from None

        (s.bootstrap_dir / config.CERT_NAME).write_bytes(cert_der)
        (s.bootstrap_dir / config.ADDRESS_NAME).write_text(s.host_address, encoding="ascii")
        for mapped in (s.package_dir, s.bootstrap_dir) + ((s.input_dir,) if s.input_files else ()):   # re-check
            config.check_contents(mapped)
        s.ready_path.write_text("", encoding="ascii")
        self._mark(s, "bootstrap_published")
        self._state(s, RUNNING)

    @_locked
    def mark_ready(self, s: SandboxSession) -> None:
        """The Host has verified the Runner. The bootstrap token is spent; remove the file the Guest can read.

        If the session already ended (stop() won the race), stop() has removed the file: nothing to do.
        """
        if s.state in (TERMINATED, FAILED):
            return
        self._need(s, RUNNING)
        s.ready_path.unlink(missing_ok=True)
        if s.bootstrap_path.exists():
            s.bootstrap_path.unlink()          # delete works on a mapped folder while running; rename does not (PoC)
            self._event(s, "BOOTSTRAP_TOKEN_REMOVED")
        self._mark(s, "ready")

    @_locked
    def is_running(self, s: SandboxSession) -> bool:
        return bool(s.sandbox_id) and s.sandbox_id in self.wsb.running()

    @_locked
    def stop(self, s: SandboxSession, reason: str, *, emergency: bool = False) -> SandboxSession:
        """Stop and confirm on the Host. The Host sends TERMINATE first unless it is an emergency;
        a Runner's TERMINATE_RESULT is never proof that the Sandbox is gone."""
        if reason not in TERMINATION_REASONS:
            raise SandboxManagerError(INVALID_ARGUMENT, f"reason must be one of {TERMINATION_REASONS}")
        if s.state in (TERMINATED, FAILED):
            return s
        self._event(s, "STOP_REQUESTED", reason=reason, emergency=emergency)
        t0 = self.clock()
        self._stop_sandbox(s)
        s.termination_reason = reason
        self._mark(s, "terminated")
        self._state(s, TERMINATED, reason=reason, verified_by="wsb list", stop_s=round(self.clock() - t0, 2))
        return s

    def _stop_sandbox(self, s: SandboxSession) -> None:
        """Remove what the Guest could still read, stop the Sandbox, return only once `wsb list` drops it."""
        s.ready_path.unlink(missing_ok=True)
        s.bootstrap_path.unlink(missing_ok=True)
        if not s.sandbox_id:
            return
        if s.sandbox_id in self.wsb.running():
            try:
                self.wsb.stop(s.sandbox_id)
            except SandboxManagerError as exc:
                self._event(s, "WSB_STOP_ERROR", error=exc.message)
        deadline = self.clock() + self.stop_verify_s
        while s.sandbox_id in self.wsb.running():
            if self.clock() > deadline:
                self._event(s, "STOP_NOT_CONFIRMED", sandbox_id=s.sandbox_id)
                raise SandboxManagerError(RUNTIME_UNAVAILABLE,
                                          f"sandbox {s.sandbox_id} still listed after {self.stop_verify_s}s")
            self.sleep(1)

    # ------------------------------------------------------------------ recovery (Host decides when)
    def _begin_recovery(self, s: SandboxSession, generation: int, *states: str) -> None:
        self._need(s, *states)
        if not isinstance(generation, int) or isinstance(generation, bool) or generation <= s.generation:
            raise SandboxManagerError(INVALID_ARGUMENT,
                                      f"generation must be an integer above {s.generation}, got {generation!r}")
        if s.restarts >= self.max_restarts:
            raise SandboxManagerError(RUNTIME_UNAVAILABLE,
                                      f"restart limit reached ({s.restarts}/{self.max_restarts}); stop the session")

    @_locked
    def restart_runner(self, s: SandboxSession, generation: int) -> None:
        """The Runner died or stopped answering; the Sandbox itself is fine.

        Ends the old Runner in the Guest and starts the Guest start script again, which waits for the
        new ready marker. The Host then writes bootstrap.json for `generation` (new token) and calls
        publish_bootstrap(); after READY, mark_ready(). Clicks or keys in flight are not replayed here.
        """
        self._begin_recovery(s, generation, RUNNING, RESTARTING)
        if not self.is_running(s):
            raise SandboxManagerError(RUNTIME_UNAVAILABLE, f"sandbox {s.sandbox_id} is gone; use reset_sandbox() or stop()")
        s.ready_path.unlink(missing_ok=True)                 # the Guest must wait for the NEW bootstrap
        s.bootstrap_path.unlink(missing_ok=True)
        s.restarts += 1
        old, s.generation = s.generation, generation
        self._state(s, RESTARTING, recovery="runner", generation_from=old, generation=generation, restarts=s.restarts)
        code = self.wsb.exec(s.sandbox_id, config.restart_command())
        if code != 0:
            self._event(s, "RUNNER_RESTART_FAILED", exit_code=code)
            raise SandboxManagerError(RUNTIME_START_FAILED,
                                      f"Guest restart script exit {code} (3: old Runner would not end); "
                                      "try reset_sandbox() or stop()")
        self._event(s, "RUNNER_RESTARTED", detail="waiting for the new bootstrap")

    @_locked
    def reset_sandbox(self, s: SandboxSession, generation: int) -> str:
        """The Sandbox is unresponsive: stop it (confirmed by `wsb list`) and start a fresh one.

        Returns the new Host address, exactly like start(); the Host then makes a certificate for it
        (the address may change), writes bootstrap.json for `generation` and calls publish_bootstrap().
        If the old Sandbox will not stop, raises RUNTIME_UNAVAILABLE and changes nothing else.
        """
        self._begin_recovery(s, generation, STARTED, RUNNING, RESTARTING)
        old_id = s.sandbox_id
        self._stop_sandbox(s)
        s.restarts += 1
        old, s.generation = s.generation, generation
        s.sandbox_id = s.guest_ip = s.host_address = None
        self._state(s, PREPARED, recovery="sandbox", old_sandbox_id=old_id, generation_from=old, generation=generation,
                    restarts=s.restarts)
        return self.start(s)

    def _fail(self, s: SandboxSession, exc: SandboxManagerError) -> None:
        """Never leave a Sandbox running after a failed start."""
        self._event(s, "START_FAILED", code=exc.code, error=exc.message)
        try:
            self.stop(s, "RUNTIME_ERROR", emergency=True)
        except SandboxManagerError as stop_exc:
            self._event(s, "STOP_AFTER_FAILURE_FAILED", error=stop_exc.message)
            return                                        # still listed: do not claim FAILED-and-gone
        self._state(s, FAILED)

    @_locked
    def cleanup(self, s: SandboxSession) -> dict:
        if s.state not in (TERMINATED, FAILED, PREPARED):
            raise SandboxManagerError(INVALID_ARGUMENT, f"session is {s.state}; stop it first")
        removed, failed = [], []
        for target in (s.bootstrap_dir, s.package_dir, s.input_dir, s.wsb_path):
            if not target.exists():
                continue
            try:
                shutil.rmtree(target) if target.is_dir() else target.unlink()
                removed.append(target.name)
            except OSError as exc:
                failed.append(f"{target.name}: {exc}")
        result = {"at": _now(), "removed": removed, "failed": failed}
        self._event(s, "CLEANUP", removed=removed, failed=failed)   # state.json stays as the audit record
        return result
