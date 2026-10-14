"""Offline capacity research; never imported by the LIVE runner or broker.

Offers must already have a point-in-time strategy qualification. This module
does not discover setups, change invalidation lines or generate real orders.
Quote-backed model fills remain hypothetical: displayed depth is not a queue.
"""
from collections import Counter
from copy import deepcopy
from datetime import datetime
import math

from core.util import floor_tick, tick_size


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def at(value):
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(stamp, datetime) or stamp.tzinfo is None:
        raise ValueError('timezone-aware evidence time required')
    return stamp


def ceil_tick(price):
    result = floor_tick(price)
    return result if result >= price else result + tick_size(result)


def break_even(entry, costs, *, spread=0., include_slippage=True):
    """Reference-price rise, with explicit spread and configured cost stress.

If entry is the executable ask, pass spread=0. Spread must not be added twice.
Uses the additive slippage convention of the engine's exit-plan estimate.
Broker minimum fees/rounding are not known from proportional rates alone.
"""
    if not finite(entry) or entry <= 0 or not finite(spread) or spread < 0:
        raise ValueError('positive entry and nonnegative spread required')
    buy_slip = costs.entry_slippage if include_slippage else 0.
    sell_slip = costs.exit_slippage if include_slippage else 0.
    divisor = 1-costs.sell-costs.tax-sell_slip
    if divisor <= 0:
        raise ValueError('invalid cost rates')
    raw = (entry*(1+costs.buy+buy_slip)+spread)/divisor
    price = ceil_tick(raw)
    return {'price': price, 'rise': price-entry, 'rise_fraction': price/entry-1,
            'raw_price': raw, 'spread': spread,
            'basis': 'PROPORTIONAL_COST_STRESS_NOT_BROKER_SETTLEMENT',
            'minimum_fees_and_rounding': 'UNKNOWN'}


def completed_distribution(trades, start, end, minutes):
    """Count completed round trips once by final exit, including zero bins."""
    start, end = at(start), at(end)
    if end < start or minutes <= 0:
        raise ValueError('invalid window')
    width = minutes*60
    bins = [0]*math.ceil((end-start).total_seconds()/width)
    seen = set()
    for row in trades:
        key = row['id']
        if key in seen or not row.get('fully_closed'):
            continue
        seen.add(key)
        stamp = at(row['exit_at'])
        if start <= stamp < end:
            bins[int((stamp-start).total_seconds()//width)] += 1
    return {'minutes': minutes, 'counts': bins, 'distribution': dict(Counter(bins)),
            'completed_round_trips': sum(bins)}


class CapacityReplay:
    """Sequential integer-share ledger for isolated 1/2/3/4-slot experiments.

    Does not infer executions from candles. reserve/fill/exit are independent
    events; unknown exit depth leaves inventory and funds locked. Ranking uses
    only offer-time fields, never future exits. Theme totals are risk exposures,
    not a claim of measured correlations or diversification.
    """
    def __init__(self, cfg, auto, *, cash, daily_loss=0., max_positions=1,
                 position_fraction=1., theme_risk_fraction=None):
        if not finite(cash) or cash < 0 or not 1 <= max_positions <= 4:
            raise ValueError('invalid experiment capital or slots')
        if not 0 < position_fraction <= 1 or daily_loss < 0:
            raise ValueError('invalid allocation or loss')
        if theme_risk_fraction is not None and (not finite(theme_risk_fraction)
                                               or not 0 < theme_risk_fraction <= cfg.max_daily_loss_fraction):
            raise ValueError('theme stress cap must fit inside daily risk')
        self.cfg, self.auto = cfg, auto
        from automation.capital import Costs
        self.costs = Costs(auto.commission_buy, auto.commission_sell, auto.sell_tax,
                           cfg.entry_slippage_fraction, cfg.exit_slippage_fraction)
        self.cash = min(cash, auto.capital_limit)
        self.initial_cash = self.cash
        self.daily_loss = daily_loss
        self.max_positions, self.fraction = max_positions, position_fraction
        self.theme_fraction = theme_risk_fraction
        self.orders, self.positions, self.trades, self.rejects = {}, {}, [], []
        self.marks, self.monitor_age = {}, {}
        self.peak_equity, self.drawdown = self.cash, 0.
        self.peak_open_risk, self.peak_theme_risk = 0., {}

    def equity(self):
        return min(self.auto.capital_limit, self.cash + sum(
            p['qty']*self.marks.get(s, p['avg']) for s, p in self.positions.items()))

    def unit_risk(self, entry, stop):
        return max(0., entry-stop+self.costs.roundtrip(entry, stop)
                   + stop*self.cfg.exit_slippage_fraction)

    def reservations(self):
        cash = sum((o['qty']-o['filled'])*o['entry']*(1+self.costs.buy)
                   for o in self.orders.values())
        risk = sum((o['qty']-o['filled'])*self.unit_risk(o['entry'], o['stop'])
                   for o in self.orders.values())
        risk += sum(p['qty']*self.unit_risk(p['avg'], p['stop'])
                    for p in self.positions.values())
        return cash, risk

    def theme_risks(self):
        result = Counter()
        for p in self.positions.values():
            result[p['theme']] += p['qty']*self.unit_risk(p['avg'], p['stop'])
        for o in self.orders.values():
            result[o['theme']] += (o['qty']-o['filled'])*self.unit_risk(o['entry'], o['stop'])
        return dict(result)

    def quote_reason(self, quote, now, *, sell=False):
        if not isinstance(quote, dict):
            return 'QUOTE_UNKNOWN'
        try:
            age = (at(now)-at(quote['time'])).total_seconds()
        except (KeyError, ValueError, TypeError):
            return 'QUOTE_TIME_UNKNOWN'
        bid, ask = quote.get('bid'), quote.get('ask')
        if not finite(bid) or not finite(ask) or not 0 < bid <= ask:
            return 'QUOTE_INVALID'
        if not 0 <= age <= self.cfg.stale_seconds:
            return 'QUOTE_STALE_OR_FUTURE'
        if (ask-bid)/ask > self.cfg.max_spread:
            return 'SPREAD_TOO_WIDE'
        field = 'bid_qty' if sell else 'ask_qty'
        if not finite(quote.get(field)) or quote[field] < 0:
            return 'DEPTH_UNKNOWN'
        return ''

    def reserve(self, offer, now):
        offer = deepcopy(offer)
        def reject(reason):
            self.rejects.append({'id': offer.get('id'), 'symbol': offer.get('symbol'),
                                 'time': at(now).isoformat(), 'reason': reason})
            return {'accepted': False, 'reason': reason, 'quantity': 0}
        try:
            known = at(offer['known_at'])
        except (KeyError, TypeError, ValueError):
            return reject('SIGNAL_TIME_UNKNOWN')
        if known > at(now):
            return reject('FUTURE_SIGNAL')
        if offer.get('strategy_qualified') is not True:
            return reject('STRATEGY_NOT_QUALIFIED')
        if not offer.get('id') or not offer.get('symbol'):
            return reject('IDENTITY_UNKNOWN')
        if offer['id'] in self.orders or any(t['id'] == offer['id'] for t in self.trades):
            return reject('DUPLICATE_LIFECYCLE')
        symbol = offer['symbol']
        if symbol in self.positions or any(o['symbol'] == symbol for o in self.orders.values()):
            return reject('SYMBOL_ALREADY_RESERVED')
        slots = set(self.positions) | {o['symbol'] for o in self.orders.values()}
        if len(slots) >= self.max_positions:
            return reject('POSITION_SLOTS')
        entry, stop, target = (offer.get(k) for k in ('entry', 'stop', 'target'))
        if not all(finite(x) for x in (entry, stop, target)) or not 0 < stop < entry < target:
            return reject('STRUCTURE_UNKNOWN_OR_INVALID')
        reason = self.quote_reason(offer.get('quote'), now)
        if reason:
            return reject(reason)
        if offer['quote']['ask'] > entry:
            return reject('ASK_ABOVE_APPROVED_LIMIT')
        reserved_cash, open_risk = self.reservations()
        equity = self.equity()
        remaining = equity*self.cfg.max_daily_loss_fraction-self.daily_loss-open_risk
        risk_budget = max(0., min(equity*self.cfg.risk_fraction, remaining))
        exposure = sum(p['qty']*p['avg'] for p in self.positions.values())
        exposure += sum((o['qty']-o['filled'])*o['entry'] for o in self.orders.values())
        amount = max(0., min(self.cash-reserved_cash, self.auto.capital_limit-exposure,
                             equity*self.fraction))
        qty = min(int(amount//(entry*(1+self.costs.buy))),
                  int(risk_budget//self.unit_risk(entry, stop)))
        theme = offer.get('theme') or 'UNKNOWN'
        if self.theme_fraction is not None:
            left = max(0., equity*self.theme_fraction-self.theme_risks().get(theme, 0.))
            qty = min(qty, int(left//self.unit_risk(entry, stop)))
        if qty <= 0:
            return reject('DAILY_OR_OPEN_RISK' if risk_budget <= 0 else 'INTEGER_CAPITAL_OR_RISK')
        self.orders[offer['id']] = {**offer, 'qty': qty, 'filled': 0, 'theme': theme}
        self._record_risk()
        return {'accepted': True, 'quantity': qty, 'reason': 'MODEL_RESERVATION',
                'actual_fill': 'UNKNOWN_QUEUE_AND_MARKET_IMPACT'}

    def reserve_simultaneous(self, offers, now):
        # Same-time fields only; future outcomes do not enter this sort key.
        ranked = sorted(offers, key=lambda o: (-o.get('score', 0.), str(o.get('symbol', ''))))
        return [(o.get('id'), self.reserve(o, now)) for o in ranked]

    def fill(self, identity, quote, now, quantity=None):
        o = self.orders[identity]
        reason = self.quote_reason(quote, now)
        if reason:
            return {'filled': 0, 'reason': reason, 'actual_fill': 'UNKNOWN'}
        remaining = o['qty']-o['filled']
        qty = remaining if quantity is None else quantity
        if type(qty) is not int or not 0 < qty <= remaining:
            raise ValueError('invalid model fill quantity')
        if quote['ask'] > o['entry'] or quote['ask_qty'] < qty:
            return {'filled': 0, 'reason': 'LIMIT_OR_DEPTH', 'actual_fill': 'UNKNOWN'}
        price = quote['ask']
        debit = qty*price*(1+self.costs.buy)
        if debit > self.cash+1e-7:
            raise AssertionError('model would overspend cash')
        old = self.positions.get(o['symbol'])
        prior = old['qty'] if old else 0
        average = ((old['avg']*prior if old else 0)+qty*price)/(prior+qty)
        self.positions[o['symbol']] = {**o, 'qty': prior+qty, 'avg': average,
                                      'entry_at': old['entry_at'] if old else at(now).isoformat()}
        self.cash -= debit
        o['filled'] += qty
        if o['filled'] == o['qty']:
            del self.orders[identity]
        self._record_risk()
        return {'filled': qty, 'price': price, 'basis': 'QUOTE_BACKED_VIRTUAL_FILL',
                'actual_fill': 'UNKNOWN_QUEUE_AND_MARKET_IMPACT'}

    def cancel(self, identity):
        # Cancellation is a supplied model event, not assumed broker success.
        del self.orders[identity]

    def mark(self, symbol, price, quote_time, now):
        if symbol not in self.positions:
            return
        if not finite(price) or price <= 0 or at(quote_time) > at(now):
            raise ValueError('invalid mark')
        self.marks[symbol] = price
        self.monitor_age[symbol] = max(self.monitor_age.get(symbol, 0.),
                                       (at(now)-at(quote_time)).total_seconds())
        equity = self.cash+sum(p['qty']*self.marks.get(s, p['avg']) for s, p in self.positions.items())
        self.peak_equity = max(self.peak_equity, equity)
        self.drawdown = max(self.drawdown, self.peak_equity-equity)

    def exit(self, symbol, quote, now):
        p = self.positions[symbol]
        if any(o['symbol'] == symbol for o in self.orders.values()):
            return {'closed': False, 'reason': 'ACTIVE_ENTRY_REMAINDER', 'actual_fill': 'UNKNOWN'}
        reason = self.quote_reason(quote, now, sell=True)
        if reason or quote['bid_qty'] < p['qty']:
            return {'closed': False, 'reason': reason or 'INSUFFICIENT_EXIT_DEPTH',
                    'actual_fill': 'UNKNOWN'}
        # Conservative configured exit slippage, not a measurement of the tape.
        price = floor_tick(quote['bid']*(1-self.costs.exit_slippage))
        gross = (price-p['avg'])*p['qty']
        fees = self.costs.roundtrip(p['avg'], price)*p['qty']
        net = gross-fees
        self.cash += price*p['qty']-self.costs.sell_fee(price*p['qty'])
        self.daily_loss += max(0., -net)
        row = {'id': p['id'], 'symbol': symbol, 'theme': p['theme'], 'qty': p['qty'],
               'entry': p['avg'], 'exit': price, 'entry_at': p['entry_at'],
               'exit_at': at(now).isoformat(), 'fully_closed': True, 'gross': gross,
               'fees': fees, 'net': net, 'holding_seconds': (at(now)-at(p['entry_at'])).total_seconds(),
               'basis': 'VIRTUAL_FILL_COST_STRESS_NOT_LIVE_EXPECTANCY'}
        self.trades.append(row)
        del self.positions[symbol]
        self.mark_remaining_drawdown()
        return {'closed': True, **row, 'actual_fill': 'UNKNOWN_QUEUE_AND_MARKET_IMPACT'}

    def mark_remaining_drawdown(self):
        equity = self.cash+sum(p['qty']*self.marks.get(s, p['avg']) for s, p in self.positions.items())
        self.peak_equity = max(self.peak_equity, equity)
        self.drawdown = max(self.drawdown, self.peak_equity-equity)

    def _record_risk(self):
        self.peak_open_risk = max(self.peak_open_risk, self.reservations()[1])
        for theme, risk in self.theme_risks().items():
            self.peak_theme_risk[theme] = max(self.peak_theme_risk.get(theme, 0.), risk)

    def summary(self):
        return {'basis': 'OFFLINE_CAPACITY_EXPERIMENT', 'max_positions': self.max_positions,
                'position_fraction': self.fraction, 'cash': self.cash,
                'completed_round_trips': len(self.trades), 'net': sum(t['net'] for t in self.trades),
                'daily_loss': self.daily_loss, 'max_drawdown_marked': self.drawdown,
                'peak_open_risk': self.peak_open_risk, 'peak_theme_risk': self.peak_theme_risk,
                'monitor_age_seconds': self.monitor_age, 'rejects': dict(Counter(r['reason'] for r in self.rejects)),
                'open_positions': len(self.positions), 'open_buy_orders': len(self.orders),
                'actual_fill_feasibility': 'UNKNOWN_QUEUE_AND_MARKET_IMPACT',
                'profitability': 'NOT_ESTABLISHED'}
