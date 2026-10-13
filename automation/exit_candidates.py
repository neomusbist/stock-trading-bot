"""Independent profit-policy hypotheses. No broker, orders or real PnL here.

Candidates A-D only observe completed, post-fill, point-in-time structure.
The operating policy remains AE3. Candle touch is not an executable fill.
"""
from copy import deepcopy
from datetime import datetime, timedelta
from automation.exit_plan import number, fresh
from automation.trade_quality import complete_sequence
from core.util import floor_tick

NAMES = ('TARGET_FULL', 'TARGET_PARTIAL', 'TARGET_REASSESS', 'STRUCTURAL_TRAIL', 'AE3')


def observe_candidates(cfg, auto, position, asof, prior=None):
    state = deepcopy(prior or {})
    plan = position.get('exit_plan') or {}
    target = plan.get('target')
    touch = position.get('target_touch') or {}
    target_known = (number(target) and target > position['avg']
                    and number(touch.get('price')) and touch['price'] >= target
                    and fresh(touch.get('time'), asof, 86400))
    if target_known and not state.get('target_known_at'):
        state['target_known_at'] = touch['time']
        state['target_touch_basis'] = touch.get('basis', 'RECEIVED_TRADE')
    target_known = bool(state.get('target_known_at'))
    try:
        opened = datetime.fromisoformat(position['opened'])
        first = opened.replace(second=0, microsecond=0)
        if opened > first: first += timedelta(minutes=1)
    except (KeyError, TypeError, ValueError): first = asof
    bars = complete_sequence(position.get('protection_bars'), asof, 3, first, require_recent=True)
    volume_valid = bool(bars and all(number(b.get('volume')) and b['volume'] >= 0 for b in bars))
    prior_pivot = state.get('pivot_low', plan.get('initial_invalidation', position['initial_stop']))
    if volume_valid:
        left, center, right = bars
        pivot = (center['low'] <= left['low'] and center['low'] < right['low']
                 and center['low'] > prior_pivot and right['close'] > center['high'])
        cost_floor = position['avg']*(1+auto.commission_buy)/((1-auto.commission_sell-auto.sell_tax)*(1-cfg.exit_slippage_fraction))
        candidate = floor_tick(center['low']*(1-cfg.structural_buffer))
        if pivot and cost_floor < candidate < right['close']:
            state.update(pivot_low=center['low'], pivot_stop=max(candidate, state.get('pivot_stop', 0)),
                         pivot_confirmed_at=asof.isoformat())
    stop = state.get('pivot_stop')
    breached = bool(volume_valid and number(stop) and bars[-1]['close'] <= stop
                    and state.get('pivot_confirmed_at', '') < asof.isoformat())
    quote = position.get('last_quote') or {}
    bid = quote.get('bid') if fresh(quote.get('time'), asof, cfg.stale_seconds) else None
    executable = number(bid) and bid > 0
    qty = position['qty']
    def row(name, action, reason, quantity=None):
        old = state.get('policies', {}).get(name, {})
        trigger = action.startswith('EXIT')
        return {'policy': name, 'action': action, 'reason': reason, 'quantity': quantity,
                'first_decision_at': old.get('first_decision_at') or (asof.isoformat() if trigger else None),
                'executable_quote_known': bool(executable), 'quote_bid': bid if executable else None,
                'actual_fill': None, 'realized_net_pnl': None, 'orders_enabled': False}
    policies = {}
    policies['TARGET_FULL'] = row('TARGET_FULL', 'EXIT_ALL' if target_known else 'HOLD' if number(target) else 'UNKNOWN',
                                  'OBSERVED_TARGET_TOUCH' if target_known else 'TARGET_NOT_OBSERVED', qty if target_known else None)
    partial = qty//2
    policies['TARGET_PARTIAL'] = row('TARGET_PARTIAL', 'NOT_APPLICABLE' if qty < 2 else 'EXIT_PART' if target_known else 'HOLD',
                                     'INTEGER_QUANTITY_CANNOT_LEAVE_REMAINDER' if qty < 2 else 'PARTIAL_TARGET_REMAINDER_TRACKED', partial if qty >= 2 and target_known else None)
    policies['TARGET_PARTIAL']['remainder_quantity'] = qty-partial if qty >= 2 else None
    modeled_unit_net=(bid*(1-auto.commission_sell-auto.sell_tax)-position['avg']*(1+auto.commission_buy)) if executable else None
    policies['TARGET_PARTIAL'].update(estimated_first_leg_net=modeled_unit_net*partial if modeled_unit_net is not None and qty>=2 else None,
                                     remainder_action='EXIT_ALL' if target_known and breached else 'TRACK_CONFIRMED_STRUCTURE',
                                     fixed_or_minimum_fee_impact='UNKNOWN_WITHOUT_BROKER_CHARGE_MODEL')
    for name, eligible in (('TARGET_REASSESS', target_known), ('STRUCTURAL_TRAIL', True)):
        action = 'EXIT_ALL' if eligible and breached else 'HOLD' if volume_valid else 'UNKNOWN'
        policies[name] = row(name, action, 'CONFIRMED_HIGHER_LOW_BROKEN' if action == 'EXIT_ALL' else
                             'TARGET_NOT_YET_OBSERVED' if not eligible else 'CONFIRMED_STRUCTURE_RETAINED' if volume_valid else 'COMPLETE_POST_FILL_BARS_UNAVAILABLE', qty if action == 'EXIT_ALL' else None)
        policies[name]['observation_stop'] = stop
        policies[name]['one_r_prerequisite'] = False
    policies['AE3'] = row('AE3', 'EXIT_ALL' if position.get('exit_requested') else 'HOLD',
                          position.get('exit_reason') or 'EXISTING_POLICY', qty if position.get('exit_requested') else None)
    state.update(asof=asof.isoformat(), policies=policies, active_policy='AE3',
                 experimental_orders_enabled=False, realized_comparison='UNKNOWN_WITHOUT_BROKER_FILLS')
    from automation.opening_failure import observe_opening_failure
    state['opening_failure_observation']=observe_opening_failure(cfg,auto,position,asof)
    return state
