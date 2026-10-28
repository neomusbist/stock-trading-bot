# 실행 및 검증 절차

LIVE는 변경하지 않는다. 아래 명령은 개발 폴더에서 실행하며 추가 증권사 접속이나 실주문을 만들지 않는다. 기존 운영본에는 공통 비용 계산·연구 전략이 설치되지 않았다. 연구 재생 결과와 운영 판단을 구분한다.

## 준비된 실제 수신 자료 재생

PowerShell에서 개발 저장소를 현재 폴더로 선택한다. 출력 폴더는 매번 새 이름으로 지정한다. 아래 경로는 이 작업에서 보존한 사설 증거이며 Git에는 계좌·원본 로그를 넣지 않는다.

```powershell
$Python = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$Work = "$env:USERPROFILE\.codex\.chatgpt-projects\g-p-6aa791b2321c81918edf968fa94ccef5\r6_oct27_paper_evidence"
$Frozen = "$Work\private\CAPTURE_NEO.jsonl"
$Config = "$Work\private\OBSERVED_config.json"
$Automation = "$Work\private\OBSERVED_automation.json"
$NewOutput = "$Work\private\replay-$(Get-Date -Format yyyyMMdd-HHmmss)"
& $Python -B tools/observe_paper.py --events $Frozen --config $Config --automation $Automation --initial-cash 70420 --day 2026-10-27 --output $NewOutput
```

- 기본은 기존 판단 전이 또는 과거 상세 표본 도착 시점에만 진입을 재평가한다. 실제 소비가 확인된 체결은 계속 적재하고 PAPER 보유 보호는 처리한다.
- `--evaluate-each-trade`는 장후 오프라인 연구에서만 추가한다. 장중 `--follow-root`에서는 명시적으로 금지된다. 그 결과의 평가 횟수를 실제 LIVE 평가·독립 기회 수로 바꾸어 부르지 않는다.
- 입력은 실제 인지/처리 시각으로 병합한다. 완료 일봉·참조봉이 나중에 도착하면 과거로 소급하지 않는다. 처리시각이 없는 receiver-only 체결로 웜업하지 않는다.
- 설정 파일은 모든 필드를 명시한 typed JSON이어야 한다. 동결한 운영 설정에 없는 연구용 필드의 기본값은 별도 `OBSERVED_RESEARCH_DEFAULTS.json`에 보존됐다. 새로운 진입 옵션·FAST·B 목표 확장은 OFF다. 설정 이름이 같다는 이유만으로 서로 다른 버전을 같은 LIVE 논리라고 부르지 않는다.
- `RESULT.json`, `evidence/paper-book-final.json`, 수신/판단 JSONL, `source_manifest`, 종료 후 `recorder_after_close`를 함께 보존한다. 단순 종료 코드 0은 관측 완전성이나 적격 신호 존재를 뜻하지 않는다.

## 다음 실제 수신의 읽기 전용 PAPER 관측

```powershell
$Live = "$env:USERPROFILE\Desktop\주식자동매매"
$NewOutput = "$Work\private\follow-$(Get-Date -Format yyyyMMdd-HHmmss)"
$Day = Get-Date -Format yyyy-MM-dd
& $Python -B tools/observe_paper.py --follow-root $Live --config $Config --automation $Automation --initial-cash 70420 --day $Day --seconds 600 --output $NewOutput
```

이 명령은 프로그램을 켜거나 금융 승인을 하지 않는다. 기존 파일의 EOF부터 읽으며 REST/WebSocket을 새로 연결하지 않는다. 설정은 실행 전에 실제 해당 일자와 대조한 전체 typed 사본을 사용해야 한다. 앞의 10월 27일 설정을 미래 운영 사실로 가정하지 않는다. 실제 잔액 70,420원을 고정 운용값으로 강제하는 기능도 아니다. 실제 당시 계좌 증거가 없으면 사설 PAPER 계좌의 숫자로 매수를 허용하지 않는다.

현재 설치본은 새로운 ENTRY_DECISION_EVIDENCE가 없으므로 최종 계좌·권한 정보가 부족한 구간은 UNKNOWN으로 남는다. 새 기록기를 설치하기 전에는 legacy 표본을 이용해 일부 전략 판단을 재구성할 수 있을 뿐이다. 시작 이전 체결·기존 보유를 생성하거나 이월하지 않으므로 PAPER 웜업과 실제 LIVE 웜업도 다를 수 있다.

PAPER BUY에는 다음이 모두 필요하다.

1. 같은 종목·체결 시각·수신 시각·누적량 및 같은 호가 수신 시각.
2. 신선한 실제 WATCH 자격과 native 최종 필수 게이트 PASS. 관측 전용 FAIL은 필수 FAIL로 오인하지 않는다.
3. 실제 당시 양수 수량과 계좌 현금·승인 자본·잔여 위험·보유한도·동일 비용/위험 프로필.
4. source의 선택된 전략과 READY 상태, 진입·무효화·목표 가격이 PAPER 계산과 일치.
5. 연구 엔진 자체의 신선도·잔량·가격·중복·위험 게이트도 통과.

통과 이후에만 FakeBroker에 영속 INTENT → 접수 → 실제 수신 L1 범위 안의 가상 체결 → 포지션 보호 → 청산을 수행한다. PAPER 접수·가상 체결은 실제 브로커 접수·체결이 아니다. 후속 실제 시세가 없으면 임의의 마지막 가격으로 청산하지 않는다.

출력의 신호·주문·청산을 같은 수신 ID와 구조 지문으로 연결한다. MFE/MAE는 해당 가상 보유시간 안에서만 산출한다. 실측 PAPER 체결 0건이면 기대값·승률·MFE/MAE는 없거나 UNKNOWN이며 0%로 계산하지 않는다.

## 성능 및 손실 확인

- Windows idle priority, 기본 1초 polling, poll당 50ms soft CPU budget. 강제 실행시간 상한이나 개장 처리능력 인증은 아니다.
- 진입 연구는 기본적으로 판단 전이에서만 수행한다. 큐/회전 backlog, 읽기 오류, 처리시각 UNKNOWN, 예산 초과를 결과에서 확인한다. 미처리 자료를 신선하다고 재표시하지 않는다.
- 기록기 기본 예산은 파일 32MiB, 하루 64MiB, 합계 256MiB, 여유 32GiB다. 참조 CSV 사본은 수신 시각과 SHA별로 불변 보존하고 별도 64MiB/run 상한 및 32GiB 여유 가드를 둔다.
- 운영 PC 개장 부하의 추가 영향은 미검증이다. 자료가 적은 4초 EOF smoke로 처리량을 인증하지 않는다. 장중에는 큰 과거 재생을 병행하지 않는다.
- 운영 주문·체결·원장의 동기 영속성은 손대지 않았다. 새 진단 큐가 넘치면 누락을 기록하며 주문 권한을 만들지 않는다.

## 회귀검사

```powershell
& $Python -B -m unittest tests.test_ab_entry_economics tests.test_observed_paper_20261027 tests.test_strict_benchmark_pipeline tests.test_live_ready_20261021 tests.test_entry_funnel tests.test_r6_readiness_repair tests.test_v13_risk_identity
```

`test_A_resistance_is_fixed_by_structure_trigger_not_decision_price`의 원래 기대값은 유지한다. 필수 사설 원장 없이 skip되는 검사는 실계좌 장부 검증 PASS로 세지 않는다.

## 관측 전용 패키지와 롤백

별도 Work 산출물 `observation_overlay`에 정확히 6개 파일, manifest, `apply_overlay.py`가 있다. Git의 `OVERLAY_MANIFEST.json`은 개인 경로를 제외한 검토용 해시 목록이다. 연구 저장소 전체를 LIVE에 복사하지 않는다.

이번 작업은 설치 승인을 포함하지 않는다. 적용 전 별도 명시적 승인이 필요하며, 정상 STOP·대상 프로세스 부재·봇 무보유·미해결 주문 없음·소스/설정/종속 파일 해시를 다시 검증해야 한다. 승인 이후 외부 백업·교체·사후검증을 수행하더라도 STOP을 유지한다. 기동/금융 활성화/실주문은 포함하지 않는다.

rollback은 외부 receipt를 지정하여 승인 범위의 소스와 RELEASE만 복원하고, 새 모듈은 설치 해시가 일치할 때에만 제거한다. 장부·계좌·당일 승인을 이전 사본으로 되돌리지 않는다. 이후 독립 변경이 있으면 억지 복원하지 않고 중단한다.
