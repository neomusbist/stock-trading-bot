# 시험 결과와 완료 상태

코드 보존 체크포인트 `9791b9bd27aad439a9ed2273212e22d1c75ba663` 이후 실행 회계 자료의 미청산 원장/승인자본 반례를 추가했다.

- FAST/SHADOW/기존 fixture/실행 회계 대상: 64 PASS, 실패0/오류0, 종료0.
- 새 실행 회계 전용 반례: 10 PASS, 실패0/오류0, 종료0.
- 실제 준비 서비스/최종 브로커 guard 대상: 87 PASS, 실패0/오류0, 네트워크 시도0, 종료0.
- 전체 회귀: 실행 중. 최종 수치와 종료코드 확인 전 PASS 아님.
- 깨끗한 최종 커밋: 미실행. 최종 기록에서 별도로 확정한다.

대상 명령:
```
python -B -m unittest tests.test_fast_momentum tests.test_shadow_tick_logger tests.test_r6_fast_repair tests.test_r6_execution_repair tests.test_r6_shadow_runtime -q
```

전체 시험은 개인 증거 폴더 `run_checks.py --full`로 Qt offscreen·개발본 import 최우선·socket connect/connect_ex 차단 상태에서 모든 `tests/test_*.py`를 load하고 AST 전체 검사를 실행한다. 전체 결과는 `TEST_RESULTS.json`과 최종 문서에 기록한다. 실행 과정에서 실계좌 반복 주문·인증 호출로 검증하지 않는다.
