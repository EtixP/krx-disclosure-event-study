from __future__ import annotations

from kdtb.backtest.cost_model import CostModel


def test_default_costs_reasonable():
    m = CostModel()
    # On ₩10M notional, roundtrip cost should be roughly 0.2-0.3% of notional
    rt = m.roundtrip_cost(10_000_000)
    pct = rt / 10_000_000
    assert 0.001 < pct < 0.005, f"unexpected cost fraction {pct:.4%}"


def test_sell_costs_higher_than_buy():
    m = CostModel()
    notional = 1_000_000
    assert m.sell_cost(notional) > m.buy_cost(notional)


def test_tx_tax_only_on_sell():
    m = CostModel(commission_per_side=0, vat_on_commission=0, slippage_bps=0)
    notional = 1_000_000
    assert m.buy_cost(notional) == 0
    assert m.sell_cost(notional) == notional * m.tx_tax_on_sell


def test_net_return_subtracts_cost_fraction():
    m = CostModel()
    gross = 0.02
    net = m.net_return(gross)
    assert net < gross
    assert abs((gross - net) - m.roundtrip_cost(1.0)) < 1e-12


def test_zero_slippage_path():
    m = CostModel(slippage_bps=0)
    notional = 1_000_000
    # Only commission+VAT on buy side
    expected = notional * m.commission_per_side * (1 + m.vat_on_commission)
    assert abs(m.buy_cost(notional) - expected) < 1e-6
