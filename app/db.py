import json
from pathlib import Path

import asyncpg

from app.config import Settings

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
# Any constant works. It only has to be the same in every app instance.
SCHEMA_LOCK_ID = 727_001


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(settings: Settings) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        settings.database_url,
        min_size=2,
        max_size=settings.db_pool_size,
        init=_init_connection,
    )


async def apply_schema(pool: asyncpg.Pool) -> None:
    # Several instances may start at once. The advisory lock makes them take turns.
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", SCHEMA_LOCK_ID)
        try:
            await conn.execute(SCHEMA_PATH.read_text())
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", SCHEMA_LOCK_ID)
