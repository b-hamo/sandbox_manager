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

## 하는 일 / 안 하는 일

| 한다 | 안 한다 (다른 담당) |
|---|---|
| 세션 작업 폴더, Runner 패키지(exe·시작 스크립트) 준비 | token 발급, bootstrap 내용 작성 (Host) |
| .wsb 생성: 읽기 전용 매핑 2개만, 클립보드·프린터·오디오·비디오 끔 | 인증서·개인 키 생성 (Host) |
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
| `sandbox_manager/config.py` | .wsb 생성과 매핑 안전 검사, Guest 시작 스크립트 |
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
| 자동 시험 | 77개 통과·1개 건너뜀 (`test_manager` 64: 동시 호출·주인 없는 Sandbox 회수·Runner 재시작·Sandbox 재설정·사용자 파일 넣기 포함, `test_isolation` 13. 링크 거부 시험은 symlink 권한이 없는 PC에서 건너뜀) |
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

- 매핑은 이 세션 작업 폴더 안의 두 폴더만, 항상 읽기 전용. 쓰기 가능 매핑을 만드는 옵션이 없다
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
