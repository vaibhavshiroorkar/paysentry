"""Routing: rules, circuit breakers, ranking and failover. See docs/design/routing.md."""

import asyncio
import random

from redis.asyncio import Redis

from app.config import Settings
from app.routing import Router
from helpers import create_merchant, merchant_headers, new_key
from payment_helpers import VISA, authorized_payment, capture, card, create_payment

AMEX = "378282246310005"

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


# Routing through the API


async def attempts_of(db) -> list[tuple[str, str]]:
    rows = await db.fetch("SELECT gateway, status FROM payment_attempts ORDER BY created_at")
    return [tuple(row) for row in rows]


async def test_failover_moves_the_payment_to_the_next_gateway(make_client, gateways, db):
    client = await make_client()
    gateways["alpha"].configure(unavailable_rate=1.0)
    _, key = await create_merchant(client)

    payment_id = await authorized_payment(client, key)

    assert await attempts_of(db) == [("alpha", "failed"), ("beta", "succeeded")]
    gateways["alpha"].configure(unavailable_rate=0.0)
    assert (await capture(client, key, payment_id)).status_code == 200
    (charge,) = gateways["beta"].charges_by_reference.values()
    assert charge.status == "captured"
    assert gateways["alpha"].charges_by_reference == {}


async def test_no_failover_after_a_decline(make_client, gateways, db):
    client = await make_client()
    gateways["alpha"].configure(decline_rate=1.0)
    _, key = await create_merchant(client)

    response = await create_payment(client, key)

    assert (response.json()["status"], response.json()["failure_reason"]) == ("failed", "do_not_honor")
    assert await attempts_of(db) == [("alpha", "declined")]


async def test_no_failover_after_an_unknown_outcome(make_client, gateways, db):
    client = await make_client(gateway_timeout_sec=0.1)
    gateways["alpha"].configure(unknown_rate=1.0, hang_ms=300)
    _, key = await create_merchant(client)

    response = await create_payment(client, key)

    # alpha may have charged the card, so trying beta could charge it twice.
    assert response.status_code == 202
    assert await attempts_of(db) == [("alpha", "unknown")]
    assert gateways["beta"].charges_by_reference == {}


async def test_all_gateways_down_fails_the_payment(make_client, gateways, db):
    client = await make_client()
    for gateway in gateways.values():
        gateway.configure(unavailable_rate=1.0)
    _, key = await create_merchant(client)

    response = await create_payment(client, key)

    assert (response.json()["status"], response.json()["failure_reason"]) == ("failed", "gateway_unavailable")
    assert await attempts_of(db) == [("alpha", "failed"), ("beta", "failed"), ("gamma", "failed")]


async def test_payments_go_only_to_gateways_that_accept_them(make_client, gateways):
    client = await make_client(use_gateways=["beta", "gamma", "alpha"])
    _, key = await create_merchant(client)

    amex = await create_payment(client, key, number=AMEX)
    usd = await client.post(
        "/payments",
        json={"amount_minor": 500, "currency": "USD", "capture_method": "automatic", "card": card()},
        headers=merchant_headers(key, new_key()),
    )

    assert amex.json()["status"] == usd.json()["status"] == "captured"
    # beta takes no amex, so the amex goes to gamma. gamma takes no USD, so the USD visa goes to beta.
    assert len(gateways["gamma"].charges_by_reference) == 1
    assert len(gateways["beta"].charges_by_reference) == 1
    assert gateways["alpha"].charges_by_reference == {}


async def test_no_eligible_gateway_answers_503_and_leaves_the_key_free(make_client):
    client = await make_client(use_gateways=["beta"])
    _, key = await create_merchant(client)
    idem = new_key()

    refused = await create_payment(client, key, number=AMEX, key=idem)
    accepted = await create_payment(client, key, key=idem)

    assert refused.status_code == 503
    assert refused.json()["error"]["code"] == "no_gateway_available"
    assert accepted.status_code == 201


async def test_open_circuit_stops_calls_to_a_failing_gateway(make_client, gateways):
    client = await make_client(circuit_failure_threshold=3)
    gateways["alpha"].configure(unavailable_rate=1.0)
    _, key = await create_merchant(client)

    for _ in range(3):
        await create_payment(client, key)
    calls_when_opened = gateways["alpha"].requests
    for _ in range(2):
        assert (await create_payment(client, key)).json()["status"] == "captured"

    assert calls_when_opened == 3
    assert gateways["alpha"].requests == 3


async def test_success_rate_routing_moves_traffic_to_the_healthy_gateway(make_client, gateways, db):
    client = await make_client(routing_strategy="success_rate", routing_seed=7, use_gateways=["alpha", "beta"])
    gateways["alpha"].configure(decline_rate=1.0)
    _, key = await create_merchant(client)

    for _ in range(60):
        await create_payment(client, key)

    rows = await db.fetch("SELECT gateway FROM payment_attempts ORDER BY created_at OFFSET 20")
    assert [row["gateway"] for row in rows].count("beta") >= 32


async def test_replay_does_not_depend_on_gateway_health(make_client, gateways):
    client = await make_client(use_gateways=["alpha"], circuit_failure_threshold=1)
    _, key = await create_merchant(client)
    idem = new_key()
    first = await create_payment(client, key, key=idem)

    # Open alpha's circuit, leaving no gateway to route to.
    gateways["alpha"].configure(unavailable_rate=1.0)
    await create_payment(client, key)
    retry = await create_payment(client, key, key=idem)

    assert retry.status_code == 201
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert retry.content == first.content


MASTERCARD = "5555555555554444"


async def test_contextual_routing_learns_each_gateways_weak_brand(make_client, gateways, db):
    client = await make_client(routing_strategy="contextual", use_gateways=["alpha", "beta"])
    gateways["alpha"].configure(decline_rate=0.05, decline_by_brand={"mastercard": 0.9}, seed=1)
    gateways["beta"].configure(decline_rate=0.05, decline_by_brand={"visa": 0.9}, seed=2)
    _, key = await create_merchant(client)

    for i in range(80):
        await create_payment(client, key, number=VISA if i % 2 == 0 else MASTERCARD)

    rows = await db.fetch(
        """
        SELECT p.card_brand, a.gateway FROM payment_attempts a JOIN payments p ON p.id = a.payment_id
        ORDER BY a.created_at OFFSET 40
        """
    )
    visa = [row["gateway"] for row in rows if row["card_brand"] == "visa"]
    mastercard = [row["gateway"] for row in rows if row["card_brand"] == "mastercard"]
    assert visa.count("alpha") >= 0.8 * len(visa)
    assert mastercard.count("beta") >= 0.8 * len(mastercard)


async def test_failover_teaches_the_model_about_both_gateways(make_client, gateways):
    client = await make_client(routing_strategy="contextual", use_gateways=["alpha", "beta"])
    gateways["alpha"].configure(unavailable_rate=1.0)
    _, key = await create_merchant(client)

    response = await create_payment(client, key)

    model = client.app.state.router.model
    _, alpha_b, _ = await model.raw("alpha")
    _, beta_b, _ = await model.raw("beta")
    assert response.json()["status"] == "captured"
    assert (alpha_b[0], beta_b[0]) == (-0.5, 0.5)
