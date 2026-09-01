"""Measure routing under gateway trouble. See docs/design/routing.md, "Simulation".

Runs the real app on real Postgres and Redis, against three mock gateways
served in-process. Each setup sends the same four phases of payments:

  1. healthy     alpha declines 5%, beta 8%, gamma 10%
  2. degraded    alpha declines 60%
  3. down        alpha answers 503 to everything
  4. recovered   back to phase 1

Needs `docker compose up -d db redis`. Run with:

  uv run python scripts/simulate_routing.py [payments_per_phase]
"""

import asyncio
import sys
import uuid
from pathlib import Path

import asyncpg
import httpx
from redis.asyncio import Redis

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from gateway.app import create_gateway_app  # noqa: E402

SERVER = "postgresql://paysentry:paysentry@localhost:5432"
DATABASE = "paysentry_sim"
REDIS_URL = "redis://localhost:6379/2"
ADMIN = {"X-Admin-Key": "sim-admin-key"}
CONCURRENCY = 10

HEALTHY = {"alpha": 0.05, "beta": 0.08, "gamma": 0.10}
PHASES = [
    ("1. All healthy", {name: {"decline_rate": rate, "unavailable_rate": 0.0} for name, rate in HEALTHY.items()}),
    ("2. Alpha declines 60%", {"alpha": {"decline_rate": 0.60}}),
    ("3. Alpha down (503)", {"alpha": {"decline_rate": 0.05, "unavailable_rate": 1.0}}),
    ("4. Alpha recovered", {"alpha": {"decline_rate": 0.05, "unavailable_rate": 0.0}}),
]
SETUPS = [
    ("Fixed priority, no breaker", {"routing_strategy": "priority", "circuit_failure_threshold": 10**9}),
    ("Fixed priority + breaker", {"routing_strategy": "priority"}),
    ("Success rate + breaker", {"routing_strategy": "success_rate"}),
]
PAYMENT = {
    "amount_minor": 10_000,
    "currency": "INR",
    "capture_method": "automatic",
    "card": {"number": "4242424242424242", "exp_month": 12, "exp_year": 2030, "cvc": "123"},
}


async def reset() -> None:
    admin = await asyncpg.connect(f"{SERVER}/postgres")
    try:
        if not await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", DATABASE):
            await admin.execute(f'CREATE DATABASE "{DATABASE}"')
    finally:
        await admin.close()
    conn = await asyncpg.connect(f"{SERVER}/{DATABASE}")
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    finally:
        await conn.close()
    redis = Redis.from_url(REDIS_URL)
    await redis.flushdb()
    await redis.aclose()


async def run_setup(overrides: dict, per_phase: int) -> list[dict]:
    await reset()
    gateway_apps = {"alpha": create_gateway_app("alpha"), "beta": create_gateway_app("beta"),
                    "gamma": create_gateway_app("alpha")}
    for seed, gateway_app in enumerate(gateway_apps.values()):
        gateway_app.state.gateway.configure(seed=seed)
    settings = Settings(
        _env_file=None,
        database_url=f"{SERVER}/{DATABASE}",
        redis_url=REDIS_URL,
        admin_api_key=ADMIN["X-Admin-Key"],
        gateways={
            "alpha": {"flavour": "alpha", "url": "http://alpha"},
            "beta": {"flavour": "beta", "url": "http://beta"},
            "gamma": {"flavour": "alpha", "url": "http://gamma"},
        },
        rate_limit_capacity=10**9,
        rate_limit_refill_per_sec=10**9,
        recovery_interval_sec=0,
        routing_seed=42,
        **overrides,
    )
    app = create_app(settings, {name: httpx.ASGITransport(app=a) for name, a in gateway_apps.items()})
    results = []
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sim") as client:
            merchant = (await client.post("/merchants", json={"name": "sim"}, headers=ADMIN)).json()
            headers = {"Authorization": f"Bearer {merchant['api_key']}"}

            async def pay() -> str:
                response = await client.post(
                    "/payments", json=PAYMENT, headers={**headers, "Idempotency-Key": str(uuid.uuid4())}
                )
                return response.json()["status"]

            for name, changes in PHASES:
                for gateway, behaviour in changes.items():
                    gateway_apps[gateway].state.gateway.configure(**behaviour)
                calls_before = sum(a.state.gateway.requests for a in gateway_apps.values())
                statuses = []
                for _ in range(per_phase // CONCURRENCY):
                    statuses += await asyncio.gather(*(pay() for _ in range(CONCURRENCY)))
                calls = sum(a.state.gateway.requests for a in gateway_apps.values()) - calls_before
                results.append(
                    {
                        "phase": name,
                        "success": statuses.count("captured") / len(statuses),
                        "calls_per_payment": calls / len(statuses),
                    }
                )

            report = (await client.get("/internal/reconciliation", headers=ADMIN)).json()
            assert report["ok"], report["mismatches"]
    return results


async def main(per_phase: int) -> None:
    table = {}
    for label, overrides in SETUPS:
        print(f"Running: {label}", file=sys.stderr)
        table[label] = await run_setup(overrides, per_phase)

    print(f"\n{per_phase} payments per phase, {CONCURRENCY} at a time.\n")
    print("Authorization success rate:\n")
    print("| Phase | " + " | ".join(table) + " |")
    print("|---|" + "---|" * len(table))
    for i, (phase, _) in enumerate(PHASES):
        print(f"| {phase} | " + " | ".join(f"{rows[i]['success']:.1%}" for rows in table.values()) + " |")
    print("\nGateway calls per payment:\n")
    print("| Phase | " + " | ".join(table) + " |")
    print("|---|" + "---|" * len(table))
    for i, (phase, _) in enumerate(PHASES):
        print(f"| {phase} | " + " | ".join(f"{rows[i]['calls_per_payment']:.2f}" for rows in table.values()) + " |")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 1000))
