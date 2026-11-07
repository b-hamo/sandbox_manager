# 명시적 micro_VM 백엔드 (#57)

MicroVmManager(root, package=..., vm_root=..., relay=...)는 기존 SandboxSession과 bootstrap을 사용하는 앱 창 백엔드다.
WSB 자동 대체 정책은 아직 없다. 전체 workspace/container GUID, 소유 SID, 세션 label, root를 시작 시 고정하고
관리·종료 전에 재검사한다. 다른 VM을 선택하거나 이동시키지 않는다.
기존 Runner 사본·cert·bootstrap·inbox 검사와 세션별 경로를 유지한다. 공유는 읽기 전용이다.
Guest 입력은 C:\RunnerInput, 진단은 C:\RunnerLogs이며 사용자 이름에 의존하지 않는다.

Host relay는 정확한 VM GUID와 무작위 service GUID에 묶이고 Guest는 loopback으로 접속한다.
READY는 기존 SCRP handshake/startup 검증으로 결정한다. VM Running이나 relay ready 파일만으로 판정하지 않는다.
창 상태는 UNAVAILABLE로 보고하므로 뷰어를 닫았다고 VM을 종료하지 않는다.
종료는 소유 VM의 Stopped와 cleanup_confirmed까지 확인한다.
Runner 재시작/VM reset은 아직 지원하지 않으며 불확실한 입력을 재전송하지 않는다.

tests/test_micro_vm.py는 소유권 불일치·전체 GUID·창 상태·고정 연결을 검사한다.
실제 연결은 Host의 micro_vm_session_lab으로 별도 검증한다.
Windows PowerShell 하위 프로세스 시험에서는 PSModulePath에 WindowsPowerShell 모듈 경로를 사용한다.
