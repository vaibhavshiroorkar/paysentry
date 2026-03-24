import asyncio
import os
from contextlib import AsyncExitStack

import asyncpg
import httpx
import pytest
from redis.asyncio import Redis

from app.config import Settings
from app.main import create_app

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://paysentry:paysentry@localhost:5432/paysentry_test"
)
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/1")


async def _create_database_if_missing(url: str) -> None:
    server, name = url.rsplit("/", 1)
    conn = await asyncpg.connect(f"{server}/postgres")
    try:
        if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name):
            await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session", autouse=True)
def test_database():
    asyncio.run(_create_database_if_missing(TEST_DATABASE_URL))


async def _reset_state() -> None:
    conn = await asyncpg.connect(TEST_DATABASE_URL)
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    finally:
        await conn.close()
    redis = Redis.from_url(TEST_REDIS_URL)
    try:
        await redis.flushdb()
    finally:
        await redis.aclose()


@pytest.fixture
async def make_client():
    """Build an app on a fresh database. Overrides go to Settings.

    By default the rate limit is too high to matter, and every client checks
    after the test that the ledger still reconciles.
    """
    stack = AsyncExitStack()
    to_reconcile = []

    async def make(check_reconciliation: bool = True, **overrides) -> httpx.AsyncClient:
        values = {
            "database_url": TEST_DATABASE_URL,
            "redis_url": TEST_REDIS_URL,
            "rate_limit_capacity": 100_000,
            "rate_limit_refill_per_sec": 100_000,
        }
        settings = Settings(_env_file=None, **(values | overrides))
        await _reset_state()
        app = create_app(settings)
        await stack.enter_async_context(app.router.lifespan_context(app))
        client = await stack.enter_async_context(
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        )
        if check_reconciliation:
            to_reconcile.append(client)
        return client

    try:
        yield make
        for client in to_reconcile:
            report = (await client.get("/internal/reconciliation")).json()
            assert report["ok"], report["mismatches"]
    finally:
        await stack.aclose()


@pytest.fixture
async def client(make_client):
    return await make_client()


@pytest.fixture
async def db():
    conn = await asyncpg.connect(TEST_DATABASE_URL)
    yield conn
    await conn.close()
