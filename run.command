#!/bin/zsh
set -e
cd "$(dirname "$0")"
if [[ -x .venv/bin/python ]]; then
  exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8765
elif which python3 >/dev/null 2>&1; then
  exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8765
else
  echo 'Chưa có môi trường Python. Làm theo các bước cài đặt trong README.md trước.'
  exit 1
fi
