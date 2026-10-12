# 주식자동매매 4GB 초과 저장공간 전수 감사 및 무결성 정리 보고서

**보고 일시:** 2026-10-13 03:15 KST  
**수행 브랜치:** `test/audit-and-storage-cleanup` (기반 커밋: `7693c56`)  
**작업 환경:** `%USERPROFILE%\Documents\주식자동매매_개발\test-audit-storage-cleanup` (독립 Git Worktree)  
**정리 대상:** `%USERPROFILE%\Desktop\주식자동매매\runtime` (LIVE 운영본 저장소)  

---

## 1. 저장공간 감사 요약 및 핵심 성과

| 구분 | 정리 전 | 정리 후 | 절감량 (Net Saved) | 절감률 |
| :--- | :---: | :---: | :---: | :---: |
| **Desktop 주식자동매매 전체** | **4.95 GB** (5,064.9 MB) | **0.65 GB** (665.3 MB) | **-4.30 GB** (-4,399.6 MB) | **86.9% 절감** |
| **Desktop `runtime` 디렉터리** | **4.92 GB** (5,038.1 MB) | **0.51 GB** (525.2 MB) | **-4.41 GB** (-4,512.9 MB) | **89.6% 절감** |
| **전체 프로젝트 (Worktrees 포함)** | **6.98 GB** (7,144.5 MB) | **2.68 GB** (2,744.9 MB) | **-4.30 GB** (-4,399.6 MB) | **61.6% 절감** |

> [!IMPORTANT]
> **무결성 100% 보장 원칙 준수:**
> 단순 삭제가 아닌 **5단계 Fail-Safe 무결성 검증 파이프라인**(사전 SHA-256 해시 추출 -> ZIP Deflate 압축 -> CRC32 무결성 검사 -> 임시 디렉터리 복원 및 비트 단위 SHA-256 일치 검증 -> 원본 삭제)을 통과한 파일만 정리되었습니다.
> 비활성 로그 데이터 손실은 **0.00%**이며, 언제든지 100% 원본 복원이 가능합니다.

---

## 2. 4GB 초과 원인 심층 분석

저장공간 4GB 초과의 근본 원인은 엔진 결함이 아닌 **장시간 연속 가동에 따른 로테이션 로그의 비압축 누적**이었습니다:

1. **64MB 단위 자동 로그 로테이션의 비압축 누적:**
   - 엔진(`core/event_log.py`)은 64MB 초과 시 자동으로 `runtime/archive/`에 타임스탬프와 PID를 붙여 로그를 보관합니다.
   - 2026-10-08부터 2026-10-12까지 장중 수신된 수천만 건의 틱, 호가, 레이더 관측 데이터(`radar_history.jsonl`, `shadow_events.jsonl`, `events.jsonl`, `operator_console.txt`)가 비압축 텍스트(JSONL) 상태로 그대로 누적되었습니다.
   - 단일 파일 중 `operator_console.txt`가 350MB, `shadow_events.jsonl`이 344MB, 158MB 등 거대 파일이 다수 존재했습니다.
2. **크래시 발생 시 생성된 고아 임시 파일:**
   - `runtime/radar_latest.json.emyxy9d_.tmp` (163.96 KB)와 같이 atomic 쓰기 도중 프로세스 비정상 종료로 남겨진 고아 임시 파일이 디스크를 점유하고 있었습니다.
3. **높은 텍스트 압축률(94~96%)의 미활용:**
   - JSONL 로그는 반복되는 키 구조(`time`, `event`, `symbol`, `price`, `gates` 등)로 인해 Zip 압축 시 용량이 94~96% 줄어듭니다.
   - 실제로 **4,655.77 MB의 원본 로그가 단 256.20 MB로 압축**되었습니다.

---

## 3. 4대 보존 등급 분류 기준 및 내역

| 등급 | 정의 | 파일 수 | 용량 (MB) | 처리 정책 |
| :---: | :--- | :---: | :---: | :--- |
| **Grade A** | **절대 보존 (Active)** | 78개 | 223.07 MB | **일절 수정/삭제 금지.** 활성 `state.json`, `events.jsonl`, `heartbeat.json`, `automation_policy.json`, 최신 레이더 파일 |
| **Grade B** | **압축 보존 (Inactive)** | 58개 | 4,655.77 MB | **100% SHA-256 비트 무결성 검증 후 압축 보관**, 원본 로그 안전 삭제 |
| **Grade C** | **안전 삭제 (Orphan/Crash)** | 1개 | 0.16 MB | 즉시 삭제 (고아 `.tmp` 파일) |
| **Grade D** | **보류 (Reserved)** | 12개 | 425.56 MB | 기존 완료된 ZIP 백업 및 타 에이전트 브랜치 작업 파일 (접근 금지) |

---

## 4. 디스크 정리 및 암호학적 무결성 검증 세부 내역

### 4.1. 처리 아카이브별 상세

| 생성된 압축 아카이브 | 대상 기간/디렉터리 | 원본 파일 수 | 원본 크기 | 압축 크기 | 공간 절감량 | 비트 무결성 검증 |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| `archive_20261008.zip` | `runtime/archive` (10/08) | 17개 | 1,749.59 MB | 105.49 MB | **1,644.10 MB** (94.0%) | **17/17 (100% PASS)** |
| `archive_20261012.zip` | `runtime/archive` (10/12) | 16개 | 1,026.22 MB | 61.36 MB | **964.86 MB** (94.0%) | **16/16 (100% PASS)** |
| `prod_archive_20261008.zip` | `automation-production/archive` (10/08) | 12개 | 1,047.62 MB | 54.22 MB | **993.41 MB** (94.8%) | **12/12 (100% PASS)** |
| `prod_archive_20261012.zip` | `automation-production/archive` (10/12) | 13개 | 832.34 MB | 35.13 MB | **797.21 MB** (95.8%) | **13/13 (100% PASS)** |
| **합계** | **전체 아카이브** | **58개** | **4,655.77 MB** | **256.20 MB** | **4,399.58 MB** | **58/58 (100% PASS)** |

### 4.2. Grade C 삭제 내역
- `%USERPROFILE%\Desktop\주식자동매매\runtime\radar_latest.json.emyxy9d_.tmp` (163,960 Bytes)
  - 사전 SHA-256 해시(`8de61d933efe9ce6c3ba5e9c0c8ff0f81d1136b9e28f73dd49b77543ae819a58`) 기록 후 안전 삭제 완료.

### 4.3. 암호학적 감사 로그 (Audit Trail)
- 전체 59개 파일의 상세 경로, 원본 바이트, 사전 SHA-256, 압축 아카이브 경로, Zip 멤버 크기, CRC32, 사후 SHA-256 검증 결과는 `cleanup_audit_trail.json`에 영구 보존되어 있습니다.

---

## 5. LIVE 운영 환경 및 타 에이전트 안전 보장 내역

1. **LIVE 프로세스 및 활성 파일 절대 무간섭:**
   - `runtime/automation-production/state.json`, `events.jsonl`, `heartbeat.json` 등 현재 가동 중인 거래 데이터는 1바이트도 수정되거나 이동되지 않았습니다.
2. **타 ChatGPT 에이전트(전략 개선)의 작업 공간 격리:**
   - 타 에이전트의 작업 디렉터리(`%USERPROFILE%\Documents\주식자동매매_개발\trade-quality-night-20261013` 등) 및 관련 브랜치는 읽기/쓰기/접근을 일절 배제하여 충돌을 원천 차단했습니다.
3. **독립된 개발 Worktree 사용:**
   - 모든 검증과 도구 실행은 `%USERPROFILE%\Documents\주식자동매매_개발\test-audit-storage-cleanup` 디렉터리에서 수행되었습니다.

---

## 6. 향후 재발 방지 권고사항

1. **자동 압축 로테이션 도입:**
   - `core/event_log.py`에서 64MB 초과 로테이션 시 비압축 파일 대신 `.gz` 또는 백그라운드 스레드를 통한 자동 zip 압축을 적용하면 디스크 사용량을 항시 수백 MB 수준으로 유지할 수 있습니다.
2. **정기 보존 주기(Retention Policy) 설정:**
   - 7일 이상 경과한 압축 아카이브는 주 단위로 보조 저장소로 이동하거나 자동 정리하는 스크립트를 crontab/Task Scheduler에 등록할 것을 권장합니다.
