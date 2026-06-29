"""Champion/challenger walk-forward trainer — the heart of the learning loop.

This is what makes the system "learn from mock trades and get better" *honestly*.

For each test period P (a half-year fold):
  1. A challenger model trains ONLY on folds strictly before P-1.
  2. Champion and challenger are scored on fold P-1 (the validation fold,
     which the challenger did NOT train on).
  3. The challenger is promoted to champion only if it beats the incumbent
     on that validation fold.
  4. The current champion is then scored on fold P — genuinely out-of-sample
     for every model involved.

Because step 1 never touches fold P-1 or P, and step 4's champion was chosen
without seeing fold P, there is no look-ahead. The reported per-fold PnL is
an honest estimate of how the policy would have performed live.

The champion starts as NeverTrade: do nothing until a model proves it can do
better. On data with real edge the champion becomes a LearnedPolicy and the
cumulative PnL rises; on data without edge the champion stays close to
NeverTrade and the cumulative PnL stays near zero (the correct outcome).
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
    never_pnl: float          # always 0.0, kept for explicitness
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


def make_folds(df: pd.DataFrame) -> list[tuple[str, pd.DataFrame]]:
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


def run_walk_forward(
    df: pd.DataFrame,
    min_history_folds: int = 2,
    random_state: int = 0,
    feature_cols: Optional[list[str]] = None,
) -> WalkForwardReport:
    """Run the promotion-gated replay over chronological folds."""
    feature_cols = feature_cols or FEATURE_NAMES
    folds = make_folds(df)
    report = WalkForwardReport()

    champion: Policy = NeverTrade()
    champion_version = 0

    # test on fold k; train challenger on folds[0..k-2]; validate on fold[k-1].
    for k in range(min_history_folds, len(folds)):
        train = pd.concat([folds[j][1] for j in range(k - 1)], ignore_index=True)
        val_period, val = folds[k - 1]
        test_period, test = folds[k]

        Xtr, rtr = train[feature_cols].to_numpy(float), train["realized_net_return"].to_numpy(float)
        Xval, rval = val[feature_cols].to_numpy(float), val["realized_net_return"].to_numpy(float)
        Xte, rte = test[feature_cols].to_numpy(float), test["realized_net_return"].to_numpy(float)

        challenger = LearnedPolicy(random_state=random_state).fit(Xtr, rtr)

        champ_val_pnl, _ = policy_pnl(champion, Xval, rval)
        chal_val_pnl, _ = policy_pnl(challenger, Xval, rval)
        promoted = chal_val_pnl > champ_val_pnl + 1e-12
        if promoted:
            champion = challenger
            champion_version += 1

        model_pnl, model_trades = policy_pnl(champion, Xte, rte)
        always_pnl, _ = policy_pnl(AlwaysTrade(), Xte, rte)

        report.folds.append(FoldResult(
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
            champion_is_learned=isinstance(champion, LearnedPolicy) and champion.model is not None,
        ))

    return report
