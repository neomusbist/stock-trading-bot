# 시험 결과와 완료 상태

코드 보존 체크포인트 `9791b9bd27aad439a9ed2273212e22d1c75ba663` 이후 실행 회계 자료의 미청산 원장/승인자본 반례를 추가했다.

- FAST/SHADOW/기존 fixture/실행 회계 대상: 64 PASS, 실패0/오류0, 종료0.
- 새 실행 회계 전용 반례: 10 PASS, 실패0/오류0, 종료0.
- 실제 준비 서비스/최종 브로커 guard 대상: 87 PASS, 실패0/오류0, 네트워크 시도0, 종료0.
- 최종 코드 전체 회귀: **1,430 PASS**, 실패0/오류0/skip0/네트워크시도0/AST오류0, 종료0, 141.159초.
- GitHub와 동일한 코드 커밋 `bfe5dd9ecd61640a07a4753e62c76c36f1c82c0e`의 새 detached checkout: **1,430 PASS**, 실패0/오류0/skip0/네트워크시도0/AST오류0, 종료0, 142.177초. 코드 변경 후 검증한 것이며 문서 마무리는 코드 변경을 포함하지 않는다.

대상 명령:
```
python -B -m unittest tests.test_fast_momentum tests.test_shadow_tick_logger tests.test_r6_fast_repair tests.test_r6_execution_repair tests.test_r6_shadow_runtime -q
```

전체 시험은 개인 증거 폴더 `run_checks.py --full`로 Qt offscreen·개발본 import 최우선·socket connect/connect_ex 차단 상태에서 모든 `tests/test_*.py`를 load하고 AST 전체 검사를 실행한다. 전체 결과는 `TEST_RESULTS.json`과 최종 문서에 기록한다. 실행 과정에서 실계좌 반복 주문·인증 호출로 검증하지 않는다.

처음 전체 실행은 기존 준비 상태 fixture 3건이 실패했다. readiness 없음/체결 시각 없음이 ON이라고 기대하던 fixture를 실제 준비 판정·신선한 체결 증거로 수정했다. 그 뒤 개발본과 깨끗한 체크아웃에서 모두 전수 통과했다. 안전 경계를 완화하거나 실패 검사를 삭제하지 않았다. 원본 identity와 replay의 CSV/JSON 6개가 Git canonical LF 기준으로 동일하게 재현됐다. CRLF checkout과 LF 결과를 구분해 비교했다. 모든 결과 수치는 TEST_RESULTS.json에 있다.
