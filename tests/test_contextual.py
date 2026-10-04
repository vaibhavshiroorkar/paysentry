"""Contextual routing (LinUCB). See docs/design/routing.md, "Contextual ranking"."""

import asyncio
import math

import pytest
from redis.asyncio import Redis

from app.config import Settings
from app.contextual import Context, ContextualModel, features
from app.routing import Router

VISA = Context("INR", "visa", 10_000)
MASTERCARD = Context("INR", "mastercard", 10_000)


def settings(**overrides) -> Settings:
    values = {
        "gateways": {
            "alpha": {"flavour": "alpha", "url": "http://alpha"},
            "beta": {"flavour": "beta", "url": "http://beta"},
        },
        "routing_strategy": "contextual",
    }
    return Settings(_env_file=None, **(values | overrides))


@pytest.mark.parametrize(
    "context, expected",
    [
        (Context("USD", "mastercard", 600_000), [1, 0, 1, 0, 0, 0, 0, 1, 0, 0, 1]),
        (Context("INR", "visa", 49_999), [1, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0]),
        (Context("INR", "amex", 50_000), [1, 0, 0, 1, 0, 0, 1, 0, 0, 1, 0]),
        (Context("INR", "unknown", 5_000_000), [1, 0, 0, 0, 1, 0, 0, 0, 1, 1, 0]),
    ],
)
def test_features_are_one_hot_with_a_bias(context, expected):
    assert features(context).tolist() == expected


async def teach_brands(router: Router, rounds: int = 30) -> None:
    """alpha approves visa and declines mastercard. beta does the opposite."""
    for _ in range(rounds):
        await router.record("alpha", "approved", VISA)
        await router.record("alpha", "declined", MASTERCARD)
        await router.record("beta", "declined", VISA)
        await router.record("beta", "approved", MASTERCARD)


async def test_empty_model_keeps_priority_order(redis):
    router = Router(redis, settings())
    assert await router.plan("INR", "visa", 10_000) == ["alpha", "beta"]


async def test_learns_which_gateway_suits_which_brand(redis):
    router = Router(redis, settings())
    await teach_brands(router)

    assert (await router.plan("INR", "visa", 10_000))[0] == "alpha"
    assert (await router.plan("INR", "mastercard", 10_000))[0] == "beta"


async def test_what_it_learns_carries_over_to_unseen_amounts(redis):
    router = Router(redis, settings())
    await teach_brands(router)

    # Only small amounts were seen. The brand weights still apply to large ones.
    assert (await router.plan("INR", "visa", 9_000_000))[0] == "alpha"
    assert (await router.plan("INR", "mastercard", 9_000_000))[0] == "beta"


async def test_forgets_old_evidence_after_a_regime_change(redis):
    router = Router(redis, settings(routing_decay=0.98))
    for _ in range(300):
        await router.record("alpha", "approved", VISA)

    # alpha goes bad for visa. beta approves 70%. Without forgetting, 300 old
    # approvals would keep alpha's estimate at 75% and alpha would still lead.
    for i in range(100):
        await router.plan("INR", "visa", 10_000)
        await router.record("alpha", "declined", VISA)
        await router.record("beta", "approved" if i % 10 < 7 else "declined", VISA)

    assert (await router.plan("INR", "visa", 10_000))[0] == "beta"


async def test_idle_gateway_is_explored_again(redis):
    router = Router(redis, settings(routing_decay=0.99))
    for _ in range(50):
        await router.record("alpha", "declined", VISA)

    explored = False
    for i in range(1_000):
        first = (await router.plan("INR", "visa", 10_000))[0]
        if first == "alpha":
            explored = True
            break
        await router.record("beta", "approved" if i % 10 < 8 else "declined", VISA)

    # alpha got no traffic, so its bad record decayed and its uncertainty grew
    # until the exploration bonus earned it another try.
    assert explored


async def test_a_huge_idle_gap_still_gives_finite_scores(redis):
    model = ContextualModel(redis, settings())
    for _ in range(10):
        await model.observe("alpha", VISA, 1)
    await redis.set("routing:decisions", 10**7)

    scores = await model.scores(["alpha", "beta"], VISA)

    assert all(math.isfinite(score) for score in scores.values())


async def test_concurrent_observations_are_never_lost(redis):
    model = ContextualModel(redis, settings(routing_decay=1.0))

    await asyncio.gather(*(model.observe("alpha", VISA, 1) for _ in range(50)))

    a, b, _ = await model.raw("alpha")
    assert a[0][0] == 50
    assert b[0] == 25  # 50 approvals, each centred to +0.5


async def test_redis_down_keeps_priority_order():
    down = Redis.from_url("redis://localhost:6390/0", socket_connect_timeout=0.2)
    router = Router(down, settings())

    assert await router.plan("INR", "visa", 10_000) == ["alpha", "beta"]
    await router.record("alpha", "approved", VISA)
    await down.aclose()
