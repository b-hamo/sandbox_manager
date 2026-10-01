"""Play Codex: drive host_control's MCP Server over stdio with the real Sandbox Manager and Runner.

Run with the host_control venv Python:
  python tools/mcp_e2e.py --host-repo <host_control checkout with PR #25> --runner <sandbox_runner.exe>
      [--sandbox-manager <sandbox_manager checkout, default: this repository>]

The Host imports sandbox_manager from --sandbox-manager (PYTHONPATH), not from its pinned pip install,
so this tests the working tree. Paths may be relative; they are resolved before the Host starts.

Flow: initialize → tools/list → task_submit (Sandbox starts in background, PREPARING)
→ computer_observe(wait_ms=10000) until an image comes back → click → type → runtime_get_state
→ session_stop → close stdin (Host stops the Sandbox and cleans up) → check `wsb list` is empty.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path


class Mcp:
    def __init__(self, cmd: list[str], cwd: Path, env: dict):
        self.p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
        self.next_id = 1
        self.log_lines: list[str] = []
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self):
        for raw in self.p.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            self.log_lines.append(line)
            if any(k in line for k in ("SANDBOX", "sandbox-manager", "READY", "STARTUP", "ERROR", "TOOL ", "TASK")):
                print(f"   host| {line[24:] if len(line) > 24 else line}", flush=True)

    def send(self, msg: dict) -> None:
        self.p.stdin.write(json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n")
        self.p.stdin.flush()

    def request(self, method: str, params: dict | None = None, timeout: float = 90) -> dict:
        mid = self.next_id
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})
        box: list = []
        t = threading.Thread(target=lambda: box.append(self.p.stdout.readline()), daemon=True)
        t.start()
        t.join(timeout)
        if not box or not box[0]:
            raise RuntimeError(f"no reply to {method} within {timeout}s")
        reply = json.loads(box[0])
        if reply.get("id") != mid:
            raise RuntimeError(f"reply id mismatch: {reply}")
        return reply

    def tool(self, name: str, args: dict, timeout: float = 90) -> tuple[bool, dict, bool]:
        """(ok, body, has_image)"""
        res = self.request("tools/call", {"name": name, "arguments": args}, timeout)["result"]
        text = next((c["text"] for c in res["content"] if c["type"] == "text"), "{}")
        has_image = any(c["type"] == "image" for c in res["content"])
        return not res["isError"], json.loads(text), has_image


def wsb_running() -> list[str]:
    out = subprocess.run(["wsb", "list", "--raw"], capture_output=True, text=True).stdout
    try:
        return json.loads(out).get("WindowsSandboxEnvironments") or []
    except ValueError:
        return [out.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host-repo", type=Path, required=True)
    ap.add_argument("--runner", type=Path, required=True)
    ap.add_argument("--sandbox-manager", type=Path, default=Path(__file__).resolve().parents[1])
    args = ap.parse_args()
    # mcp_server runs with cwd=host_repo, so relative paths would point somewhere else there.
    args.host_repo, args.runner, args.sandbox_manager = (
        p.resolve() for p in (args.host_repo, args.runner, args.sandbox_manager))
    for path, what in ((args.host_repo / "host" / "mcp_server.py", "--host-repo has no host/mcp_server.py"),
                       (args.runner, "--runner not found"),
                       (args.sandbox_manager / "sandbox_manager" / "__init__.py", "--sandbox-manager is not a checkout")):
        if not path.is_file():
            print(f"{what}: {path}")
            return 2
    if wsb_running():
        print("a Sandbox is already running; stop it first")
        return 2

    env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1",
               PYTHONPATH=str(args.sandbox_manager))              # this working tree, not the pinned pip copy
    t0 = time.monotonic()
    el = lambda: f"{time.monotonic() - t0:6.1f}s"                  # noqa: E731
    mcp = Mcp([sys.executable, "host/mcp_server.py", "--runner-exe", str(args.runner)], args.host_repo, env)
    steps: list[tuple[str, bool, str]] = []

    def step(name, ok, detail=""):
        steps.append((name, ok, detail))
        print(f"[{el()}] {'OK ' if ok else 'BAD'} {name} {detail}", flush=True)

    try:
        init = mcp.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                          "clientInfo": {"name": "mcp_e2e", "version": "0.1"}}, 30)
        step("initialize", "result" in init, init.get("result", {}).get("serverInfo", {}).get("name", ""))
        mcp.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = [t["name"] for t in mcp.request("tools/list", timeout=30)["result"]["tools"]]
        step("tools/list", "task_submit" in tools, f"{len(tools)} tools")

        ok, body, _ = mcp.tool("task_submit", {"goal": "메모 입력 시험"})
        step("task_submit", ok, f"runtime_state={body.get('runtime_state')} sandbox={body.get('sandbox', {}).get('state')}")

        got_image = False
        for i in range(15):                                        # up to ~150 s
            ok, body, got_image = mcp.tool("computer_observe", {"wait_ms": 10000}, timeout=40)
            if ok and got_image:
                step("computer_observe", True, f"image after {i + 1} call(s), {body.get('width')}x{body.get('height')}")
                break
            print(f"[{el()}]     waiting: {body.get('error') or body.get('runtime_state')}", flush=True)
        else:
            step("computer_observe", False, "no image")
        if got_image:
            w, h = body.get("width", 800), body.get("height", 600)
            ok, body, _ = mcp.tool("computer_click", {"x": w // 2, "y": h // 2})
            step("computer_click", ok, str(body.get("status") or body.get("error")))
            ok, body, _ = mcp.tool("computer_type", {"text": "안녕하세요"})
            step("computer_type", ok, str(body.get("status") or body.get("error")))
            ok, body, _ = mcp.tool("runtime_get_state", {})
            step("runtime_get_state", ok, f"runtime_state={body.get('runtime_state')} sandbox={body.get('sandbox')}")
            ok, body, _ = mcp.tool("session_stop", {"reason": "TASK_COMPLETE"})
            step("session_stop", ok, json.dumps(body, ensure_ascii=False)[:120])
    except Exception as e:  # noqa: BLE001
        step("driver", False, f"{type(e).__name__}: {e}")
    finally:
        mcp.p.stdin.close()                                        # Codex closes → Host stops + cleans up
        try:
            mcp.p.wait(90)
        except subprocess.TimeoutExpired:
            mcp.p.kill()
            step("mcp_server exit", False, "killed after 90 s")
        left = wsb_running()
        step("sandbox gone after exit", not left, str(left) if left else "")
        stopped = [l for l in mcp.log_lines if "stopped and cleaned up" in l]
        step("Host logged stop+cleanup", bool(stopped), stopped[-1][-140:] if stopped else "")
    passed = all(ok for _, ok, _ in steps)
    print(f"\nRESULT {'PASS' if passed else 'FAIL'}  total {el().strip()}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
