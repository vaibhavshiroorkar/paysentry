"""The Stripe connector against a fake Stripe, and a payment through the whole app."""

import httpx
import pytest
from pydantic import ValidationError

from app.cards import Card
from app.config import Settings
from app.connectors import build_connector
from app.main import gateway_headers
from fake_stripe import FakeStripe
from helpers import create_merchant
from payment_helpers import DECLINED, NO_FUNDS, VISA, authorized_payment, captured_payment

TEST_KEY = "sk_test_" + "x" * 24


def card(number: str = VISA) -> Card:
    return Card(number=number, exp_month=12, exp_year=2030, cvc="987")


def connect(transport: httpx.AsyncBaseTransport):
    client = httpx.AsyncClient(transport=transport, base_url="https://api.stripe.test")
    return build_connector("stripe", "stripe", client, 0.5)


def authorize(connector, number: str = VISA, capture: bool = False, reference: str = "ref-1"):
    return connector.authorize(reference=reference, amount_minor=1_234, currency="INR", card=card(number), capture=capture)


class Status(httpx.MockTransport):
    def __init__(self, status: int, body: dict | None = None):
        super().__init__(lambda request: httpx.Response(status, json=body or {}))


async def test_manual_capture_authorizes_and_sends_our_reference():
    stripe = FakeStripe()
    result = await authorize(connect(stripe.transport()))

    assert (result.outcome, result.status) == ("approved", "authorized")
    request = stripe.requests[0]
    form = stripe.form(request)
    assert request.headers["Idempotency-Key"] == "ref-1"
    assert form["metadata[reference]"] == "ref-1"
    assert (form["amount"], form["currency"], form["capture_method"]) == ("1234", "inr", "manual")
    assert form["payment_method"] == "pm_card_visa"
    assert "4242" not in request.content.decode()


async def test_automatic_capture_reports_captured():
    result = await authorize(connect(FakeStripe().transport()), capture=True)
    assert (result.outcome, result.status) == ("approved", "captured")


@pytest.mark.parametrize("number, code", [(DECLINED, "generic_decline"), (NO_FUNDS, "insufficient_funds")])
async def test_declines_keep_stripes_code_and_intent(number, code):
    stripe = FakeStripe()
    result = await authorize(connect(stripe.transport()), number)
    assert (result.outcome, result.decline_code) == ("declined", code)
    assert result.gateway_reference in stripe.intents


async def test_cards_without_a_stripe_test_method_are_refused_without_a_request():
    stripe = FakeStripe()
    result = await authorize(connect(stripe.transport()), "4111111111111111")
    assert (result.outcome, result.decline_code) == ("declined", "stripe_test_card_required")
    assert stripe.requests == []


async def test_a_repeated_authorization_returns_the_first_intent():
    stripe = FakeStripe()
    connector = connect(stripe.transport())
    first, second = await authorize(connector), await authorize(connector)
    assert first.gateway_reference == second.gateway_reference
    assert len(stripe.intents) == 1


async def test_capture_void_and_refund():
    stripe = FakeStripe()
    connector = connect(stripe.transport())
    captured = await authorize(connector, reference="a")
    voided = await authorize(connector, reference="b")

    capture = await connector.capture(
        gateway_reference=captured.gateway_reference, reference="capture:a", amount_minor=1_000, currency="INR"
    )
    void = await connector.void(gateway_reference=voided.gateway_reference, reference="void:b")
    refund = await connector.refund(
        gateway_reference=captured.gateway_reference, reference="refund-1", amount_minor=500, currency="INR"
    )

    assert (capture.outcome, capture.status) == ("approved", "captured")
    assert stripe.intents[captured.gateway_reference]["amount_received"] == 1_000
    assert (void.outcome, void.status) == ("approved", "voided")
    assert refund.outcome == "approved" and refund.gateway_reference in stripe.refunds


async def test_capturing_twice_with_one_reference_acts_once():
    stripe = FakeStripe()
    connector = connect(stripe.transport())
    intent = (await authorize(connector)).gateway_reference
    for _ in range(2):
        result = await connector.capture(gateway_reference=intent, reference="capture:p", amount_minor=1_234, currency="INR")
        assert result.status == "captured"


async def test_refunding_an_uncaptured_intent_is_declined():
    connector = connect(FakeStripe().transport())
    intent = (await authorize(connector)).gateway_reference
    result = await connector.refund(gateway_reference=intent, reference="r", amount_minor=100, currency="INR")
    assert (result.outcome, result.decline_code) == ("declined", "charge_not_refundable")


async def test_lookup_finds_an_intent_by_our_reference_across_pages():
    stripe = FakeStripe(page_size=2)
    connector = connect(stripe.transport())
    await authorize(connector, reference="wanted")
    for n in range(5):
        await authorize(connector, reference=f"other-{n}")

    found = await connector.lookup(reference="wanted")
    missing = await connector.lookup(reference="never-sent")

    assert (found.outcome, found.status) == ("approved", "authorized")
    assert missing.outcome == "not_found"


async def test_lookup_reports_a_declined_intent():
    connector = connect(FakeStripe().transport())
    await authorize(connector, DECLINED, reference="d")
    assert (await connector.lookup(reference="d")).outcome == "declined"


@pytest.mark.parametrize(
    "status, outcome",
    [(401, "not_processed"), (403, "not_processed"), (429, "not_processed"), (409, "unknown"),
     (500, "unknown"), (502, "unknown"), (503, "unknown"), (504, "unknown")],
)
async def test_stripe_statuses_map_to_outcomes(status, outcome):
    # Unlike the mock gateways, Stripe does not promise that a 503 did nothing.
    assert (await authorize(connect(Status(status)))).outcome == outcome


async def test_an_intent_still_processing_is_unknown():
    transport = Status(200, {"id": "pi_1", "status": "processing"})
    assert (await authorize(connect(transport))).outcome == "unknown"


async def test_an_intent_needing_3d_secure_is_declined():
    transport = Status(200, {"id": "pi_1", "status": "requires_action"})
    result = await authorize(connect(transport))
    assert (result.outcome, result.decline_code) == ("declined", "authentication_required")


async def test_a_failed_lookup_is_retried_later():
    assert (await connect(Status(400, {"error": {"code": "x"}})).lookup(reference="r")).outcome == "unknown"
    assert (await connect(Status(503)).lookup(reference="r")).outcome == "unknown"


STRIPE_GATEWAY = {"stripe": {"flavour": "stripe", "url": "https://api.stripe.com"}}


@pytest.mark.parametrize("key", [None, "sk_live_" + "x" * 24, "pk_test_" + "x" * 24])
def test_a_stripe_gateway_needs_a_test_mode_secret_key(key):
    with pytest.raises(ValidationError, match="test mode key"):
        Settings(_env_file=None, gateways=STRIPE_GATEWAY, stripe_secret_key=key)


def test_the_key_is_sent_only_to_stripe():
    settings = Settings(_env_file=None, gateways=STRIPE_GATEWAY | {"alpha": {"flavour": "alpha", "url": "http://a"}},
                        stripe_secret_key=TEST_KEY)
    assert gateway_headers(settings, settings.gateways["stripe"]) == {"Authorization": f"Bearer {TEST_KEY}"}
    assert gateway_headers(settings, settings.gateways["alpha"]) == {}
    assert TEST_KEY not in repr(settings)


async def test_payments_run_through_stripe_end_to_end(make_client):
    stripe = FakeStripe()
    client = await make_client(
        gateways=STRIPE_GATEWAY, stripe_secret_key=TEST_KEY, extra_transports={"stripe": stripe.transport()}
    )
    _, api_key = await create_merchant(client)

    payment_id = await authorized_payment(client, api_key, amount=2_000)
    captured = await client.post(
        f"/payments/{payment_id}/capture", json={"amount_minor": 1_500}, headers=_headers(api_key)
    )
    refunded = await client.post(
        f"/payments/{payment_id}/refunds", json={"amount_minor": 500}, headers=_headers(api_key)
    )
    other = await captured_payment(client, api_key)

    assert captured.json()["status"] == "captured", captured.text
    assert refunded.json()["status"] == "succeeded", refunded.text
    assert (await client.get(f"/payments/{other}", headers=_headers(api_key))).json()["gateway"] == "stripe"
    assert all(r.headers["Authorization"] == f"Bearer {TEST_KEY}" for r in stripe.requests)


def _headers(api_key: str) -> dict:
    from helpers import merchant_headers, new_key

    return merchant_headers(api_key, new_key())
