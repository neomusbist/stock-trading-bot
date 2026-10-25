"""Inactive sizing research on a previously sealed recovery-noise audit.

No detector, receiver, account or broker hooks. Reuses the common cash-flow
model without changing a target, structural line, emergency or LIVE setting.
Price stresses are scenarios, never evidence of a quote or an execution.
"""
import argparse
from decimal import Decimal
import json
import math
from pathlib import Path
import time

from automation.capital import Costs, entry_economics, entry_quantity, loss_reserve, viable_entry_zone
from automation.policy import load_automation
from core.settings import load_config
from core.util import floor_tick, tick_size
from research.causal_data import file_sha


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def stress_sizing(economics, cash_budget, risk_budget, extra_exit_move, costs):
    """Cap quantity by one ex-ante extra exit jump, using shared reserves.

    The jump must come from information available BEFORE the trigger. This is
    a research scenario limit, not a bound on all possible losses. Unknown
    account balances or jump evidence do not receive an invented quantity.
    """
    unknown = dict(status='UNKNOWN', quantity=None, actual_order_permission=False,
                   reason='KNOWN_ECONOMICS_BUDGET_AND_PRETRIGGER_STRESS_REQUIRED')
    if (economics.get('status') != 'KNOWN' or not positive(extra_exit_move)
            or any(type(x) not in (int, float) or not math.isfinite(x) or x < 0
                   for x in (cash_budget, risk_budget))):
        return unknown
    stressed_exit = floor_tick(economics['emergency_stop'] - extra_exit_move)
    if stressed_exit <= 0:
        return {**unknown, 'reason': 'NONPOSITIVE_STRESSED_EXIT'}
    unit = loss_reserve(economics['entry_price'], stressed_exit, costs)
    # entry_quantity is also used by ordinary evaluation. Only its research
    # risk-reserve input changes; signal economics and emergency stay intact.
    original = entry_quantity(economics, cash_budget, risk_budget)
    conservative = entry_quantity({**economics, 'risk_per_share': unit}, cash_budget, risk_budget)
    quantity = min(original['quantity'], conservative['quantity'])
    stressed_loss = quantity * unit
    return dict(status='KNOWN_SCENARIO_NOT_VALIDATED_POLICY', quantity=quantity,
        ordinary_quantity=original['quantity'], extra_exit_move=extra_exit_move,
        stressed_exit_price=stressed_exit, stressed_risk_per_share=unit,
        planned_loss_reserve=quantity * economics['risk_per_share'],
        stressed_loss_with_reserves=stressed_loss,
        within_scenario_budget=Decimal(str(quantity)) * Decimal(str(unit)) <= Decimal(str(risk_budget)),
        target=economics['target'], structural_stop=economics['structural_stop'],
        emergency_stop=economics['emergency_stop'], risk_budget=risk_budget,
        actual_order_permission=False, live_enabled=False,
        limitation='NO_LOSS_BOUND_FOR_LARGER_JUMPS_OR_UNOBSERVED_BOOK_GAPS')


def entry_stresses(case, model, cfg, costs):
    """Adverse entry quotes are NOT imputed to the historical tape."""
    entry = case['recorded_entry_ask']; geometry = case['unchanged_geometry']
    cash = case.get('scenario_cash'); risk = case.get('scenario_per_trade_risk')
    jump = case['prefix'].get('max_adjacent_price_change')
    moves = [('RECORDED_ASK', 0), ('ONE_TICK_WORSE', tick_size(entry)),
             ('TWO_TICKS_WORSE', tick_size(entry) * 2)]
    if positive(jump):
        moves.append(('PRETRIGGER_OBSERVED_JUMP_WORSE', jump))
    results = []
    for name, move in moves:
        price = floor_tick(entry + move)
        e = entry_economics(price, geometry['structural_stop'], model['emergency'],
                            geometry['target'], costs, cfg.min_reward_risk)
        zone = viable_entry_zone(e, geometry['level'], cfg.max_chase)
        size = entry_quantity(e, cash * cfg.max_position_fraction, risk) if cash is not None else {'quantity': None}
        results.append(dict(scenario=name, price=price, economics=e, entry_zone=zone,
            diagnostic_sizing=size, price_RR_pass=bool(e['rr_pass'] and zone.get('current_order_price_in_zone')),
            historically_received_quote=name == 'RECORDED_ASK',
            changed_price_quote_depth='UNKNOWN_STRESS_HAS_NO_INVENTED_DEPTH' if move else 'SEE_RECORDED_L1',
            actual_order_permission=False))
    return results


def protection_stress(case, model, cfg, costs, capped):
    """Keep observed invalidation separate from an actual protection request."""
    path = model['path']; entry = case['recorded_entry_ask']
    quantities = {'ordinary_scenario': model['sizing_scenario']['quantity'],
                  'stress_capped_scenario': capped.get('quantity')}
    latency = []
    for row in path.get('liquidation_sensitivity', []):
        bid = row.get('fresh_bid')
        # Older sealed audit schema uses bid; copy its timestamps and flags
        # verbatim, and never fill a missing quote at the next last-trade price.
        if bid is None:
            bid = row.get('bid')
        reserves = {name: q * loss_reserve(entry, bid, costs)
                    if q is not None and positive(bid) and row.get('quote_at')
                    and type(row.get('available_bid_qty')) is int and row['available_bid_qty'] >= q
                    and row.get('full_quantity_L1_observed') is True else None
                    for name, q in quantities.items()}
        latency.append({'observed_quote': row, 'loss_reserve_scenarios': reserves, 'actual_fill': False})
    close = path.get('first_eligible_complete_close_failure')
    close_at = close.get('available_at') if close else None
    emergency_at = path.get('first_actionable_emergency_at')
    return dict(structural_close_known_at=close_at, emergency_touch_known_at=emergency_at,
        structural_close_precedes_emergency=bool(close_at and emergency_at and close_at < emergency_at),
        fixed_line_protection_evidence=path.get('first_fixed_line_protection_event'),
        actual_protection_request_at='UNKNOWN_NONEXECUTED_RESEARCH_HYPOTHESIS',
        normal_adjustment_excursion_below_structure=path.get('max_observed_excursion_below_structural_before_close_failure'),
        future_unbalanced_volume_pairs=path.get('future_unbalanced_volume_pairs'),
        future_scope_and_stream=path.get('future_controls'),
        quote_gap_and_delay='UNKNOWN_OUTSIDE_DOCUMENTED_FRESH_L1_NOT_ASSUMED_EXECUTABLE',
        latency_scenarios=latency, actual_holding_period=False, stop_stability_validated=False)


def analyze_case(case, cfg, auto):
    costs = Costs(auto.commission_buy, auto.commission_sell, auto.sell_tax,
                  cfg.entry_slippage_fraction, cfg.exit_slippage_fraction)
    cash = case.get('scenario_cash'); risk = case.get('scenario_per_trade_risk')
    before = case['prefix']; jump = before.get('max_adjacent_price_change')
    # The observed pretrigger spread/jump is frozen; future excursions never
    # feed the cap. Missing pretrigger evidence keeps the cap UNKNOWN.
    stress = max(tick_size(case['recorded_entry_ask']), jump,
                 before.get('max_recorded_print_quote_spread') or 0) if positive(jump) else None
    models = {}
    for name, model in case['models'].items():
        if 'economics' not in model:
            models[name] = {'status': 'UNKNOWN', 'reason': model.get('reason')}; continue
        e = model['economics']
        recalculated = entry_economics(case['recorded_entry_ask'], e['structural_stop'],
            e['emergency_stop'], e['target'], costs, cfg.min_reward_risk)
        if any(recalculated.get(k) != e.get(k) for k in ('status', 'net_rr', 'max_entry_price', 'risk_per_share')):
            raise ValueError('SEALED_RESULT_COMMON_COST_OR_GEOMETRY_MISMATCH')
        cap = stress_sizing(e, cash * cfg.max_position_fraction if cash is not None else None, risk, stress, costs)
        exit_stresses = []
        for label, move in [('ONE_EXTRA_EXIT_TICK', tick_size(e['emergency_stop'])),
                            ('PRETRIGGER_JUMP_AFTER_STOP', jump)]:
            if not positive(move): continue
            stressed_exit = floor_tick(e['emergency_stop'] - move)
            if stressed_exit <= 0: continue
            q = model['sizing_scenario']['quantity']
            reserve = q * loss_reserve(e['entry_price'], stressed_exit, costs) if q is not None else None
            exit_stresses.append(dict(scenario=label, extra_move=move, exit_price=stressed_exit,
                quantity=q, loss_with_reserves=reserve, scenario_risk_budget=risk,
                exceeds_scenario_budget=reserve > risk if reserve is not None and risk is not None else None,
                basis='ADDITIONAL_MOVE_BEYOND_EMERGENCY_AND_EXISTING_COST_RESERVES_NOT_A_FILL'))
        level1 = bool(e['rr_pass'] and model['entry_zone'].get('current_order_price_in_zone'))
        models[name] = dict(noise=model['noise'], emergency=model['emergency'],
            entry_price_scenarios=entry_stresses(case, model, cfg, costs), exit_jump_scenarios=exit_stresses,
            conservative_sizing=cap, protection=protection_stress(case, model, cfg, costs, cap),
            evidence_levels={
                '1_price_RR': level1,
                '2_price_RR_fresh_L1_scenario_quantity': bool(model['price_RR_L1_scenario_pass']),
                '3_actual_WATCH_account_final_gates': 'UNKNOWN_NOT_PROVEN_BY_THIS_AUDIT',
                '4_stop_and_protection_stability': 'UNKNOWN_INCOMPLETE_PATH_AND_QUOTE_ENVELOPE'},
            actual_orderable=False, live_candidate=False)
    return dict(symbol=case['symbol'], trigger_at=case['structure_and_trigger_at'], source_id=case['source_id'],
        recorded_ask=case['recorded_entry_ask'], unchanged_geometry=case['unchanged_geometry'],
        pretrigger_jump=jump, sizing_stress_move=stress, models=models,
        scope='RECORDED_COHORT_NOT_INDEPENDENT_TRADES', actual_order_permission=False)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--noise-result', required=True, type=Path)
    p.add_argument('--freeze', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    a = p.parse_args(argv); root = a.freeze.resolve(); out = a.output.resolve()
    if out.exists() or out == root or root in out.parents or a.noise_result.resolve() in out.parents:
        raise ValueError('NEW_EXTERNAL_OUTPUT_REQUIRED')
    sealed = json.loads(a.noise_result.read_bytes()); manifest = json.loads((root / 'MANIFEST.json').read_bytes())
    if sealed['input_manifest_sha256'] != file_sha(root / 'MANIFEST.json') or sealed['day'] != manifest['day']:
        raise ValueError('SEALED_AUDIT_INPUT_MANIFEST_MISMATCH')
    expected = {f['relative']: f['sha256'] for f in manifest['files']}
    if any(expected.get(n) != file_sha(root / n) for n in ('config.json', 'automation.json')):
        raise ValueError('FROZEN_COST_OR_RISK_CONFIG_CHANGED')
    cfg = load_config(root / 'config.json'); auto = load_automation(root / 'automation.json')
    if auto.fast_momentum_enabled or auto.pullback_expansion_target_enabled:
        raise ValueError('KEEP_FAST_AND_EXPANSION_OFF')
    started = time.perf_counter(); cpu = time.process_time()
    result = dict(day=sealed['day'], reused_noise_audit_sha256=file_sha(a.noise_result),
        input_manifest_sha256=sealed['input_manifest_sha256'],
        input_validation='REUSED_SEALED_ACTUAL_AUDIT_MANIFEST_AND_CONFIG_NO_HEAVY_TAPE_REPLAY',
        cases=[analyze_case(c, cfg, auto) for c in sealed['cases']],
        actual_orderable_signals=0, actual_orders=0, LIVE_modified=False,
        conclusion='HOLD_BOUNDARY_PRICE_AND_UNVALIDATED_NOISE_NOT_A_LIVE_CANDIDATE',
        cpu_seconds=time.process_time()-cpu, wall_seconds=time.perf_counter()-started,
        source_sha256=file_sha(Path(__file__)))
    out.mkdir(parents=True); (out / 'RESULT.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k != 'cases'}, ensure_ascii=False)); return 0


if __name__ == '__main__':
    main()
