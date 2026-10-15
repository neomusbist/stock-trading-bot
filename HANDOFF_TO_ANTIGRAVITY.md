# 2026-10-15 후속 사고 수정 인계 — 배포 승인 아님

이 절이 아래 10월 14일 기록보다 최신이다. 이전 기록은 원본 그대로 보존했다.

## 정확한 보존 상태

- 개발 브랜치: `fix/r6-target-cap-incident-20261015`.
- 기준: 로컬 `f158bb6`의 독립 worktree. 다른 candidate worktree의 미커밋 `automation/runner.py`를 포함하거나 수정하지 않았다.
- 코드 커밋: `9e09770dc5fcc37256fe3fe1be50c638513c513c`.
- 현재 문서 포함 HEAD는 `git rev-parse HEAD`로 확인한다. 위 SHA는 실제 소스·신규 테스트의 커밋이다.
- **GitHub push 미완료**: CLI 자격증명을 사용할 수 없어 실패했다. 연결된 GitHub 조회에서도 기준 f158bb6/0da27dd가 없었다. 기준의 추가 도구 파일 두 개가 기존 공개 허용목록 밖이었고, 그중 하나는 개인 경로를 포함해 전체 기준 소스를 무검사로 우회 게시하지 않았다. 로컬 커밋과 Git bundle로 그대로 이어받을 수 있다.
- main, 다른 개발 브랜치, 운영본 코드·위험예산·승인 파일은 변경하지 않았다. 운영 Worker는 STOP이며 실제 주문을 제출하지 않았다.

## 확인한 결함과 수정

1. A 구조 무효화선을 더 넓게 계산한 뒤 구조 계산의 목표 상한으로 기존 패턴 목표를 덮어쓰면서 기대 보상이 늘어나는 경로가 있었다. 익명 시점 재현에서 원래 목표 8,060이 8,200이 되고 손익비가 1.17에서 1.51로 높아졌다.
2. `automation/patterns.py`: 기존 목표·저항과 구조 상한의 최솟값을 사용한다. 더 넓어진 손절을 정당화하려고 목표를 올리지 않는다. 손절거리·위험비율·AE3를 바꾸지 않았다.
3. `automation/engine.py`: 최종 주문 직전에도 A의 `target <= projection`을 검사한다. 예전의 부풀린 신호를 전달해도 거절한다.
4. `core/engine.py`: 장부에 없는 실시간 주문을 수신하면 주문번호·원주문번호·선별한 원문 필드를 기록한다. 계좌 FID는 제외하며 최근 100건만 장부에 유지한다. 발신 주체를 수동 주문으로 추측하거나 자동 반영·HALT 해제하지 않는다.
5. `tests/test_target_cap_incident.py`: 미래봉 없이 7개 전용 검사. 목표 상향 거절, 기존 상한·저항 유지, 실제 detector로 생성한 정상 범위 돌파 통과, 주문 경계 재검사, 미래봉 배제, 외부 주문 증거와 민감 필드 제외.

## 실제 사고에 관한 운영 확인

외부 원장에서 매도 체결이 확인됐지만 사고 전 운영 Worker에는 SELL 의도·청산 요청·주문이 없었다. 신선한 계좌·체결 조회와 복사 장부 모의 반영 후 공식 recovery로 장부 정합성과 HALT를 복구했다. 증권사 수수료·세금으로 정산했으며 장부를 리셋하지 않았다.
사용자는 HTS 0624에 잔고 편입 자동 −2% 손절이 설정돼 있었다고 후속 확인했다. 가격·시장가 매도·장부 밖 주문과 일치하는 유력 원인이지만 주문매체 원장 대조는 아직 미완료다. 이를 사용자의 직접 수동 매도로 기록하지 않는다.
HTS의 고정 손절과 봇의 구조적 청산선이 충돌한다. HTS 설정은 임의로 바꾸지 않았으며 자동 LIVE 시작도 하지 않았다.

## 검증과 남은 한계

- 새 전용 검사: **7 PASS**. 기존 운영 코드의 당시 봉·구조·비용 손익비 재현: 별도 5항목 PASS.
- 전체 검사 1492개: PASS 1473, FAIL 19, ERROR 0, SKIP 0. 네트워크 시도 0, 실제 주문 0.
- 실패한 19개만 수정 전 f158bb6에서 다시 실행: 13 PASS, 6 FAIL, 0 ERROR. 기존 기준에서도 실패한 6건과 이 패치로 달라진 13건을 BASELINE_COMPARISON.json에 따로 기록했다. 13건에는 A 진입 fixture가 먼저 거절되어 후속 청산 검사까지 도달하지 못한 경우도 포함된다.
- 기존 테스트 파일·공용 설정은 수정하거나 삭제하지 않았다. 아래 실패를 숨기지 않는다.

- `test_external_book_query_due_boundaries (test_audit_followup.ObservationAuditTests.test_external_book_query_due_boundaries)`
- `test_far_external_book_queries_reduce_but_near_monitoring_remains (test_audit_followup.ObservationAuditTests.test_far_external_book_queries_reduce_but_near_monitoring_remains)`
- `test_after_local_continuation_does_not_inherit_the_burst_exit_policy (test_night_judgment_quality.SustainedLocalRiseTests.test_after_local_continuation_does_not_inherit_the_burst_exit_policy)`
- `test_current_structure_can_form_A_below_an_older_session_high (test_night_judgment_quality.SustainedLocalRiseTests.test_current_structure_can_form_A_below_an_older_session_high)`
- `test_complete_trade_costs_and_price_band_are_analysis_only (test_policy_update.PolicyUpdateTests.test_complete_trade_costs_and_price_band_are_analysis_only)`
- `test_final_static_lock_cannot_be_removed_by_old_release_flag (test_preflight.PreflightTests.test_final_static_lock_cannot_be_removed_by_old_release_flag)`
- `test_old_build_authorization_is_not_reused (test_setup_acceleration.SetupAccelerationTests.test_old_build_authorization_is_not_reused)`
- `test_meaningful_range_floor_and_noise_sized_hard_line (test_structural_a_exit.EntryStructureTests.test_meaningful_range_floor_and_noise_sized_hard_line)`
- `test_sustained_rise_is_distinct_from_ten_second_volume_spike (test_structural_a_exit.EntryStructureTests.test_sustained_rise_is_distinct_from_ten_second_volume_spike)`
- `test_wider_price_structure_is_not_confused_with_account_risk_percent (test_structural_a_exit.EntryStructureTests.test_wider_price_structure_is_not_confused_with_account_risk_percent)`
- `test_completed_close_below_structure_requests_exit (test_structural_a_exit.ExitAndOrderTests.test_completed_close_below_structure_requests_exit)`
- `test_fresh_emergency_price_exits_without_waiting_for_close (test_structural_a_exit.ExitAndOrderTests.test_fresh_emergency_price_exits_without_waiting_for_close)`
- `test_gui_distinguishes_structural_close_line_and_emergency_risk_line (test_structural_a_exit.ExitAndOrderTests.test_gui_distinguishes_structural_close_line_and_emergency_risk_line)`
- `test_intra_minute_wick_with_recovery_does_not_exit (test_structural_a_exit.ExitAndOrderTests.test_intra_minute_wick_with_recovery_does_not_exit)`
- `test_new_order_sizes_from_emergency_line_and_keeps_soft_structure (test_structural_a_exit.ExitAndOrderTests.test_new_order_sizes_from_emergency_line_and_keeps_soft_structure)`
- `test_single_touch_between_structural_and_hard_line_holds (test_structural_a_exit.ExitAndOrderTests.test_single_touch_between_structural_and_hard_line_holds)`
- `test_stale_completed_bar_does_not_undo_current_recovery (test_structural_a_exit.ExitAndOrderTests.test_stale_completed_bar_does_not_undo_current_recovery)`
- `test_stale_trade_and_stale_quote_do_not_fake_emergency (test_structural_a_exit.ExitAndOrderTests.test_stale_trade_and_stale_quote_do_not_fake_emergency)`
- `test_identity_separates_signal_rules_and_risk_experiment (test_v13_risk_identity.V13RiskIdentityTests.test_identity_separates_signal_rules_and_risk_experiment)`

- 정상 수익 거래의 과잉 차단 및 수익성 개선은 미입증이다. 기존 정상 fixture가 작은 패턴 목표를 구조 상한으로 높인다는 가정에 의존했는지 구분해야 한다. 이번 사고 한 건만 막았다고 전략 PASS로 평가하지 않는다.
- 비용 반영 손익비 검사가 운영 설정에서 OFF였다는 사실도 확인했다. 이 개발 패치는 그 설정을 켜거나 임계값을 바꾸지 않는다. 비용 후 손익비 정책은 별도 검증이 남는다.
- 마지막 백테스트: 이번 코드 뒤 새 전체 손익 재계산은 하지 않았다. 아래 이전 R6 보고 수치를 이번 패치의 결과로 재사용하지 않는다. 1틱 불리 체결·주문 지연 분석도 이번 패치 뒤 재실행하지 않았다.
- SHADOW 수집을 이번 작업에서 새로 시작하지 않았다. 기존 수집 기능과 안전 경계는 유지했다. LIVE 배포 없음. 개발 패치는 회귀 영향과 과잉 차단을 해결하기 전 배포 금지.

## Antigravity가 이어서 실행할 순서

1. `git status --short`, `git branch --show-current`, `git rev-parse HEAD`. 이 브랜치 또는 제공된 bundle을 사용하고 다른 미커밋 작업을 지우지 않는다.
2. 로컬 사고 자료: `%USERPROFILE%\Documents\주식자동매매_개발\live-incident-20261015-396300`. `INCIDENT_REPORT.md`, `OFFLINE_REPLAY_RESULT.json`, `development-full.json`/`.log`, 복구 전 장부와 증권사 응답이 있다. 계좌/주문 원본은 Git에 넣지 않는다.
3. `python -X utf8 -B -m unittest tests.test_target_cap_incident -v`로 7개 전용 검사를 재현한다.
4. 전체 회귀 실패를 기존 기준 f158bb6와 비교한다. 정당한 정상 진입을 새 목표 상한이 막는지, 기존 fixture가 보상을 임의로 키우고 있었는지 따로 검증한다. 테스트 기대값만 바꿔 PASS로 만들지 않는다.
5. HTS 0624 주문매체 기록으로 외부 자동 손절의 실제 출처를 확인한다. 자동매매 재개 시 청산 정책 충돌을 사용자와 명시적으로 정리한다. 자동으로 HTS 손절을 끄거나 과거 주문을 봇 발신으로 바꾸지 않는다.
6. 정상 상승·얕은 눌림·실패 돌파·근접 저항 사례의 당시 자료로 재현 후 비용·1틱·지연 민감도를 재계산한다. 검증 전 수익성 PASS 또는 LIVE READY 판정 금지.
7. 전체 검사를 완료하고 별도 새 BUILD 배포·직접 LIVE 승인을 거친다. 기존 승인 영수증·계좌 손실장부를 복사/초기화하지 않는다.
8. 로컬 `INCIDENT_SOURCE.bundle`을 가져올 때는 `git bundle verify <bundle>` 다음 `git fetch <bundle> fix/r6-target-cap-incident-20261015:fix/r6-target-cap-incident-20261015`를 사용한다. 같은 브랜치가 이미 있으면 reset/force하지 말고 새 이름으로 가져온다. `INCIDENT_SOURCE.patch`는 코드 커밋의 4개 파일만 포함한 별도 적용용 패치다. GitHub 자격증명과 기준 소스 공개 검사를 해결한 후 이 개발 브랜치만 게시한다.

---

<!-- PREVIOUS_R6_HANDOFF -->
# R6 개발 인계 — 2026-10-14

## 보존 상태

- 개발 브랜치: `fix/r6-work-repair-20261014`.
- 기준 로컬 커밋: `2affd6dea43dc2c0c03cb22e6e5f397bc5cbde8e`. 그 위의 독립 수정이다. main과 LIVE R5는 병합·교체하지 않았다.
- 원본 R6 GitHub 브랜치 `feat/v1.6-fast-momentum-r6-20261014` 게시 완료. 원격 HEAD `3ad45e15f76f29edb6bd8dfa1948230a7967daef`의 tree는 원본 `2affd6d`의 `24575c4d9a6e938a48d629bba202594eb66a7f34`와 일치한다.
- 로컬 CLI 인증을 사용할 수 없어 승인받은 GitHub 연결로 6개 원본 변경을 순서대로 게시했다. 원본 커밋은 로컬에 보존하며 게시 커밋 SHA가 달라졌음을 숨기지 않는다. 이번 수정 브랜치 게시도 성공했고 로컬·원격의 정확한 코드 커밋은 `bfe5dd9ecd61640a07a4753e62c76c36f1c82c0e`로 일치한다. 코드 tree `599e10ec8e1cf1db1a071342cc43b9e4523af4f8`. 기존 로컬 체크포인트 9791b9b/81b27fc는 별도 보존됐다. 이후 인계 문서만 추가하는 커밋의 HEAD는 `git rev-parse HEAD`로 확인한다.
- 재개 시 가장 먼저 `git branch --show-current`, `git rev-parse HEAD`, `git status --short`를 확인한다. 미커밋 변경을 지우거나 원본 R6로 다시 개발하지 않는다.

## 실제 수정

- `automation/fast_momentum.py`: 임의의 고정 손절폭·손익비를 맞춘 목표가 제거. 당시 확인된 미시 지지와 알려진 저항, 신선한 실제 호가·잔량, 비용·비상선 위험으로 정수 수량 계산. 단일 정상 무효화선 접촉과 두 독립 체결의 구조 붕괴를 구분. 고정 +1.8% 보호·시간 만료 대신 확정된 진입 후 고저점 구조를 이용한다. FAST는 여전히 SHADOW 전용이다.
- `automation/shadow_observation.py`, `automation/observation_worker.py`: 모든 유효 틱의 누적과 평가 주기 분리, `shadow_view` 저항/asof 전달 누락 수정, 수신·처리 시각 구분, 구독 공백·큐 유실 초기화, 동시 생산자 sequence 보호, callback 완료 뒤 processed sequence, 비동기 원시 TRADE/BOOK 기록·로테이션·종료 flush.
- `automation/shadow_fast_runtime.py`: 독립 데이터 전용 진입점. source-root와 LIVE 충돌 검사가 없으면 인증 불허. 주문·계좌 HTTP endpoint 차단. 실제 FAST→후속 신선한 호가 가상 계좌→파일 기록 연결. 데이터 끝 강제청산 금지.
- `automation/runner.py`, `operator_service.py`, `operator_ui_service.py`: readiness 없는 상태의 ON 기본값 제거, GUI가 읽는 시점의 시세·계좌 나이 재계산, 수신기 실패·적체·지연 반영, 정확한 OFF 사유 로그. 개별 종목 5초 최종 주문 검사 유지.
- 전용 신규 테스트 4개와 기존 FAST/logger 테스트의 잘못된 가정을 수정했다. 기존 정상 A/B/C·위험·주문 안전장치를 완화하지 않았다.
- `audit/r6_work_repair_20261014/replay.py`: 기존 snapshot proxy 진입을 고정 비교군으로 남기고 없는 호가를 만들어 체결하지 않음, 누적 손실·승인자본·지연·틱 이동·비용 분리·미실현 구분을 수정. 이 코드를 실제 FAST 백테스트라고 부르지 않는다.

## 현재 확인 결과와 한계

- FAST/SHADOW/기존 fixture/실행 회계 대상 검사: 마지막 64개 PASS, 실패 0, 오류 0, 종료코드 0.
- 실제 두 운영 서비스와 주문 직전 가짜 브로커 경로: 87 PASS, 네트워크 시도 0. 5/7초 quiet와 12/20초 OFF, 단절·계좌 만료·지연·큐 적체를 구분했다.
- 최종 개발본 전체 1,430 PASS 및 GitHub와 동일한 코드 커밋의 깨끗한 detached checkout 전체 1,430 PASS, 실패0/오류0/skip0/네트워크시도0/AST오류0/종료0. identity/replay 결과 6개 canonical hash도 일치했다. TEST_RESULTS.json 참조.
- 원본 오후 파일 SHA-256 `0c698c6ef4c7d9815c83a081d5d81d66205588e120c2e07caaf3f10f2050b50c`, 67,132,914바이트. 실제 구간 14:21:21~14:52:49, 6,072 ENTRY_EVAL. 원시 체결·잔량 tape가 아니다.
- 원본에서 서산 7,060~7,320원, 엘앤씨바이오 61,700~66,900원 확인. 문제의 7,650~7,720/13,400원대는 이 동일 파일에 없다. 기존 일부 보고의 종목명은 잘못 연결돼 있다. 잘못된 요약의 최초 출처는 미확보이므로 인코딩 탓이라고 단정하지 않는다.
- 이전 proxy 재현: 기준 +1,469.92원, 1틱 -278.17원, 2틱 -2,116.29원, 3틱 -2,894.39원, 한 snapshot 지연 -61.41원. 실제 FAST 또는 실제 체결의 수익이 아니다.
- 엄격한 실행·회계 proxy: 기준 2건 청산 -282.139원, 1틱 -471.89원, 2틱 -874.956원, 3틱 -1,238.062원. 1 snapshot 지연 기준 -132.4615원. 종료 시 에스엠벡셀 30주 가상 보유가 남고 마지막 bid가 없어 미실현은 UNKNOWN. 최종 재실행 CSV를 우선한다.
- 단일 거래일 31분의 snapshot, 호가 누락, 독립 원시 데이터 없음 때문에 새 FAST 수익성·초입 우위·1틱 내성은 UNKNOWN이다. 5~10분당 왕복 1회도 입증되지 않았다.
- 실제 SHADOW 접속은 마지막 확인 R5 LIVE PID 19568 때문에 `LIVE_WORKER_LOCK_ACTIVE`로 인증 전에 차단됐다. 추가 토큰 발급·실주문·R6 LIVE 배포는 하지 않았다.
- 사용자가 요청한 HALT는 공식 STOP→신선한 계좌 대조→recover_halt 경로로 해제했다. 당시 일일 손실 1,256원 유지. 이후 R5가 다시 LIVE로 확인됐다. 이 문서의 상태는 확인 시점 기록이므로 재조회한다.

## Antigravity 재개 순서

1. 이 작업 폴더의 현재 HEAD와 미커밋 변경, GitHub 수정 브랜치 존재를 확인한다. 다른 작업공간·main·운영본을 덮어쓰지 않는다.
2. `python -B -m unittest tests.test_r6_fast_repair tests.test_r6_shadow_runtime tests.test_r6_readiness_repair tests.test_r6_execution_repair tests.test_fast_momentum tests.test_shadow_tick_logger -q`를 실행한다.
3. 전체 회귀는 Qt offscreen 및 socket 접속 차단 harness로 수행한다. 완료 로그·종료코드·실패가 없으면 PASS로 적지 않는다. `REPRODUCE.md`의 최종 명령을 따른다.
4. `$env:USERPROFILE\Desktop\주식자동매매\runtime\automation-production\archive\events.jsonl.20261014-145249-570438.7232.32e27bce`를 해시 확인 후 `python -B -m audit.r6_work_repair_20261014.replay "<원본>" --out "<개별 재현 폴더>"`로 재현한다.
5. SHADOW 실행은 `python -B -m automation.shadow_fast_runtime --check --source-root "<R5 운영 폴더>" --output "<운영 외 독립 SHADOW 폴더>"`부터. R5 실행 중이면 우회·강제 종료·토큰 추가 발급하지 않는다. 사용자가 승인한 공식 STOP과 보유·미체결·UNKNOWN 대조 뒤에만 다음 실데이터 실행을 한다.
6. 2026-10-15 08:50~09:10 `--seconds 1200`으로 실제 원시 TRADE/BOOK을 수집한다. 파일·구독 ACK·exchange/receiver/processed time·큐 유실·flush·자원 사용을 확인한다. 누락을 감지할 수 있다는 것과 무손실 보장은 다르다.
7. 실제 FAST를 원시 tape로 재생하여 A/B/C와 발견·관측·신호·후속 호가 적격 시각을 비교한다. 0~3틱 및 1프레임 이상 지연, 비용, 단일 계좌·2%/4%·한 종목을 유지한다. 별도 거래일도 평가하고 손실/수익 양쪽의 과잉 차단을 측정한다.
8. 수정→전체 회귀→새 clean checkout 재현→민감정보 staged 검사→별도 브랜치 게시. 수익성 미입증 상태에서 LIVE를 테스트 수단으로 사용하지 않는다.

## 로컬 증거 위치

- 개발: `$env:USERPROFILE\Documents\주식자동매매_개발\r6-work-repair-20261014`.
- 개인 증거: 같은 상위 폴더의 `r6-work-repair-private-20261014` 및 `r6-work-repair-audit-20261014`. HALT 복구 결과·백업, 원본 이전 replay, KRX 명칭 확인, 전체 검사 harness와 로그를 보존했다. 인증 파일과 계좌 원본은 Git에 넣지 않는다.
- 저장소 공개 결과: `audit/r6_work_repair_20261014/`.

이 인계서는 성공 선언이 아니다. 전체 검사·게시·실접속 미완료는 최종 기록에서 구분한다.

## 최종 인계 상태

**부분 완료**: 실제 코드 수정·GitHub 게시·개발본 및 clean checkout 전수 검사·자료 재현·인계 문서 보존은 완료. 실제 FAST의 독립 시장 수익성, 1틱 체결 내성, 개장 RAW→신호 지연 비교와 증권사 실구독은 미완료다. 10월14일 22:23 인증 없는 --check는 source STOP 상태에서 충돌 검사 PASS/주문 surface false/네트워크0/종료0. 이를 실시간 수신 PASS로 읽지 않는다. 운영 R5는 배포하지 않았고 main은 3c6fb27557fc0c556ce8312a4bd1000dd07f7d7c로 유지했다.

재개 명령: 기존 작업 폴더에서 `git log -5 --oneline`, `git status --short`, `git rev-parse HEAD`, `git rev-parse origin/fix/r6-work-repair-20261014`부터 확인한다. 코드 기준은 bfe5dd9ecd61640a07a4753e62c76c36f1c82c0e이고 이후 문서 커밋은 해당 코드와 동일하다. 다른 LLM의 미커밋 변경을 reset하지 않는다. 위 순서와 SHADOW_RUNBOOK의 조건을 유지해 10월15일 실제 자료 수집·검증을 이어간다.
