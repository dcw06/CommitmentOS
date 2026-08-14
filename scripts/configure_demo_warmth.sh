#!/usr/bin/env bash
# Temporarily keep one Cloud Run instance warm for the Phase 4C gate.
#
# Usage:
#   bash scripts/configure_demo_warmth.sh warm
#   bash scripts/configure_demo_warmth.sh normal
#
# `normal` restores scale-to-zero. The script makes no change until invoked.

set -euo pipefail

MODE="${1:-}"
if [[ "$MODE" != "warm" && "$MODE" != "normal" ]]; then
  echo "usage: $0 warm|normal" >&2
  exit 2
fi

ENV_FILE="${COMMITMENTOS_ENV_FILE:-.env}"
get() { grep "^COMMITMENTOS_${1}=" "$ENV_FILE" | head -1 | cut -d= -f2-; }

PROJECT="$(get GOOGLE_CLOUD_PROJECT)"
REGION="$(get GOOGLE_CLOUD_REGION)"
SERVICE="${COMMITMENTOS_CLOUD_RUN_SERVICE:-commitmentos}"
MIN_INSTANCES=0
if [[ "$MODE" == "warm" ]]; then
  MIN_INSTANCES=1
fi

gcloud run services update "$SERVICE" \
  --project "$PROJECT" \
  --region "$REGION" \
  --min-instances "$MIN_INSTANCES"

echo "$SERVICE minimum instances set to $MIN_INSTANCES in $REGION"
