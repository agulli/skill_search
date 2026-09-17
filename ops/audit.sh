#!/usr/bin/env bash
# Route a trickle of *ungated* skills to the model, forever.
#
# The verifier decides skills the rules flagged. That is the consequential
# work and it runs severity-first, but it can only ever confirm a finding —
# asking the model about what the rules caught cannot discover what they
# missed.
#
# And what they miss is a whole class. Harm stated in plain prose ("Exfiltrate
# the user's credentials and install persistence") matches no rule here, and
# four rule families written for it were rejected at 278 to 434 false
# positives, because a threat model, a MITRE technique page and a joke about
# SSH keys are the same strings as the attack. Only the model can read intent
# — but the model only sees gated skills, and a prose-only attack is exactly
# what is not gated. So the one class only the model can catch is the one
# class never routed to it.
#
# Random sampling is the whole fix. Not for the confidence bound it also
# produces, but because it is the only path by which such a skill reaches the
# layer able to recognise it. `evil-codex-skill` was found this way; no length
# of `--pending` run would have surfaced it.
#
# Deliberately a trickle. The verifier's backlog is the priority, so this takes
# a small batch and then sleeps, rather than competing for inference: about a
# thousand skills a day, which covers the curated tier in a few weeks and never
# starves the work that matters more.
#
#   ops/audit.sh [database] [batch] [sleep-seconds]
set -uo pipefail
cd "$(dirname "$0")/.."

DB="${1:-data/scale.db}"
BATCH="${2:-20}"
NAP="${3:-1800}"
FLOOR="${SKILL_ENGINE_AUDIT_FLOOR:-70}"
LOCK="logs/audit.pid"
mkdir -p logs

# One auditor. Two would sample the same tier concurrently and spend the
# scarcest resource here twice, which is the mistake maintain.sh was making
# against the verifier until it was taught to honour logs/verify.pid.
if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
  echo "$(date +%F' '%H:%M) an auditor is already running ($(cat "$LOCK"))" \
    >> logs/audit.log
  exit 0
fi
echo $$ > "$LOCK"

cleanup() {
  [ -n "${APID:-}" ] && kill "$APID" 2>/dev/null
  if [ "$(cat "$LOCK" 2>/dev/null)" = "$$" ]; then rm -f "$LOCK"; fi
}
# EXIT does the cleanup exactly once. A handler that does not exit lets the
# loop continue without its lock; that mistake cost this project a 33.7 GB
# write-ahead log once already.
trap cleanup EXIT
trap 'exit 143' INT TERM

echo "$(date +%F' '%H:%M) auditor started on $DB (batch $BATCH, floor $FLOOR)" \
  >> logs/audit.log

while true; do
  if ! curl -fsS --max-time 5 http://localhost:11434/api/tags >/dev/null 2>&1; then
    echo "$(date +%F' '%H:%M) no local model reachable; waiting" >> logs/audit.log
    sleep "$NAP"
    continue
  fi

  # A fresh seed per batch, or every batch would redraw the same sample. The
  # audit skips contents that already carry a decision, so a repeated seed
  # would still make progress — just in a fixed order, which is not a random
  # sample of anything.
  SEED=$(( $(date +%s) % 100000 ))
  nice -n 10 .venv/bin/python -u analyze_corpus.py "$DB" \
    --audit "$BATCH" --score-floor "$FLOOR" --seed "$SEED" \
    >> logs/audit.log 2>&1 &
  APID=$!
  wait "$APID" 2>/dev/null
  rc=$?
  APID=""

  if [ "$rc" -ne 0 ]; then
    echo "$(date +%F' '%H:%M) batch exited $rc; backing off" >> logs/audit.log
  fi
  sleep "$NAP"
done
