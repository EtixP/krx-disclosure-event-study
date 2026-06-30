#!/usr/bin/env bash
# Daily refresh for the ranker watchlist. All free data + local compute — uses
# NO Claude / LLM / API credits. Safe to run once per trading day after close.
#
# Does:
#   1. ingest today's DART disclosures (keeps the universe current)  [cheap]
#   2. refresh fundamentals IF the cache is >25 days old             [~monthly]
#   3. re-run the ranker -> data/ranker_watchlist.csv                [daily]
#
# Enable it: see scripts/com.tradeforme.daily.plist (launchd) or the cron line
# in the README. Logs to data/daily_update.log.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p data
PY=.venv/bin/python

{
  echo "=== daily update $(date '+%Y-%m-%d %H:%M:%S') ==="

  # 1. keep disclosures current (non-fatal)
  "$PY" scripts/ingest_disclosures.py --date "$(date +%Y-%m-%d)" || echo "  (ingest skipped/failed — continuing)"

  # 2. refresh fundamentals only if our cache is stale (>25 days) — quarterly data
  NEED=$("$PY" - <<'PYEOF'
import sqlite3, datetime
try:
    c = sqlite3.connect("data/kdtb.db")
    r = c.execute("SELECT MAX(fetched_at) FROM fundamentals").fetchone()[0]
    if not r:
        print("yes")
    else:
        ts = datetime.datetime.fromisoformat(r.split(".")[0])
        print("yes" if (datetime.datetime.now() - ts).days > 25 else "no")
except Exception:
    print("yes")
PYEOF
)
  if [ "$NEED" = "yes" ]; then
    echo "  fundamentals cache stale -> refreshing"
    "$PY" scripts/build_fundamentals_cache.py || echo "  (fundamentals refresh failed — using existing cache)"
  else
    echo "  fundamentals cache fresh -> skip"
  fi

  # 3. refresh the watchlist (prices + momentum + theme)
  "$PY" scripts/run_ranker.py

  echo "=== done $(date '+%H:%M:%S') ==="
} >> data/daily_update.log 2>&1

echo "daily update complete -> data/ranker_watchlist.csv  (log: data/daily_update.log)"
