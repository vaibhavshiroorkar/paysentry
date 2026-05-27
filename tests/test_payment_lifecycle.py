"""Capture, void and refund on a single payment, one request at a time."""

import uuid

from helpers import create_merchant, new_key
from payment_helpers import (
    CAPTURE_FAILS,
    authorized_payment,
    capture,
    captured_payment,
    get_payment,
    merchant_balance,
    void,
)


async def test_capture_with_no_body_captures_the_full_amount(client):
    _, key = await create_merchant(client)
    payment_id = await authorized_payment(client, key, 1_000)

    response = await capture(client, key, payment_id)

    assert response.status_code == 200
    assert response.json()["status"] == "captured"
    assert response.json()["amount_captured"] == 1_000
    assert await merchant_balance(client, key) == 1_000


async def test_partial_capture_releases_the_rest(client):
    _, key = await create_merchant(client)
    payment_id = await authorized_payment(client, key, 1_000)

    response = await capture(client, key, payment_id, 600)
    again = await capture(client, key, payment_id, 400)

    assert response.status_code == 200
    assert response.json()["amount_captured"] == 600
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "invalid_state"
    assert await merchant_balance(client, key) == 600


async def test_capture_above_the_authorized_amount_is_rejected_and_key_stays_free(client):
    _, key = await create_merchant(client)
    payment_id = await authorized_payment(client, key, 1_000)
    idem = new_key()

    too_much = await capture(client, key, payment_id, 1_001, key=idem)
    fixed = await capture(client, key, payment_id, 1_000, key=idem)

    assert too_much.status_code == 422
    assert too_much.json()["error"]["code"] == "capture_exceeds_authorized"
    assert fixed.status_code == 200
    assert "Idempotent-Replayed" not in fixed.headers


async def test_capture_retry_replays_and_moves_money_once(client):
    _, key = await create_merchant(client)
    payment_id = await authorized_payment(client, key, 1_000)
    idem = new_key()

    first = await capture(client, key, payment_id, key=idem)
    retry = await capture(client, key, payment_id, key=idem)

    assert retry.content == first.content
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert await merchant_balance(client, key) == 1_000


async def test_key_reused_on_another_payments_capture_is_rejected(client):
    _, key = await create_merchant(client)
    first = await authorized_payment(client, key)
    second = await authorized_payment(client, key)
    idem = new_key()

    await capture(client, key, first, key=idem)
    response = await capture(client, key, second, key=idem)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "idempotency_key_reused"
    assert (await get_payment(client, key, second))["status"] == "authorized"


async def test_void_releases_the_hold(client):
    _, key = await create_merchant(client)
    payment_id = await authorized_payment(client, key)

    response = await void(client, key, payment_id)
    late_capture = await capture(client, key, payment_id)

    assert response.status_code == 200
    assert response.json()["status"] == "voided"
    assert late_capture.status_code == 409
    assert await merchant_balance(client, key) == 0


async def test_captured_payment_cannot_be_voided_or_captured_again(client):
    _, key = await create_merchant(client)
    payment_id = await captured_payment(client, key)

    assert (await void(client, key, payment_id)).status_code == 409
    assert (await capture(client, key, payment_id)).status_code == 409


async def test_processor_rejecting_capture_or_void_leaves_payment_authorized(client):
    _, key = await create_merchant(client)
    payment_id = await authorized_payment(client, key, number=CAPTURE_FAILS)

    captured = await capture(client, key, payment_id)
    voided = await void(client, key, payment_id)

    for response, code in [(captured, "capture_declined"), (voided, "void_declined")]:
        assert response.status_code == 402
        assert response.json()["error"]["code"] == "processor_declined"
        assert response.json()["error"]["decline_code"] == code
    assert (await get_payment(client, key, payment_id))["status"] == "authorized"
    assert await merchant_balance(client, key) == 0


async def test_other_merchant_cannot_capture_or_void(client):
    _, owner = await create_merchant(client, "Owner")
    _, other = await create_merchant(client, "Other")
    payment_id = await authorized_payment(client, owner)

    assert (await capture(client, other, payment_id)).status_code == 404
    assert (await void(client, other, payment_id)).status_code == 404
    assert (await get_payment(client, owner, payment_id))["status"] == "authorized"


async def test_capture_of_unknown_payment_is_404(client):
    _, key = await create_merchant(client)
    assert (await capture(client, key, str(uuid.uuid4()))).status_code == 404
