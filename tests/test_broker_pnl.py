"""Authoritative reported charges vs provisional risk cost, no network or orders."""
import copy,unittest
from datetime import datetime,timedelta
from automation.broker_pnl import settle_broker_pnl
from automation.capital import Costs
from core.settings import KST

NOW=datetime(2026,10,13,19,40,tzinfo=KST)
COSTS=Costs(.00015,.00015,.002,.002,.003)

def data():
    amount_buy=17460*3;amount_sell=17500*3;old=120-COSTS.roundtrip(amount_buy,amount_sell)
    orders={side:{'local':side,'id':side.upper(),'symbol':'999990','side':side.upper(),'qty':3,'filled':3,
        'amount':amount_buy if side=='buy' else amount_sell,'created':NOW.isoformat(),'last_fill_at':NOW.isoformat(),
        'status':'FILLED','signal':{}} for side in ('buy','sell')}
    trade={'trade_id':'buy','symbol':'999990','qty':3,'entry':17460,'exit':17500,'closed':NOW.isoformat(),
           'pnl_estimate':old,'completed':True,'exit_order_local':'sell'}
    state={'identity':'synthetic','day':NOW.date().isoformat(),'orders':orders,'trades':[trade],
           'cash':50000.,'realized':old,'daily_loss':1000+max(0,-old),'entries':{'999990':1},
           'automation':{'cash':50000.,'consecutive_losses':4}}
    fills=[{'ord_no':side.upper(),'stk_cd':'999990','ord_stt':'체결','io_tp_nm':'+매수' if side=='buy' else '-매도',
            'cntr_qty':'3','ord_qty':'3','cntr_pric':'17460' if side=='buy' else '17500',
            'tdy_trde_cmsn':'0','tdy_trde_tax':'0' if side=='buy' else '105'} for side in ('buy','sell')]
    return state,fills

class BrokerPnlTests(unittest.TestCase):
    def settle(self,state,fills):return settle_broker_pnl(state,fills,COSTS,NOW,'synthetic')
    def test_reported_zero_commission_is_authoritative_and_prior_loss_is_preserved(self):
        s,f=data();r=self.settle(s,f)
        self.assertEqual(s['trades'][0]['pnl_estimate'],15);self.assertEqual(s['daily_loss'],1000)
        self.assertAlmostEqual(r['cash_adjustment'],15.732);self.assertAlmostEqual(s['realized'],15)
        self.assertEqual(s['trades'][0]['pnl_source'],'BROKER_QUERY');self.assertEqual(s['automation']['consecutive_losses'],0)
    def test_performance_uses_actual_charges_and_marks_their_source(self):
        from automation.performance import report
        s,f=data();s['trades'][0].update(opened=NOW.isoformat(),MFE=240,MAE=160,holding_seconds=300,fee_estimate=120.732)
        self.settle(s,f);summary=report(s['trades'])
        self.assertEqual(summary['completed_trades'][0]['fee_estimate'],105)
        self.assertEqual(summary['completed_trades'][0]['net_pnl'],15)
        self.assertIn('증권사',summary['all']['cost_basis'])
    def test_same_query_does_not_double_credit_cash_or_adjust_losses(self):
        s,f=data();self.settle(s,f);before=(s['cash'],s['realized'],s['daily_loss'])
        r=self.settle(s,f)
        self.assertEqual(before,(s['cash'],s['realized'],s['daily_loss']))
        self.assertEqual((r['cash_adjustment'],r['pnl_adjustment'],r['loss_adjustment']),(0,0,0))
    def test_later_broker_charge_revision_updates_exact_difference_once(self):
        s,f=data();self.settle(s,f)
        for row in f:row['tdy_trde_cmsn']='10'
        r=self.settle(s,f)
        self.assertEqual(s['trades'][0]['pnl_estimate'],-5);self.assertEqual(s['daily_loss'],1005)
        self.assertEqual(r['cash_adjustment'],-20)
        self.assertEqual(self.settle(s,f)['cash_adjustment'],0)
    def test_actual_tax_is_used_instead_of_tax_rate_estimate(self):
        s,f=data();f[1]['tdy_trde_tax']='0';self.settle(s,f)
        self.assertEqual(s['trades'][0]['tax'],0);self.assertEqual(s['trades'][0]['pnl_estimate'],120)
    def test_missing_negative_nonfinite_charge_does_not_become_zero(self):
        for value in ('',None,'-1','NaN','Infinity'):
            s,f=data();f[1]['tdy_trde_cmsn']=value;before=s['trades'][0]['pnl_estimate']
            r=self.settle(s,f)
            self.assertEqual(s['trades'][0]['pnl_estimate'],before);self.assertTrue(r['unknown'])
        s,f=data();self.settle(s,f);f[1]['tdy_trde_cmsn']=''
        settle_broker_pnl(s,f,COSTS,NOW+timedelta(minutes=1),'synthetic')
        self.assertEqual(s['trades'][0]['broker_pnl_observed_at'],NOW.isoformat())
    def test_wrong_order_side_symbol_quantity_or_amount_cannot_settle_trade(self):
        for key,value in (('stk_cd','999980'),('io_tp_nm','+매수'),('cntr_qty','2'),('cntr_pric','17501')):
            s,f=data();f[1][key]=value;before=s['trades'][0]['pnl_estimate']
            self.settle(s,f);self.assertEqual(s['trades'][0]['pnl_estimate'],before)
    def test_duplicated_or_missing_order_rows_are_ambiguous(self):
        for change in ('duplicate','missing'):
            s,f=data();before=s['trades'][0]['pnl_estimate']
            f=f+[copy.deepcopy(f[1])] if change=='duplicate' else f[:1]
            self.settle(s,f);self.assertEqual(s['trades'][0]['pnl_estimate'],before)
    def test_partial_sell_fragments_allocate_charges_without_double_charging_buy(self):
        s,f=data();s['trades'][0]['qty']=1;s['trades'][0]['pnl_estimate']/=3;s['trades'][0]['completed']=False
        last={**s['trades'][0],'qty':2,'pnl_estimate':s['trades'][0]['pnl_estimate']*2,'completed':True}
        s['trades'].append(last);self.settle(s,f)
        self.assertAlmostEqual(sum(t['pnl_estimate'] for t in s['trades']),15)
        self.assertAlmostEqual(sum(t['fee_actual'] for t in s['trades']),105)
    def test_partial_order_fee_growth_only_replaces_the_new_modeled_increment(self):
        s,f=data();s['orders']['buy'].update(filled=1,amount=17460);f[0]['cntr_qty']='1'
        self.settle(s,f);cash=s['cash']
        s['orders']['buy'].update(filled=3,amount=52380);f[0]['cntr_qty']='3'
        s['cash']-=COSTS.buy_fee(34920);s['automation']['cash']-=COSTS.buy_fee(34920)
        self.settle(s,f);self.assertAlmostEqual(s['cash'],cash)
    def test_identity_or_day_mismatch_is_not_authoritative(self):
        for identity,stamp in (('foreign',NOW),('synthetic',NOW+timedelta(days=1))):
            s,f=data();before=copy.deepcopy(s)
            settle_broker_pnl(s,f,COSTS,stamp,identity);self.assertEqual(s,before)
    def test_legacy_exit_is_matched_only_by_unique_fill_time(self):
        s,f=data();s['trades'][0].pop('exit_order_local');self.settle(s,f)
        self.assertEqual(s['trades'][0]['pnl_estimate'],15)
        s,f=data();s['trades'][0].pop('exit_order_local');s['orders']['other']={**s['orders']['sell'],'local':'other'}
        old=s['trades'][0]['pnl_estimate'];self.settle(s,f);self.assertEqual(s['trades'][0]['pnl_estimate'],old)
        self.assertAlmostEqual(s['cash'],50000+COSTS.buy_fee(52380))
    def test_external_policy_holdings_and_entry_counters_are_untouched(self):
        s,f=data();s['orders']['sell']['signal']={'ownership':'EXISTING_POLICY'};entries=copy.deepcopy(s['entries'])
        old=s['trades'][0]['pnl_estimate'];self.settle(s,f)
        self.assertEqual(s['trades'][0]['pnl_estimate'],old);self.assertEqual(s['entries'],entries)
