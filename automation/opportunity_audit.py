"""Pure unique-pattern audit. Repeated evaluations are never counted as trades."""
from collections import Counter,defaultdict
from datetime import datetime


def at(row):
    value=row.get('asof') or row.get('timestamp') or row.get('time')
    try:
        value=datetime.fromisoformat(value) if isinstance(value,str) else value
        return value if isinstance(value,datetime) and value.tzinfo is not None else None
    except (TypeError,ValueError):return None


def audit_records(records,cutoff):
    if not isinstance(cutoff,datetime) or cutoff.tzinfo is None:raise ValueError('AWARE_CUTOFF_REQUIRED')
    episodes={};symbols=set();reasons=Counter();unknown_time=0;future=0;samples=0
    for row in records:
        stamp=at(row)
        if stamp is None:unknown_time+=1;continue
        if stamp>cutoff:future+=1;continue
        if stamp.date()!=cutoff.date():continue
        symbol=row.get('symbol')
        if not symbol:continue
        symbols.add(symbol);samples+=1
        fast=(row.get('details') or {}).get('fast_evaluation') or row.get('fast') or {}
        if fast.get('reason'):reasons['FAST_'+str(fast['reason'])]+=1
        trials=row.get('setup_trials') or row.get('trials') or []
        for trial in trials:
            setup=trial.get('setup');geometry=trial.get('setup_geometry') or {}
            formed=trial.get('candidate_generated',trial.get('formed')) is True
            if not formed:continue
            # The same observed support/range is one episode across repeated
            # prints/minutes. Repriced orders and parameter trials add no trade.
            identity=(symbol,setup,geometry.get('breakout'),geometry.get('support'),
                      geometry.get('local_range_start') or geometry.get('impulse_start_at'))
            if identity[2:]==(None,None,None):
                identity+=(trial.get('entry',trial.get('virtual_entry')),trial.get('stop',trial.get('virtual_invalidation')))
            item=episodes.setdefault(identity,{'symbol':symbol,'setup':setup,'first_at':stamp.isoformat(),
                'last_at':stamp.isoformat(),'ready':False,'failure_sets':set(),'geometry':geometry})
            item['last_at']=max(item['last_at'],stamp.isoformat())
            item['first_at']=min(item['first_at'],stamp.isoformat())
            item['ready']|=trial.get('entry_ready',trial.get('ready')) is True
            failed=trial.get('failed_reasons',trial.get('failures'))
            if failed is None:
                failed=[key.upper()+'_FAIL' for key,v in trial.get('gates',{}).items()
                        if v.get('required',True) and v.get('result')!='PASS']
            item['failure_sets'].add(tuple(sorted(set(failed))))
    by_setup={}
    for setup in sorted({value['setup'] for value in episodes.values()},key=str):
        subset=[value for value in episodes.values() if value['setup']==setup]
        failures=Counter();sole=Counter()
        for item in subset:
            failures.update({r for group in item['failure_sets'] for r in group})
            sole.update({group[0] for group in item['failure_sets'] if len(group)==1})
        by_setup[setup]={'unique_symbols':len({item['symbol'] for item in subset}),
            'unique_geometry_episodes':len(subset),'ready_geometry_episodes':sum(item['ready'] for item in subset),
            'failure_episode_counts':dict(failures.most_common()),'sole_blocker_episode_counts':dict(sole.most_common())}
    return {'cutoff':cutoff.isoformat(),'unique_symbols':len(symbols),'evaluations_observed':samples,
        'unknown_timestamp_records':unknown_time,'future_records_excluded':future,'patterns':by_setup,
        'fast_rejection_evaluations':dict(reasons.most_common()),
        'actual_market_opportunities':None,'actual_completed_trades':None,'net_expectancy':None,
        'profitability_proven':False,'orders_enabled':False,
        'count_basis':'Observed unique price geometries; correlated patterns are not independent opportunities',
        'coverage':'INPUT_RECORDS_ONLY_MISSING_SYMBOLS_AND_INTERVALS_UNKNOWN'}
