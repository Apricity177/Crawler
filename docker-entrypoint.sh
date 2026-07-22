#!/bin/sh
set -eu

exec python3 opportunity_dashboard.py \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --data-dir "${DATA_DIR:-/app/data}" \
  --daily-at "${DAILY_AT:-08:30}" \
  --crawler-config "${CRAWLER_CONFIG:-/app/config.example.json}" \
  --env-file "${ENV_FILE:-/app/.env}"
