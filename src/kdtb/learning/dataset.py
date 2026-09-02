"""Assemble the historical mock-trade dataset for the learner.

A "mock trade" is the realistic fill we validated in Loop 9: enter at the
T+1 close, exit at the T+5 close, pay the roundtrip cost. Current research
uses benchmark-adjusted net return as the reward signal while retaining raw
net return alongside it. The label is the sign of the selected reward.

For supply_contract we enrich each row with the deterministic parser's
extracted fields (ratio, contract value, counterparty type). Other
categories use the intrinsic CSV features only.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal, Optional

import pandas as pd

from kdtb.backtest.cost_model import CostModel
from kdtb.data.benchmarks import require_benchmark_columns
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
    flat_cost_fraction: Optional[float] = None,
    return_basis: Literal["raw", "abnormal"] = "abnormal",
) -> pd.DataFrame:
    """Return a time-sorted DataFrame of mock trades for one event category.

    Columns: FEATURE_NAMES..., event_date, realized_net_return, label.
    """
    csv_path = csv_path or _default_csv(category)
    if not Path(csv_path).exists():
        raise FileNotFoundError(f"event-study CSV not found: {csv_path}")

    df = pd.read_csv(csv_path)
    df = df.dropna(subset=["t+1_close", "t+5_close"]).copy()
    df = df[df["t+1_close"] > 0]
    if flat_cost_fraction is not None:
        costs = pd.Series(float(flat_cost_fraction), index=df.index, dtype=float)
    else:
        required = ["t+1_date", "t+5_date", "market"]
        missing = [column for column in required if column not in df.columns]
        if missing:
            raise ValueError(
                "dated transaction costs require event-study columns: "
                + ", ".join(missing)
            )
        if df[required].isna().any().any():
            raise ValueError("dated transaction-cost inputs contain missing values")
        model = cost_model or CostModel()
        costs = pd.Series(
            model.roundtrip_cost_fractions(
                buy_dates=df["t+1_date"],
                sell_dates=df["t+5_date"],
                markets=df["market"],
            ),
            index=df.index,
            dtype=float,
        )
    if return_basis not in {"raw", "abnormal"}:
        raise ValueError(f"unsupported mock-trade return basis: {return_basis}")
    stock_gross = (df["t+5_close"] - df["t+1_close"]) / df["t+1_close"]
    df["realized_raw_net_return"] = stock_gross - costs
    if return_basis == "abnormal":
        require_benchmark_columns(df, tokens=("t1", "t5"))
        benchmark_gross = (
            (df["benchmark_t5_close"] - df["benchmark_t1_close"])
            / df["benchmark_t1_close"]
        )
        df["realized_abnormal_net_return"] = stock_gross - benchmark_gross - costs
        df["realized_net_return"] = df["realized_abnormal_net_return"]
    else:
        # The explicit raw mode exists to reproduce frozen M0.1/M0.2 artifacts.
        df["realized_abnormal_net_return"] = float("nan")
        df["realized_net_return"] = df["realized_raw_net_return"]
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
        [
            feat_df,
            df[
                [
                    "event_date",
                    "realized_raw_net_return",
                    "realized_abnormal_net_return",
                    "realized_net_return",
                    "label",
                ]
            ],
        ],
        axis=1,
    )
    out["return_basis"] = return_basis
    out["event_date"] = pd.to_datetime(out["event_date"])
    return out.sort_values("event_date").reset_index(drop=True)
