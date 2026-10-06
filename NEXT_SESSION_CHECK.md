# V1.3 다음 정규장 확인 절차

빌드 `V1.3-preopen-risk2-20261007`. 프로그램 재가동과 실제 LIVE는 사용자 결정입니다. 다음 검사는 오프라인 결과를 실제 계좌/시장 성공으로 확대 해석하지 않기 위한 절차입니다.

1. **08:00 이후** 바탕화면 「주식자동매매」 안의 실행파일을 엽니다. 빌드·현재 모드/감시 실행 여부·실제 주문 권한을 구분하고 보호정책·봇 잔량·미체결·불명확·HALT를 확인합니다.
2. **자동매매 시작**으로 준비를 시작합니다. 고정 시간 검사를 반복하지 않습니다. 로컬 정상과 실제 account/auth/WS 확인을 구분합니다. 08시 REST/account/auth 가능 여부가 실제로 확인되지 않으면 미확인 상태와 원문 오류를 보존합니다. 개장 전 regular tick 부재는 그 자체로 실패가 아닙니다.
3. 실제 오늘 LIVE 운용을 선택하면 **현재 승인 cap**과 명시 승인 문구를 직접 확인합니다. 확인된 실제 비용과 현재 위험실험 값을 검토합니다. cap 20,000원·risk2%·daily4%·max_positions1·max_entries3이며 프로그램이 임의로 cap이나 승인을 늘리지 않습니다. 이 작업자가 실제 승인을 자동 생성하지 않습니다.
4. **09:00** 첫 regular tick부터 관측되는지 확인합니다. market 정규장, WS/REG ACK, 신선한 regular tick, 신선한 account, radar pipeline 증거가 충족되면 즉시 OPEN_READY여야 합니다. 필요한 증거가 없으면 BUY=0과 개별 차단 사유가 보여야 합니다. preopen 승인+runner 준비가 유효하면 개장 때 별도 버튼을 누르지 않습니다.
5. OPEN_READY와 전략 준비를 구분합니다. 70초 buffer·A2/B4/C5 유효 완성봉은 그대로이며 premarket 틱/봉은 제외됩니다. SHADOW/WATCH와 fast promotion, source2 주경로/source1 빈칸 보조, warmup 보존, 두 heartbeat를 확인합니다.
6. 시작 snapshot/ENTRY_EVAL/RiskGate의 build/strategy/experiment와 risk_fraction·max_daily_loss_fraction·allowed_risk_krw·remaining_daily_risk_krw를 확인합니다. 초기 20,000원 자본이라면 400원/800원이고 실제 남은 위험은 실현손실/예약위험을 차감합니다. 조건이 없으면 NO_TRADE가 정상입니다.
7. 실제 체결이 발생하면 초기 무효화·수량·보호선·MFE/MAE·청산의도와 체결/잔량을 따로 확인합니다. 일일 4% 한도는 신규매수만 차단하고 기존 보호는 계속합니다. API 단절·부분체결·거절·미체결은 실제 확인 전 성공으로 표시하지 않습니다.
8. **14:55 BUY 차단 / 15:10 봇 청산 / 15:20 지원 종료**를 확인합니다. STOP은 보호 유지, STOP_FLAT은 봇만 청산, KILL은 기존 긴급 절차입니다. HARD_LOCK/외부보유 보호 유지와 미청산 잔량을 HTS에서 확인합니다.

설정/cap/build 변경 시 당일 승인도 무효이며 다음 거래일은 별도 사용자 승인이 필요합니다. build가 다른 과거 readiness 증빙을 재사용하지 않습니다. 저장 자료를 지우거나 cap/비용을 억지로 바꿔 검사 조건을 맞추지 않습니다.

실시장 08시 API 가용성, 첫 틱 수신 지연, 장시간 성능, 실제 주문/청산 체결 품질, 슬리피지, 수익성은 오프라인 테스트로 확정되지 않습니다. 실제 승인/계좌/시장 로그는 Git 밖에 보관합니다.
