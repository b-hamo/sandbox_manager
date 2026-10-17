"""Runner restart and Sandbox reset with the real Host and the real Runner (both unmodified).

One session, three Host runs:
  1. generation 1: start, Runner READY, then the Host process is killed (the Runner loses its Host)
  2. restart_runner(generation 2): the old Runner is ended in the Guest; a new Host run with a new
     token must reach READY and finish the broker demo in the SAME Sandbox
  3. reset_sandbox(generation 3): the Sandbox is replaced; a new Host run must finish the demo in the NEW one
Then stop + cleanup, and nothing may be left running.

Run with the host_control venv Python (needs cryptography), like e2e_host.py:
  python tools/recovery_e2e.py --host-repo <host_control checkout> --runner <sandbox_runner.exe>
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_host import DEFAULT_HOST_REPO, DEFAULT_RUNNER, host_cert  # noqa: E402
from sandbox_manager import SandboxManager, SandboxManagerError  # noqa: E402


class HostRun:
    """One host/sender.py --demo broker process for one generation."""

    def __init__(self, repo: Path, s, log_path: Path):
        self.ready, self.done, self.failed = threading.Event(), threading.Event(), threading.Event()
        env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1")
        cmd = [sys.executable, "host/sender.py", "--demo", "broker", "--session", s.session_id, "--runtime", s.runtime_id,
               "--generation", str(s.generation), "--bootstrap-out", str(s.bootstrap_path), "--startup-timeout", "120"]
        if "--advertise-address" in (repo / "host" / "sender.py").read_text(encoding="utf-8"):
            cmd += ["--advertise-address", s.host_address]   # host_control PR #23+: certificate SAN + bootstrap host
        self.proc = subprocess.Popen(cmd, cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8")
        tag = f"g{s.generation}"

        def pump():
            with log_path.open("a", encoding="utf-8") as f:
                for line in self.proc.stdout:
                    f.write(f"{tag} {line}")
                    if " SEND " not in line and " RECV " not in line and "Traceback" not in line:
                        print(f"  host {tag}| {line.rstrip()}", flush=True)
                    if f"READY {s.session_id}" in line:
                        self.ready.set()
                    elif "broker demo complete" in line:
                        self.done.set()
                    elif "STARTUP FAILED" in line or "demo stopped" in line:
                        self.failed.set()
            self.failed.set()                                   # Host exited

        threading.Thread(target=pump, daemon=True).start()

    def kill(self):
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)], capture_output=True)
        self.proc.wait(10)


def publish(m: SandboxManager, s, pem: str, run: HostRun) -> None:
    deadline = time.monotonic() + 30
    while True:
        try:
            used = json.loads(s.bootstrap_path.read_text(encoding="utf-8"))
            if used["generation"] != s.generation:
                raise ValueError(f"bootstrap is for generation {used['generation']}, want {s.generation}")
            # Trust exactly what the Host put in the bootstrap. host_control PR #23+ makes its own
            # certificate for the address instead of reusing the one written here.
            if used["host_certificate_pem"].strip() != pem.strip():
                print("[recovery] Host made its own certificate (PR #23+ behaviour)", flush=True)
            m.publish_bootstrap(s, used["host_certificate_pem"])
            return
        except (OSError, ValueError, KeyError, SandboxManagerError):
            if time.monotonic() > deadline or run.proc.poll() is not None:
                raise
            time.sleep(0.2)


def until(run: HostRun, m: SandboxManager, s, want: str, timeout: float) -> bool:
    end, marked = time.monotonic() + timeout, False
    while time.monotonic() < end:
        if run.ready.is_set() and not marked:
            m.mark_ready(s)
            marked = True
            if want == "ready":
                return True
        if want == "done" and run.done.is_set():
            return True
        if run.failed.is_set() and not run.done.is_set():
            return False
        time.sleep(0.2)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host-repo", type=Path, default=DEFAULT_HOST_REPO)
    ap.add_argument("--runner", type=Path, default=DEFAULT_RUNNER)
    args = ap.parse_args()
    cert_dir = args.host_repo / "host" / ".certs"
    if cert_dir.exists() and any(cert_dir.iterdir()):
        print(f"[recovery] {cert_dir} already has files; refusing to overwrite", flush=True)
        return 2

    m = SandboxManager(log=lambda msg: print(msg, flush=True))
    s = m.prepare(f"SES-{time.strftime('%Y%m%d')}-R{time.strftime('%H%M%S')}", "RT-SBX-001", 1, args.runner)
    log_path = s.dir / "host.log"
    steps: list[tuple[str, bool, str]] = []
    run = None
    t0 = time.monotonic()

    def step(name, ok, detail=""):
        steps.append((name, ok, detail))
        print(f"[{time.monotonic() - t0:6.1f}s] {'OK ' if ok else 'BAD'} {name} {detail}", flush=True)
        return ok

    try:
        # 1. generation 1 up to READY, then the Host dies
        address = m.start(s)
        pem = host_cert(cert_dir, address)
        run = HostRun(args.host_repo, s, log_path)
        publish(m, s, pem, run)
        if not step("gen1 READY", until(run, m, s, "ready", 150)):
            raise RuntimeError("generation 1 never became READY")
        run.kill()
        first_sandbox = s.sandbox_id
        step("gen1 Host killed", True, f"sandbox still running: {m.is_running(s)}")

        # 2. restart the Runner in the same Sandbox
        m.restart_runner(s, 2)
        run = HostRun(args.host_repo, s, log_path)          # same address: the certificate is reused
        publish(m, s, pem, run)
        ok = until(run, m, s, "done", 180)
        step("gen2 restart_runner -> demo complete", ok and s.sandbox_id == first_sandbox,
             f"same sandbox: {s.sandbox_id == first_sandbox}, restarts={s.restarts}")
        run.kill()                                          # the demo Host keeps running after "complete"

        # 3. replace the Sandbox
        address = m.reset_sandbox(s, 3)
        shutil.rmtree(cert_dir, ignore_errors=True)
        pem = host_cert(cert_dir, address)                   # the address may differ: new certificate
        run = HostRun(args.host_repo, s, log_path)
        publish(m, s, pem, run)
        ok = until(run, m, s, "done", 180)
        step("gen3 reset_sandbox -> demo complete", ok and s.sandbox_id != first_sandbox,
             f"new sandbox: {s.sandbox_id != first_sandbox}, address={address}, restarts={s.restarts}")
        run.kill()
    except (SandboxManagerError, RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        step("exception", False, f"{type(exc).__name__}: {exc}")
    finally:
        if run and run.proc.poll() is None:
            run.kill()
        try:
            m.stop(s, "TASK_COMPLETE" if all(ok for _, ok, _ in steps) else "RUNTIME_ERROR", emergency=True)
            m.cleanup(s)
        except SandboxManagerError as exc:
            step("stop/cleanup", False, exc.message)
        shutil.rmtree(cert_dir, ignore_errors=True)          # private key never outlives the run
    left = subprocess.run(["wsb", "list"], capture_output=True, text=True).stdout.strip()
    step("nothing left running", not left, left)
    outcome = "PASS" if len(steps) == 5 and all(ok for _, ok, _ in steps) else "FAIL"
    print(json.dumps({"outcome": outcome, "session": s.session_id, "restarts": s.restarts,
                      "generation": s.generation, "timings": s.timings}, ensure_ascii=False, indent=2), flush=True)
    return 0 if outcome == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
