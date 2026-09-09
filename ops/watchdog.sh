#!/usr/bin/env bash
# Keep the harvest sweep alive. Discovery is deliberately NOT supervised: the
# queue holds well over a million unharvested repositories and discovery runs
# roughly four times faster than the sweep drains it, so more discovery adds
# backlog we will never reach — and a long-running discovery process kept
# queueing at a stale priority after its code was changed, which silently
# starved the productive sources for two days.
cd "$(dirname "$0")/.."
export GITHUB_TOKEN="$(gh auth token)"
while true; do
  if ! pgrep -f "overnight.py 5000000" > /dev/null; then
    echo "$(date +%F' '%H:%M) restarting sweep" >> logs/watchdog.log
    nohup env GITHUB_TOKEN="$GITHUB_TOKEN" \
      SKILL_ENGINE_SWEEP_CONCURRENCY=12 SKILL_ENGINE_SWEEP_BATCH=120 SKILL_ENGINE_MAX_MB=50 \
      SKILL_ENGINE_MIN_DELAY=0.25 SKILL_ENGINE_MAX_DELAY=3.0 \
      SKILL_ENGINE_RECOVER_STEP=0.001 SKILL_ENGINE_FORBIDDEN_LIMIT=5 \
      SKILL_ENGINE_RERANK_EVERY=100000000 \
      .venv/bin/python overnight.py 5000000 data/scale.db >> logs/overnight.log 2>&1 &
  fi
  sleep 120
done
