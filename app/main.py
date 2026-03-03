from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from app.config import Settings
from app.db import apply_schema, create_pool
from app.errors import register_error_handlers
from app.ratelimit import RateLimiter
from app.routes import accounts, ops, transfers


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    pool = await create_pool(settings)
    redis = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=settings.redis_timeout_sec,
        socket_timeout=settings.redis_timeout_sec,
    )
    try:
        await apply_schema(pool)
        app.state.pool = pool
        app.state.redis = redis
        app.state.limiter = RateLimiter(redis, settings)
        yield
    finally:
        await redis.aclose()
        await pool.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="PaySentry Core", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings or Settings()
    register_error_handlers(app)
    app.include_router(accounts.router)
    app.include_router(transfers.router)
    app.include_router(ops.router)
    return app


app = create_app()
