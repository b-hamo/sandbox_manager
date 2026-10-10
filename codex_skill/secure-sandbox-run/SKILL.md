---
name: "secure-sandbox-run"
description: "Run, open, install or try any file or program from the internet (or any untrusted file) safely: download it with the scrp inbox_download tool (or, for a file the user already downloaded, copy it with inbox_import) and run it only inside the Secure CUA Windows Sandbox through the scrp MCP tools, never on this PC. Use whenever the user asks to download-and-run, run a file they already downloaded (Downloads folder), open an internet file (exe, zip, pdf, installer, script), test a program, or do anything risky that should not touch this computer. Do not use for ordinary coding or reading local project files."
---

# Secure Sandbox Run (Secure CUA, S-개발자)

This PC (the Host) is protected. **Nothing is run or installed on this PC. Files come in only through `inbox_download` or `inbox_import`; programs run only inside the Sandbox.**
Router (the component that will decide this automatically) is not built yet; until then this skill tells you the procedure. The real blocking is done by the Host (tool policy, deny-execute folder, read-only mapping), not by this text.

Reply to the user in Korean, briefly.

## 1. Get the file
- Use the `scrp` tool `inbox_download` (https URLs only). For a .zip pass `extract: true` to unpack it.
- The file lands in the Host inbox `C:\Users\JH\SecureCUA\codex-work\inbox`, which the Sandbox sees read-only as `C:\Inbox`.
- **The user already downloaded it** ("다운로드한 X 실행해줘", a file in the Downloads folder): use the `scrp` tool `inbox_import` with the **file name only** (e.g. `{"name": "ZoomIt.zip", "extract": true}`), never a path. It copies the file from Downloads into the same inbox; the original stays. If you are not sure of the exact name, list the Downloads folder with a read-only shell command (`Get-ChildItem $HOME\Downloads -Name`) and ask the user when several files match. Files outside Downloads are refused on purpose: do not try another path.
- Do not download with shell commands (curl, Invoke-WebRequest, ...). Do not run, install, move or copy the downloaded file on this PC.

## 2. Start the Sandbox and run it there (scrp MCP tools only)
1. `task_submit` (goal: what the user asked). The Sandbox starts; this may be before or after the download.
2. Call `computer_observe` with `wait_ms: 10000` until a screenshot comes back. "PREPARING" errors are normal: retry up to 12 times.
3. Each downloaded file is copied within seconds to the Sandbox desktop folder `Input` and checked. Big files can take 10–30 s.
4. Before running anything, double-click the desktop icon `input-check.html` (opens in Edge). Confirm the file's line says `OK` and its SHA-256 equals the `inbox_download` / `inbox_import` result; tell the user the size and SHA-256.
   - If the line is not there yet, wait 5 s and press F5; repeat up to 12 times (about 1 minute).
   - If it is still missing or says `FAIL`, do not run the file; report it. Never re-download it inside the Sandbox instead.
5. Open things by **double-clicking desktop icons** (the `Input` folder opens in Explorer) or use `computer_hotkey` with `keys: ["win", "r"]` to open the Run dialog inside the Sandbox. This requires host_control policy `POL-0.1.1` or later; after updating the Host, start a new session. The Sandbox has no Notepad, so `.txt` files do not open. If an older Host returns `POLICY_DENIED`, report that the Host needs updating; do not work around the denial.
6. Run the program by double-clicking it in the `Input` folder (GUI actions through scrp only).
7. Say it ran only after `computer_observe` shows its window. If only a license window appeared, say exactly "라이선스 창까지 확인" and do not accept the license unless the user asks.

## 3. Rules
- If a tool returns `POLICY_DENIED`, do not work around it; tell the user.
- If the Sandbox does not start, do not run the file on this PC instead; report it.
- When done, **do not call `session_stop`**. Leave the Sandbox open for the user to look at; call `session_stop` only when the user says "끝내" / "종료해".
- After `session_stop`, this conversation cannot start a Sandbox again (`SESSION_TERMINATED`): tell the user to open a new conversation.
