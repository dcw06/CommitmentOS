#!/usr/bin/env bash
# Phase 2 — Cloud Scheduler jobs for the maintenance routes (plan §17 join).
#
# Creates four OIDC-authenticated jobs against /internal/scheduler/maintenance/*:
#   dispatch_pending  every 5 minutes  (write-before-enqueue gap repair)
#   renew_watches     daily 03:10 UTC  (Gmail watch re-arm; expires every 7 days)
#   recover_cursors   every 6 hours    (quiet-window Gmail catch-up)
#   safety_reconciliation every minute (lifecycle/risk/recovery observations)
#
# Usage (repo root, after `gcloud auth login` with the admin account):
#   bash scripts/create_scheduler_jobs.sh
#
# Idempotent: existing jobs are updated in place.

set -euo pipefail

ENV_FILE="${COMMITMENTOS_ENV_FILE:-.env}"
get() { grep "^COMMITMENTOS_${1}=" "$ENV_FILE" | head -1 | cut -d= -f2-; }

PROJECT="$(get GOOGLE_CLOUD_PROJECT)"
REGION="$(get GOOGLE_CLOUD_REGION)"
BASE_URL="${COMMITMENTOS_LIVE_SERVICE_BASE_URL:-$(get SERVICE_BASE_URL)}"
SCHEDULER_SA="$(get SCHEDULER_SERVICE_ACCOUNT)"
AUDIENCE="${COMMITMENTOS_LIVE_SCHEDULER_OIDC_AUDIENCE:-$(get SCHEDULER_OIDC_AUDIENCE)}"

BASE_URL="${BASE_URL%/}"
if [[ "$BASE_URL" == *"localhost"* || "$BASE_URL" == *"127.0.0.1"* ]]; then
  echo "refusing to create cloud jobs with a local service URL" >&2
  echo "set COMMITMENTOS_LIVE_SERVICE_BASE_URL and COMMITMENTOS_LIVE_SCHEDULER_OIDC_AUDIENCE" >&2
  exit 2
fi

create_or_update() {
  local name="$1" schedule="$2" kind="$3"
  local uri="${BASE_URL}/internal/scheduler/maintenance/${kind}"
  local action="create"
  if gcloud scheduler jobs describe "$name" --project "$PROJECT" --location "$REGION" >/dev/null 2>&1; then
    action="update"
  fi
  gcloud scheduler jobs "$action" http "$name" \
    --project "$PROJECT" \
    --location "$REGION" \
    --schedule "$schedule" \
    --time-zone "Etc/UTC" \
    --uri "$uri" \
    --http-method POST \
    --oidc-service-account-email "$SCHEDULER_SA" \
    --oidc-token-audience "$AUDIENCE" \
    --attempt-deadline "300s"
  echo "${action}d: ${name} -> ${uri} (${schedule})"
}

create_or_update commitmentos-dispatch-pending "*/5 * * * *" dispatch_pending
create_or_update commitmentos-renew-watches "10 3 * * *" renew_watches
create_or_update commitmentos-recover-cursors "20 */6 * * *" recover_cursors
create_or_update commitmentos-safety-reconciliation "* * * * *" safety_reconciliation

echo
echo "Jobs in ${REGION}:"
gcloud scheduler jobs list --project "$PROJECT" --location "$REGION" | grep commitmentos || true
