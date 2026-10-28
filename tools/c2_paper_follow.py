"""Bounded C2 PAPER follower of existing capture journals; never connects or orders.

Research only: recorded context may be sampled/incomplete. This process does not
start the LIVE receiver; STOP with no producer yields no actual market data.
"""
import argparse
from collections import deque
import contextlib
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from automation.policy import AutomationConfig
from core.settings import Config, KST
from research.c2_validation import C2ObservedSession
from research.evidence_collector import JsonTail
from tools.benchmark_all_strategies import read_settings
from tools.observe_paper import set_idle_priority

MIN_FREE_BYTES = 32 * 1024**3


class SampledC2Session(C2ObservedSession):
    """Sample entry evaluation but still deliver all admitted frames for protection."""
    def __init__(self, *args, **kwargs):
        self.last_evaluation = {}
        super().__init__(*args, **kwargs)

    def _step(self, frame, retry, *, evaluate=True):
        if evaluate:
            symbol = frame.tick.symbol
            previous = self.last_evaluation.get(symbol)
            if previous is not None and (frame.available_at - previous).total_seconds() < 1:
                self.counts['C2_THROTTLED_EVALUATION_UNKNOWN'] += 1
                evaluate = False
            else:
                self.last_evaluation[symbol] = frame.available_at
        return super()._step(frame, retry, evaluate=evaluate)


def _write_json(path, value):
    # Output is always a newly-created owned directory, outside LIVE.
    # Windows readers may hold the prior status open. The existing helper
    # retries sharing violations and preserves atomic replacement; this is
    # diagnostic output, not an order or accounting ledger.
    from core.util import atomic_json
    atomic_json(path,value,fsync=False)


def follow(root, output, cfg, auto, cash, seconds, *, poll=1., cpu_budget_ms=50.):
    root = Path(root).resolve()
    output = Path(output).resolve()
    if output.exists() or output == root or root in output.parents:
        raise ValueError('OUTPUT_MUST_BE_NEW_AND_OUTSIDE_SOURCE')
    if not root.is_dir():
        raise ValueError('SOURCE_ROOT_REQUIRED')
    if not (1 <= seconds <= 28800) or not (0 < poll <= 10) or not (0 < cpu_budget_ms <= 100):
        raise ValueError('BOUNDED_OBSERVER_LIMITS_REQUIRED')
    output.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(output.parent).free < MIN_FREE_BYTES:
        raise ValueError('32GiB_FREE_SPACE_RESERVE_REQUIRED')
    start = datetime.now(KST)
    session = None
    result = None
    rows = 0
    budget_hits = 0
    pending = deque()
    tails = {}
    known_paths = set()
    references = {}
    reference_log = []
    reference_bytes = 0
    initial = True
    output.mkdir()
    try:
        _write_json(output/'STATUS.json', {'status':'STARTING', 'started_at':start.isoformat(),
                    'actual_orders':False, 'source_is_not_receiver':True})
        session = SampledC2Session(start, cfg, auto, cash, output/'evidence',
                                   basis='REALTIME_SIDECAR', evaluate_each_trade=True)
        def reference_snapshot(symbol):
            # Snapshot only when the sidecar actually sees the WATCH/sample.
            # A disk file observed now is not proof it was known earlier.
            nonlocal reference_bytes
            if not isinstance(symbol,str) or not symbol.isalnum() or len(symbol)>12:
                return
            from candidate_builder import bars_from_csv
            from core.util import shared_binary_reader
            for folder in ('data/raw','data/observation'):
                path = root/folder/(symbol+'_daily.csv')
                if not path.is_file():continue
                resolved=path.resolve()
                if root not in resolved.parents:
                    session.counts['REFERENCE_PATH_UNKNOWN'] += 1;return
                st=path.stat();signature=(str(resolved),st.st_mtime_ns,st.st_size)
                if references.get(symbol)==signature:return
                if st.st_size>1024*1024:
                    session.counts['REFERENCE_OVERSIZE_UNKNOWN'] += 1;return
                with shared_binary_reader(path) as stream:data=stream.read(1024*1024+1)
                sha=hashlib.sha256(data).hexdigest()
                target=output/'reference-inputs'/(path.stem+'.'+sha+'.csv')
                if not target.exists():
                    if reference_bytes+len(data)>64*1024**2 or shutil.disk_usage(output).free-len(data)<MIN_FREE_BYTES:
                        session.counts['REFERENCE_STORAGE_BUDGET_UNKNOWN'] += 1;return
                    target.parent.mkdir(exist_ok=True);target.write_bytes(data);reference_bytes+=len(data)
                try:bars=bars_from_csv(target,datetime.now(KST).date())[-cfg.history_bars:]
                except (ValueError,KeyError):
                    session.counts['REFERENCE_FORMAT_UNKNOWN'] += 1;return
                if len(bars)<cfg.min_history_bars:
                    session.counts['REFERENCE_INSUFFICIENT_UNKNOWN'] += 1;return
                observed=datetime.now(KST)
                session.feed(dict(event='DAILY_REFERENCE',symbol=symbol,bars=bars,
                                  known_at=observed.isoformat()),path.name,observed)
                references[symbol]=signature
                reference_log.append(dict(source=str(path.relative_to(root)),known_at=observed.isoformat(),
                    sha256=sha,snapshot=target.name,bars=len(bars),
                    basis='SIDECAR_OBSERVED_DISK_REFERENCE_NOT_HISTORICAL_REST_CACHE_CERTIFICATION'))
                return
            session.counts['REFERENCE_SOURCE_MISSING_UNKNOWN'] += 1
        deadline = time.monotonic() + seconds
        next_status = 0.
        while time.monotonic() < deadline and not session.capture.storage_blocked:
            now = time.monotonic()
            if now >= next_status:
                if shutil.disk_usage(output).free < MIN_FREE_BYTES:
                    session.counts['OBSERVER_FREE_SPACE_LIMIT_UNKNOWN'] += 1
                    break
                _write_json(output/'STATUS.json', {'status':'RUNNING','started_at':start.isoformat(),
                    'checked_at':datetime.now(KST).isoformat(),'rows':rows, 'pending_rows':len(pending),
                    'budget_hits':budget_hits,'actual_orders':False,'source_is_not_receiver':True,
                    'capture_losses':session.capture.stats().get('capture_losses'),
                    'opportunity_completeness':'UNKNOWN_DUE_TO_SAMPLING_AND_SOURCE_GAPS'})
                next_status = now + 30.
            paths = sorted((root/'runtime/automation-production/trade-review').glob(
                'trade-review-'+start.strftime('%Y%m%d')+'-*.jsonl'))
            current = paths[-2:]
            # More than two rotations between polls would otherwise silently
            # omit a whole new file. Existing history is intentionally not read.
            if not initial:
                for skipped in set(paths[:-2]) - known_paths:
                    try:
                        missing = max(1, skipped.stat().st_size)
                    except OSError:
                        missing = 1
                    session.counts['OBSERVER_UNSEEN_ROTATION_GAP_BYTES_UNKNOWN'] += missing
                    for sym in session.last_trade:
                        session.replay.detector.mark_coverage_gap(sym, datetime.now(KST), 'OBSERVER_UNSEEN_ROTATION')
            known_paths.update(paths)
            # A previously followed file can fall outside the two-file scan
            # before its buffered bytes are consumed. Count that exact loss;
            # never silently drop it merely because the file itself was known.
            current_set = set(current)
            for old in list(tails):
                if old in current_set:
                    continue
                tail = tails.pop(old)
                try:
                    unread = max(0, old.stat().st_size-tail.offset)
                except OSError:
                    unread = 1
                if unread:
                    session.counts['OBSERVER_ROTATION_GAP_BYTES_UNKNOWN'] += unread
                    for sym in session.last_trade:
                        session.replay.detector.mark_coverage_gap(sym, datetime.now(KST), 'OBSERVER_ROTATION_BACKLOG_GAP')
            began = time.process_time()
            for path in current:
                if path not in tails:
                    tails[path] = JsonTail(path, start_at_end=initial, with_provenance=True)
                if len(pending) < 128:
                    pending.extend((path.name, row) for row in tails[path].read(max_rows=128-len(pending)))
            while pending:
                name, row = pending.popleft()
                session.feed(row, name, datetime.now(KST))
                rows += 1
                if row.get('event') in {'WATCH_METADATA','OPPORTUNITY_SAMPLE'}:
                    reference_snapshot(row.get('symbol'))
                if (time.process_time()-began)*1000 >= cpu_budget_ms:
                    budget_hits += 1
                    break
            initial = False
            time.sleep(min(poll, max(0., deadline-time.monotonic())))
        result = session.result()
        result.update(observer_rows=rows, observer_pending_rows=len(pending),
            observer_cpu_budget_hits=budget_hits, cpu_budget_ms_per_poll=cpu_budget_ms,
            poll_seconds=poll, tail_counters={p.name:dict(t.stats) for p,t in tails.items()},
            actual_orders=False, live_activation_allowed=False, additional_broker_connections=0,
            source_is_not_receiver=True, market_capacity_certified=False,
            opportunity_completeness='UNKNOWN_DUE_TO_SAMPLING_AND_SOURCE_GAPS',
            measured_live_orderability='UNKNOWN_UNLESS_EVERY_OBSERVED_CONTEXT_GATE_IS_KNOWN')
        result['reference_observations'] = reference_log
    except BaseException as exc:
        _write_json(output/'STATUS.json', {'status':'FAILED','exception_type':type(exc).__name__,
            'checked_at':datetime.now(KST).isoformat(),'actual_orders':False,'rows':rows})
        raise
    finally:
        if session is not None:
            session.close()
            if result is not None:
                result['recorder_after_close'] = session.capture.stats()
    _write_json(output/'RESULT.json', result)
    _write_json(output/'STATUS.json', {'status':'FINISHED','checked_at':datetime.now(KST).isoformat(),
        'actual_orders':False,'rows':rows,'opportunity_completeness':'UNKNOWN_DUE_TO_SAMPLING_AND_SOURCE_GAPS'})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--follow-root',type=Path,required=True)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--automation',type=Path,required=True)
    parser.add_argument('--initial-cash',type=float,required=True,help='Explicit hypothetical PAPER cash, not a broker balance')
    parser.add_argument('--day',required=True)
    parser.add_argument('--seconds',type=int,default=120)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    for source in (args.config.resolve(),args.automation.resolve()):
        if output == source or source in output.parents:
            parser.error('output must not overwrite inputs')
    if args.day != datetime.now(KST).date().isoformat():
        parser.error('follow day must be today')
    cfg = read_settings(args.config, Config)
    auto = read_settings(args.automation, AutomationConfig)
    set_idle_priority()
    # Do not mirror per-event engine diagnostics into an unbounded console log.
    # The bounded sidecar evidence writer and critical PAPER ledger remain active.
    with open(os.devnull,'w') as quiet,contextlib.redirect_stdout(quiet):
        result = follow(args.follow_root,output,cfg,auto,args.initial_cash,args.seconds)
    print(json.dumps({'output':str(output),'observer_rows':result['observer_rows'],
                      'actual_orders':False,'live_activation_allowed':False}))


if __name__ == '__main__':
    main()
