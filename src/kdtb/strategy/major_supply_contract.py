from __future__ import annotations

from typing import Optional

from kdtb.config import StrategyParams
from kdtb.schemas import Extraction, Signal
from kdtb.strategy.base import Strategy


class MajorSupplyContractStrategy(Strategy):
    """Placeholder for major-supply-contract strategy (Milestone 4)."""

    name = "major_supply_contract_v1"

    def __init__(self, params: StrategyParams) -> None:
        self.params = params

    def evaluate(self, extraction: Extraction) -> Optional[Signal]:
        raise NotImplementedError("Implemented in Milestone 4")
