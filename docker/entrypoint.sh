#!/bin/bash
set -e

# Guard against WSL2/Docker Desktop delayed bind mount initialization
MAX_RETRIES=20
RETRY_COUNT=0

# Verify that key application files are accessible inside /app
while [ ! -f "/app/app/main.py" ] && [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
  RETRY_COUNT=$((RETRY_COUNT + 1))
  echo "[ENTRYPOINT] Waiting for /app mount to become ready... ($RETRY_COUNT/$MAX_RETRIES)"
  sleep 1
done

if [ ! -f "/app/app/main.py" ]; then
  echo "[ENTRYPOINT ERROR] /app/app/main.py not found after $MAX_RETRIES seconds. Mount is missing or unattached."
  exit 1
fi

echo "[ENTRYPOINT] Application codebase verified at /app. Launching: $@"
exec "$@"
