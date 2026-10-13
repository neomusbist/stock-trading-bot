"""Engine-owned exit plans. Targets are hypotheses, never resting sell orders.

Display consumers may invalidate freshness, but cannot recompute protection or
produce trading instructions. New profit strategies are observation-only.
"""
from copy import deepcopy
from datetime import datetime, timedelta
import math
from core.util import floor_tick, tick_size


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def fresh(stamp, asof, seconds):
    try:
        stamp = datetime.fromisoformat(stamp) if isinstance(stamp, str) else stamp
        return (isinstance(stamp, datetime) and stamp.tzinfo is not None
                and asof.tzinfo is not None and stamp.date() == asof.date()
                and 0 <= (asof-stamp).total_seconds() <= seconds)
    except (TypeError, ValueError):
        return False


def invalidation_lines(cfg, auto, position):
    """The same numerical lines are used by ticks, books, bars and the UI."""
    initial = position['initial_stop']
    stop = position.get('stop', initial)
    policy = position.get('signal', {}).get('invalidation_policy') or {}
    close_based = (auto.confirmed_invalidation_enabled and policy.get('enabled', True)
                   and number(policy.get('structural_stop'))
                   and number(policy.get('noise_band')) and policy['noise_band'] > 0)
    if close_based:
        soft = max(policy['structural_stop'], stop)
        hard = max(initial, floor_tick(soft-policy['noise_band']))
        return {'initial_structure': policy['structural_stop'], 'active_structure': soft,
                'emergency': hard, 'close_based': True}
    if auto.resilient_protection_enabled:
        emergency = floor_tick(stop-max(3*tick_size(stop), stop*.008))
    else:
        emergency = min(stop-tick_size(stop), floor_tick(stop*(1-cfg.structural_buffer)))
    return {'initial_structure': initial, 'active_structure': stop,
            'emergency': emergency, 'close_based': False}


def build_plan(cfg, auto, signal, entry=None, quantity=None, filled=False):
    entry = signal.get('limit') if entry is None else entry
    inputs = signal.get('efficiency', {}).get('inputs', {})
    target = signal.get('target', inputs.get('target'))
    stop = signal.get('stop')
    policy = signal.get('invalidation_policy') or {}
    structure = policy.get('structural_stop', stop) if auto.confirmed_invalidation_enabled else stop
    valid_risk = number(entry) and number(stop) and 0 < stop < entry
    valid_target = number(target) and number(entry) and target > entry > 0
    risk = entry-stop if valid_risk else None
    costs = dict(buy=auto.commission_buy, sell=auto.commission_sell, tax=auto.sell_tax,
                 entry_slippage=cfg.entry_slippage_fraction, exit_slippage=cfg.exit_slippage_fraction)
    reward_net = (target*(1-auto.commission_sell-auto.sell_tax-cfg.exit_slippage_fraction)
                  - entry*(1+auto.commission_buy+(0 if filled else cfg.entry_slippage_fraction))) if valid_target else None
    risk_net = (entry-stop+entry*auto.commission_buy+stop*(auto.commission_sell+auto.sell_tax+cfg.exit_slippage_fraction)) if valid_risk else None
    net_rr = reward_net/risk_net if reward_net is not None and risk_net and risk_net > 0 else None
    peak = policy.get('nearest_confirmed_peak')
    if not peak and isinstance(signal.get('price_context'), dict):
        peak = signal['price_context'].get('nearest_confirmed_peak')
    resistance = peak.get('price') if isinstance(peak, dict) else None
    if not number(resistance):
        candidate = signal.get('structure', {}).get('resistance')
        resistance = candidate if number(candidate) and number(entry) and candidate > entry else None
    target_basis = signal.get('target_basis') or ('저장된 진입 계산의 목표 가설' if valid_target else '가격구조 근거 부족')
    session = signal.get('session', 'UNKNOWN')
    setup = signal.get('pattern', 'UNKNOWN')
    confirmed = (auto.confirmed_structure_protection or
                 auto.confirmed_invalidation_enabled and bool(policy))
    burst = (auto.after_momentum_failure_enabled and auto.confirmed_invalidation_enabled
             and bool(policy) and session == 'AFTER' and setup == 'A'
             and signal.get('evaluation_gates', {}).get('acceleration', {}).get('values', {}).get('mode') != 'CONFIRMED_CONTINUATION')
    conditions = ['완성봉 종가의 구조선 이탈' if auto.confirmed_invalidation_enabled and policy else '신선한 체결가의 보호선 이탈',
                  '신선한 호가·체결의 비상선 이탈', '실제 전역 데이터 장애 확인', 'STOP_FLAT/KILL 청산 요청']
    if burst: conditions.append('AFTER 급등 후 낮은 반등 및 3개 확정봉 하락 실패')
    deadline = (auto.after_force_flatten_start if session == 'AFTER' else auto.force_flatten_start) if not auto.ALLOW_OVERNIGHT else None
    return {'version': 1, 'setup': setup, 'entry_logic': {'A': '관측 돌파 또는 확정 상승 지속', 'B': '상승 후 확인된 눌림 회복', 'C': '관측 박스 돌파'}.get(setup, '진입 근거 미확인'),
            'entry': entry, 'quantity': quantity, 'entry_basis': 'ACTUAL_AVERAGE_FILL' if filled else 'ORDER_LIMIT',
            'initial_invalidation': structure, 'initial_emergency': stop, 'initial_price_risk': risk,
            'one_r_price': entry+risk if valid_risk else None,
            'profit_activation_price': entry+cfg.protect_at_r*risk if valid_risk else None,
            'activation_r': cfg.protect_at_r, 'target': target if valid_target else None,
            'target_status': 'HYPOTHESIS' if valid_target else 'UNKNOWN', 'target_basis': target_basis,
            'confirmed_resistance': resistance, 'projection': signal.get('projection'),
            'target_action': '청산 재평가 지점; 도달만으로 자동매도하지 않음', 'resting_target_order': False,
            'reward_after_cost_per_share': reward_net, 'risk_after_cost_per_share': risk_net,
            'net_reward_risk': net_rr, 'minimum_net_reward_risk': cfg.min_reward_risk,
            'reward_guard_pass': bool(net_rr is not None and net_rr >= cfg.min_reward_risk and valid_target and valid_risk),
            'reward_guard_enabled': auto.exit_plan_reward_guard_enabled,
            'cost_basis': '설정 비용·슬리피지 추정; 청산 후 증권사 비용으로 정산', 'costs': costs,
            'profit_policy': 'AE3', 'confirmed_pivot_protection': confirmed,
            'profit_activation': '1R 이후 비용을 넘는 확정 높은 저점' if confirmed else '1R 이후 기존 트레일링·수익 반납선',
            'after_failed_rebound': burst, 'exit_conditions': conditions,
            'flatten_at': deadline, 'session': session, 'venue': signal.get('venue', 'UNKNOWN'),
            'market_close': auto.after_market_close if session == 'AFTER' else '15:30:00',
            'holding_horizon_is_deadline': False,
            'experimental_profit_orders_enabled': False}


def state_for_position(cfg, auto, position, asof, pending=0, order_allowed=False):
    plan = position.get('exit_plan') or build_plan(cfg, auto, position.get('signal', {}), position['avg'], position['qty'], True)
    lines = invalidation_lines(cfg, auto, position)
    quote = position.get('last_quote') or {}
    quote_fresh = fresh(quote.get('time'), asof, cfg.stale_seconds)
    standing_fresh = (quote.get('confirmation') == 'MATCHED_STANDING_BOOK'
                      and fresh(quote.get('snapshot_confirmed_at'), asof, cfg.stale_seconds))
    bid = quote.get('bid') if (quote_fresh or standing_fresh) and number(quote.get('bid')) and quote['bid'] > 0 else None
    trade_fresh = fresh(position.get('last_trade_time'), asof, cfg.stale_seconds)
    current = position.get('last_trade_price') if trade_fresh else None
    if not number(current) or current <= 0: current = None
    mfe = max(0., position['high']-position['avg'])
    armed = number(plan.get('profit_activation_price')) and position['high'] >= plan['profit_activation_price']
    raised = (lines['active_structure'] > lines['initial_structure'] if lines['close_based'] else position['stop'] > position['initial_stop'])
    protection = '구조 보호 중' if raised else '1R 도달·확정 지지 대기' if armed and plan['confirmed_pivot_protection'] else '발동' if armed else '미발동'
    bar = position.get('last_invalidation_bar') or (position.get('protection_bars') or [{}])[-1]
    try: bar_fresh = fresh(datetime.fromisoformat(bar['time'])+timedelta(minutes=1), asof, 65)
    except (KeyError, TypeError, ValueError): bar_fresh = False
    failed = position.get('after_momentum_evidence') or {}
    if position.get('exit_requested'):
        trend, hold = '실패 확정 / 청산 요청', position.get('exit_reason') or '청산 요청 처리 중'
    elif not (bid is not None or current is not None):
        trend, hold = '판단 불가', '신선한 호가·체결 대기; 기존 보호 규칙 유지'
    elif failed.get('reason') == 'AFTER_BURST_FAILED_REBOUND':
        trend, hold = '실패 확정', 'AFTER 반등 실패 확인'
    elif not bar_fresh:
        trend, hold = '판단 불가', '확정봉 구조 판단 대기; 비상선 보호 유지'
    elif (current if current is not None else bid) < position['high']:
        trend, hold = '조정 / 실패 미확정', '현재 구조선 유지·조건부 실패 미확정'
    else:
        trend, hold = '상승 유지', '신선한 가격의 고점 유지·현재 구조선 유효'
    net = (bid*(1-auto.commission_sell-auto.sell_tax)-position['avg']*(1+auto.commission_buy))*position['qty'] if bid is not None else None
    # Modeled allowance, not a guaranteed fill or a hard cap in a gap.
    risk_line = lines['emergency'] if lines['close_based'] else lines['active_structure']
    remaining_risk = max(0., position['avg']-risk_line+position['avg']*auto.commission_buy+risk_line*(auto.commission_sell+auto.sell_tax+cfg.exit_slippage_fraction))*position['qty']
    next_exit = ((f"완성봉 종가 ≤ {lines['active_structure']:,.0f}원 / 신선한 호가·체결 ≤ 비상선 {lines['emergency']:,.0f}원" if lines['close_based'] else
                  f"신선한 체결가 ≤ 보호선 {lines['active_structure']:,.0f}원 / 호가 ≤ 비상선 {lines['emergency']:,.0f}원"))
    if plan['after_failed_rebound']: next_exit += ' / AFTER 낮은 반등 이후 3개 확정봉 실패'
    if plan['flatten_at']: next_exit += f" / {plan['flatten_at']} 당일 청산"
    next_exit += ' / 전역 데이터 장애·STOP_FLAT/KILL'
    return {'version': 1, 'asof': asof.isoformat(), 'plan': deepcopy(plan), 'lines': lines,
            'highest': position['high'], 'mfe_per_share': mfe, 'mfe_fraction': mfe/position['avg'],
            'mae_per_share': max(0., position['avg']-position.get('low', position['avg'])),
            'profit_armed': armed, 'protection_status': protection, 'trend_status': trend,
            'hold_reason': hold, 'next_exit': next_exit, 'remaining_risk_estimate': remaining_risk,
            'estimated_net_pnl': net, 'net_pnl_basis': '신선한 매수호가 및 설정 수수료·세금 추정; 체결 보장 아님',
            'market_data': {'bid': bid, 'quote_time': quote.get('time'), 'quote_confirmed_at': quote.get('snapshot_confirmed_at'),
                            'quote_fresh': quote_fresh, 'standing_fresh': standing_fresh,
                            'current': current, 'trade_time': position.get('last_trade_time'), 'trade_fresh': trade_fresh,
                            'last_complete_bar': bar.get('time') if bar_fresh else None},
            'pending_orders': pending, 'exit_requested': bool(position.get('exit_requested')),
            'order_allowed': bool(order_allowed), 'quantity_blocked': bool(position.get('qty_blocked')),
            'data_health': deepcopy(position.get('data_health', {}))}


def display_state(saved, asof, stale_seconds):
    """Invalidate expired display data only; never recalculate engine decisions."""
    view = deepcopy(saved or {})
    data = view.get('market_data', {})
    bid_valid = (fresh(data.get('quote_time'), asof, stale_seconds) or
                 data.get('standing_fresh') and fresh(data.get('quote_confirmed_at'), asof, stale_seconds))
    if not bid_valid:
        data['bid'] = None
        data['quote_fresh'] = data['standing_fresh'] = False
        view['estimated_net_pnl'] = None
    if not fresh(data.get('trade_time'), asof, stale_seconds):
        data['current'] = None
        data['trade_fresh'] = False
    if data.get('bid') is None and data.get('current') is None:
        view['trend_status'] = '판단 불가'
        view['hold_reason'] = '신선한 호가·체결 대기; 기존 보호 규칙 유지'
    if not fresh(view.get('asof'), asof, stale_seconds): view['order_allowed'] = False
    view['market_data'] = data
    return view
