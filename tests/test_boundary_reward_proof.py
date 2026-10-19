"""Exercise the actual quote boundary with the required Setup B reward proof.

The older weekend fixture lacks any target and is now (correctly) stopped by
the existing net-reward guard before its intended quote assertions.
"""
from tests import test_weekend_execution as _weekend


class BoundaryWithRewardProofTests(_weekend.FinalBuyEvidenceTests):
    def ready(self,depth=100):
        super().ready(depth)
        self.signal['target']=6660
