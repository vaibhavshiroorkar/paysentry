"""Capture, void and refund on a single payment, one request at a time."""

import uuid

from helpers import create_merchant, new_key
from payment_helpers import (
    CAPTURE_FAILS,
    REFUND_FAILS,
    authorized_payment,
    capture,
    captured_payment,
    get_payment,
    merchant_balance,
    refund,
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


async def test_partial_refunds_until_fully_refunded(client):
    _, key = await create_merchant(client)
    payment_id = await captured_payment(client, key, 1_000)

    first = await refund(client, key, payment_id, 400)
    second = await refund(client, key, payment_id, 600)

    assert first.status_code == second.status_code == 201
    assert first.json()["status"] == "succeeded"
    payment = await get_payment(client, key, payment_id)
    assert payment["status"] == "refunded"
    assert payment["amount_refunded"] == 1_000
    assert [r["amount_minor"] for r in payment["refunds"]] == [400, 600]
    assert await merchant_balance(client, key) == 0


async def test_refund_above_what_is_left_is_rejected(client):
    _, key = await create_merchant(client)
    payment_id = await captured_payment(client, key, 1_000)
    await refund(client, key, payment_id, 700)

    response = await refund(client, key, payment_id, 301)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "refund_exceeds_available"
    assert (await get_payment(client, key, payment_id))["amount_refunded"] == 700


async def test_refund_after_partial_capture_is_limited_to_the_captured_amount(client):
    _, key = await create_merchant(client)
    payment_id = await authorized_payment(client, key, 1_000)
    await capture(client, key, payment_id, 600)

    assert (await refund(client, key, payment_id, 601)).status_code == 422
    assert (await refund(client, key, payment_id, 600)).status_code == 201


async def test_refund_needs_a_captured_payment(client):
    _, key = await create_merchant(client)
    authorized = await authorized_payment(client, key)
    fully_refunded = await captured_payment(client, key, 500)
    await refund(client, key, fully_refunded, 500)

    for payment_id in [authorized, fully_refunded]:
        response = await refund(client, key, payment_id, 100)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "invalid_state"


async def test_failed_refund_releases_its_reservation(client):
    _, key = await create_merchant(client)
    payment_id = await captured_payment(client, key, 1_000, number=REFUND_FAILS)

    first = await refund(client, key, payment_id, 1_000)
    second = await refund(client, key, payment_id, 1_000)

    # Both are accepted, so the first failure gave its 1000 back. Both then fail at the processor.
    assert first.status_code == second.status_code == 201
    assert first.json()["status"] == second.json()["status"] == "failed"
    assert first.json()["failure_reason"] == "refund_declined"
    payment = await get_payment(client, key, payment_id)
    assert (payment["status"], payment["amount_refunded"]) == ("captured", 0)
    assert await merchant_balance(client, key) == 1_000


async def test_refund_retry_replays_and_refunds_once(client):
    _, key = await create_merchant(client)
    payment_id = await captured_payment(client, key, 1_000)
    idem = new_key()

    first = await refund(client, key, payment_id, 300, key=idem)
    retry = await refund(client, key, payment_id, 300, key=idem)

    assert retry.content == first.content
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert await merchant_balance(client, key) == 700


async def test_other_merchant_cannot_refund(client):
    _, owner = await create_merchant(client, "Owner")
    _, other = await create_merchant(client, "Other")
    payment_id = await captured_payment(client, owner)

    assert (await refund(client, other, payment_id, 100)).status_code == 404
    assert (await get_payment(client, owner, payment_id))["amount_refunded"] == 0
