#!/usr/bin/env bash
# Rescore the whole corpus, with every other writer stopped.
#
# A full rerank rewrites all 4.3M skill rows. Three attempts failed against
# live writers -- the first with no retry, the second after 75 seconds of
# backoff, the third after five minutes -- each time discarding roughly two
# hours of corpus statistics and author profiling before the first row landed.
#
# The lesson is not "retry harder". SQLite allows one writer, the verifier
# writes continuously, and a pass of this size cannot win that race. It is
# exclusive maintenance and belongs in a window where it is the only writer.
#
# Stops the supervisors, not just their children: verify.sh and maintain.sh
# respawn what you kill. Restarts them in a trap so an interrupted or failed
# rerank still leaves the system running -- forgetting to restart them is how
# protection work silently stops.
set -uo pipefail
cd "$(dirname "$0")/.."

log() { echo "$(date +%F' '%H:%M) $*" | tee -a logs/rerank.log; }

restart() {
  log "restarting writers"
  [ -f logs/verify.pid ] && rm -f logs/verify.pid
  [ -f logs/audit.pid ] && rm -f logs/audit.pid
  nohup bash ops/verify.sh data/scale.db   > /dev/null 2>&1 &
  nohup bash ops/audit.sh  data/scale.db 20 1800 > /dev/null 2>&1 &
  nohup bash ops/maintain.sh               > /dev/null 2>&1 &
  sleep 2
  log "writers back: $(pgrep -fc 'verify.sh|audit.sh|maintain.sh') supervisors"
}
trap restart EXIT INT TERM

log "stopping writers for an exclusive rerank"
pkill -f "ops/verify.sh"   2>/dev/null
pkill -f "ops/audit.sh"    2>/dev/null
pkill -f "ops/maintain.sh" 2>/dev/null
sleep 2
pkill -f "analyze_corpus.py" 2>/dev/null
pkill -f "overnight.py"      2>/dev/null
sleep 3
still=$(pgrep -fc "analyze_corpus.py|overnight.py|ops/verify.sh|ops/maintain.sh|ops/audit.sh" || true)
log "remaining writers: ${still:-0}"

log "rerank starting (this rewrites every row; expect a few hours)"
SKILL_ENGINE_BUSY_TIMEOUT=900 nice -n 5 .venv/bin/python -u -m skill_engine.cli \
  --db data/scale.db rank 2>&1 | tee -a logs/rerank.log
rc=${PIPESTATUS[0]}
log "rerank exited $rc"
exit "$rc"
