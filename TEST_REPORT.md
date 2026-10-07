# 2026-10-07 Discovery/WATCH 복원 최종 검증

기준 `de5fc0c`의 정리된 기존 533개 검사 + 신규 18개 = **551 PASS / 0 FAIL / 0 ERROR / 0 SKIP**. 최종 전체 실행 60.30초. 아래 620개 표기는 테스트 정리 전의 역사 결과이며 현재 검사 수가 아니다.

최종 targeted: **386 PASS / 0 FAIL / 0 ERROR / 0 SKIP**. 후보 압축, SHADOW, WATCH 경쟁/승격, 구독, A/B/C, readiness, 위험 identity, 보호·복구·GUI 운영 경로를 포함한다. 전체/targeted 모두 네트워크 접근 시도 0건. 실제 broker 주문 0건.

추가 18개 검사는 source1의 min_source_count=2 운영 설정 호환, 전체 8슬롯 경쟁, 더 좋은 tier/점수 교체와 작은 점수차 유지, 후보 행/현재 자격/재생성 신호의 fallback 일치, 정상 history 유지, A/B/C 실제 detector부터 가짜 broker guard까지 도달, warmup/가속/유동성/spread/구조/RR/Economic/risk 실패 차단을 확인한다. 별도로 자기 정상 INTENT와 외부 INTENT/UNKNOWN/HALT, 타 장부 INTENT, 저장 레코드 불일치, HARD_LOCK/외부보유/자본0/계좌 freshness, 중복 및 계좌 identity를 검증한다.

개발 과정에서 숨기지 않은 실패:
- 최초 연결 targeted 268개 중 정상 자기 INTENT의 당일 승인 오차단 1건을 재현했다. 정확한 현재 레코드 한 개만 구분하는 수정 후 통과했다.
- 전체 첫 실행은 474개, 470 PASS / 1 FAIL / 3 ERROR였다. 개발 QtTest overlay가 빠져 GUI 3모듈이 로드되지 않았고, continuous mock 후보에 실제 입력이 항상 갖는 source_count가 빠져 있었다. 기존 QtTest를 개발 실행기에 연결하고 mock 후보에 source_count/source_details를 명시했다. 기존 assertion을 삭제하거나 완화하지 않았다. 이 원인 해결 후 최종 전체 결과는 위 551 PASS이다. 최초 실패 출력은 private 검증 자료에 보존했다.

구문검사: 공개 Python 소스 73파일 정상. diff whitespace 검사 정상. repo_guard/staged 검사는 최종 커밋 전 별도로 수행한다. 원본 운영 로그와 계좌/승인/설정은 Git에 포함하지 않는다.

오늘 6종목 163주기 재검사는 source-only 거절 0건을 확인했다. 비엘팜텍 36회 관찰 압축 자격 통과 외 표본은 기존 경제성/거래대금/구조/감속 또는 당시 공식 상태 미확인으로 탈락/대기한다. 전일까지 일봉 재계산은 당시 캐시 보유를 증명하지 않으며, 부분 유니버스 replay로 실제 WATCH 편성·Setup·BUY·수익성을 주장하지 않는다. 실운영본 배포/재시작 및 Git push는 수행하지 않는다.

---

## 테스트 정리 이전의 역사 결과

# Offline verification
The preserved V1.1 suite has 620 tests, including original regressions and SHADOW/entry/exit/GUI checks. Previous validation: 620 PASS, 0 FAIL, 0 ERROR, 0 SKIP. Re-run the entire suite after installing dependencies; do not infer current results from this historical count.

Tests use fake brokers, synthetic accounts, temporary files and Qt test services. No real broker orders or account network tests form part of this result. A/B/C paths, source/SHADOW isolation, cap-zero permission, protected holdings, monotone stops, rejection/partial fill/recovery and session boundaries are covered.

Local verification outputs, account data, actual logs, screenshots, performance samples and backup archives are excluded from this repository. QtTest is a development dependency; production GUI modules are QtCore/QtGui/QtWidgets.
