#!/bin/sh
set -eu
# Keep one application process: demo queues and sessions are in memory.
# No wildcard trust of X-Forwarded-For from incoming clients.

# PO token provider (bgutil) listening on loopback only. It exists so YouTube
# accepts requests from datacenter IPs; without it the app still runs, just
# without PO tokens. Workers may reach this single loopback port. It starts by
# default when the image bundles it; CLIPDROP_POT_URL=0 turns it off.
pot_url="${CLIPDROP_POT_URL:-http://127.0.0.1:4416}"
case "$(printf %s "${CLIPDROP_POT_URL:-}" | tr 'A-Z' 'a-z')" in
  0 | off | none) pot_url='' ;;
esac
if [ -n "$pot_url" ] && [ -f /opt/bgutil/app/build/main.js ]; then
  /opt/bgutil/node --max-old-space-size=128 /opt/bgutil/app/build/main.js --host 127.0.0.1 &
  tries=0
  until python -c "import urllib.request; urllib.request.urlopen('$pot_url/ping', timeout=1)" 2>/dev/null; do
    tries=$((tries + 1))
    if [ "$tries" -ge 40 ]; then
      echo "PO token provider did not answer on $pot_url; continuing without it." >&2
      break
    fi
    sleep 0.5
  done
fi

exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-10000}" \
  --workers 1 --no-proxy-headers --limit-concurrency 40 --timeout-keep-alive 5
