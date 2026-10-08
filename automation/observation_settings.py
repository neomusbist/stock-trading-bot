"""관찰 용량/부하 설정. BUY 위험비율·source 자격·LIVE 승인은 포함하지 않는다."""
import json
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class ObservationSettings:
    max_symbols:int=32
    metadata_budget:int=4
    metadata_ttl_seconds:int=180
    evaluation_seconds:float=2
    far_book_seconds:int=30
    near_threshold_fraction:float=.03
    far_evaluation_seconds:int=5
    rank_limit_multiplier:int=1
    max_raw_symbols:int=300
    def validate(self):
        if type(self.rank_limit_multiplier) is not int or self.rank_limit_multiplier not in (1,2):raise ValueError('랭킹 범위 배수는 1 또는 2')
        if type(self.max_raw_symbols) is not int or not 100<=self.max_raw_symbols<=300:raise ValueError('원시 후보 한도는 100~300')
        if type(self.max_symbols) is not int or not 1<=self.max_symbols<=64:raise ValueError('관찰 최대종목수는 1~64')
        if type(self.metadata_budget) is not int or not 1<=self.metadata_budget<=8:raise ValueError('진단 조회 예산은 1~8')
        if not 30<=self.metadata_ttl_seconds<=300 or not .5<=self.evaluation_seconds<=10:raise ValueError('관찰 신선도/평가 주기 범위 확인 필요')
        if not 5<=self.far_book_seconds<=30 or not 0<self.near_threshold_fraction<=.05 or not 1<=self.far_evaluation_seconds<=5:raise ValueError('외부보유 감시 주기 범위 확인 필요')
        return self

def load_observation(root):
    path=Path(root)/'observation.json'
    return ObservationSettings(**json.loads(path.read_text(encoding='utf-8-sig'))).validate() if path.exists() else ObservationSettings()
