"""Engine-owned exit plans. Targets are hypotheses, never resting sell orders.

Display consumers may invalidate freshness, but cannot recompute protection or
produce trading instructions. New profit strategies are observation-only.
"""
from copy import deepcopy
from datetime import datetime, timedelta
import math
from core.util import floor_tick, tick_size
from automation.capital import COMMON_COST_PATTERNS


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
    close_based = ((auto.confirmed_invalidation_enabled or position.get('pattern') == 'FAST' or policy.get('policy_name') == 'FAST_MICRO_STRUCTURE')
                   and policy.get('enabled', True)
                   and number(policy.get('structural_stop'))
                   and number(policy.get('noise_band')) and policy['noise_band'] > 0)
    if close_based:
        soft = max(policy['structural_stop'], stop)
        policy_emergency = policy.get('emergency_stop')
        base_emergency = policy_emergency if number(policy_emergency) and policy_emergency > 0 else initial
        hard = max(base_emergency, floor_tick(soft-policy['noise_band']))
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
    emergency = (policy.get('emergency_stop')
                 if auto.confirmed_invalidation_enabled and number(policy.get('emergency_stop')) and 0 < policy['emergency_stop'] < (entry or float('inf'))
                 else signal.get('emergency') if number(signal.get('emergency')) and 0 < signal['emergency'] < (entry or float('inf'))
                 else stop)
    valid_risk = number(entry) and number(stop) and 0 < stop < entry
    valid_target = number(target) and number(entry) and target > entry > 0
    risk = entry-stop if valid_risk else None
    costs = dict(buy=auto.commission_buy, sell=auto.commission_sell, tax=auto.sell_tax,
                 entry_slippage=cfg.entry_slippage_fraction, exit_slippage=cfg.exit_slippage_fraction)
    reward_net = (target*(1-auto.commission_sell-auto.sell_tax-cfg.exit_slippage_fraction)
                  - entry*(1+auto.commission_buy+(0 if filled else cfg.entry_slippage_fraction))) if valid_target else None
    risk_stop = emergency if number(emergency) and number(entry) and 0 < emergency < entry else stop
    risk_net = (entry-risk_stop+entry*auto.commission_buy+risk_stop*(auto.commission_sell+auto.sell_tax+cfg.exit_slippage_fraction)) if valid_risk else None
    net_rr = reward_net/risk_net if reward_net is not None and risk_net and risk_net > 0 else None
    economics=None
    if signal.get('pattern') in COMMON_COST_PATTERNS:
        from automation.capital import signal_economics
        economics=signal_economics(cfg,auto,signal,entry=entry,filled=filled,
            actual_entry_fee_per_share=signal.get('actual_entry_fee_per_share') if filled else None)
        reward_net=economics.get('reward_after_cost');risk_net=economics.get('risk_per_share');net_rr=economics.get('net_rr')
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
    return {'version': 1, 'setup': setup, 'entry_logic': {'A': '관측 돌파 또는 확정 상승 지속', 'B': '상승 후 확인된 눌림 회복', 'C': '관측 박스 돌파', 'E': '지지 확인 후 재상승 회복', 'FAST': '실시간 마이크로 구조 모멘텀 진입'}.get(setup, '진입 근거 미확인'),
            'entry': entry, 'quantity': quantity, 'entry_basis': 'ACTUAL_AVERAGE_FILL' if filled else 'ORDER_LIMIT',
            'initial_invalidation': structure, 'initial_emergency': emergency, 'initial_price_risk': risk,
            'one_r_price': entry+risk if valid_risk else None,
            'profit_activation_price': entry+cfg.protect_at_r*risk if valid_risk else None,
            'activation_r': cfg.protect_at_r, 'target': target if valid_target else None,
            'initial_target': target if number(target) and target > 0 else None,
            'initial_target_basis': target_basis,
            'target_status': 'HYPOTHESIS' if valid_target else 'UNKNOWN', 'target_basis': target_basis,
            'confirmed_resistance': resistance, 'projection': signal.get('projection'),
            'target_action': '청산 재평가 지점; 도달만으로 자동매도하지 않음', 'resting_target_order': False,
            'reward_after_cost_per_share': reward_net, 'risk_after_cost_per_share': risk_net,
            'net_reward_risk': net_rr, 'minimum_net_reward_risk': cfg.min_reward_risk,
            'entry_economics':economics,'maximum_entry_price':economics.get('max_entry_price') if economics else None,
            'reward_guard_pass': bool((economics['rr_pass'] if economics is not None else net_rr is not None and net_rr >= cfg.min_reward_risk) and valid_target and valid_risk),
            'reward_guard_enabled': auto.exit_plan_reward_guard_enabled or setup in COMMON_COST_PATTERNS,
            'cost_basis': '설정 비용·슬리피지 추정; 청산 후 증권사 비용으로 정산', 'costs': costs,
            'profit_policy': 'AE3', 'confirmed_pivot_protection': confirmed,
            'profit_activation': '1R 이후 비용을 넘는 확정 높은 저점' if confirmed else '1R 이후 기존 트레일링·수익 반납선',
            'after_failed_rebound': burst, 'exit_conditions': conditions,
            'flatten_at': deadline, 'session': session, 'venue': signal.get('venue', 'UNKNOWN'),
            'market_close': auto.after_market_close if session == 'AFTER' else '15:30:00',
            'holding_horizon_is_deadline': False,
            'experimental_profit_orders_enabled': False}


def _condition(key, label, status, detail, evidence=None):
    return {'key': key, 'label': label, 'status': status, 'detail': detail,
            'evidence': deepcopy(evidence or {})}


def _profit_view(cfg, auto, p, plan, lines, data, asof, bar_fresh):
    """Engine-authored explanation only. Never raise a stop or request an exit."""
    sig = p.get('signal') or {}
    initial = sig.get('target', plan.get('initial_target', plan.get('target')))
    initial = initial if number(initial) and initial > 0 else None
    current_target = plan.get('target')
    current_target = current_target if number(current_target) and current_target > 0 else None
    unchanged = initial is not None and current_target == initial
    touch = p.get('target_touch')
    if touch and not fresh(touch.get('time'), asof, 86400): touch = None
    original_rr = sig.get('evaluation_gates', {}).get('risk_reward', {}).get('values', {}).get('calculated_rr')
    target = {'initial': initial, 'current': current_target,
              'initial_price_rr': original_rr if number(original_rr) else None,
              'initial_basis': sig.get('target_basis') or plan.get('initial_target_basis') or plan.get('target_basis'),
              'change_reason': '최초 목표 유지' if unchanged else plan.get('target_change_reason') or 'UNKNOWN / 목표 변경 근거 기록 없음',
              'touch': deepcopy(touch),
              'reassessment': '목표 도달 시각 기록 및 관찰 후보 비교만 수행. 활성 보호선·확정봉 실패·비상선은 기존 규칙으로 계속 평가. 목표만으로 매도·목표 상향하지 않음',
              'resting_sell_order': False}
    confirmed = bool(auto.confirmed_structure_protection or
                     auto.confirmed_invalidation_enabled and sig.get('invalidation_policy'))
    risk = p.get('initial_r')
    activation = p['avg'] + cfg.protect_at_r*risk if number(risk) and risk > 0 else None
    armed = activation is not None and p['high'] >= activation
    raised = lines['active_structure'] > lines['initial_structure'] if lines['close_based'] else p['stop'] > p['initial_stop']
    protect_price = lines['active_structure']
    modeled_stop_net = (protect_price*(1-cfg.exit_slippage_fraction)*(1-auto.commission_sell-auto.sell_tax)
                        - p['avg']*(1+auto.commission_buy))*p['qty']
    protecting = bool(raised and modeled_stop_net > 0)
    evidence = deepcopy(p.get('protection_evidence') or {})
    close = '완성봉 종가' if lines['close_based'] else '신선한 체결가'
    if protecting:
        expected = f"{close} ≤ {protect_price:,.0f}원 시 청산 요청. 현재 보호선의 비용·슬리피지 포함 순손익 추정 {modeled_stop_net:+,.2f}원; 실제 체결 수익 보장 아님"
    elif confirmed:
        expected = (f"익절 보호 미발동 · {activation:,.0f}원 도달 후 " if activation is not None else '익절 보호 미발동 · 1R 기준 UNKNOWN · ')
        expected += '진입 후 연속 3개 확정봉의 높은 저점 확인·비용 초과 보호선 필요. 그 보호선의 완성봉 종가 이탈 시 청산' if lines['close_based'] else '진입 후 연속 3개 확정봉의 높은 저점 확인·비용 초과 보호선 필요. 보호선 체결가 이탈 시 청산'
    else:
        expected = (f"익절 보호 미발동 · {activation:,.0f}원 도달 후 " if activation is not None else '익절 보호 미발동 · 1R 기준 UNKNOWN · ')
        expected += '기존 트레일링·수익 반납선 상승 후 신선한 체결가가 보호선 이하이면 청산'
    if confirmed:
        rule = '1R 이후 진입 후 연속 3개 확정봉: 중앙 저점 ≤ 왼쪽 저점, < 오른쪽 저점, 직전 확정 저점보다 높음. 완충 적용 보호선이 이전 선보다 높고 현재가 아래이며 비용 기준을 초과해야 상승'
    else:
        rule = (f"1R 이후 고점 대비 {cfg.trailing_fraction:.1%} 트레일링, 비용 기준선, 유효 완성봉 저점 및 "
                f"최고 미실현 이익의 {auto.unrealized_giveback_fraction:.0%} 반납선 중 높은 값. 실제 선은 엔진 stop 사용")
    failures = deepcopy(p.get('after_momentum_evidence') or {})
    after_enabled = bool(auto.after_momentum_failure_enabled and auto.confirmed_invalidation_enabled
                         and sig.get('invalidation_policy') and sig.get('session') == 'AFTER'
                         and sig.get('pattern') == 'A'
                         and sig.get('evaluation_gates', {}).get('acceleration', {}).get('values', {}).get('mode') != 'CONFIRMED_CONTINUATION')
    after_ready = fresh(failures.get('asof'), asof, 65) and bool(failures.get('decline_times'))
    after_failed = after_ready and failures.get('reason') == 'AFTER_BURST_FAILED_REBOUND'
    failure_rule = '최근 최대 30개의 진입 후 연속 확정봉(최소 5개)에서 최고점 이후 낮은 반등 확인. 마지막 3개 봉의 고점·저점·종가가 모두 하락하고 마지막 종가가 반등 저점 아래이면 1R 전에도 청산'
    conditions = [
        _condition('structure', '구조적 종가 무효화' if lines['close_based'] else '체결가 보호선',
                   '활성' if (bar_fresh if lines['close_based'] else data['current'] is not None) else '데이터 부족',
                   f"규칙 활성 · {close} ≤ {protect_price:,.0f}원. 단일 틱의 종가 구조선 접촉은 종가 확정이 아님" if lines['close_based'] else f"규칙 활성 · {close} ≤ {protect_price:,.0f}원"),
        _condition('emergency', '즉시 비상청산선', '활성' if data['bid'] is not None or data['current'] is not None else '데이터 부족',
                   f"규칙 활성 · 신선한 호가·체결 ≤ {lines['emergency']:,.0f}원. 갭 체결손실 상한 아님"),
        _condition('profit', '현재 이익보호선', '활성' if protecting else '미발동', expected, evidence),
        _condition('activation', '이익보호 발동 기준', '활성' if armed else '미발동' if activation is not None else '데이터 부족',
                   f"고점 기록 ≥ {activation:,.0f}원 · " + ('도달; 추가 지지 확인 필요' if confirmed else '도달') if armed else f"{activation:,.0f}원 미도달" if activation is not None else '실제 initial_r 기록 부족'),
        _condition('after', 'AFTER 급등 후 반등 실패', '활성' if after_failed else '미발동' if not after_enabled or after_ready else '데이터 부족',
                   failure_rule + (' · 실패 확정' if after_failed else ' · 규칙 ON / 실패 미확정' if after_ready else ' · 규칙 ON / 확정봉 증거 대기' if after_enabled else ' · 이 포지션에는 미적용 (OFF 또는 범위 밖)'), failures),
        _condition('deadline', '세션 종료 청산', '활성' if plan.get('flatten_at') else '미발동',
                   str(plan.get('flatten_at') or '강제청산 없음') + ' · ' + str(plan.get('session', 'UNKNOWN'))),
        _condition('request', '이미 발동한 청산 요청', '활성' if p.get('exit_requested') else '미발동',
                   p.get('exit_reason') or '청산 요청 없음', {'time': p.get('exit_signal_at')}),
        _condition('experimental', '익절 후보 A~D', '관찰 전용',
                   '목표 전량·목표 부분·목표 후 재평가·조기 구조 추적: 실제 매도 주문에 적용하지 않음')]
    saved_history = p.get('stop_history') or []
    history = deepcopy(saved_history[-20:])
    if not saved_history: history_note = 'UNKNOWN / 보호선 변경 이력 기록 없음'
    elif len(saved_history) == 1: history_note = '최초 보호선 유지 (상향 변경 기록 없음)'
    else: history_note = '엔진이 저장한 보호선 변경 ' + str(len(saved_history)-1) + '회 (최근 20개 표시)'
    return {'target': target, 'profit': {'active_policy': 'AE3', 'path': 'CONFIRMED_HIGHER_LOW' if confirmed else 'LEGACY_TRAIL_GIVEBACK',
                'policy_text': '목표가 도달만으로 매도하지 않음 / 구조적 청산 또는 보호선 이탈 시 매도',
                'activation_price': activation, 'one_r_price': p['avg']+risk if number(risk) and risk > 0 else None,
                'armed': armed, 'protecting': protecting, 'protect_price': protect_price if protecting else None,
                'status': '익절 보호 활성' if protecting else '익절 보호 미발동',
                'expected_condition': expected, 'rule': rule, 'evidence': evidence,
                'modeled_net_at_protect_price': modeled_stop_net if protecting else None},
            'conditions': conditions, 'after': {'enabled': after_enabled, 'ready': bool(after_ready), 'failed': bool(after_failed), 'evidence': failures},
            'history': history, 'history_note': history_note}


def state_for_position(cfg, auto, position, asof, pending=0, order_allowed=False, orders=None):
    plan = deepcopy(position.get('exit_plan') or build_plan(cfg, auto, position.get('signal', {}), position['avg'], position['qty'], True))
    # Display activation uses the exact engine risk even for fractional fills.
    if number(position.get('initial_r')) and position['initial_r'] > 0:
        plan['one_r_price'] = position['avg']+position['initial_r']
        plan['profit_activation_price'] = position['avg']+cfg.protect_at_r*position['initial_r']
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
        reason=position.get('exit_reason') or '청산 요청 처리 중'
        trend = '실패 확정' if reason in ('확정봉 종가의 구조적 무효화','AFTER 급등 후 반등 실패 확인') else '청산 요청 / 추세 별도 확인'
        hold = '청산 요청 처리 중: '+reason
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
    data = {'bid': bid, 'current': current}
    presentation = _profit_view(cfg, auto, position, plan, lines, data, asof, bar_fresh)
    return {'version': 2, 'asof': asof.isoformat(), 'plan': deepcopy(plan), 'lines': lines,
            'presentation': presentation, 'exit_reason': position.get('exit_reason'),
            # Diagnostic state only; never promote the FAST micro line into
            # the confirmed structure or single-quote emergency line.
            'fast_exit': deepcopy(position.get('fast_exit_evidence')),
            'active_orders': [{'side': o.get('side'), 'status': o.get('status'), 'qty': o.get('qty'), 'filled': o.get('filled'),
                               'limit': o.get('limit'), 'created': o.get('created'), 'reason': (o.get('signal') or {}).get('reason')}
                              for o in (orders or [])],
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
        if view.get('exit_requested'):
            view['trend_status'] = '청산 요청 유지 / 현재 추세 판단 불가'
        else:
            view['trend_status'] = '판단 불가'
            view['hold_reason'] = '신선한 호가·체결 대기; 기존 보호 규칙 유지'
    if not fresh(view.get('asof'), asof, stale_seconds):
        view['order_allowed'] = False
        for condition in view.get('presentation', {}).get('conditions', []):
            if condition['key'] in {'structure', 'emergency'} or condition['key'] == 'after' and view['presentation']['after']['enabled']:
                condition['status'] = '데이터 부족'
                condition['detail'] += ' · 엔진의 현재 판단 기록 만료 / 규칙 유지'
    view['market_data'] = data
    return view
