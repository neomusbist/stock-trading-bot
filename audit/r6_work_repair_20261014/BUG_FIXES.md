# 실제 결함과 변경

| 파일 | 결함 | 수정 및 반례 |
|---|---|---|
| fast_momentum.py | 고정 0.9/1.8% 손절과 계산상 RR를 맞춘 목표가 | 진입 전 확정 미시 지지·당시 시각을 가진 저항·호가/노이즈 여유·비상선 비용으로 수량 계산. 단조 상승·마지막 체결 자기확인·미래/누락 저항·잔량 없음은 거절 |
| fast_momentum.py | 같은 가격도 매수세로 집계, 중복·역순·누적량 후퇴 | 중립 체결 분리 및 원시 순서/날짜/수신 현재성 검증, 자료 공백 초기화 |
| fast_momentum.py | 한 번의 soft line 접촉/고정 수익률/보유 시간만으로 청산 | 두 독립 체결과 bid의 정상 무효화 침범 확인. 신선한 비상 bid/계좌 위험 초과는 즉시. 확정 lower high 실패와 post-fill higher low 보호로 변경 |
| shadow_observation.py | 평가 주기 때문에 FAST 틱 입력도 간헐적, bare RAW만으로 저항 누락 | 모든 수락 체결 ingest, 평가/로그 시계 분리, 실제 evaluation view 및 저항 계산 asof 전달 |
| observation_worker.py | 수신/메인 producer sequence 경합, callback 전 처리 완료 표시 | 짧은 delivery lock, drop 구간 복사, FAST·모델 callback 뒤 processed sequence |
| observation_worker.py | 파일 flush가 capture를 막고 잔량 기본 0이 실제 데이터처럼 저장 | capture는 메모리만, TRADE/BOOK·exchange/receiver/process·미제공 depth UNKNOWN·순번·유실 범위·회전/오류/종료 flush |
| shadow_fast_runtime.py | 독립 실제 SHADOW 진입점 없음 | source-root·출력 분리·LIVE 충돌 검사·실주문과 계좌 HTTP 차단·raw callback·후속 호가 가상 체결·tape 끝 보유 유지 |
| shadow_fast_runtime.py | 구독 32개와 metadata 선정 32개가 다를 수 있음 | 동일 ranked32 목록 및 가상 보유 보호 연결 |
| runner.py | 같은 WAITING 내 ON/OFF 로그 누락, 수신기 실패가 메인 큐 처리 전 반영 안 됨 | boolean/사유 전환 로그, transport_failed 즉시 반영 |
| operator_service.py / operator_ui_service.py | readiness 없으면 기본 ON, 저장된 나이가 화면에서 정지 | 공유한 읽기 시점 판정과 UNKNOWN/OFF. 명시적 엔진 OFF를 ON으로 복원하지 않음 |
| replay.py | 호가 추정 청산·총 손익으로 일일 한도 충당·손익비 및 현금 가정 | 없는 호가 유지, 누적 손실, 승인자본 상한, 정수 수량, 틱 이동/지연/비용 분리, 미청산 별도 원장 |
| 기존 FAST/logger 테스트 | 독립 지지·현재성 없는 신호나 자동 1R 보호, 디스크 I/O 포함 100ms가 정답 | 기존 파일을 삭제하지 않고 안전 전제와 반례에 맞춰 수정. capture 동기 I/O 금지를 실제로 확인 |

일반 시장에서 지지/저항 검사가 너무 보수적인지, 정상 수익 기회를 얼마나 거절하는지, 재진입 cooldown 15분의 적합성은 미검증이다. 고정 손절폭을 넓혀 해결한 것이 아니며 새로운 시장 자료 없이 임계값을 최적화하지 않았다. 기존 AE3/A/B/C·LIVE 승인·5초 최종 주문 경계·수동 보호정책은 유지했다.
