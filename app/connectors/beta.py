from decimal import Decimal

import httpx

from app.cards import Card
from app.connectors.base import NOT_FOUND, GatewayResult, HttpConnector, refused

STATES = {"AUTHORISED": "authorized", "CAPTURED": "captured", "CANCELLED": "voided"}


def to_major(minor: int) -> str:
    """Minor units to Beta's decimal string. Decimal, never float: 0.1 has no exact float."""
    return f"{Decimal(minor) / 100:.2f}"


def from_major(text: str) -> int:
    value = Decimal(text)
    if value.as_tuple().exponent < -2:
        raise ValueError(f"{text} has fractions of a minor unit")
    return int(value * 100)


def _money(minor: int, currency: str) -> dict:
    return {"value": to_major(minor), "currency": currency}


def _error_code(response: httpx.Response) -> str | None:
    try:
        return response.json()["errorCode"].lower()
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


class BetaConnector(HttpConnector):
    """Beta: camel case JSON, decimal strings, refusals inside a 200."""

    def _payment(self, response: httpx.Response | GatewayResult) -> GatewayResult:
        if isinstance(response, GatewayResult):
            return response
        if response.status_code != 200:
            return refused(response, _error_code(response))
        body = response.json()
        if body["state"] == "REFUSED":
            return GatewayResult("declined", body["paymentId"], decline_code=body["refusalReason"])
        return GatewayResult("approved", body["paymentId"], status=STATES[body["state"]])

    def _operation(self, response: httpx.Response | GatewayResult) -> GatewayResult:
        if isinstance(response, GatewayResult):
            return response
        if response.status_code != 200:
            return refused(response, _error_code(response))
        body = response.json()
        if body["result"] == "REFUSED":
            return GatewayResult("declined", body["paymentId"], decline_code=body["refusalReason"])
        return GatewayResult("approved", body["paymentId"], status=STATES[body["state"]])

    async def authorize(
        self, *, reference: str, amount_minor: int, currency: str, card: Card, capture: bool
    ) -> GatewayResult:
        response = await self.send(
            "POST",
            "/api/payments",
            json={
                "merchantRef": reference,
                "money": _money(amount_minor, currency),
                "autoCapture": capture,
                "cardNumber": card.number.get_secret_value(),
                "expiry": f"{card.exp_month:02d}/{card.exp_year % 100:02d}",
                "cvv": card.cvc.get_secret_value(),
            },
        )
        return self._payment(response)

    async def lookup(self, *, reference: str) -> GatewayResult:
        response = await self.send("GET", "/api/payments", params={"merchantRef": reference})
        if isinstance(response, httpx.Response) and response.status_code == 404:
            return NOT_FOUND
        return self._payment(response)

    async def capture(
        self, *, gateway_reference: str, reference: str, amount_minor: int, currency: str
    ) -> GatewayResult:
        response = await self.send(
            "POST",
            f"/api/payments/{gateway_reference}/captures",
            json={"merchantRef": reference, "money": _money(amount_minor, currency)},
        )
        return self._operation(response)

    async def void(self, *, gateway_reference: str, reference: str) -> GatewayResult:
        response = await self.send(
            "POST", f"/api/payments/{gateway_reference}/cancels", json={"merchantRef": reference}
        )
        return self._operation(response)

    async def refund(
        self, *, gateway_reference: str, reference: str, amount_minor: int, currency: str
    ) -> GatewayResult:
        response = await self.send(
            "POST",
            f"/api/payments/{gateway_reference}/refunds",
            json={"merchantRef": reference, "money": _money(amount_minor, currency)},
        )
        if isinstance(response, GatewayResult):
            return response
        if response.status_code != 200:
            return refused(response, _error_code(response))
        body = response.json()
        if body["result"] == "REFUSED":
            return GatewayResult("declined", body["refundId"], decline_code=body["refusalReason"])
        return GatewayResult("approved", body["refundId"])
