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

# Refuse to run twice, via a lockfile holding the loop's own PID.
#
# pgrep is not usable for this: the timeout subshell forked below is a copy of
# this script and appears in `ps` with an identical command line, so a
# pgrep-based guard both miscounts and would refuse a legitimate restart. A
# lockfile naming one PID has no such ambiguity, and checking that the PID is
# alive means a crashed loop does not lock itself out.
LOCK="logs/maintain.pid"
if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
  echo "$(date +%F' '%H:%M) maintain.sh already running as PID $(cat "$LOCK"); exiting" \
    >> logs/maintain.log
  exit 0
fi
echo $$ > "$LOCK"

# Own every child. Killing this script previously orphaned its discovery
# process, which then survived at 0% CPU for four and a half hours — and
# because the in-cycle guard saw *a* crawler running, every subsequent cycle
# was skipped while the monitor still read "loop=1, healthy".
cleanup() {
  [ -n "${DPID:-}" ] && kill "$DPID" 2>/dev/null
  [ -n "${SPID:-}" ] && kill "$SPID" 2>/dev/null
  # Only remove the lock if it is still ours. An unconditional `rm` meant a
  # dying instance deleted the *live* instance's lockfile, after which a third
  # invocation saw no lock and started a second loop — two writers on one
  # SQLite file, which is the condition that produced a 33.7 GB log.
  if [ "$(cat "$LOCK" 2>/dev/null)" = "$$" ]; then rm -f "$LOCK"; fi
}
# A signal handler that does not exit is worse than none: bash runs the handler
# and then *continues*. Trapping TERM to `cleanup` alone released the lockfile
# and left the loop running without it, so the next invocation acquired the now
# free lock — two supervisors on one SQLite file, which is how a 33.7 GB
# write-ahead log happened. The signal handlers now only exit; EXIT does the
# cleanup, exactly once.
trap cleanup EXIT
trap 'exit 143' INT TERM

# An orphaned crawler (parent is init) is owned by nobody and will never be
# stopped or bounded. Reap it rather than deferring to it forever.
for orphan in $(ps -eo pid,ppid,args | \
                awk '/python.*(discover_hard|overnight)\.py/ && $2==1 {print $1}'); do
  echo "$(date +%F' '%H:%M) reaping orphaned crawler PID $orphan" >> logs/maintain.log
  kill -9 "$orphan" 2>/dev/null
done

while true; do
  echo "$(date +%F' '%H:%M) cycle start" >> logs/maintain.log

  # Never run alongside a crawler started by hand. These match Python
  # processes, not this script, so pgrep is unambiguous here.
  if pgrep -f "python.*discover_hard.py" >/dev/null \
     || pgrep -f "python.*overnight.py" >/dev/null; then
    echo "$(date +%F' '%H:%M) a crawler is already running; skipping this cycle" \
      >> logs/maintain.log
    sleep "$INTERVAL"
    continue
  fi

  # Discovery: bounded, because it cycles its query set and finding nothing new
  # is the normal outcome now rather than a fault.
  # macOS has no `timeout`; bound the pass by killing it after 20 minutes.
  .venv/bin/python discover_hard.py data/scale.db >> logs/discover.log 2>&1 &
  DPID=$!
  # Poll for completion rather than forking a killer that dies with us.
  waited=0
  while kill -0 "$DPID" 2>/dev/null && [ "$waited" -lt 1200 ]; do
    sleep 10; waited=$((waited + 10))
  done
  if kill -0 "$DPID" 2>/dev/null; then
    echo "$(date +%F' '%H:%M) discovery exceeded 20 min; stopping it" >> logs/maintain.log
    kill -9 "$DPID" 2>/dev/null
  fi
  wait "$DPID" 2>/dev/null || true
  DPID=""

  # Harvest only what scored well enough to be worth the bandwidth.
  env SKILL_ENGINE_SWEEP_CONCURRENCY=12 SKILL_ENGINE_SWEEP_BATCH=120 \
      SKILL_ENGINE_MAX_MB=50 SKILL_ENGINE_MIN_DELAY=0.25 \
      SKILL_ENGINE_MAX_DELAY=3.0 SKILL_ENGINE_RECOVER_STEP=0.001 \
      SKILL_ENGINE_FORBIDDEN_LIMIT=5 SKILL_ENGINE_RERANK_EVERY=100000000 \
      SKILL_ENGINE_MIN_PRIORITY=110 \
      .venv/bin/python overnight.py 5000000 data/scale.db \
      >> logs/overnight.log 2>&1 & SPID=$!
  wait "$SPID" 2>/dev/null || true
  SPID=""

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
