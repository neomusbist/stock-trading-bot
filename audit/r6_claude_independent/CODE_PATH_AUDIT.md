# CODE_PATH_AUDIT (FAST 전략 실제 호출 경로 및 SHADOW 주문 차단 검증)

## 1. FAST 전략 실제 실행 경로 추적

FAST 전략(`FastMomentumStrategy`)의 실제 실행 파이프라인은 다음과 같이 완전히 연결되어 있습니다:

```
[WebSocket 체결/호가 수신]
        │
        ▼
[runner.py:733 ObservationWorker.deliver(tick, received)]
        │ (메인 주문 루프와 분리된 전용 진단 큐)
        ▼
[observation_worker.py:248 TickLogger.log(t, received)]
        │ (손실 방지 틱 파일 기록: shadow_ticks.jsonl)
        ▼
[observation_worker.py:252 ShadowObserver.tick(t, clock, transport_received)]
        │
        ├─► [fast_strategy.ingest_tick(t)] (모든 수신 틱을 실시간 MicroTape에 100% 누적)
        │
        ▼ (2초 진단 주기 및 평가 조건 충족 시)
[fast_strategy.evaluate(t, row, ingest=False)]
        │ (Regime A 판정, 4단계 무효화/목표가/수량 산출, Net RR >= 1.5 검사)
        ▼
[ShadowObserver.event('SHADOW_FAST_SIGNAL', ...)]
        │ (SHADOW_ONLY=True, actual_order_permission=False)
        ▼
[shadow_events.jsonl 기록 및 GUI 상태 갱신] (실주문 API 호출 0건)
```

---

## 2. 발견된 구조적 결함 및 실제 코드 수정

### (1) MicroTape 틱 결손(Starvation) 결함 수정
- **결함 내용**:
  - 기존 코드에서는 `ShadowObserver.tick`의 2초 평가 쓰로틀(`eval_seconds=2`) 내부에서만 `fast_strategy.evaluate()`를 호출했고, 그 안에서 `ingest_tick()`이 호출되었습니다.
  - 이로 인해 초당 5~10개의 틱이 들어와도 FAST 전략의 `MicroTape`에는 2초당 1개의 틱만 누적되어, 10초 틱 속도 게이트(`ticks_10 >= 6`)가 구조적으로 통과할 수 없는 결함이 있었습니다.
- **수정 내용**:
  - `automation/shadow_observation.py`의 `tick()` 진입부에서 검증된 모든 틱에 대해 `self.fast_strategy.ingest_tick(t)`를 즉시 호출하도록 수정.
  - `fast_strategy.evaluate()`에 `ingest=False` 인자를 추가하여 중복 누적을 방지하고 쓰로틀링과 무관하게 온전한 마이크로 테이프를 형성하도록 개선.
  - [`tests/test_r6_independent_audit.py`](file:///C:/Users/MJS/Documents/주식자동매매_개발/v15-frequency-capacity-r5-20261014/tests/test_r6_independent_audit.py)의 `test_every_tick_reaches_fast_micro_tape_despite_eval_throttle` 단위 테스트로 검증 완료 (60틱 수신 시 60틱 전부 마이크로 테이프에 유지).

### (2) `Costs` 객체 타입 호환 결함 수정
- **결함 내용**:
  - `FastMomentumStrategy` 내부에서 `self.costs.get(...)` 딕셔너리 메서드를 호출하여, `core.models.Costs` 데이터클래스 인스턴스가 전달될 경우 `AttributeError`가 발생하던 결함.
- **수정 내용**:
  - `automation/fast_momentum.py`의 `__init__`에서 `Costs` 인스턴스를 표준 딕셔너리로 정규화하도록 수정.

---

## 3. SHADOW 프로세스의 실주문 원천 격리 증거 (Static & Dynamic Isolation)

1. **정적 AST 감사 (Static AST Audit)**:
   - `automation/observation_worker.py`, `automation/shadow_observation.py`, `automation/fast_momentum.py` 세 모듈에 대해 AST 전수 조사를 실시한 결과:
     - `core.broker`, `automation.engine` 모듈에 대한 import **0건**.
     - `submit`, `enter`, `place_order`, `send_order`, `cancel_order` 등 주문 메서드 정의 및 호출 **0건**.
     - `ObservationWorker`가 REST 클라이언트에서 호출하는 메서드는 읽기 전용 상태 조회(`stock_status`)가 유일함.
2. **동적 런타임 잠금 (Dynamic Runtime Lock)**:
   - `ShadowObserver` 객체는 `broker`, `engine`, `enter` 속성을 일체 보유하지 않음.
   - 모든 진단 이벤트는 `actual_order_permission=False`, `SHADOW_ONLY=True`를 필수 필드로 방출함.
   - [`tests/test_r6_independent_audit.py`](file:///C:/Users/MJS/Documents/주식자동매매_개발/v15-frequency-capacity-r5-20261014/tests/test_r6_independent_audit.py)의 `test_observation_modules_expose_no_order_surface` 테스트로 무결성 검증 완료.
