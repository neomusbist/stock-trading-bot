"""주기별 고유 종목의 최신 판단 집계. 기록은 기존 events와 작은 JSON만 사용한다."""
from collections import Counter
from automation.decisions import STRATEGY_VERSION
from automation.entry_funnel import EntryFunnel

class CandidateAudit:
    def __init__(self):
        self.rows={}; self.evaluations={}; self.started=None; self.context={}
        self.funnel=EntryFunnel();self.funnel_errors=0

    def begin(self,rows,stamp,context):
        self.rows={r['code']:dict(r) for r in rows}; self.evaluations={}
        self.started=stamp; self.context=dict(context)
        try:
            self.funnel.rows(rows,stamp,'DISCOVERY_REVIEWED')
            self.funnel.rows([r for r in rows if r.get('status') in {'WATCH','ELIGIBLE','RESERVE','SHADOW'}],stamp,'QUALIFIED')
            self.funnel.rows([r for r in rows if r.get('status')=='WATCH'],stamp,'WATCH_AT_SCAN')
        except Exception:self.funnel_errors+=1

    def observe(self,symbol,reason,gates,decision,metadata=None):
        meta=metadata or {}
        try:
            self.funnel.boundary(symbol,meta.get('timestamp') or self.started,reason,gates,
                                 {'pattern':meta.get('signal_pattern')})
        except Exception:self.funnel_errors+=1
        if symbol in self.rows:
            self.evaluations[symbol]={'primary_reason_code':reason,'gates':gates,'decision':decision,**(metadata or {})}

    def record_rows(self,rows,stamp,stage):
        try:self.funnel.rows(rows,stamp,stage)
        except Exception:self.funnel_errors+=1

    def funnel_snapshot(self,stamp):
        try:return self.funnel.snapshot(stamp)
        except Exception:
            self.funnel_errors+=1
            return {'schema':'ENTRY_FUNNEL_V1','status':'UNKNOWN_DIAGNOSTIC_ERROR','errors':self.funnel_errors}

    def summary(self,stamp,context=None):
        counts=Counter(); reasons=Counter(); candidates=[]
        for sym,row in self.rows.items():
            a=row.get('affordability',{}); eligible=a.get('entry_eligible')
            if eligible is True: counts['affordable']+=1
            elif a.get('reason_code')=='ONE_SHARE_UNAFFORDABLE': counts['unaffordable']+=1
            else: counts['affordability_unknown_or_observation']+=1
            evaluation=self.evaluations.get(sym); gates=(evaluation or {}).get('gates',{})
            reason=(evaluation or {}).get('primary_reason_code') or row.get('reason') or 'NOT_YET_EVALUATED'
            reasons[reason]+=1
            failed={k for k,v in gates.items() if v['result']=='FAIL' and v.get('required',True)}
            raw_reason=row.get('reason','')
            if 'liquidity' in failed or '거래대금 부족' in raw_reason: counts['liquidity_fail']+=1
            if 'spread' in failed: counts['spread_fail']+=1
            if failed & {'structure','invalidation','resistance','risk_reward'} or row.get('status')=='REJECT' and any(s in raw_reason for s in ('상단','저점 이탈','가격폭')): counts['structure_fail']+=1
            if failed & {'warmup','pattern','breakout','chase','acceleration','economic'}: counts['setup_not_ready']+=1
            sizing=gates.get('risk_quantity',{}).get('values',{})
            if sizing.get('risk_quantity')==0 and sizing.get('risk_per_share',0)>0: counts['risk_zero_qty']+=1
            if evaluation and gates and all(v['result']=='PASS' for v in gates.values() if v.get('required',True)): counts['final_entry_eligible']+=1
            if evaluation: counts['evaluated']+=1
            if row.get('status')=='WATCH':counts['watch']+=1
            if any(t.get('candidate_generated',bool(t.get('gates'))) for t in (evaluation or {}).get('setup_trials') or []):counts['setup_candidates']+=1
            if 'risk_quantity' in gates:counts['risk_reached']+=1
            candidates.append({'symbol':sym,'name':row.get('name'),'discovery_status':row.get('status'),
                'current_price':(evaluation or {}).get('current_price',row.get('reference_price')),'source_count':row.get('source_count'),
                'timestamp':(evaluation or {}).get('timestamp',row.get('observed_at')),'setup_trials':(evaluation or {}).get('setup_trials'),
                'primary_reason_code':reason,'evaluation_observed':bool(evaluation),
                'fallback_source1':bool(row.get('fallback_source1')),'watch_type':row.get('watch_type'),
                'discovery_context':row.get('discovery_context'),
                'market_context':next((t.get('context') for t in (evaluation or {}).get('setup_trials') or [] if t.get('context')),None),
                'gate_results':{k:v['result'] for k,v in gates.items()},'sizing':sizing,'affordability':a})
        metrics={k:counts[k] for k in ('affordable','unaffordable','affordability_unknown_or_observation',
                 'liquidity_fail','spread_fail','structure_fail','setup_not_ready','risk_zero_qty','final_entry_eligible','evaluated','watch','setup_candidates','risk_reached')}
        return {'timestamp':stamp,'strategy_version':STRATEGY_VERSION,'cycle_started':self.started,
                'capital_at_discovery':self.context,'capital_now':dict(context or self.context),
                'effective_auto_cap':(context or self.context).get('effective_auto_cap'),
                'raw_candidates':len(self.rows),'not_yet_evaluated':len(self.rows)-counts['evaluated'],
                **metrics,'shadow':getattr(self,'shadow_count',0),'refresh_seconds':getattr(self,'refresh_seconds',60),
                'raw_definition':'5개 ranking API의 일반주 고유종목 합집합; HTS 0168과 동일 목록은 아님',
                'reason_counts':dict(reasons),'candidates':candidates,
                'entry_funnel':self.funnel_snapshot(stamp),
                'count_basis':'주기 내 고유종목 최신판단; gate 탈락수는 중복 가능; 미평가를 통과로 세지 않음'}
