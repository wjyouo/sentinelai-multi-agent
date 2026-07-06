#!/bin/bash
set -e

echo "==> SentinelAI backend starting"
echo "==> Waiting for MySQL at ${DB_HOST}:${DB_PORT}..."

# Poll MySQL using pymysql (already in requirements.txt)
DB_WAIT_TIMEOUT="${DB_WAIT_TIMEOUT:-30}"
DB_WAIT_ELAPSED=0
DB_READY=0

while [ "$DB_WAIT_ELAPSED" -lt "$DB_WAIT_TIMEOUT" ]; do
if python3 -c "
import os, sys
try:
    import pymysql
    conn = pymysql.connect(
        host=os.environ['DB_HOST'],
        port=int(os.environ.get('DB_PORT', 3306)),
        user=os.environ['DB_USER'],
        password=os.environ['DB_PASSWORD'],
        database=os.environ['DB_NAME'],
        connect_timeout=5,
    )
    conn.close()
except Exception as e:
    sys.exit(1)
" 2>/dev/null; then
    DB_READY=1
    break
fi
echo "    MySQL not ready, retrying in 2s..."
sleep 2
DB_WAIT_ELAPSED=$((DB_WAIT_ELAPSED + 2))
done

if [ "$DB_READY" = "1" ]; then
    echo "==> MySQL is ready. Initializing database tables..."

    if python3 tools/SentinelSpider/schema/init_database.py; then
        echo "==> Database initialization complete."
    else
        echo "==> Database initialization failed. Continuing with DB-dependent features degraded."
    fi
else
    echo "==> MySQL not ready after ${DB_WAIT_TIMEOUT}s. Starting FastAPI with DB-dependent features degraded."
fi
echo "==> Starting FastAPI on ${HOST:-0.0.0.0}:${PORT:-5000}..."

exec uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-5000}" --log-level info
