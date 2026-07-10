"""Connectors classify every gateway answer into one of the outcomes in gateways.md."""

import httpx
import pytest

from app.cards import Card
from app.connectors import build_connector
from app.connectors.beta import from_major, to_major
from gateway.app import create_gateway_app
from payment_helpers import CAPTURE_FAILS, DECLINED, REFUND_FAILS, VISA

FLAVOURS = pytest.mark.parametrize("flavour", ["alpha", "beta"])


class FailingTransport(httpx.AsyncBaseTransport):
    """Fails the way a network does. With `inner`, the gateway acts first."""

    def __init__(self, error: Exception, inner: httpx.AsyncBaseTransport | None = None):
        self.error = error
        self.inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.inner is not None:
            await self.inner.handle_async_request(request)
        raise self.error


class StatusTransport(httpx.AsyncBaseTransport):
    def __init__(self, status_code: int):
        self.status_code = status_code

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(self.status_code, json={})


def card(number: str = VISA) -> Card:
    return Card(number=number, exp_month=12, exp_year=2030, cvc="987")


def connect(flavour: str, transport=None, timeout: float = 0.5):
    app = create_gateway_app(flavour)
    gateway_transport = httpx.ASGITransport(app=app)
    if callable(transport):
        transport = transport(gateway_transport)
    client = httpx.AsyncClient(transport=transport or gateway_transport, base_url="http://gateway")
    return build_connector(flavour, flavour, client, timeout), app.state.gateway


def authorize(connector, number: str = VISA, capture: bool = False, reference: str = "ref-1"):
    return connector.authorize(reference=reference, amount_minor=1_234, currency="INR", card=card(number), capture=capture)


@FLAVOURS
async def test_authorization_is_approved(flavour):
    connector, gateway = connect(flavour)

    result = await authorize(connector)

    assert (result.outcome, result.status) == ("approved", "authorized")
    assert result.gateway_reference == gateway.charges_by_reference["ref-1"].id
    assert gateway.charges_by_reference["ref-1"].amount == 1_234


@FLAVOURS
async def test_automatic_capture_reports_captured(flavour):
    connector, _ = connect(flavour)
    result = await authorize(connector, capture=True)
    assert (result.outcome, result.status) == ("approved", "captured")


@FLAVOURS
async def test_decline_card_is_declined(flavour):
    connector, _ = connect(flavour)
    result = await authorize(connector, DECLINED)
    assert (result.outcome, result.decline_code) == ("declined", "card_declined")


@FLAVOURS
async def test_capture_void_and_refund(flavour):
    connector, _ = connect(flavour)
    to_capture = await authorize(connector, reference="a")
    to_void = await authorize(connector, reference="b")

    captured = await connector.capture(gateway_reference=to_capture.gateway_reference, reference="capture:a", amount_minor=600, currency="INR")
    voided = await connector.void(gateway_reference=to_void.gateway_reference, reference="void:b")
    refunded = await connector.refund(gateway_reference=to_capture.gateway_reference, reference="r1", amount_minor=100, currency="INR")

    assert (captured.outcome, captured.status) == ("approved", "captured")
    assert (voided.outcome, voided.status) == ("approved", "voided")
    assert refunded.outcome == "approved"


@FLAVOURS
async def test_gateway_refusals_are_declines(flavour):
    connector, _ = connect(flavour)
    capture_fails = await authorize(connector, CAPTURE_FAILS, reference="a")
    refund_fails = await authorize(connector, REFUND_FAILS, capture=True, reference="b")

    capture = await connector.capture(gateway_reference=capture_fails.gateway_reference, reference="c", amount_minor=1_234, currency="INR")
    void = await connector.void(gateway_reference=capture_fails.gateway_reference, reference="v")
    refund = await connector.refund(gateway_reference=refund_fails.gateway_reference, reference="r", amount_minor=100, currency="INR")

    assert (capture.outcome, capture.decline_code) == ("declined", "capture_declined")
    assert (void.outcome, void.decline_code) == ("declined", "void_declined")
    assert (refund.outcome, refund.decline_code) == ("declined", "refund_declined")


@FLAVOURS
async def test_lookup_finds_by_reference(flavour):
    connector, _ = connect(flavour)
    await authorize(connector, reference="known")
    await authorize(connector, DECLINED, reference="refused")

    found = await connector.lookup(reference="known")
    refused = await connector.lookup(reference="refused")
    missing = await connector.lookup(reference="never-sent")

    assert (found.outcome, found.status) == ("approved", "authorized")
    assert (refused.outcome, refused.decline_code) == ("declined", "card_declined")
    assert missing.outcome == "not_found"


@FLAVOURS
async def test_503_is_not_processed(flavour):
    connector, gateway = connect(flavour)
    gateway.configure(unavailable_rate=1.0)

    result = await authorize(connector)

    assert result.outcome == "not_processed"
    assert gateway.charges_by_reference == {}


@FLAVOURS
@pytest.mark.parametrize("error", [httpx.ConnectError("refused"), httpx.ConnectTimeout("slow handshake")])
async def test_connection_failure_is_not_processed(flavour, error):
    connector, _ = connect(flavour, transport=FailingTransport(error))
    assert (await authorize(connector)).outcome == "not_processed"


@FLAVOURS
async def test_deadline_after_sending_is_unknown(flavour):
    connector, gateway = connect(flavour, timeout=0.1)
    gateway.configure(unknown_rate=1.0, hang_ms=1_000)

    result = await authorize(connector)

    assert result.outcome == "unknown"
    assert "ref-1" in gateway.charges_by_reference


@FLAVOURS
async def test_connection_dropped_after_gateway_acted_is_unknown(flavour):
    connector, gateway = connect(flavour, transport=lambda inner: FailingTransport(httpx.ReadError("reset"), inner))

    result = await authorize(connector)

    assert result.outcome == "unknown"
    assert "ref-1" in gateway.charges_by_reference


@FLAVOURS
@pytest.mark.parametrize("status_code", [500, 502, 504])
async def test_server_errors_other_than_503_are_unknown(flavour, status_code):
    connector, _ = connect(flavour, transport=StatusTransport(status_code))
    assert (await authorize(connector)).outcome == "unknown"


@pytest.mark.parametrize("minor, text", [(5, "0.05"), (100, "1.00"), (123_456, "1234.56")])
def test_beta_money_to_major_units(minor, text):
    assert to_major(minor) == text


@pytest.mark.parametrize("text, minor", [("12.5", 1_250), ("12", 1_200), ("0.05", 5), ("1234.56", 123_456)])
def test_beta_money_from_major_units(text, minor):
    assert from_major(text) == minor


def test_beta_money_rejects_fractions_of_a_paisa():
    with pytest.raises(ValueError):
        from_major("12.345")
