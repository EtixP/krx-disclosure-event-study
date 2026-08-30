#!/usr/bin/env bash
# Dump data/kdtb.db to a timestamped SQL file under data/backups/.
# Keeps the last N backups; older ones are pruned. Safe to run repeatedly.
#
# Usage:
#   ./scripts/backup_db.sh                      # default: keep 7 newest
#   KEEP=14 ./scripts/backup_db.sh              # keep 14
#   ./scripts/backup_db.sh /path/to/other.db    # different source DB
#
# To run nightly via cron (macOS launchd or cron):
#   0 23 * * * cd /path/to/repo && ./scripts/backup_db.sh >> data/backups/backup.log 2>&1

set -euo pipefail

DB="${1:-data/kdtb.db}"
KEEP="${KEEP:-7}"
BACKUP_DIR="$(dirname "$DB")/backups"
TS="$(date +%Y%m%d-%H%M%S)"
OUT="$BACKUP_DIR/kdtb-$TS.sql.gz"

if [[ ! -f "$DB" ]]; then
  echo "ERROR: $DB not found" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"

# `.dump` is a logical export (SQL statements) — safer than copying the
# file while another process might be writing it.
sqlite3 "$DB" ".dump" | gzip > "$OUT"

size=$(ls -lh "$OUT" | awk '{print $5}')
echo "$(date +%Y-%m-%dT%H:%M:%S) backed up $DB -> $OUT ($size)"

# Prune old backups (keep newest $KEEP)
cd "$BACKUP_DIR"
ls -1t kdtb-*.sql.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do
  echo "  pruning $old"
  rm -f -- "$old"
done

# Show what we have now
echo "Current backups:"
ls -lht kdtb-*.sql.gz 2>/dev/null | head -"$KEEP" | awk '{print "  "$9"  "$5"  "$6, $7, $8}'
