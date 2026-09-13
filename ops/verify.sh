#!/usr/bin/env bash
# Continuously decide the skills the crawler has flagged.
#
# The rules run inside the crawler, so a `critical` skill is withheld the
# moment it is stored. Everything else waits on the model, and this is the loop
# that clears that backlog — `--pending` reads the stored verdict instead of
# re-running the rules over four million skills, which is the difference
# between a loop and an eight-hour pass.
#
# Severity first, deduplicated by content. An interrupted run has decided the
# most consequential skills rather than an arbitrary prefix, which is what
# makes running this for days acceptable: protection arrives in order of how
# much it matters.
#
#   ops/verify.sh [database] [idle-seconds]
set -uo pipefail
cd "$(dirname "$0")/.."

DB="${1:-data/scale.db}"
IDLE="${2:-180}"
LOCK="logs/verify.pid"
mkdir -p logs

# One verifier at a time. Two would model the same skills concurrently and
# waste the scarcest resource here, which is inference.
if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
  echo "$(date +%F' '%H:%M) a verifier is already running ($(cat "$LOCK"))" \
    >> logs/verify.log
  exit 0
fi
echo $$ > "$LOCK"

cleanup() {
  [ -n "${VPID:-}" ] && kill "$VPID" 2>/dev/null
  if [ "$(cat "$LOCK" 2>/dev/null)" = "$$" ]; then rm -f "$LOCK"; fi
}
# A handler that does not exit lets the loop continue without its lock; EXIT
# does the cleanup, exactly once. The same mistake cost this project a 33.7 GB
# write-ahead log once already.
trap cleanup EXIT
trap 'exit 143' INT TERM

echo "$(date +%F' '%H:%M) verifier started on $DB" >> logs/verify.log

while true; do
  if ! curl -fsS --max-time 5 http://localhost:11434/api/tags >/dev/null 2>&1; then
    echo "$(date +%F' '%H:%M) no local model reachable; waiting" >> logs/verify.log
    sleep "$IDLE"
    continue
  fi

  .venv/bin/python -u analyze_corpus.py "$DB" --pending >> logs/verify.log 2>&1 &
  VPID=$!
  wait "$VPID" 2>/dev/null
  rc=$?
  VPID=""

  if [ "$rc" -ne 0 ]; then
    echo "$(date +%F' '%H:%M) pass exited $rc; backing off" >> logs/verify.log
    sleep "$IDLE"
    continue
  fi

  # Idle only when there is genuinely nothing to do. While the crawl is running
  # this rarely triggers; once it finishes, this is what keeps the loop cheap.
  if tail -3 logs/verify.log | grep -q "nothing pending"; then
    sleep "$IDLE"
  fi
done
