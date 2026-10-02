"""End to end through SandboxManager with the real Host and the real Runner (both unmodified).

Stands in for the Host-side glue that 이준원's code will own later:
  - makes the Host certificate for the address start() returns (Host ensure_dev_cert only knows 127.0.0.1)
  - runs host/sender.py --demo broker with --bootstrap-out pointing at the session's bootstrap path
  - calls publish_bootstrap / mark_ready / stop / cleanup at the right moments

Run with the host_control venv Python (needs cryptography):
  python tools/e2e_host.py --host-repo <host_control checkout> --runner <sandbox_runner.exe>
"""
from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sandbox_manager import SandboxManager, SandboxManagerError  # noqa: E402

DOCS = Path.home() / "Documents"
DEFAULT_HOST_REPO = DOCS / "host_control-src" / "host_control-feat-19-observation-upload"
DEFAULT_RUNNER = DOCS / "sandbox_runner-src" / "sandbox_runner-develop" / "build" / "sandbox_runner.exe"


def host_cert(cert_dir: Path, ip: str) -> str:
    """Write the Host cert/key where host/sender.py reuses them. Returns the certificate PEM only."""
    cert_dir.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "scrp-host")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        # host/tls.py reuses a cert only if it is valid for more than 1 more day; otherwise it silently
        # replaces it with a 127.0.0.1-only cert. Deleted after the run anyway.
        .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=7))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName("scrp-host"), x509.IPAddress(ipaddress.ip_address(ip))]), critical=False)
        .sign(key, hashes.SHA256()))
    (cert_dir / "host-key.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    pem = cert.public_bytes(serialization.Encoding.PEM)
    (cert_dir / "host-cert.pem").write_bytes(pem)
    return pem.decode("ascii")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host-repo", type=Path, default=DEFAULT_HOST_REPO)
    ap.add_argument("--runner", type=Path, default=DEFAULT_RUNNER)
    ap.add_argument("--timeout", type=float, default=240)
    args = ap.parse_args()
    cert_dir = args.host_repo / "host" / ".certs"
    if cert_dir.exists() and any(cert_dir.iterdir()):
        print(f"[e2e] {cert_dir} already has files; refusing to overwrite", flush=True)
        return 2

    m = SandboxManager(log=lambda msg: print(msg, flush=True))
    sid = f"SES-{time.strftime('%Y%m%d')}-E{time.strftime('%H%M%S')}"
    s = m.prepare(sid, "RT-SBX-001", 1, args.runner)
    done, failed, ready = threading.Event(), threading.Event(), threading.Event()
    host = None
    outcome = "ERROR"
    log_path = s.dir / "host.log"
    try:
        address = m.start(s)

        pem = host_cert(cert_dir, address)
        env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1")
        host = subprocess.Popen(
            [sys.executable, "host/sender.py", "--demo", "broker", "--session", s.session_id, "--runtime", s.runtime_id,
             "--generation", str(s.generation), "--bootstrap-out", str(s.bootstrap_path), "--startup-timeout", "120"],
            cwd=args.host_repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")

        def pump():
            with log_path.open("w", encoding="utf-8") as f:
                for line in host.stdout:
                    f.write(line)
                    if " SEND " not in line and " RECV " not in line:
                        print(f"  host| {line.rstrip()}", flush=True)
                    if f"READY {s.session_id}" in line:
                        ready.set()
                    elif "broker demo complete" in line:
                        done.set()
                    # No "Traceback" check: asyncio on Windows logs a harmless ConnectionResetError traceback
                    # when the Runner hangs up. A real crash ends the Host without "broker demo complete",
                    # which the failed.set() below catches.
                    elif "STARTUP FAILED" in line or "demo stopped" in line:
                        failed.set()
            failed.set()                                      # Host exited

        threading.Thread(target=pump, daemon=True).start()

        deadline = time.monotonic() + 30
        while True:
            try:
                # Trust exactly what the Host put in the bootstrap, and make sure it is the one we made.
                used = json.loads(s.bootstrap_path.read_text(encoding="utf-8"))["host_certificate_pem"]
                if used.strip() != pem.strip():
                    raise RuntimeError("Host is not using the certificate made for this address")
                m.publish_bootstrap(s, used)
                break
            except (OSError, ValueError, KeyError, SandboxManagerError):
                if time.monotonic() > deadline or host.poll() is not None:
                    raise
                time.sleep(0.2)

        end = time.monotonic() + args.timeout
        marked = False
        while time.monotonic() < end and not done.is_set() and not failed.is_set():
            if ready.is_set() and not marked:
                m.mark_ready(s)
                marked = True
            time.sleep(0.2)
        outcome = "PASS" if done.is_set() else ("FAIL" if failed.is_set() else "TIMEOUT")
    except (SandboxManagerError, RuntimeError, OSError, ValueError, KeyError) as exc:
        print(f"[e2e] {type(exc).__name__}: {exc}", flush=True)
    finally:
        if host:
            host.terminate()
            try:
                host.wait(10)
            except subprocess.TimeoutExpired:
                host.kill()
        m.stop(s, "TASK_COMPLETE" if outcome == "PASS" else "RUNTIME_ERROR")
        m.cleanup(s)
        shutil.rmtree(cert_dir, ignore_errors=True)          # private key never outlives the run
    summary = {k: v for k, v in s.to_json().items() if k != "events"}
    summary["outcome"] = outcome
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if outcome == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
