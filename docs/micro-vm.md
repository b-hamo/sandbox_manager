# 명시적 micro_VM 백엔드 (#57)

## 작업 전 앱 준비

`prepare_apps(session, names)`는 현재 세션에서만 앱을 준비한다. 알려진 Guest 실행 경로를 먼저 확인하고,
없으면 Host PATH·Windows App Paths·등록된 설치 경로에서 찾아 필요한 앱 폴더를 세션 package로 복사한다.
여러 파일인 프로그램은 감시 공유 밖에서 ZIP을 완성해 한 파일로 게시한다. Guest는 ZIP 해시·경로·크기,
펼친 파일별 해시를 검사하고 해당 VM의 전용 폴더에 연결한다. Host 원본과 전달 공유는 읽기 전용이며
Guest에서 펼친 앱 사본은 Guest 소유다. 수천 개 파일을 관리 서비스로 개별 전송하지 않는다.
메모장과 cmd/PowerShell, 기존 프로그램 목록(python/git/gcc/node/go/java/zig/dotnet/ruby/php)을 지원한다.
Host 원본은 수정하지 않으며 Guest 해시 검사가 끝나야 PREPARED다. 이는 파일 준비 증거이고 앱 실행 성공은 아니다.
세션 잠금과 지속 상태로 중복 준비를 막으며 불확실한 응답/이전 PREPARING은 UNKNOWN으로 남겨 재시도하지 않는다.
Host 설치본이 없으면 NEEDS_INSTALL, 알려진 제공 방법이 없으면 UNSUPPORTED다. Agent는 작업 목적에 맞는
공식 설치 파일을 기존 inbox로 받아 같은 Guest 안에서 설치·확인한다. 임의 Host 경로나 명령을 앱 이름으로 실행하지 않는다.
micro_VM은 관리 서비스의 해당 세션 package와 Guest 프로세스 영수증을 사용한다. WSB 전용 share/exec를 호출하지 않는다.
AutomaticVmManager도 고정된 backend로 앱 준비와 기존 program_share를 전달한다.
`tests/test_app_preparation.py`는 Guest 재사용, 의존 파일/해시, 재조회, 응답 소실, 설치 필요 결과를 검사한다.

2026-11-10 실제 micro_VM `94c38f45-2a7c-4275-b3cc-56dd7556730f`에서 PowerShell 재사용,
메모장 제공, Python 3.13.16 묶음 전송/Guest 검사를 확인했다. Python 준비는 약 105초였고
Router RUN-0001은 `GUEST_PROCESS_VERIFIED`, exit 0, execution_count 1을 기록했다.
한글 TXT 46바이트는 Defender 검사 뒤 ART-000001로 반출됐다. 증거는
`C:\SCRPAppLab\app-prepare-bundle-20261110`의 요청별 결과·versions.json에 보존한다.
Manager 회귀검증은 260 passed, 1 skipped, 8 subtests passed다.
이 VM의 메모장 추가 GUI 시험은 실행창을 열지 않은 입력이 거부돼 실행하지 않았다.
이전 별도 VM의 메모장 GUI 검증과 이번 Python 실행 검증을 구분한다.

MicroVmManager(root, package=..., vm_root=..., relay=...)는 기존 SandboxSession과 bootstrap을 사용하는 앱 창 백엔드다.
AutomaticVmManager(..., allow_app_window=True)는 WSB 시작 잠금 아래 점유 여부를 확인하여 비었으면 WSB,
점유됐으면 앱 창 micro_VM을 선택한다. 전체 바탕화면 요구에 자동 적용하지 않는다.
조회·잠금·시작 실패에서 다른 VM으로 재시도하지 않으며 인스턴스당 한 세션만 배정한다.
tests/test_automatic.py는 가짜 VM 백엔드로 점유·오류·고정 배정을 검사한다.
전체 workspace/container GUID, 소유 SID, 세션 label, root를 시작 시 고정하고
관리·종료 전에 재검사한다. 다른 VM을 선택하거나 이동시키지 않는다.
기존 Runner 사본·cert·bootstrap·inbox 검사와 세션별 경로를 유지한다. 공유는 읽기 전용이다.
Guest 입력은 C:\RunnerInput, 진단은 C:\RunnerLogs이며 사용자 이름에 의존하지 않는다.

### 기본 메모장 제공

앱 창 VM의 최초 bootstrap 게시 전에 Host Windows System32의 클래식 `notepad.exe`와
언어별 `notepad.exe.mui`를 해당 세션의 `RunnerPackage/builtin-apps/notepad`에 복사한다.
minoBox `--app notepad`와 같은 파일 구성이며 Windows 폴더 전체를 공유하거나 Host에서 실행하지 않는다.
복사본 SHA-256 manifest를 남기고 Guest 준비 단계에서 다시 검사한다. 준비 실패는 Runner 시작 전
`RUNTIME_UNAVAILABLE`로 보고하며 입력을 전송하거나 재전송하지 않는다.
새 Runner의 앱 창 백엔드가 이 경로를 자신의 PATH에 추가하므로 전용 실행창과 그 자식 프로세스에서
`notepad`/`notepad.exe`를 사용할 수 있다. 이 변경은 Manager와 Runner를 함께 배포해야 한다.
재연결 시 기존 세션 사본을 사용하며 다른 세션이나 Host 원본을 수정하지 않는다.
`tests/test_builtin_apps.py`는 선택 복사, 세션 분리, 원본 부재, 해시 불일치, 기존 사본 덮어쓰기 거부를 검사한다.
2026-11-10 실제 VM `234b05aa-9d2a-4312-b082-7ddc502344e3`에서 추가 옵션 없이
`notepad.exe` 실행 및 `메모장 자동 제공 확인`/`Hello World` 입력 화면을 확인했다.
Runner SHA-256은 `07493a84a8eab91d60c398d398bab4cd4be9ec51687f43fa810884d0a0318c75`다.
초기 Guest 검사에서 PowerShell 5.1 배열 중첩 오류가 발생해 수정했고, 실제 PowerShell 파일 검사를 포함한
Manager 회귀검증은 249 passed, 1 skipped, 8 subtests passed다.
증거는 `C:\SCRPAppLab\notepad-auto2-20261110`에 있으며 메모장 저장·반출과 일반 채팅 기본 배포 반영은 별도다.

Host relay는 정확한 VM GUID와 무작위 service GUID에 묶이고 Guest는 loopback으로 접속한다.
READY는 기존 SCRP handshake/startup 검증으로 결정한다. VM Running이나 relay ready 파일만으로 판정하지 않는다.
창 상태는 UNAVAILABLE로 보고하므로 뷰어를 닫았다고 VM을 종료하지 않는다.
종료는 소유 VM의 Stopped와 cleanup_confirmed까지 확인한다.
VM reset은 지원하지 않으며 불확실한 입력을 재전송하지 않는다. Runner 재시작은 아래 원래 프로세스 종료 확인 조건을 따른다.
`fixed_vm_binding=True`는 Host가 통신 장애를 VM 종료·재생성 근거로 삼지 않도록 알린다.
이는 연결 또는 앱 복구 성공 표시가 아니다. 실제 READY·상태 동기화가 필요하다.

tests/test_micro_vm.py는 소유권 불일치·전체 GUID·창 상태·고정 연결을 검사한다.
실제 연결은 Host의 micro_vm_session_lab으로 별도 검증한다.
Windows PowerShell 하위 프로세스 시험에서는 PSModulePath에 WindowsPowerShell 모듈 경로를 사용한다.

## 2026-11-09 추가 변경·검증

자동 배정은 listener 설정 전에 WSB 점유를 확인한다. 비었으면 0.0.0.0:17443/17444,
점유됐으면 micro_VM용 loopback 동적 포트를 고른다. 시작 잠금 안에서 점유를 다시 확인하며,
그 사이 변경되면 다른 backend로 이동하지 않고 시작을 거부한다. WSB 시작 잠금은 최대 10초 기다린다.

2026-11-12 추가: Host가 고정 listener를 실제로 열다가 EADDRINUSE/WinError 10048을 확인한 경우,
자신이 일부 열어 둔 listener만 닫고 `listener_binding(wsb_listeners_busy=True)`를 호출할 수 있다.
아직 VM이 선택되지 않았고 직전 계획이 WSB일 때만 micro_VM용 loopback 동적 포트를 반환한다.
이 계획은 WSB 부팅 전/종료 뒤에도 유지해 두 번째 Host가 고정 포트를 다시 사용하지 않게 한다.
다른 프로세스를 종료하거나 포트를 강제로 회수하지 않는다. 접근 거부, 점유 조회 오류, VM 시작 후
실패는 전환 사유가 아니다. Host #131과 함께 적용한다. `test_automatic.py`의 추가 4개 사례 및
전체 Manager 테스트 264 passed / 1 skipped / 8 subtests passed로 검증했다. 다른 PC의 실제 VM
부팅·파일 반출은 이 테스트 결과에 포함하지 않는다.
실제 최초 WSB A는 Guest에 게시한 172.29.80.1로 READY가 됐고 이후 B/C는 서로 다른 micro_VM으로 연결됐다.

`max_active`는 1..64 정수(기본 8)다. 협력하는 Host는 별도의 전역 micro_VM 시작 mutex 안에서
broker root 기록을 세고 native start의 생성 기록까지 직렬화한다. Stopped/Failed라도 cleanup_confirmed가
true가 아니면 예산에 포함한다. 손상된 기록은 시작 거부이며 기존 VM을 강제로 회수하지 않는다.
수동 CLI나 다른 root의 절대 VM 수/물리 메모리를 보장하는 OS quota는 아니다.
실제 한도 5에서 신규 요청 두 개를 동시에 보내 모두 5/5로 거부되고 기록 전체 해시 및 기존 A/B/C READY가 유지됐다.
전체 Manager 회귀 검증은 216 passed, 1 skipped, 8 subtests passed다.

설치된 비편집 Manager wheel로 B/C 연결·동시 입력·검사 반출을 확인했지만 이 wheel은 max_active 추가 전 빌드다.
한도 시험은 수정 checkout을 별도 Host에서 로드했다. 일반 비관리자 설치의 native broker 권한 연결과
Runner 프로세스 소실 후 재시작은 별도 실제 micro_VM에서 기존 A/B/C를 유지한 채 검증했다.

### 원래 Runner 프로세스 종료 후 복구

새 WorkspaceAgent의 `run_process_status`와 Agent PID·생성 시각을 확인한 경우에만 사용한다.
준비 스크립트의 실제 exit 0을 확인한 뒤 Runner를 직접 시작하고 run_id·PID·생성 시각을 저장한다.
Agent는 시작 때 보유한 원래 프로세스 핸들로 종료를 확인한다. PID 재사용·Agent 교체·기록 소실은
종료 증거가 아니므로 재시작하지 않는다. launch 요청의 응답이 소실돼도 다시 보내지 않는다.
종료가 확인되면 같은 VM/읽기 전용 공유/중계에서 새 bootstrap과 인증 generation으로 Runner만 시작한다.
`output_storage_generation`은 최초 값으로 유지한다. 새 handshake 실패도 VM 교체 근거가 아니다.
실제 원래 Runner PID 4764 종료 뒤 같은 VM에서 PID 2488이 새 인증 generation 2로 READY가 됐다.
기존 앱 PID 1056과 한글을 유지하고 추가 입력·새 Router 실행·Defender 검사 반출을 확인했다.
앱의 저장 버튼은 입력 전달 뒤 파일이 생기지 않아 별도 미통과로 기록한다.
전체 Manager 회귀검증은 이 변경 및 준비 실패 정리·시작 제한 수정 후 236 passed, 1 skipped, 8 subtests passed다.
startup_timeout_s는 1..3600초이며 Host --startup-timeout과 같은 값을 받는다. 긴 이미지 준비에서
고정 180초가 먼저 끝나는 문제를 없앤다. READY 판정이나 실제 준비 완료 조건을 완화하지 않는다.

이미지 준비 단계에서 실패한 경우 전체 workspace ID·세션 label·root 소유 SID와
`runtime_creation=not_attempted`, `cleanup_confirmed=true`, 결과의 `cleaned_up=true`를 모두 확인해야
VM 생성 없는 정리 완료로 인정한다. 누락되거나 다른 ID이면 stop을 추측해서 보내지 않는다.
