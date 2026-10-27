"""Offline account replay through the unmodified AutonomousEngine functions.

The only broker is a closed in-memory simulator. Synthetic authorization lives
in a new temporary directory, never the operating directory. This is PAPER
execution feasibility under stated latency/L1 assumptions, not actual fills.
"""
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path
import tempfile
import time

from core.settings import Config
from core.util import atomic_json, tick_size, floor_tick
from core.engine import TERMINAL
from core.broker import Rejected
from automation.engine import AutonomousEngine
from automation.policy import AutomationConfig, HARD_LOCKS
from automation.allocation import efficiency
from research.causal_data import finite,aware,json_values


class PaperPolicy:
    """An explicit fake authorization fixture; never a production approval."""
    def __init__(self,root):
        self.path=root/'runtime/paper-policy.json'
        self.state={'mode':'LIVE','live_authorized':True,'revision':0,'releases':{},
                    'PAPER_FIXTURE_ONLY':True,'actual_order_permission':False}
    @property
    def mode(self):return self.state['mode']
    def refresh(self):return self.state
    def locked(self,symbol):return symbol in HARD_LOCKS
    def command(self,command,**kwargs):
        if command not in {'STOP','STOP_FLAT','KILL'}:raise ValueError('PAPER_CONTROL_ONLY')
        self.state.update(mode=command,revision=self.state['revision']+1)


class QuoteReplayBroker:
    identity='OFFLINE_PAPER_L1_REPLAY_NO_NETWORK'
    def __init__(self,cash,costs,latency_ms=0,adverse_ticks=0,manual=None):
        if not finite(cash,False) or latency_ms<0 or adverse_ticks<0:raise ValueError('INVALID_REPLAY_ACCOUNT_INPUT')
        self.cash=cash;self.costs=costs;self.latency=timedelta(milliseconds=latency_ms)
        self.adverse_ticks=adverse_ticks;self.manual=deepcopy(manual or {})
        self.holdings=deepcopy(self.manual);self.pending={};self.submissions=[];self.fills=[]
        self.consumed={};self.frame=None;self.engine=None;self.sequence=0

    def submit(self,side,sym,qty,limit,venue='KRX',session='REGULAR'):
        if type(self.engine) is not AutonomousEngine:raise Rejected('PAPER_ENGINE_REQUIRED')
        # Engine writes INTENT before calling the broker. A test cannot directly
        # call submit and masquerade as an integrated order test.
        intents=[o for o in self.engine.active_orders(sym,side) if o['status']=='INTENT']
        if len(intents)!=1:raise Rejected('ENGINE_INTENT_REQUIRED')
        self.order_guard('kt10000' if side=='BUY' else 'kt10001',
            {'stk_cd':sym,'ord_qty':str(qty),'ord_uv':str(limit) if limit else ''})
        self.sequence+=1;oid=f'PAPER-L1-{self.sequence}'
        row={'id':oid,'side':side,'symbol':sym,'qty':qty,'remaining':qty,'limit':limit,
             'arrives_at':self.engine.clock()+self.latency,'filled':0,'amount':0.,'canceled':False}
        self.pending[oid]=row;self.submissions.append(dict(row));return oid

    def cancel(self,sym,oid,**kwargs):
        self.order_guard('kt10003',{'stk_cd':sym,'orig_ord_no':oid})
        if oid in self.pending:self.pending[oid]['canceled']=True
        return 'PAPER-CANCEL-'+oid

    def snapshot(self):
        orders=[{'ord_no':oid,'stk_cd':o['symbol'],'oso_qty':o['remaining'],
                 'cntr_tot_amt':o['amount']} for oid,o in self.pending.items()
                if o['remaining'] and not o['canceled']]
        equity=self.cash+sum(h['qty']*self.engine.prices.get(s,h.get('avg',0)) for s,h in self.holdings.items())
        return {'cash':self.cash,'equity':equity,'orders':orders,'holdings':deepcopy(self.holdings)}

    def advance(self,frame):
        self.frame=frame;t=frame.tick;e=self.engine
        # A recorded/synthetic off-grid quote is bad evidence, not a fill price.
        # In particular, never execute the old benchmark's 17,214 KRW price.
        market_prices_valid=all(finite(p,False) and p == floor_tick(p) for p in (t.price,t.ask,t.bid))
        changed=any(o['canceled'] for o in self.pending.values())
        for oid,o in list(self.pending.items()):
            if o['canceled'] or not o['remaining']:continue
            if not market_prices_valid:continue
            if o['symbol']!=t.symbol or o['arrives_at']>frame.available_at:continue
            if frame.quote_reason(frame.available_at,e.c.stale_seconds):continue
            side=o['side'];step=tick_size(t.ask if side=='BUY' else t.bid)
            price=t.ask+self.adverse_ticks*step if side=='BUY' else t.bid-self.adverse_ticks*step
            if price != floor_tick(price):continue
            if price<=0 or o['limit'] and (price>o['limit'] if side=='BUY' else price<o['limit']):continue
            key=(side,frame.book_key());remaining_depth=(t.ask_qty if side=='BUY' else t.bid_qty)-self.consumed.get(key,0)
            qty=min(o['remaining'],max(0,int(remaining_depth)))
            if not qty:continue
            # Price beyond L1 is a separately labelled sensitivity assumption;
            # it never increases a BUY's persisted limit.
            if side=='BUY':
                qty=min(qty,int(self.cash//(price*(1+self.costs.buy))))
                if not qty:continue
                self.cash-=qty*price+self.costs.buy_fee(qty*price)
                h=self.holdings.setdefault(t.symbol,{'qty':0,'available':0,'avg':0.})
                h['avg']=(h['avg']*h['qty']+qty*price)/(h['qty']+qty)
                h['qty']+=qty;h['available']=h['qty']
            else:
                h=self.holdings.get(t.symbol)
                qty=min(qty,h['available'] if h else 0)
                if not qty:continue
                self.cash+=qty*price-self.costs.sell_fee(qty*price)
                h['qty']-=qty;h['available']=h['qty']
                if not h['qty']:del self.holdings[t.symbol]
            self.consumed[key]=self.consumed.get(key,0)+qty
            o['remaining']-=qty;o['filled']+=qty;o['amount']+=qty*price
            order=next(x for x in e.s['orders'].values() if x['id']==oid)
            e.cumulative_fill(order,o['filled'],o['amount'])
            changed=True
            if self.adverse_ticks:
                fill_basis='EXPLICIT_ADVERSE_TICK_SENSITIVITY'
            elif frame.basis=='MEASURED_PAIRED_TRADE_QUOTE':
                fill_basis='MEASURED_L1_MARKETABLE_SIMULATION'
            elif frame.basis=='SYNTHETIC_TEST_QUOTE':
                fill_basis='SYNTHETIC_TEST_L1_SIMULATION'
            else:
                fill_basis='UNKNOWN_OR_UNVERIFIED_L1_SIMULATION'
            self.fills.append({'at':frame.available_at.isoformat(),'side':side,'symbol':t.symbol,
                'qty':qty,'price':price,'order':oid,'source_id':frame.source_id,
                'basis':fill_basis,'input_evidence_basis':frame.basis,
                'latency_ms':self.latency.total_seconds()*1000,'actual_fill':False})
        # Reconciliation and cancel acknowledgement use the real state machine.
        # Holdings reflect only prior simulated fills, never a future close.
        if changed:e.reconcile(self.snapshot())
        self.consumed={k:v for k,v in self.consumed.items() if k[1][0]!=t.symbol or k[1]==frame.book_key()}


class PaperAccount:
    def __init__(self,start,*,initial_cash,approved_cap=None,cfg=None,auto=None,
                 latency_ms=0,adverse_ticks=0,manual=None):
        if cfg is None:raise ValueError('EXPLICIT_PAPER_CONFIG_REQUIRED')
        if auto is None:raise ValueError('EXPLICIT_PAPER_AUTOMATION_REQUIRED')
        if approved_cap is None:approved_cap=auto.capital_limit
        self.temp=tempfile.TemporaryDirectory(prefix='R6-PAPER-CAUSAL-')
        self.root=Path(self.temp.name);self.at=start;self.frame=None
        # Callers must choose a scenario. Library defaults are 0.3%/1%, not
        # evidence of the user's 2%/4% operating configuration.
        self.cfg=replace(cfg,live_mode=True)
        self.auto=replace(auto,AUTO_TRADING_CAP_KRW=approved_cap)
        # live_mode=True exercises the installed asynchronous broker/guard path.
        # The constructor owns a fresh temporary root and this exact simulator;
        # no caller may inject Rest or a user runtime folder.
        from automation.capital import Costs
        costs=Costs(self.auto.commission_buy,self.auto.commission_sell,self.auto.sell_tax,
                    self.cfg.entry_slippage_fraction,self.cfg.exit_slippage_fraction)
        self.broker=QuoteReplayBroker(initial_cash,costs,latency_ms,adverse_ticks,manual)
        self.e=AutonomousEngine(self.cfg,self.auto,PaperPolicy(self.root),self.root/'book',self.broker,lambda:self.at)
        self.broker.engine=self.e
        self.e.reconcile(self.broker.snapshot());self.e.connected=True;self.e.market='3'
        self.e.entry_quote_provider=lambda sym:(self.frame.tick,{}) if self.frame and self.frame.tick.symbol==sym else (None,{})
        self.initial_cash=initial_cash;self.initial_bot_cash=self.e.s['cash']
        self.rejections=Counter();self.entry_decisions=[]
        self.metadata={}

    def close(self):self.temp.cleanup()

    def step(self,frame,*,bar_events=(),market_status=None):
        if self.frame and frame.available_at<self.at:raise ValueError('NONCAUSAL_ACCOUNT_FRAME')
        self.at=frame.available_at;self.frame=frame;t=frame.tick
        self.e.prices[t.symbol]=t.price
        if market_status is not None:self.e.market_status[t.symbol]=dict(market_status)
        self.broker.advance(frame)
        for symbol,bar in bar_events:self.e.observe_protection_bar(symbol,bar)
        if t.symbol in self.e.s['positions']:
            if frame.quote_reason(self.at,self.cfg.stale_seconds) is None:
                p=self.e.s['positions'][t.symbol]
                p['last_quote']={'time':t.book_time.isoformat(),'received_at':t.book_received_at,
                    'bid':t.bid,'ask':t.ask,'bid_qty':t.bid_qty,'ask_qty':t.ask_qty}
            self.e.on_tick(t)
        # Global session deadlines and order timeouts use the installed code.
        if self.e.s['positions'] or self.e.active_orders():self.e.timer(self.e.prices)
        self.broker.advance(frame)
        expected=self.initial_bot_cash+(self.broker.cash-self.initial_cash)
        if abs(self.e.s['cash']-expected)>.001:raise AssertionError('PAPER_CASH_CONSERVATION_FAILURE')

    def enter(self,signal):
        if type(self.broker) is not QuoteReplayBroker or self.root.name[:16]!='R6-PAPER-CAUSAL-':
            raise RuntimeError('OFFLINE_TYPED_BROKER_REQUIRED')
        if self.frame is None:raise ValueError('NO_CURRENT_MARKET_FRAME')
        if self.frame.quote_reason(self.at,self.cfg.stale_seconds):
            reason=self.frame.quote_reason(self.at,self.cfg.stale_seconds);self.rejections[reason]+=1;return None
        sig=json_values(deepcopy(signal))
        source=sig.get('source_evidence')
        if not isinstance(source,dict) or not aware(source.get('timestamp')):
            self.rejections['SOURCE_QUALIFICATION_UNKNOWN']+=1;return None
        if type(source.get('source_count')) is not int or source['source_count']<0:
            self.rejections['SOURCE_QUALIFICATION_UNKNOWN']+=1;return None
        self.e.source_qualification_seen=True
        self.e.source_qualification[sig['symbol']]=dict(source)
        sig.update(source_count=source.get('source_count'),fallback_source1=source.get('fallback_source1',False))
        detected=aware(sig.get('signal_detected_at') or sig.get('time'))
        if not detected or not 0<=(self.at-detected).total_seconds()<=sig.get('ttl_seconds',self.cfg.stale_seconds):
            self.rejections['EXPIRED_OR_FUTURE_SIGNAL']+=1;return None
        # This conversion is private to the owned paper account. The original
        # SHADOW signal never acquires production order permission.
        sig.pop('SHADOW_ONLY',None);sig.pop('actual_order_permission',None)
        sig.update(PAPER_CONTEXT_ONLY=True,source_original_permission=False)
        # A counterfactual snapshot of the PAPER ledger, not a historical REST
        # poll. It contains only already simulated fills and current prices.
        self.e.reconcile(self.broker.snapshot())
        self.e.entry_tick=self.frame.tick
        order=self.e.enter(sig,self.frame.tick)
        decision={'at':self.at.isoformat(),'symbol':sig['symbol'],'pattern':sig.get('pattern'),
            'order':order['local'] if order else None,'order_status':order.get('status') if order else None,
            'risk_equity':self.e.risk_equity(),'daily_risk_budget':self.e.daily_risk_budget(),
            'daily_realized_loss_used':self.e.s['daily_loss'],'cash':self.e.s['cash']}
        self.entry_decisions.append(decision)
        if not order:self.rejections['ENGINE_REJECTED']+=1
        self.broker.advance(self.frame)
        return order

    def result(self):
        grouped={}
        for t in self.e.s['trades']:
            group=grouped.setdefault(t['trade_id'],{'trade_id':t['trade_id'],'symbol':t['symbol'],
                'opened':t['opened'],'closed':t['closed'],'qty':0,'net_pnl':0.,'MFE':0.,'MAE':0.,'reason':t['reason']})
            group['qty']+=t['qty'];group['net_pnl']+=t['pnl_estimate'];group['closed']=t['closed']
            group['MFE']=max(group['MFE'],t['MFE']);group['MAE']=max(group['MAE'],t['MAE'])
        open_ids={p['trade_id'] for p in self.e.s['positions'].values()}
        closed=[g for key,g in grouped.items() if key not in open_ids]
        pnl=[g['net_pnl'] for g in closed];wins=[n for n in pnl if n>0];loss=[n for n in pnl if n<0]
        streak=max_streak=0
        for n in pnl:streak=streak+1 if n<0 else 0;max_streak=max(max_streak,streak)
        marked=sum(p['qty']*self.e.prices.get(s,p['avg']) for s,p in self.e.s['positions'].items())
        basis=sum(p['qty']*p['avg'] for p in self.e.s['positions'].values())
        return {'basis':'PAPER_ACCOUNT_SCENARIO_NOT_ACTUAL_TRADES','completed_trades':closed,
            'completed_count':len(closed),'wins':len(wins),'losses':len(loss),'breakeven':sum(n==0 for n in pnl),
            'net_pnl_closed':sum(pnl) if pnl else None,'win_rate':len(wins)/len(pnl) if pnl else None,
            'expectancy':sum(pnl)/len(pnl) if pnl else None,'profit_factor':sum(wins)/abs(sum(loss)) if loss else None,
            'average_win':sum(wins)/len(wins) if wins else None,'average_loss':sum(loss)/len(loss) if loss else None,
            'max_consecutive_losses':max_streak,'initial_cash':self.initial_cash,'initial_bot_cash':self.initial_bot_cash,
            'final_broker_cash':self.broker.cash,'final_bot_cash':self.e.s['cash'],
            'bot_marked_equity':self.e.s['cash']+marked,'unrealized_gross_pnl':marked-basis,
            'daily_risk_budget':self.e.daily_risk_budget(),'daily_loss_used':self.e.s['daily_loss'],
            'open_positions':deepcopy(self.e.s['positions']),'pending_orders':deepcopy(self.e.active_orders()),
            'fills':self.broker.fills,'orders_submitted':len(self.broker.submissions),
            'rejections':dict(self.rejections),'entry_decisions':self.entry_decisions,
            'forced_close_from_last_bar':False,'actual_order_permission':False,
            'account_authorization_fixture':'TEMPORARY_PAPER_ONLY_NOT_LIVE_APPROVAL',
            'cost_basis':'FROZEN_CONFIG_ESTIMATE_NOT_BROKER_FINAL_FEE_ROUNDING',
            'global_market_phase':'EXPLICIT_REGULAR_OPEN_3_SCENARIO_NOT_A_RECORDED_0G_EVENT',
            'initial_day_ledger':'EXPLICIT_FRESH_PAPER_SCENARIO_NOT_RESTORED_HISTORICAL_LEDGER',
            'safety_scope':'Engine risk, order and exit functions; production supervisor/REST/approval are not simulated',
            'risk_limits_are_gates_not_guaranteed_loss_caps':True}
