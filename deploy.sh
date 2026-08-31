#!/usr/bin/env bash
# Deploy skill-engine and ship the corpus to its volume.
#
# The corpus is built on your machine and uploaded as a file; the server never
# writes to it. That is why this is two steps rather than one, and why the
# container needs no GitHub credentials.
#
#   ./deploy.sh build      build a release index from the crawl database
#   ./deploy.sh app        deploy the code only (fast, no upload)
#   ./deploy.sh data       upload the release index
#   ./deploy.sh all        build, deploy, upload
set -euo pipefail

APP="${FLY_APP:-searchskills}"
CRAWL_DB="${SKILL_ENGINE_CRAWL_DB:-data/scale.db}"
DB="${SKILL_ENGINE_DB:-dist/skills.db}"
REGION="${FLY_REGION:-lhr}"
STEP="${1:-all}"

have() { command -v "$1" >/dev/null 2>&1; }
have flyctl || { echo "flyctl not found: https://fly.io/docs/flyctl/install/"; exit 1; }

deploy_app() {
  echo "==> deploying $APP"
  flyctl deploy --app "$APP" --ha=false
}

build_release() {
  # A crawl database is not servable: scores are zero (reranking is disabled
  # during harvest), categories are unassigned, and full bodies make it several
  # times larger than it needs to be. release.py fixes all three and reports
  # what volume to provision.
  echo "==> building release index from $CRAWL_DB"
  python release.py "$CRAWL_DB" "$DB"
}

deploy_data() {
  [ -f "$DB" ] || { echo "no release index at $DB — run './deploy.sh build' first"; exit 1; }
  local staged="$DB"
  echo "==> uploading $DB ($(du -h "$DB" | cut -f1))"

  # Upload compressed over SSH. sqlite files compress well — typically to about
  # a third — and the volume has the disk to spare, not the bandwidth.
  echo "==> uploading to $APP:/data/skills.db"
  gzip -c "$staged" | flyctl ssh console --app "$APP" \
    --command "sh -c 'cat > /data/skills.db.gz'"
  flyctl ssh console --app "$APP" --command \
    "sh -c 'gunzip -f /data/skills.db.gz && ls -la /data/'"

  echo "==> restarting so the new corpus is picked up"
  flyctl apps restart "$APP"
}

case "$STEP" in
  build) build_release ;;
  app)   deploy_app ;;
  data)  deploy_data ;;
  all)   build_release; deploy_app; deploy_data ;;
  *)     echo "usage: $0 [build|app|data|all]"; exit 1 ;;
esac

echo "==> done"
flyctl status --app "$APP" 2>/dev/null | head -20 || true
