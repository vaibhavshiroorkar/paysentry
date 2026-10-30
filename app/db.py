import json
import re
from pathlib import Path

import asyncpg

from app.config import Settings

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
# Any constant works. It only has to be the same in every app instance.
SCHEMA_LOCK_ID = 727_001


async def init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(settings: Settings) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        settings.database_url,
        min_size=2,
        max_size=settings.db_pool_size,
        init=init_connection,
    )


def migrations() -> list[tuple[int, Path]]:
    """Files named NNN_description.sql, in version order."""
    found = []
    for path in MIGRATIONS_DIR.glob("*.sql"):
        match = re.fullmatch(r"(\d+)_[a-z0-9_]+\.sql", path.name)
        if match is None:
            raise RuntimeError(f"Migration file {path.name} must be named NNN_description.sql")
        found.append((int(match.group(1)), path))
    versions = [version for version, _ in found]
    if len(set(versions)) != len(versions):
        raise RuntimeError("Two migration files share a version number")
    return sorted(found)


async def apply_schema(pool: asyncpg.Pool) -> None:
    """Run each migration the database has not recorded, oldest first.

    A migration and its record commit together, so a failure leaves the
    database at the last version that fully applied. Edit a migration that has
    shipped and the change never reaches a live database; add a new file instead.
    """
    # Several instances may start at once. The advisory lock makes them take turns.
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", SCHEMA_LOCK_ID)
        try:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version    integer PRIMARY KEY,
                    name       text NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            applied = {row["version"] for row in await conn.fetch("SELECT version FROM schema_migrations")}
            for version, path in migrations():
                if version in applied:
                    continue
                async with conn.transaction():
                    await conn.execute(path.read_text())
                    await conn.execute(
                        "INSERT INTO schema_migrations (version, name) VALUES ($1, $2)", version, path.stem
                    )
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", SCHEMA_LOCK_ID)
