"""Routing: rules, circuit breakers, ranking and failover. See docs/design/routing.md."""

import asyncio
import random

from redis.asyncio import Redis

from app.config import Settings
from app.routing import Router

GATEWAYS = {
    "alpha": {"flavour": "alpha", "url": "http://alpha"},
    "beta": {"flavour": "beta", "url": "http://beta", "brands": ["visa", "mastercard"]},
    "gamma": {"flavour": "alpha", "url": "http://gamma", "currencies": ["INR"]},
}


def router(redis, seed: int = 1, **overrides) -> Router:
    values = {
        "gateways": GATEWAYS,
        "routing_strategy": "priority",
        "circuit_failure_threshold": 3,
        "circuit_cooldown_sec": 60,
    }
    return Router(redis, Settings(_env_file=None, **(values | overrides)), random.Random(seed))


async def open_circuit(r: Router, gateway: str = "alpha") -> None:
    for _ in range(3):
        await r.record(gateway, "not_processed")


async def test_rules_filter_by_currency_and_brand(redis):
    r = router(redis)

    assert await r.plan("INR", "visa") == ["alpha", "beta", "gamma"]
    assert await r.plan("USD", "visa") == ["alpha", "beta"]
    assert await r.plan("INR", "amex") == ["alpha", "gamma"]
    assert await r.plan("USD", "amex") == ["alpha"]


async def test_circuit_opens_after_the_threshold_of_errors(redis):
    r = router(redis)

    await r.record("alpha", "not_processed")
    await r.record("alpha", "unknown")
    assert "alpha" in await r.plan("INR", "visa")

    await r.record("alpha", "not_processed")
    assert await r.plan("INR", "visa") == ["beta", "gamma"]
    assert await r.acquire("alpha") is False


async def test_any_answer_from_the_gateway_resets_the_error_streak(redis):
    r = router(redis)
    for outcome in ["not_processed", "not_processed", "declined", "not_processed", "not_processed"]:
        await r.record("alpha", outcome)
    assert "alpha" in await r.plan("INR", "visa")


async def test_closed_circuit_is_always_acquirable(redis):
    r = router(redis)
    assert await r.acquire("alpha") is True
    assert await r.acquire("alpha") is True


async def test_half_open_circuit_allows_exactly_one_probe(redis):
    r = router(redis, circuit_cooldown_sec=0.1)
    await open_circuit(r)
    await asyncio.sleep(0.15)

    assert "alpha" in await r.plan("INR", "visa")
    assert await r.acquire("alpha") is True
    assert await r.acquire("alpha") is False
    # If the probe's payment never reaches the gateway, the slot frees itself.
    assert await redis.pttl("circuit:alpha:probe") > 0


async def test_successful_probe_closes_the_circuit(redis):
    r = router(redis, circuit_cooldown_sec=0.1)
    await open_circuit(r)
    await asyncio.sleep(0.15)
    await r.acquire("alpha")

    await r.record("alpha", "approved")

    assert await r.acquire("alpha") is True
    assert await r.acquire("alpha") is True


async def test_failed_probe_reopens_the_circuit(redis):
    r = router(redis, circuit_cooldown_sec=0.1)
    await open_circuit(r)
    await asyncio.sleep(0.15)
    await r.acquire("alpha")

    await r.record("alpha", "not_processed")

    assert "alpha" not in await r.plan("INR", "visa")
    assert await r.acquire("alpha") is False


async def test_success_rate_ranking_prefers_the_healthier_gateway(redis):
    r = router(redis, routing_strategy="success_rate")
    for _ in range(50):
        await r.record("alpha", "declined")
        await r.record("beta", "approved")

    firsts = [(await r.plan("USD", "visa"))[0] for _ in range(100)]

    assert firsts.count("beta") >= 90


async def test_success_window_forgets_old_outcomes(redis):
    r = router(redis, routing_strategy="success_rate", routing_window=10)
    for _ in range(50):
        await r.record("alpha", "declined")
    for _ in range(10):
        await r.record("alpha", "approved")
        await r.record("beta", "declined")

    firsts = [(await r.plan("USD", "visa"))[0] for _ in range(100)]

    assert firsts.count("alpha") >= 90


async def test_equal_gateways_both_get_traffic(redis):
    r = router(redis, routing_strategy="success_rate")
    for _ in range(20):
        await r.record("alpha", "approved")
        await r.record("beta", "approved")

    firsts = [(await r.plan("USD", "visa"))[0] for _ in range(100)]

    assert firsts.count("alpha") >= 20
    assert firsts.count("beta") >= 20


async def test_empty_windows_rank_without_error(redis):
    r = router(redis, routing_strategy="success_rate")
    assert sorted(await r.plan("INR", "visa")) == ["alpha", "beta", "gamma"]


async def test_redis_down_falls_back_to_priority_with_circuits_closed():
    down = Redis.from_url("redis://localhost:6390/0", socket_connect_timeout=0.2)
    r = router(down, routing_strategy="success_rate")

    assert await r.plan("INR", "visa") == ["alpha", "beta", "gamma"]
    assert await r.acquire("alpha") is True
    await r.record("alpha", "approved")
    await down.aclose()
