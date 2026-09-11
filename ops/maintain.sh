#!/usr/bin/env bash
# Steady-state operation, replacing the always-on sweep.
#
# The crawl has drained its productive sources. Measured by priority band, the
# leftover pool returns ~0.02-0.26 skills/repo — 720 repositories harvested for
# 187 skills, and zero across one three-minute window — so an always-running
# sweep spends bandwidth to find nothing. Discovery, by contrast, still lands
# repositories at ~45% productive when new ones appear.
#
# So the shape changes from "sweep continuously" to "discover, harvest what was
# found, wait". The sweep runs with a priority floor so it only ever touches
# material worth fetching, and exits in seconds when there is none.
cd "$(dirname "$0")/.."
export GITHUB_TOKEN="$(gh auth token)"
INTERVAL="${SKILL_ENGINE_CYCLE_SECONDS:-3600}"

# Refuse to run twice. Two of these — or one of these alongside a manually
# started crawler — means two writers on one SQLite file, which is how a
# 33.7 GB write-ahead log happened: neither connection could checkpoint past
# the other's read snapshot.
if pgrep -f "ops/maintain.sh" | grep -qv "^$$\$"; then
  others=$(pgrep -f "ops/maintain.sh" | grep -v "^$$\$" | wc -l | tr -d ' ')
  if [ "$others" -gt 0 ]; then
    echo "$(date +%F' '%H:%M) another maintain.sh is running; exiting" >> logs/maintain.log
    exit 0
  fi
fi

while true; do
  echo "$(date +%F' '%H:%M) cycle start" >> logs/maintain.log

  # Never run alongside a crawler started by hand.
  if pgrep -f "discover_hard.py" >/dev/null || pgrep -f "overnight.py" >/dev/null; then
    echo "$(date +%F' '%H:%M) a crawler is already running; skipping this cycle" \
      >> logs/maintain.log
    sleep "$INTERVAL"
    continue
  fi

  # Discovery: bounded, because it cycles its query set and finding nothing new
  # is the normal outcome now rather than a fault.
  # macOS has no `timeout`; bound the pass by killing it after 20 minutes.
  .venv/bin/python discover_hard.py data/scale.db >> logs/discover.log 2>&1 &
  dpid=$!
  ( sleep 1200; kill "$dpid" 2>/dev/null ) & killer=$!
  wait "$dpid" 2>/dev/null || true
  kill "$killer" 2>/dev/null || true

  # Harvest only what scored well enough to be worth the bandwidth.
  env SKILL_ENGINE_SWEEP_CONCURRENCY=12 SKILL_ENGINE_SWEEP_BATCH=120 \
      SKILL_ENGINE_MAX_MB=50 SKILL_ENGINE_MIN_DELAY=0.25 \
      SKILL_ENGINE_MAX_DELAY=3.0 SKILL_ENGINE_RECOVER_STEP=0.001 \
      SKILL_ENGINE_FORBIDDEN_LIMIT=5 SKILL_ENGINE_RERANK_EVERY=100000000 \
      SKILL_ENGINE_MIN_PRIORITY=110 \
      .venv/bin/python overnight.py 5000000 data/scale.db \
      >> logs/overnight.log 2>&1 || true

  # Reclaim the log while nothing holds a snapshot. This is the only moment in
  # the cycle when a checkpoint is guaranteed to succeed.
  wal_before=$(stat -f%z data/scale.db-wal 2>/dev/null || echo 0)
  sqlite3 data/scale.db "PRAGMA wal_checkpoint(TRUNCATE);" >/dev/null 2>&1 || true
  wal_after=$(stat -f%z data/scale.db-wal 2>/dev/null || echo 0)

  n=$(sqlite3 data/scale.db "SELECT COUNT(*) FROM skills;" 2>/dev/null)
  echo "$(date +%F' '%H:%M) cycle done, $n skills, wal $((wal_before/1048576))MB -> $((wal_after/1048576))MB" \
    >> logs/maintain.log
  sleep "$INTERVAL"
done
