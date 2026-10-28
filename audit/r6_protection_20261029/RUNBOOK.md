# 다음 실제 장의 PAPER 관측과 배포 경계

이번 작업은 개발 및 모의 검증까지다. LIVE 설치/기동/당일 금융 승인/실주문을 실행하지 않았다. 설치 후보도 사용자 승인 전까지 적용하지 않는다.

## 배포 후보

후보 `R6-PROTECT-OBS-20261029-239d14a4fd41`, 기존 LIVE `R6-KT00018-20261028-7d3c3d949105`.
Work의 `r6_protection_20261029/candidate`에 payload, manifest, 파일별 `CHANGES.patch`, 승인 필수 적용 도구가 있다. 같은 Work 폴더의 `DEPLOYMENT.md`에 정확한 Windows 경로와 검사/백업/롤백 명령이 있다.

정확히 11파일만 교체한다: `automation/decisions.py`, `automation/entry_evidence.py`, `automation/trade_review_capture.py`, `automation/engine.py`, `automation/runner.py`, `automation/protection_safety.py`, `automation/position_health.py`, `core/engine.py`, `operator_service.py`, `operator_ui_service.py`, `runtime/RELEASE_IDENTITY.json`.

개발 소스 전체를 LIVE에 덮어쓰지 않는다. 후보에는 C2 또는 미배포 A/B·공통 경제성 연구가 없다. 설정·장부·계좌 승인·자본·HARD_LOCK을 보존한다. 정상 STOP·프로세스 부재·봇 장부 무보유/무활성주문·전 소스/보호 해시를 재확인하고, 외부 백업 후 승인 파일만 원자 교체한다. 설치 후에도 STOP 유지다. 저장 장부의 검사는 증권사 실제 계좌 확인을 대신하지 않는다.

롤백은 이번 설치 영수증의 11파일만 기존 KT00018 소스로 복구한다. 새 모듈 두 개는 설치 후 해시가 일치할 때만 제거한다. 현재 장부/설정/승인/시세 기록은 되돌리지 않는다. 파일 또는 전제가 달라졌으면 중단하고 다시 확인한다.

## 개발 회귀

```text
python -B -m unittest tests.test_protection_account_outage tests.test_paper_funnel_counts tests.test_kt00018_rate_budget tests.test_auth_recovery tests.test_observed_paper_20261027 tests.test_c2_box_retest tests.test_c2_causality_and_exits tests.test_strict_benchmark_pipeline tests.test_trade_review_capture tests.test_automation tests.test_exit_plan tests.test_daily_risk_override tests.test_position_data_health tests.test_operator_interface
```

고립 fixture의 임시 LIVE 정책은 실계좌 승인이 아니다. 변경 전 개발 무결성 목록을 쓰면 안전검사에서 fixture가 거절되므로, 검증한 개발 소스용 목록을 사용한다. 운영 식별 파일을 바꾸거나 검사를 끄지 않는다.

## 실제 장 시작 전 — 사용자 조작과 자동 확인 분리

1. 배포를 원하면 별도로 승인한다. 승인 전에는 현재 운영본을 그대로 둔다.
2. 정식 GUI·키움 인증·당일 승인·시작은 사용자가 기존 절차로 수행한다. PAPER 관측 도구가 이를 대신하지 않는다.
3. 실제 실행 RELEASE/105개 해시, 설정, 계좌·미체결 대조, Worker/기록기 상태를 읽기 전용으로 확인한다. 승인 전 구버전 운영 시 관측 계약의 필수 항목이 없으면 UNKNOWN이다.
4. 운영이 안정적일 때만 아래 읽기 전용 관측을 하나 실행한다. 다른 계좌/API 접속을 만들지 않는다. CPU/큐/지연이 증가하면 PAPER 관측부터 중지하고 운영의 주문·보유 보호를 우선한다.

```text
python -B tools/observe_paper.py --follow-root <LIVE_ROOT> --config <FROZEN_CONFIG_JSON> --automation <FROZEN_AUTOMATION_JSON> --initial-cash <EXPLICIT_PAPER_SCENARIO_CASH> --day <KST_TODAY> --seconds 120 --output <NEW_PRIVATE_OBSERVER_DIR>
```

기본 관측은 현재 EOF부터 시작하고 낮은 프로세스 우선순위/처리 예산을 사용한다. 추가 키움 API 호출은 없다. 이 시간/예산은 실제 PC의 검증된 성능 보장이 아니다. 장중 `--evaluate-each-trade`나 고부하 전체 재생을 병행하지 않는다.

## 동일 실제 입력의 비교 — 장후 오프라인

기존 도구로 append-only 기록의 byte 범위·SHA256를 동결한다. 원본을 옮기거나 삭제하지 않는다. 동일 설정 스냅샷과 동일 수신 입력을 사용한다.

```text
python -B tools/observe_paper.py --events <FROZEN_JOURNAL> --config <FROZEN_CONFIG_JSON> --automation <FROZEN_AUTOMATION_JSON> --initial-cash <SCENARIO_CASH> --day <DAY> --output <NEW_ABC_DIR>
python -B tools/c2_paper.py --events <SAME_FROZEN_JOURNAL> --config <SAME_CONFIG_JSON> --automation <SAME_AUTOMATION_JSON> --initial-cash <SAME_SCENARIO_CASH> --day <SAME_DAY> --output <NEW_C2_DIR>
```

ABC는 native 보고 신호와 연구 엔진 판단의 차이를 유지한다. C2는 비활성 연구 경로이며 주문 전 심사·체결은 봉인된 FakeBroker에서만 실행한다. 운영과 같은 판단이라고 가정하지 않는다. 동일 수신 ID와 당시 계좌·WATCH·호가 증거가 없으면 실제 주문 적격으로 분류하지 않는다.

각 `RESULT.json`의 `measurement_funnel`, `observed_authority_blocks`, `source_counts`, `source_manifest`, `recorder_after_close`, fills/closed trade를 함께 검토한다. `native_reported_preintent_pass_NOT_revalidated`는 원본의 보고일 뿐이며 검증된 주문 가능 수가 아니다. 실제 문맥을 검증하고 PAPER 엔진이 접수한 `observed_context_PAPER_accepted_buys`와 구분한다.

기록 손실 카운터는 공유 기록기 전체 범위다. 대상 종목/시간의 수신 패킷 손실로 환산하지 않는다. 중복 평가·분할 체결·재시작의 구조 ID·서로 겹치는 표본을 독립 거래로 합산하지 않는다. 체결이 없거나 필요한 자료가 없으면 비용 후 순손익과 기대값은 UNKNOWN/NOT_ESTIMABLE이다.

## 보호 장애 복구

GUI에 `긴급 — 보호청산 불가`가 보이면 사유·검증 수량·예약 매도·미보호 잔량을 확인한다. 금융 활성화나 강제 주문으로 우회하지 않는다. UNKNOWN/취소 불명의 원주문을 재전송하지 않는다. 네트워크 복구 후 완전한 계좌·체결·미체결 대조와 신선한 호가를 확보한다. 일반 HALT가 남으면 기존 정식 해제 절차의 수동 검토가 필요하다.

보호 전용 권한이 회복돼도 신규매수의 오래된 계좌 조건은 풀리지 않는다. 원장 저장 실패는 진단 로그 실패와 다르며 전송을 중단해야 한다. 스톱지정가 API의 자동 등록이나 OCO는 이번 구현에 없으므로 현재 보호 방식인 것처럼 설명하지 않는다.
