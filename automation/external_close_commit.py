"""Durable single-incident ledger commit; caller must hold official idle locks."""
import hashlib
import json
from pathlib import Path
from core.util import atomic_json
from core.event_log import append_jsonl

def commit_staged_close(*,service,state_path,backup_dir,original,staged,proof,stamp):
    import threading
    if getattr(service,'_idle_thread',None)!=threading.get_ident():
        raise ValueError('OFFICIAL_IDLE_LOCK_REQUIRED')
    state_path=Path(state_path);backup_dir=Path(backup_dir)
    current=json.loads(state_path.read_text(encoding='utf-8-sig'))
    if current!=original:raise ValueError('STATE_CHANGED_SINCE_RECONCILIATION')
    if proof.get('status')!='PROOF_ONLY_NOT_APPLIED':raise ValueError('BROKER_PROOF_REQUIRED')
    sym=proof['symbol'];oid=proof['order_id'];eid=proof['execution_id']
    if sym in staged['positions'] or staged['halt']!=original['halt']:
        raise ValueError('STAGED_STATE_UNSAFE')
    if staged['automation']['existing']!=original['automation']['existing']:
        raise ValueError('EXISTING_HOLDINGS_CHANGED')
    reviews=[r for r in staged.get('external_close_reviews',[]) if r.get('order_id')==oid and r.get('execution_id')==eid]
    if len(reviews)!=1 or len(staged['trades'])!=len(original['trades'])+1:
        raise ValueError('EXTERNAL_CLOSE_REVIEW_INVALID')
    if staged['trades'][-1].get('pnl_source')!='BROKER_QUERY':raise ValueError('BROKER_PNL_REQUIRED')
    backup_dir.mkdir(parents=True,exist_ok=False)
    atomic_json(backup_dir/'before.json',original)
    atomic_json(backup_dir/'after.json',staged)
    event={'time':stamp.isoformat(),'event':'BROKER_CONFIRMED_EXTERNAL_CLOSE',
           'symbol':sym,'order_id':oid,'execution_id':eid,'quantity':proof['quantity'],
           'price':proof['price'],'query_started_at':proof['account_query_started_at'],
           'backup_dir':str(backup_dir),'halt_preserved':True}
    atomic_json(backup_dir/'receipt.json',{**event,'status':'PREPARED'})
    try:
        atomic_json(state_path,staged)
        if json.loads(state_path.read_text(encoding='utf-8-sig'))!=staged:raise ValueError('READBACK_MISMATCH')
        append_jsonl(state_path.parent/'events.jsonl',event)
        digest=hashlib.sha256(state_path.read_bytes()).hexdigest()
        atomic_json(backup_dir/'receipt.json',{**event,'status':'APPLIED','state_sha256':digest})
    except Exception:
        atomic_json(state_path,original)
        atomic_json(backup_dir/'rollback.json',{**event,'status':'ROLLED_BACK'})
        raise
    return {'status':'APPLIED','state_sha256':digest,'receipt':str(backup_dir/'receipt.json')}
