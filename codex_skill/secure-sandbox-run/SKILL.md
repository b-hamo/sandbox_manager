---
name: "secure-sandbox-run"
description: "Install, run, open or try any program or file from the internet (or any untrusted file) safely: this PC downloads it into the Downloads folder with the scrp inbox_download tool (or copies a file the user already has with inbox_import), the Windows Sandbox runs that copy from its desktop Input folder, and the installed result comes back to this PC through the Sandbox's desktop Shared folder. Use whenever the user asks to download, install, run or test a program (e.g. \"7-Zip 받아서 설치해줘\"), run a file in their Downloads folder, or open an internet file (exe, msi, zip, pdf, installer, script). Never run such files on this PC. Do not use for ordinary coding or reading local project files."
---

# Secure Sandbox Run (Secure CUA, S-개발자)

Reply to the user in Korean, briefly.

## The flow (배주한 결정 2026-10-15) — always these three steps

```
1. THIS PC downloads the file        → Downloads\7z2604-x64.exe   (stays there after the session)
2. The Sandbox runs THAT file        → Sandbox desktop Input\7z2604-x64.exe → install
3. The result comes back to THIS PC  → install into Sandbox desktop Shared\7-Zip
                                        → this PC sees it, and keeps it after the user says yes at the end
```

Example: "7-Zip 받아서 설치해줘" → step 1 `inbox_download https://www.7-zip.org/a/7z2604-x64.exe` → step 2 double-click `Input\7z2604-x64.exe` in the Sandbox → step 3 set the install folder to `C:\Users\WDAGUtilityAccount\Desktop\Shared\7-Zip`, install.

**Never** download anything inside the Sandbox (no Edge/browser download, no Win+R or PowerShell download command there), and **never** run or install the file on this PC. If a step fails, report it and stop; do not take another route.

## Step 1. This PC gets the file (`scrp` tools `inbox_download` / `inbox_import`)
1. **The user gave a URL** → `inbox_download` with it.
2. **The user named a program or file without a URL** ("7-Zip 받아서 설치해줘", "tailscale-setup-1.102.4 설치해줘"):
   - **First list the Downloads folder** (`Get-ChildItem $HOME\Downloads -Name`, read-only). If a file matches the name (ignoring the extension), use exactly that file with `inbox_import` (`{"name": "7z2604-x64.exe"}`, file name only, never a path). Do not download it again or swap it for another package (other architecture, .msi instead of .exe, newer version). Several matches: ask which one.
   - **Nothing matches:** find the **official** download yourself (the vendor's own site; a web search on this PC is fine) and call `inbox_download` with the direct https URL of the Windows installer (64-bit, latest stable unless the user named a version). Do not ask the user for the URL; tell them which URL you used. Ask only if you cannot tell which site is official.
   - For a .zip pass `extract: true`.
3. **Check the result.** `downloads_copy` says where the file is in Downloads (Host option `--save-downloads`): tell the user ("다운로드 폴더에 저장했어요: 7z2604-x64.exe"). It stays there after the session. `sandbox_path` (`Desktop\Input\<name>`) is where the Sandbox will see it.
- Do not download with shell commands (curl, Invoke-WebRequest, ...) on this PC either. Do not open, run, move or copy the downloaded file on this PC.

## Step 2. The Sandbox runs that file (`scrp` GUI tools only)
1. `task_submit` (goal: what the user asked). Call it first or right after step 1; the Sandbox takes up to a minute to start.
2. `computer_observe` with `wait_ms: 10000` until a screenshot comes back. "PREPARING" errors are normal: retry up to 12 times.
3. Within seconds the file is copied to the Sandbox desktop folder **`Input`** and checked. Double-click the desktop icon `input-check.html` (opens in Edge): its line for the file must say `OK` with the same SHA-256 as step 1. Tell the user the size and SHA-256. Not there yet: wait 5 s, press F5, up to 12 times. Missing or `FAIL`: do not run it; report it.
4. Open the `Input` folder (double-click the desktop icon, or `computer_hotkey` `["win", "e"]`) and **double-click the file there**. That copy is the only one you may run. Win+R works too (`computer_hotkey` `["win", "r"]`, host policy `POL-0.1.1` or later).
5. **Finish the job without asking** (배주한 결정 2026-10-12: the Sandbox is thrown away afterwards):
   - "설치해줘", "실행해줘", "깔아줘" → go all the way: accept the license, Next / Install / Finish, default options except the install folder (step 3), then start the program. The user's request counts as agreement to the license.
   - "열리는지만 봐줘" → stop at the first window and say so.
   - **Stop and ask** only for sign-in, passwords, payment, personal information, or anything that sends the user's data somewhere. Never type such data yourself.
   - UAC prompt inside the Sandbox: press Yes (administrator of the Sandbox only).
   - **Be patient:** installers are slow in the Sandbox. While a progress bar or "Preparing…" shows, keep calling `computer_observe` with `wait_ms: 10000` for up to 5 minutes. Do not start a second installer meanwhile.
   - Before saying it failed or is not finished, look once more after 10 s (finish screen, the program's window, its files). Report what the screen shows, not a guess.

## Step 3. The result comes back through `Shared` (when `task_submit`'s description mentions `Desktop\Shared`)
The user does not have to ask for this. By default everything you install or save goes under the Sandbox desktop folder **`Shared`** = `C:\Users\WDAGUtilityAccount\Desktop\Shared`:
- **Installer with a destination folder** (7-Zip, Notepad++, most setup wizards): on the "Destination folder" / "Install location" page, replace the path with `C:\Users\WDAGUtilityAccount\Desktop\Shared\<program name>` (e.g. `...\Desktop\Shared\7-Zip`), then install.
- **Zip / portable program:** unzip into `Desktop\Shared\<program name>`.
- **Files** (PDF, document, result): "Save as" into `Desktop\Shared`.
- **Check it:** after installing, open `Desktop\Shared\<program name>` in Explorer and start the program from there (e.g. `7zFM.exe`) with `computer_observe` showing its window. That is the proof it is installed; it will not be in the Start menu or `Program Files`.
- This PC sees a copy within seconds (`%USERPROFILE%\SecureCUA\codex-work\shared\<session>`, where nothing can run). When the session ends the Host scans it and asks the person at this PC: **Yes** = copied to `Downloads\SandboxShared\<session>` (usable on this PC), **No** = deleted, no answer = held where it is (not runnable). Do not decide this for the user.
- **Cannot come along:** an installer with no folder choice, or one that adds services or drivers (Tailscale, VPNs, antivirus, printer drivers). Install it normally and say it stays in the Sandbox only. Do not copy `C:\Program Files\...` into `Shared` afterwards.
- Skip `Shared` only when the user says the Sandbox only ("샌드박스에서만", "내 PC엔 필요 없어").

## Report when done
Say, in Korean, in a few lines:
- **Step 1:** source URL (or the Downloads file used) and where it is now: `Downloads\<name>`
- **Step 2:** the file run in the Sandbox (`Input\<name>`), what the screen showed (finish page, program window)
- **Step 3:** where it is installed in the Sandbox (`Desktop\Shared\<program>`), that this PC already has a copy, and that saying "끝내" will ask whether to keep it in `Downloads\SandboxShared`
- Anything you could not check.

## Rules
- `POLICY_DENIED`: do not work around it; tell the user.
- Sandbox does not start, download fails, install fails: report what you saw and **leave the Sandbox open**. Never tell the user to install or run the file on this PC instead.
- **Do not call `session_stop` on your own**, not even after a failure. Call it only when the user says "끝내" / "종료해". The Host asks the person at this PC first, then asks about the `Shared` files.
- **Files in the inbox at the end:** they are deleted when the session ends. Files from step 1 are already in Downloads (`downloads_copy`, or the original of an `inbox_import`), so nothing the user has is lost; do not ask about them. Only if a received file has no Downloads copy and the user wants it kept, call `inbox_finish` with `keep` = its name before `session_stop`.
- After `session_stop`, this conversation cannot start a Sandbox again (`SESSION_TERMINATED`): tell the user to open a new conversation.
- The real protection is the Host (tool policy, deny-execute folders, read-only inbox, scan before anything is kept). This text tells you the procedure.
