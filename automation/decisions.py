"""events.jsonl에 동일 스키마로 기록하는 작은 어댑터. 새 DB나 매 틱 로그 없음."""
import json
import math
STRATEGY_VERSION='V1.3-b-confirmed-impulse-bc20M-20261008'
BUILD_ID='V1.3-risk2-watch12-bc20M-stabilization-20261012'
STRATEGY_EXPERIMENT='20261007-risk2-daily4-dynamiccap-watch12-accelA-bc20M-bclose20261008'

def check_experiment_consistency(cfg, build_id=BUILD_ID, experiment=STRATEGY_EXPERIMENT):
    """BUILD_ID 또는 STRATEGY_EXPERIMENT에 risk2/daily4가 적혀 있는데
    runtime risk_fraction/max_daily_loss_fraction이 0.02/0.04가 아니면 ValueError를 발생시킨다."""
    mismatches = []
    if 'risk2' in str(build_id) or 'risk2' in str(experiment):
        val = getattr(cfg, 'risk_fraction', None)
        if val is None or abs(float(val) - 0.02) > 1e-6:
            mismatches.append(f"risk2 declared in {build_id}/{experiment}, but risk_fraction={val} != 0.02")
    if 'daily4' in str(build_id) or 'daily4' in str(experiment):
        val = getattr(cfg, 'max_daily_loss_fraction', None)
        if val is None or abs(float(val) - 0.04) > 1e-6:
            mismatches.append(f"daily4 declared in {build_id}/{experiment}, but max_daily_loss_fraction={val} != 0.04")
    if mismatches:
        raise ValueError("EXPERIMENT_CONSISTENCY_FAILURE: " + "; ".join(mismatches))
    return True

def reason_code(reason,position):
    exact={'승인 자동매매 자본 0원':'AUTO_CAP_ZERO','기존 보유: 추가매수 금지':'EXTERNAL_HOLD_NO_BUY',
           'LIVE 명시 승인 누락':'LIVE_NOT_AUTHORIZED','live_mode=false':'LIVE_MODE_DISABLED',
           '거래비용 설정 미확인':'COSTS_UNCONFIRMED','연속손실 한도':'CONSECUTIVE_LOSS_LIMIT',
           '비용 포함 효율 부족/UNKNOWN':'EFFICIENCY_UNKNOWN_OR_LOW','당일 청산 시각':'SELL_SESSION_FLATTEN',
           '사용자 STOP_FLAT':'SELL_STOP_FLAT','사용자 KILL':'SELL_EMERGENCY','시세 지연/연결 단절':'SELL_STALE_QUOTE',
           '전일 미청산 봇 잔고':'SELL_PREVIOUS_SESSION_REMAINS','HARD_LOCK':'MANUAL_HOLD',
           'BREAKEVEN_EXIT':'SELL_BREAKEVEN_EXIT','TEST_CAPITAL_RELEASE':'SELL_EXPLICIT_TEST_RELEASE',
           'policy':'NO_BUY_POLICY_BLOCK','risk_quantity':'NO_BUY_MIN_QTY_ZERO','repeat_slots':'NO_BUY_REPEAT_OR_SLOT_LIMIT',
           'connection_account':'NO_BUY_ACCOUNT_OR_CONNECTION','session':'NO_BUY_OUTSIDE_ENTRY_WINDOW',
           'tick':'NO_BUY_STALE_OR_NON_REGULAR_TICK','efficiency':'NO_BUY_EFFICIENCY'}
    if reason=='구조/이익보호선 이탈':
        return 'SELL_PROFIT_PROTECTION_HIT' if position.get('stop',0)>position.get('initial_stop',0) else 'SELL_INVALIDATION_HIT'
    if reason in exact: return exact[reason]
    if isinstance(reason,str) and reason.endswith(' 신규 실주문 금지'): return 'MODE_'+reason.split()[0]+'_NO_NEW_ENTRY'
    return reason

def _safe_walk(item,memo):
    if item is None or type(item) in (str,int,bool):return item
    if isinstance(item,float):return item if math.isfinite(item) else 'NON_FINITE:'+repr(item)
    if isinstance(item,(dict,tuple,list)):
        key=id(item)
        if key in memo:return memo[key]
        result={k:_safe_walk(v,memo) for k,v in item.items()} if isinstance(item,dict) else [_safe_walk(v,memo) for v in item]
        memo[key]=result;return result
    return item

def safe_values(value):
    # Memo belongs to this serialization only; no strategy/safety state cache.
    return _safe_walk(value,{})


def gate(passed,**values):
    return {'result':'UNKNOWN' if passed is None else 'PASS' if passed else 'FAIL','values':safe_values(values)}

class DecisionLog:
    def __init__(self,engine): self.engine=engine; self.last={}
    def emit(self,kind,symbol,decision,reason,gates=None,position=None,signal=None,price=None,extra=None,force=False):
        e=self.engine; p=position or {}; sig=signal or p.get('signal') or {}; extra=extra or {}
        gates=gates or {}; key=(kind,symbol)
        reason=reason_code(reason,p)
        if kind=='ENTRY_EVAL' and hasattr(e,'candidate_audit'):
            e.candidate_audit.observe(symbol,reason,gates,decision,{'timestamp':e.clock().isoformat(),'current_price':price,'setup_trials':sig.get('setup_trials',extra.get('setup_trials'))})
        state=('EXIT_PENDING' if p.get('exit_requested') else 'BOT_POSITION') if p else e.policy.mode
        if sig.get('ownership')=='EXISTING_POLICY': state='EXTERNAL_POLICY_PENDING' if e.active_orders(symbol) else 'EXTERNAL_POLICY_HOLD'
        signature=json.dumps([state,decision,reason,p.get('stop'),p.get('qty'),
                              {k:v['result'] for k,v in gates.items()},sig.get('pattern')],sort_keys=True)
        stamp=e.clock().timestamp(); prev=self.last.get(key)
        if not force and prev and prev[0]==signature and stamp-prev[1]<e.a.decision_snapshot_seconds: return
        self.last[key]=(signature,stamp)
        risk_context=e.capital_context() if hasattr(e,'capital_context') else {}
        risk_gate_values=gates.get('risk_quantity',{}).get('values',{})
        # 수량 판단 뒤 주문/자금 예약이 생겨도 해당 RiskGate가 실제 사용한 기준을 보존한다.
        risk_equity=risk_gate_values.get('equity',e.risk_equity())
        risk_remaining=risk_gate_values.get('remaining_risk',risk_context.get('risk_remaining',
            max(0,risk_equity*e.c.max_daily_loss_fraction-e.s.get('daily_loss',0))))
        risk_identity={'risk_fraction':e.c.risk_fraction,'max_daily_loss_fraction':e.c.max_daily_loss_fraction,
                       'allowed_risk_krw':risk_equity*e.c.risk_fraction,
                       'remaining_daily_risk_krw':max(0,risk_remaining)}
        if 'risk_quantity' in gates:
            gates={**gates,'risk_quantity':{**gates['risk_quantity'],'values':{**gates['risk_quantity'].get('values',{}),**risk_identity}}}
        current=price if price is not None else e.prices.get(symbol)
        entry=p.get('avg',sig.get('limit')); qty=p.get('qty',extra.get('quantity'))
        cost=None
        if entry is not None and qty is not None and current is not None:
            cost=e.buy_fee(entry*qty)+e.sell_fee(current*qty)
        codes=[k.upper() for k,v in gates.items() if v['result']!='PASS' and v.get('required',True) and k!=reason]
        e.event(kind,timestamp=e.clock().isoformat(timespec='milliseconds'),strategy_version=STRATEGY_VERSION,
            build_id=BUILD_ID,strategy_experiment=STRATEGY_EXPERIMENT,**risk_identity,symbol=symbol,
            name=sig.get('name') or e.names.get(symbol),setup=sig.get('pattern'),
            signal_detected=bool(sig.get('pattern') and sig.get('time')),signal_at=sig.get('time'),
            price_structure=sig.get('structure'),setup_trials=sig.get('setup_trials',extra.get('setup_trials')),state=state,decision=decision,primary_reason_code=reason,
            market_context=sig.get('market_context') or next((t.get('context') for t in extra.get('setup_trials',[]) or [] if t.get('context')),None),
            discovery_context=sig.get('discovery_context'),
            additional_reason_codes=codes,gates=gates,current_price=current,entry_price=entry,
            invalidation=p.get('initial_stop',sig.get('stop')),protection=p.get('stop'),quantity=qty,
            invalidation_price=p.get('initial_stop',sig.get('stop')),protection_price=p.get('stop'),
            highest_price=p.get('high'),lowest_price=p.get('low'),
            protection_evidence=p.get('protection_evidence'),data_health=p.get('data_health'),
            actual_orderable_cash=e.available_cash,approved_cap=e.a.capital_limit,effective_auto_cap=e.auto_available_capital(),calculated_available_capital=e.watch_budget(),
            MFE=p.get('high')-entry if p.get('high') is not None and entry is not None else None,
            MAE=entry-p.get('low') if p.get('low') is not None and entry is not None else None,
            costs={'estimated_krw':cost,'confirmed':e.a.costs_confirmed,
                   'buy_commission':e.a.commission_buy,'sell_commission':e.a.commission_sell,'sell_tax':e.a.sell_tax,
                   'entry_slippage':e.c.entry_slippage_fraction,'exit_slippage':e.c.exit_slippage_fraction},
            gross_pnl=extra.get('gross_pnl'),net_pnl=extra.get('net_pnl_completed'),
            net_pnl_completed=extra.get('net_pnl_completed'),details=extra)
