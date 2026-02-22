#!/bin/bash
set -e

echo "Starting LYRA OCTAVIAN"

if [ -f ".env" ]; then
set -a
. ./.env
set +a
fi

exec python -m uvicorn app.main:app \
--host 0.0.0.0 \
--port ${PORT:-8080} \
--timeout-keep-alive 600
