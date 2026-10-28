# 재현 및 인계

## 현재 작업 범위

브랜치 `tech-stability/market-open-0900`, 기준 `78188479ceaf0186963dba29fab69ccb5304e96d`. 새 커밋은 이 문서의 Git 이력으로 식별한다. 원격 푸시/브랜치 변경/병합은 수행하지 않는다.

LIVE는 `R6-KT00018-20261028-7d3c3d949105`, STOP 유지다. 개발 전체를 LIVE에 복사하지 않는다. 소스/설정/장부/승인 변경·기동·주문은 별도 승인 대상이다.

## 개발 검증

```text
python -B -m unittest tests.test_c2_box_retest tests.test_c2_causality_and_exits tests.test_observed_paper_20261027 tests.test_strict_benchmark_pipeline tests.test_kt00018_rate_budget tests.test_auth_recovery tests.test_trade_review_capture tests.test_automation tests.test_exit_plan tests.test_daily_risk_override
```

종전 C 양성 fixture가 비용 후 거절되는지, 새 C2 합성 BUY/NO_BUY/청산, PAPER 위장 실브로커 차단, HARD_LOCK, 오래된 계좌, 누락 호가/잔량, 사후 저항 이동, 미래 참조, 본전/구조 추적 정책을 검사한다. 실제 계좌나 네트워크를 사용하지 않는다. 기존 고립 fixture의 임시 LIVE 정책은 실계좌 승인과 무관하다.

개발용 무결성 목록이 소스 변경 전이면 고립 fixture도 `UNVERIFIED_CODE_BLOCKED_FOR_LIVE`로 중단된다. 검증 대상 개발 소스의 테스트용 식별 목록을 갱신해 사용하며 운영 식별 목록을 덮어쓰거나 검사를 끄지 않는다.

## 실제 동결 자료 PAPER

```text
python -B tools/c2_paper.py --events <FROZEN_JSONL> --config <EXPLICIT_CONFIG_SNAPSHOT_JSON> --automation <EXPLICIT_AUTOMATION_SNAPSHOT_JSON> --initial-cash 70420 --day 2026-10-27 --output <NEW_PRIVATE_OUTPUT_DIR>
```

출력 폴더는 새 폴더여야 한다. 입력은 기존 WATCH/체결/호가/참조/계좌 증거와 실제 가용 시각을 담아야 한다. 줄 단위 처리 시각/수신 연결이 없으면 UNKNOWN이다. 실제 계좌가 누락됐다고 임의로 계좌 스냅샷을 생성하면 안 된다. 비용/위험/포지션 프로필을 명시해야 하며 FAST/B확장/D/E2는 OFF로 둔다.

`--exit-policy INITIAL_STRUCTURE`, `AFTER_1R_COST_BREAKEVEN`, `CONFIRMED_PIVOT_TRAIL`은 별도 새 출력으로 재생한다. 기본은 `BASELINE`이다. 동일한 진입 시각·가격·수량이 일치하는 사례만 paired exit로 비교한다. 미체결/관측 종료 포지션을 마지막 가격으로 강제 청산하지 않는다. 호가/깊이·지연·손실 구간은 그대로 보존한다.

`RESULT.json`의 `c2_episodes`, `c2_events`, `c2_stage_counts`, `source_counts`, `observed_authority_blocks`, `source_sha256`, `source_manifest`, `recorder_after_close`를 함께 읽는다. stage 0은 성립을 확인한 건수 0이며, 미평가된 단계는 UNKNOWN/NOT_EVALUATED다. 카운터를 독립 거래 수로 합산하지 않는다.

실제 당시 계좌·WATCH·권한·최종 시장 상태까지 확인된 경우에만 실제 주문 적격으로 분류한다. PAPER 브로커 접수는 증권사 접수가 아니다. 날짜 10/15·26·27의 결과를 외부 검증 성과로 사용하지 않는다.

## 관측 후보 설치 경계

별도 Work 산출물 `r6_c2_observe_20261028/observation_candidate`에 payload, manifest, `CHANGES.patch`, 적용 도구가 있다. `DEPLOYMENT.md`는 같은 Work 폴더에 있으며 정확한 Windows 경로와 명령을 담는다. 이 저장소의 `OVERLAY_MANIFEST.json`은 개인정보 경로를 제거한 검토본이다.

적용 전 현재 release/전 소스 해시/6개 before 해시/API 보호 해시/STOP/실행 프로세스 부재/장부의 봇 무보유·무활성주문/설정 불변을 재확인한다. 이후 명시적 승인하에 외부 백업 후 6개만 원자 교체하고 104개 소스와 보호 상태를 검사한다. 기동하지 않는다.

롤백은 해당 설치 영수증의 외부 백업에서 정확히 6개만 복구한다. API 수정 전 구버전이 아니라 KT00018 기준선으로 돌아가야 한다. 현재 장부·설정·승인·새 기록 데이터는 롤백하지 않는다. 새 모듈은 승인된 생성 파일이고 현재 해시가 설치본과 일치할 때만 제거한다. 상태가 달라졌으면 중단한다.

## 남은 판단

관측 후보의 실제 개장 부하 영향, 계좌 장기 불통 시 보호 전송 제약, C2의 실제 주문 적격성/정상 변동 안정성/기대값은 미확인이다. 실제 신호 표본과 연속 자료가 확보되기 전에는 C2를 활성화하지 않는다. 기존 청산 정책을 유지한다.
