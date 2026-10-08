"""Daily historical replay with maturity-purged fit, calibration and promotion.

State freezes at period starts. This is an exploratory daily-price simulation;
reconstructed dates do not establish original source availability or fills.
Private historical entry points are exposed only through the research adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from kdtb.learning.features import FEATURE_NAMES
from kdtb.learning.policy import (
    AlwaysTrade,
    LearnedPolicy,
    NeverTrade,
    Policy,
    policy_pnl,
)


@dataclass
class FoldResult:
    fold_index: int
    period: str
    train_n: int
    test_n: int
    model_pnl: float
    always_pnl: float
    never_pnl: float  # always 0.0, kept for explicitness
    model_trades: int
    promoted: bool
    champion_version: int
    champion_is_learned: bool


@dataclass
class WalkForwardReport:
    folds: list[FoldResult] = field(default_factory=list)

    @property
    def cumulative_model_pnl(self) -> float:
        return sum(f.model_pnl for f in self.folds)

    @property
    def cumulative_always_pnl(self) -> float:
        return sum(f.always_pnl for f in self.folds)

    @property
    def total_model_trades(self) -> int:
        return sum(f.model_trades for f in self.folds)

    @property
    def n_promotions(self) -> int:
        return sum(1 for f in self.folds if f.promoted)


def _make_historical_folds(df: pd.DataFrame) -> list[tuple[str, pd.DataFrame]]:
    """Split into chronological half-year folds (YYYYH1 / YYYYH2)."""
    d = df.copy()
    d["event_date"] = pd.to_datetime(d["event_date"])
    half = (d["event_date"].dt.month > 6).astype(int) + 1
    d["_period"] = d["event_date"].dt.year.astype(str) + "H" + half.astype(str)
    folds: list[tuple[str, pd.DataFrame]] = []
    # groupby sorts by the period key; 4-digit years keep the sort chronological.
    for period, sub in d.groupby("_period", sort=True):
        folds.append((str(period), sub.drop(columns="_period").reset_index(drop=True)))
    return folds


def _run_historical_walk_forward(
    df: pd.DataFrame,
    min_history_folds: int = 2,
    random_state: int = 0,
    feature_cols: Optional[list[str]] = None,
) -> WalkForwardReport:
    """Run the promotion-gated replay over chronological folds."""
    feature_cols = feature_cols or FEATURE_NAMES
    folds = _make_historical_folds(df)
    report = WalkForwardReport()

    champion: Policy = NeverTrade()
    champion_version = 0

    # test on fold k; train challenger on folds[0..k-2]; validate on fold[k-1].
    for k in range(min_history_folds, len(folds)):
        train = pd.concat([folds[j][1] for j in range(k - 1)], ignore_index=True)
        val_period, val = folds[k - 1]
        test_period, test = folds[k]

        Xtr, rtr = train[feature_cols].to_numpy(float), train[
            "realized_net_return"
        ].to_numpy(float)
        Xval, rval = val[feature_cols].to_numpy(float), val[
            "realized_net_return"
        ].to_numpy(float)
        Xte, rte = test[feature_cols].to_numpy(float), test[
            "realized_net_return"
        ].to_numpy(float)

        challenger = LearnedPolicy(random_state=random_state)._fit_historical(Xtr, rtr)

        champ_val_pnl, _ = policy_pnl(champion, Xval, rval)
        chal_val_pnl, _ = policy_pnl(challenger, Xval, rval)
        promoted = chal_val_pnl > champ_val_pnl + 1e-12
        if promoted:
            champion = challenger
            champion_version += 1

        model_pnl, model_trades = policy_pnl(champion, Xte, rte)
        always_pnl, _ = policy_pnl(AlwaysTrade(), Xte, rte)

        report.folds.append(
            FoldResult(
                fold_index=k,
                period=test_period,
                train_n=len(train),
                test_n=len(test),
                model_pnl=model_pnl,
                always_pnl=always_pnl,
                never_pnl=0.0,
                model_trades=model_trades,
                promoted=promoted,
                champion_version=champion_version,
                champion_is_learned=isinstance(champion, LearnedPolicy)
                and champion.model is not None,
            )
        )

    return report


@dataclass
class TemporalFoldResult(FoldResult):
    validation_period_start: str
    test_period_start: str
    train_candidates_n: int
    promotion_candidates_n: int
    promotion_n: int
    promotion_purged_n: int
    promotion_status: str
    promotion_row_ids: list[str]
    fit_diagnostics: dict
    challenger_threshold: float
    challenger_state_sha256: str
    champion_state_sha256: str
    champion_trained_before: str | None
    champion_selected_before: str | None
    test_decisions: list[dict]


def period_start(period: str) -> pd.Timestamp:
    return pd.Timestamp(f"{period[:4]}-{'01' if period[-1] == '1' else '07'}-01")


def make_folds(df: pd.DataFrame) -> list[tuple[str, pd.DataFrame]]:
    """Half-year cohorts by decision day; require chronology before grouping."""
    from kdtb.learning.chronology import require_chronology

    d = require_chronology(df)
    half = (d["decision_date"].dt.month > 6).astype(int) + 1
    d["_period"] = d["decision_date"].dt.year.astype(str) + "H" + half.astype(str)
    return [
        (str(period), group.drop(columns="_period").reset_index(drop=True))
        for period, group in d.groupby("_period", sort=True)
    ]


def run_walk_forward(
    df: pd.DataFrame,
    min_history_folds: int = 2,
    random_state: int = 0,
    feature_cols: Optional[list[str]] = None,
) -> WalkForwardReport:
    """Strict daily replay; a return basis never selects historical chronology."""
    import numpy as np

    feature_cols = feature_cols or FEATURE_NAMES
    folds = make_folds(df)
    if min_history_folds < 2:
        raise ValueError("need at least two history folds for fit and promotion")
    if (
        not np.isfinite(df[feature_cols].to_numpy(float)).all()
        or not np.isfinite(df["realized_net_return"].to_numpy(float)).all()
    ):
        raise ValueError("learner features and rewards must be finite")
    report = WalkForwardReport()
    champion: Policy = NeverTrade()
    champion_version = 0
    champion_trained_before = None
    champion_selected_before = None
    for k in range(min_history_folds, len(folds)):
        train = pd.concat([folds[j][1] for j in range(k - 1)], ignore_index=True)
        val_period, val_candidates = folds[k - 1]
        test_period, test = folds[k]
        validation_cutoff = period_start(val_period)
        test_cutoff = period_start(test_period)
        promotion = val_candidates[val_candidates["label_end_date"] < test_cutoff]
        challenger = LearnedPolicy(random_state=random_state).fit(
            train[feature_cols].to_numpy(float),
            train["realized_net_return"].to_numpy(float),
            decision_dates=train["decision_date"],
            label_end_dates=train["label_end_date"],
            row_ids=train["row_id"],
            cutoff=validation_cutoff,
        )
        promoted = False
        promotion_status = "no_mature_promotion_labels"
        if len(promotion):
            Xval = promotion[feature_cols].to_numpy(float)
            rval = promotion["realized_net_return"].to_numpy(float)
            champ_pnl, _ = policy_pnl(champion, Xval, rval)
            chal_pnl, _ = policy_pnl(challenger, Xval, rval)
            promoted = chal_pnl > champ_pnl + 1e-12
            promotion_status = "promoted" if promoted else "incumbent_retained"
        if promoted:
            champion = challenger
            champion_version += 1
            champion_trained_before = str(validation_cutoff.date())
            champion_selected_before = str(test_cutoff.date())
        Xte = test[feature_cols].to_numpy(float)
        rte = test["realized_net_return"].to_numpy(float)
        decisions = champion.decide(Xte)
        model_pnl, model_trades = policy_pnl(champion, Xte, rte)
        always_pnl, _ = policy_pnl(AlwaysTrade(), Xte, rte)
        report.folds.append(
            TemporalFoldResult(
                fold_index=k,
                period=test_period,
                train_n=challenger.diagnostics["fit_n"],
                test_n=len(test),
                model_pnl=model_pnl,
                always_pnl=always_pnl,
                never_pnl=0.0,
                model_trades=model_trades,
                promoted=promoted,
                champion_version=champion_version,
                champion_is_learned=isinstance(champion, LearnedPolicy)
                and champion.model is not None,
                validation_period_start=str(validation_cutoff.date()),
                test_period_start=str(test_cutoff.date()),
                train_candidates_n=len(train),
                promotion_candidates_n=len(val_candidates),
                promotion_n=len(promotion),
                promotion_purged_n=len(val_candidates) - len(promotion),
                promotion_status=promotion_status,
                promotion_row_ids=promotion["row_id"].tolist(),
                fit_diagnostics=challenger.diagnostics,
                challenger_threshold=challenger.threshold,
                challenger_state_sha256=challenger.state_fingerprint(),
                champion_state_sha256=(
                    champion.state_fingerprint()
                    if isinstance(champion, LearnedPolicy)
                    else "never_trade"
                ),
                champion_trained_before=champion_trained_before,
                champion_selected_before=champion_selected_before,
                test_decisions=[
                    {
                        "row_id": key,
                        "decision_date": str(day.date()),
                        "trade": bool(trade),
                    }
                    for key, day, trade in zip(
                        test["row_id"], test["decision_date"], decisions
                    )
                ],
            )
        )
    return report
