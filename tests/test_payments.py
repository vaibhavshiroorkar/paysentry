import asyncio
import uuid

import pytest

from helpers import create_merchant, merchant_headers, new_key
from payment_helpers import (
    ALL_TEST_CARDS,
    DECLINED,
    NO_FUNDS,
    VISA,
    card,
    create_payment,
    get_payment,
    merchant_balance,
)


async def test_automatic_capture_payment_is_captured_and_credits_the_merchant(client):
    _, key = await create_merchant(client)

    response = await create_payment(client, key, 1_000, "automatic")

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "captured"
    assert (body["amount_minor"], body["amount_captured"], body["amount_refunded"]) == (1_000, 1_000, 0)
    assert body["card"] == {"brand": "visa", "last4": "4242"}
    assert body["refunds"] == []
    assert await merchant_balance(client, key) == 1_000


async def test_manual_capture_payment_is_only_authorized(client):
    _, key = await create_merchant(client)

    response = await create_payment(client, key, 1_000, "manual")

    assert response.status_code == 201
    assert response.json()["status"] == "authorized"
    assert response.json()["amount_captured"] == 0
    assert await merchant_balance(client, key) == 0


@pytest.mark.parametrize("number, reason", [(DECLINED, "card_declined"), (NO_FUNDS, "insufficient_funds")])
async def test_declined_card_still_creates_a_failed_payment(client, number, reason):
    _, key = await create_merchant(client)

    response = await create_payment(client, key, number=number)

    assert response.status_code == 201
    assert response.json()["status"] == "failed"
    assert response.json()["failure_reason"] == reason
    assert await merchant_balance(client, key) == 0


async def test_invalid_card_is_rejected_without_echoing_the_number(client):
    _, key = await create_merchant(client)
    for bad_card in [card("4242424242424241"), card(VISA, exp_year=2020)]:
        response = await client.post(
            "/payments",
            json={"amount_minor": 1_000, "currency": "INR", "capture_method": "automatic", "card": bad_card},
            headers=merchant_headers(key, new_key()),
        )
        assert response.status_code == 422
        assert bad_card["number"] not in response.text


@pytest.mark.parametrize(
    "change",
    [{"amount_minor": 0}, {"amount_minor": "1000"}, {"currency": "EUR"}, {"capture_method": "later"}],
)
async def test_invalid_payment_fields_are_rejected(client, change):
    _, key = await create_merchant(client)
    body = {"amount_minor": 1_000, "currency": "INR", "capture_method": "automatic", "card": card(), **change}
    response = await client.post("/payments", json=body, headers=merchant_headers(key, new_key()))
    assert response.status_code == 422


async def test_payment_needs_an_api_key_and_an_idempotency_key(client):
    _, key = await create_merchant(client)
    body = {"amount_minor": 1_000, "currency": "INR", "capture_method": "automatic", "card": card()}

    assert (await client.post("/payments", json=body, headers={"Idempotency-Key": new_key()})).status_code == 401
    assert (await client.post("/payments", json=body, headers=merchant_headers(key))).status_code == 400


async def test_retry_replays_the_same_payment(client, db):
    _, key = await create_merchant(client)
    idem = new_key()

    first = await create_payment(client, key, key=idem)
    retry = await create_payment(client, key, key=idem)

    assert retry.content == first.content
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert await db.fetchval("SELECT count(*) FROM payments") == 1
    assert await merchant_balance(client, key) == 1_000


async def test_same_key_with_a_different_amount_is_rejected(client):
    _, key = await create_merchant(client)
    idem = new_key()
    await create_payment(client, key, 1_000, key=idem)

    response = await create_payment(client, key, 2_000, key=idem)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "idempotency_key_reused"


async def test_idempotency_keys_are_scoped_per_merchant(client):
    _, first_key = await create_merchant(client, "First")
    _, second_key = await create_merchant(client, "Second")

    first = await create_payment(client, first_key, key="order-1")
    second = await create_payment(client, second_key, key="order-1")

    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]


async def test_duplicate_sent_while_the_first_is_in_flight_gets_409(make_client):
    client = await make_client(fake_processor_delay_ms=300)
    _, key = await create_merchant(client)
    idem = new_key()

    responses = await asyncio.gather(create_payment(client, key, key=idem), create_payment(client, key, key=idem))

    assert sorted(r.status_code for r in responses) == [201, 409]
    conflict = next(r for r in responses if r.status_code == 409)
    assert conflict.json()["error"]["code"] == "request_in_progress"
    later = await create_payment(client, key, key=idem)
    assert later.status_code == 201
    assert later.headers["Idempotent-Replayed"] == "true"


async def test_payment_is_readable_by_its_merchant_only(client):
    _, owner = await create_merchant(client, "Owner")
    _, other = await create_merchant(client, "Other")
    created = (await create_payment(client, owner)).json()

    assert await get_payment(client, owner, created["id"]) == created
    response = await client.get(f"/payments/{created['id']}", headers=merchant_headers(other))
    assert response.status_code == 404


async def test_unknown_payment_is_404(client):
    _, key = await create_merchant(client)
    response = await client.get(f"/payments/{uuid.uuid4()}", headers=merchant_headers(key))
    assert response.status_code == 404


async def test_rate_limit_is_per_merchant(make_client):
    client = await make_client(rate_limit_capacity=2, rate_limit_refill_per_sec=0.01)
    _, busy = await create_merchant(client, "Busy")
    _, quiet = await create_merchant(client, "Quiet")

    codes = [(await create_payment(client, busy)).status_code for _ in range(3)]

    assert codes == [201, 201, 429]
    assert (await create_payment(client, quiet)).status_code == 201


async def test_full_card_number_is_never_stored(client, db):
    _, key = await create_merchant(client)
    for number in ALL_TEST_CARDS:
        for method in ("automatic", "manual"):
            await create_payment(client, key, number=number, capture_method=method)

    assert await db.fetchval("SELECT count(*) FROM payments") == 10
    for table in ["payments", "idempotency_keys", "transfers", "ledger_entries", "accounts"]:
        for number in ALL_TEST_CARDS:
            hits = await db.fetchval(f"SELECT count(*) FROM {table} t WHERE t::text LIKE '%' || $1 || '%'", number)
            assert hits == 0, (table, number)
