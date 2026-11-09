#!/bin/sh
set -e
# Apply database migrations, then start the API.
# One worker per container: each live call is handled by the process that accepted it.
alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*' --timeout-graceful-shutdown 300
