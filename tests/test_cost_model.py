from __future__ import annotations

from datetime import date

import pytest

from kdtb.backtest.cost_model import CostModel, tax_rates


@pytest.mark.parametrize(
    ("sell_date", "expected_total"),
    [
        (date(2021, 1, 1), 0.0023),
        (date(2022, 12, 31), 0.0023),
        (date(2023, 1, 1), 0.0020),
        (date(2023, 12, 31), 0.0020),
        (date(2024, 1, 1), 0.0018),
        (date(2024, 12, 31), 0.0018),
        (date(2025, 1, 1), 0.0015),
        (date(2025, 12, 31), 0.0015),
        (date(2026, 1, 1), 0.0020),
        (date(2026, 12, 31), 0.0020),
    ],
)
def test_tax_regime_boundaries(sell_date, expected_total):
    assert tax_rates(sell_date=sell_date, market="KOSPI").total == expected_total
    assert tax_rates(sell_date=sell_date, market="KOSDAQ").total == expected_total


def test_market_specific_tax_components_are_preserved():
    kospi = tax_rates(sell_date="2024-06-30", market="KOSPI")
    kosdaq = tax_rates(sell_date="2024-06-30", market="KOSDAQ")

    assert kospi.securities_transaction_tax == 0.0003
    assert kospi.special_rural_tax == 0.0015
    assert kosdaq.securities_transaction_tax == 0.0018
    assert kosdaq.special_rural_tax == 0
    assert kospi.total == kosdaq.total


def test_2026_increase_is_applied_on_sell_date_across_year_boundary():
    model = CostModel(
        commission_per_side=0,
        vat_on_commission=0,
        slippage_bps_per_side=0,
    )
    cost = model.roundtrip_cost(
        1_000_000,
        buy_date="2025-12-30",
        sell_date="2026-01-02",
        market="KOSPI",
    )
    assert cost == 2_000


def test_breakdown_distinguishes_every_component_and_totals():
    model = CostModel()
    breakdown = model.roundtrip_breakdown(
        10_000_000,
        buy_date="2024-06-03",
        sell_date="2024-06-10",
        market="KOSPI",
    )

    assert breakdown.buy_commission == pytest.approx(1_500)
    assert breakdown.buy_commission_vat == pytest.approx(150)
    assert breakdown.buy_slippage == pytest.approx(5_000)
    assert breakdown.sell_commission == pytest.approx(1_500)
    assert breakdown.sell_commission_vat == pytest.approx(150)
    assert breakdown.securities_transaction_tax == pytest.approx(3_000)
    assert breakdown.special_rural_tax == pytest.approx(15_000)
    assert breakdown.sell_slippage == pytest.approx(5_000)
    assert breakdown.total == pytest.approx(31_300)


def test_slippage_bps_is_per_side():
    model = CostModel(
        commission_per_side=0,
        vat_on_commission=0,
        slippage_bps_per_side=5,
    )
    breakdown = model.roundtrip_breakdown(
        1_000_000,
        buy_date="2025-06-02",
        sell_date="2025-06-09",
        market="KOSDAQ",
    )
    assert breakdown.buy_slippage == 500
    assert breakdown.sell_slippage == 500
    assert breakdown.total == 2_500  # 1,000 slippage + 1,500 sell tax


def test_net_return_requires_dates_and_market():
    model = CostModel()
    gross = 0.02
    net = model.net_return(
        gross,
        buy_date="2023-05-02",
        sell_date="2023-05-09",
        market="KOSDAQ",
    )
    assert gross - net == pytest.approx(0.00333)
    with pytest.raises(TypeError):
        model.net_return(gross)  # type: ignore[call-arg]


@pytest.mark.parametrize("market", ["OTHER", "KONEX", ""])
def test_unknown_market_fails_explicitly(market):
    with pytest.raises(ValueError, match="market"):
        tax_rates(sell_date="2024-01-02", market=market)


@pytest.mark.parametrize("sell_date", ["2020-12-31", "2027-01-01"])
def test_unverified_date_fails_explicitly(sell_date):
    with pytest.raises(ValueError, match="outside"):
        tax_rates(sell_date=sell_date, market="KOSPI")


def test_invalid_chronology_and_vector_lengths_fail():
    model = CostModel()
    with pytest.raises(ValueError, match="buy_date"):
        model.roundtrip_cost(
            1.0,
            buy_date="2025-01-03",
            sell_date="2025-01-02",
            market="KOSPI",
        )
    with pytest.raises(ValueError, match=r"zip\(\) argument"):
        model.roundtrip_cost_fractions(
            buy_dates=["2025-01-02"],
            sell_dates=["2025-01-03", "2025-01-06"],
            markets=["KOSPI"],
        )
