#!/usr/bin/env bash
# Deploy skill-engine and ship the corpus to its volume.
#
# The corpus is built on your machine and uploaded as a file; the server never
# writes to it. That is why this is two steps rather than one, and why the
# container needs no GitHub credentials.
#
#   ./deploy.sh app        deploy the code only (fast, no upload)
#   ./deploy.sh data       upload the database only
#   ./deploy.sh all        both
set -euo pipefail

APP="${FLY_APP:-searchskills}"
DB="${SKILL_ENGINE_DB:-data/big.db}"
REGION="${FLY_REGION:-lhr}"
STEP="${1:-all}"

have() { command -v "$1" >/dev/null 2>&1; }
have flyctl || { echo "flyctl not found: https://fly.io/docs/flyctl/install/"; exit 1; }

deploy_app() {
  echo "==> deploying $APP"
  flyctl deploy --app "$APP" --ha=false
}

deploy_data() {
  [ -f "$DB" ] || { echo "no database at $DB"; exit 1; }

  # Compact and vacuum into a throwaway copy first. VACUUM reclaims pages freed
  # by the crawl and repacks the FTS index; on this corpus it is worth a few
  # hundred MB, which is transfer time on every single deploy.
  local staged="/tmp/skills-deploy.db"
  echo "==> preparing $DB ($(du -h "$DB" | cut -f1))"
  rm -f "$staged"
  sqlite3 "$DB" "VACUUM INTO '$staged'"
  sqlite3 "$staged" "INSERT INTO skills_fts(skills_fts) VALUES('optimize'); ANALYZE;"
  echo "    staged: $(du -h "$staged" | cut -f1)"

  # Upload compressed over SSH. sqlite files compress well — typically to about
  # a third — and the volume has the disk to spare, not the bandwidth.
  echo "==> uploading to $APP:/data/skills.db"
  gzip -c "$staged" | flyctl ssh console --app "$APP" \
    --command "sh -c 'cat > /data/skills.db.gz'"
  flyctl ssh console --app "$APP" --command \
    "sh -c 'gunzip -f /data/skills.db.gz && ls -la /data/'"

  rm -f "$staged"
  echo "==> restarting so the new corpus is picked up"
  flyctl apps restart "$APP"
}

case "$STEP" in
  app)  deploy_app ;;
  data) deploy_data ;;
  all)  deploy_app; deploy_data ;;
  *)    echo "usage: $0 [app|data|all]"; exit 1 ;;
esac

echo "==> done"
flyctl status --app "$APP" 2>/dev/null | head -20 || true
