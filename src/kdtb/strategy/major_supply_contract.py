from __future__ import annotations

from typing import Optional

from kdtb.config import StrategyParams
from kdtb.schemas import Disclosure, Extraction, Signal
from kdtb.strategy.base import Strategy


class MajorSupplyContractStrategy(Strategy):
    """Generate LONG signal when a sufficiently large NEW supply contract is disclosed.

    Per CLAUDE.md, the rule is:
      - event_type == major_supply_contract
      - is_new_contract == true and not a revision/cancellation
      - contract_to_revenue_ratio >= threshold (default 0.08 = 8%)
      - LLM confidence >= threshold (default 0.80)
      - stock_code is present
    """

    name = "major_supply_contract_v1"

    def __init__(self, params: StrategyParams) -> None:
        self.params = params

    def evaluate(
        self, extraction: Extraction, disclosure: Disclosure
    ) -> Optional[Signal]:
        if not self.params.enabled:
            return None
        if extraction.event_type != "major_supply_contract":
            return None
        if extraction.is_cancellation:
            return None
        if extraction.is_revision and not extraction.is_new_contract:
            return None
        if extraction.is_new_contract is not True:
            return None
        if extraction.confidence < self.params.min_llm_confidence:
            return None
        ratio = extraction.contract_to_revenue_ratio
        if ratio is None or ratio < self.params.min_contract_to_revenue_ratio:
            return None
        if not disclosure.stock_code:
            return None
        # Loop-7 filter: KOSPI showed 3/4 walk-forward windows positive vs
        # KOSDAQ's no edge. Restricting to KOSPI removes ~half of signals but
        # keeps the half with consistent edge.
        if self.params.kospi_only and disclosure.market != "KOSPI":
            return None
        # Loop-7 filter: government counterparties showed 3/4 walk-forward
        # windows from -5% to -0.5% T+5. Excluded by default.
        if (
            extraction.counterparty_type
            and extraction.counterparty_type in self.params.excluded_counterparty_types
        ):
            return None

        # ratio of 0.20 maps to strength 1.0; capped at 1.0
        strength = min(1.0, ratio / 0.20)
        reason_codes = [
            f"contract_to_revenue_ratio={ratio:.3f}",
            f"llm_confidence={extraction.confidence:.2f}",
            f"contract_value_krw={extraction.contract_value_krw or 0}",
            f"prior_year_revenue_krw={extraction.prior_year_revenue_krw or 0}",
            f"market={disclosure.market}",
            f"counterparty_type={extraction.counterparty_type or 'unknown'}",
        ]
        return Signal(
            signal_id=f"{disclosure.receipt_no}-{self.name}",
            disclosure_id=disclosure.id or 0,
            extraction_id=extraction.id or 0,
            stock_code=disclosure.stock_code,
            strategy_name=self.name,
            direction="long",
            strength=strength,
            reason_codes=reason_codes,
            stop_loss_pct=self.params.stop_loss_pct,
            take_profit_pct=self.params.take_profit_pct,
        )
