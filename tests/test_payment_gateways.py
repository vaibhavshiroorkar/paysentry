"""Payments through the mock gateways, including every outcome the gateway can give."""

import uuid

import pytest

from helpers import create_merchant, merchant_headers, new_key
from payment_helpers import (
    authorized_payment,
    capture,
    captured_payment,
    card,
    create_payment,
    get_payment,
    merchant_balance,
    refund,
    void,
)

EACH_GATEWAY = pytest.mark.parametrize("gateway", ["alpha", "beta"])


@EACH_GATEWAY
async def test_full_lifecycle_through_each_gateway(make_client, gateways, db, gateway):
    client = await make_client(default_gateway=gateway)
    _, key = await create_merchant(client)
    payment_id = await authorized_payment(client, key, 1_000)

    assert (await capture(client, key, payment_id, 600)).status_code == 200
    assert (await refund(client, key, payment_id, 250)).json()["status"] == "succeeded"

    payment = await get_payment(client, key, payment_id)
    assert (payment["amount_captured"], payment["amount_refunded"]) == (600, 250)
    assert await merchant_balance(client, key) == 350
    (charge,) = gateways[gateway].charges_by_reference.values()
    assert (charge.captured_amount, charge.refunded_amount) == (600, 250)
    attempts = await db.fetch(
        "SELECT gateway, status FROM payment_attempts WHERE payment_id = $1", uuid.UUID(payment_id)
    )
    assert [tuple(a) for a in attempts] == [(gateway, "succeeded")]


@EACH_GATEWAY
async def test_usd_payment_through_each_gateway(make_client, gateways, gateway):
    client = await make_client(default_gateway=gateway)
    _, key = await create_merchant(client)
    created = await client.post(
        "/payments",
        json={"amount_minor": 2_550, "currency": "USD", "capture_method": "automatic", "card": card()},
        headers=merchant_headers(key, new_key()),
    )

    refunded = await refund(client, key, created.json()["id"], 550)

    assert created.json()["status"] == "captured"
    assert refunded.json()["status"] == "succeeded"
    assert await merchant_balance(client, key, "USD") == 2_000


async def test_gateway_unavailable_fails_the_payment(make_client, gateways, db):
    client = await make_client()
    gateways["alpha"].configure(unavailable_rate=1.0)
    _, key = await create_merchant(client)

    response = await create_payment(client, key)

    assert response.status_code == 201
    assert (response.json()["status"], response.json()["failure_reason"]) == ("failed", "gateway_unavailable")
    assert await db.fetchval("SELECT status FROM payment_attempts") == "failed"


async def test_unknown_authorization_answers_202_and_holds_the_key(make_client, gateways, db):
    client = await make_client(gateway_timeout_sec=0.1)
    gateways["alpha"].configure(unknown_rate=1.0, hang_ms=500)
    _, key = await create_merchant(client)
    idem = new_key()

    response = await create_payment(client, key, key=idem)
    retry = await create_payment(client, key, key=idem)

    assert response.status_code == 202
    assert response.json()["status"] == "processing"
    assert await db.fetchval("SELECT status FROM payment_attempts") == "unknown"
    assert retry.status_code == 409
    assert retry.json()["error"]["code"] == "request_in_progress"
    assert await merchant_balance(client, key) == 0


async def test_unknown_capture_answers_202(make_client, gateways):
    client = await make_client(gateway_timeout_sec=0.1)
    _, key = await create_merchant(client)
    payment_id = await authorized_payment(client, key)
    gateways["alpha"].configure(unknown_rate=1.0, hang_ms=500)

    response = await capture(client, key, payment_id)

    assert response.status_code == 202
    assert response.json()["status"] == "capture_pending"
    assert await merchant_balance(client, key) == 0


async def test_capture_not_processed_puts_payment_back_and_frees_the_key(make_client, gateways):
    client = await make_client()
    _, key = await create_merchant(client)
    payment_id = await authorized_payment(client, key)
    idem = new_key()

    gateways["alpha"].configure(unavailable_rate=1.0)
    refused = await capture(client, key, payment_id, key=idem)
    gateways["alpha"].configure(unavailable_rate=0.0)
    retried = await capture(client, key, payment_id, key=idem)

    assert refused.status_code == 503
    assert refused.json()["error"]["code"] == "gateway_unavailable"
    assert retried.status_code == 200
    assert "Idempotent-Replayed" not in retried.headers


async def test_void_not_processed_puts_payment_back(make_client, gateways):
    client = await make_client()
    _, key = await create_merchant(client)
    payment_id = await authorized_payment(client, key)

    gateways["alpha"].configure(unavailable_rate=1.0)
    response = await void(client, key, payment_id)

    assert response.status_code == 503
    assert (await get_payment(client, key, payment_id))["status"] == "authorized"


async def test_unknown_refund_answers_202_and_keeps_its_reservation(make_client, gateways):
    client = await make_client(gateway_timeout_sec=0.1)
    _, key = await create_merchant(client)
    payment_id = await captured_payment(client, key, 1_000)
    gateways["alpha"].configure(unknown_rate=1.0, hang_ms=500)

    pending = await refund(client, key, payment_id, 600)
    over = await refund(client, key, payment_id, 500)

    assert pending.status_code == 202
    assert pending.json()["status"] == "pending"
    assert over.status_code == 422


async def test_refund_not_processed_frees_the_key_and_the_amount(make_client, gateways):
    client = await make_client()
    _, key = await create_merchant(client)
    payment_id = await captured_payment(client, key, 1_000)
    idem = new_key()

    gateways["alpha"].configure(unavailable_rate=1.0)
    refused = await refund(client, key, payment_id, 1_000, key=idem)
    gateways["alpha"].configure(unavailable_rate=0.0)
    retried = await refund(client, key, payment_id, 1_000, key=idem)

    assert refused.status_code == 503
    assert retried.status_code == 201
    assert retried.json()["status"] == "succeeded"
    assert await merchant_balance(client, key) == 0
