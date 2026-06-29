"""Assemble the historical mock-trade dataset for the learner.

A "mock trade" is the realistic fill we validated in Loop 9: enter at the
T+1 close, exit at the T+5 close, pay the roundtrip cost. The realized net
return is the reward signal. The label is sign(net return).

For supply_contract we enrich each row with the deterministic parser's
extracted fields (ratio, contract value, counterparty type). Other
categories use the intrinsic CSV features only.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

import pandas as pd

from kdtb.backtest.cost_model import CostModel
from kdtb.learning.features import FEATURE_NAMES, extract_features


def _default_csv(category: str) -> str:
    # supply_contract has both a 2yr (event_study_results.csv) and 5yr
    # (event_study_supply_contract.csv) file; prefer the 5yr one if present.
    if category == "supply_contract":
        cat_path = "data/event_study_supply_contract.csv"
        return cat_path if Path(cat_path).exists() else "data/event_study_results.csv"
    return f"data/event_study_{category}.csv"


def load_mock_trades(
    category: str,
    db_path: str = "data/kdtb.db",
    csv_path: Optional[str] = None,
    cost_model: Optional[CostModel] = None,
) -> pd.DataFrame:
    """Return a time-sorted DataFrame of mock trades for one event category.

    Columns: FEATURE_NAMES..., event_date, realized_net_return, label.
    """
    csv_path = csv_path or _default_csv(category)
    if not Path(csv_path).exists():
        raise FileNotFoundError(f"event-study CSV not found: {csv_path}")

    df = pd.read_csv(csv_path)
    cost = (cost_model or CostModel()).roundtrip_cost(1.0)

    df = df.dropna(subset=["t+1_close", "t+5_close"]).copy()
    df = df[df["t+1_close"] > 0]
    # realistic mock trade: enter T+1 close, exit T+5 close
    df["realized_net_return"] = (df["t+5_close"] - df["t+1_close"]) / df["t+1_close"] - cost
    df["label"] = (df["realized_net_return"] > 0).astype(int)

    if category == "supply_contract" and Path(db_path).exists():
        conn = sqlite3.connect(db_path)
        try:
            ex = pd.read_sql_query(
                "SELECT disclosure_id, contract_to_revenue_ratio, contract_value_krw, "
                "counterparty_type FROM extractions "
                "WHERE model_name='deterministic_supply_contract_v1' AND validation_status='ok'",
                conn,
            )
        finally:
            conn.close()
        df = df.merge(ex, left_on="id", right_on="disclosure_id", how="left")

    feats = [extract_features(r) for r in df.to_dict("records")]
    feat_df = pd.DataFrame(feats, columns=FEATURE_NAMES, index=df.index)

    out = pd.concat(
        [feat_df, df[["event_date", "realized_net_return", "label"]]], axis=1
    )
    out["event_date"] = pd.to_datetime(out["event_date"])
    return out.sort_values("event_date").reset_index(drop=True)
