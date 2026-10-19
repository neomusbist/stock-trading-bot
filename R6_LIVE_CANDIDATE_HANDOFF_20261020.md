# R6 통합 후보 인계 — 2026-10-20

완료. 운영 반영은 아직 승인되지 않았다.

- 브랜치: fix/r6-live-integration-20261019
- 최종 운영 소스 커밋: a4551c28b6fc299a9183432fbece2bc94d44633f
- 현재 코드·테스트 커밋 뒤의 문서 커밋을 포함한 HEAD: `git rev-parse HEAD`로 확인.
- 비교한 GitHub 기준 HEAD: 64e5157757256e1cd40e8af32d7bfa7de03498a8.
- 새 운영 후보 전체 적용 범위 1770 PASS, 실패/오류 0. 미설치 연구 7개 모듈은 미실행.
- 후보 기술 검증 GO. LIVE 파일 교체·재시작·실제 주문은 수행하지 않음.
- 기존 설정·손실·예산·승인 원본 해시 보존. 새로운 손익 백테스트는 UNKNOWN.

상세 검사 결과와 거래 사건 자료는 아래 로컬 파일 및 배포 ZIP의 review/에 보존한다.
실거래 상세 자료는 GitHub 코드 브랜치에 올리지 않는다.
로컬 자료: audit/r6_live_integration_20261019/INTEGRATION_REPORT.md,
SOURCE_VERIFICATION.json, LIVE_TO_CANDIDATE.diff, VERIFICATION.json,
INCIDENT_CHECK.json, RELEASE_IDENTITY.json, ROLLBACK_RELEASE_IDENTITY.json,
DEPLOYMENT_AND_ROLLBACK.md.

GitHub 보존 대상은 별도 개발 브랜치의 소스·테스트·검사 도구와 이 인계 문서,
DEPLOYMENT_AND_ROLLBACK.md, GITHUB_REFERENCE.json이다. main과 LIVE는 변경하지 않는다.
원격 반영 여부는 같은 브랜치의 `git ls-remote` HEAD로 최종 확인한다.

개발 경로: C:\Users\MJS\Documents\주식자동매매_개발\r6-live-candidate-20261019
실제 LIVE: C:\Users\MJS\Desktop\주식자동매매
비공개 원본·실행 로그·배포 ZIP: C:\Users\MJS\Documents\주식자동매매_개발\r6-live-integration-20261019-private

다음 작업은 전략 재개발이 아니다. 사용자 승인 후 현재 소스 기준 해시를 재검사하고
증권사의 봇 보유/미체결/진행 중/UNKNOWN/처리 작업 0과 HALT 없음을 새로 확인한다.
보유 보호를 끊지 않는다. 설정·장부·승인 기록을 초기화하지 않는다. 정확한 8개 경로만
백업·교체하고 92개 파일 해시와 기존 설정·위험 정책을 검사한다. legacy deploy_release.py는
이 후보에 사용하지 않는다. 새 BUILD의 기존 승인 검사를 유지한다.

재현: 기존 Python 환경에서 audit/r6_live_integration_20261019/validate_candidate.py
--operational --output <별도 비공개 결과 폴더>. 전체 무필터 초기 결과와 실패 이력도
비공개 capsule-full/FULL.json 및 FULL.log에 남아 있다. 최종 근거는
final-capsule-operational/OPERATIONAL.json 및 OPERATIONAL.log이다.
