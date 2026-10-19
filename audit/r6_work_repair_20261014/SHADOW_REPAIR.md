# R6 SHADOW 실행 경로 수정 증거

범위: `observation_worker.py`, `shadow_observation.py`, `shadow_fast_runtime.py`, `test_r6_shadow_runtime.py`. 실제 주문·LIVE 시작·운영본·장부 변경은 수행하지 않았다. 인증·실시간 실연결 호출도 이 작업자에서는 수행하지 않았다.

## 실제 수정

1. FAST는 평가 간격과 분리하여 모든 detector가 수락한 체결을 누적한다. receiver time으로 누적하고 processing time으로 신호의 현재성을 검사한다. FAST 담당자가 제공한 `reset/forget`를 일자 변경, 실제 구독 탈락 공백, 큐 sequence gap에 연결했다.
   별도의 `last_evaluated` 시계를 사용하여 로그 중복 억제와 평가 간격도 분리했다.50개 실제 합성 체결은 모두 누적하되2초 평가3회만 실행하는 반례를 통과했다. 메타데이터가 `shadow_view`에 있을 때도 병합한 view를 FAST에 전달하며 저항을 실제 계산한 로컬 시각을 기록한다. 저항이 알려졌는데 영구 UNKNOWN이 되는 호출 배선 결함을 수정했다.
2. raw TRADE와 BOOK을 별도 종류로 기록한다. 거래소 시각·로컬 수신시각·처리시각·구독 원문·원본 시장 필드·증가 sequence가 남는다. 제공되지 않은 depth의 기본 0을 실제 잔량 0으로 기록하지 않는다. 0D에 원래 호가시각21이 없다면 exchange time은 UNKNOWN이며 receiver time으로 대체해 FAST 호가 현재성을 인정하지 않는다.
3. recorder의 log는 메모리 버퍼만 사용한다. 수신 콜백이 파일 I/O를 기다리지 않으며 worker의 주기적 flush가 기록한다. 로테이션 파일은 삭제하지 않고 순번으로 보존한다. write 실패 시 버퍼와 오류 카운터를 보존하고 부분 쓰기의 rollback을 시도한다. 종료 시 큐를 drain하며 flush 실패를 명시적 오류로 반환한다.
4. bounded worker queue의 input/processed sequence 및 drop 구간을 기록한다. 누락 전 대기 프레임은 신호를 만들지 못하며 이후 sequence 경계에서 FAST·ABC를 다시 초기화한다. upstream 증권사 전체 프레임의 손실까지 측정하는 것은 아니다.
5. 독립 SHADOW 실행 진입점을 만들었다. Feed 수신 콜백에서 원시 프레임을 worker에 전달하므로 메인 랭킹 REST 대기와 분리된다. 후보 상태 조회와 실시간 구독에 동일한 선정32개를 사용하고 실제 가상 보유종목을 유지한다. 후보 목록만 바꿔 FAST가 관측하지 못하는 불일치를 방지한다.
6. token file 또는 명시적인 `--source-root` 실행에서만 인증한다. source config·.env는 읽기만 수행한다. LIVE 잠금·프로세스·봇 보유·미체결·UNKNOWN이 있으면 인증 전 차단한다. 독립 출력 폴더와 OS lock을 요구한다. HTTP API allowlist는 auth/rank/status/history만 허용하고 주문·계좌 API를 거절한다. broker 주문 객체와 LIVE engine은 생성하지 않는다.
7. virtual 계좌는 동시1종목, 정수 수량, 매수 예약현금·예약위험, 합산 일일 손실 한도, 실제 다음 신선한 프레임의 ask/bid·잔량을 사용한다. 진입 슬리피지, 청산 슬리피지, 수수료·세금을 각각 한 번 반영한다. 다음 프레임의 비용 포함 RR가 부족하면 진입을 거절한다. 매도는 충분한 신선한 bid를 사용하며 얇은 ask 잔량 때문에 막지 않는다. FULL 가상 매도 완료 뒤에만 재진입 cooldown을 기록한다.

## 실행 검증

```
python -m unittest tests.test_r6_shadow_runtime tests.test_r6_fast_repair tests.test_r6_independent_audit tests.test_shadow_observation tests.test_shadow_capacity_roles -q
```

최종 결과: 71개, failure0/error0, 종료코드0. 이번 SHADOW 전용 신규 검사는19개다. 신호를 주입하지 않은 실제 FAST 알고리즘의 합성 causal 틱도 worker→모델 진입1→tape 종료 보유 유지·실현 왕복0까지 확인했다. 해당 검사에서는 RAW 최상위의 저항을 제거하고 worker와 동일한 shadow_view 위치에만 둔다. 별도 배선 검사는 mock Feed→실제 worker→FAST 신호→후속 호가 모델 진입·청산→TRADE/BOOK/ledger 파일을 확인한다. 이 합성 결과는 시장 수익성 증거가 아니다.
실연결 및 인증에는 token file 경로를 사용하더라도 source-root가 필수다. source-root 없는 CLI check가 collision PASS를 주장하거나 token file로 LIVE 충돌 검사를 우회할 수 없음을 전용 검사로 확인했다.

레거시 `test_tick_logger_cpu_overhead_is_negligible`의100ms assertion은 파일flush I/O를 포함하고 환경에 따라 실패했다. 수정 전50행마다 동기 파일쓰기 방식1,358ms가 관측됐고 capture/flush 분리 뒤120ms 사례도 있었다. 실제 수신콜백은 현재 파일쓰기 없이 enqueue만 수행한다. 해당 시간은 CPU100% 원인이나 실전 부하 결과로 간주하지 않는다. 레거시 MagicMock 비용 fixture 문제도 별도로 부모 작업자에게 보고했다.

## 판정 경계

- 오프라인 실행 연결·누락 감지·기록 보존·종료·주문 차단: PASS.
- 증권사 실제 웹소켓 로그인/구독/파일 기록: 이 작업자에서는 미실행. 부모의 실연결 probe 결과를 별도로 합쳐야 한다.
  부모 확인에서는 R5 LIVE·PID19568이 다시 실행 중이어서 실행 진입점이 `LIVE_WORKER_LOCK_ACTIVE`로 정당하게 차단됐다. 승인 없이 STOP하거나 토큰을 추가 발급하지 않았다. 따라서 실제 SHADOW 개장 준비는 충돌 해소와 실접속 검증이 필요한 조건부 상태다.
- 장전 준비32개 전체의 충분한 관측·09:00 첫 신호·실전 지연·CPU·메모리: 새 개장의 실자료가 필요하다.
- 호가 순번과 주문 대기열이 없으므로 모든 가상 체결은 `MODELED_NOT_BROKER_FILL` 및 `UNKNOWN_WITHOUT_QUEUE_POSITION`이다. 원본 종료를 장 마감으로 취급하거나 보유를 강제 실현하지 않는다.
- 원시 logger의 sequence는 로컬에 도착한 데이터 이후의 큐 유실을 측정한다. 증권사→PC 네트워크 단계의 무손실은 입증되지 않았다.
- SHADOW 준비와 LIVE 실거래 준비·플러스 기대값 입증은 서로 다른 판정이다.
