"""Korean equity transaction cost model.

Sources (subject to change — verify before live trading):
- Broker commission: typical online broker ~0.015% per side (Korea Investment
  Securities default; some brokers offer cheaper). Configurable.
- Securities transaction tax (증권거래세): 0.18% on the SELL side only
  (KOSPI/KOSDAQ, 2024+; check KRX for current rate).
- VAT on commission: 10% of commission (commission is taxable).
- Slippage: depends on order type, liquidity, urgency. Default 5 bps.

Costs are quoted in KRW per share or as a fraction of notional.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

KOSPI_KOSDAQ_TX_TAX = 0.0018  # 0.18% on sale, current as of 2024+
DEFAULT_COMMISSION_PER_SIDE = 0.00015  # 0.015%
DEFAULT_VAT_ON_COMMISSION = 0.10
DEFAULT_SLIPPAGE_BPS = 5.0  # 5 bps = 0.05%


# ---------------------------------------------------------------------------
# Tradability bar
# ---------------------------------------------------------------------------
# Minimum mean net return per trade, in percent, for a category to count as
# tradable. Applied to a return that is ALREADY net of the modeled roundtrip
# cost below.
#
# Derivation. The modeled roundtrip is 0.313% of notional (commission x2 +
# VAT + 0.18% sale tax + 5bps slippage). Requiring a post-cost mean of roughly
# one further roundtrip means a strategy still clears zero if the true all-in
# cost turns out to be about double what we model. That margin is not
# decorative: the 5bps slippage assumption is the weakest input in the model
# and is optimistic for the illiquid KOSDAQ names where most of these events
# occur, and the event studies fill at a daily close that a real order may not
# obtain. So the bar is set at one modeled roundtrip, rounded down to a round
# number: 0.30%.
#
# This is a chosen hurdle, not an estimate of anything. It is deliberately
# blunt, and it is the criterion the project's headline conclusion turns on:
# every category tested lands below it. Note the conclusion is not sensitive
# to the rounding — the best realistic mean observed across seven categories
# is +0.25%, so the verdict is unchanged anywhere in a 0.25%-0.35% band.
TRADABILITY_BAR_PCT = 0.30

# Companion gates applied alongside the bar (see analyze_event_category._verdict):
# a category must also clear PF > 1.15 and have >=60% of walk-forward windows
# positive, so a single lucky regime cannot carry the mean past the bar.
MIN_TRADABLE_PF = 1.15
MIN_TRADABLE_WINDOW_FRACTION = 0.60


class CostModel(BaseModel):
    """Per-trade cost assumptions for Korean equities."""

    commission_per_side: float = Field(default=DEFAULT_COMMISSION_PER_SIDE, ge=0)
    tx_tax_on_sell: float = Field(default=KOSPI_KOSDAQ_TX_TAX, ge=0)
    vat_on_commission: float = Field(default=DEFAULT_VAT_ON_COMMISSION, ge=0)
    slippage_bps: float = Field(default=DEFAULT_SLIPPAGE_BPS, ge=0)

    def buy_cost(self, notional_krw: float) -> float:
        """Total cost on the buy side (commission + VAT on commission + slippage)."""
        commission = notional_krw * self.commission_per_side
        vat = commission * self.vat_on_commission
        slippage = notional_krw * (self.slippage_bps / 10000.0)
        return commission + vat + slippage

    def sell_cost(self, notional_krw: float) -> float:
        """Total cost on the sell side (commission + VAT + tx tax + slippage)."""
        commission = notional_krw * self.commission_per_side
        vat = commission * self.vat_on_commission
        tax = notional_krw * self.tx_tax_on_sell
        slippage = notional_krw * (self.slippage_bps / 10000.0)
        return commission + vat + tax + slippage

    def roundtrip_cost(self, notional_krw: float) -> float:
        """Buy + sell costs combined, assuming same notional."""
        return self.buy_cost(notional_krw) + self.sell_cost(notional_krw)

    def net_return(self, gross_return: float) -> float:
        """Net return after roundtrip costs, given a gross return (e.g. 0.02 = +2%).

        Approximate: costs are a fixed fraction of notional, so the cost drag is
        independent of return. We subtract the cost fraction directly.
        """
        cost_fraction = self.roundtrip_cost(1.0)  # cost on notional=1
        return gross_return - cost_fraction
