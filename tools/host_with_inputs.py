"""DEMO GLUE, not product code: run host_control's MCP Server with Host files handed into the Sandbox.

host_control has no way yet to pass files to the Sandbox Manager (task_submit has no file field and
SandboxLauncher calls prepare() without input_files). Until the Host owner adds that (proposal:
docs/반입제안_Host연결.md), this wrapper does it from the outside, without changing host_control:

  1. at launch: register each --input (finished file, size, SHA-256, --input-source recorded)
  2. at task_submit, right before the Sandbox is prepared: ask the person at the Host with the
     Host's own approval dialog (host/approval.py, 45 s, no answer = no)
  3. approved: prepare(..., input_files=[registered files]); the Sandbox Manager refuses a file whose
     bytes changed since step 1. Denied: the start fails (POLICY_DENIED); nothing runs on the Host.

Or, with --inbox <folder> (배주한 결정 2026-10-08, no approval): the folder the Agent downloads into is
mapped read-only and live at C:\\Inbox, and an inbox.InboxWatcher lists each finished file (SHA-256) for
the Guest, which copies it to Desktop\\Input. Prepare the folder once with tools/setup_inbox.ps1 so
nothing in it can run on the Host.

Everything else is the unchanged Host: Broker policy, TLS, tokens, Startup Verification, SCRP.
The file is never opened on the Host except to read it. Running it is the Agent's GUI action inside
the Sandbox through SCRP (Desktop\\Input, after Desktop\\input-check.txt says OK).

Codex config (instead of host/mcp_server.py), with the host_control venv Python:
  args = ['<this file>', '--host-repo', '<host_control>', '--input', '<file>', '--input-source', '<url>',
          '--', '--runner-exe', '<sandbox_runner.exe>']
Everything after `--` goes to host/mcp_server.py unchanged.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))                     # this working tree, not the pinned pip copy in the Host venv
from sandbox_manager import SandboxManagerError, clear_inbox, config, register_input  # noqa: E402
from sandbox_manager.inbox import InboxWatcher, check_inbox_root  # noqa: E402

log = logging.getLogger("host-with-inputs")
TITLE = "SCRP 파일 반입 승인"


def describe(files) -> str:
    lines = ["AI 작업용 Sandbox에 이 컴퓨터의 파일을 넣으려고 합니다.", ""]
    for f in files:
        lines += [f"파일: {f.path.name}", f"크기: {f.size:,} bytes", f"SHA-256: {f.sha256}",
                  f"출처: {f.source or '(기록 없음)'}", ""]
    lines += ["이 컴퓨터에서는 실행하지 않습니다. Sandbox에 읽기 전용 사본으로만 들어가고,",
              "Sandbox 안에서 복사한 뒤 크기·SHA-256이 같을 때만 쓸 수 있습니다.",
              "Sandbox를 닫으면 사본은 함께 사라집니다.", "",
              "허용할까요? (45초 안에 답하지 않으면 거부됩니다)"]
    return "\n".join(lines)


class InputGate:
    """Wraps the real SandboxManager: prepare() gets the registered files (after approval) and the inbox."""

    def __init__(self, manager, files, ask, inbox=None):
        self._manager, self._files, self._ask, self._inbox = manager, files, ask, inbox
        self.watchers = []

    def __getattr__(self, name):
        return getattr(self._manager, name)

    def prepare(self, session_id, runtime_id, generation, runner_exe, **kwargs):
        if self._inbox is not None:
            kwargs["inbox"] = self._inbox
        s = self._prepare_files(session_id, runtime_id, generation, runner_exe, **kwargs)
        if self._inbox is not None:
            # 배주한 결정 2026-10-08: the inbox goes in without approval; the watcher lists finished files only.
            w = InboxWatcher(self._manager, s)
            w.start()
            self.watchers.append(w)
            log.info("INBOX %s mapped read-only at %s; watching %s", session_id, config.GUEST_INBOX, self._inbox)
        return s

    def _prepare_files(self, session_id, runtime_id, generation, runner_exe, **kwargs):
        if self._files:
            if not self._ask(describe(self._files), f"{TITLE} {session_id}"):
                log.warning("INPUT %s denied by the user; the Sandbox is not started", session_id)
                raise SandboxManagerError("POLICY_DENIED", "the user did not approve handing the files to the Sandbox")
            log.info("INPUT %s approved: %s", session_id,
                     ", ".join(f"{f.path.name} sha256={f.sha256}" for f in self._files))
            kwargs["input_files"] = list(self._files)
        return self._manager.prepare(session_id, runtime_id, generation, runner_exe, **kwargs)


    def cleanup(self, s):
        """Session end (the Host calls stop → cleanup): stop watching, then the default — empty the inbox
        (배주한 결정 2026-10-11). Whatever the user chose to keep was already copied by inbox_finish."""
        result = self._manager.cleanup(s)
        for w in [w for w in self.watchers if w.session is s]:
            w.stop()
            self.watchers.remove(w)
        if self._inbox is not None:
            try:
                log.info("INBOX %s emptied at session end: %d files deleted", s.session_id, clear_inbox(self._inbox))
            except (OSError, SandboxManagerError) as exc:
                log.warning("INBOX %s could not be emptied: %s", s.session_id, exc)
        return result


CONFIRM_TITLE = "샌드박스 작업 확인"
# What Codex did on its own in the app (2026-10-12) although the Skill said otherwise: it skipped the
# Skill, fetched a different package from the web although the file was in Downloads, copied it to
# Downloads without being asked, and ended the Sandbox by itself. Rules in text are not enough: the
# steps that touch this PC or end the user's Sandbox now need a "예" from the person at the Host.
STOP_RULE = (" ONLY when the user explicitly says to end it (\"끝내\", \"종료해\"). Never end the Sandbox on "
             "your own, not even after a failure: leave it open and report. The user is asked to confirm on "
             "screen; if they say no, the Sandbox stays open.")


def _add_inbox_tools(mcp_server, inbox: Path, user_folder: Path | None, ask=None) -> None:
    """Three more MCP tools (tools/inbox_download.py), answered here, outside the Broker:
    inbox_download (https URL -> inbox), inbox_import (a file already in Downloads -> inbox),
    inbox_finish (keep chosen items in Downloads, delete the rest).
    `ask(text, title) -> bool` is the Host's own yes/no dialog (45 s, no answer = no)."""
    import json
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from inbox_download import (FINISH_TOOL, IMPORT_TOOL, TOOL, DownloadError, download_into_inbox, finish,
                                import_into_inbox, similar_files)
    from sandbox_manager.importer import downloads_folder
    ask = ask or (lambda text, title: False)

    def fetch_or_import(key, run, check=None):
        def call(arguments):
            arg, extract = arguments.get(key), arguments.get("extract", False)
            if not isinstance(arg, str) or not isinstance(extract, bool) or set(arguments) - {key, "extract"}:
                raise DownloadError("INVALID_ARGUMENT", f"arguments: {key} (string), extract (boolean) only")
            if check:
                check(arg)
            body = run(arg, extract)
            return arg, body, ", ".join(f"{f['name']} sha256={f['sha256']}" for f in body["files"])
        return call

    def download_check(url):
        same = similar_files(user_folder or downloads_folder(), url)
        if same and not ask("AI가 인터넷에서 새 파일을 받으려고 합니다.\n\n받을 주소: " + url +
                            "\n\n그런데 다운로드 폴더에 비슷한 파일이 이미 있습니다:\n  " + "\n  ".join(same) +
                            "\n\n그래도 인터넷에서 새로 받을까요?\n(\"아니요\"면 받지 않습니다. 45초 안에 답이 없으면 아니요)",
                            CONFIRM_TITLE):
            raise DownloadError("POLICY_DENIED", "The user's Downloads folder already has " + ", ".join(same) +
                                " and the user did not approve downloading another file. Use inbox_import with "
                                "that file's name instead.")

    def keep_and_clear(arguments):
        keep = arguments.get("keep", [])
        if not isinstance(keep, list) or not all(isinstance(k, str) for k in keep) or set(arguments) - {"keep"}:
            raise DownloadError("INVALID_ARGUMENT", "arguments: keep (list of names) only")
        if keep and not ask("AI가 샌드박스에 넣었던 파일을 이 PC의 다운로드 폴더에 남기려고 합니다.\n\n  " +
                            "\n  ".join(keep) + "\n\n남길까요?\n(\"아니요\"면 아무것도 남기지 않습니다. "
                            "45초 안에 답이 없으면 아니요)", CONFIRM_TITLE):
            raise DownloadError("POLICY_DENIED", "The user did not approve keeping these files; nothing was kept. "
                                "Keep files only when the user asked for it in the chat.")
        body = finish(keep, inbox, user_folder)
        return keep, body, f"kept {[k['to'] for k in body['kept']]}, deleted {body['deleted_files']} files"

    tools = {TOOL["name"]: fetch_or_import("url", lambda u, x: download_into_inbox(u, inbox, x), download_check),
             IMPORT_TOOL["name"]: fetch_or_import("name", lambda n, x: import_into_inbox(n, inbox, x, user_folder)),
             FINISH_TOOL["name"]: keep_and_clear}
    load = mcp_server.load_tool_list

    def tool_list(*a, **k):
        listed = load(*a, **k)
        for t in listed:
            if t["name"] == "session_stop":
                t["description"] = t["description"].rstrip() + STOP_RULE
        return listed + [TOOL, IMPORT_TOOL, FINISH_TOOL]
    mcp_server.load_tool_list = tool_list
    call = mcp_server.HostBackend.call

    def patched(self, tool: str, arguments: dict) -> dict:
        if tool == "session_stop":
            if not ask("AI가 샌드박스를 끝내려고 합니다.\n\n끝내면 샌드박스 안의 모든 것이 사라집니다.\n"
                       "끝낼까요?\n(\"아니요\"면 샌드박스를 계속 열어 둡니다. 45초 안에 답이 없으면 아니요)",
                       CONFIRM_TITLE):
                log.warning("SESSION_STOP refused by the user: the Sandbox stays open")
                body = {"ok": False, "error": "POLICY_DENIED", "retryable": False,
                        "message": "The user chose to keep the Sandbox open. Do not call session_stop again "
                                   "unless the user says to end it."}
                return {"content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}], "isError": True}
            return call(self, tool, arguments)
        if tool not in tools:
            return call(self, tool, arguments)
        try:
            arg, body, summary = tools[tool](arguments)
            log.info("%s %s -> %s", tool.upper(), arg, summary)
            body = {"ok": True, **body}
            return {"content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}], "isError": False}
        except DownloadError as e:
            log.warning("%s %r refused: %s %s", tool.upper(), arguments, e.code, e.message)
            body = {"ok": False, "error": e.code, "message": e.message, "retryable": False}
            return {"content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}], "isError": True}

    mcp_server.HostBackend.call = patched


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    rest = argv[argv.index("--") + 1:] if "--" in argv else []
    mine = argv[:argv.index("--")] if "--" in argv else argv
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host-repo", type=Path, required=True)
    ap.add_argument("--input", type=Path, action="append", default=[], help="finished Host file (repeat)")
    ap.add_argument("--input-source", action="append", default=[], help="where it came from, same order")
    ap.add_argument("--inbox", type=Path, default=None,
                    help="shared folder the Agent downloads into, mapped read-only (tools/setup_inbox.ps1 first)")
    ap.add_argument("--import-from", type=Path, default=None,
                    help="folder inbox_import copies from by file name, and inbox_finish keeps into "
                         "(default: the user's Downloads)")
    a = ap.parse_args(mine)
    if len(a.input_source) > len(a.input):
        ap.error("more --input-source than --input")
    # Registered now, at launch: these exact bytes are what the user approves and the Sandbox gets.
    files = [register_input(p, a.input_source[i] if i < len(a.input_source) else None)
             for i, p in enumerate(a.input)]

    sys.path.insert(1, str(a.host_repo.resolve()))
    from host import mcp_server                    # the unchanged Host
    from host.approval import DialogApprover       # the Host's own approval dialog (45 s, no answer = no)

    real = mcp_server._real_sandbox_manager
    inbox = check_inbox_root(a.inbox) if a.inbox is not None else None   # refuse a bad folder before Codex starts
    mcp_server._real_sandbox_manager = lambda root: InputGate(real(root), files, DialogApprover()._ask, inbox)
    if inbox is not None:
        # A new Host = a new conversation. What an earlier one left (it was killed before cleanup) was not
        # chosen to be kept, so the default applies: delete. Logged once logging starts in mcp_server.main.
        leftover = clear_inbox(inbox)
        _add_inbox_tools(mcp_server, inbox, a.import_from, DialogApprover()._ask)
        if leftover:
            print(f"host-with-inputs: deleted {leftover} files left in the inbox by an earlier session",
                  file=sys.stderr)
    mcp_server.main(rest)                          # logging starts here; the approval line names each file


if __name__ == "__main__":
    main()
