"""Actual-context PAPER observer. Reads existing evidence; never imports Rest.

Account/authority evidence is a prerequisite, not a substitute for the real
detector, entry engine, quote boundary, sizing, persisted INTENT and exits.
Missing context blocks new PAPER buys while existing PAPER protection runs.
"""
from collections import Counter
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import json

from automation.entry_evidence import AUTHORITY_GATES
from automation.trade_review_capture import TradeReviewCapture
from core.realtime import current_book_tick
from core.market_evidence import aware,finite
from research.causal_data import Frame,json_values
from research.strict_benchmark import FullPathReplay


def configured_c_reward_bound(cfg,auto,costs):
    """Diagnostic best-case bound, not an entry or a proposed target.

    Existing C: width/low <= box_max_range, entry >= top, stop <= low,
    target <= top+2*width. Ignore buffers/noise/overshoot to OVERSTATE RR.
    Scale cancels in the existing linear common cost model.
    """
    from automation.capital import entry_economics
    low=10000.;width=low*auto.box_max_range;top=low+width
    best=entry_economics(top,low,low,top+2*width,costs,cfg.min_reward_risk)
    return dict(basis='MATHEMATICAL_UPPER_BOUND_NOT_MARKET_SIGNAL',
        box_max_range=auto.box_max_range,maximum_projection_widths=2,
        net_rr_upper_bound=best.get('net_rr'),required_net_rr=cfg.min_reward_risk,
        configured_intersection_possible=best.get('net_rr') is not None and best['net_rr']>=cfg.min_reward_risk,
        ignored_cost_increases=['STOP_BUFFER','NOISE','BREAKOUT_OVERSHOOT'],actual_order_permission=False)


class ObservedAuthority:
    def __init__(self,account):
        self.account=account;self.context={};self.frame=None;self.counts=Counter()
        e=account.e;quantity=e.quantity_details;market=e.entry_market_reason
        self.original_readiness=getattr(e,'readiness_reason',None)
        e.readiness_reason=self.reason
        def sized(signal,equity,cash,remaining):
            row=self.context.get(signal.get('symbol'),{});a=row.get('account') or {}
            known=all(finite(a.get(k),False) for k in ('actual_cash','book_cash','risk_equity','remaining_risk','approved_cap'))
            if known:
                equity=min(equity,a['risk_equity']);cash=min(cash,a['actual_cash'],a['book_cash'],a['approved_cap'])
                remaining=min(remaining,a['remaining_risk'])
            result=quantity(signal,equity,cash,remaining)
            result['observed_quantity_status']='OBSERVED_ACCOUNT_CAPPED_PAPER' if known else 'UNKNOWN_ACTUAL_ACCOUNT_SCENARIO_ONLY'
            return result
        def boundary(signal,qty,fallback=None,phase='PRE_INTENT'):
            why=self.reason()
            if why:return why
            row=self.context[self.frame.tick.symbol]
            setup='E2' if signal.get('pattern')=='E' else signal.get('pattern')
            trial=next((t for t in row.get('strategies',[]) if t.get('setup')==setup),{})
            if trial.get('status')=='DISABLED':return 'OBSERVED_STRATEGY_DISABLED'
            if trial.get('status')!='READY':return 'OBSERVED_MATCHING_STRATEGY_NOT_READY'
            native='E2' if row.get('selected_setup')=='E' else row.get('selected_setup')
            if native!=setup:return 'OBSERVED_SELECTED_STRATEGY_DIFFERS'
            if any(signal.get(k)!=row.get(v) for k,v in
                   (('limit','order_limit'),('stop','initial_invalidation'),('target','target'))):
                return 'OBSERVED_SIGNAL_PRICES_DIFFER'
            return market(signal,qty,fallback,phase)
        e.quantity_details=sized;e.entry_market_reason=boundary

    def update(self,row):
        if row.get('event')=='ENTRY_DECISION_EVIDENCE' and row.get('symbol'):
            self.context[row['symbol']]=row
            if len(self.context)>12:self.context.pop(next(iter(self.context)))

    def reason(self):
        return self.context_reason()

    def context_reason(self,*,require_native_signal=True):
        e=self.account.e;frame=self.frame
        if frame is None:return self._why('OBSERVED_FRAME_UNKNOWN')
        row=self.context.get(frame.tick.symbol)
        if row is None:return self._why('OBSERVED_CONTEXT_UNKNOWN')
        if row.get('producer_execution')=='PAPER' and row.get('input_evidence_basis')!='SYNTHETIC_TEST_QUOTE':
            return self._why('PAPER_OUTPUT_IS_NOT_LIVE_AUTHORITY')
        at=aware(row.get('asof'));now=e.clock()
        if at is None or not 0<=(now-at).total_seconds()<=e.c.stale_seconds:
            return self._why('OBSERVED_CONTEXT_STALE_OR_FUTURE')
        # A nearby symbol/price sample is not the same received print.
        t=frame.tick
        if (row.get('exchange_at')!=t.time.isoformat() or row.get('received_at')!=t.received_at
                or row.get('cumulative')!=t.cumulative):
            return self._why('OBSERVED_RECEIPT_LINK_UNKNOWN')
        if (row.get('book_received_at')!=t.book_received_at
                or row.get('book_time')!=(t.book_time.isoformat() if t.book_time else None)):
            return self._why('OBSERVED_BOOK_RECEIPT_LINK_UNKNOWN')
        authority=row.get('authority') or {};a=row.get('account') or {}
        if row.get('input_evidence_basis') not in {'ACTUAL_RECEIVED_LIVE_PIPELINE','MEASURED_PAIRED_TRADE_QUOTE','SYNTHETIC_TEST_QUOTE'}:
            return self._why('OBSERVED_INPUT_ORIGIN_UNKNOWN')
        if require_native_signal:
            for k in AUTHORITY_GATES:
                value=authority.get('observed_final_gates',{}).get(k)
                if value!='PASS':return self._why('OBSERVED_GATE_'+('UNKNOWN_' if value in (None,'UNKNOWN') else 'BLOCKED_')+k.upper())
            final=row.get('final_gate_results') or {}
            required=row.get('final_required_gate_results') or {}
            if not required or any(value!='PASS' or final.get(k)!=value for k,value in required.items()):
                return self._why('OBSERVED_FINAL_GATE_NOT_PASS_OR_UNKNOWN')
            if type(a.get('quantity')) is not int or a['quantity']<1:
                return self._why('OBSERVED_PRE_INTENT_QUANTITY_NOT_POSITIVE_OR_UNKNOWN')
        if authority.get('mode')!='LIVE' or authority.get('live_authorized') is not True:
            return self._why('OBSERVED_LIVE_AUTHORITY_NOT_CONFIRMED')
        if any(authority.get(k) is not True for k in ('live_mode','connected','account_ok')):
            return self._why('OBSERVED_CONNECTION_OR_ACCOUNT_NOT_CONFIRMED')
        if authority.get('costs_confirmed') is not True or authority.get('halt'):
            return self._why('OBSERVED_COSTS_OR_HALT_BLOCKED')
        if authority.get('balance_refresh_uncertain') is True:return self._why('OBSERVED_BALANCE_UNCERTAIN')
        snapshot=aware(a.get('snapshot_at'))
        if (a.get('account_fresh') is not True or snapshot is None
                or not 0<=(now-snapshot).total_seconds()<=e.c.reconcile_seconds*3):
            return self._why('OBSERVED_ACCOUNT_FRESHNESS_UNKNOWN')
        for k in ('actual_cash','book_cash','risk_equity','remaining_risk','approved_cap','slots','max_positions'):
            if not finite(a.get(k),False) or a[k]<0:return self._why('OBSERVED_ACCOUNT_VALUE_UNKNOWN_'+k.upper())
        if a['slots']>=a['max_positions']:return self._why('OBSERVED_POSITION_CAPACITY_BLOCKED')
        if a.get('risk_fraction')!=e.c.risk_fraction or a.get('max_daily_loss_fraction')!=e.c.max_daily_loss_fraction:
            return self._why('OBSERVED_RISK_PROFILE_DIFFERS')
        if a.get('max_position_fraction')!=e.c.max_position_fraction or a['max_positions']!=e.c.max_positions:
            return self._why('OBSERVED_POSITION_PROFILE_DIFFERS')
        if a.get('min_reward_risk')!=e.c.min_reward_risk:return self._why('OBSERVED_RR_PROFILE_DIFFERS')
        expected=dict(buy=e.costs.buy,sell=e.costs.sell,tax=e.costs.tax,
                      entry_slippage=e.costs.entry_slippage,exit_slippage=e.costs.exit_slippage)
        if row.get('costs')!=expected:return self._why('OBSERVED_COST_PROFILE_DIFFERS')
        if self.original_readiness:
            why=self.original_readiness()
            if why:return self._why(why)
        return ''

    def _why(self,value):self.counts[value]+=1;return value


class ObservedPaperSession:
    def __init__(self,start,cfg,auto,cash,output,*,basis='RECORDED_REPLAY',start_writer=True,evaluate_each_trade=False,
                 replay_factory=FullPathReplay,authority_factory=ObservedAuthority):
        self.replay=replay_factory(start,'LIVE_ABC',cfg,auto,cash)
        self.authority=authority_factory(self.replay.account)
        self.capture=TradeReviewCapture(output,clock=lambda:self.replay.account.at,start=start_writer,
            day_bytes=64*1024**2,total_bytes=256*1024**2,queue_rows=1024,queue_bytes=2*1024**2)
        self.replay.account.e.trade_review=self.capture
        self.basis=basis;self.last_trade={};self.books={};self.counts=Counter();self.source_losses={};self.references={}
        self.evaluate_each_trade=evaluate_each_trade

    def feed(self,row,source_id='',observer_at=None):
        event=row.get('event');self.counts['INPUT:'+str(event)]+=1
        stamp=max((v for k in ('processed_at','known_at','asof','received_at','at','time')
                   if (v:=aware(row.get(k))) is not None),default=None)
        if stamp is None:self.counts['UNKNOWN_AVAILABILITY']+=1;return
        source_stamp=stamp
        if observer_at is not None:stamp=max(stamp,observer_at)
        session=row.get('capture_session');loss=row.get('capture_losses')
        if session and type(loss) is int:
            previous=self.source_losses.get(session)
            if previous is None and loss:self.counts['PREEXISTING_CAPTURE_LOSS_UNKNOWN']+=loss
            if previous is not None and loss>previous:
                self.counts['REPORTED_CAPTURE_LOSS']+=loss-previous
                for sym in self.last_trade:self.replay.detector.mark_coverage_gap(sym,stamp,'RECORDER_LOSS_UNKNOWN_RANGE')
            self.source_losses[session]=loss
        symbol=row.get('symbol')
        if event=='WATCH_METADATA':
            if row.get('active',True) and isinstance(row.get('watch'),dict):
                watch=dict(row['watch']);self.replay.qualify(symbol,watch,source_stamp)
            else:self.replay.watch.pop(symbol,None);self.replay.watch_times.pop(symbol,None)
            self.capture.set_scope(self.replay.watch,self.replay.account.e.s['positions'],stamp)
        elif event=='DAILY_REFERENCE':self.replay.reference(symbol,row['bars'],stamp)
        elif event=='REFERENCE_BARS_PART':
            if type(row.get('parts')) is not int or not 1<=row['parts']<=96 or not 0<=row.get('part',-1)<row['parts']:
                self.counts['REFERENCE_PARTS_INVALID_UNKNOWN']+=1;return
            key=(symbol,row.get('reference_sha256'),row.get('known_at'));parts=self.references.setdefault(key,{})
            parts[row['part']]=row['rows']
            if len(parts)==row['parts']:
                import hashlib
                raw_bars=[b for i in range(row['parts']) for b in parts[i]]
                values=[[b['time']]+[b[k] for k in ('open','high','low','close')]+[b.get('volume')] for b in raw_bars]
                digest=hashlib.sha256(json.dumps(values,separators=(',',':')).encode()).hexdigest()
                bars=[{**b,'time':aware(b['time'])} for b in raw_bars]
                if digest==row.get('reference_sha256'):
                    self.replay.detector.price_context.merge_history(symbol,bars,stamp,row.get('venue','KRX'))
                else:self.counts['REFERENCE_HASH_MISMATCH_UNKNOWN']+=1
                self.references.pop(key,None)
            if len(self.references)>24:self.references.pop(next(iter(self.references)));self.counts['REFERENCE_PART_OVERFLOW_UNKNOWN']+=1
        elif event=='SUBSCRIPTION_SCOPE':
            for sym in row.get('removed',[]):self.replay.detector.mark_coverage_gap(sym,stamp,'RECORDED_UNSUBSCRIBE')
        elif event=='STREAM_STATE':
            if row.get('kind') in {'DISCONNECTED','RECONNECTING','TRANSPORT_FAILURE','ERROR'}:
                for sym in self.last_trade:self.replay.detector.mark_coverage_gap(sym,stamp,'RECORDED_STREAM_GAP')
        elif event=='ENTRY_DECISION_EVIDENCE':
            self.authority.update(row)
            prior=self.last_trade.get(symbol)
            if prior and stamp>=self.replay.account.at:
                self._step(replace(prior,available_at=stamp),True)
        elif event=='OPPORTUNITY_SAMPLE':
            # Its WATCH is known at this sample, not at an earlier price.
            watch={**(row.get('watch') or {}),'market_status':row.get('market_status',{})}
            if type(watch.get('source_count')) is int:
                self.replay.qualify(symbol,watch,source_stamp)
                self.capture.set_scope(self.replay.watch,self.replay.account.e.s['positions'],stamp)
            self.counts['SAMPLED_CONTEXT_NOT_FINAL_ORDER_AUTHORITY']+=1
            prior=self.last_trade.get(symbol)
            if (prior and row.get('cumulative')==prior.tick.cumulative
                    and str(row.get('exchange_at'))==prior.tick.time.isoformat()):
                self._step(replace(prior,available_at=stamp),True)
        elif event=='BOOK':
            previous=self.books.get(symbol)
            if previous and str(row.get('received_at',''))<str(previous.get('received_at','')):
                self.counts['OLDER_BOOK_IGNORED']+=1;return
            book={**row,'time':row.get('exchange_at'),'received_at':row.get('received_at')}
            # Historical process monotonic counters are not this process's clock.
            book.pop('received_monotonic',None);self.books[symbol]=book
            prior=self.last_trade.get(symbol)
            if prior:
                tick=current_book_tick(prior.tick,book,stamp,self.replay.cfg.stale_seconds)
                if tick is not prior.tick:
                    admitted=symbol in self.authority.context
                    self._step(replace(prior,tick=tick,available_at=stamp),True,evaluate=admitted or self.evaluate_each_trade)
        elif event in {'TRADE','TICK'}:
            raw={**row,'event':'TRADE','time':row.get('time') or row.get('exchange_at')}
            try:frame=Frame.from_record(raw,source_id)
            except (KeyError,TypeError,ValueError):
                self.counts['TRADE_PROCESSING_OR_RECEIPT_UNKNOWN']+=1;return
            frame=replace(frame,basis=row.get('input_evidence_basis','MEASURED_PAIRED_TRADE_QUOTE'),available_at=stamp)
            prior=self.last_trade.get(symbol)
            if prior and (frame.tick.time,frame.tick.cumulative)==(prior.tick.time,prior.tick.cumulative):
                self.counts['DUPLICATE_TRADE']+=1;return
            if symbol in self.books:
                frame=replace(frame,tick=current_book_tick(frame.tick,self.books[symbol],stamp,self.replay.cfg.stale_seconds))
            self.last_trade[symbol]=frame
            self._step(frame,False,evaluate=self.evaluate_each_trade)

    def _step(self,frame,retry,*,evaluate=True):
        if frame.available_at<self.replay.account.at:
            self.counts['OUT_OF_ORDER_AVAILABILITY_UNKNOWN']+=1;return
        self.authority.frame=frame
        order=self.replay.step(frame,quote_retry=retry,evaluate=evaluate)
        self.last_trade[frame.tick.symbol]=frame
        if order and order.get('side')=='BUY':
            context=self.authority.context.get(frame.tick.symbol,{})
            measured=context.get('input_evidence_basis') in {'ACTUAL_RECEIVED_LIVE_PIPELINE','MEASURED_PAIRED_TRADE_QUOTE'}
            self.counts['MEASURED_CONTEXT_PAPER_BUY_INTENTS' if measured else 'SYNTHETIC_PAPER_BUY_INTENTS']+=1
        self.counts['QUOTE_RETRY' if retry else 'INGESTED_TRADE']+=1

    def result(self):
        result=self.replay.result()
        result.update(observation_mode=self.basis,source_counts=dict(self.counts),
            configured_C_economics=configured_c_reward_bound(self.replay.cfg,self.replay.auto,self.replay.account.e.costs),
            evaluation_scope='EACH_TRADE_RESEARCH' if self.evaluate_each_trade else 'NATIVE_DECISION_TRANSITIONS_OR_LEGACY_SAMPLES',
            source_decision_completeness='NATIVE_TRANSITIONS_PRESENT_CHECK_LOSSES' if self.counts['INPUT:ENTRY_DECISION_EVIDENCE'] else 'UNKNOWN_LEGACY_SAMPLED_SOURCE',
            observed_authority_blocks=dict(self.authority.counts),recorder=self.capture.stats(),
            additional_broker_connections=0,actual_orders=False,
            source_watch_or_account_not_synthesized=True,
            measured_live_orderability='UNKNOWN_UNLESS_EVERY_OBSERVED_CONTEXT_GATE_IS_KNOWN')
        return json_values(result)

    def close(self):
        self.capture.close()
        # Preserve the isolated engine ledger before its temporary root ends.
        # This is never a copy of the user's real ledger.
        import shutil
        shutil.copyfile(self.replay.account.e.path,self.capture.folder/'paper-book-final.json')
        self.replay.close()
