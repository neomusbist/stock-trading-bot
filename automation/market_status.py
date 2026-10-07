"""공식 ka10100 state/auditInfo 원문만 판별. 미확인 상태는 정상으로 추정하지 않는다."""
import re

AFTER_INELIGIBLE_KEYWORDS = (
    ('거래정지', 'TRADING_SUSPENDED'),
    ('매매정지', 'TRADING_SUSPENDED'),
    ('정리매매', 'SPECIAL_LIQUIDATION'),
    ('상장폐지', 'SPECIAL_DELISTING'),
    ('관리종목', 'AFTER_INELIGIBLE_MANAGED'),
    ('투자경고', 'AFTER_INELIGIBLE_INVESTMENT_WARNING'),
    ('투자위험', 'AFTER_INELIGIBLE_INVESTMENT_RISK'),
    ('이상급등', 'AFTER_INELIGIBLE_ABNORMAL_SURGE'),
    ('초저유동', 'AFTER_INELIGIBLE_ULTRA_LOW_LIQUIDITY'),
    ('초저유동성', 'AFTER_INELIGIBLE_ULTRA_LOW_LIQUIDITY'),
    ('투자주의환기', 'AFTER_INELIGIBLE_INVESTMENT_CAUTION_ALERT'),
    ('투자주의환기종목', 'AFTER_INELIGIBLE_INVESTMENT_CAUTION_ALERT'),
    ('투자유의', 'AFTER_INELIGIBLE_INVESTMENT_CAUTION'),
    ('단일가', 'SPECIAL_NON_CONTINUOUS'),
)

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

def assess_after_status(data, today_volume=None):
    raw={k:data.get(k) for k in ('code','name','state','auditInfo')}
    text='|'.join(str(raw.get(k) or '') for k in ('state','auditInfo'))
    for word, reason in AFTER_INELIGIBLE_KEYWORDS:
        if word in text:
            return {'pass': False, 'reason_code': reason, 'raw': raw, 'basis': 'KRX/NXT AFTER eligibility rules'}
    if today_volume is not None and today_volume <= 0:
        return {'pass': False, 'reason_code': 'AFTER_INELIGIBLE_ZERO_REGULAR_VOLUME', 'raw': raw, 'basis': '당일 정규장 미거래 종목 AFTER 불가'}
    if data.get('is_etf_etn') or (raw.get('name') and any(etf_kw in str(raw['name']).upper() for etf_kw in ('KODEX','TIGER','KOSEF','KBSTAR','ARIRANG','ACE','SOL','PLUS','ETN'))):
        return {'pass': False, 'reason_code': 'AFTER_INELIGIBLE_ETF_ETN', 'raw': raw, 'basis': 'Kiwoom AFTER ETF/ETN 제외 정책'}
    return assess_status(data)
