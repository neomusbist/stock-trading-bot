"""Fail-closed evidence check for an operator-requested external-close stop."""
from datetime import datetime
from automation.external_close_proof import validate_maintenance_shutdown

def external_close_stop_proof(state,policy_mode,request,snapshot,identity,now):
    if not isinstance(request,dict) or request.get('kind')!='REVIEW_EXTERNAL_CLOSE':
        raise ValueError('MAINTENANCE_REQUEST_INVALID')
    try: requested=datetime.fromisoformat(request['requested_at'])
    except (KeyError,ValueError,TypeError):raise ValueError('MAINTENANCE_TIME_UNKNOWN') from None
    if not requested.tzinfo or not now.tzinfo or not 0<=(now-requested).total_seconds()<=60:
        raise ValueError('MAINTENANCE_REQUEST_STALE')
    snap=dict(snapshot);snap['account_identity']=identity
    if datetime.fromisoformat(snap['query_started_at'])<=requested:
        raise ValueError('BROKER_QUERY_MUST_FOLLOW_REQUEST')
    return validate_maintenance_shutdown(state,snap,now,symbol=request['symbol'],
        order_id=request['order_id'],execution_id=request['execution_id'],policy_mode=policy_mode)
