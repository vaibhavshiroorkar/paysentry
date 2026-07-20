"""Requests that race on the same payment, against real Postgres.

Each gateway call takes 50 ms, so requests overlap while one of them is
between transaction A and transaction B.
"""

import asyncio
from collections import Counter

from helpers import create_merchant
from payment_helpers import (
    authorized_payment,
    capture,
    captured_payment,
    create_payment,
    get_payment,
    merchant_balance,
    refund,
    void,
)


async def test_concurrent_captures_succeed_exactly_once(make_client):
    client = await make_client(gateway_latency_ms=50)
    _, key = await create_merchant(client)
    payment_id = await authorized_payment(client, key, 1_000)

    responses = await asyncio.gather(*(capture(client, key, payment_id) for _ in range(20)))

    assert Counter(r.status_code for r in responses) == {200: 1, 409: 19}
    assert await merchant_balance(client, key) == 1_000


async def test_capture_and_void_racing_have_one_winner(make_client):
    client = await make_client(gateway_latency_ms=50)
    _, key = await create_merchant(client)
    payment_id = await authorized_payment(client, key, 1_000)

    responses = await asyncio.gather(
        *(capture(client, key, payment_id) if i % 2 else void(client, key, payment_id) for i in range(20))
    )

    assert Counter(r.status_code for r in responses) == {200: 1, 409: 19}
    status = (await get_payment(client, key, payment_id))["status"]
    assert await merchant_balance(client, key) == (1_000 if status == "captured" else 0)


async def test_concurrent_refunds_never_exceed_the_capture(make_client):
    client = await make_client(gateway_latency_ms=50)
    _, key = await create_merchant(client)
    payment_id = await captured_payment(client, key, 1_000)

    responses = await asyncio.gather(*(refund(client, key, payment_id, 100) for _ in range(20)))

    assert Counter(r.status_code for r in responses) == {201: 10, 422: 10}
    payment = await get_payment(client, key, payment_id)
    assert (payment["status"], payment["amount_refunded"]) == ("refunded", 1_000)
    assert await merchant_balance(client, key) == 0


async def test_many_merchants_paying_at_once_share_the_clearing_account_safely(client):
    keys = [(await create_merchant(client, f"m{i}"))[1] for i in range(5)]

    # Every capture and refund touches the one clearing account, from every merchant.
    payments = await asyncio.gather(*(create_payment(client, key, 100) for key in keys for _ in range(10)))
    refunds = await asyncio.gather(
        *(refund(client, keys[i // 10], p.json()["id"], 40) for i, p in enumerate(payments))
    )

    assert {p.status_code for p in payments} == {201}
    assert {r.status_code for r in refunds} == {201}
    for key in keys:
        assert await merchant_balance(client, key) == 10 * 60
