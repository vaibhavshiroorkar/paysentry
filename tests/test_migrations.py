import asyncpg
import pytest

from app import db as app_db
from app.db import apply_schema, init_connection
from tests.conftest import TEST_DATABASE_URL, _reset_state

INITIAL = app_db.MIGRATIONS_DIR / "001_initial.sql"


@pytest.fixture
async def pool():
    await _reset_state()
    pool = await asyncpg.create_pool(TEST_DATABASE_URL, min_size=1, max_size=2, init=init_connection)
    try:
        yield pool
    finally:
        await pool.close()


async def versions(pool) -> list[int]:
    return [row["version"] for row in await pool.fetch("SELECT version FROM schema_migrations ORDER BY version")]


async def test_fresh_database_gets_every_migration_once(pool):
    await apply_schema(pool)
    await apply_schema(pool)
    assert await versions(pool) == [1]
    assert await pool.fetchval("SELECT to_regclass('public.payments')") == "payments"


async def test_database_from_before_tracking_keeps_its_rows(pool):
    # Startup used to run the schema file directly on every boot.
    await pool.execute(INITIAL.read_text())
    await pool.execute(
        "INSERT INTO merchants (name, api_key_hash, api_key_prefix) VALUES ('Old', 'hash', 'sk_test_old')"
    )
    await apply_schema(pool)
    assert await versions(pool) == [1]
    assert await pool.fetchval("SELECT count(*) FROM merchants WHERE name = 'Old'") == 1


async def test_new_migration_runs_once_after_the_ones_already_applied(pool, tmp_path, monkeypatch):
    (tmp_path / "001_initial.sql").write_text(INITIAL.read_text())
    monkeypatch.setattr(app_db, "MIGRATIONS_DIR", tmp_path)
    await apply_schema(pool)
    (tmp_path / "002_add_notes.sql").write_text("ALTER TABLE merchants ADD COLUMN notes text;")
    await apply_schema(pool)
    await apply_schema(pool)
    assert await versions(pool) == [1, 2]
    assert await pool.fetchval(
        "SELECT count(*) FROM information_schema.columns WHERE table_name = 'merchants' AND column_name = 'notes'"
    ) == 1


async def test_failed_migration_leaves_no_trace(pool, tmp_path, monkeypatch):
    (tmp_path / "001_initial.sql").write_text(INITIAL.read_text())
    (tmp_path / "002_broken.sql").write_text("CREATE TABLE half_done (id int); SELECT no_such_function();")
    monkeypatch.setattr(app_db, "MIGRATIONS_DIR", tmp_path)
    with pytest.raises(asyncpg.UndefinedFunctionError):
        await apply_schema(pool)
    assert await versions(pool) == [1]
    assert await pool.fetchval("SELECT to_regclass('public.half_done')") is None


def test_badly_named_migration_is_refused(tmp_path, monkeypatch):
    (tmp_path / "add_notes.sql").write_text("SELECT 1;")
    monkeypatch.setattr(app_db, "MIGRATIONS_DIR", tmp_path)
    with pytest.raises(RuntimeError, match="NNN_description"):
        app_db.migrations()
