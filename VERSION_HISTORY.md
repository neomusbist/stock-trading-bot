# 전체 버전 이력

아래 순서는 명시적인 버전 순서입니다. 숫자를 소수로 계산하거나 문자열 정렬로 버전 순서를 결정하지 않습니다. GitHub 게시일은 2026-10-20의 소급 기록 작성일이며, 소스 기준 날짜 및 실제 배포일과 다릅니다.

| 버전 | 소스 기준 날짜 | 주요 특징 | 기준 커밋 | LIVE 적용 / 게시 상태 |
|---|---|---|---|---|
| v1.0 | 확인 불가 | 첫 커밋에는 프로그램 소스가 없음 | — | 확인 불가 · 태그/Release 미생성 |
| [v1.1](https://github.com/neomusbist/stock-trading-bot/releases/tag/v1.1) | 2026-10-01 | 최초로 확인되는 프로그램 소스 기준 | [`3800fff`](https://github.com/neomusbist/stock-trading-bot/commit/3800fffec5b2b6920ca16673875c26b6e8fefa59) | 동일 커밋 배포 미확인 · Historical Pre-release |
| [v1.2](https://github.com/neomusbist/stock-trading-bot/releases/tag/v1.2) | 2026-10-06 | 세션 계획과 빠른 FOCUS 개발 기준 | [`92eb5df`](https://github.com/neomusbist/stock-trading-bot/commit/92eb5df8cb81459f41e8ad13df88457fccceee92) | 동일 커밋 배포 미확인 · Historical Pre-release |
| [v1.3](https://github.com/neomusbist/stock-trading-bot/releases/tag/v1.3) | 2026-10-07 | 장전 준비와 readiness 개발 기준 | [`119d1df`](https://github.com/neomusbist/stock-trading-bot/commit/119d1df78a822d439f1eff07debd51243dc96b8b) | 동일 커밋 배포 미확인 · Historical Pre-release |
| [v1.4](https://github.com/neomusbist/stock-trading-bot/releases/tag/v1.4) | 2026-10-12 | Setup O 개장 모멘텀 개발 지선 | [`3bf4d08`](https://github.com/neomusbist/stock-trading-bot/commit/3bf4d086ee659d068e6d2413ef957329de822368) | 동일 커밋 배포 미확인 · Historical Pre-release |
| [v1.5](https://github.com/neomusbist/stock-trading-bot/releases/tag/v1.5) | 2026-10-14 | WATCH 관측 보전 R5 소스 기준 | [`2da04d3`](https://github.com/neomusbist/stock-trading-bot/commit/2da04d3c794444b1a1332e8a2f0209bad3227049) | 동일 커밋 배포 미확인 · Historical Pre-release |
| [v1.6](https://github.com/neomusbist/stock-trading-bot/releases/tag/v1.6) | 2026-10-20 | 현재 LIVE 운영 기준 | [`eac9bc5`](https://github.com/neomusbist/stock-trading-bot/commit/eac9bc5880d25902d0d9f695ffdaabcac9632a16) | 현재 LIVE · 정식 Release / Latest |

## 최초 버전부터 확인한 근거

- 최초 커밋: [`e7a3e32`](https://github.com/neomusbist/stock-trading-bot/commit/e7a3e327e13d9f60b58042bc36a72972f04c800f) (2026-10-01). 파일은 `.gitattributes`, `.gitignore`, `README.md` 세 개뿐입니다. v1.0이라고 식별할 코드나 배포 기록이 없습니다.
- v1.1: 실제 소스가 들어온 `3800fff`의 커밋 메시지와 README가 V1.1을 명시합니다.
- v1.2: `92eb5df`의 V1.2 구현 커밋과 SessionPlan/FOCUS 코드가 근거입니다. 당시 일부 내부 문자열은 V1.1로 남아 있습니다.
- v1.3: `119d1df`의 README, BUILD 및 V1.3 인계 문서가 근거입니다. 문서의 배포 준비 완료를 실제 배포로 바꾸어 쓰지 않습니다.
- v1.4: `3bf4d08`의 BUILD/전략 식별자는 실제 V1.4입니다. 별도 `integrate/v1.4-trade-quality-audit-gui`는 BUILD가 V1.3인 다른 통합 지선이라 동일 상태로 합치지 않습니다.
- v1.5: R5 소스 `2da04d3`가 V1.5 BUILD를 명시합니다. R1~R5마다 정식 출시 번호를 새로 만들지 않았습니다.
- v1.6: 운영 manifest의 `eac9bc5`와 92개 파일 해시를 대조했습니다. 90개는 Git blob과 바이트 동일, `gui_menu.cmd`와 `requirements.txt`는 줄바꿈만 다릅니다. 설치된 물리 파일 92개의 manifest 검사는 모두 일치했습니다.

## 이름과 실제 코드의 차이

- `feat/v1.6-fast-momentum-r6-20261014`라는 브랜치의 현재 head `3ad45e1`은 내부 BUILD가 여전히 V1.5 R5입니다. 브랜치 이름만 보고 R6 출시 커밋으로 지정하지 않았습니다.
- `main`은 `1bbb215`의 V1.3 계열 소스이며 실제 운영 기준은 `eac9bc5`입니다. 문서만 갱신하고 main 프로그램 소스는 보존했습니다.
- v1.6의 긴 BUILD는 내부 추적 정보입니다. 이번 등록은 표시용 별칭이며 현재 LIVE manifest·승인 신원을 바꾸지 않습니다.
- 현재 진행 중인 `research/backtest`는 v1.7 목표의 연구입니다. 기존 89건/5건 백테스트는 인과성·가짜 호가·계좌 재생 문제로 성능 수치가 무효이며, 수정 연구도 플러스 기대값을 입증하지 못했습니다. v1.7 태그/Release는 만들지 않습니다.

## 번호를 만들지 않은 항목

v1.0, v1.51, v1.52, v1.61, v1.62 및 v1.7이 예시에 있었다는 이유만으로 과거 출시를 생성하지 않았습니다. v1.0은 코드가 없고, 다른 후속 번호는 향후 검증·출시 절차에서 확정합니다. 확인된 현재 태그는 여섯 개이며 동일 소스 상태에 중복 번호를 부여하지 않습니다.

## 이 기록의 한계

태그는 확인된 Git 소스 기준을 보존합니다. Historical Pre-release는 정확한 LIVE 배포 또는 당시 모든 테스트의 성공을 뜻하지 않습니다. 낡은 TEST_REPORT가 뒤 커밋에 남아 있는 사례는 해당 Release 설명에서 분리했습니다. 수익성은 어느 버전에서도 이번 이력 정리만으로 검증하지 않았습니다.
