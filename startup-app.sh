#!/usr/bin/env bash
# Uppcoder runs this instead of the default `uvicorn main:app --reload`
# when present in the app folder. Keep the process in the foreground.
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONUNBUFFERED=1
# Prefer project venv tools if the platform provides them; fall back to PATH.
if command -v uvicorn >/dev/null 2>&1; then
  UVICORN=uvicorn
elif [ -x .venv/bin/uvicorn ]; then
  UVICORN=.venv/bin/uvicorn
else
  UVICORN="python3 -m uvicorn"
fi
# Bind as the platform proxy expects (same as starter: reload for live edits).
exec $UVICORN main:app --host 127.0.0.1 --port 8000 --reload
