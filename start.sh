#!/usr/bin/env bash
set -e

echo "Starting LYRA OCTAVIAN application..."

# Load environment variables if .env exists
if [ -f ".env" ]; then
  set -a
  source .env
  set +a
fi

# Start FastAPI / Uvicorn
exec python -m uvicorn app.main:app \
  --host 0.0.0.0 \
  --port ${PORT:-880} \
  --timeout-keep-alive 600

