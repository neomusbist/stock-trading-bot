"""Stopped-runner external-close reconciliation. Defaults to dry run.

Requires matching broker REST and recorded websocket evidence, complete broker
pagination, official process locks, unchanged policy and a flat broker account
apart from the protected manual holdings. Never sends or cancels an order.
"""
import argparse,copy,json,tempfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from core.settings import load_config,now
from core.broker import Rest
from core.util import ProcessLock
from automation.policy import Policy,load_automation,HARD_LOCKS
from automation.engine import AutonomousEngine
from automation.broker_pnl import settle_broker_pnl
from automation.external_close_proof import validate_maintenance_shutdown
from automation.external_close_commit import commit_staged_close
from automation.release_integrity import check_runtime_integrity
from operator_service import OperatorService
import main

def run(root,backup,symbol,order_id,execution_id,apply=False):
    root=Path(root).resolve();backup=Path(backup).resolve()
    if backup==root or root in backup.parents:raise ValueError('BACKUP_MUST_BE_EXTERNAL')
    # Do not combine a DEV engine or credentials loader with a LIVE ledger.
    import core.engine,automation.engine
    if (Path(main.ROOT).resolve()!=root or Path(core.engine.__file__).resolve()!=root/'core/engine.py'
            or Path(automation.engine.__file__).resolve()!=root/'automation/engine.py'):
        raise ValueError('INSTALLED_CODE_ROOT_MISMATCH')
    svc=OperatorService(root)
    if svc.running():raise ValueError('RUNNER_ACTIVE')
    with svc.idle(),ProcessLock(root/'runtime/control.lock'):
        integrity=check_runtime_integrity(root)
        if not integrity.get('valid') or not integrity.get('manifest_verified'):raise ValueError('LIVE_INTEGRITY_FAILED')
        cfg=load_config(root/'config.json');auto=load_automation(root/'automation.json')
        cfg=replace(cfg,roundtrip_cost_fraction=auto.commission_buy+auto.commission_sell+auto.sell_tax)
        state_path=root/'runtime'/('automation-'+cfg.broker_environment)/'state.json'
        original=json.loads(state_path.read_text(encoding='utf-8-sig'))
        policy_bytes=(root/'runtime/automation_policy.json').read_bytes()
        policy_obj=json.loads(policy_bytes.decode('utf-8-sig'))
        if policy_obj['mode']!='STOP' or policy_obj.get('releases') or not {'005930','017900','052260'}.issubset(HARD_LOCKS):
            raise ValueError('STOP_AND_HARD_LOCK_REQUIRED')
        main.load_credentials();rest=Rest(replace(cfg,live_mode=False));started=now()
        snapshot=rest.snapshot();fills=rest.fills()
        snapshot.update(account_identity=rest.identity,query_started_at=started.isoformat(),fills=fills,
            holdings_complete=True,orders_complete=True,fills_complete=True)
        proof=validate_maintenance_shutdown(original,snapshot,now(),symbol=symbol,order_id=order_id,
            execution_id=execution_id,policy_mode='STOP')['proof']
        class NoOrders:
            identity=rest.identity
            def submit(self,*args,**kwargs):raise RuntimeError('ORDER_FORBIDDEN')
            def cancel(self,*args,**kwargs):raise RuntimeError('ORDER_FORBIDDEN')
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder);(folder/'state.json').write_text(json.dumps(original,ensure_ascii=False),encoding='utf8')
            (folder/'policy.json').write_bytes(policy_bytes)
            fill_time=datetime.fromisoformat(proof['observed_at'])
            engine=AutonomousEngine(cfg,auto,Policy(folder/'policy.json'),folder,NoOrders(),lambda:fill_time)
            engine.s=copy.deepcopy(original)
            local='EXTERNAL-'+order_id+'-'+execution_id
            if local in engine.s['orders']:raise ValueError('DUPLICATE_LOCAL_ID')
            order={'local':local,'id':order_id,'symbol':symbol,'side':'SELL','qty':proof['quantity'],
                'limit':proof['price'],'filled':0,'amount':0.,'status':'ACCEPTED','created':proof['observed_at'],
                'signal':{'ownership':'USER_EXTERNAL'},'cancel_id':'','session':'UNKNOWN','venue':'UNKNOWN',
                'execution':'BROKER_EXTERNAL'}
            engine.s['orders'][local]=order
            engine.cumulative_fill(order,proof['quantity'],proof['quantity']*proof['price'])
            if symbol in engine.s['positions'] or order['status']!='FILLED':raise ValueError('STAGED_FILL_FAILED')
            review={'source':'BROKER_CONFIRMED_USER_EXTERNAL_SELL','local':local,'symbol':symbol,'order_id':order_id,
                'execution_id':execution_id,'quantity':proof['quantity'],'price':proof['price'],
                'observed_at':proof['observed_at'],'account_query_started_at':proof['account_query_started_at']}
            engine.s.setdefault('external_close_reviews',[]).append(review)
            engine.s['trades'][-1]['external_close_review']=review
            report=settle_broker_pnl(engine.s,fills,engine.costs,now(),rest.identity)
            if report['unknown'] or engine.s['trades'][-1].get('pnl_source')!='BROKER_QUERY':raise ValueError('BROKER_PNL_UNVERIFIED')
            if (engine.s['automation']['existing']!=original['automation']['existing'] or engine.s['halt']!=original['halt']
                    or len(engine.s['trades'])!=len(original['trades'])+1):raise ValueError('UNEXPECTED_STATE_CHANGE')
            if (root/'runtime/automation_policy.json').read_bytes()!=policy_bytes:raise ValueError('POLICY_CHANGED')
            if (now()-started).total_seconds()>15:raise ValueError('BROKER_SNAPSHOT_EXPIRED')
            result={'status':'DRY_RUN_ONLY','symbol':symbol,'order_id':order_id,'execution_id':execution_id}
            if apply:
                result=commit_staged_close(service=svc,state_path=state_path,backup_dir=backup,original=original,
                    staged=engine.s,proof=proof,stamp=now())
            return result

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for name in ('root','backup','symbol','order-id','execution-id'):parser.add_argument('--'+name,required=True)
    parser.add_argument('--apply',action='store_true',help='commit reviewed broker-confirmed external fill')
    args=parser.parse_args()
    print(json.dumps(run(args.root,args.backup,args.symbol,args.order_id,args.execution_id,args.apply),ensure_ascii=False))
