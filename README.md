# sandbox_manager — Windows Sandbox Manager

배주한 담당. Secure CUA Runtime의 Runtime/Lifecycle Manager 중 **Windows Sandbox 부분**이다(WBS 3.1~3.3, 4.8, 5.9).
Host(`b-hamo/host_control`)와 Runner(`b-hamo/sandbox_runner`) 사이에서, 사람 손 없이 Sandbox를 켜고 끄고 정리한다.
Python 3.13.15, 표준 라이브러리만 쓴다.

**2026-09-29: 실제 Host + 실제 Runner와 전체 왕복 성공** (`tools/e2e_host.py`, Host·Runner 소스 수정 없음).

저장소: `github.com/b-hamo/sandbox_manager` (배주한 관리). Host(`host_control`)는 패키지로 설치해서 부른다.

```
pip install git+https://github.com/b-hamo/sandbox_manager.git
```
```python
from sandbox_manager import SandboxManager, SandboxManagerError
```
호출 시점·순서는 `docs/연동약속_Lifecycle-Host-Runner.md` 2절(Sandbox는 `task_submit` 때 켠다).

## 다른 PC에서 코덱스로 쓰기: 처음 설치 (2026-10-15)

**GitHub 코드만 받아서는 코덱스에 적용되지 않는다.** 코덱스는 PC마다 `~/.codex/config.toml`(Host 등록)과 `~/.codex/skills/`(Skill)를
읽기 때문에, 아래를 PC마다 한 번씩 해야 한다. Host가 막는 것(정책, 확인 창, 실행 금지, 도구 설명에 붙는 규칙)은 Host를 같은 옵션으로
돌리면 어느 PC에서나 똑같이 적용되고, Skill에만 있는 안내(다운로드 폴더 먼저 보기, 설치 5분 기다리기, `input-check.html` 확인 등)는
Skill을 복사해야 들어간다.

1. **Windows 준비:** 아래 "실행 전 조건"의 1~3 (Windows Sandbox 켜기, Smart App Control 끄고 재부팅, 관리자 PowerShell로 방화벽 한 번)
2. **받기·빌드** (저장소 3개를 한 폴더에, 예: `%USERPROFILE%\Documents`)
   ```powershell
   git clone https://github.com/b-hamo/host_control.git
   git clone https://github.com/b-hamo/sandbox_runner.git
   git clone https://github.com/b-hamo/sandbox_manager.git   # Skill·준비 스크립트용
   cd host_control; py -3.13 -m venv .venv; .venv\Scripts\python -m pip install -r requirements.txt; cd ..
   ```
   Runner는 `sandbox_runner` README대로 MSYS2 UCRT64에서 빌드 → `sandbox_runner\build\sandbox_runner.exe`
3. **받는 폴더·공유 폴더 준비** (관리자 아님, 다시 돌려도 됨)
   ```powershell
   powershell -ExecutionPolicy Bypass -File sandbox_manager\tools\setup_inbox.ps1
   ```
   `%USERPROFILE%\SecureCUA\codex-work\inbox`·`\shared`를 만들고, 그 아래에서는 이 PC가 아무것도 실행하지 못하게 한다(마지막 줄 `OK: ...`가 나와야 함)
4. **Skill 복사**
   ```powershell
   New-Item -ItemType Directory -Force "$env:USERPROFILE\.codex\skills" | Out-Null
   Copy-Item -Recurse -Force sandbox_manager\codex_skill\secure-sandbox-run "$env:USERPROFILE\.codex\skills\"
   ```
   Skill이 바뀌면(`git pull` 뒤) 다시 복사한다
5. **코덱스에 Host 등록:** `%USERPROFILE%\.codex\config.toml`에 추가하고 `<나>`를 자기 사용자 이름으로 바꾼다(경로는 `/`로)
   ```toml
   [mcp_servers.scrp]
   command = 'C:/Users/<나>/Documents/host_control/.venv/Scripts/python.exe'
   args = ['C:/Users/<나>/Documents/host_control/host/mcp_server.py',
           '--runner-exe', 'C:/Users/<나>/Documents/sandbox_runner/build/sandbox_runner.exe',
           '--inbox', 'C:/Users/<나>/SecureCUA/codex-work/inbox',
           '--save-downloads',
           '--shared', 'C:/Users/<나>/SecureCUA/codex-work/shared',
           '--agent-guard']
   default_tools_approval_mode = "approve"
   startup_timeout_sec = 60
   tool_timeout_sec = 120
   ```
   | 옵션 | 켜지는 것 |
   |---|---|
   | `--inbox` | 다운로드 폴더 파일·인터넷 파일을 Sandbox로 넣기, 끝날 때 받는 폴더 비우기 |
   | `--save-downloads` | 인터넷에서 받은 파일을 다운로드 폴더에도 저장(브라우저처럼), 끝나도 남김 |
   | `--shared` | Sandbox 바탕화면 `Shared` → 이 PC(실행 금지) → 끝날 때 검사 후 확인 창(예 남기기·아니요 지우기·무응답 보관) |
   | `--agent-guard` | AI가 혼자 Sandbox를 끝내거나 비슷한 파일을 또 받을 때 확인 창 |
6. **확인:** 코덱스를 완전히 껐다 켜고 `codex mcp list`에서 `scrp`가 `enabled`인지 본다. 새 대화에서 "7-Zip 포터블 받아서 Sandbox에서 실행해 보고 내 PC에도 남겨줘"

### Sandbox 안에서 Win+R 사용

Sandbox Manager의 `.wsb`와 Guest 시작 스크립트는 Win+R을 비활성화하지 않는다. 기존 `POLICY_DENIED`는 `host_control/host/policy.py`의 입력 정책에서 발생했다. Win+R을 허용하는 Host 정책 `POL-0.1.1` 이상으로 업데이트하고 새 세션을 시작하면, READY 뒤 `computer_observe`를 거쳐 `computer_hotkey(keys=["win", "r"])`로 Sandbox 안 실행 창을 열 수 있다. Sandbox Manager만 업데이트하면 기존 Host의 차단은 해제되지 않는다. 아래 2026-09-29 시험 표의 Win+R 거부는 변경 전 정책의 기록이다.

## 하는 일 / 안 하는 일

| 한다 | 안 한다 (다른 담당) |
|---|---|
| 세션 작업 폴더, Runner 패키지(exe·시작 스크립트) 준비 | token 발급, bootstrap 내용 작성 (Host) |
| .wsb 생성: 읽기 전용 매핑만(기본 2개, 선택: 사용자 파일 사본·받는 폴더), 클립보드·프린터·오디오·비디오 끔 | 인증서·개인 키 생성 (Host) |
| `wsb start` + 창 열기(LogonCommand 실행 조건) | HELLO 검사, Startup Verification, READY 판정 (Host) |
| **켠 뒤** Host 주소(vSwitch IPv4) 확인, LAN 경로면 거부 | Heartbeat, Action 전달 (Host·Runner) |
| 방화벽 규칙이 지금 어댑터에 묶여 있는지 검사 | GUI 캡처·입력 (Runner) |
| 인증서·주소·준비 표시 파일 전달, Guest에서 인증서 자동 신뢰 → Runner 실행 | 방화벽 규칙 변경 (관리자 권한, 설치 과정) |
| READY 뒤 token 파일 삭제 | |
| 종료 후 `wsb list`로 사라짐 확인해야만 TERMINATED | |
| 정리(패키지·bootstrap·.wsb 삭제, state.json은 감사 기록으로 남김) | |

## Host가 부르는 방법

```python
from sandbox_manager import SandboxManager

m = SandboxManager()                                   # 작업 폴더: %LOCALAPPDATA%\SecureCUA\sandbox-manager (OneDrive 금지)
s = m.prepare(session_id, runtime_id, generation, runner_exe)
address = m.start(s)                                   # Sandbox를 켜고, Runner가 접속할 Host 주소를 돌려줌
# Host: SAN에 address를 넣은 인증서로 서버를 열고, 세션을 등록해 bootstrap을 s.bootstrap_path에 쓴다
m.publish_bootstrap(s, host_cert_pem)                  # 인증서·주소를 넣고 준비 표시 파일을 마지막에 → Guest가 Runner 실행
# Host: Runner 접속 → Startup Verification → READY
m.mark_ready(s)                                        # 다 쓴 token 파일 삭제
# ... 작업 ...
# Host: TERMINATE 전송 (긴급이면 생략)
m.stop(s, "TASK_COMPLETE")                             # 또는 emergency=True
m.cleanup(s)
```

### 사용자 파일 넣기 (선택)

```python
s = m.prepare(session_id, runtime_id, generation, runner_exe,
              input_files=[r"C:\Users\me\Documents\견적서.xlsx"])   # 넣어도 되는 파일인지는 Host가 먼저 판단
s.guest_input_paths   # ['C:\\UserFiles\\견적서.xlsx'] → Agent에게 "이 경로의 파일을 열어"라고 알려 줌
```

- 원본을 연결하지 않고 **복사본**을 세션 작업 폴더에 두고 Sandbox 안 `C:\UserFiles`에 **읽기 전용**으로 보여 줌. Sandbox 안에서 만들기·고치기·지우기 모두 실패(실제 Sandbox 확인)
- 거부: 없는 파일, 폴더, 바로가기·링크, 같은 이름 두 개(대소문자 무시), 파일당 50 MiB·합계 200 MiB 초과 → `INVALID_ARGUMENT`, 작업 폴더를 만들기 전에 거부
- `state.json`에는 이름·크기·SHA-256만 남기고 **원래 경로는 남기지 않음**(민감할 수 있음). `cleanup()`이 복사본 삭제
- `reset_sandbox()`로 새 Sandbox가 떠도 같은 파일이 다시 보임
- **결과 파일 꺼내기는 여기 없음** — Artifact Broker(검사 후 반출) 경로. 쓰기 가능한 연결 폴더는 만들지 않음
- 파일을 안 넘기면 지금과 똑같음(연결 폴더 2개)
- **바뀐 파일 거부 (2026-10-08):** `register_input(path, source=URL)`로 다 받아진 파일을 등록하면 그때의 크기·SHA-256·출처가 고정되고(`InputFile`), `prepare(input_files=[등록한 것])`가 복사본이 다르면 거부한다. 받는 중 이름(`.crdownload`·`.part` 등)은 등록 거부
- Sandbox 안에서는 지킴이(`input_watch.ps1`)가 `C:\UserFiles`의 파일을 바탕화면 `Input`으로 복사하고 크기·SHA-256을 다시 비교한다(아래 "받는 폴더"와 같은 지킴이)

### 받는 폴더(inbox): Agent가 받은 파일을 Sandbox에서 실행 (선택, 2026-10-08)

```python
from sandbox_manager.inbox import InboxWatcher, check_inbox_root

inbox = check_inbox_root(r"C:\Users\me\SecureCUA\codex-work\inbox")   # 먼저 tools/setup_inbox.ps1 한 번
s = m.prepare(session_id, runtime_id, generation, runner_exe, inbox=inbox)
watcher = InboxWatcher(m, s); watcher.start()          # 1초마다 m.scan_inbox(s)
# ... start → publish_bootstrap → READY ... (그대로)
watcher.stop(); m.stop(s, "TASK_COMPLETE"); m.cleanup(s)   # cleanup은 받는 폴더를 지우지 않음
```

- 고정된 Host 폴더 **자체**를 Sandbox `C:\Inbox`에 **읽기 전용**으로 연결한다(배주한 결정: 사본 아님, 승인 없음). Sandbox가 켜진 뒤 들어온 파일도 보인다(실제 Sandbox 확인)
- 폴더 조건(`check_inbox_root`, 아니면 `INVALID_ARGUMENT`): 절대 경로, 링크 아님, 드라이브 최상위·사용자 프로필 자체 아님, 문서·바탕화면·다운로드·사진·AppData·`.ssh`·`.codex` 등 사용자 폴더 안 아님, OneDrive 아님, 작업 폴더와 안 겹침
- `scan_inbox(s)` (InboxWatcher가 부름):
  - 다 받아진 파일만 목록(`package\inputs.json`)에 올린다: 받는 중 이름 아님, 아무도 쓰고 있지 않음, 크기·시각이 2초 동안 그대로, 파일당 50 MiB·합계 200 MiB·200개 이하. 하위 폴더는 4단계까지
  - **junction·symlink·hard link가 보이면 링크만 지운다**(가리키는 원본은 그대로). 켤 때 이미 있으면 `start()`가 거부
  - 바뀐 파일은 새 SHA-256으로 다시 올린다. 이벤트: `INBOX_FILE_REGISTERED` / `_CHANGED` / `_GONE` / `_SKIPPED`, `INBOX_LINK_REMOVED`, `INBOX_HARDLINK_REMOVED`
- Sandbox 안 지킴이(`input_watch.ps1`, 시작 스크립트가 Runner보다 먼저 띄움, 1초마다): 목록의 파일을 바탕화면 `Input`에 복사하고 **복사 전후 크기·SHA-256**을 목록과 비교 → `Desktop\input-check.txt`·`.html`에 `OK`/`FAIL`, 다르면 사본 삭제. 실행은 하지 않는다(실행은 Agent가 SCRP GUI 도구로). Sandbox엔 메모장이 없어 결과는 Edge로 여는 html도 만든다
- 받는 폴더에서 Host가 실행하지 못하게: `tools/setup_inbox.ps1`(관리자 아님)이 폴더 안 파일마다 **Everyone 실행 거부** + OWNER RIGHTS(만든 사람도 권한을 못 바꿈)를 건다
- 내려받기는 Host 몫이다(명세). 시연용으로 `tools/inbox_download.py`(https·공개 주소만, 50 MiB, zip 경로 검사)와 host_control을 고치지 않고 붙이는 `tools/host_with_inputs.py --inbox`가 있다. 정식 연결은 이준원(제안서 2026-10-08)
- Codex에게 절차를 알려 주는 Skill: `codex_skill/secure-sandbox-run/SKILL.md` (`~/.codex/skills/`에 복사)

### Sandbox 창은 뒤에서 열림 (2026-10-12)

- `start()`는 창을 열어야 Guest가 로그인하고 Runner가 켜진다(`wsb connect`). 그 창이 약 1초 뒤 맨 앞으로 와서 사용자가 하던 작업이 끊겼다
- 이제 `connect` 전에 사용자의 맨 앞 창을 기억해 두고, Sandbox 창이 스스로 포커스를 가져가면 **창을 맨 뒤로 보내고 포커스를 사용자 창에 돌려준다**(`sandbox_manager/window.py`). 끄려면 `SandboxManager(window_behind=False)`
- **Sandbox가 켜져 있는 동안 계속** 지켜본다(전에는 처음 20~30초만): 포커스 없이 사용자 창 **위로 다시 올라와도** 뒤로 보낸다(Codex 앱에서 발견: 포커스는 돌아왔는데 0.4초 뒤 창만 다시 올라와 화면을 가림)
- **사용자가 직접 Sandbox 창을 클릭하거나 작업 표시줄·Alt+Tab으로 고르면 그대로 둔다**(`user_chose()`: 그 창 위에서 마우스 버튼이 실제로 눌렸는지로 판단, 커서만 올려져 있는 건 아님)
- 시험: Guest 안에서 탐색기·설치 창·Edge·UAC 창을 열어도 Sandbox 창이 앞으로 오지 않음(포커스 0~0.1초)
- **최소화는 하지 않는다:** 최소화하면 Guest 화면 그리기가 멈춰(실험: 밝은 화면 713 → 60) AI가 화면을 못 본다
- 실측(JH PC, 실제 Host·Runner `mcp_e2e.py` 3회): Sandbox가 포커스를 가진 시간 약 0.1초(전에는 끝날 때까지 약 14초), 화면 보기·클릭·입력 모두 통과

### 이미 받아 둔 파일을 받는 폴더로 복사 (2026-10-10)

```python
from sandbox_manager import import_to_inbox

r = import_to_inbox("ZoomIt.zip", inbox, extract=True)   # 원본 폴더 기본값: 사용자의 다운로드 폴더
# r["files"] = [{"name": "ZoomIt\\ZoomIt.exe", "size": ..., "sha256": ...}], 원본은 그대로 남음(복사)
```

- **파일 이름만** 받는다. 원본 폴더(기본: 다운로드 폴더, Known Folder로 찾음) **바로 안의 파일**만 가져올 수 있다. Agent가 속아서 문서·키 같은 다른 Host 파일을 Sandbox(인터넷 열림)로 넘기지 못하게 하려는 것
- 거부(`POLICY_DENIED`): 폴더 경로·`..`·드라이브·`:`(스트림)·장치 이름(CON, NUL 등), hard link(다른 파일의 또 다른 이름), zip 안 `..`·절대 경로·링크
- 거부(`INVALID_ARGUMENT`): 없는 파일, 폴더, junction·symlink, 받는 중 파일(`.crdownload` 등)·쓰고 있는 파일, 50 MiB 초과, 앞뒤 공백·끝 점, 복사하는 동안 바뀐 파일
- 임시 폴더에 해시하며 한 번 복사 → 원본이 그 사이 바뀌었으면 거부 → 받는 폴더에 새 이름으로(덮어쓰지 않음, `이름-2`). 이후는 받는 폴더와 같음(InboxWatcher 목록 → Guest 지킴이)
- 지금은 Agent가 Host 도구 `inbox_import`(`tools/inbox_download.py`, `host_with_inputs.py --inbox`가 붙임, `--import-from`으로 원본 폴더 변경)로 부른다. 나중에 Host가 직접 부르게 바꿔도 검사는 이 함수 한 곳이라 그대로다

### 세션이 끝날 때: 받는 폴더 유지/삭제 (2026-10-11)

```python
from sandbox_manager import clear_inbox, finish_inbox

finish_inbox(inbox, keep=["ZoomIt"])   # 남길 것만 다운로드 폴더로 복사, 나머지는 받는 폴더에서 삭제
clear_inbox(inbox)                     # 기본값: 전부 삭제 (받는 폴더 자체는 남김)
```

- **기본은 삭제**(배주한 결정). 남길 것을 고르면 **다운로드 폴더**로 간다. Sandbox에서 먼저 설치·실행해 보고 괜찮은 설치 파일을 PC에서 이어서 쓰기 위한 것
- 남길 이름은 `inbox_download`·`inbox_import`가 알려 준 이름 그대로(`ZoomIt`처럼 폴더면 폴더째). `..`·절대 경로·링크·hard link는 거부하고, **하나라도 거부되면 아무것도 지우지 않는다**
- 옮기지 않고 **복사**한다: 옮기면 받는 폴더의 "실행 금지" 권한이 따라가서 PC에서 실행이 안 된다(실제 확인). 복사본에는 **인터넷에서 받은 파일 표시**(Zone.Identifier, ZoneId=3)를 붙여 PC에서 실행할 때 Windows가 한 번 더 묻게 한다. 같은 이름은 `이름-2`, 다운로드 폴더에 똑같은 파일이 이미 있으면(`inbox_import`로 가져온 원본) 그대로 둔다
- 시연용 Host(`host_with_inputs.py`): Agent 도구 `inbox_finish(keep)`, Host가 세션을 끝낼 때(`cleanup`) 받는 폴더를 비움, 새 Host가 켜질 때 지난 세션이 남긴 파일도 비움(Codex 창을 갑자기 닫은 경우)

### 세션 공유 폴더: Sandbox에서 만든 파일을 내 PC에도 (선택, 2026-10-15)

```python
from sandbox_manager import SharedWatcher, finish_shared, make_share

share = make_share(r"C:\Users\me\SecureCUA\codex-work\shared", session_id)  # 세션마다 새 빈 폴더
s = sm.prepare(..., shared=share)          # Sandbox 안 C:\Shared 로 쓰기 가능하게 연결
SharedWatcher(sm, s).start()               # 1초마다 링크가 생기면 지움
# ... 세션 ...
r = finish_shared(share, decide=ask)       # 끝: 검사 → 깨끗하면 ask(r)가 keep/delete/hold → 다운로드로 복사/삭제/보관
```

- **포터블 프로그램·PDF 같은 파일용.** 서비스·드라이버를 까는 설치형(Tailscale 등)은 파일만 와서 PC에서 동작하지 않는다
- Sandbox 안에서는 바탕화면 `Shared`에 설치·저장·압축 해제한다. **사용자가 말하지 않아도 기본**(설치 폴더를 `Shared\<프로그램>`으로, 10/15 배주한 결정). 남길지는 끝날 때 확인 창에서. Guest의 `shared_mirror.ps1`이 3초마다 `C:\Shared`로 복사(지우지는 않음, 링크 건너뜀, 500 MiB 넘는 파일 제외)
- PC 쪽 폴더에서는 **실행 금지**(`setup_inbox.ps1`이 건 deny-execute가 상속). `make_share`가 whoami 복사본으로 실제로 막히는지 확인하고, 안 막히면 거부. 실험(10/15): Guest는 이 폴더에 링크를 못 만들고, ACL을 바꿔도 PC 쪽엔 반영 안 됨. 실행 금지는 Guest에서도 걸려서 Guest는 자기 바탕화면 쪽에서 실행한다
- 끝날 때 `finish_shared`: 링크 제거 → 크기(2 GiB, 2만 개) → **Microsoft Defender 검사** → 깨끗하면 새 파일로 복사(인터넷 표시 붙임) 후 공유 폴더 삭제. 위협(BLOCKED)·검사 오류(QUARANTINED)·너무 큼(BLOCKED)이면 실행 금지 상태 그대로 남김
- **남길지 지울지(10/15 배주한 결정):** 깨끗한 것만 `decide`에 물어본다. `keep` → 다운로드로 복사(EXPORTED), `delete` → 삭제(DELETED), `hold`·답 없음·오류 → 공유 폴더에 실행 금지 상태로 보관(HELD). Host는 PC 확인 창으로 묻는다(예=남기기, 아니요=지우기, 취소·45초 무응답=보관). `decide`를 안 주면 남기기
- 실제 확인(10/15): Guest가 저장한 exe·PDF가 6초 안에 PC에 보임, PC에서 실행 거부, Guest에서는 실행됨, 끝나고 Downloads로 복사·실행됨

### Host가 살아 있을 때 복구 (언제 할지는 Host가 정함)

```python
# Runner가 죽었거나 대답이 없음, Sandbox는 멀쩡함 → 같은 Sandbox에서 Runner만 다시
m.restart_runner(s, s.generation + 1)                  # Guest 안 옛 Runner 종료, 시작 스크립트가 새 bootstrap을 기다림
# Sandbox 전체가 먹통 → Sandbox를 끄고(wsb list로 확인) 새로 켬
address = m.reset_sandbox(s, s.generation + 1)         # start()처럼 새 Host 주소를 돌려줌 (주소가 바뀔 수 있음 → 인증서 다시)
# 둘 다 그다음은 처음과 같음: 새 token·새 generation으로 bootstrap 쓰기 → publish_bootstrap() → READY → mark_ready()
```

- 두 함수를 합쳐 세션마다 **최대 `max_restarts`번(기본 3)**. 넘으면 `RUNTIME_UNAVAILABLE` → Host가 `stop()`
- `generation`은 지금보다 커야 한다. 처리 중이던 클릭·입력은 여기서 다시 보내지 않는다(UNKNOWN 처리·재시도 승인은 Host)
- `restart_runner()`는 Guest에 `wsb exec`로 **고정된 명령 하나**(`C:\RunnerPackage\restart.ps1`)만 실행한다. 실패(exit≠0) → `RUNTIME_START_FAILED`, 상태는 `RESTARTING`에 머묾 → Host가 `reset_sandbox()` 또는 `stop()`
- `reset_sandbox()`는 옛 Sandbox가 안 꺼지면 `RUNTIME_UNAVAILABLE`, 아무것도 바꾸지 않음
- `RESTARTING`도 살아 있는 세션이라 주인 없는 Sandbox 회수가 건드리지 않는다(켠 Host가 죽었을 때만)

상태: `PREPARED → STARTING → STARTED → RUNNING → TERMINATED`, 시작 실패 시 Sandbox를 끄고 `FAILED`. 복구 중: `RUNNING → RESTARTING → RUNNING`(Runner 재시작), `→ PREPARED → STARTING → STARTED → RUNNING`(Sandbox 재설정).
종료 사유: `TASK_COMPLETE`, `USER_STOP`, `SECURITY_VIOLATION`, `TIMEOUT`, `RUNTIME_ERROR`.
오류: `SandboxManagerError.code` = `INVALID_ARGUMENT` / `RUNTIME_UNAVAILABLE` / `RUNTIME_START_FAILED`.

### 왜 주소를 켠 뒤에 정하나
재부팅 직후에는 첫 Sandbox를 켤 때까지 `vEthernet (Default Switch)`가 없고, 새로 생기면 대역도 바뀐다(172.20.208.1 → 192.168.208.1 관찰). 그래서 Guest 시작 스크립트는 명령줄로 주소를 받지 않고 `bootstrap.ready` 표시 파일을 기다렸다가(최대 120초) 파일에서 주소를 읽는다. 매핑 폴더 안에서는 Host가 파일 이름을 바꿀 수 없어서(쓰기·삭제만 됨) 표시 파일을 마지막에 쓰는 방식으로 "다 썼음"을 알린다.

### Host 쪽 주의 (이준원 코드 연결 시)
- `host/tls.py ensure_dev_cert`는 SAN이 `127.0.0.1`뿐이고, **유효기간이 1일 이하로 남은 인증서는 말없이 새로 만든다.** Runner는 주소 검사를 하므로 `address`가 SAN에 있어야 한다
- `publish_bootstrap`에는 Host가 bootstrap.json에 실제로 넣은 `host_certificate_pem`을 넘기는 게 가장 안전하다(`tools/e2e_host.py` 참고)

## 구성

| 파일 | 역할 |
|---|---|
| `sandbox_manager/manager.py` | 본체. prepare, start, publish_bootstrap, mark_ready, is_running, stop, cleanup, load |
| `sandbox_manager/config.py` | .wsb 생성과 매핑 안전 검사, Guest 시작 스크립트, Guest 파일 지킴이(`INPUT_WATCH_PS1`) |
| `sandbox_manager/inputs.py` | 파일 등록 `register_input()` (다 받아진 파일만, 크기·SHA-256·출처 고정) |
| `sandbox_manager/inbox.py` | 받는 폴더: `check_inbox_root()`, `InboxScanner`(다 받아진 파일 목록·링크 삭제), `InboxWatcher` |
| `sandbox_manager/importer.py` | 이미 받아 둔 파일을 받는 폴더로 복사 `import_to_inbox()` (다운로드 폴더 바로 안, 이름만, 링크·hard link 거부), zip 안전하게 풀기, 세션 끝 `finish_inbox()`(남길 것만 다운로드 폴더로·나머지 삭제)·`clear_inbox()` |
| `sandbox_manager/firewall.py` | Host 방화벽 규칙이 현재 어댑터에 묶였는지, 17443·17444 허용·나머지 차단인지 검사(읽기만) |
| `sandbox_manager/wsb.py` | `wsb` CLI 감싸기 (start, running, ip, connect, stop, exec — exec는 Runner 재시작 고정 명령에만) |
| `sandbox_manager/network.py` | vSwitch 주소, Guest로 가는 Host 주소 |
| `sandbox_manager/process.py` | Sandbox를 켠 Host 프로세스가 살아 있는지 (PID + 생성 시각, 주인 없는 Sandbox 회수용) |
| `tests/test_manager.py` | 가짜 wsb·네트워크·방화벽·시계·프로세스로 42개 시험 (동시 호출, 주인 없는 Sandbox 회수 포함) |
| `tests/test_isolation.py` | 격리 우회 시도 13개: 연결 폴더 안 junction·symlink·hard link(켜기 전·켠 뒤), 폴더 바꿔치기, 사용자 폴더·브라우저 프로필 직접 지정, `..` 탈출, .wsb에 Host 경로 새는지 |
| `tools/smoke_real.py` | 실제 Sandbox로 Manager 단독 시험 (Host 없음) |
| `tools/e2e_host.py` | 실제 Host(`sender.py --demo broker`) + 실제 Runner 전체 왕복. host_control venv로 실행 |
| `tools/input_files_probe.py` | 실제 Sandbox로 사용자 파일 넣기 확인: 복사본 2개(한글 이름 포함)가 `C:\UserFiles`에 보이고 내용 일치, 만들기·고치기·지우기 실패, 정리 후 남음 없음. Host 불필요 |
| `tools/recovery_e2e.py` | 실제 Host·Runner로 복구 시험: READY 뒤 Host 강제 종료 → `restart_runner()` 후 같은 Sandbox에서 데모 완료 → `reset_sandbox()` 후 새 Sandbox에서 데모 완료 → 남은 Sandbox 없음. host_control venv로 실행 |
| `tools/mcp_e2e.py` | Codex 역할: host_control `mcp_server.py`를 MCP stdio로 불러 task_submit → observe → click → type → session_stop, 끝난 뒤 Sandbox 남음 없음 확인. host_control venv로 실행 |
| `tools/repeat_e2e.py` | `e2e_host.py`를 N회 연속 실행하고 회차 사이 남은 Sandbox·인증서·세션 파일 검사 (5.9) |
| `tests/test_importer.py` | `import_to_inbox` 15개 + 세션 끝 유지/삭제 8개(기본 삭제, 파일·폴더 유지, 덮어쓰기 안 함, 잘못된 이름이면 아무것도 안 지움, 링크 안 따라감, Host 감싸개 cleanup): 복사·원본 유지·덮어쓰기 안 함·zip 풀기, 경로·`..`·스트림·장치 이름 거부, hard link·junction 거부, 복사 중 바뀐 파일 거부, MCP 도구 모양 |
| `tests/test_guest_scripts.py` | Guest 파일 지킴이를 이 PC의 Windows PowerShell 5.1로 실제 실행(경로만 임시 폴더로): 여러 항목·하위 폴더·해시 불일치·`..` 이름 |
| `tools/setup_inbox.ps1` | 받는 폴더 만들기 + 실행 거부 권한 + Host에서 실행 안 되는지 확인 (관리자 아님) |
| `tools/inbox_download.py` | (시연용, Host 몫) MCP 도구 2개: `inbox_download`(https 공개 주소에서 받는 폴더로 내려받기, zip 풀기), `inbox_import`(다운로드 폴더의 파일을 이름으로 복사), `inbox_finish`(남길 것만 다운로드 폴더로, 나머지 삭제) |
| `tools/host_with_inputs.py` | (시연용) host_control `mcp_server.py`를 고치지 않고 감싸서 `--input`(승인 창 뒤 사본) 또는 `--inbox`(받는 폴더 + `inbox_download`·`inbox_import` 도구, `--import-from`)를 붙임 |
| `codex_skill/secure-sandbox-run/SKILL.md` | Codex Skill: 받기 → Sandbox → `input-check.html` 확인 → Sandbox 안에서 실행 절차 |
| `tools/install_firewall.ps1` / `uninstall_firewall.ps1` | 방화벽 규칙 + 재부팅 후 자동 복구 예약 작업 설치/제거 (관리자, 한 번) |

## 시험

```
python -m unittest discover -s tests -v
python tools/smoke_real.py <sandbox_runner.exe>
<host_control>\.venv\Scripts\python.exe tools/e2e_host.py
<host_control>\.venv\Scripts\python.exe tools/mcp_e2e.py --host-repo <host_control PR #25> --runner <sandbox_runner.exe>
```

| 시험 | 결과 (2026-09-29, KISIA PC) |
|---|---|
| **Sandbox에서 확인 후 PC에 남기기** (JH PC 2026-10-11, 같은 Host·Runner·Codex) | **PASS** — Codex에 "다운로드 폴더에 있는 ZoomIt.zip 을 Sandbox에서 실행해서 창이 뜨는지만 확인하고, 압축 푼 ZoomIt 폴더는 PC에 남기고 끝내줘" → `inbox_import` → Sandbox 안 4개 `OK` → `ZoomIt64.exe` 라이선스 창 확인 → `inbox_finish(keep)` → `session_stop`. `Downloads\ZoomIt`에 4개 파일·인터넷 표시(ZoneId=3), 받는 폴더 비어 있음, 남은 Sandbox 없음. 따로: 받는 폴더 안 exe는 PC 실행 거부, 다운로드 폴더로 남긴 사본은 실행됨·거부 권한 없음(whoami 사본으로 확인) |
| **다운로드 폴더에 받아 둔 파일을 Sandbox에서 실행** (JH PC 2026-10-10, 같은 Host·Runner·Codex) | **PASS** — Codex에 "다운로드 폴더에 있는 ZoomIt.zip 을 Sandbox에서 실행해줘" → Codex가 `inbox_import`(이름만, 압축 풀기) → Sandbox 안 `OK`, SHA-256 같음 → Sandbox 안에서 `ZoomIt64.exe` 실행, **라이선스 창까지 확인**. 다운로드 폴더 원본 그대로, Codex의 Host 명령은 Skill 읽기 1번뿐, Host에 ZoomIt 프로세스 없음 |
| **받는 폴더로 받아 Sandbox에서 실행** (JH PC 2026-10-08, host_control develop `7aa6f84` + Runner `0c25213`, Host 무수정, Codex `0.162.0-alpha.2`) | **PASS** — Codex에 "ZoomIt.zip 받아서 실행해줘" → `inbox_download`로 받기·압축 풀기 → Sandbox 안 `OK`, SHA-256 원본과 같음 → Sandbox 안에서 실행, **ZoomIt 라이선스 창까지 확인**(기능은 안 봄). Codex의 Host 명령 0번, Host에 ZoomIt 실행 흔적 없음. 받는 폴더 파일은 Host에서 실행 거부(Access is denied), Sandbox는 복사 후 실행 가능, Sandbox는 그 폴더에 못 씀. reset 뒤 다시 연결은 자동 시험으로만 확인 |
| 자동 시험 (2026-10-12) | 117개 중 116개 통과·1개 건너뜀(symlink 권한) (Sandbox 창 뒤로 4개 추가) |
| 자동 시험 (2026-10-11) | 113개 중 112개 통과·1개 건너뜀(symlink 권한) (세션 끝 유지/삭제 8개 추가) |
| 자동 시험 (2026-10-10) | 105개 중 104개 통과·1개 건너뜀(symlink 권한) (`import_to_inbox` 15개 추가) |
| 자동 시험 (2026-10-08) | 90개 중 89개 통과·1개 건너뜀(symlink 권한) (받는 폴더 8개, 지킴이 실제 PowerShell 1개 포함) |
| 자동 시험 (이전) | 77개 통과·1개 건너뜀 (`test_manager` 64: 동시 호출·주인 없는 Sandbox 회수·Runner 재시작·Sandbox 재설정·사용자 파일 넣기 포함, `test_isolation` 13. 링크 거부 시험은 symlink 권한이 없는 PC에서 건너뜀) |
| **사용자 파일 넣기** (JH PC 2026-10-04, `tools/input_files_probe.py`) | **PASS** — 복사본 2개가 Sandbox `C:\UserFiles`에 보이고 SHA-256 일치, 만들기·고치기·지우기 모두 실패, 정리 후 `input` 삭제 |
| **Runner 재시작·Sandbox 재설정** (JH PC 2026-10-02, `tools/recovery_e2e.py`, Runner develop `452e1b5`) | **PASS** — host_control feat/19(11칸) 44.8초, PR #25 `0690845`(12칸, `--advertise-address`) 46.6초. 재시작 후 READY까지 약 2초(같은 Sandbox), 재설정 후 새 Sandbox에서 데모 완료, 끝난 뒤 Sandbox 0 |
| **Host 강제 종료 후 회수** (JH PC, READY 직후 `taskkill /F /T` → 다음 `e2e_host.py`) | **PASS.** Sandbox가 남은 것을 확인 → 다음 `start()`가 `ORPHAN_RECLAIMED` → 종료 확인 1.67초·정리 → 새 세션 PASS, 끝난 뒤 Sandbox 0 |
| **반복 안정성 10회** (`tools/repeat_e2e.py --runs 10`, 실제 Host·Runner) | **10/10 PASS, 흔적 0.** READY 18.3~20.1초(평균 19.3), 종료 확인 1.74~1.87초(평균 1.81), 회당 약 24초. 매 회차 뒤 Sandbox·인증서·개인 키·세션 파일 남음 없음 |
| **재부팅 후 방화벽 자동 복구** (`install_firewall.ps1` 설치 → 재부팅 → 수동 명령 없이 `e2e_host.py`) | **PASS.** 재부팅 직후 규칙은 옛 어댑터에 묶여 무효, Sandbox 주소 대역도 바뀜(172.31.208.1). 감시 작업이 새 어댑터에 다시 묶었고 READY 21.0초, 종료 확인 1.9초 |
| 실제 Host + Runner 전체 왕복 (`e2e_host.py`) | **PASS.** 켜기 2.5초, 주소 확인 4.1초, 준비 표시 8.6초, READY 17.6초(Host 검증 8.9초), Broker 데모 9호출(6 성공·3 의도 거부: 작업 등록 전 관찰, 화면 밖 클릭, Win+R), 종료 확인 1.86초, 정리 실패 0 |

## 실행 전 조건 (PC마다)

1. Windows Sandbox 켜짐, `wsb` 명령 있음
2. **Smart App Control 꺼짐 + 끈 뒤 재부팅.** 켜져 있으면 서명 없는 Runner exe가 Sandbox 안에서 차단된다. Host에서 꺼도 재부팅 전까지 Sandbox 안은 켜진 상태로 남는다
3. **방화벽: 관리자 PowerShell에서 처음 한 번만**
   ```
   powershell -ExecutionPolicy Bypass -File tools\install_firewall.ps1 -PythonExe "<Host를 돌리는 python.exe 전체 경로>"
   ```
   - 규칙 4개(17443·17444는 그 Python만 허용, 나머지 Sandbox→Host TCP·UDP 차단)를 만들고,
     **재부팅 후 자동 복구 예약 작업**(`SecureCUA Sandbox Firewall Rebind`, SYSTEM)을 등록한다
   - 왜: 재부팅하면 Sandbox 네트워크 어댑터가 새로 생겨(첫 Sandbox를 켤 때) 규칙이 옛 어댑터에 묶인 채 무효가 된다. 예약 작업이 5초마다 확인해서 새 어댑터에 다시 묶는다. `start()`도 최대 20초 기다려 준다
   - 명령은 예약 작업 안에 들어 있고 스크립트 파일로 두지 않는다(일반 사용자가 SYSTEM 권한 명령을 바꿀 수 없게)
   - 규칙은 꺼진 상태로 만들어지고, Sandbox 어댑터에 묶일 때 켜진다(차단 규칙이 Wi-Fi·LAN에 걸리지 않게)
   - Python 경로 확인: `python -c "import sys; print(sys.executable)"` (venv라면 그 venv를 만든 원래 python.exe)
   - 지우기: `tools\uninstall_firewall.ps1`
   - 설치 안 했으면 `start()`가 Sandbox를 끄고 고치는 방법을 알려 준다
4. 참고: Claude 데스크톱 앱 안에서 실행하면 `%LOCALAPPDATA%`가 앱 전용 폴더(`...\Packages\Claude_...\LocalCache\Local`)로 바뀌어 저장된다. 동작에는 문제없다

## 지켜야 할 규칙 (코드가 막는 것)

- 매핑은 이 세션 작업 폴더 안의 폴더만, 항상 읽기 전용. 예외 둘: `inbox=` 받는 폴더(역시 읽기 전용), 그리고 **`shared=` 세션 공유 폴더 하나만 쓰기 가능**(2026-10-15, 아래). 둘 다 사용자 폴더·OneDrive 거부, 링크는 계속 지움
- 작업 폴더가 OneDrive 안이면 거부 (살아 있는 token이 동기화되지 않게)
- Host 인증서에 개인 키가 섞여 오면 거부
- LogonCommand는 고정 문자열. 주소는 파일로 전달하고 Host·Guest 양쪽에서 IPv4인지 검사
- Guest가 LAN 주소로 Host에 닿는 경우 거부 (방화벽이 못 거르는 NAT 경로)
- 한 PC에 Sandbox 하나. 이미 떠 있으면 `RUNTIME_UNAVAILABLE`
- **주인 없는 Sandbox 회수**: `start()`는 먼저 기록(state.json)을 보고, 켠 Host 프로세스가 죽었는데 안 끝난 세션(STARTING·STARTED·RUNNING)을 끄고 정리한다(`RUNTIME_ERROR`, 이벤트 `ORPHAN_RECLAIMED`). 켠 Host가 살아 있거나 확인이 안 되면 건드리지 않는다. 기록이 없는 Sandbox(손으로 켠 것 등)는 끄지 않고 거절한다. `reclaim_orphans()`로 따로 부를 수도 있다
- 준비 표시 파일은 bootstrap.json이 완전한 JSON이 된 뒤, 인증서·주소 다음에 마지막으로
- `wsb list`에서 사라진 것을 못 보면 TERMINATED로 기록하지 않는다
- Guest 안 명령 실행(`wsb exec`)은 고정 문자열 `restart_command()` 하나뿐. 바깥에서 명령을 넘길 방법이 없다(Generic RPC 금지)

## 남은 것

- Host 연결 고리: 이준원이 `mcp_server`에서 부르기로 함(`task_submit` 때 켜기). 인증서 SAN·재생성·1일 버그도 이준원이 수정 예정
- `task_submit` 응답 방식: READY까지 18초, 재부팅 직후 1분 이상 → 바로 답하고 `runtime_get_state`로 확인하는 방식 제안(이준원 결정)
- Runner pinning 전환(Runner 담당과 논의): 되면 Guest의 `certutil` 단계 제거 가능
- 방화벽 규칙을 설치 과정·재부팅 후 자동으로 다시 묶는 방법 (관리자 권한, 미결 14·20)
- Host 쪽 복구 연결(이준원): 언제 `restart_runner()`/`reset_sandbox()`를 부를지, 새 generation 세션 받아 주기, 진행 중 Action UNKNOWN 처리, Agent 알림
- 네트워크 격리(미결 16): Guest outbound 차단 등
- 받는 폴더 Host 연결(이준원, 제안서 2026-10-08): `--inbox`, `prepare(inbox=)`·InboxWatcher, `inbox_download` 도구를 Broker 정책·감사 안으로. 승인 여부·내려받기 주소 범위는 같이 정함
- Codex 창을 갑자기 닫으면 Sandbox가 남음(다음 `start()`가 회수). 바로 정리하는 감시 프로세스는 아직
