"""Measure routing under gateway trouble. See docs/design/routing.md, "Simulation".

Runs the real app on real Postgres and Redis, against three mock gateways
served in-process. Two scenarios:

Outages: one card type, and alpha's health changes in four phases.
  1. healthy     alpha declines 5%, beta 8%, gamma 10%
  2. degraded    alpha declines 60%
  3. down        alpha answers 503 to everything
  4. recovered   back to phase 1

Segments: mixed traffic, and each gateway is good at different payments.
  alpha  visa 5%, mastercard 45%, amex 10%, at least 5,000: at least 40%
  beta   visa 35%, mastercard 6%, amex 50%
  gamma  12% on everything
  Then alpha's visa declines rise to 50%.

Needs `docker compose up -d db redis`. Run with:

  uv run python scripts/simulate_routing.py [payments_per_phase]
"""

import asyncio
import random
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

CARDS = {"visa": "4242424242424242", "mastercard": "5555555555554444", "amex": "378282246310005"}

CLEAR = {"decline_rate": 0.0, "unavailable_rate": 0.0, "decline_by_brand": {}, "large_amount_minor": None}
OUTAGES = [
    ("1. All healthy", {"alpha": {**CLEAR, "decline_rate": 0.05}, "beta": {**CLEAR, "decline_rate": 0.08},
                        "gamma": {**CLEAR, "decline_rate": 0.10}}),
    ("2. Alpha declines 60%", {"alpha": {"decline_rate": 0.60}}),
    ("3. Alpha down (503)", {"alpha": {"decline_rate": 0.05, "unavailable_rate": 1.0}}),
    ("4. Alpha recovered", {"alpha": {"decline_rate": 0.05, "unavailable_rate": 0.0}}),
]
SEGMENTS = [
    ("1. Gateways differ by segment", {
        "alpha": {**CLEAR, "decline_rate": 0.10, "decline_by_brand": {"visa": 0.05, "mastercard": 0.45},
                  "large_amount_minor": 500_000, "large_amount_decline_rate": 0.40},
        "beta": {**CLEAR, "decline_by_brand": {"visa": 0.35, "mastercard": 0.06, "amex": 0.50}},
        "gamma": {**CLEAR, "decline_rate": 0.12},
    }),
    ("2. Alpha worsens on visa", {"alpha": {"decline_by_brand": {"visa": 0.50, "mastercard": 0.45}}}),
]

BREAKER_OFF = {"circuit_failure_threshold": 10**9}
OUTAGE_SETUPS = [
    ("Fixed priority, no breaker", {"routing_strategy": "priority", **BREAKER_OFF}),
    ("Fixed priority + breaker", {"routing_strategy": "priority"}),
    ("Success rate + breaker", {"routing_strategy": "success_rate"}),
    ("Contextual + breaker", {"routing_strategy": "contextual"}),
]
SEGMENT_SETUPS = [
    ("Fixed priority", {"routing_strategy": "priority"}),
    ("Success rate", {"routing_strategy": "success_rate"}),
    ("Contextual (LinUCB)", {"routing_strategy": "contextual"}),
]


def single_card(rng: random.Random) -> tuple[str, int]:
    return "visa", 10_000


def mixed_traffic(rng: random.Random) -> tuple[str, int]:
    """50% visa, 40% mastercard, 10% amex. 70% of amounts under 5,000, the rest up to 60,000."""
    brand = rng.choices(["visa", "mastercard", "amex"], weights=[50, 40, 10])[0]
    amount = rng.randint(10_000, 499_999) if rng.random() < 0.7 else rng.randint(500_000, 6_000_000)
    return brand, amount


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


async def run_setup(overrides: dict, phases: list, traffic, per_phase: int) -> list[dict]:
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
    rng = random.Random(7)
    app = create_app(settings, {name: httpx.ASGITransport(app=a) for name, a in gateway_apps.items()})
    results = []
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sim") as client:
            merchant = (await client.post("/merchants", json={"name": "sim"}, headers=ADMIN)).json()
            headers = {"Authorization": f"Bearer {merchant['api_key']}"}

            async def pay(brand: str, amount: int) -> str:
                body = {
                    "amount_minor": amount,
                    "currency": "INR",
                    "capture_method": "automatic",
                    "card": {"number": CARDS[brand], "exp_month": 12, "exp_year": 2030,
                             "cvc": "1234" if brand == "amex" else "123"},
                }
                response = await client.post(
                    "/payments", json=body, headers={**headers, "Idempotency-Key": str(uuid.uuid4())}
                )
                return response.json()["status"]

            for name, changes in phases:
                for gateway, behaviour in changes.items():
                    gateway_apps[gateway].state.gateway.configure(**behaviour)
                calls_before = sum(a.state.gateway.requests for a in gateway_apps.values())
                statuses = []
                for _ in range(per_phase // CONCURRENCY):
                    batch = [traffic(rng) for _ in range(CONCURRENCY)]
                    statuses += await asyncio.gather(*(pay(brand, amount) for brand, amount in batch))
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


def print_table(title: str, phases: list, table: dict, metric: str, fmt: str) -> None:
    print(f"\n{title}:\n")
    print("| Phase | " + " | ".join(table) + " |")
    print("|---|" + "---|" * len(table))
    for i, (phase, _) in enumerate(phases):
        print(f"| {phase} | " + " | ".join(format(rows[i][metric], fmt) for rows in table.values()) + " |")


async def run_scenario(title: str, setups: list, phases: list, traffic, per_phase: int, calls: bool) -> None:
    table = {}
    for label, overrides in setups:
        print(f"{title}: {label}", file=sys.stderr)
        table[label] = await run_setup(overrides, phases, traffic, per_phase)
    print(f"\n### {title}\n\n{per_phase} payments per phase, {CONCURRENCY} at a time.")
    print_table("Authorization success rate", phases, table, "success", ".1%")
    if calls:
        print_table("Gateway calls per payment", phases, table, "calls_per_payment", ".2f")


async def main(per_phase: int) -> None:
    await run_scenario("Outages", OUTAGE_SETUPS, OUTAGES, single_card, per_phase, calls=True)
    await run_scenario("Segments", SEGMENT_SETUPS, SEGMENTS, mixed_traffic, per_phase * 2, calls=False)


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 1000))
