"""R6 independent replay / audit (audit/r6_claude_independent).

Purpose: re-derive the 2026-10-14 snapshot replay from the raw archive, WITHOUT trusting
audit/r6_final_20261014. This is a *proxy* replay built on ENTRY_EVAL snapshots:

  * it is NOT a replay of automation.fast_momentum.FastMomentumStrategy (that strategy needs raw
    ticks/volume which the archive does not contain), and
  * snapshot Bid/Ask prove what was displayed, not that an order could have been filled at it,
    so every fill is labelled fill_feasibility=UNKNOWN.

Entry thresholds are the ones inherited from automation/fast_momentum_replay.py. They are
deliberately NOT re-tuned here (same data must not be used to optimise and to prove).

Deterministic: no wall-clock values are written; two runs produce byte-identical files.
Usage:  python audit/r6_claude_independent/independent_replay.py [archive_path] [--out DIR]
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.util import floor_tick, tick_size  # noqa: E402

DEFAULT_ARCHIVE = ('C:/Users/MJS/Desktop/주식자동매매/runtime/automation-production/archive/'
                   'events.jsonl.20261014-145249-570438.7232.32e27bce')
EXPECTED_SIZE = 67132914
EXPECTED_SHA256 = '0c698c6ef4c7d9815c83a081d5d81d66205588e120c2e07caaf3f10f2050b50c'

# Secondary-source corroboration (web search, NOT an official KRX master file). Never used in any
# calculation; only reported next to the code<->name pairs found in the raw events.
EXTERNAL_REFERENCE = {
    '079650': '서산', '010580': '에스엠벡셀', '393890': '더블유씨피',
    '062970': '한국첨단소재', '290650': '엘앤씨바이오', '042510': '라온시큐어',
}
FILL_FEASIBILITY = 'UNKNOWN_NO_RAW_TICKS_OR_DEPTH'


@dataclass(frozen=True)
class Params:
    capital: float = 81300.0
    risk_per_trade: float = 1626.0      # 2% of capital
    daily_risk_cap: float = 3252.0      # 4% of capital
    cooldown_minutes: float = 15.0
    adverse_ticks: int = 0
    fill_mode: str = 'SAME_SNAPSHOT'    # or NEXT_SNAPSHOT (entry/market-like exits fill one snapshot later)
    use_profit_lock: bool = True
    legacy: bool = False                # reproduce audit/r6_final_20261014 behaviours (for before/after)
    comm_buy: float = 0.00015
    comm_sell: float = 0.00015
    tax: float = 0.0020
    entry_slip: float = 0.0020
    exit_slip: float = 0.0030
    max_chase: float = 0.003
    min_net_rr: float = 1.5


def _num(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def load_entry_evals(path):
    """Return (events sorted by (time, line), stats). bid/ask/gain/... are None when not observed."""
    events, stats = [], Counter()
    with open(path, 'r', encoding='utf-8') as f:
        for number, line in enumerate(f, 1):
            stats['lines'] += 1
            if '"ENTRY_EVAL"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                stats['json_error'] += 1
                continue
            if e.get('event') != 'ENTRY_EVAL':
                continue
            stats['entry_eval'] += 1
            symbol, price = e.get('symbol'), _num(e.get('current_price'))
            stamp = e.get('time') or e.get('timestamp')
            if not isinstance(symbol, str) or not symbol or price is None or not stamp:
                stats['unusable_missing_symbol_price_time'] += 1
                continue
            gates = e.get('gates') or {}
            sp = (gates.get('spread') or {}).get('values') or {}
            acc = (gates.get('acceleration') or {}).get('values') or {}
            liq = (gates.get('liquidity') or {}).get('values') or {}
            bid, ask = _num(sp.get('bid')), _num(sp.get('ask'))
            record = {
                'line': number, 'dt': datetime.fromisoformat(stamp), 'symbol': symbol, 'name': e.get('name'),
                'price': price, 'bid': bid, 'ask': ask, 'spread': _num(sp.get('spread')),
                'gain': _num(acc.get('gain')), 'ratio': _num(acc.get('ratio')),
                'turnover': _num(liq.get('window_turnover')),
                'decision': e.get('decision'),
            }
            if bid is None or ask is None:
                stats['no_bid_ask'] += 1
            elif not (bid <= price <= ask):
                stats['price_outside_bid_ask'] += 1
            events.append(record)
    events.sort(key=lambda r: (r['dt'], r['line']))
    stats['same_timestamp_pairs'] = sum(1 for a, b in zip(events, events[1:]) if a['dt'] == b['dt'])
    return events, stats


def entry_geometry(ev, p, cash):
    """Inherited FAST-proxy entry rule + geometry. Returns dict or None."""
    price = ev['price']
    if p.legacy:
        ask = ev['ask'] or price
        gain, ratio, turnover = ev['gain'] or 0.0, ev['ratio'] if ev['ratio'] is not None else 1.0, ev['turnover'] or 0.0
        spread = ev['spread'] or 0.0
    else:
        ask = ev['ask']
        gain, ratio, turnover = ev['gain'], ev['ratio'], ev['turnover']
        spread = ev['spread']
        if spread is None and ev['bid']:
            spread = (ask - ev['bid']) / ev['bid'] if ask is not None else None
        if ask is None or gain is None or ratio is None or turnover is None or spread is None:
            return None
    if spread > 0.0035 or turnover < 6_000_000.0 or gain < 0.002 or gain > 0.020 or ratio < 1.5:
        return None
    ts = tick_size(price)
    entry = ask
    stop = floor_tick(entry * (1.0 - max(0.009, gain * 0.8)))
    risk_dist = entry - stop
    buy_cost, sell_cost = p.comm_buy + p.entry_slip, p.comm_sell + p.tax + p.exit_slip
    risk_net = entry - stop + entry * buy_cost + stop * sell_cost
    if risk_net <= 0:
        return None
    target_min = floor_tick((p.min_net_rr * risk_net + entry * (1.0 + buy_cost)) / (1.0 - sell_cost) + ts)
    target = max(floor_tick(entry + 2.5 * risk_dist), target_min)
    reward_net = target * (1.0 - sell_cost) - entry * (1.0 + buy_cost)
    net_rr = reward_net / risk_net
    if net_rr < p.min_net_rr:
        return None
    return {'entry': entry, 'stop': stop, 'target': target, 'net_rr': net_rr, 'risk_net': risk_net}


def count_signals(events, p):
    """Rule hits (any account state) and per-symbol de-duplicated hits."""
    raw, dedup, last = 0, 0, {}
    gap = timedelta(minutes=p.cooldown_minutes)
    for ev in events:
        if entry_geometry(ev, p, p.capital) is None:
            continue
        raw += 1
        if ev['symbol'] not in last or ev['dt'] - last[ev['symbol']] >= gap:
            dedup += 1
            last[ev['symbol']] = ev['dt']
    return {'rule_hit_events': raw, 'deduped_by_symbol_cooldown': dedup,
            'symbols': len({e['symbol'] for e in events if entry_geometry(e, p, p.capital)})}


def simulate(events, p):
    """Strictly chronological single-slot account. Returns (trades, equity_rows, counters)."""
    cash, realized = p.capital, 0.0
    trades, equity, last_exit = [], [], {}
    counters = Counter()
    gap = timedelta(minutes=p.cooldown_minutes)
    pos = None
    equity.append({'seq': 0, 'time': events[0]['dt'].isoformat(), 'line': events[0]['line'], 'action': 'INIT',
                   'cash': round(cash, 2), 'position_cost': 0.0, 'equity_at_cost': round(cash, 2)})

    def bid_eff(ev):
        if ev['bid'] is not None:
            return ev['bid']
        return ev['price'] if p.legacy else None

    def exit_reference(ev):
        """Observed bid, else (strict mode) last price minus one tick flagged as an estimate."""
        b = bid_eff(ev)
        if b is not None:
            return b, False
        return float(ev['price'] - tick_size(ev['price'])), True

    def close(ev, reference, estimated, reason, trigger_line, kind):
        nonlocal cash, realized, pos
        adv, qty, entry_ask = p.adverse_ticks, pos['qty'], pos['entry_ask']
        ts_x, ts_e = tick_size(ev['price']), pos['ts_e']
        assumed_sell = (reference - adv * ts_x) * (1.0 - p.exit_slip)
        gross_obs = (reference - entry_ask) * qty
        buy_slip = entry_ask * p.entry_slip * qty
        sell_slip = reference * p.exit_slip * qty
        adverse = (adv * ts_e * (1.0 + p.entry_slip) + adv * ts_x * (1.0 - p.exit_slip)) * qty
        commissions = (pos['assumed_buy'] * p.comm_buy + assumed_sell * p.comm_sell) * qty
        tax = assumed_sell * p.tax * qty
        net = gross_obs - buy_slip - sell_slip - adverse - commissions - tax
        cash += assumed_sell * qty * (1.0 - p.comm_sell - p.tax)
        realized += net
        planned = pos['risk_net'] * qty
        trades.append({
            'trade_id': len(trades) + 1, 'status': 'CLOSED', 'code': pos['sym'], 'name': pos['name'],
            'signal_line': pos['signal_line'], 'signal_time': pos['signal_dt'].isoformat(),
            'entry_fill_line': pos['entry_line'], 'entry_fill_time': pos['entry_dt'].isoformat(),
            'entry_observed_ask': entry_ask, 'entry_observed_bid': pos['entry_bid'],
            'entry_assumed_price': round(pos['assumed_buy'], 4), 'qty': qty,
            'stop': pos['stop'], 'target': pos['target'], 'planned_net_rr': round(pos['net_rr'], 4),
            'exit_trigger_line': trigger_line, 'exit_fill_line': ev['line'], 'exit_time': ev['dt'].isoformat(),
            'exit_reason': reason, 'exit_reference_price': reference, 'exit_reference_is_estimate': estimated,
            'exit_assumed_price': round(assumed_sell, 4),
            'holding_seconds': round((ev['dt'] - pos['entry_dt']).total_seconds(), 3),
            'gross_pnl_at_observed': round(gross_obs, 4), 'commissions': round(commissions, 4), 'tax': round(tax, 4),
            'assumed_slippage': round(buy_slip + sell_slip, 4), 'adverse_tick_cost': round(adverse, 4),
            'net_pnl': round(net, 4), 'planned_risk_krw': round(planned, 4),
            'risk_budget_exceeded': net < -p.risk_per_trade, 'fill_feasibility': FILL_FEASIBILITY,
            'cash_after': round(cash, 4),
        })
        equity.append({'seq': len(equity), 'time': ev['dt'].isoformat(), 'line': ev['line'],
                       'action': f"SELL_{pos['sym']}_{reason}", 'cash': round(cash, 2), 'position_cost': 0.0,
                       'equity_at_cost': round(cash, 2)})
        last_exit[pos['sym']] = ev['dt']
        pos = None

    for ev in events:
        sym = ev['symbol']
        if pos is not None and pos['sym'] != sym:
            continue
        if pos is not None and pos['phase'] == 'PENDING_ENTRY':
            if ev['dt'] <= pos['signal_dt'] and ev['line'] <= pos['signal_line']:
                continue
            if ev['ask'] is None or ev['ask'] > pos['signal_ask'] * (1.0 + p.max_chase):
                counters['entry_cancelled_no_ask_or_chase'] += 1
                pos = None
                continue
            ask = ev['ask']
            assumed_buy = (ask + p.adverse_ticks * tick_size(ev['price'])) * (1.0 + p.entry_slip)
            cost = assumed_buy * pos['qty'] * (1.0 + p.comm_buy)
            if cost > cash:
                counters['entry_cancelled_insufficient_cash'] += 1
                pos = None
                continue
            cash -= cost
            pos.update(phase='OPEN', entry_dt=ev['dt'], entry_line=ev['line'], entry_ask=ask, entry_bid=ev['bid'],
                       assumed_buy=assumed_buy, ts_e=tick_size(ev['price']), highest=bid_eff(ev) or ask, locked=False)
            equity.append({'seq': len(equity), 'time': ev['dt'].isoformat(), 'line': ev['line'], 'action': f'BUY_{sym}',
                           'cash': round(cash, 2), 'position_cost': round(cost, 2), 'equity_at_cost': round(cash + cost, 2)})
            continue
        if pos is not None and pos['phase'] == 'PENDING_EXIT':
            if ev['line'] <= pos['trigger_line'] and ev['dt'] <= pos['trigger_dt']:
                continue
            reference, estimated = exit_reference(ev)
            close(ev, reference, estimated, pos['pending_reason'], pos['trigger_line'], 'MARKET')
            continue
        if pos is not None:  # OPEN
            b, px = bid_eff(ev), ev['price']
            if b is not None:
                pos['highest'] = max(pos['highest'], b)
            holding = (ev['dt'] - pos['entry_dt']).total_seconds()
            reason = kind = None
            if (b is not None and b >= pos['target']) or px >= pos['target']:
                reason, kind = 'TARGET_REACHED', 'TARGET'
            elif (b is not None and b <= pos['stop']) or px <= pos['stop']:
                reason, kind = 'STOP_LOSS', 'MARKET'
            elif p.use_profit_lock:
                base = pos['entry_ask']
                if (pos['highest'] - base) / base * 100.0 >= 1.8:
                    pos['locked'] = True
                    pos['lock_stop'] = floor_tick(max(base * 1.018, pos['highest'] * 0.992))
                if pos['locked'] and ((b is not None and b <= pos['lock_stop']) or px <= pos['lock_stop']):
                    reason, kind = 'PROFIT_LOCK', 'MARKET'
            if reason is None and holding >= 180 and ((b if b is not None else px) < pos['entry_ask']):
                reason, kind = 'MOMENTUM_STALL_SCRATCH', 'MARKET'
            if reason is None:
                continue
            if kind == 'TARGET':
                close(ev, pos['target'], False, reason, ev['line'], 'TARGET')
            elif p.fill_mode == 'NEXT_SNAPSHOT':
                pos.update(phase='PENDING_EXIT', pending_reason=reason, trigger_line=ev['line'], trigger_dt=ev['dt'])
            else:
                reference, estimated = exit_reference(ev)
                close(ev, reference, estimated, reason, ev['line'], 'MARKET')
            continue
        # flat: look for an entry
        last = last_exit.get(sym)
        if last is not None and ev['dt'] - last < gap:
            continue
        geo = entry_geometry(ev, p, cash)
        if geo is None:
            continue
        entry, risk_net = geo['entry'], geo['risk_net']
        qty = min(int(p.risk_per_trade // risk_net), int(cash // (entry * (1.0 + p.comm_buy + p.entry_slip))))
        if not p.legacy:
            loss_so_far = max(0.0, -realized)
            room = p.daily_risk_cap - loss_so_far
            capped = int(room // risk_net) if room > 0 else 0
            if capped < qty:
                counters['qty_reduced_by_daily_risk_cap'] += 1
                qty = capped
            if qty <= 0 and capped <= 0:
                counters['entry_blocked_daily_risk_cap'] += 1
        if qty <= 0:
            continue
        pos = {'sym': sym, 'name': ev['name'], 'signal_dt': ev['dt'], 'signal_line': ev['line'], 'signal_ask': entry,
               'qty': qty, 'stop': geo['stop'], 'target': geo['target'], 'net_rr': geo['net_rr'], 'risk_net': risk_net,
               'phase': 'PENDING_ENTRY'}
        if p.fill_mode == 'SAME_SNAPSHOT':
            assumed_buy = (entry + p.adverse_ticks * tick_size(ev['price'])) * (1.0 + p.entry_slip)
            cost = assumed_buy * qty * (1.0 + p.comm_buy)
            cash -= cost
            pos.update(phase='OPEN', entry_dt=ev['dt'], entry_line=ev['line'], entry_ask=entry, entry_bid=ev['bid'],
                       assumed_buy=assumed_buy, ts_e=tick_size(ev['price']), highest=bid_eff(ev) or entry, locked=False)
            equity.append({'seq': len(equity), 'time': ev['dt'].isoformat(), 'line': ev['line'], 'action': f'BUY_{sym}',
                           'cash': round(cash, 2), 'position_cost': round(cost, 2), 'equity_at_cost': round(cash + cost, 2)})

    if pos is not None and pos['phase'] in ('OPEN', 'PENDING_EXIT'):
        last_ev = [e for e in events if e['symbol'] == pos['sym']][-1]
        if p.legacy:
            reference, estimated = exit_reference(last_ev)
            close(last_ev, reference, estimated, 'END_OF_TAPE', last_ev['line'], 'MARKET')
        else:
            reference, estimated = exit_reference(last_ev)
            counters['unresolved_at_tape_end'] += 1
            mark = (reference - pos['entry_ask']) * pos['qty']
            trades.append({
                'trade_id': len(trades) + 1, 'status': 'UNRESOLVED_AT_TAPE_END', 'code': pos['sym'], 'name': pos['name'],
                'signal_line': pos['signal_line'], 'signal_time': pos['signal_dt'].isoformat(),
                'entry_fill_line': pos['entry_line'], 'entry_fill_time': pos['entry_dt'].isoformat(),
                'entry_observed_ask': pos['entry_ask'], 'entry_observed_bid': pos['entry_bid'],
                'entry_assumed_price': round(pos['assumed_buy'], 4), 'qty': pos['qty'], 'stop': pos['stop'],
                'target': pos['target'], 'planned_net_rr': round(pos['net_rr'], 4), 'exit_trigger_line': '',
                'exit_fill_line': '', 'exit_time': '', 'exit_reason': 'NOT_EXITED_BEFORE_ARCHIVE_ENDED',
                'exit_reference_price': reference, 'exit_reference_is_estimate': estimated, 'exit_assumed_price': '',
                'holding_seconds': round((last_ev['dt'] - pos['entry_dt']).total_seconds(), 3),
                'gross_pnl_at_observed': round(mark, 4), 'commissions': '', 'tax': '', 'assumed_slippage': '',
                'adverse_tick_cost': '', 'net_pnl': '', 'planned_risk_krw': round(pos['risk_net'] * pos['qty'], 4),
                'risk_budget_exceeded': '', 'fill_feasibility': FILL_FEASIBILITY, 'cash_after': round(cash, 4),
            })
            equity.append({'seq': len(equity), 'time': last_ev['dt'].isoformat(), 'line': last_ev['line'],
                           'action': f"MARK_{pos['sym']}_UNRESOLVED", 'cash': round(cash, 2),
                           'position_cost': round(pos['assumed_buy'] * pos['qty'], 2),
                           'equity_at_cost': round(cash + reference * pos['qty'], 2)})
    return trades, equity, counters


def metrics(trades, p):
    closed = [t for t in trades if t['status'] == 'CLOSED']
    wins = [t['net_pnl'] for t in closed if t['net_pnl'] > 0]
    losses = [t['net_pnl'] for t in closed if t['net_pnl'] <= 0]
    peak = running = p.capital
    mdd = 0.0
    for t in closed:
        running += t['net_pnl']
        peak = max(peak, running)
        mdd = max(mdd, peak - running)
    gross_win, gross_loss = sum(wins), -sum(losses)
    return {
        'closed_trades': len(closed), 'unresolved_trades': len(trades) - len(closed),
        'wins': len(wins), 'losses': len(losses),
        'win_rate': round(len(wins) / len(closed), 4) if closed else None,
        'net_pnl': round(sum(t['net_pnl'] for t in closed), 2),
        'gross_pnl_at_observed': round(sum(t['gross_pnl_at_observed'] for t in closed), 2),
        'commissions_and_tax': round(sum(t['commissions'] + t['tax'] for t in closed), 2),
        'assumed_slippage': round(sum(t['assumed_slippage'] for t in closed), 2),
        'adverse_tick_cost': round(sum(t['adverse_tick_cost'] for t in closed), 2),
        'avg_net_per_trade': round(sum(t['net_pnl'] for t in closed) / len(closed), 2) if closed else None,
        'avg_win': round(sum(wins) / len(wins), 2) if wins else None,
        'avg_loss': round(sum(losses) / len(losses), 2) if losses else None,
        'profit_factor': round(gross_win / gross_loss, 4) if gross_loss > 0 else None,
        'max_drawdown_krw': round(mdd, 2),
        'avg_holding_seconds': round(sum(t['holding_seconds'] for t in closed) / len(closed), 1) if closed else None,
        'trades_exceeding_2pct_risk': sum(1 for t in closed if t['risk_budget_exceeded']),
        'estimated_exit_reference_trades': sum(1 for t in closed if t['exit_reference_is_estimate']),
    }


def write_csv(path, rows, fields):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator='\n')
        w.writeheader()
        for row in rows:
            w.writerow(row)


def symbol_identity_audit(path, trades, events):
    """Re-read every cited raw line and compare with the ledger; cross-check code<->name uniqueness."""
    code_names, name_codes = defaultdict(Counter), defaultdict(set)
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            if '"symbol"' not in line or '"name"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            s, n = e.get('symbol'), e.get('name')
            if isinstance(s, str) and n:
                code_names[s][n] += 1
                name_codes[n].add(s)
    cited = {}
    for t in trades:
        for key in ('signal_line', 'entry_fill_line', 'exit_fill_line'):
            if t.get(key) not in ('', None):
                cited[int(t[key])] = None
    with open(path, 'r', encoding='utf-8') as f:
        for number, line in enumerate(f, 1):
            if number in cited:
                cited[number] = json.loads(line)
    by_line = {e['line']: e for e in events}
    rows = []
    for t in trades:
        series = [e for e in events if e['symbol'] == t['code'] and int(t['entry_fill_line']) <= e['line']
                  and (t['exit_fill_line'] == '' or e['line'] <= int(t['exit_fill_line']))]
        jumps = [abs(b['price'] / a['price'] - 1.0) for a, b in zip(series, series[1:]) if a['price'] > 0]
        max_jump = max(jumps) if jumps else 0.0
        for role, key in (('SIGNAL', 'signal_line'), ('ENTRY_FILL', 'entry_fill_line'), ('EXIT_FILL', 'exit_fill_line')):
            if t.get(key) in ('', None):
                continue
            line_no = int(t[key])
            raw = cited[line_no]
            ev = by_line[line_no]
            sp = (((raw.get('gates') or {}).get('spread') or {}).get('values') or {})
            names = code_names.get(t['code'], Counter())
            checks = {
                'raw_symbol_equals_ledger_code': raw.get('symbol') == t['code'],
                'code_is_6_digit_string_preserved': isinstance(raw.get('symbol'), str) and re.fullmatch(r'\d{6}', raw['symbol']) is not None,
                'raw_name_is_unique_name_for_code': len(names) == 1 and raw.get('name') in names,
                'name_maps_to_single_code': len(name_codes.get(raw.get('name'), set())) == 1,
                'raw_price_equals_parsed_price': _num(raw.get('current_price')) == ev['price'],
                'raw_bid_ask_equal_parsed': _num(sp.get('bid')) == ev['bid'] and _num(sp.get('ask')) == ev['ask'],
                'price_continuity_ok_max_snapshot_move_below_30pct': max_jump < 0.30,
            }
            if role == 'ENTRY_FILL' and t['entry_observed_ask'] is not None:
                checks['ledger_entry_ask_equals_raw_ask'] = _num(sp.get('ask')) == t['entry_observed_ask']
            rows.append({
                'trade_id': t['trade_id'], 'role': role, 'raw_line': line_no, 'ledger_code': t['code'],
                'raw_event_code': raw.get('symbol'), 'ledger_name': t['name'], 'raw_event_name': raw.get('name'),
                'names_seen_for_code_in_archive': '|'.join(f'{n}:{c}' for n, c in sorted(names.items())),
                'codes_seen_for_name_in_archive': '|'.join(sorted(name_codes.get(raw.get('name'), set()))),
                'external_reference_name_unverified_secondary_source': EXTERNAL_REFERENCE.get(t['code'], 'NOT_CHECKED'),
                'external_name_matches_raw': EXTERNAL_REFERENCE.get(t['code']) == raw.get('name') if t['code'] in EXTERNAL_REFERENCE else 'UNKNOWN',
                'raw_price': raw.get('current_price'), 'raw_bid': sp.get('bid'), 'raw_ask': sp.get('ask'),
                'max_snapshot_move_during_hold': round(max_jump, 6),
                **{k: v for k, v in checks.items()},
                'official_krx_master_checked': 'NO_OFFLINE_NO_KRX_MASTER_FILE',
                'verdict': 'MATCH' if all(checks.values()) else 'MISMATCH',
            })
    return rows


LEDGER_FIELDS = ['trade_id', 'status', 'code', 'name', 'signal_line', 'signal_time', 'entry_fill_line', 'entry_fill_time',
                 'entry_observed_ask', 'entry_observed_bid', 'entry_assumed_price', 'qty', 'stop', 'target', 'planned_net_rr',
                 'exit_trigger_line', 'exit_fill_line', 'exit_time', 'exit_reason', 'exit_reference_price',
                 'exit_reference_is_estimate', 'exit_assumed_price', 'holding_seconds', 'gross_pnl_at_observed',
                 'commissions', 'tax', 'assumed_slippage', 'adverse_tick_cost', 'net_pnl', 'planned_risk_krw',
                 'risk_budget_exceeded', 'fill_feasibility', 'cash_after']
EQUITY_FIELDS = ['seq', 'time', 'line', 'action', 'cash', 'position_cost', 'equity_at_cost']
SENS_FIELDS = ['variant', 'fill_mode', 'adverse_ticks', 'cooldown_minutes', 'closed_trades', 'unresolved_trades', 'wins',
               'losses', 'win_rate', 'net_pnl', 'gross_pnl_at_observed', 'commissions_and_tax', 'assumed_slippage',
               'adverse_tick_cost', 'avg_net_per_trade', 'avg_win', 'avg_loss', 'profit_factor', 'max_drawdown_krw',
               'avg_holding_seconds', 'trades_exceeding_2pct_risk', 'estimated_exit_reference_trades', 'verdict']


def run_all(archive, out_dir, enforce_hash=True):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    size, digest = Path(archive).stat().st_size, sha256_of(archive)
    if enforce_hash and (size != EXPECTED_SIZE or digest != EXPECTED_SHA256):
        raise SystemExit(f'archive mismatch: size={size} sha256={digest}')
    events, stats = load_entry_evals(archive)
    base = Params()

    def row(variant, p):
        trades, _, _ = simulate(events, p)
        m = metrics(trades, p)
        return {'variant': variant, 'fill_mode': p.fill_mode, 'adverse_ticks': p.adverse_ticks,
                'cooldown_minutes': p.cooldown_minutes, **m, 'verdict': 'PROFIT' if m['net_pnl'] > 0 else 'LOSS'}

    legacy_trades, _, _ = simulate(events, replace(base, legacy=True))
    trades, equity, counters = simulate(events, base)
    write_csv(out_dir / 'TRADE_LEDGER.csv', trades, LEDGER_FIELDS)
    write_csv(out_dir / 'LEGACY_REPRODUCTION_LEDGER.csv', legacy_trades, LEDGER_FIELDS)
    write_csv(out_dir / 'EQUITY_CURVE.csv', equity, EQUITY_FIELDS)

    sens = []
    for mode in ('SAME_SNAPSHOT', 'NEXT_SNAPSHOT'):
        for adv in range(4):
            sens.append(row('CORRECTED', replace(base, fill_mode=mode, adverse_ticks=adv)))
    for adv in range(4):
        sens.append(row('LEGACY_AS_REPORTED_BY_r6_final', replace(base, legacy=True, adverse_ticks=adv)))
    write_csv(out_dir / 'SLIPPAGE_SENSITIVITY.csv', sens, SENS_FIELDS)

    cooldown = [row('CORRECTED', replace(base, cooldown_minutes=c)) for c in (0, 5, 15, 30)]
    write_csv(out_dir / 'COOLDOWN_SENSITIVITY.csv', cooldown, SENS_FIELDS)

    identity = symbol_identity_audit(archive, trades, events)
    if identity:
        id_fields = []
        for r in identity:
            id_fields += [k for k in r if k not in id_fields]
        write_csv(out_dir / 'SYMBOL_IDENTITY_AUDIT.csv', identity, id_fields)

    decisions = Counter(e['decision'] for e in events)
    window_minutes = (events[-1]['dt'] - events[0]['dt']).total_seconds() / 60.0
    summary = {
        'archive_size_bytes': size, 'archive_sha256': digest, 'archive_hash_matches_expected': digest == EXPECTED_SHA256,
        'load_stats': dict(stats), 'first_event': events[0]['dt'].isoformat(), 'last_event': events[-1]['dt'].isoformat(),
        'window_minutes': round(window_minutes, 2), 'distinct_symbols': len({e['symbol'] for e in events}),
        'abc_decisions_in_archive': dict(decisions),
        'proxy_signal_counts': count_signals(events, base),
        'corrected_baseline': metrics(trades, base), 'corrected_counters': dict(counters),
        'legacy_reproduction': metrics(legacy_trades, replace(base, legacy=True)),
        'round_trips_per_10_minutes_corrected': round(metrics(trades, base)['closed_trades'] / window_minutes * 10, 3),
        'identity_rows': len(identity), 'identity_mismatches': sum(1 for r in identity if r['verdict'] != 'MATCH'),
        'sensitivity_net_pnl': {f"{r['variant']}|{r['fill_mode']}|{r['adverse_ticks']}": r['net_pnl'] for r in sens},
        'cooldown_net_pnl': {str(r['cooldown_minutes']): r['net_pnl'] for r in cooldown},
    }
    with open(out_dir / 'REPLAY_SUMMARY.json', 'w', encoding='utf-8', newline='\n') as f:
        json.dump(summary, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write('\n')
    return summary


def main(argv):
    args = [a for a in argv if not a.startswith('--')]
    out = Path(__file__).resolve().parent
    if '--out' in argv:
        out = Path(argv[argv.index('--out') + 1])
        args = [a for a in args if a != str(out)]
    archive = args[0] if args else DEFAULT_ARCHIVE
    summary = run_all(archive, out)
    b = summary['corrected_baseline']
    print('archive sha256 ok:', summary['archive_hash_matches_expected'])
    print('ENTRY_EVAL:', summary['load_stats'].get('entry_eval'), 'no_bid_ask:', summary['load_stats'].get('no_bid_ask'))
    print('corrected baseline:', json.dumps(b, ensure_ascii=False, sort_keys=True))
    print('legacy reproduction net:', summary['legacy_reproduction']['net_pnl'])
    print('identity mismatches:', summary['identity_mismatches'], '/', summary['identity_rows'])


if __name__ == '__main__':
    main(sys.argv[1:])
