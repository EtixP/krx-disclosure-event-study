"""Cross-category summary table — runs scripts/analyze_event_category.py
on every event category for which a CSV exists, and prints a single
comparison table + machine-readable JSON.

Run as a module (it imports a sibling in scripts/):
    python -m scripts.summarize_all_categories
    python -m scripts.summarize_all_categories --json   # JSON only, no human table
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.analyze_event_category import MIN_WINDOW_EVENTS, analyze, _human_report


CATEGORIES = [
    "supply_contract",
    "buyback",
    "bonus_issue",
    "rights_offering",
    "convertible_bond",
    "halt_resumption",
    "shareholder_change",
]


def _table_row(r: dict) -> str:
    if "error" in r:
        return f"  {r['category']:>20}  ERROR: {r['error']}"
    a = r["aggregate"]
    real_raw = r["realistic"].get("realistic", {})
    real_abnormal = r["realistic_abnormal"].get("realistic", {})
    wf_pos = sum(1 for w in r["walk_forward_abnormal"]
                 if w.get("n", 0) >= MIN_WINDOW_EVENTS and w.get("mean_pct", 0) > 0)
    wf_total = sum(1 for w in r["walk_forward_abnormal"] if w.get("n", 0) >= MIN_WINDOW_EVENTS)
    pf_abnormal = a["t5_abnormal_net"].get("pf")
    pf_real_abnormal = real_abnormal.get("pf")

    def pct(x): return f"{x:+.2f}%" if isinstance(x, (int, float)) else "n/a"
    def num(x): return f"{x:.2f}" if isinstance(x, (int, float)) else "n/a"

    return (
        f"  {r['category']:>20}  "
        f"n={r['n_events']:>5}  "
        f"T+5_raw={pct(a['t5_net'].get('mean_pct', 0)):>8}  "
        f"T+5_abn={pct(a['t5_abnormal_net'].get('mean_pct', 0)):>8}/PF{num(pf_abnormal):>5}  "
        f"real_raw={pct(real_raw.get('mean_pct', 0)):>8}  "
        f"real_abn={pct(real_abnormal.get('mean_pct', 0)):>8}/PF{num(pf_real_abnormal):>5}  "
        f"WF={wf_pos}/{wf_total}  "
        f"=> {r['verdict']}"
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    results = []
    for cat in CATEGORIES:
        csv = f"data/event_study_{cat}.csv"
        if not Path(csv).exists():
            results.append({"category": cat, "error": f"missing {csv}"})
            continue
        r = analyze(cat, csv)
        results.append(r)

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    print()
    print("=" * 160)
    print("CROSS-CATEGORY EVENT STUDY SUMMARY  (raw + KOSPI/KOSDAQ-adjusted, dated costs)")
    print(f"WF = walk-forward windows positive / windows SCORED "
          f"(a window needs >={MIN_WINDOW_EVENTS} events to be scored)")
    print("=" * 160)
    for r in results:
        print(_table_row(r))
    print()
    print("Per-category detail:")
    print()
    for r in results:
        print(_human_report(r))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
