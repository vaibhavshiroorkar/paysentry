import asyncio
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import FastAPI
from redis.asyncio import Redis

from app.config import Settings
from app.db import apply_schema, create_pool
from app.errors import register_error_handlers
from app.connectors import build_connector
from app.ratelimit import RateLimiter
from app.recovery import recovery_loop
from app.routing import Router
from app.routes import accounts, merchants, ops, payments, transfers


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    pool = await create_pool(settings)
    redis = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=settings.redis_timeout_sec,
        socket_timeout=settings.redis_timeout_sec,
    )
    transports = app.state.gateway_transports or {}
    # The connect timeout is shorter than the call deadline, so a connection
    # that never opens is reported as such (not processed) rather than as a
    # deadline miss (unknown).
    timeout = httpx.Timeout(settings.gateway_timeout_sec, connect=settings.gateway_timeout_sec / 2)
    clients = {
        name: httpx.AsyncClient(base_url=config.url, transport=transports.get(name), timeout=timeout)
        for name, config in settings.gateways.items()
    }
    loop = None
    try:
        await apply_schema(pool)
        app.state.pool = pool
        app.state.redis = redis
        app.state.limiter = RateLimiter(redis, settings)
        app.state.router = Router(redis, settings)
        app.state.gateways = {
            name: build_connector(name, config.flavour, clients[name], settings.gateway_timeout_sec)
            for name, config in settings.gateways.items()
        }
        if settings.recovery_interval_sec > 0:
            loop = asyncio.create_task(
                recovery_loop(pool, app.state.gateways, settings.recovery_interval_sec, settings.recovery_after_sec)
            )
        yield
    finally:
        if loop is not None:
            loop.cancel()
            with suppress(asyncio.CancelledError):
                await loop
        for client in clients.values():
            await client.aclose()
        await redis.aclose()
        await pool.close()


def create_app(
    settings: Settings | None = None, gateway_transports: dict[str, httpx.AsyncBaseTransport] | None = None
) -> FastAPI:
    """`gateway_transports` lets tests serve the mock gateways in-process."""
    app = FastAPI(title="PaySentry Core", version="0.3.0", lifespan=lifespan)
    app.state.settings = settings or Settings()
    app.state.gateway_transports = gateway_transports
    register_error_handlers(app)
    app.include_router(merchants.router)
    app.include_router(payments.router)
    app.include_router(accounts.router)
    app.include_router(transfers.router)
    app.include_router(ops.router)
    return app


app = create_app()
