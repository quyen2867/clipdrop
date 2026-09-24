#!/bin/sh
set -eu
# Keep one application process: demo queues and sessions are in memory.
# No wildcard trust of X-Forwarded-For from incoming clients.
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-10000}" \
  --workers 1 --no-proxy-headers --limit-concurrency 40 --timeout-keep-alive 5
