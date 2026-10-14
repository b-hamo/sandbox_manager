---
name: "secure-sandbox-run"
description: "Run, open, install or try any file or program from the internet (or any untrusted file) safely: download it with the scrp inbox_download tool (or, for a file the user already downloaded, copy it with inbox_import) and run it only inside the Secure CUA Windows Sandbox through the scrp MCP tools, never on this PC. Use whenever the user asks to download-and-run, run a file they already downloaded (Downloads folder), open an internet file (exe, zip, pdf, installer, script), test a program, or do anything risky that should not touch this computer. Do not use for ordinary coding or reading local project files."
---

# Secure Sandbox Run (Secure CUA, S-개발자)

This PC (the Host) is protected. **Nothing is run or installed on this PC. Files come in only through `inbox_download` or `inbox_import`; programs run only inside the Sandbox.**
Router (the component that will decide this automatically) is not built yet; until then this skill tells you the procedure. The real blocking is done by the Host (tool policy, deny-execute folder, read-only mapping), not by this text.

Reply to the user in Korean, briefly.

## 1. Get the file
- **Which source? Decide this first.**
  - The user gave a URL → `inbox_download`.
  - The user named a file but gave **no URL** (e.g. "tailscale-setup-1.102.4 설치해줘", with or without ".exe") → it is a file they already have. **First list the Downloads folder** (`Get-ChildItem $HOME\Downloads -Name`) and pick the file whose name matches (ignoring the extension). Use exactly that file with `inbox_import`. Do **not** search the web or pick a different package (other architecture, .msi instead of .exe, newer version).
  - Only if no file in Downloads matches: tell the user and ask for a URL (or whether to download the official installer). Several matches: ask which one.
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
7. **Inside the Sandbox, finish the job without asking** (배주한 결정 2026-10-12: the Sandbox is thrown away afterwards, so nothing there touches this PC):
   - If the user asked to install or run it ("설치해줘", "실행해줘", "깔아줘"), go all the way: tick the license agreement, press Next / Install / Finish, accept default options, and launch the program. Do not stop to ask. The user asking for the install counts as their agreement to the license.
   - If the user only asked to check it ("열리는지만 봐줘", "창이 뜨는지만"), stop when its first window (or license window) appears and say so.
   - **Stop and ask** only for: sign-in, passwords, payment, personal information, or anything that would send the user's data somewhere. Never type such data yourself.
   - UAC prompts inside the Sandbox: press Yes (the Sandbox user is an administrator of the Sandbox only).
   - **Installers are slow in the Sandbox. Be patient:** while a progress bar or "Preparing…" is shown, keep calling `computer_observe` with `wait_ms: 10000` for **up to 5 minutes**. A bar that stays at the same spot for a while is normal. Do not start a second installer for the same program meanwhile.
   - **Before saying it failed or is not finished, look once more** (`computer_observe` after 10 s) and check for a finish screen, the program's own window, or its icon on the desktop / Start menu. Report what the screen shows, not a guess.
8. Say it ran or was installed only after `computer_observe` shows it.
9. **Keeping something on this PC too (shared folder, when `task_submit`'s description mentions `Desktop\Shared`; 배주한 요청 2026-10-15):** a **portable program** (a zip you unpack, a single exe that needs no installer) or a **file** (PDF, document, result) the user wants on this PC goes in the Sandbox's desktop folder `Shared`: unzip it there, or "Save as" into it. It still runs there as usual. The Host sees a copy within seconds (where it cannot run). When the session ends the Host scans it and asks the person at this PC: Yes = keep (copied to `Downloads\SandboxShared\<session>`), No = delete, no answer = held in `SecureCUA\codex-work\shared\<session>` (not runnable). Tell the user this before `session_stop`; do not decide it for them.
   - An **installed** program (installer with services or drivers, e.g. Tailscale, VPNs) cannot come along this way. Install it in the Sandbox as above and tell the user it stays in the Sandbox only.
   - Do not put anything there the user did not want on this PC.

## 3. Rules
- If a tool returns `POLICY_DENIED`, do not work around it; tell the user.
- If the Sandbox does not start, do not run the file on this PC instead; report it.
- If an install fails, **report what you saw and leave the Sandbox open**. Never tell the user to install or run the file on this PC, never keep it in Downloads on your own, never end the Sandbox on your own. (The Host asks the user on screen before keeping files on this PC, before ending the Sandbox, and before downloading a file from the web when a similar one is already in Downloads. If the user says no, you get `POLICY_DENIED`: accept it.)
- When done, **do not call `session_stop`**. Leave the Sandbox open for the user to look at; call `session_stop` only when the user says "끝내" / "종료해".
- **Ending: keep or delete the inbox files.** By default everything in the inbox is deleted when the session ends. If the user wants to keep a file to use on this PC ("ZoomIt 남기고 끝내", "설치 파일은 남겨줘"), call `inbox_finish` with `keep` = the names exactly as `inbox_download` / `inbox_import` reported them (a folder name keeps the whole unpacked folder), then `session_stop`. Kept items go to the Downloads folder, marked as from the internet. If the user did not say, ask once ("받은 파일 중 PC에 남길 것이 있나요? 없으면 모두 지웁니다") before `session_stop`; "없어" → `session_stop` only. Tell the user where kept files went. Never run a kept file on this PC.
- After `session_stop`, this conversation cannot start a Sandbox again (`SESSION_TERMINATED`): tell the user to open a new conversation.
