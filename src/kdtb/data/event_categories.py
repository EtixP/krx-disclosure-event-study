"""Title-based event category SQL fragments.

Shared between the event-study runner (scripts/run_event_study.py) and
the risk engine's negative-event blacklist (src/kdtb/risk/event_blacklist.py).

Each fragment is a SQL WHERE clause fragment matching the `report_name`
column. The fragment is meant to be combined with stock_code / market
filters depending on the call site.
"""
from __future__ import annotations

CATEGORIES: dict[str, str] = {
    "supply_contract":   "report_name LIKE '단일판매%' AND report_name NOT LIKE '%정정%' AND report_name NOT LIKE '%해지%'",
    "buyback":           "report_name LIKE '%자기주식%취득%' AND report_name NOT LIKE '%처분%' AND report_name NOT LIKE '%정정%'",
    "rights_offering":   "report_name LIKE '%유상증자%' AND report_name NOT LIKE '%정정%'",
    "bonus_issue":       "report_name LIKE '%무상증자%' AND report_name NOT LIKE '%정정%'",
    "convertible_bond":  "report_name LIKE '%전환사채%' AND report_name LIKE '%발행%' AND report_name NOT LIKE '%정정%'",
    "halt_resumption":   "(report_name LIKE '%매매거래정지%' OR report_name LIKE '%거래재개%')",
    "shareholder_change":"report_name LIKE '%최대주주%변경%' AND report_name NOT LIKE '%정정%'",
}

# Categories empirically shown to predict negative returns on long signals.
# Updated Loop 12 (iter 2/5) after re-validation on 4.5-year extended dataset.
#
# Loop 10 originally identified both halt_resumption and shareholder_change as
# clean negative signals on 2-year data. Loop 12 re-tested both on 4.5 years
# and found halt_resumption fails all 3 adversarial lenses:
#   - regime_consistency: only 60% windows negative (below 70% threshold);
#     2023 had two strongly positive windows (+5.81%, +10.57%)
#   - effect_size_vs_std: sharpe-ish shrank from -0.107 (24mo) to -0.026 (4.5y),
#     in the noise band; std exploded from ~24% to ~45%
#   - structural_validity: the umbrella category mixes positive sub-events
#     (bonus-issue halts +1.56%) with negative ones (rumor halts -5.99%)
#   - Realistic-fill mean is +0.05% — the "negative" edge is a slippage artifact
#
# shareholder_change survived 2 of 3 lenses with realistic-fill -1.09%, 90%
# walk-forward consistency, both markets independently negative, and most-recent
# 4 half-years intensifying (-2.41% to -3.36%).
DEFAULT_NEGATIVE_CATEGORIES: list[str] = [
    "shareholder_change",
]
