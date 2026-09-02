from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

import kdtb.data.market_data_client as market_data_module
from kdtb.data.market_data_client import MarketDataClient, PriceAdjustment


def _make_prices(rows: list[tuple[str, float]]) -> pd.DataFrame:
    idx = pd.to_datetime([r[0] for r in rows]).normalize()
    df = pd.DataFrame({"close": [r[1] for r in rows]}, index=idx)
    df.index.name = "date"
    return df


@pytest.mark.parametrize(
    ("adjustment", "expected_flag", "expected_source"),
    [
        (
            PriceAdjustment.VENDOR_ADJUSTED,
            True,
            "NAVER_FINANCE_VIA_PYKRX",
        ),
        (PriceAdjustment.UNADJUSTED, False, "KRX_VIA_PYKRX"),
    ],
)
def test_fetch_ohlcv_passes_explicit_adjustment_to_pykrx(
    monkeypatch, adjustment, expected_flag, expected_source
):
    calls = []

    def fake_fetch(*args, **kwargs):
        calls.append((args, kwargs))
        return pd.DataFrame(
            {"시가": [99], "고가": [102], "저가": [98], "종가": [101], "거래량": [7]},
            index=pd.to_datetime(["2026-02-26"]),
        )

    monkeypatch.setattr(
        market_data_module,
        "get_market_ohlcv_by_date",
        fake_fetch,
    )
    result = MarketDataClient().fetch_ohlcv(
        "300120",
        date(2026, 2, 26),
        date(2026, 2, 27),
        adjustment=adjustment,
    )

    assert calls == [
        (
            ("20260226", "20260227", "300120"),
            {"freq": "d", "adjusted": expected_flag, "name_display": False},
        )
    ]
    assert result.attrs == {
        "price_adjustment": adjustment.value,
        "price_source": expected_source,
    }


def test_fetch_ohlcv_has_no_implicit_adjustment(monkeypatch):
    monkeypatch.setattr(
        market_data_module,
        "get_market_ohlcv_by_date",
        lambda *args, **kwargs: pytest.fail("provider must not be called"),
    )
    with pytest.raises(TypeError, match="adjustment"):
        MarketDataClient().fetch_ohlcv(  # type: ignore[call-arg]
            "300120",
            date(2026, 2, 26),
            date(2026, 2, 27),
        )


def test_fetch_ohlcv_rejects_non_enum_adjustment(monkeypatch):
    monkeypatch.setattr(
        market_data_module,
        "get_market_ohlcv_by_date",
        lambda *args, **kwargs: pytest.fail("provider must not be called"),
    )
    with pytest.raises(TypeError, match="PriceAdjustment"):
        MarketDataClient().fetch_ohlcv(
            "300120",
            date(2026, 2, 26),
            date(2026, 2, 27),
            adjustment="vendor_adjusted",  # type: ignore[arg-type]
        )


def test_uniform_corporate_action_scale_revision_preserves_returns():
    """Regression for the observed 300120 fivefold vendor revision."""
    stored = _make_prices(
        [
            ("2026-02-26", 1607.0),
            ("2026-02-27", 1735.0),
            ("2026-03-03", 1585.0),
            ("2026-03-04", 1406.0),
            ("2026-03-05", 1549.0),
            ("2026-03-06", 1089.0),
        ]
    )
    refetched = stored.copy()
    refetched["close"] *= 5

    stored_returns = MarketDataClient.event_returns(stored, date(2026, 2, 26))
    fresh_returns = MarketDataClient.event_returns(refetched, date(2026, 2, 26))

    assert fresh_returns["t0_close"] == 8035.0
    assert fresh_returns["t+5_close"] == 5445.0
    for field in ("ret_1d", "ret_2d", "ret_5d"):
        assert fresh_returns[field] == pytest.approx(stored_returns[field])


def test_event_returns_basic():
    prices = _make_prices([
        ("2026-05-26", 100.0),
        ("2026-05-27", 110.0),  # t0
        ("2026-05-28", 115.5),  # t+1 → +5%
        ("2026-05-29", 121.0),  # t+2 → +10%
        ("2026-06-01", 99.0),
        ("2026-06-02", 121.0),
        ("2026-06-03", 132.0),  # t+5 → +20%
    ])
    out = MarketDataClient.event_returns(prices, date(2026, 5, 27))
    assert out["t0_date"] == "2026-05-27"
    assert out["t+1_date"] == "2026-05-28"
    assert out["t+2_date"] == "2026-05-29"
    assert out["t+5_date"] == "2026-06-03"
    assert out["t0_close"] == 110.0
    assert abs(out["ret_1d"] - 0.05) < 1e-9
    assert abs(out["ret_2d"] - 0.10) < 1e-9
    assert abs(out["ret_5d"] - 0.20) < 1e-9


def test_event_returns_handles_event_on_non_trading_day():
    # event date is Saturday; first trading day after is Monday
    prices = _make_prices([
        ("2026-05-29", 100.0),  # Fri before
        ("2026-06-01", 105.0),  # Mon — first on-or-after Saturday
        ("2026-06-02", 110.0),  # Tue
    ])
    out = MarketDataClient.event_returns(prices, date(2026, 5, 30))  # Sat
    assert out["t0_date"] == "2026-06-01"
    assert out["t+1_date"] == "2026-06-02"
    assert out["t0_close"] == 105.0
    assert abs(out["ret_1d"] - (110.0 / 105.0 - 1)) < 1e-9


def test_event_returns_with_missing_future_data():
    prices = _make_prices([
        ("2026-05-27", 100.0),  # only t0
    ])
    out = MarketDataClient.event_returns(prices, date(2026, 5, 27))
    assert out["t0_close"] == 100.0
    assert out["ret_1d"] is None
    assert out["ret_5d"] is None


def test_event_returns_on_empty_frame():
    out = MarketDataClient.event_returns(pd.DataFrame(), date(2026, 5, 27))
    assert all(v is None for v in out.values())


def test_event_returns_event_before_all_data():
    prices = _make_prices([("2026-05-27", 100.0), ("2026-05-28", 105.0)])
    # Event date is before the first row → first on-or-after is the first row
    out = MarketDataClient.event_returns(prices, date(2026, 5, 26))
    assert out["t0_close"] == 100.0
    assert abs(out["ret_1d"] - 0.05) < 1e-9
