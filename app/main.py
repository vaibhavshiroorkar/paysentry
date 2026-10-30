import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis

from app.config import GatewayConfig, Settings
from app.db import apply_schema, create_pool
from app.errors import register_error_handlers
from app.connectors import build_connector
from app.ratelimit import RateLimiter
from app.recovery import recovery_loop
from app.routing import Router
from app.routes import accounts, merchants, ops, payments, transfers


def gateway_headers(settings: Settings, config: GatewayConfig) -> dict[str, str]:
    if config.flavour == "stripe" and settings.stripe_secret_key is not None:
        return {"Authorization": f"Bearer {settings.stripe_secret_key.get_secret_value()}"}
    return {}


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
    if settings.demo_embedded_gateways:
        from gateway.app import create_gateway_app

        transports = {
            name: httpx.ASGITransport(app=create_gateway_app(config.flavour))
            for name, config in settings.gateways.items()
            if config.flavour != "stripe"
        }
    # The connect timeout is shorter than the call deadline, so a connection
    # that never opens is reported as such (not processed) rather than as a
    # deadline miss (unknown).
    timeout = httpx.Timeout(settings.gateway_timeout_sec, connect=settings.gateway_timeout_sec / 2)
    clients = {
        name: httpx.AsyncClient(
            base_url=config.url, transport=transports.get(name), timeout=timeout, headers=gateway_headers(settings, config)
        )
        for name, config in settings.gateways.items()
    }
    loop = None
    try:
        await apply_schema(pool)
        app.state.pool = pool
        app.state.redis = redis
        app.state.limiter = RateLimiter(redis, settings)
        if settings.demo_enabled:
            app.state.demo_boot_at = await pool.fetchval("SELECT clock_timestamp()")
            app.state.demo_limiter = RateLimiter(redis, settings.model_copy(update={"rate_limit_fail_open": False}))
            app.state.demo_session_limiter = RateLimiter(
                redis, settings.model_copy(update={
                    "rate_limit_capacity": 3, "rate_limit_refill_per_sec": 1 / 60, "rate_limit_fail_open": False,
                }),
            )
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
    app = FastAPI(title="PaySentry Core", version="0.4.0", lifespan=lifespan)
    app.state.settings = settings or Settings()
    # Uvicorn configures only its own loggers, so without this the recovery
    # loop's records never reach the host's log stream.
    logging.basicConfig(level=app.state.settings.log_level, format="%(levelname)s %(name)s: %(message)s")
    app.state.gateway_transports = gateway_transports
    register_error_handlers(app)
    app.include_router(merchants.router)
    app.include_router(payments.router)
    app.include_router(accounts.router)
    app.include_router(transfers.router)
    app.include_router(ops.router)
    if app.state.settings.demo_enabled:
        from app.demo import router as demo_router

        static = Path(__file__).with_name("static")
        app.include_router(demo_router)
        app.mount("/demo-assets", StaticFiles(directory=static, check_dir=False), name="demo-assets")

        @app.get("/", include_in_schema=False)
        async def demo_page():
            return FileResponse(static / "demo.html")

        @app.middleware("http")
        async def demo_headers(request, call_next):
            response = await call_next(request)
            path = request.url.path
            # /docs loads Swagger UI from a CDN, so the strict policy covers only the demo.
            if path == "/" or path.startswith(("/demo/", "/demo-assets/")):
                response.headers["X-Content-Type-Options"] = "nosniff"
                response.headers["Referrer-Policy"] = "same-origin"
                response.headers["Content-Security-Policy"] = (
                    "default-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; object-src 'none'; form-action 'self'"
                )
            if path == "/" or path.startswith("/demo/"):
                response.headers["Cache-Control"] = "no-store"
            return response
    return app


app = create_app()
