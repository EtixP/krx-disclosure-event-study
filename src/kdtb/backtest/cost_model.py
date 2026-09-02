"""Date-aware Korean-equity transaction costs for the 2021-2026 sample.

The statutory sell-side schedule is encoded from Korea's official National
Law Information Center. The securities-transaction-tax rates are in Article
5 of the Enforcement Decree of the Securities Transaction Tax Act; the KOSPI-
only special rural tax is Article 5(1)(5) of the Act on Special Rural
Development Tax and Article 5(1) of its Enforcement Decree.

Official sources verified 2026-08-30:
- historical/current securities transaction tax:
  https://www.law.go.kr/LSW/lsRvsDocListP.do?chrClsCd=010102&lsId=005028
- 2026 securities transaction tax:
  https://www.law.go.kr/lsLinkCommonInfo.do?chrClsCd=010202&lspttninfSeq=64014
- special rural tax rate and KOSPI scope:
  https://www.law.go.kr/LSW/lsLinkCommonInfo.do?chrClsCd=010202&lsJoLnkSeq=1032879999
  https://www.law.go.kr/LSW/lsLinkCommonInfo.do?lspttninfSeq=72632

Commission, VAT on commission, and slippage remain configurable modeling
assumptions rather than statutory-history claims. Slippage is quoted in basis
points *per side*. Costs use the same notional on both sides, matching the
project's pre-existing return-drag approximation.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_COMMISSION_PER_SIDE = 0.00015  # 0.015%
DEFAULT_VAT_ON_COMMISSION = 0.10
DEFAULT_SLIPPAGE_BPS_PER_SIDE = 5.0  # 5 bps = 0.05% on each side

SUPPORTED_MARKETS = frozenset({"KOSPI", "KOSDAQ"})
SUPPORTED_START_DATE = date(2021, 1, 1)
SUPPORTED_END_DATE = date(2026, 12, 31)


@dataclass(frozen=True)
class TaxRegime:
    """Statutory sell-side rates for an inclusive date range."""

    start: date
    end: date
    kospi_transaction_tax: float
    kospi_special_rural_tax: float
    kosdaq_transaction_tax: float
    kosdaq_special_rural_tax: float = 0.0


TAX_REGIMES: tuple[TaxRegime, ...] = (
    TaxRegime(date(2021, 1, 1), date(2022, 12, 31), 0.0008, 0.0015, 0.0023),
    TaxRegime(date(2023, 1, 1), date(2023, 12, 31), 0.0005, 0.0015, 0.0020),
    TaxRegime(date(2024, 1, 1), date(2024, 12, 31), 0.0003, 0.0015, 0.0018),
    TaxRegime(date(2025, 1, 1), date(2025, 12, 31), 0.0, 0.0015, 0.0015),
    TaxRegime(date(2026, 1, 1), date(2026, 12, 31), 0.0005, 0.0015, 0.0020),
)


# ---------------------------------------------------------------------------
# Tradability bar
# ---------------------------------------------------------------------------
# This pre-existing 0.30% research hurdle is deliberately unchanged in M0.2:
# changing it after observing corrected results would be parameter tuning. It
# was originally rounded from the old 2024-style 0.313% modeled roundtrip.
# Corrected roundtrip costs vary by sell date from 0.283% to 0.363%, so the bar
# is now a fixed decision hurdle, not a claim that it equals every trade's cost.
TRADABILITY_BAR_PCT = 0.30

# Companion gates applied alongside the bar (see analyze_event_category._verdict).
MIN_TRADABLE_PF = 1.15
MIN_TRADABLE_WINDOW_FRACTION = 0.60


@dataclass(frozen=True)
class TaxRates:
    securities_transaction_tax: float
    special_rural_tax: float

    @property
    def total(self) -> float:
        return self.securities_transaction_tax + self.special_rural_tax


class CostBreakdown(BaseModel):
    """Auditable KRW components for one same-notional roundtrip."""

    model_config = ConfigDict(frozen=True)

    buy_commission: float
    buy_commission_vat: float
    buy_slippage: float
    sell_commission: float
    sell_commission_vat: float
    securities_transaction_tax: float
    special_rural_tax: float
    sell_slippage: float
    total: float


def _as_date(value: date | datetime | str, *, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise ValueError(f"{field} must be an ISO date, got {value!r}") from error
    raise TypeError(f"{field} must be a date, datetime, or ISO date string")


def _market(value: str) -> str:
    market = str(value).upper()
    if market not in SUPPORTED_MARKETS:
        raise ValueError(
            f"market must be one of {sorted(SUPPORTED_MARKETS)}, got {value!r}"
        )
    return market


def _notional(value: float) -> float:
    notional = float(value)
    if not math.isfinite(notional) or notional < 0:
        raise ValueError("notional_krw must be finite and non-negative")
    return notional


def tax_rates(*, sell_date: date | datetime | str, market: str) -> TaxRates:
    """Return statutory sell taxes for an explicitly dated supported market."""

    sold = _as_date(sell_date, field="sell_date")
    normalized_market = _market(market)
    for regime in TAX_REGIMES:
        if regime.start <= sold <= regime.end:
            if normalized_market == "KOSPI":
                return TaxRates(
                    regime.kospi_transaction_tax,
                    regime.kospi_special_rural_tax,
                )
            return TaxRates(
                regime.kosdaq_transaction_tax,
                regime.kosdaq_special_rural_tax,
            )
    raise ValueError(
        "sell_date is outside the law-verified 2021-2026 schedule: "
        f"{sold.isoformat()}"
    )


class CostModel(BaseModel):
    """Per-trade assumptions combined with the statutory historical schedule."""

    model_config = ConfigDict(frozen=True)

    commission_per_side: float = Field(default=DEFAULT_COMMISSION_PER_SIDE, ge=0)
    vat_on_commission: float = Field(default=DEFAULT_VAT_ON_COMMISSION, ge=0)
    slippage_bps_per_side: float = Field(
        default=DEFAULT_SLIPPAGE_BPS_PER_SIDE, ge=0
    )

    def roundtrip_breakdown(
        self,
        notional_krw: float,
        *,
        buy_date: date | datetime | str,
        sell_date: date | datetime | str,
        market: str,
    ) -> CostBreakdown:
        """Return explicit cost components; dates and market are mandatory."""

        notional = _notional(notional_krw)
        bought = _as_date(buy_date, field="buy_date")
        sold = _as_date(sell_date, field="sell_date")
        normalized_market = _market(market)
        if bought > sold:
            raise ValueError("buy_date must be on or before sell_date")
        if bought < SUPPORTED_START_DATE or bought > SUPPORTED_END_DATE:
            raise ValueError(
                "buy_date is outside the model's 2021-2026 supported period: "
                f"{bought.isoformat()}"
            )

        rates = tax_rates(sell_date=sold, market=normalized_market)
        commission = notional * self.commission_per_side
        vat = commission * self.vat_on_commission
        slippage = notional * (self.slippage_bps_per_side / 10_000.0)
        transaction_tax = notional * rates.securities_transaction_tax
        rural_tax = notional * rates.special_rural_tax
        total = 2 * (commission + vat + slippage) + transaction_tax + rural_tax
        return CostBreakdown(
            buy_commission=commission,
            buy_commission_vat=vat,
            buy_slippage=slippage,
            sell_commission=commission,
            sell_commission_vat=vat,
            securities_transaction_tax=transaction_tax,
            special_rural_tax=rural_tax,
            sell_slippage=slippage,
            total=total,
        )

    def buy_cost(
        self,
        notional_krw: float,
        *,
        buy_date: date | datetime | str,
        market: str,
    ) -> float:
        """Buy-side commission, VAT, and per-side slippage."""

        notional = _notional(notional_krw)
        bought = _as_date(buy_date, field="buy_date")
        _market(market)
        if bought < SUPPORTED_START_DATE or bought > SUPPORTED_END_DATE:
            raise ValueError("buy_date is outside the model's 2021-2026 supported period")
        commission = notional * self.commission_per_side
        return (
            commission
            + commission * self.vat_on_commission
            + notional * (self.slippage_bps_per_side / 10_000.0)
        )

    def sell_cost(
        self,
        notional_krw: float,
        *,
        sell_date: date | datetime | str,
        market: str,
    ) -> float:
        """Sell-side commission, VAT, statutory taxes, and per-side slippage."""

        notional = _notional(notional_krw)
        rates = tax_rates(sell_date=sell_date, market=market)
        commission = notional * self.commission_per_side
        return (
            commission
            + commission * self.vat_on_commission
            + notional * rates.total
            + notional * (self.slippage_bps_per_side / 10_000.0)
        )

    def roundtrip_cost(
        self,
        notional_krw: float,
        *,
        buy_date: date | datetime | str,
        sell_date: date | datetime | str,
        market: str,
    ) -> float:
        """Total same-notional roundtrip cost in KRW."""

        return self.roundtrip_breakdown(
            notional_krw,
            buy_date=buy_date,
            sell_date=sell_date,
            market=market,
        ).total

    def roundtrip_cost_fractions(
        self,
        *,
        buy_dates: Iterable[date | datetime | str],
        sell_dates: Iterable[date | datetime | str],
        markets: Iterable[str],
    ) -> list[float]:
        """Vector-friendly per-row costs on unit notional.

        ``zip(..., strict=True)`` prevents silent truncation when a research
        frame supplies misaligned date or market columns.
        """

        return [
            self.roundtrip_cost(
                1.0, buy_date=bought, sell_date=sold, market=market
            )
            for bought, sold, market in zip(
                buy_dates, sell_dates, markets, strict=True
            )
        ]

    def net_return(
        self,
        gross_return: float,
        *,
        buy_date: date | datetime | str,
        sell_date: date | datetime | str,
        market: str,
    ) -> float:
        """Approximate net return after the dated same-notional cost fraction."""

        return float(gross_return) - self.roundtrip_cost(
            1.0, buy_date=buy_date, sell_date=sell_date, market=market
        )
