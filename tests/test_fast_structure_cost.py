from datetime import datetime,timedelta
from statistics import median
import random,unittest
from automation.fast_momentum import MicroTape
from core.settings import KST
AT=datetime(2026,10,22,10,40,tzinfo=KST)

def legacy(tape,at,exclude=True):
    rows=[r for r,_ in tape._observed_rows(at)]
    if exclude and rows:rows=rows[:-1]
    turns=[]
    for row in rows:
        if not turns or row[1]!=turns[-1][1]:turns.append(row)
    evidence={'basis':'PREDECISION_OBSERVED_PULLBACK_REBOUND','asof':at.isoformat(),
              'price_points':len(turns),'synthetic_bars_used':False}
    for i in range(len(turns)-2,0,-1):
        left,low,right=turns[i-1:i+2]
        if not left[1]>low[1]<right[1]:continue
        peak_i=max(range(i),key=lambda n:turns[n][1]);peak=turns[peak_i]
        if peak_i==0:continue
        origin=min(turns[:peak_i],key=lambda r:r[1])
        if low[1]<=origin[1] or any(r[1]<low[1] for r in turns[peak_i+1:]):continue
        noise=median([abs(b[1]-a[1]) for a,b in zip(turns,turns[1:])])
        return {**evidence,'pass':True,'support':low[1],'support_at':low[0].isoformat(),
            'confirmation_at':right[0].isoformat(),'breakout':peak[1],'breakout_at':peak[0].isoformat(),
            'origin':origin[1],'origin_at':origin[0].isoformat(),'observed_noise':noise}
    return {**evidence,'pass':False,'reason':'CONFIRMED_MICRO_SUPPORT_UNKNOWN'}

class FastStructureCost(unittest.TestCase):
    def tape(self,prices):
        t=MicroTape()
        for i,p in enumerate(prices):
            at=AT-timedelta(seconds=(len(prices)-i)/100)
            t.ticks.append((at,p,1,0,p-1,p))
            t.metadata.append({'received_at':at,'received_monotonic':None})
        return t

    def test_structure_matches_preserved_algorithm_for_ties_and_random_paths(self):
        rng=random.Random(20261022)
        paths=[[],[1],[1,2,1],[100,110,105,110,108,109,111],[100,120,110,120,115,118,121]]
        paths += [[rng.randrange(90,111) for _ in range(rng.randrange(2,160))] for _ in range(250)]
        for prices in paths:
            tape=self.tape(prices)
            for exclude in (True,False):
                self.assertEqual(tape.structure(AT,exclude),legacy(tape,AT,exclude))

    def test_oscillating_decline_does_not_repeat_prefix_searches(self):
        class Price(float):
            comparisons=0
            def __lt__(self,other):Price.comparisons+=1;return super().__lt__(other)
            def __gt__(self,other):Price.comparisons+=1;return super().__gt__(other)
            def __le__(self,other):Price.comparisons+=1;return super().__le__(other)
        prices=[Price(10000)]+[Price(9000-i//2+(i%2)*.5) for i in range(1000)]
        tape=self.tape(prices);Price.comparisons=0
        result=tape.structure(AT)
        self.assertFalse(result['pass'])
        self.assertLess(Price.comparisons,20*len(prices))

    def test_unreceived_and_clock_lead_bounds_keep_same_structure(self):
        tape=self.tape([100,110,105,107,112,109,111])
        tape.metadata[-2]['received_at']=AT+timedelta(seconds=2)
        for seconds in (-.5,0,.5,2):
            at=AT+timedelta(seconds=seconds)
            self.assertEqual(tape.structure(at),legacy(tape,at))
