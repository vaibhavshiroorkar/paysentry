"""The mock gateway service itself, called the way a connector would call it."""

import time

import httpx
import pytest

from gateway.app import create_gateway_app
from payment_helpers import CAPTURE_FAILS, DECLINED, NO_FUNDS, REFUND_FAILS, VISA


@pytest.fixture
async def alpha():
    app = create_gateway_app("alpha")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://alpha") as client:
        yield client, app.state.gateway


@pytest.fixture
async def beta():
    app = create_gateway_app("beta")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://beta") as client:
        yield client, app.state.gateway


def alpha_charge(client, reference="ref-1", amount=1_000, number=VISA, capture=False):
    return client.post(
        "/v1/charges",
        json={
            "reference": reference,
            "amount": amount,
            "currency": "INR",
            "capture": capture,
            "card": {"number": number, "exp_month": 12, "exp_year": 2030, "cvc": "987"},
        },
    )


def beta_payment(client, ref="ref-1", value="10.00", number=VISA, auto=False):
    return client.post(
        "/api/payments",
        json={
            "merchantRef": ref,
            "money": {"value": value, "currency": "INR"},
            "autoCapture": auto,
            "cardNumber": number,
            "expiry": "12/30",
            "cvv": "987",
        },
    )


async def test_alpha_charge_is_idempotent_on_reference(alpha):
    client, state = alpha

    first = await alpha_charge(client)
    again = await alpha_charge(client)

    assert first.status_code == again.status_code == 200
    assert first.json()["status"] == "authorized"
    assert again.json()["id"] == first.json()["id"]
    assert len(state.charges_by_reference) == 1


@pytest.mark.parametrize("number, code", [(DECLINED, "card_declined"), (NO_FUNDS, "insufficient_funds")])
async def test_alpha_declines_decline_cards(alpha, number, code):
    client, _ = alpha
    body = (await alpha_charge(client, number=number)).json()
    assert (body["status"], body["decline_code"]) == ("declined", code)


async def test_alpha_capture_partial_then_repeat_is_idempotent(alpha):
    client, _ = alpha
    charge = (await alpha_charge(client)).json()

    captured = await client.post(f"/v1/charges/{charge['id']}/capture", json={"amount": 600, "reference": "cap-1"})
    again = await client.post(f"/v1/charges/{charge['id']}/capture", json={"amount": 600, "reference": "cap-1"})

    assert captured.status_code == again.status_code == 200
    assert (captured.json()["status"], captured.json()["captured_amount"]) == ("captured", 600)
    assert again.json() == captured.json()


async def test_alpha_capture_above_amount_is_400(alpha):
    client, _ = alpha
    charge = (await alpha_charge(client)).json()
    response = await client.post(f"/v1/charges/{charge['id']}/capture", json={"amount": 1_001, "reference": "c"})
    assert response.status_code == 400


async def test_alpha_capture_and_void_fail_for_the_capture_failure_card(alpha):
    client, _ = alpha
    charge = (await alpha_charge(client, number=CAPTURE_FAILS)).json()

    capture = await client.post(f"/v1/charges/{charge['id']}/capture", json={"amount": 1_000, "reference": "c"})
    void = await client.post(f"/v1/charges/{charge['id']}/void", json={"reference": "v"})

    assert (capture.status_code, capture.json()["error"]["code"]) == (402, "capture_declined")
    assert (void.status_code, void.json()["error"]["code"]) == (402, "void_declined")


async def test_alpha_void(alpha):
    client, _ = alpha
    charge = (await alpha_charge(client)).json()
    response = await client.post(f"/v1/charges/{charge['id']}/void", json={"reference": "v"})
    assert response.json()["status"] == "voided"


async def test_alpha_refunds(alpha):
    client, _ = alpha
    charge = (await alpha_charge(client, capture=True)).json()
    url = f"/v1/charges/{charge['id']}/refunds"

    first = await client.post(url, json={"amount": 400, "reference": "r1"})
    again = await client.post(url, json={"amount": 400, "reference": "r1"})
    too_much = await client.post(url, json={"amount": 601, "reference": "r2"})

    assert first.json()["status"] == "succeeded"
    assert again.json()["id"] == first.json()["id"]
    assert too_much.status_code == 400


async def test_alpha_refund_fails_for_the_refund_failure_card(alpha):
    client, _ = alpha
    charge = (await alpha_charge(client, number=REFUND_FAILS, capture=True)).json()
    body = (await client.post(f"/v1/charges/{charge['id']}/refunds", json={"amount": 100, "reference": "r"})).json()
    assert (body["status"], body["decline_code"]) == ("declined", "refund_declined")


async def test_alpha_lookup_by_reference(alpha):
    client, _ = alpha
    charge = (await alpha_charge(client, reference="known")).json()

    found = await client.get("/v1/charges", params={"reference": "known"})
    missing = await client.get("/v1/charges", params={"reference": "unknown"})

    assert found.json() == charge
    assert missing.status_code == 404


async def test_unavailable_answers_503_and_does_nothing(alpha):
    client, state = alpha
    await client.post("/_control", json={"unavailable_rate": 1.0})

    response = await alpha_charge(client)

    assert response.status_code == 503
    assert state.charges_by_reference == {}


async def test_unknown_acts_and_then_hangs(alpha):
    client, state = alpha
    state.configure(unknown_rate=1.0, hang_ms=200)

    start = time.perf_counter()
    response = await alpha_charge(client)

    assert time.perf_counter() - start >= 0.2
    assert response.status_code == 200
    assert "ref-1" in state.charges_by_reference


async def test_decline_rate_declines_with_do_not_honor(alpha):
    client, state = alpha
    state.configure(decline_rate=1.0)
    body = (await alpha_charge(client)).json()
    assert (body["status"], body["decline_code"]) == ("declined", "do_not_honor")


async def test_beta_authorize_and_lookup(beta):
    client, state = beta

    first = await beta_payment(client, value="12.34")
    again = await beta_payment(client, value="12.34")
    found = await client.get("/api/payments", params={"merchantRef": "ref-1"})
    missing = await client.get("/api/payments", params={"merchantRef": "nope"})

    assert first.json()["state"] == "AUTHORISED"
    assert first.json()["money"] == {"value": "12.34", "currency": "INR"}
    assert again.json()["paymentId"] == first.json()["paymentId"]
    assert found.json() == first.json()
    assert missing.status_code == 404
    assert state.charges_by_reference["ref-1"].amount == 1_234


async def test_beta_refuses_decline_cards(beta):
    client, _ = beta
    body = (await beta_payment(client, number=DECLINED)).json()
    assert (body["state"], body["refusalReason"]) == ("REFUSED", "card_declined")


async def test_beta_rejects_sub_paisa_amounts(beta):
    client, _ = beta
    assert (await beta_payment(client, value="12.345")).status_code == 400


async def test_beta_capture_cancel_and_refund(beta):
    client, _ = beta
    to_capture = (await beta_payment(client, ref="a")).json()["paymentId"]
    to_cancel = (await beta_payment(client, ref="b")).json()["paymentId"]

    capture = await client.post(
        f"/api/payments/{to_capture}/captures", json={"merchantRef": "c", "money": {"value": "6.00", "currency": "INR"}}
    )
    cancel = await client.post(f"/api/payments/{to_cancel}/cancels", json={"merchantRef": "v"})
    refund = await client.post(
        f"/api/payments/{to_capture}/refunds", json={"merchantRef": "r", "money": {"value": "2.50", "currency": "INR"}}
    )

    assert (capture.json()["result"], capture.json()["state"]) == ("ACCEPTED", "CAPTURED")
    assert (cancel.json()["result"], cancel.json()["state"]) == ("ACCEPTED", "CANCELLED")
    assert refund.json()["result"] == "ACCEPTED"
    assert refund.json()["refundId"]


async def test_beta_refuses_capture_for_the_capture_failure_card(beta):
    client, _ = beta
    payment_id = (await beta_payment(client, number=CAPTURE_FAILS)).json()["paymentId"]
    body = (
        await client.post(
            f"/api/payments/{payment_id}/captures",
            json={"merchantRef": "c", "money": {"value": "10.00", "currency": "INR"}},
        )
    ).json()
    assert (body["result"], body["state"], body["refusalReason"]) == ("REFUSED", "AUTHORISED", "capture_declined")
