#!/bin/zsh
set -e
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo 'Chưa có môi trường Python. Làm theo các bước cài đặt trong README.md trước.'
  exit 1
fi
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
