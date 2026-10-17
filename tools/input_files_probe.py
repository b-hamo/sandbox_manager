"""User files in a real Sandbox: prepare(input_files=...) -> start() -> check inside the Guest -> stop.

No Host is needed: the Runner never starts (no bootstrap is published). Inside the Guest, through
`wsb exec`, a fixed check verifies that both copies are visible under C:\\UserFiles, that the content
hash matches, and that creating, changing and deleting files there all fail (read-only mapping).

  python tools/input_files_probe.py --runner <sandbox_runner.exe>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sandbox_manager import SandboxManager, SandboxManagerError  # noqa: E402
from sandbox_manager import config  # noqa: E402

# exit codes from the Guest check
MEANING = {0: "OK: 2 files visible, content matches, create/change/delete all refused",
           2: "file count is not 2", 3: "report.txt content hash differs", 4: "could create a new file",
           5: "could change report.txt", 6: "could delete report.txt"}


def guest_check(expected_sha256: str) -> str:
    d = config.GUEST_INPUT
    script = (
        f"$d='{d}';"
        "if (@(Get-ChildItem -LiteralPath $d -File).Count -ne 2) { exit 2 };"
        f"if ((Get-FileHash -LiteralPath (Join-Path $d 'report.txt') -Algorithm SHA256).Hash.ToLower() -ne '{expected_sha256}') {{ exit 3 }};"
        "try { Set-Content -LiteralPath (Join-Path $d 'new.txt') -Value x -ErrorAction Stop; exit 4 } catch {};"
        "try { Add-Content -LiteralPath (Join-Path $d 'report.txt') -Value x -ErrorAction Stop; exit 5 } catch {};"
        "try { Remove-Item -LiteralPath (Join-Path $d 'report.txt') -ErrorAction Stop; exit 6 } catch {};"
        "exit 0")
    return f'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "{script}"'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runner", type=Path, required=True)
    args = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix="input-probe-"))
    report = work / "report.txt"
    report.write_bytes("Sandbox input file test\n합계: 12,345원\n".encode("utf-8"))
    (work / "견적서.txt").write_bytes("견적서 내용".encode("utf-8"))
    expected = hashlib.sha256(report.read_bytes()).hexdigest()

    m = SandboxManager(log=lambda msg: print(msg, flush=True))
    s = m.prepare(f"SES-{time.strftime('%Y%m%d')}-I{time.strftime('%H%M%S')}", "RT-SBX-001", 1, args.runner,
                  input_files=[report, work / "견적서.txt"])
    print("guest paths:", s.guest_input_paths, flush=True)
    outcome, code = "ERROR", None
    try:
        m.start(s)
        code = m.wsb.exec(s.sandbox_id, guest_check(expected))
        print(f"guest check exit {code}: {MEANING.get(code, 'unexpected')}", flush=True)
        outcome = "PASS" if code == 0 and report.read_bytes().decode("utf-8").startswith("Sandbox") else "FAIL"
    except SandboxManagerError as exc:
        print(f"[probe] {exc.code}: {exc.message}", flush=True)
    finally:
        m.stop(s, "TASK_COMPLETE", emergency=True)
        removed = m.cleanup(s)["removed"]
    print(json.dumps({"outcome": outcome, "guest_exit": code, "input_files": s.input_files,
                      "cleanup_removed": removed}, ensure_ascii=False, indent=2), flush=True)
    return 0 if outcome == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
