# 전체 버전 이력

아래 순서는 명시적인 버전 순서입니다. 숫자를 소수로 계산하거나 문자열 정렬로 버전 순서를 결정하지 않습니다.

| 버전 | 소스 기준 날짜 | 주요 특징 | 기준 커밋 | LIVE 적용 / 게시 상태 |
|---|---|---|---|---|
| v1.0 | 확인 불가 | 첫 커밋에는 프로그램 소스가 없음 | — | 확인 불가 · 태그/Release 미생성 |
| [v1.1](https://github.com/neomusbist/stock-trading-bot/releases/tag/v1.1) | 2026-10-01 | 최초로 확인되는 프로그램 소스 기준 | [`3800fff`](https://github.com/neomusbist/stock-trading-bot/commit/3800fffec5b2b6920ca16673875c26b6e8fefa59) | 동일 커밋 배포 미확인 · Historical Pre-release |
| [v1.2](https://github.com/neomusbist/stock-trading-bot/releases/tag/v1.2) | 2026-10-06 | 세션 계획과 빠른 FOCUS 개발 기준 | [`92eb5df`](https://github.com/neomusbist/stock-trading-bot/commit/92eb5df8cb81459f41e8ad13df88457fccceee92) | 동일 커밋 배포 미확인 · Historical Pre-release |
| [v1.3](https://github.com/neomusbist/stock-trading-bot/releases/tag/v1.3) | 2026-10-07 | 장전 준비와 readiness 개발 기준 | [`119d1df`](https://github.com/neomusbist/stock-trading-bot/commit/119d1df78a822d439f1eff07debd51243dc96b8b) | 동일 커밋 배포 미확인 · Historical Pre-release |
| [v1.4](https://github.com/neomusbist/stock-trading-bot/releases/tag/v1.4) | 2026-10-12 | Setup O 개장 모멘텀 개발 지선 | [`3bf4d08`](https://github.com/neomusbist/stock-trading-bot/commit/3bf4d086ee659d068e6d2413ef957329de822368) | 동일 커밋 배포 미확인 · Historical Pre-release |
| [v1.5](https://github.com/neomusbist/stock-trading-bot/releases/tag/v1.5) | 2026-10-14 | WATCH 관측 보전 R5 소스 기준 | [`2da04d3`](https://github.com/neomusbist/stock-trading-bot/commit/2da04d3c794444b1a1332e8a2f0209bad3227049) | 동일 커밋 배포 미확인 · Historical Pre-release |
| [v1.6](https://github.com/neomusbist/stock-trading-bot/releases/tag/v1.6) | 2026-10-20 | v1.6 이전 운영 기준 (FAST 기초 통합) | [`eac9bc5`](https://github.com/neomusbist/stock-trading-bot/commit/eac9bc5880d25902d0d9f695ffdaabcac9632a16) | 이전 LIVE 기준 · 정식 Release |
| [v1.7](https://github.com/neomusbist/stock-trading-bot/releases/tag/v1.7) | 2026-11-03 | 11월 3일 첫 실거래(3건 매수·청산) 검증 완료, 이중 진입 경로(돌파 및 눌림목 반등), 구조적 가격기하 순RR, 호가·스프레드 판정 정상화 | [`781de40`](https://github.com/neomusbist/stock-trading-bot/commit/781de402fbe78918fe7ff58306d5349c0fa8cc4b) | 현재 LIVE · 공식 운영 릴리스 / Latest |

## 최초 버전부터 확인한 근거

- 최초 커밋: [`e7a3e32`](https://github.com/neomusbist/stock-trading-bot/commit/e7a3e327e13d9f60b58042bc36a72972f04c800f) (2026-10-01). 파일은 `.gitattributes`, `.gitignore`, `README.md` 세 개뿐입니다. v1.0이라고 식별할 코드나 배포 기록이 없습니다.
- v1.1: 실제 소스가 들어온 `3800fff`의 커밋 메시지와 README가 V1.1을 명시합니다.
- v1.2: `92eb5df`의 V1.2 구현 커밋과 SessionPlan/FOCUS 코드가 근거입니다. 당시 일부 내부 문자열은 V1.1로 남아 있습니다.
- v1.3: `119d1df`의 README, BUILD 및 V1.3 인계 문서가 근거입니다. 문서의 배포 준비 완료를 실제 배포로 바꾸어 쓰지 않습니다.
- v1.4: `3bf4d08`의 BUILD/전략 식별자는 실제 V1.4입니다. 별도 `integrate/v1.4-trade-quality-audit-gui`는 BUILD가 V1.3인 다른 통합 지선이라 동일 상태로 합치지 않습니다.
- v1.5: R5 소스 `2da04d3`가 V1.5 BUILD를 명시합니다. R1~R5마다 정식 출시 번호를 새로 만들지 않았습니다.
- v1.6: 운영 manifest의 `eac9bc5`와 92개 파일 해시를 대조했습니다. 90개는 Git blob과 바이트 동일, `gui_menu.cmd`와 `requirements.txt`는 줄바꿈만 다릅니다. 설치된 물리 파일 92개의 manifest 검사는 모두 일치했습니다.
- v1.7: 11월 3일 LIVE 실거래에서 3건(삼기에너지솔루션즈, AP위성, 동진쎄미켐)의 실제 매수·청산 전 과정을 완주한 운영 소스 기준입니다. 이중 진입 경로(초기 압축 돌파 및 눌림목 지지 회복), Lee-Ready 틱 검정, KRX 1틱 스프레드 보정, 구조적 순RR 기하학, 15:10 강제청산 보호를 포함하며 현재 배포된 LIVE 소스(`781de40`)와 물리적으로 일치합니다.

## 이름과 실제 코드의 차이

- R6 명칭의 원래 의미: Git 이력, 커밋 메시지, 브랜치명 어디에도 R6의 단어 단위 약어(acronym) 명시는 확인되지 않았습니다(UNKNOWN). 과거 R5에 이은 내부 개발/실험 계열 식별자(Legacy Development Series)로 정의하며, 공식 사용자 표시는 `v1.7`로 일원화합니다.
- v1.7의 긴 BUILD(`V1.6-risk2-daily4-fast-live-r6-integrated-20261020`)는 계좌 승인 및 내부 추적 식별자로 유지되며, 사용자 버전명과 분리하여 관리합니다.
- v1.7은 실제 실거래 주문 및 청산 루프가 정상 동작함을 확인한 운영 버전이며, 플러스 기대값(장기 수익성)이 검증되었다는 의미가 아닙니다.
