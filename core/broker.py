"""Kiwoom 공식 REST 명세 기반. 주문 전송 실패는 자동 재전송하지 않는다."""
import hashlib
import json
import os
import time
import threading
from datetime import datetime, timedelta
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from core.settings import KST, now
from core.util import number, code

class Rejected(RuntimeError):
    def __init__(self,message,api=None,response=None):
        super().__init__(message); self.api=api; self.response=response

class AmbiguousOrder(RuntimeError):
    pass

class Rest:
    def __init__(self, cfg, opener=urlopen,auth_session=None):
        self.cfg=cfg
        self.base='https://mockapi.kiwoom.com' if cfg.broker_environment=='mock' else 'https://api.kiwoom.com'
        self.key=os.getenv('KIWOOM_APP_KEY','')
        self.secret=os.getenv('KIWOOM_APP_SECRET','')
        if not self.key or not self.secret:
            raise ValueError('KIWOOM_APP_KEY / KIWOOM_APP_SECRET 환경변수 필요. 인증정보는 로그에 기록하지 않습니다.')
        self.identity=hashlib.sha256((self.base+self.key).encode()).hexdigest()
        self.opener=opener
        if auth_session is not None and auth_session['identity']!=self.identity:
            raise ValueError('인증 세션 계좌/서버 불일치')
        self.auth_session=auth_session if auth_session is not None else {'identity':self.identity,'token':'',
            'expires':now(),'lock':threading.RLock(),'rate_lock':threading.Lock(),'next_call':0.}
        self.last_call=0
        self.profile={'api_wait_ms':0.,'http_ms':0.,'requests':0}
        self.auth_lock=self.auth_session['lock']

    @property
    def token(self): return self.auth_session['token']
    @token.setter
    def token(self,value): self.auth_session['token']=value
    @property
    def expires(self): return self.auth_session['expires']
    @expires.setter
    def expires(self,value): self.auth_session['expires']=value

    def get_token(self):
        with self.auth_lock:
            if not self.token or now()+timedelta(minutes=5)>=self.expires:
                if getattr(self,'diagnostic_only',False):raise Rejected('DIAGNOSTIC_DEFERRED_AUTH_REFRESH')
                body,_=self._http('/oauth2/token',{'grant_type':'client_credentials','appkey':self.key,'secretkey':self.secret},{})
                if not body.get('token'):
                    raise Rejected('인증 응답에 토큰 없음',api='au10001',response=body)
                self.token=body['token']
                self.expires=datetime.strptime(body['expires_dt'],'%Y%m%d%H%M%S').replace(tzinfo=KST)
            return self.token

    def _http(self,path,body,headers):
        # 같은 앱키를 쓰는 조회 스레드는 토큰과 전송 간격을 공유한다.
        wait_started=time.perf_counter()
        deadline=time.monotonic()+1.2
        while True:
            with self.auth_session['rate_lock']:
                scheduled=max(time.monotonic(),self.auth_session['next_call'])
                deferred=getattr(self,'diagnostic_only',False) and scheduled>time.monotonic()+.001
                if not deferred:
                    self.auth_session['next_call']=scheduled+0.3;break
            # 진단은 미래 슬롯을 선점하지 않는다. 정상 조회/주문이 먼저 예약할 수 있게 양보한다.
            if scheduled-time.monotonic()>.8 or time.monotonic()>=deadline:raise Rejected('DIAGNOSTIC_DEFERRED_PRIORITY_API')
            time.sleep(min(.05,max(0,scheduled-time.monotonic())))
        time.sleep(max(0,scheduled-time.monotonic()))
        self.profile['api_wait_ms']+=(time.perf_counter()-wait_started)*1000
        self.last_call=time.monotonic()
        session_guard=getattr(self,'session_guard',None)
        if session_guard is not None and not session_guard(): raise Rejected('OUT_OF_SUPPORTED_SESSION')
        req=Request(self.base+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json;charset=UTF-8',**headers},method='POST')
        api=headers.get('api-id')
        if api in {'kt10000','kt10001','kt10003'}:
            if not self.mutation_allowed(api,body): raise Rejected('dry-run: 주문 API 접근 차단')
            guard=getattr(self,'order_guard',None)
            if guard is not None: guard(api,body)  # 인증/대기 후 실제 전송 직전에도 시각·정책을 재검사한다.
        observer=getattr(self,'submission_observer',None)
        if api in {'kt10000','kt10001','kt10003'} and observer is not None: observer(api,body)
        http_started=time.perf_counter()
        try:
            with self.opener(req,timeout=8) as response:
                data=json.load(response)
                return data, {k.lower():v for k,v in response.headers.items()}
        finally:
            self.profile['http_ms']+=(time.perf_counter()-http_started)*1000;self.profile['requests']+=1

    def mutation_allowed(self,api,body):
        if getattr(self,'diagnostic_only',False):return False
        if self.cfg.live_mode is True: return True
        if api!='kt10001' or body.get('stk_cd')!='267320' or body.get('ord_qty')!='1': return False
        if getattr(self,'order_guard',None) is None: return False
        permit=getattr(self,'test_release_authorizer',None)
        return permit is not None and permit(api,body) is True

    def call(self,api,path,body,cont='N',key='',mutation=False):
        if mutation and not self.mutation_allowed(api,body):
            raise Rejected('dry-run: 주문 API 접근 차단')
        guard=getattr(self,'order_guard',None)
        if mutation and guard is not None:
            guard(api,body)
        attempts=1 if mutation else 3
        auth_retried=False
        for attempt in range(attempts):
            try:
                used_token=self.get_token()
                headers={'authorization':'Bearer '+used_token,'api-id':api,'cont-yn':cont,'next-key':key}
                data,headers=self._http(path,body,headers)
                if data.get('return_code') not in (0,'0'):
                    if 'return_code' not in data and mutation:
                        raise AmbiguousOrder('주문 응답 결과코드 없음')
                    # HTTP 200 can contain an explicitly invalid token. Retry
                    # reads once only; never retransmit an order or refresh
                    # authentication from a diagnostic client.
                    if (not mutation and not getattr(self,'diagnostic_only',False)
                        and not auth_retried and attempt+1<attempts
                        and str(data.get('return_code'))=='3'
                        and '8005:' in str(data.get('return_msg',''))):
                        with self.auth_lock:
                            if self.token==used_token:self.token=''
                            self.get_token()
                        auth_retried=True
                        continue
                    raise Rejected(f'{api}: 오류코드 {data.get("return_code", "MISSING")}',api=api,response=data)
                return data,headers
            except Rejected:
                raise
            except (HTTPError,URLError,TimeoutError,OSError,ValueError,KeyError) as e:
                if mutation:
                    raise AmbiguousOrder(f'{api}: 응답 불명확, 재전송 금지 ({type(e).__name__})') from None
                if isinstance(e,HTTPError) and e.code==401:
                    self.token=''
                if attempt+1==attempts:
                    raise RuntimeError(f'{api}: 조회 실패 ({type(e).__name__})') from None
                time.sleep(1+attempt)

    def query(self,api,body,path='/api/dostk/acnt'):
        return self.call(api,path,body)[0]

    def pages(self,api,body,array,path='/api/dostk/acnt',limit=None):
        rows=[]; key=''; cont='N'; seen=set(); first=None
        for _ in range(200):
            data,h=self.call(api,path,body,cont,key)
            first=first or data
            if array not in data or not isinstance(data[array],list):
                raise ValueError(f'{api}: {array} 필드 없음/형식 오류')
            rows.extend(data[array])
            if h.get('cont-yn')!='Y' or (limit and len(rows)>=limit):
                return rows[:limit] if limit else rows, first
            key=h.get('next-key','')
            if not key or key in seen:
                raise ValueError('연속조회 키 누락/반복')
            seen.add(key); cont='Y'
        raise ValueError('연속조회 최대 페이지 초과')

    def snapshot(self):
        holdings,summary=self.pages('kt00018',{'qry_tp':'2','dmst_stex_tp':'KRX'},'acnt_evlt_remn_indv_tot')
        cash=self.query('kt00001',{'qry_tp':'2'})
        # Production with AFTER enabled must see foreign/self NXT as well as KRX orders.
        exchange='0' if getattr(self.cfg,'after_enabled',False) is True and self.cfg.broker_environment=='production' else '1'
        orders,_=self.pages('ka10075',{'all_stk_tp':'0','trde_tp':'0','stk_cd':'','stex_tp':exchange},'oso')
        balances={}
        for h in holdings:
            symbol=code(h['stk_cd'])
            qty=int(number(h['rmnd_qty']))
            if qty:
                if symbol in balances:
                    raise ValueError('동일 종목 복수 잔고: 현금/신용 혼재 확인 필요')
                balances[symbol]={'qty':qty,'available':int(number(h['trde_able_qty'])),'avg':number(h['pur_pric'])}
        return {'holdings':balances,'orders':orders,'cash':number(cash['100stk_ord_alow_amt'],signed=True),
                'equity':number(summary['prsm_dpst_aset_amt'],signed=True)}

    def fills(self):
        exchange='0' if getattr(self.cfg,'after_enabled',False) is True and self.cfg.broker_environment=='production' else '1'
        return self.pages('ka10076',{'stk_cd':'','qry_tp':'0','sell_tp':'0','ord_no':'','stex_tp':exchange},'cntr')[0]

    def stock_status(self,symbol):
        from automation.market_status import assess_status
        data=self.query('ka10100',{'stk_cd':code(symbol)},path='/api/dostk/stkinfo')
        if data.get('code')!=symbol: raise ValueError('ka10100 종목 불일치')
        result=assess_status(data); result['checked_at']=now().isoformat()
        return result

    def existing_book(self,symbol):
        # 공식 ka10004의 최우선 매수호가/잔량/기준시간. 전체 호가를 추정하지 않는다.
        from datetime import datetime
        status=self.stock_status(symbol)
        raw=self.query('ka10004',{'stk_cd':code(symbol)},path='/api/dostk/mrkcond')
        received=now(); stamp=datetime.strptime(raw['bid_req_base_tm'],'%H%M%S').time()
        quoted=datetime.combine(received.date(),stamp,tzinfo=received.tzinfo)
        qty=number(raw['buy_fpr_req'],signed=True)
        if qty<0 or not qty.is_integer(): raise ValueError('ka10004 유효한 정수 호가잔량 필요')
        return {'time':quoted.isoformat(),'bid':number(raw['buy_fpr_bid']),'bid_qty':int(qty),
                'regular':True,'status':status,'source':'ka10004',
                'raw':{k:raw[k] for k in ('bid_req_base_tm','buy_fpr_bid','buy_fpr_req')}}

    def position_book(self,symbol,session,venue):
        """Read-only ka10004, using the held position's exchange, never a KRX substitute.

        Official Kiwoom example: KRX code, NXT code_NX, SOR code_AL.
        The exchange quote timestamp is evidence; HTTP receipt time is not freshness.
        """
        if session not in {'REGULAR','AFTER'} or venue not in {'KRX','NXT','SOR'}:
            raise ValueError('Position quote session/exchange required')
        symbol=code(symbol)
        if session=='AFTER' and venue=='KRX':
            raise Rejected('AFTER_POSITION_QUOTE_VENUE_UNAVAILABLE')
        request_symbol=symbol+{'KRX':'','NXT':'_NX','SOR':'_AL'}[venue]
        raw=self.query('ka10004',{'stk_cd':request_symbol},path='/api/dostk/mrkcond')
        received=now()
        quoted=datetime.combine(received.date(),datetime.strptime(raw['bid_req_base_tm'],'%H%M%S').time(),tzinfo=received.tzinfo)
        values={k:number(raw[k],signed=True) for k in ('buy_fpr_bid','sel_fpr_bid','buy_fpr_req','sel_fpr_req')}
        bid=abs(values['buy_fpr_bid']);ask=abs(values['sel_fpr_bid'])
        if not 0<bid<=ask or any(values[k]<0 or not values[k].is_integer() for k in ('buy_fpr_req','sel_fpr_req')):
            raise ValueError('Invalid position quote price/depth')
        return {'time':quoted.isoformat(),'received_at':received.isoformat(),'bid':bid,'best_bid':bid,'best_ask':ask,
                'bid_qty':int(values['buy_fpr_req']),'ask_qty':int(values['sel_fpr_req']),
                'regular':session=='REGULAR','session':session,'venue':venue,'requested_symbol':request_symbol,
                'source':'ka10004','depth_available':True}

    def submit(self,side,symbol,qty,limit=0,venue='KRX',session='REGULAR'):
        if type(qty) is not int or qty<=0:
            raise Rejected('양의 정수 주문수량 필요')
        code(symbol)
        if session=='AFTER' and (side=='SELL' and limit<=0):
            raise Rejected('AFTER 시장에서는 지정가 주문만 허용 (시장가 매도 금지)')
        if side not in ('BUY','SELL') or (side=='BUY' and limit<=0):
            raise Rejected('매수는 가격 상한 지정가만 허용')
        stex = venue if venue in ('SOR','NXT','KRX') else 'KRX'
        trde_tp = '0' if limit or session=='AFTER' else '3'
        data,_=self.call('kt10000' if side=='BUY' else 'kt10001','/api/dostk/ordr',{
            'dmst_stex_tp':stex,'stk_cd':symbol,'ord_qty':str(qty),'ord_uv':str(int(limit)) if limit else '',
            'trde_tp':trde_tp,'cond_uv':''},mutation=True)
        if not data.get('ord_no'):
            raise AmbiguousOrder('주문번호 없는 접수 응답')
        return str(data['ord_no'])

    def cancel(self,symbol,order_id,venue='KRX'):
        stex = venue if venue in ('SOR','NXT','KRX') else 'KRX'
        data,_=self.call('kt10003','/api/dostk/ordr',{'dmst_stex_tp':stex,'orig_ord_no':order_id,
            'stk_cd':symbol,'cncl_qty':'0'},mutation=True)
        if not data.get('ord_no'):
            raise AmbiguousOrder('취소 주문번호 누락')
        return str(data['ord_no'])

