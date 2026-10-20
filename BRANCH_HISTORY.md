# 브랜치 정리 기록

GitHub 원격 ref를 정리했고, 코드·커밋·현재 작업 트리·로컬 브랜치를 삭제하지 않았습니다. 새 `dev`는 운영 기준 `eac9bc5`에서 시작합니다. 현재 연구는 `research/backtest`와 Draft PR #2에 보존하며 정식 출시하지 않습니다.

## 실제 정리한 원격 ref

| 이전 브랜치 | 마지막 SHA | 보존 위치 | 결과 |
|---|---|---|---|
| `feat/r6-opportunity-evidence-20261019` | [`64f1b55`](https://github.com/neomusbist/stock-trading-bot/commit/64f1b5567a8d082783d5f8227e8aafcb3a060a73) | `v1.6`의 동일 tip 또는 포함 이력 | 원격 중복 ref 삭제, force 없음 |
| `feat/v1.5-entry-quality-20261013` | [`2c47544`](https://github.com/neomusbist/stock-trading-bot/commit/2c475446abd873c325dda51385057b0f381e3dee) | `v1.6`의 동일 tip 또는 포함 이력 | 원격 중복 ref 삭제, force 없음 |
| `fix/continuous-trading-20261012` | [`7693c56`](https://github.com/neomusbist/stock-trading-bot/commit/7693c563d1d42d4c4467f17a85564743df0c4824) | `v1.6`의 동일 tip 또는 포함 이력 | 원격 중복 ref 삭제, force 없음 |
| `fix/r6-watch-pipeline-20261019` | [`a04e93a`](https://github.com/neomusbist/stock-trading-bot/commit/a04e93a0a51652e9f4ad37fc4689863c69e81381) | `v1.6`의 동일 tip 또는 포함 이력 | 원격 중복 ref 삭제, force 없음 |
| `fix/r6-work-repair-20261014` | [`fec9845`](https://github.com/neomusbist/stock-trading-bot/commit/fec98452bd21670b99f41fbda87fd53a2d161ffd) | `v1.6`의 동일 tip 또는 포함 이력 | 원격 중복 ref 삭제, force 없음 |
| `fix/v1.5-opening-live-20261014` | [`1c29b35`](https://github.com/neomusbist/stock-trading-bot/commit/1c29b35567423752f01908098e6f49fcd9038cef) | `v1.6`의 동일 tip 또는 포함 이력 | 원격 중복 ref 삭제, force 없음 |
| `test/audit-and-storage-cleanup` | [`9464649`](https://github.com/neomusbist/stock-trading-bot/commit/94646498ec52c0ded97a2130dd3dfc712d58ffbe) | `v1.6`의 동일 tip 또는 포함 이력 | 원격 중복 ref 삭제, force 없음 |
| `fix/r6-causal-validation-20261020` | [`72da577`](https://github.com/neomusbist/stock-trading-bot/commit/72da577430fe43796040c1861a0c40dbde4fb064) | `research/backtest`의 동일 tip 또는 포함 이력 | 원격 중복 ref 삭제, force 없음 |

앞의 일곱 ref는 v1.6 및 dev 이력에 모든 커밋이 포함됨을 확인했습니다. 마지막 ref는 이 작업 중 생성했던 긴 연구 이름을 짧은 research/backtest로 바꾼 것입니다. 열린 PR·현재 작업 트리와 연결되지 않았고 삭제 직전 원격 SHA를 다시 대조했습니다. 개별 기능이 현재 모두 활성화됐다는 뜻은 아닙니다.

## 유지한 원격 브랜치

이 표는 8개 ref 정리 직후의 원격 스냅샷입니다. 이 문서를 게시하는 `docs/versions`는 별도의 일시 문서 작업 브랜치이며 프로그램 소스 변경을 포함하지 않습니다.

| 브랜치 | SHA | 분류 / 보존 이유 |
|---|---|---|
| `baseline/r6-live-5a4264c-20261020` | `5a4264c` | 열린 PR의 head/base이므로 보존 |
| `dev` | `eac9bc5` | 차기 통합 기준, 현재 운영 소스에서 시작 |
| `feat/setup-o-opening-momentum` | `3bf4d08` | 로컬 작업 트리에 연결돼 있어 보존 |
| `feat/v1.6-fast-momentum-r6-20261014` | `3ad45e1` | 로컬 작업 트리에 연결돼 있어 보존 |
| `fix/r6-capture-consistency-20261020` | `8174b5d` | 열린 PR의 head/base이므로 보존 |
| `fix/r6-live-integration-20261019` | `40bbfe0` | 로컬 작업 트리에 연결돼 있어 보존 |
| `fix/r6-midday-focus-20261019` | `64e5157` | 로컬 작업 트리에 연결돼 있어 보존 |
| `fix/trade-quality-night-20261013` | `a8c4ea8` | 로컬 작업 트리에 연결돼 있어 보존 |
| `fix/v1.5-exit-lifecycle-20261013` | `d4c0647` | 로컬 작업 트리에 연결돼 있어 보존 |
| `fix/v1.5-exit-plan-20261013` | `d602fc8` | 로컬 작업 트리에 연결돼 있어 보존 |
| `fix/v1.5-live-observation-r5-20261014` | `2da04d3` | 로컬 작업 트리에 연결돼 있어 보존 |
| `fix/v1.5-night-quality-20261014` | `2574146` | 로컬 작업 트리에 연결돼 있어 보존 |
| `fix/v1.5-observation-watch-20261013` | `e49a18b` | 로컬 작업 트리에 연결돼 있어 보존 |
| `fix/v1.5-stability-entry-exit-20261013` | `a43352d` | 로컬 작업 트리에 연결돼 있어 보존 |
| `fix/v1.5-structural-a-exit-20261013` | `718a7ae` | 로컬 작업 트리에 연결돼 있어 보존 |
| `fix/v1.5-structural-b-20261013` | `ed5f76c` | 로컬 작업 트리에 연결돼 있어 보존 |
| `integrate/v1.4-trade-quality-audit-gui` | `abb2981` | 로컬 작업 트리에 연결돼 있어 보존 |
| `main` | `1bbb215` | 기본 브랜치, 과거 프로그램 소스 보존; 문서만 별도 갱신 |
| `perf/gui-latency-optimization` | `274c6e0` | 로컬 작업 트리에 연결돼 있어 보존 |
| `research/backtest` | `72da577` | 현재 전략 연구, Draft PR #2; v1.7 목표·미출시 |

## 로컬 브랜치 전수 검토

| 로컬 브랜치 | SHA | 현재 작업 트리 | 판단 |
|---|---|---|---|
| `audit/nightly-quality-20261013` | `7693c56` | 연결 | 활성 작업 트리 보존 |
| `candidate/v1.6-fast-live-r6-20261015` | `f158bb6` | 연결 | 활성 작업 트리 보존 |
| `docs/versions` | `1bbb215` | 연결 | 활성 작업 트리 보존 |
| `feat/r6-opportunity-evidence-20261019` | `3e7d542` | 없음 | 로컬 참조 보존, 삭제·reset·pull 없음 |
| `feat/setup-o-opening-momentum` | `3bf4d08` | 연결 | 활성 작업 트리 보존 |
| `feat/v1.5-entry-quality-20261013` | `2c47544` | 없음 | 로컬 참조 보존, 삭제·reset·pull 없음 |
| `feat/v1.6-fast-live-r6-20261014` | `0da27dd` | 연결 | 활성 작업 트리 보존 |
| `feat/v1.6-fast-momentum-r6-20261014` | `2affd6d` | 연결 | 활성 작업 트리 보존 |
| `fix/continuous-trading-20261012` | `7693c56` | 없음 | 로컬 참조 보존, 삭제·reset·pull 없음 |
| `fix/r6-0925-latency-20261020` | `5dc2d0d` | 없음 | 로컬 참조 보존, 삭제·reset·pull 없음 |
| `fix/r6-capture-consistency-20261020` | `8174b5d` | 연결 | 활성 작업 트리 보존 |
| `fix/r6-independent-execution-20261019` | `1a6a153` | 연결 | 활성 작업 트리 보존 |
| `fix/r6-live-integration-20261019` | `40bbfe0` | 연결 | 활성 작업 트리 보존 |
| `fix/r6-midday-focus-20261019` | `557e83c` | 연결 | 활성 작업 트리 보존 |
| `fix/r6-opening-loop-20261019` | `eda73b9` | 없음 | 로컬 참조 보존, 삭제·reset·pull 없음 |
| `fix/r6-readiness-observation-20261020` | `5a4264c` | 연결 | 활성 작업 트리 보존 |
| `fix/r6-target-cap-incident-20261015` | `7e60406` | 없음 | 로컬 참조 보존, 삭제·reset·pull 없음 |
| `fix/r6-watch-pipeline-20261019` | `7f74491` | 없음 | 로컬 참조 보존, 삭제·reset·pull 없음 |
| `fix/r6-work-repair-20261014` | `fec9845` | 없음 | 로컬 참조 보존, 삭제·reset·pull 없음 |
| `fix/trade-quality-night-20261013` | `a8c4ea8` | 연결 | 활성 작업 트리 보존 |
| `fix/v1.5-exit-lifecycle-20261013` | `d4c0647` | 연결 | 활성 작업 트리 보존 |
| `fix/v1.5-exit-plan-20261013` | `d602fc8` | 연결 | 활성 작업 트리 보존 |
| `fix/v1.5-live-observation-r5-20261014` | `2da04d3` | 연결 | 활성 작업 트리 보존 |
| `fix/v1.5-night-quality-20261014` | `2574146` | 연결 | 활성 작업 트리 보존 |
| `fix/v1.5-observation-watch-20261013` | `e49a18b` | 연결 | 활성 작업 트리 보존 |
| `fix/v1.5-opening-live-20261014` | `1c29b35` | 없음 | 로컬 참조 보존, 삭제·reset·pull 없음 |
| `fix/v1.5-r4-opportunity-fix-20261014` | `e0d2f7a` | 연결 | 활성 작업 트리 보존 |
| `fix/v1.5-stability-entry-exit-20261013` | `a43352d` | 연결 | 활성 작업 트리 보존 |
| `fix/v1.5-structural-a-exit-20261013` | `718a7ae` | 연결 | 활성 작업 트리 보존 |
| `fix/v1.5-structural-b-20261013` | `ed5f76c` | 연결 | 활성 작업 트리 보존 |
| `fix/weekend-stabilization-20261008` | `feefc5c` | 연결 | 활성 작업 트리 보존 |
| `gui_cockpit` | `fa4f29b` | 연결 | 활성 작업 트리 보존 |
| `inspect_repo_status` | `460b399` | 연결 | 활성 작업 트리 보존 |
| `integrate/v1.4-trade-quality-audit-gui` | `abb2981` | 연결 | 활성 작업 트리 보존 |
| `main` | `3c6fb27` | 연결 | 활성 작업 트리 보존 |
| `perf/gui-latency-optimization` | `274c6e0` | 연결 | 활성 작업 트리 보존 |
| `research/backtest` | `72da577` | 연결 | 활성 작업 트리 보존 |
| `research/r6-evidence-shadow-20261020` | `d601d9e` | 연결 | 활성 작업 트리 보존 |
| `research/v1.5-frequency-capacity-r5-20261014` | `bd30749` | 없음 | 로컬 참조 보존, 삭제·reset·pull 없음 |
| `test/audit-and-storage-cleanup` | `9464649` | 없음 | 로컬 참조 보존, 삭제·reset·pull 없음 |

로컬 main은 원격 main과 동일하다고 가정하지 않았습니다. 사용자의 기존 GitHub Desktop 작업 트리와 미푸시 체크포인트를 그대로 유지했습니다.

## 별도 승인이 필요한 범위

- main의 프로그램 소스를 현재 LIVE 기준으로 통합하는 변경. 문서 갱신과 별개이며 현재 기본 브랜치 및 배포 참조는 바꾸지 않습니다.
- 미병합 중요 코드·진행 중 PR·작업 트리에 연결된 브랜치 삭제.
- 기존 태그 이동, 정식 기준 변경, force-push, 이력 재작성.
- 실제 LIVE 파일 교체·재시작·주문·설정·장부·위험 한도 변경.
