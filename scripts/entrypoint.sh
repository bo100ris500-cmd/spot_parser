#!/bin/sh
set -e
echo "Waiting for Postgres..."
python - <<'PY'
import asyncio, os, sys
import asyncpg

async def wait():
    url = os.environ.get("DATABASE_URL", "")
    # asyncpg wants postgresql:// without +asyncpg
    dsn = url.replace("postgresql+asyncpg://", "postgresql://")
    for i in range(60):
        try:
            conn = await asyncpg.connect(dsn)
            await conn.close()
            print("Postgres is ready")
            return
        except Exception as e:
            print(f"wait {i}: {e}")
            await asyncio.sleep(2)
    sys.exit(1)

asyncio.run(wait())
PY

echo "Running alembic (best-effort)..."
alembic upgrade head || echo "Alembic skipped/failed; create_all will ensure schema"

exec python -m app.main
