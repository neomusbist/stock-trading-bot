# R6 운영 후보 적용·복구 범위

이 문서는 배포 후보의 검토 자료다. 이 작업에서는 운영 파일 교체, 프로세스 재시작,
실제 주문, 승인 기록 수정, 위험예산·장부 초기화를 수행하지 않는다.

## 승인 대상: 정확히 8개 경로

1. automation/engine.py
2. automation/runner.py
3. automation/focus_selection.py
4. automation/position_health.py
5. automation/patterns.py — SHADOW에서 기존 전략 평가를 허용하되 주문 권한은 부여하지 않는 의존 수정
6. automation/release_integrity.py — 실제 파일과 manifest 해시 대조
7. automation/decisions.py — 새 BUILD 식별자
8. runtime/RELEASE_IDENTITY.json — 실제 92개 운영 파일을 기준으로 생성한 새 manifest

BUILD: `V1.6-risk2-daily4-fast-live-r6-integrated-20261020`.
설정 스키마, 계좌 위험 2%, 일일 손실 4%, HARD_LOCK 005930/017900/052260,
구조선·비상선 구분, FAST 청산 임계값, AE3, 계좌·손실 장부는 유지한다.
새 진입 전략·RR 완화·추격 가격 상향은 포함하지 않는다.

## 승인 후 적용 순서

1. 사용자가 위 파일 범위와 새 BUILD 적용을 승인했는지 확인한다.
2. 현재 GUI·Worker·정책 상태를 확인하고 공식 STOP 절차를 사용한다. 보유 포지션이
   있다면 보호를 끊고 재시작하지 않는다.
3. 증권사를 **새로 조회**해 봇 보유, 미체결, 진행 중·UNKNOWN 주문, 주문 처리 작업이
   모두 0이고 HALT가 없는지 운영 장부와 대조한다. 저장된 STOP·heartbeat나 과거
   조회를 이 확인으로 대신하지 않는다. 수동 보유 HARD_LOCK은 봇 보유와 구별한다.
4. 현재 운영 소스가 `SOURCE_VERIFICATION.json`의 LIVE 기준 해시와 같은지 대조한다.
   다르면 복사를 중지하고 변경 내용을 다시 결합한다. 현재 설정·장부·승인 파일은
   그 시점의 해시와 원본을 별도 보존한다.
5. 적용 직전 위 8개 파일을 새 백업 폴더에 보존한다. ZIP의 `payload/`에서 **위 8개만**
   복사한다. 전체 개발 폴더를 덮어쓰거나 ZIP 전체를 LIVE에 풀지 않는다.
6. 새 무결성 검사로 92개 파일 해시·BUILD·필수 보호 패치를 확인한다. 설정 파일이
   같은 바이트인지, 기존 장부와 HARD_LOCK이 보존됐는지 확인한다.
7. 검증 실패 시 Worker와 주문권한을 OFF로 유지하고 아래 복구 절차를 따른다.
8. 정상 재시작 후에도 새 BUILD의 기존 승인 검사를 따른다. 이전 BUILD 영수증을
   고쳐 재사용하지 않는다. 사용자 승인 범위를 넘어 LIVE를 자동 시작하지 않는다.

현재 `deploy_release.py`는 6개 파일용 과거 도구다. 이 후보의 8개 경로와 92개
manifest를 설치하지 못하므로 이 후보의 배포에 사용하지 않는다. 부분 소스를
거부하는 회귀 검사와 별도 8개 경로 교체·복구 재현을 수행한다.

## 복구 순서

1. 신규 진입·Worker를 STOP으로 유지하고 새로운 실계좌·주문 대조를 한다. 보유
   보호가 필요하면 파일 복구를 위해 강제로 프로세스를 끊지 않는다.
2. 적용 직전 백업에서 소스 7개를 원래 바이트로 복구한다. 계좌 설정, 정책, 승인
   영수증, state.json, 당일 손실·예산 파일은 복구 대상으로 덮어쓰지 않는다.
3. 최초 LIVE manifest에는 engine.py·observation_worker.py 해시 불일치가 있었다.
   원본 manifest는 감사 증거로 보존한다. 정상 복구 확인에는 옛 실제 92개 파일로
   다시 계산한 `ROLLBACK_RELEASE_IDENTITY.json`을 사용한다. 옛 소스의 BUILD와
   manifest BUILD가 같아야 한다.
4. 92개 옛 소스 해시와 보호·설정·장부 보존을 검증하고 STOP을 유지한다. 복구도
   자동 LIVE 시작 또는 승인 기록 초기화의 근거가 아니다.

배포 전 준비된 롤백 ZIP은 소스 복구 자료다. 실제 적용 시점의 새 백업과 새 계좌
조회가 필요하다는 조건은 동일하다.

## 재현 명령

Python은 기존 stock-trading-bot 가상환경을 사용한다. `validate_candidate.py --full`
은 모든 테스트의 최초 원시 결과를 보존한다. `--operational`은 설치되지 않은
연구 모듈 7개를 이름·사유·선언된 테스트 수와 함께 미실행으로 기록하고, 실제
운영 후보에 적용 가능한 전체 검사를 실행한다. 제외를 PASS 또는 SKIP으로 세지 않는다.

최종 배포 판단은 같은 폴더의 `VERIFICATION.json`과 `INTEGRATION_REPORT.md`를 따른다.
