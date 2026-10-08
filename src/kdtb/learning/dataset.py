"""Strict daily historical learner admission with retained source identity.

The public loader enforces reconstructed T0/T+1/T+5 windows against pinned
observed benchmark sessions. Historical reproduction is explicitly separate.
A close-price simulation is not an observed fill or original-vintage source.
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


def _load_historical_mock_trades(
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
        benchmark_gross = (df["benchmark_t5_close"] - df["benchmark_t1_close"]) / df[
            "benchmark_t1_close"
        ]
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


def load_mock_trades(
    category: str,
    db_path: str = "data/kdtb.db",
    csv_path: Optional[str] = None,
    cost_model: Optional[CostModel] = None,
    flat_cost_fraction: Optional[float] = None,
    return_basis: Literal["raw", "abnormal"] = "abnormal",
    *,
    calendar_path: str = "data/benchmark_indices.csv",
    calendar_sha256: str | None = None,
) -> pd.DataFrame:
    """Strict daily research admission, with row quarantine in ``attrs['admission']``.

    Raw reward selection never enables legacy chronology. Supply-contract
    enrichment has no original-version provenance and is refused even with a
    trusted DB hash; this path never opens a database. Reconstructed T0 feature
    dates are a daily modeling constraint, not proof of original availability.
    """
    import hashlib
    import json
    from collections import Counter

    import numpy as np

    from kdtb.learning.chronology import (
        IDENTITY_COLUMNS,
        SOURCE_DATES,
        ObservedSessionHorizon,
        PINNED_BENCHMARK_SHA256,
        daily_date,
        require_chronology,
        sha256,
    )

    if category == "supply_contract":
        raise ValueError(
            "supply_contract_unversioned_enrichment: strict learner unavailable; "
            "original-version extraction provenance is required. Use buyback for "
            "the bounded daily research replay; do not restore or enrich from the current DB."
        )
    if return_basis not in {"raw", "abnormal"}:
        raise ValueError(f"unsupported mock-trade return basis: {return_basis}")
    path = Path(csv_path or _default_csv(category))
    df = pd.read_csv(path, dtype={key: "string" for key in IDENTITY_COLUMNS})
    required = {
        *IDENTITY_COLUMNS,
        *SOURCE_DATES,
        "market",
        "t0_close",
        "t+1_close",
        "t+5_close",
    }
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError("strict learner input requires columns: " + ", ".join(missing))
    horizon = ObservedSessionHorizon(
        calendar_path, expected_sha256=calendar_sha256 or PINNED_BENCHMARK_SHA256
    )
    if return_basis == "abnormal":
        # Schema failure is explicit even if all rows would otherwise quarantine.
        require_benchmark_columns(df.iloc[:0], tokens=("t1", "t5"))
    duplicates = df["receipt_no"].duplicated(keep=False) | df["id"].duplicated(
        keep=False
    )
    admitted = []
    excluded = []
    for index, row in df.iterrows():
        receipt = row["receipt_no"]
        row_id = (
            f"{category}:{receipt}"
            if pd.notna(receipt)
            else "missing:"
            + hashlib.sha256(
                json.dumps(
                    {str(k): str(v) for k, v in row.items()}, sort_keys=True
                ).encode()
            ).hexdigest()
        )
        reason = None
        dates = {}
        if any(
            pd.isna(row[key]) or not str(row[key]).strip() for key in IDENTITY_COLUMNS
        ):
            reason = "missing_identity"
        elif duplicates.loc[index]:
            reason = "duplicate_identity"
        else:
            try:
                dates = {key: daily_date(row[key]) for key in SOURCE_DATES}
            except ValueError as exc:
                reason = str(exc)
        if reason is None:
            ev, t0, entry, end = (dates[key] for key in SOURCE_DATES)
            if not ev <= t0 < entry < end:
                reason = "reversed_window"
            else:
                try:
                    expected = horizon.window(str(row["market"]), ev)
                    if t0 != expected[0]:
                        reason = "t0_session_mismatch"
                    elif entry != expected[1]:
                        reason = "entry_session_mismatch"
                    elif end != expected[2]:
                        reason = "label_session_mismatch"
                except ValueError as exc:
                    reason = str(exc)
        if reason is None:
            try:
                prices = [
                    float(row[key]) for key in ("t0_close", "t+1_close", "t+5_close")
                ]
                if not all(np.isfinite(value) and value > 0 for value in prices):
                    reason = "invalid_stock_price"
                if pd.notna(row.get("error")) and str(row.get("error")).strip():
                    reason = "source_row_error"
                if return_basis == "abnormal":
                    require_benchmark_columns(pd.DataFrame([row]), tokens=("t1", "t5"))
            except (TypeError, ValueError):
                reason = "invalid_price_or_benchmark"
        if reason is not None:
            excluded.append(
                {
                    "row_id": row_id,
                    "receipt_no": None if pd.isna(receipt) else str(receipt),
                    "reason": reason,
                    **{
                        key: None if pd.isna(row[key]) else str(row[key])
                        for key in SOURCE_DATES
                    },
                }
            )
            continue
        gross = (float(row["t+5_close"]) - float(row["t+1_close"])) / float(
            row["t+1_close"]
        )
        cost = (
            float(flat_cost_fraction)
            if flat_cost_fraction is not None
            else (
                (cost_model or CostModel()).roundtrip_cost(
                    1.0,
                    buy_date=dates["t+1_date"],
                    sell_date=dates["t+5_date"],
                    market=row["market"],
                )
            )
        )
        if not np.isfinite(cost) or cost < 0:
            raise ValueError("invalid_transaction_cost")
        benchmark = (
            (float(row["benchmark_t5_close"]) - float(row["benchmark_t1_close"]))
            / float(row["benchmark_t1_close"])
            if return_basis == "abnormal"
            else float("nan")
        )
        raw = gross - cost
        abnormal = gross - benchmark - cost
        reward = abnormal if return_basis == "abnormal" else raw
        record = {
            **dict(zip(FEATURE_NAMES, extract_features(row.to_dict()))),
            **{key: str(row[key]) for key in IDENTITY_COLUMNS},
            **dates,
            "event_id": str(row["id"]),
            "issuer_id": str(row["corp_code"]),
            "row_id": row_id,
            "market": str(row["market"]),
            "feature_observation_date": dates["t0_date"],
            "entry_date": dates["t+1_date"],
            "decision_date": dates["t+1_date"],
            "label_end_date": dates["t+5_date"],
            "realized_raw_net_return": raw,
            "realized_abnormal_net_return": abnormal,
            "realized_net_return": reward,
            "label": int(reward > 0),
            "return_basis": return_basis,
        }
        admitted.append(record)
    columns = [
        *FEATURE_NAMES,
        *IDENTITY_COLUMNS,
        *SOURCE_DATES,
        "event_id",
        "issuer_id",
        "row_id",
        "market",
        "feature_observation_date",
        "entry_date",
        "decision_date",
        "label_end_date",
        "realized_raw_net_return",
        "realized_abnormal_net_return",
        "realized_net_return",
        "label",
        "return_basis",
    ]
    out = require_chronology(pd.DataFrame(admitted, columns=columns))
    out.attrs["admission"] = {
        "schema_version": "m0.6_daily_chronology_v1",
        "input_sha256": sha256(path),
        "calendar": horizon.metadata(),
        "input_rows": len(df),
        "admitted_rows": len(out),
        "excluded_rows": len(excluded),
        "reason_counts": dict(
            sorted(Counter(row["reason"] for row in excluded).items())
        ),
        "exclusions": sorted(
            excluded,
            key=lambda row: (
                row["row_id"],
                row["reason"],
                json.dumps(row, sort_keys=True),
            ),
        ),
        "availability_limit": "reconstructed daily observation dates; original source availability and intraday fills unproven",
        "db_used": False,
    }
    return out
