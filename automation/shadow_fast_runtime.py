"""Standalone data-only SHADOW session; no broker order object or LIVE engine.

Run with python -m automation.shadow_fast_runtime --check --source-root ... --output ...
Authentication occurs only in an explicit websocket run, after collision checks.
Virtual fills are assumptions on subsequent fresh frames, never brokerage fills.
"""
import argparse
import json
import math
import os
import queue
import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.request import urlopen

from core.settings import Config, now, load_config
from core.strategy import Tick
from core.util import code, tick_size, read_json_retry, atomic_json, ProcessLock
from core.event_log import append_jsonl
from core.realtime import Feed, parse_tick, parse_book, stream_symbol
from automation.policy import AutomationConfig, load_automation
from automation.capital import Costs
from automation.observation_worker import ObservationWorker

class RuntimeFeed(Feed):
    """Enqueue raw market frames on arrival, before main-loop ranking HTTP waits."""
    def __init__(self,rest,symbols,runtime):
        super().__init__(rest,symbols);self.runtime=runtime
    def emit(self,event):
        received=now()
        if event.get('trnm')=='REAL':self.runtime.raw(event,received)
        elif event.get('trnm') in ('ERROR','DISCONNECTED'):
            self.runtime.books.clear()
            self.runtime.worker.deliver_reset('TRANSPORT_BREAK',received)
        super().emit(event)
    def update_symbols(self,symbols):
        old=set(self.symbols);wire=list(self.wire_symbols)
        changed=super().update_symbols(symbols)
        for symbol in old-set(self.symbols):
            self.runtime.books.pop(symbol,None)
            self.runtime.worker.deliver_reset('EXPLICIT_UNSUBSCRIBE',now(),symbol=symbol)
        if old==set(self.symbols) and wire!=self.wire_symbols:
            self.runtime.books.clear();self.runtime.worker.deliver_reset('EXCHANGE_ROUTE_CHANGED',now())
        return changed


def ensure_isolated(output, source_root=None):
    output = Path(output).resolve()
    if source_root is not None:
        source = Path(source_root).resolve()
        if output == source or source in output.parents:
            raise RuntimeError('SHADOW_OUTPUT_MUST_BE_OUTSIDE_LIVE_ROOT')
    return output


def assert_source_stopped(source_root):
    """Read-only collision gate, repeated before every authentication/query."""
    from operator_process import busy, process_identity
    source = Path(source_root)
    if busy(source): raise RuntimeError('LIVE_WORKER_LOCK_ACTIVE')
    runtime = source/'runtime/automation-production'
    heartbeat = read_json_retry(runtime/'heartbeat.json') if (runtime/'heartbeat.json').exists() else {}
    if process_identity(heartbeat.get('pid')) is not None:
        raise RuntimeError('LIVE_WORKER_PROCESS_ACTIVE')
    state = read_json_retry(runtime/'state.json') if (runtime/'state.json').exists() else {}
    positions = state.get('positions', {})
    if any(p.get('qty', 0)>0 and p.get('ownership', 'BOT')=='BOT' for p in positions.values()):
        raise RuntimeError('LIVE_BOT_POSITION_PRESENT')
    blocked = {'NEW', 'INTENT', 'SUBMITTING', 'ACCEPTED', 'PARTIAL', 'CANCEL_SENT', 'UNKNOWN'}
    if any(o.get('status') in blocked for o in state.get('orders', {}).values()):
        raise RuntimeError('LIVE_PENDING_OR_UNKNOWN_ORDER_PRESENT')
    return True


class MarketDataOnly:
    """Only token, documented ranking/status/history reads reach the HTTP client.

    Both the exposed API and final HTTP opener deny order/account endpoints.
    Credentials and token stay in memory; nothing is copied into output files.
    """
    ALLOWED = {'ka10029', 'ka10032', 'ka10027', 'ka00198', 'ka10031', 'ka10100', 'ka10081'}
    def __init__(self, cfg, source_root=None, token_file=None):
        self.cfg = replace(cfg, live_mode=False)
        self.source_root = Path(source_root) if source_root else None
        self.token_file = Path(token_file) if token_file else None
        self._client = None
        self._client_lock = threading.RLock()
        self.auth_requests = 0
        self.query_requests = 0
        self.session_guard = self.guard

    def guard(self):
        if self.source_root is not None: assert_source_stopped(self.source_root)
        return True

    def _open(self, request, **kwargs):
        self.guard()
        api = request.get_header('Api-id') or request.get_header('api-id')
        if request.full_url.endswith('/oauth2/token'):
            self.auth_requests += 1
        elif api in self.ALLOWED and '/api/dostk/' in request.full_url:
            self.query_requests += 1
        else: raise RuntimeError('SHADOW_HTTP_ENDPOINT_DENIED')
        return urlopen(request, **kwargs)

    def _bootstrap(self):
        with self._client_lock:
            return self._bootstrap_locked()

    def _bootstrap_locked(self):
        self.guard()
        if self._client is None:
            if self.source_root is None: raise RuntimeError('SOURCE_ROOT_REQUIRED_FOR_REST_DISCOVERY')
            path = self.source_root/'.env'
            env = {}
            if path.exists():
                for line in path.read_text(encoding='utf-8-sig').splitlines():
                    if '=' not in line or line.lstrip().startswith('#'): continue
                    name, value = line.split('=', 1)
                    if name.strip() in {'KIWOOM_APP_KEY', 'KIWOOM_APP_SECRET'}:
                        env[name.strip()] = value.strip().strip('"\'')
            saved = {name:os.environ.get(name) for name in env}
            try:
                os.environ.update(env)
                from core.broker import Rest
                self._client = Rest(self.cfg, opener=self._open)
                self._client.session_guard = self.guard
                if self.token_file is not None:
                    # Reuse only the explicit SHADOW token source. A parallel
                    # ranking request must not issue a second token and eject WS.
                    self._client.get_token = self.get_token
            finally:
                for name, value in saved.items():
                    if value is None: os.environ.pop(name, None)
                    else: os.environ[name] = value
        return self._client

    def get_token(self):
        if self.source_root is None:raise RuntimeError('SOURCE_ROOT_REQUIRED_FOR_LIVE_COLLISION_CHECK')
        self.guard()
        if self.token_file is not None:
            text = self.token_file.read_text(encoding='utf-8-sig').strip()
            if text.startswith('{'): text = json.loads(text).get('token', '')
            if not text: raise RuntimeError('EMPTY_SHADOW_TOKEN_FILE')
            return text
        return self._bootstrap().get_token()

    def query(self, api, payload, path='/api/dostk/rkinfo', **kwargs):
        if api not in self.ALLOWED: raise RuntimeError('SHADOW_READ_API_DENIED')
        return self._bootstrap().query(api, payload, path=path, **kwargs)

    def pages(self, api, payload, key, path='/api/dostk/rkinfo', **kwargs):
        if api not in self.ALLOWED: raise RuntimeError('SHADOW_READ_API_DENIED')
        return self._bootstrap().pages(api, payload, key, path=path, **kwargs)

    def stock_status(self, symbol):
        self.guard()
        return self._bootstrap().stock_status(symbol)


class VirtualAccount:
    """One-slot chronological model, integer shares, no tape-end liquidation."""
    def __init__(self, cfg, auto, output, strategy):
        self.cfg, self.auto, self.output, self.strategy = cfg, auto, Path(output), strategy
        self.initial = float(auto.capital_limit or 81300)
        self.cash = self.initial; self.loss = 0.; self.realized = 0.
        self.pending = None; self.position = None; self.exit_pending = None
        self.entries = 0; self.closed = 0
        self.last_bid = None; self.last_quote_at = None; self.coverage_unknown = False

    def event(self, kind, **values):
        append_jsonl(self.output/'runtime/shadow_virtual_ledger.jsonl',
            {'event':kind,'SHADOW_ONLY':True,'actual_order_permission':False,
             'evidence_class':'MODELED_NOT_BROKER_FILL','fill_certainty':'UNKNOWN_WITHOUT_QUEUE_POSITION',**values})

    def risk_unit(self, entry, emergency):
        return entry*(1+self.auto.commission_buy)-emergency*(1-self.cfg.exit_slippage_fraction)*(1-self.auto.commission_sell-self.auto.sell_tax)

    def context(self):
        equity = self.cash+(self.position['qty']*self.position['avg'] if self.position else 0)
        budget = self.initial*self.cfg.max_daily_loss_fraction
        open_risk=self.position['qty']*self.risk_unit(self.position['avg'],self.position['emergency']) if self.position else 0
        pending_risk=self.pending.get('reserved_risk',0) if self.pending else 0
        remaining=max(0,budget-self.loss-open_risk-pending_risk)
        risk = min(equity*self.cfg.risk_fraction,remaining)
        reserved = self.pending.get('reserved_cash',0) if self.pending else 0
        return {'effective_auto_cap':equity,'risk_equity':equity,'entry_budget':max(0,self.cash-reserved),
                'risk_remaining':remaining,'account_fresh':True,'risk_budget':risk,'reserved_risk':pending_risk,'open_risk':open_risk,
                'cash_is_virtual':True,'approved_cap':self.initial}

    def signal(self, record):
        if self.position or self.pending or self.loss>=self.initial*self.cfg.max_daily_loss_fraction: return
        if not record.get('SHADOW_ONLY') or record.get('actual_order_permission') is not False: return
        self.pending = dict(record)
        worst_entry=record['max_chase_price']*(1+self.cfg.entry_slippage_fraction)
        worst_unit=self.risk_unit(worst_entry,record['emergency'])
        allowance=self.context()['risk_budget']
        quantity=min(int(record['quantity']),int(self.cash//(worst_entry*(1+self.auto.commission_buy))),
            int(allowance//worst_unit) if worst_unit>0 else 0)
        if quantity<=0:self.pending=None;return
        self.pending['quantity']=quantity
        self.pending['reserved_cash'] = min(self.cash,record['quantity']*record['max_chase_price']*(1+self.cfg.entry_slippage_fraction)*(1+self.auto.commission_buy))
        self.pending['reserved_cash']=quantity*worst_entry*(1+self.auto.commission_buy)
        self.pending['reserved_risk']=quantity*worst_unit
        self.event('VIRTUAL_ENTRY_PENDING',timestamp=record['timestamp'],symbol=record['symbol'],signal_at=record.get('signal_at'))

    def fresh(self,t,processed,qty=1,side='BUY'):
        return (t.time.tzinfo is not None and processed.tzinfo is not None and
                0<=(processed-t.time).total_seconds()<=self.cfg.stale_seconds and t.regular and
                t.depth_available is True and t.book_time is not None and
                0<=(processed-t.book_time).total_seconds()<=self.cfg.stale_seconds and
                0<t.bid<=t.ask and t.bid_qty>=qty and (side=='SELL' or t.ask_qty>=qty))

    def tick(self,t,received,processed):
        if self.position and t.symbol==self.position['symbol'] and self.fresh(t,processed,self.position['qty'],side='SELL'):
            self.last_bid=t.bid;self.last_quote_at=processed.isoformat()
        if self.exit_pending and self.position and t.symbol==self.position['symbol']:
            if processed>datetime.fromisoformat(self.exit_pending['timestamp']) and self.fresh(t,processed,self.position['qty'],side='SELL'):
                p=self.position; price=t.bid*(1-self.cfg.exit_slippage_fraction)
                proceeds=price*p['qty']*(1-self.auto.commission_sell-self.auto.sell_tax)
                net=proceeds-p['paid']; self.cash+=proceeds; self.realized+=net; self.loss+=max(0,-net); self.closed+=1
                self.strategy.record_exit(t.symbol,processed)
                self.event('VIRTUAL_EXIT_FILL',timestamp=processed.isoformat(),symbol=t.symbol,qty=p['qty'],bid=t.bid,
                    exit_price=price,entry_price=p['avg'],net_pnl=net,reason=self.exit_pending['reason'],
                    signal_at=self.exit_pending['timestamp'],cash=self.cash)
                self.position=None;self.exit_pending=None
                return
        if self.position and t.symbol==self.position['symbol'] and not self.exit_pending and self.fresh(t,processed,self.position['qty'],side='SELL'):
            should,reason,detail=self.strategy.evaluate_exit(self.position,t,processed)
            if should:
                self.exit_pending={'timestamp':processed.isoformat(),'reason':reason}
                self.event('VIRTUAL_EXIT_PENDING',timestamp=processed.isoformat(),symbol=t.symbol,reason=reason,evidence=detail)
        s=self.pending
        if not s or t.symbol!=s['symbol']:return
        made=datetime.fromisoformat(s.get('signal_at') or s['timestamp'])
        if (processed-made).total_seconds()>s['ttl_seconds']:
            self.event('VIRTUAL_ENTRY_EXPIRED',timestamp=processed.isoformat(),symbol=t.symbol);self.pending=None;return
        if processed<=datetime.fromisoformat(s['timestamp']) or not self.fresh(t,processed) or t.ask>s['max_chase_price']:return
        price=t.ask*(1+self.cfg.entry_slippage_fraction)
        # Replacing this pending intent releases its own reservation, not any
        # other slot/cash. The one-slot model cannot reserve another BUY.
        budget=min(self.initial*self.cfg.max_daily_loss_fraction-self.loss,self.cash*self.cfg.risk_fraction)
        unit=self.risk_unit(price,s['emergency'])
        reward=s['target']*(1-self.cfg.exit_slippage_fraction)*(1-self.auto.commission_sell-self.auto.sell_tax)-price*(1+self.auto.commission_buy)
        if unit<=0 or reward/unit<self.cfg.min_reward_risk:
            self.event('VIRTUAL_ENTRY_REWARD_REJECT',timestamp=processed.isoformat(),symbol=t.symbol,net_reward_risk=reward/unit if unit>0 else None)
            self.pending=None;return
        qty=min(int(s['quantity']),int(self.cash//(price*(1+self.auto.commission_buy))),int(budget//unit) if unit>0 else 0)
        if qty<=0 or min(t.bid_qty,t.ask_qty)<qty:return
        paid=price*qty*(1+self.auto.commission_buy);self.cash-=paid;self.entries+=1
        self.position={'symbol':t.symbol,'avg':price,'qty':qty,'paid':paid,'stop':s['stop'],'emergency':s['emergency'],
            'target':s['target'],'opened':processed.isoformat(),'risk_budget':budget,'initial_stop':s['stop']}
        self.pending=None
        self.event('VIRTUAL_ENTRY_FILL',timestamp=processed.isoformat(),symbol=t.symbol,qty=qty,ask=t.ask,
            entry_price=price,stop=s['stop'],emergency=s['emergency'],risk=unit*qty,cash=self.cash,signal_at=s.get('signal_at'))

    def finish(self):
        marked=None
        if self.position and self.last_bid is not None:
            marked=self.last_bid*(1-self.cfg.exit_slippage_fraction)*self.position['qty']*(1-self.auto.commission_sell-self.auto.sell_tax)-self.position['paid']
        result={'entries':self.entries,'closed_round_trips':self.closed,'cash':self.cash,'realized_net_pnl':self.realized,
            'daily_loss_used':self.loss,'open_position':self.position,'pending_entry':self.pending,'pending_exit':self.exit_pending,
            'unrealized_modeled_net_pnl':marked,'valuation_quote_at':self.last_quote_at,'coverage_unknown':self.coverage_unknown,
            'tape_end_action':'KEEP_UNRESOLVED_NOT_LIQUIDATED','actual_order_permission':False,
            'virtual_fill_certainty':'UNKNOWN_WITHOUT_QUEUE_POSITION','profitability_proven':False}
        atomic_json(self.output/'runtime/shadow_account.json',result);return result

    def coverage_break(self,record):
        symbol=record.get('symbol')
        if symbol is not None and not (self.pending and self.pending['symbol']==symbol) and not (self.position and self.position['symbol']==symbol):return
        self.coverage_unknown=True
        if self.pending and (symbol is None or self.pending['symbol']==symbol):
            self.event('VIRTUAL_ENTRY_COVERAGE_CANCELED',timestamp=record['timestamp'],symbol=self.pending['symbol'],reason=record.get('reason'))
            self.pending=None


class ShadowFastRuntime:
    def __init__(self, cfg, auto, output, rest=None, clock=now):
        self.output=Path(output);self.output.mkdir(parents=True,exist_ok=True)
        self.cfg=replace(cfg,live_mode=False,max_positions=1);self.auto=auto;self.clock=clock
        costs=Costs(auto.commission_buy,auto.commission_sell,auto.sell_tax,cfg.entry_slippage_fraction,cfg.exit_slippage_fraction)
        self.worker=ObservationWorker(self.cfg,auto,costs,self.output,rest=rest,
            on_fast_signal=self.on_signal,on_processed_tick=self.on_tick,on_stream_reset=self.on_reset)
        self.worker.clock=clock
        self.account=VirtualAccount(self.cfg,auto,self.output,self.worker.observer.fast_strategy)
        self.worker.set_capital(self.account.context())
        self.feed=None;self.feed_drops=0;self.books={}
        self.subscription_acks=0

    def on_signal(self,record): self.account.signal(record)
    def on_reset(self,record):self.account.coverage_break(record)
    def on_tick(self,t,received,processed):
        self.account.tick(t,received,processed);self.worker.set_capital(self.account.context())

    def candidates(self,rows,stamp):
        self.worker.event('SHADOW_CANDIDATE_BATCH',timestamp=stamp.isoformat(),actual_order_permission=False,
            candidates=[{k:r.get(k) for k in ('code','name','first_raw_at','observed_at','reference_price','source_count','affordability')} for r in rows])
        self.worker.submit(rows,stamp,self.account.context(),allow_metadata=self.worker.rest is not None)

    def capture_rows(self,rows,limit=32):
        """Use one confirmed ranking order for subscription AND metadata input."""
        def rank(row):
            ranks=[v.get('observed_rank',999999) for v in row.get('source_details',{}).values()]
            return (-row.get('source_count',0),min(ranks,default=999999),row['code'])
        eligible=sorted((r for r in rows if r.get('affordability',{}).get('entry_eligible') is True),key=rank)
        held=self.account.position['symbol'] if self.account.position else None
        protected=next((r for r in eligible if r['code']==held),None)
        if held and protected is None:protected=self.worker.observer.rows.get(held)
        selected=eligible[:max(0,limit-(1 if protected else 0))]
        if protected:selected=[dict(protected,status='WATCH')]+[r for r in selected if r['code']!=held]
        return selected[:limit]

    def raw(self,event,received=None):
        received=received or self.clock()
        for item in event.get('data',[]):
            typ=item.get('type');raw=item.get('item')
            if typ not in ('0B','0D') or not raw:continue
            sym=stream_symbol(raw)
            if typ=='0D':
                self.books[sym]=parse_book(item,received)
                self.books[sym]['subscription_item']=raw
                if not item.get('values',{}).get('21'):
                    # A receive-time fallback must not masquerade as the
                    # original quote clock or establish FAST depth freshness.
                    self.books[sym]['time']=None
                self.worker.deliver_book(sym,self.books[sym],received)
            else:
                parsed=parse_tick(item,received,book=self.books.get(sym),book_stale_seconds=self.cfg.stale_seconds)
                fields=item.get('values',{})
                recorded=SimpleNamespace(**parsed.__dict__,subscription_item=raw,
                    venue_basis='EXCHANGE_FIELD_9081' if fields.get('9081') else 'PARSE_DEFAULT_UNKNOWN_VENUE',
                    raw_market_fields={k:fields.get(k) for k in ('20','10','15','13','27','28','61','71','9081','290')})
                self.worker.deliver(recorded,received)

    def close(self):
        connection={'logged_in':self.feed.logged_in,'transport_failed':self.feed.transport_failed,
            'last_message_at':self.feed.last_message_at.isoformat() if self.feed.last_message_at else None} if self.feed else None
        if self.feed:self.feed.close()
        stats=self.worker.close();result=self.account.finish()
        result.update(worker=stats,feed_control_queue_overflow_events=self.feed_drops,
            feed_control_frames_lost='UNKNOWN_EVENT_BOOLEAN_ONLY',actual_order_calls=0,
            connection_before_close=connection,subscription_acks=self.subscription_acks)
        atomic_json(self.output/'runtime/shadow_result.json',result);return result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',required=True);p.add_argument('--source-root');p.add_argument('--token-file')
    p.add_argument('--symbols',nargs='*',default=[]);p.add_argument('--seconds',type=float,default=600)
    p.add_argument('--check',action='store_true');p.add_argument('--mock-jsonl')
    args=p.parse_args(argv);source=Path(args.source_root).resolve() if args.source_root else None
    if source is None and not args.mock_jsonl:
        raise RuntimeError('SOURCE_ROOT_REQUIRED_FOR_LIVE_COLLISION_CHECK')
    output=ensure_isolated(args.output,source)
    cfg=replace(load_config(source/'config.json'),live_mode=False) if source else Config(risk_fraction=.02,max_daily_loss_fraction=.04,max_positions=1,max_position_fraction=1.)
    auto=load_automation(source/'automation.json') if source else AutomationConfig(AUTO_TRADING_CAP_KRW=81300,costs_confirmed=True)
    if source:assert_source_stopped(source)
    if args.check:
        print(json.dumps({'collision_check':'PASS','network_calls':0,'live_mode':False,'output':str(output),
            'order_surface':False,'auth_on_explicit_websocket_run_only':True,'source_root':str(source) if source else None}));return 0
    symbols=[code(s) for s in args.symbols]
    with ProcessLock(output/'runtime/shadow_fast.lock'):
        rest=None if args.mock_jsonl else MarketDataOnly(cfg,source,args.token_file)
        runtime=ShadowFastRuntime(cfg,auto,output,rest)
        runtime.worker.start()
        try:
            if args.mock_jsonl:
                for line in Path(args.mock_jsonl).read_text(encoding='utf-8-sig').splitlines():
                    frame=json.loads(line);stamp=datetime.fromisoformat(frame['received'])
                    if frame.get('candidates'):runtime.candidates(frame['candidates'],stamp)
                    else:runtime.raw(frame,stamp)
            else:
                # Feed exposes only websocket traffic here; no order/position engine exists.
                feed=RuntimeFeed(rest,symbols,runtime);runtime.feed=feed
                feed.start();end=time.monotonic()+args.seconds;next_rank=0.
                while time.monotonic()<end:
                    rest.guard()
                    if source and time.monotonic()>=next_rank:
                        from automation.radar import collect
                        rows=collect(rest);stamp=now()
                        for row in rows:
                            price=row.get('reference_price')
                            row.update(source_count=len(row.get('source_details',{})),status='RAW',observed_at=stamp.isoformat(),
                                affordability={'entry_eligible':bool(price and price*(1+auto.commission_buy)<=auto.capital_limit)})
                        captured=runtime.capture_rows(rows)
                        runtime.worker.event('SHADOW_RAW_CANDIDATES',timestamp=stamp.isoformat(),raw_count=len(rows),
                            capture_symbols=[r['code'] for r in captured],actual_order_permission=False,
                            candidates=[{k:r.get(k) for k in ('code','name','first_raw_at','observed_at','reference_price','source_count')} for r in rows])
                        runtime.candidates(captured,stamp)
                        selected=[r['code'] for r in captured]
                        feed.update_symbols(list(dict.fromkeys(symbols+selected)))
                        next_rank=time.monotonic()+max(60,auto.scan_seconds)
                    try:received,event=feed.q.get(timeout=.1)
                    except queue.Empty:continue
                    if feed.overflow.is_set():
                        runtime.feed_drops+=1;feed.overflow.clear();runtime.worker.deliver_reset('FEED_CONTROL_QUEUE_GAP',now())
                    if event.get('trnm') in ('DISCONNECTED','ERROR'):
                        runtime.books.clear()
                        runtime.worker.event('SHADOW_TRANSPORT',timestamp=now().isoformat(),status=event.get('trnm'),reason=event.get('reason'),actual_order_permission=False)
                    elif event.get('trnm')=='REG':
                        runtime.subscription_acks+=1
                        runtime.worker.event('SHADOW_SUBSCRIPTION_ACK',timestamp=received.isoformat(),return_code=event.get('return_code'),actual_order_permission=False)
                    # REAL frames were already enqueued at transport receipt;
                    # processing them here again would duplicate trades/volume.
        finally:
            print(json.dumps(runtime.close(),ensure_ascii=False,default=str))
    return 0


if __name__=='__main__':raise SystemExit(main())
