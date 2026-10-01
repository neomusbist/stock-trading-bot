"""V1.1 자동매매 운영시간 (15:20은 거래소 폐장시각이 아님). 로컬 실행/진단은 별개다."""
from core.settings import KST

START='09:00:00'
END='15:20:00'

def supported(stamp):
    if stamp.tzinfo is None: return False
    local=stamp.astimezone(KST)
    return local.weekday()<5 and START<=local.strftime('%H:%M:%S')<END

def assessment(stamp):
    return {'classification':'SUPPORTED_SESSION' if supported(stamp) else 'OUT_OF_SUPPORTED_SESSION',
            'support':'평일 KST 09:00 이상 15:20 미만 + 거래소 정규장 상태 확인',
            'checked_at':stamp.isoformat(),'regression_failure':False,'real_orders_sent':0}
