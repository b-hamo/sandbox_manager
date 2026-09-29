"""Real Windows Sandbox smoke test of SandboxManager alone: prepare -> start -> publish -> stop -> cleanup.

No Host runs, so the Runner will not connect; this checks only what the Manager owns
(workspace, .wsb, start, window, Host address from the running Sandbox, firewall check,
ready marker, Host-confirmed stop, cleanup). The bootstrap and certificate are synthetic.

Run: python tools/smoke_real.py <path to sandbox_runner.exe> [--hold 10]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sandbox_manager import SandboxManager  # noqa: E402

DUMMY_CERT = "-----BEGIN CERTIFICATE-----\nMIIBdGVzdA==\n-----END CERTIFICATE-----\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runner_exe", type=Path)
    ap.add_argument("--hold", type=float, default=10)
    args = ap.parse_args()

    m = SandboxManager(log=lambda msg: print(msg, flush=True))
    sid = f"SES-{time.strftime('%Y%m%d')}-S{time.strftime('%H%M%S')}"
    s = m.prepare(sid, "RT-SBX-901", 1, args.runner_exe)
    try:
        address = m.start(s)
        print(f"host address for the certificate: {address}", flush=True)
        s.bootstrap_path.write_text(json.dumps({"synthetic": True}), encoding="utf-8")
        m.publish_bootstrap(s, DUMMY_CERT)
        time.sleep(args.hold)
        print(f"is_running={m.is_running(s)}", flush=True)
        m.mark_ready(s)
    finally:
        if s.state not in ("TERMINATED", "FAILED"):
            m.stop(s, "TASK_COMPLETE")
        m.cleanup(s)
    print(json.dumps({k: v for k, v in s.to_json().items() if k != "events"}, ensure_ascii=False, indent=2))
    return 0 if s.state == "TERMINATED" else 1


if __name__ == "__main__":
    sys.exit(main())
