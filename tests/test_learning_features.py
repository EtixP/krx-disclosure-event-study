from __future__ import annotations

from datetime import date, datetime

from kdtb.learning.features import FEATURE_NAMES, extract_features


def test_feature_vector_length_matches_schema():
    row = {"market": "KOSPI", "event_date": "2026-05-15", "t0_close": 10000}
    feats = extract_features(row)
    assert len(feats) == len(FEATURE_NAMES)


def test_market_is_kospi_flag():
    assert extract_features({"market": "KOSPI", "event_date": "2026-05-15"})[0] == 1.0
    assert extract_features({"market": "KOSDAQ", "event_date": "2026-05-15"})[0] == 0.0


def test_day_of_week_and_month():
    # 2026-05-15 is a Friday (weekday 4)
    feats = extract_features({"market": "KOSPI", "event_date": "2026-05-15"})
    idx_dow = FEATURE_NAMES.index("day_of_week")
    idx_month = FEATURE_NAMES.index("month")
    assert feats[idx_dow] == 4.0
    assert feats[idx_month] == 5.0


def test_handles_datetime_and_date_objects():
    f1 = extract_features({"market": "KOSPI", "event_date": datetime(2026, 5, 15)})
    f2 = extract_features({"market": "KOSPI", "event_date": date(2026, 5, 15)})
    assert f1 == f2


def test_missing_optional_fields_default_to_zero():
    feats = extract_features({"market": "KOSDAQ", "event_date": "2026-05-15"})
    idx_ratio = FEATURE_NAMES.index("contract_to_revenue_ratio")
    idx_value = FEATURE_NAMES.index("log_contract_value")
    idx_hasext = FEATURE_NAMES.index("has_extraction")
    assert feats[idx_ratio] == 0.0
    assert feats[idx_value] == 0.0
    assert feats[idx_hasext] == 0.0


def test_counterparty_ordinal_mapping():
    idx = FEATURE_NAMES.index("counterparty_ordinal")
    gov = extract_features({"market": "KOSPI", "event_date": "2026-05-15", "counterparty_type": "government"})
    foreign = extract_features({"market": "KOSPI", "event_date": "2026-05-15", "counterparty_type": "foreign"})
    assert gov[idx] == 0.0
    assert foreign[idx] == 3.0


def test_has_extraction_set_when_ratio_present():
    idx = FEATURE_NAMES.index("has_extraction")
    feats = extract_features({"market": "KOSPI", "event_date": "2026-05-15", "contract_to_revenue_ratio": 0.12})
    assert feats[idx] == 1.0


def test_log_features_safe_on_zero_and_negative():
    feats = extract_features({"market": "KOSPI", "event_date": "2026-05-15", "t0_close": 0, "contract_value_krw": -5})
    idx_close = FEATURE_NAMES.index("log_t0_close")
    idx_value = FEATURE_NAMES.index("log_contract_value")
    assert feats[idx_close] == 0.0
    assert feats[idx_value] == 0.0


def test_nan_ratio_treated_as_missing():
    feats = extract_features({"market": "KOSPI", "event_date": "2026-05-15", "contract_to_revenue_ratio": float("nan")})
    idx_ratio = FEATURE_NAMES.index("contract_to_revenue_ratio")
    idx_hasext = FEATURE_NAMES.index("has_extraction")
    assert feats[idx_ratio] == 0.0
    assert feats[idx_hasext] == 0.0
