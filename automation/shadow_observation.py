"""주문 객체/브로커를 받지 않는 독립 진단 관찰기. 가상 평가/신호를 실제 주문에 전달하지 않는다."""
from datetime import datetime
from core.util import floor_tick
from core.util import tick_size
from automation.patterns import PatternDetector
from automation.policy import HARD_LOCKS,BREAKEVEN_EXITS,TEST_RELEASE_SYMBOL
from automation.decisions import STRATEGY_VERSION
from automation.focus_selection import calculate_tier, TIER_NAMES

def make_evaluation_view(row):
    """구조 view + 당시 원본 row의 메타데이터를 명시적으로 병합한 EvaluationView."""
    view = dict(row)
    shadow_view = row.get('shadow_view', {})
    view.update(shadow_view)
    view['code'] = row.get('code')
    view['name'] = row.get('name')
    view['source_count'] = row.get('source_count', len(row.get('source_details', {})))
    view['source_details'] = dict(row.get('source_details', {}))
    view['source_deltas'] = dict(row.get('source_deltas', {}))
    view['observed_at'] = row.get('observed_at')
    view['fallback_source1'] = bool(row.get('fallback_source1'))
    view['status'] = 'WATCH' if view.get('status') in {'ELIGIBLE', 'WATCH'} else view.get('status')
    if 'resistance_room_pass' in shadow_view:
        view['resistance_room_pass'] = shadow_view['resistance_room_pass']
    elif 'resistance_room_pass' in row:
        view['resistance_room_pass'] = row['resistance_room_pass']
    else:
        view['resistance_room_pass'] = True
    return view

class ShadowObserver:
 def __init__(self,cfg,auto,costs,emit,risk=None,limit=32):
  self.cfg=cfg;self.auto=auto;self.detector=PatternDetector(cfg,auto,costs);self.emit=emit;self.risk=risk;self.limit=limit;self.rows={};self.first={};self.last_log={};self.previous={}
  self.capital={};self.epoch=None;self.eval_seconds=2;self.selected_at={};self.latest={};self.detached=set()
  self.last_tier={};self.observation_seq=0
 def event(self,kind,stamp,**data):
  if kind=='SHADOW_EVAL':self.latest[data['symbol']]={'timestamp':stamp.isoformat(),**data}
  data.setdefault('SHADOW_ONLY',True)
  self.emit(kind,timestamp=stamp.isoformat(),strategy_version=STRATEGY_VERSION,**data)
 def update(self,rows,stamp):
  if isinstance(stamp,str):stamp=datetime.fromisoformat(stamp)
  if self.epoch!=stamp.date():
   self.detector.reset();self.first={};self.previous={};self.rows={};self.last_log={};self.selected_at={};self.latest={};self.detached=set();self.epoch=stamp.date()
  for sym in list(self.detached):
   if not self.detector.observation_fresh(sym,stamp):
    self.detector.forget(sym);self.detached.discard(sym)
    self.event('SHADOW_DATA_RESET',stamp,symbol=sym,data_action='RESET_STALE_GAP')
  eligible=[]
  for row in rows:
   sym=row['code']
   if row.get('affordability',{}).get('entry_eligible') is not True or sym in HARD_LOCKS or sym in BREAKEVEN_EXITS or sym==TEST_RELEASE_SYMBOL:continue
   if sym not in self.first:
    self.first[sym]={'first_raw_at':row.get('first_raw_at',stamp.isoformat()),'first_raw_price':row.get('first_raw_price',row.get('reference_price'))}
    self.event('SHADOW_RAW_DISCOVERED',stamp,symbol=sym,name=row.get('name'),**self.first[sym])
   if row.get('source_count',0)>=1 and row.get('shadow_view',row).get('market_status',{}).get('pass') is True:eligible.append(row)
  # 가장 긴 C 형상의 완성봉 준비시간을 확보한다. 더 강한 새 발견/WATCH는 먼저 수용한다.
  residence_seconds=max(180,(max(4,self.auto.box_min_bars)+1)*60)
  resident_scores=[r.get('discovery_score',r.get('source_count',0)*10) for r in self.rows.values() if r.get('status')!='WATCH']
  weakest=min(resident_scores,default=float('inf'))
  def priority(row):
   sym=row['code'];score=row.get('discovery_score',row.get('source_count',0)*10)
   held=sym in self.rows and (stamp-self.selected_at.get(sym,stamp)).total_seconds()<residence_seconds
   new_stronger=sym not in self.rows and score>weakest
   gates=self.latest.get(sym,{}).get('gates',{})
   try:fresh=0<=(stamp-datetime.fromisoformat(self.latest[sym]['timestamp'])).total_seconds()<=15
   except (KeyError,ValueError):fresh=False
   strong=fresh and all(gates.get(k,{}).get('result')=='PASS' for k in ('acceleration','liquidity'))
   return (0 if row.get('status')=='WATCH' else 1 if strong or new_stronger else 2 if held else 3 if sym not in self.rows else 4,-score,sym)
  eligible.sort(key=priority)
  selected={r['code']:r for r in eligible[:self.limit]}
  for row in eligible[self.limit:]:self.event('SHADOW_CAPACITY',stamp,symbol=row['code'],reason='DIAGNOSTIC_LIMIT',limit=self.limit)
  for sym,row in selected.items():
   if sym in self.detached:
    if not self.detector.observation_fresh(sym,stamp):self.detector.forget(sym)
    self.detached.discard(sym)
   self.selected_at.setdefault(sym,stamp)
   prior=self.previous.get(sym);first_start=self.first[sym].setdefault('first_shadow_at',stamp.isoformat())
   if sym not in self.rows or not prior or (prior.get('source_count'),prior.get('status'))!=(row.get('source_count'),row.get('status')):
    last=self.detector.last.get(sym);fresh=bool(last and (stamp-last.time).total_seconds()<=self.cfg.stale_seconds)
    self.event('SHADOW_TRANSITION',stamp,symbol=sym,name=row.get('name'),**self.first[sym],from_status=(prior or {}).get('status'),to_status=row.get('status'),source_before=(prior or {}).get('source_count'),source_count=row.get('source_count'),reason=row.get('reason'),data_action='RETAIN_CONTINUOUS' if last and self.detector.continuous_observation else 'RETAIN_FRESH' if fresh else 'NO_DATA' if last is None else 'STALE_PENDING_RESET',live_detector_unchanged=True)
  for sym in self.rows.keys()-selected.keys():
   self.event('SHADOW_ENDED',stamp,symbol=sym,reason='INELIGIBLE_OR_CAPACITY',data_action='RETAIN_UNTIL_STALE_GAP')
   self.detached.add(sym);self.selected_at.pop(sym,None);self.last_log.pop(sym,None);self.latest.pop(sym,None)
  self.rows=selected;self.previous.update(selected)
 def tick(self,t,received):
  row=self.rows.get(t.symbol)
  if not row:return
  old=self.detector.last.get(t.symbol);reset=bool(old and (t.time-old.time).total_seconds()>self.cfg.stale_seconds)
  if not self.detector.ingest(t,received):return
  self.first[t.symbol].setdefault('first_tick_at',received.isoformat())
  if reset and not self.detector.continuous_observation:self.event('SHADOW_DATA_RESET',received,symbol=t.symbol,reason='STALE_TICK_GAP',gap_seconds=(t.time-old.time).total_seconds())
  previous=self.last_log.get(t.symbol)
  if previous and (received-previous[0]).total_seconds()<self.eval_seconds:return
  view=make_evaluation_view(row)
  sig=self.detector.signal(t,view);gates=self.detector.evaluation_gates(t,view)
  if sig:
   sig['SHADOW_ONLY']=True;sig['actual_order_permission']=False;sig['diagnostic_only']=True
  history=list(self.detector.history.get(t.symbol,[]));usable=[]
  for bar in reversed(history):
   if bar['first_second']>self.cfg.stale_seconds:break
   usable.append(bar)
  usable=list(reversed(usable));d=self.detector
  candidates={'B':d.pullback(usable),'C':d.box(usable),'A':None}
  prior=d.prior_high.get(t.symbol)
  if prior and t.price>prior and len(usable)>=2:candidates['A']={'pattern':'A','breakout':prior,'support':min(b['low'] for b in usable[-2:]),'projection':prior+2*(prior-min(b['low'] for b in usable[-2:]))}
  trials=[]
  for name in ['A','B','C']:
   setup=candidates[name];why=[]
   if setup is None:why=['INSUFFICIENT_USABLE_BARS' if len(usable)<(4 if name=='B' else self.auto.box_min_bars if name=='C' else 2) else 'PATTERN_GEOMETRY_OR_VOLUME_NOT_MET' if name!='A' else 'PRIOR_HIGH_NOT_BROKEN']
   trial=d.setup_gates(t,view,setup) if setup else {}
   failed=[k for k,v in trial.items() if v['result']!='PASS'];pre=[k for k in ['warmup','acceleration','liquidity','spread','structure','market_status'] if gates[k]['result']!='PASS']
   virtual={'limit':floor_tick(min(t.ask,setup['breakout']*(1+self.cfg.max_chase))),'stop':floor_tick(setup['support']*(1-self.cfg.structural_buffer))} if setup else None
   sizing=self.risk({'symbol':t.symbol,**virtual}) if virtual and self.risk else self.virtual_size(virtual) if virtual else None
   trials.append({'setup':name,'candidate_generated':bool(setup),'failed_reasons':why+pre+failed,'gates':trial,'virtual_entry':virtual['limit'] if virtual else None,'virtual_invalidation':virtual['stop'] if virtual else None,'invalidation_ticks':(virtual['limit']-virtual['stop'])/tick_size(t.price) if virtual else None,'virtual_risk_quantity':sizing,'risk_basis':'DIAGNOSTIC_SIZING_ONLY_NOT_ORDER_AUTHORIZATION'})
  self.observation_seq+=1
  current_tier=calculate_tier(gates,trials,row)
  prior_tier=self.last_tier.get(t.symbol)
  if prior_tier!=current_tier:
   self.last_tier[t.symbol]=current_tier
   self.event('OBSERVATION_READINESS_CHANGED',received,symbol=t.symbol,
              tier=current_tier,prior_tier=prior_tier,readiness_kind=TIER_NAMES.get(current_tier,'TIER_CHANGED'),
              observation_seq=self.observation_seq,source_count=row.get('source_count'),
              usable_completed_bars=len(usable),stored_completed_bars=len(history),
              SHADOW_ONLY=True,actual_order_permission=False,diagnostic_only=True)
  signature=repr(([(k,v['result']) for k,v in gates.items()],[(x['setup'],x['candidate_generated'],x['failed_reasons']) for x in trials]))
  previous=self.last_log.get(t.symbol)
  if previous and previous[1]==signature and (received-previous[0]).total_seconds()<10:return
  self.last_log[t.symbol]=(received,signature)
  self.event('SHADOW_EVAL',received,symbol=t.symbol,name=row.get('name'),**self.first[t.symbol],source_count=row.get('source_count'),discovery_status=row.get('status'),current_price=t.price,usable_completed_bars=len(usable),stored_completed_bars=len(history),gates=gates,setup_trials=trials,virtual_signal=sig,actual_order_permission=False,
    microstructure={'tick_size':tick_size(t.price),'tick_fraction':tick_size(t.price)/t.price,'spread_fraction':(t.ask-t.bid)/t.price,'last_trade_gap_seconds':(t.time-old.time).total_seconds() if old else None,
      'book_depth':None,'book_gap':None,'vi_state':None,'slippage_basis':'CONFIG_ESTIMATE_NOT_MEASURED','entry_slippage_fraction':self.cfg.entry_slippage_fraction,'exit_slippage_fraction':self.cfg.exit_slippage_fraction})
 def virtual_size(self,virtual):
  entry=virtual['limit'];stop=virtual['stop'];capital=self.capital
  budget=max(0,capital.get('entry_budget',0));risk=max(0,min(capital.get('risk_equity',capital.get('effective_auto_cap',0))*self.cfg.risk_fraction,capital.get('risk_remaining',float('inf'))))
  unit=entry-stop+self.detector.costs.roundtrip(entry,stop)+stop*self.cfg.exit_slippage_fraction
  quantity=max(0,min(int(budget//(entry*(1+self.auto.commission_buy))),int(risk//unit))) if 0<stop<entry and unit>0 and capital.get('account_fresh') else 0
  return {'quantity':quantity,'risk_budget':risk,'risk_per_share':unit,'entry_budget':budget,'account_fresh':capital.get('account_fresh',False),'diagnostic_only':True,'SHADOW_ONLY':True,'actual_order_permission':False}
