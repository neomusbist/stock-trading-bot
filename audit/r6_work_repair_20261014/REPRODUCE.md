# 최종 코드 재현 절차

루트 인계 문서의 최종 개발 커밋과 GitHub 게시 상태를 먼저 확인한다. 기존 R6 `2affd6d`에서 이어진 독립 수정이며 운영 R5 재개나 main 병합 명령이 아니다.

```
git branch --show-current
git rev-parse HEAD
git status --short
python -B -m unittest tests.test_r6_fast_repair tests.test_r6_shadow_runtime tests.test_r6_readiness_repair tests.test_r6_execution_repair tests.test_fast_momentum tests.test_shadow_tick_logger -q
```

PowerShell 로컬 환경의 Python은 `$env:USERPROFILE\Documents\PythonEnvs\stock-trading-bot\Scripts\python.exe`다. Qt 검사는 offscreen으로 실행한다. 개인 증거 폴더 `r6-work-repair-private-20261014/run_checks.py`는 `--root <검사할 checkout> --full`을 지원한다. 의존성 경로는 기존 `test_dependencies`, 운영의 vendor/gui_vendor를 읽기만 참조하고 네트워크 연결을 막는다. 결과 파일은 개인 증거 폴더에 기록되며 운영 runtime을 쓰지 않는다.

```
git worktree add --detach "<새 빈 재현 폴더>" "<최종 커밋>"
python -B "<개인 증거 폴더>/run_checks.py" --root "<재현 폴더>" --full
python -B -m audit.r6_work_repair_20261014.symbol_audit "<원본 archive>" --official-names "<보존된 OFFICIAL_KRX_NAMES.json>" --out "<재현 출력>"
python -B -m audit.r6_work_repair_20261014.replay "<원본 archive>" --out "<재현 출력>"
```

원본은 운영 폴더 `runtime/automation-production/archive/events.jsonl.20261014-145249-570438.7232.32e27bce`다. SHA-256은 `0c698c6ef4c7d9815c83a081d5d81d66205588e120c2e07caaf3f10f2050b50c`. 스크립트는 다른 해시를 거절한다. 원본은 Git에 복사하지 않았다. KRX 당일 명칭 증거는 개인 증거의 `OFFICIAL_KRX_NAMES.json`/`krx-corp-list.html`이며 공개 결과 JSON에도 필요한 명칭·URL·캡처 시각·해시만 들어 있다.

최종 CSV/JSON을 개발 결과와 byte SHA-256으로 비교한다. 자료를 확보하지 못하면 시장 재현 미완료로 기록한다. 실제 FAST 재생은 별도 raw TRADE/BOOK이 필요하므로 이 snapshot proxy의 재현 성공을 실제 전략 수익성 PASS로 바꾸지 않는다.

Git CLI 인증이 불가능해 승인된 GitHub 연결로 게시했다. 게시 commit SHA가 로컬 원본과 다를 수 있으나 각 tree를 원본과 검증한다. `BASE_COMMIT_MAP.json`이 6개 기존 이력의 대응표다. 이번 수정 브랜치의 최종 commit/tree 검증은 루트 인계 문서의 최신 기록을 따른다. force push·main 갱신·운영 브랜치 갱신을 수행하지 않는다.
