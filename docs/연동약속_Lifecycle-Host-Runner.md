# 연동 약속 v0.2 — Lifecycle ↔ Host ↔ Runner

작성: 배주한·최정우. v0.1 2026-09-29 초안 → **v0.2 2026-09-29 이준원 답변 반영.**
목적: 이준원 Host(`host_control`), Runner(`sandbox_runner`), 우리 Lifecycle(Sandbox Manager)을 이을 때 누가 무엇을 언제 하는지 정한다.

기준 코드
- Host: `github.com/b-hamo/host_control` `feat/19-observation-upload` (`6055cc6`)
- Runner: `github.com/b-hamo/sandbox_runner` `develop` (2026-09-28 병합분), 직접 빌드 SHA-256 `3f1c20f6…2027e2`
- Lifecycle: `github.com/b-hamo/sandbox_manager` (배주한·최정우, 아래 1절)

**검증:** 2026-09-29 KISIA PC에서 위 Host·Runner를 수정 없이 Sandbox Manager로 이어 `sender.py --demo broker` 전체 왕복 PASS. READY 17.6초, Broker 9호출(6 성공·3 의도 거부), 종료 확인 1.86초. 단, Host 쪽 연결은 시험 도구(`tools/e2e_host.py`)가 대신 했다. 이걸 `mcp_server.py`가 직접 하게 만드는 게 다음 작업이다. 시험 도구는 `sandbox_manager/tools/e2e_host.py`.

---

## 1. 합의된 것

| # | 내용 | 결정 | 담당 |
|---|---|---|---|
| 1 | 저장소 위치 | ~~`host_control` 안 `lifecycle/`~~ → **새 저장소 `b-hamo/sandbox_manager`** (배주한 결정, 이준원에게 알림 필요). Host는 `pip install git+https://github.com/b-hamo/sandbox_manager.git`로 설치. 서로 리뷰 | 전원 |
| 2 | Sandbox 켜는 시점 | **`task_submit` 때** (명세 흐름: 작업 등록 → Sandbox 생성) | 이준원 |
| 3 | 함수 호출 | `mcp_server`가 아래 2절 순서대로 부름 | 이준원 |
| 4 | 인증서 SAN | `start()`가 돌려준 주소를 SAN에 넣음 | 이준원 |
| 5 | 인증서 재생성 | 주소가 바뀌면 새로 만듦 | 이준원 |
| 6 | 인증서 버그 | 유효기간 1일 이하 남으면 127.0.0.1용으로 말없이 재생성되던 문제 수정 | 이준원 |
| 7 | bootstrap `host` | `null` 대신 `start()`가 준 주소를 직접 넣음 | 이준원 |
| 8 | bootstrap 지문 | `host_certificate_sha256` 추가 | 이준원 |
| 9 | 시작 제한시간 | 명세대로 **120초** 통일 | 이준원 |
| 10 | README | Python 3.13.15, 17444 방화벽 안내 | 이준원 |

## 2. 호출 순서 (확정)

```
Agent: task_submit
  Host  → s = sm.prepare(session_id, runtime_id, generation, runner_exe)
  Host  → address = sm.start(s)                       # Sandbox 켜기. 보통 3~5초, 재부팅 직후 첫 번째는 1분 이상
  Host  : 인증서(SAN=address) 준비, 서버(17443·17444) 열기, token 발급
  Host  : write_bootstrap(s.bootstrap_path, ...)      # host=address, host_certificate_sha256 포함
  Host  → sm.publish_bootstrap(s, cert_pem)           # 인증서·주소 전달, 준비 표시 파일을 마지막에 → Guest가 Runner 실행
  Runner: 접속 → HELLO
  Host  : Startup Verification → READY
  Host  → sm.mark_ready(s)                            # 다 쓴 token 파일 삭제
  ... Action ...
Agent: session_stop  /  Startup 실패  /  긴급 종료
  Host  : TERMINATE 전송 (긴급이면 생략)
  Host  → sm.stop(s, reason, emergency=...)           # wsb list로 사라짐 확인해야 TERMINATED
  Host  → sm.cleanup(s)
Host 프로세스 종료(Codex가 끔)
  Host  → 살아 있는 세션이 있으면 sm.stop(s, "USER_STOP") → sm.cleanup(s)
```

- import: `from sandbox_manager import SandboxManager, SandboxManagerError`
- `sm.start()`, `sm.stop()`은 수 초~수십 초 막히는 함수라 asyncio 루프에서는 `await asyncio.to_thread(sm.start, s)`로 부른다
- 종료 사유: `TASK_COMPLETE`, `USER_STOP`, `SECURITY_VIOLATION`, `TIMEOUT`, `RUNTIME_ERROR`
- 오류는 `SandboxManagerError.code`: `INVALID_ARGUMENT` / `RUNTIME_UNAVAILABLE` / `RUNTIME_START_FAILED`. `start()`가 실패하면 Sandbox는 이미 꺼져 있다(FAILED)
- `publish_bootstrap`에는 Host가 bootstrap에 실제로 넣은 인증서 PEM을 넘긴다(개인 키가 섞이면 거부)

### 2-3. 사용자 파일 넣기 (2026-10-04 Lifecycle 추가, Host 연결 필요)

```
Agent: task_submit (+ 넣을 파일 경로)
  Host  : 넣어도 되는 파일인지 판단 (정책: 비밀번호·브라우저 저장 정보·시스템 폴더 등 거부)
  Host  → s = sm.prepare(session_id, runtime_id, generation, runner_exe, input_files=[경로, ...])
  Host  : Agent에게 s.guest_input_paths (예: C:\UserFiles\견적서.xlsx) 를 알려 줌
  ... 이후는 2절과 같음 (start → publish_bootstrap → READY → mark_ready → ...)
```

- 원본은 연결하지 않고 복사본만, Sandbox 안 `C:\UserFiles`에 **읽기 전용**. 고친 결과를 꺼내는 건 Artifact Broker 경로(여기 아님)
- Sandbox Manager가 거부하는 것(`INVALID_ARGUMENT`): 없는 파일, 폴더, 바로가기·링크, 같은 이름 두 개, 파일당 50 MiB·합계 200 MiB 초과
- 기록에는 이름·크기·SHA-256만 남고 원래 경로는 남지 않음
- **Host가 할 일**: `task_submit`에 파일 칸 추가, 넣어도 되는지 판단, `prepare(..., input_files=...)`로 넘기기, Agent에게 Sandbox 안 경로 알려 주기

### 2-2. Host가 살아 있을 때 복구 (2026-10-02 Lifecycle 추가, Host 연결은 이준원)

```
Runner 연결 끊김 / 응답 없음, Sandbox는 살아 있음 (sm.is_running(s) == True)
  Host  → sm.restart_runner(s, generation+1)       # Guest 안 옛 Runner 종료, 시작 스크립트가 새 bootstrap 대기
Sandbox 먹통 (Heartbeat UNRESPONSIVE 등) 또는 restart_runner 실패
  Host  → address = sm.reset_sandbox(s, generation+1)   # 옛 Sandbox 끄기(wsb list 확인) → 새 Sandbox, 새 주소
그다음 (둘 다)
  Host  : 같은 session_id, 새 generation으로 세션 등록·새 token, (reset이면 새 주소로 인증서) → write_bootstrap
  Host  → sm.publish_bootstrap(s, cert_pem) → Runner 접속 → Startup Verification → READY → sm.mark_ready(s)
```

- 두 함수를 합쳐 세션마다 최대 3번(`SandboxManager(max_restarts=...)`로 바꿈). 넘으면 `RUNTIME_UNAVAILABLE` → `stop()`
- **Host가 할 일:** 언제·몇 번 부를지(보안 위반이면 재시작 안 함 등), 새 generation 세션을 받아 주기(옛 generation 메시지는 계속 거부), 끊길 때 처리 중이던 Action은 UNKNOWN(자동 재전송 금지, 상태 조회 뒤 새 action_id로 재시도 승인), Agent에게 "재시작 중" 알림
- `restart_runner()` 실패(`RUNTIME_START_FAILED`, 상태 `RESTARTING`) → `reset_sandbox()` 또는 `stop()`. `reset_sandbox()`가 옛 Sandbox를 못 끄면 `RUNTIME_UNAVAILABLE`, 상태 그대로 → `stop(emergency=True)`
- 실제 시험: `tools/recovery_e2e.py` (host_control feat/19, PR #25 `0690845` 둘 다 PASS)

### 2-1. task_submit 응답 시간 (주의)
READY까지 약 18초, **재부팅 직후 첫 Sandbox는 1분 이상**(네트워크 스위치 생성 51초 관찰). `task_submit`이 READY까지 붙잡고 있으면 Codex 도구 호출 시간 초과에 걸릴 수 있다.
**확정 (2026-09-29 이준원):** `task_submit`은 Sandbox 켜기를 시작만 하고 바로 `PREPARING`으로 답한다. 준비 전 `computer_*` 호출은 "아직 준비 안 됨, 다시 시도"로 답하고, Codex는 `wait_ms`로 기다렸다 다시 시도한다(이준원 확인).

## 3. 책임 나누기

| 일 | 담당 |
|---|---|
| 세션 등록, token, bootstrap 내용, 인증서·개인 키, Startup Verification, READY, Heartbeat, TERMINATE | Host (이준원) |
| 작업 폴더, Runner 패키지, .wsb(읽기 전용 매핑 2개), `wsb start`·창 열기, 켠 뒤 주소 확인, 방화벽 규칙 검사, 인증서·주소·준비 표시 전달, Guest 인증서 자동 신뢰, token 파일 삭제, 종료 확인, 정리, 주인 없는 Sandbox 회수, Runner 재시작·Sandbox 재설정 실행 | Lifecycle (배주한) |
| HELLO, GUI 캡처·입력, 스크린샷 업로드, Output 감시 | Runner |
| 방화벽 규칙 설치·재부팅 후 재적용 (관리자 권한) | 설치 과정 (미정, 아래 5절) |

## 4. 인증서: "CA 인증서 설치" 창에 대해

- 이 창은 사람이 Guest 안에서 인증서를 승인하던 방식(Runner 담당 검증 때)에서 뜬다
- **Sandbox Manager 방식에서는 뜨지 않는다.** Guest 시작 스크립트가 Runner 실행 전에 관리자 권한(`WDAGUtilityAccount`)으로 `certutil -addstore -f Root`를 실행한다. 0.15초, 창 없음(2026-09-29 실험, 전체 왕복도 이 방식)
- 그래서 지금 방식은 **SAN에 주소가 맞기만 하면** 동작한다(1절 4·5번)
- **개선안 (Runner 담당과 논의):** Runner가 Windows 신뢰 저장소 대신 bootstrap 인증서 지문(`host_certificate_sha256`)만 비교(pinning)하면 SAN·신뢰 등록이 필요 없어지고 주소가 바뀌어도 된다. 급하지 않음. 바뀌면 Lifecycle은 `certutil` 단계를 빼면 된다

## 5. 남은 결정

| # | 내용 | 현황 |
|---|---|---|
| A | 방화벽 규칙: 설치 과정에 넣는 방법, **재부팅마다 풀리는 문제**(Sandbox 어댑터가 새로 생겨 규칙이 옛 어댑터에 묶임) | Lifecycle은 `start()`에서 검사하고, 안 맞으면 Sandbox를 끄고 고치는 명령을 알려 줌. 자동 재적용 방법은 팀 결정(progress 미결 14·20) |
| B | Sandbox → LAN·인터넷 노출 | Windows 기능으로 못 막음. 팀 결정(미결 16) |
| C | Smart App Control 켜진 PC에서 서명 없는 Runner 차단 | 끄고 재부팅하면 됨. 제품은 코드 서명 필요한지 결정(미결 19). 시연 PC 확인 |
| D | 끊겼을 때 재시작 | **Lifecycle 쪽 완료 (2026-10-02):** `restart_runner()`·`reset_sandbox()`, 2-2절. Host 쪽 연결(언제 부를지, 새 generation 받기, Action UNKNOWN, Agent 알림)은 이준원 |
| E | Runner pinning 전환 | 4절 개선안 |
| F | ~~task_submit 응답 방식~~ | 확정, 2-1절 |

## 6. 실행 전 조건 (PC마다)

1. Windows 11 Pro, Windows Sandbox 켜짐, `wsb` 명령
2. Python 3.13.15
3. **Smart App Control 끄고 재부팅** (Host에서 꺼도 재부팅 전에는 Sandbox 안이 켜진 상태로 남음)
4. `sandbox_runner.exe` 빌드 (MSYS2 UCRT64)
5. 관리자 PowerShell로 방화벽 규칙 4개 (17443·17444 허용, 나머지 Sandbox→Host 차단). 재부팅 뒤에는 첫 Sandbox를 켠 다음:
   `Get-NetFirewallRule -DisplayName "SCRP PoC*" | Set-NetFirewallRule -InterfaceAlias "vEthernet (Default Switch)"`
6. 작업 폴더는 OneDrive 밖(기본 `%LOCALAPPDATA%\SecureCUA\sandbox-manager`). OneDrive 안이면 Lifecycle이 거부
