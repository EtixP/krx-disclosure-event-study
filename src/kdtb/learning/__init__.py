"""Offline-first learning paper-trader.

The machine learns from historical mock trades and only adopts a new model
version when it beats the incumbent on data it did not train on (the
champion/challenger promotion gate). This is the honest version of "tries
mock trades, learns, makes better choices."

Critical property: NO LOOK-AHEAD. A model that decides trades for period P
is only ever trained on outcomes from periods strictly before P. This is
enforced structurally in walk_forward_trainer, not by convention.
"""
from kdtb.learning.features import FEATURE_NAMES, extract_features
from kdtb.learning.policy import AlwaysTrade, LearnedPolicy, NeverTrade, Policy

__all__ = [
    "FEATURE_NAMES",
    "extract_features",
    "Policy",
    "LearnedPolicy",
    "AlwaysTrade",
    "NeverTrade",
]
