"""기존 보유 평가금액을 섞지 않는 봇 자본과 명시적 거래비용."""
from dataclasses import dataclass

@dataclass(frozen=True)
class Costs:
    buy: float
    sell: float
    tax: float
    entry_slippage: float
    exit_slippage: float

    def buy_fee(self,amount): return amount*self.buy
    def sell_fee(self,amount): return amount*(self.sell+self.tax)
    def roundtrip(self,entry,exit_price):
        return self.buy_fee(entry)+self.sell_fee(exit_price)
    def estimate(self,entry,target):
        return self.roundtrip(entry,target)+entry*self.entry_slippage+target*self.exit_slippage

def owned_equity(cash,positions,prices):
    return cash+sum(p['qty']*prices.get(s,p['avg']) for s,p in positions.items() if p.get('ownership','BOT')=='BOT')

def initial_allocation(snapshot,limit):
    return min(max(0,snapshot['cash']),max(0,limit))

