"""Offline A/B/C profit-component review of bounded capture, never LIVE policy.

Counterfactual bids are execution hypotheses. No result is a broker fill or a
proof of strategy expectancy; common account/order/connection logic is not a
complete simulator here. An unclosed alternative is right-censored, not profit.
"""
from copy import deepcopy
from datetime import datetime,timedelta
import math

from automation.fast_momentum import FastMomentumStrategy
from automation.fast_profit_observation import observe_fast_profit,POLICIES
from core.strategy import Tick
from core.util import tick_size


def at(row):
    try:
        value=(row.get('processed_at') or row.get('confirmed_at')
               if row.get('event')=='COMPLETED_BAR' else
               row.get('received_at') or row.get('confirmed_at') or row.get('at'))
        dt=datetime.fromisoformat(value)
        return dt if dt.tzinfo is not None else None
    except (TypeError,ValueError):return None


def compare_trade(cfg,auto,trade,records,*,delay_seconds=.5):
    """Evaluate in receipt order, then price only a later full-quantity bid."""
    if not math.isfinite(delay_seconds) or delay_seconds<0:raise ValueError('INVALID_DELAY')
    out={'trade_id':trade.get('trade_id'),'symbol':trade.get('symbol'),
         'observation_only':True,'profit_component_only':True,'actual_orders':0,
         'profitability_proven':False,
         'actual_broker_net_pnl':trade.get('net_pnl',trade.get('pnl_estimate')) if trade.get('pnl_source')=='BROKER_QUERY' else None,
         'unconfirmed_ledger_pnl':trade.get('pnl_estimate') if trade.get('pnl_source')!='BROKER_QUERY' else None,
         'delay_seconds':delay_seconds,'policies':{n:{'status':'UNKNOWN','reason':'NO_CAPTURE',
              'actual_fill':None,'realized_net_pnl':None} for n in POLICIES}}
    opened=datetime.fromisoformat(trade['opened']);entry=trade.get('entry',trade.get('avg'));qty=trade['qty']
    rows=[r for r in records if r.get('symbol') in (None,trade['symbol']) and at(r) is not None]
    rows.sort(key=lambda r:(at(r),r.get('capture_sequence',0)))
    if not rows:return out
    prior_ticks=[r for r in rows if r.get('event')=='TRADE' and at(r)<=opened]
    missing=[]
    if not prior_ticks:missing.append('PRE_FILL_RECEIVER_COVERAGE_UNAVAILABLE')
    sessions={r.get('capture_session',r.get('session')) for r in rows if at(r)>=opened}
    sessions.discard(None)
    if len(sessions)>1:missing.append('RESTART_DURING_WINDOW')
    if any(r.get('capture_losses',0)>0 for r in rows):missing.append('CAPTURE_DROPS')
    if any(r.get('event')=='CAPTURE_STATUS' and (r.get('write_errors') or r.get('storage_blocked')) for r in rows):
        missing.append('CAPTURE_STORAGE_OR_WRITE_FAILURE')
    if any(r.get('event')=='STREAM_STATE' and r.get('kind') in {'ERROR','DISCONNECTED'} and at(r)>=opened for r in rows):
        missing.append('CONNECTION_GAP')
    if any(r.get('event')=='SUBSCRIPTION_SCOPE' and trade['symbol'] in r.get('removed',[]) and at(r)>=opened for r in rows):
        missing.append('SUBSCRIPTION_GAP')
    positions={n:{'symbol':trade['symbol'],'opened':trade['opened'],'avg':entry,'qty':qty,
        'initial_stop':trade['initial_stop'],'stop':trade['initial_stop'],
        'emergency':trade.get('signal',{}).get('emergency'),
        'signal':deepcopy(trade.get('signal',{})),'pattern':'FAST',
        'risk_budget':trade.get('signal',{}).get('account_risk_budget_krw'),
        'protection_bars':[]} for n in POLICIES}
    fast={n:FastMomentumStrategy(cfg,auto) for n in POLICIES}
    observer_state=None;actions={};quotes=[];observed_prices=[];last_book=None;seen_bars=set()
    for r in rows:
        stamp=at(r)
        if r.get('event')=='BOOK':
            last_book=r
            if stamp>=opened and r.get('depth_available') is True:
                try:
                    exchange=datetime.fromisoformat(r['exchange_at'])
                    if not 0<=(stamp-exchange).total_seconds()<=cfg.stale_seconds:continue
                    if not 0<r['best_bid']<=r['best_ask'] or r['bid_qty']<qty:continue
                    quotes.append((stamp,r['best_bid']))
                except (KeyError,TypeError,ValueError):continue
        if stamp<opened:continue
        if r.get('event')=='COMPLETED_BAR':
            bar=dict(r['bar']);key=bar.get('time')
            try:
                start=datetime.fromisoformat(key)
                valid=(start+timedelta(minutes=1)<=stamp and not bar.get('gap_reason')
                       and bar.get('local_drop_count',0)==0 and bar.get('first_second',999)<=cfg.stale_seconds)
            except (TypeError,ValueError):valid=False
            if valid and key not in seen_bars:
                seen_bars.add(key);bar.update(complete=True,received_at=stamp.isoformat())
                for p in positions.values():p['protection_bars'].append(bar)
        if r.get('event')=='TRADE':
            try:
                t=Tick(trade['symbol'],datetime.fromisoformat(r['exchange_at']),r['price'],r['volume'],
                    r['cumulative'],r['ask'],r['bid'],r.get('regular') is True,
                    ask_qty=r.get('ask_qty',0),bid_qty=r.get('bid_qty',0),
                    depth_available=r.get('depth_available'),depth_source=r.get('depth_source'),
                    book_time=datetime.fromisoformat(r['book_time']) if r.get('book_time') else None)
            except (KeyError,TypeError,ValueError):missing.append('INVALID_TICK');continue
            observed_prices.append((stamp,t.price))
            for name,strategy in fast.items():
                p=positions[name]
                if name!=POLICIES[0]:p.pop('protection_stop',None)
                if not strategy.ingest_tick(t,stamp):continue
                exit_now,reason,evidence=strategy.evaluate_exit(p,t,stamp)
                p['fast_exit_evidence']={'checked_at':stamp.isoformat(),
                    'decision':'EXIT' if exit_now else 'HOLD' if reason=='HOLD' else 'UNKNOWN',
                    'reason':reason,'micro_protection_stop':p.get('protection_stop')}
                if exit_now and name not in actions:
                    actions[name]=(stamp,reason)
        if last_book:
            for p in positions.values():p['last_quote']={'time':last_book.get('exchange_at'),
                'bid':last_book.get('best_bid'),'ask':last_book.get('best_ask')}
        observed=observe_fast_profit(cfg,auto,positions[POLICIES[1]],stamp,observer_state)
        observer_state=observed['state']
        for name in POLICIES[1:]:
            choice=observed['policies'][name]
            if choice['action']=='EXIT_ALL_OBSERVATION' and name not in actions:
                actions[name]=(stamp,choice['reason'])
    costs=next(iter(fast.values())).costs
    for name in POLICIES:
        base=out['policies'][name]
        if missing:
            base.update(reason=','.join(sorted(set(missing))));continue
        if name not in actions:
            if name!=POLICIES[0] and len(seen_bars)<3:
                base.update(reason='COMPLETE_POST_FILL_BARS_UNAVAILABLE');continue
            base.update(status='RIGHT_CENSORED',reason='NO_CONFIRMED_EXIT_IN_CAPTURE',
                        completed_bars=len(seen_bars));continue
        decision,reason=actions[name]
        earliest=decision+timedelta(seconds=delay_seconds)
        executable=next(((ts,bid) for ts,bid in quotes if earliest<=ts<=earliest+timedelta(seconds=cfg.stale_seconds)),None)
        if executable is None:
            base.update(reason='POST_DELAY_EXECUTABLE_BID_UNAVAILABLE',signal_at=decision.isoformat());continue
        stamp,bid=executable
        modeled=bid*(1-costs['exit_slippage'])
        pnl=(modeled*(1-costs['sell']-costs['tax'])-entry*(1+costs['buy']))*qty
        prices=[px for ts,px in observed_prices if ts<=stamp]
        mfe=max([0.]+[px-entry for px in prices]);mae=max([0.]+[entry-px for px in prices])
        base.update(status='MODEL_COMPONENT_EXIT',reason=reason,signal_at=decision.isoformat(),
            modeled_at=stamp.isoformat(),executable_bid=bid,modeled_exit=modeled,modeled_net_pnl=pnl,
            modeled_holding_seconds=(stamp-opened).total_seconds(),observed_mfe_per_share=mfe,
            observed_mae_per_share=mae,mfe_net_capture_ratio=pnl/(mfe*qty) if mfe>0 else None,
            gross_giveback_per_share=entry+mfe-modeled,
            one_tick_worse_model_net=((bid-tick_size(bid))*(1-costs['exit_slippage'])*(1-costs['sell']-costs['tax'])
                                      -entry*(1+costs['buy']))*qty,
            cost_basis='PROPORTIONAL_FEES_AND_SLIPPAGE_HYPOTHESIS_NOT_BROKER_FILL')
    out['limitations']=['PARTIAL_PROFIT_COMPONENT_COMPARISON_NOT_FULL_ACCOUNT_ENGINE',
        'EXECUTABLE_QUOTE_IS_NOT_GUARANTEED_FILL','CENSORED_ALTERNATIVES_EXCLUDED_FROM_EXPECTANCY']
    return out


def summarize(reviews):
    # Use the same fully observed cohort for all alternatives. No cherry-picking.
    complete=[r for r in reviews if all(r['policies'][n]['status']=='MODEL_COMPONENT_EXIT' for n in POLICIES)]
    result={'trades':len(reviews),'common_model_cohort':len(complete),'profitability_proven':False,
        'actual_expectancy':None,'policies':{}}
    for name in POLICIES:
        rows=[r['policies'][name] for r in complete];pnl=[r['modeled_net_pnl'] for r in rows]
        avg=lambda a:sum(a)/len(a) if a else None
        result['policies'][name]={'model_n':len(rows),'model_win_rate':avg([float(v>0) for v in pnl]),
            'model_average_win':avg([v for v in pnl if v>0]),'model_average_loss':avg([v for v in pnl if v<0]),
            'model_mean_net_pnl':avg(pnl),'model_mean_holding_seconds':avg([r['modeled_holding_seconds'] for r in rows]),
            'model_mean_mfe_capture':avg([r['mfe_net_capture_ratio'] for r in rows if r['mfe_net_capture_ratio'] is not None]),
            'model_mean_mae':avg([r['observed_mae_per_share'] for r in rows]),
            'model_mean_giveback':avg([r['gross_giveback_per_share'] for r in rows])}
    return result
