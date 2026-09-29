"""Run tools/e2e_host.py N times in a row and check nothing is left between runs (WBS 5.9).

Run with the host_control venv Python (same as e2e_host.py):
  python tools/repeat_e2e.py --runs 10
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sandbox_manager.wsb import WsbCli  # noqa: E402
from tools.e2e_host import DEFAULT_HOST_REPO  # noqa: E402

E2E = Path(__file__).with_name("e2e_host.py")


def last_json(text: str) -> dict:
    start = text.rfind("\n{")
    try:
        return json.loads(text[start + 1:]) if start >= 0 else {}
    except ValueError:
        return {}


def leftovers(wsb: WsbCli, cert_dir: Path) -> list[str]:
    found = []
    if wsb.running():
        found.append(f"sandbox still running: {sorted(wsb.running())}")
    if cert_dir.exists() and any(cert_dir.iterdir()):
        found.append(f"host certificate/key left in {cert_dir}")
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=10)
    args = ap.parse_args()
    wsb = WsbCli()
    cert_dir = DEFAULT_HOST_REPO / "host" / ".certs"
    rows = []
    for i in range(1, args.runs + 1):
        t0 = time.monotonic()
        proc = subprocess.run([sys.executable, str(E2E)], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=400)
        s = last_json(proc.stdout)
        t = s.get("timings", {})
        left = leftovers(wsb, cert_dir)
        row = {"run": i, "outcome": s.get("outcome", "NO_RESULT"), "ready_s": t.get("ready"),
               "stop_s": None, "total_s": round(time.monotonic() - t0, 1),
               "host_address": s.get("host_address"), "leftovers": left}
        stop_events = []
        state = Path(s["dir"]) / "state.json" if s.get("dir") else None
        if state and state.exists():
            ev = json.loads(state.read_text(encoding="utf-8")).get("events", [])
            stop_events = [e for e in ev if e.get("kind") == "STATE" and e.get("to") == "TERMINATED"]
            removed = [e for e in ev if e.get("kind") == "CLEANUP"]
            row["cleanup_failed"] = removed[-1]["failed"] if removed else ["no CLEANUP event"]
            left_dirs = [n for n in ("package", "bootstrap", "sandbox.wsb") if (Path(s["dir"]) / n).exists()]
            if left_dirs:
                row["leftovers"].append(f"session files left: {left_dirs}")
        if stop_events:
            row["stop_s"] = stop_events[-1].get("stop_s")
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        if row["outcome"] != "PASS":
            print(proc.stdout[-3000:], flush=True)

    ok = [r for r in rows if r["outcome"] == "PASS" and not r["leftovers"] and not r.get("cleanup_failed")]
    ready = [r["ready_s"] for r in ok if r["ready_s"] is not None]
    stop = [r["stop_s"] for r in ok if r["stop_s"] is not None]
    summary = {"runs": len(rows), "clean_pass": len(ok),
               "ready_s": {"min": min(ready), "avg": round(statistics.mean(ready), 1), "max": max(ready)} if ready else None,
               "stop_s": {"min": min(stop), "avg": round(statistics.mean(stop), 2), "max": max(stop)} if stop else None,
               "addresses": sorted({r["host_address"] for r in rows if r["host_address"]})}
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if len(ok) == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
