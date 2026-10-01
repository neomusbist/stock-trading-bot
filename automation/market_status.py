"""공식 ka10100 state/auditInfo 원문만 판별. 미확인 상태는 정상으로 추정하지 않는다."""
import re

def assess_status(data):
    raw={k:data.get(k) for k in ('code','name','state','auditInfo')}
    text='|'.join(str(raw.get(k) or '') for k in ('state','auditInfo'))
    for word,reason in (('정리매매','SPECIAL_LIQUIDATION'),('상장폐지','SPECIAL_DELISTING'),
                        ('거래정지','TRADING_SUSPENDED'),('매매정지','TRADING_SUSPENDED'),
                        ('단일가','SPECIAL_NON_CONTINUOUS')):
        if word in text: return {'pass':False,'reason_code':reason,'raw':raw,'basis':'ka10100 literal state/auditInfo'}
    if not isinstance(raw['state'],str) or not raw['state'].strip():
        return {'pass':None,'reason_code':'MARKET_STATUS_UNKNOWN','raw':raw}
    known={'정상','담보대출','신용가능','관리종목','투자주의','투자경고','투자위험','투자주의환기종목'}
    tokens=[x.strip() for x in text.split('|') if x.strip()]
    if any(t not in known and not re.fullmatch(r'증거금\d+%',t) for t in tokens):
        return {'pass':None,'reason_code':'MARKET_STATUS_UNKNOWN','raw':raw}
    return {'pass':True,'reason_code':'STATUS_NO_KNOWN_SPECIAL_FLAG','raw':raw,
            'basis':'공식 원문의 확인 범위만 통과; 모든 특수상태를 보장하지 않음'}
