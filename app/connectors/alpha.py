import httpx

from app.cards import Card
from app.connectors.base import NOT_FOUND, GatewayResult, HttpConnector, refused


def _error_code(response: httpx.Response) -> str | None:
    try:
        return response.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return None


class AlphaConnector(HttpConnector):
    """Alpha: snake case JSON, integer minor units, refusals as 402."""

    def _charge(self, response: httpx.Response | GatewayResult) -> GatewayResult:
        if isinstance(response, GatewayResult):
            return response
        if response.status_code != 200:
            return refused(response, _error_code(response))
        body = response.json()
        if body["status"] == "declined":
            return GatewayResult("declined", body["id"], decline_code=body["decline_code"])
        return GatewayResult("approved", body["id"], status=body["status"])

    async def authorize(
        self, *, reference: str, amount_minor: int, currency: str, card: Card, capture: bool
    ) -> GatewayResult:
        response = await self.send(
            "POST",
            "/v1/charges",
            json={
                "reference": reference,
                "amount": amount_minor,
                "currency": currency,
                "capture": capture,
                "card": {
                    "number": card.number.get_secret_value(),
                    "exp_month": card.exp_month,
                    "exp_year": card.exp_year,
                    "cvc": card.cvc.get_secret_value(),
                },
            },
        )
        return self._charge(response)

    async def lookup(self, *, reference: str) -> GatewayResult:
        response = await self.send("GET", "/v1/charges", params={"reference": reference})
        if isinstance(response, httpx.Response) and response.status_code == 404:
            return NOT_FOUND
        return self._charge(response)

    async def capture(
        self, *, gateway_reference: str, reference: str, amount_minor: int, currency: str
    ) -> GatewayResult:
        response = await self.send(
            "POST", f"/v1/charges/{gateway_reference}/capture", json={"amount": amount_minor, "reference": reference}
        )
        return self._charge(response)

    async def void(self, *, gateway_reference: str, reference: str) -> GatewayResult:
        response = await self.send("POST", f"/v1/charges/{gateway_reference}/void", json={"reference": reference})
        return self._charge(response)

    async def refund(
        self, *, gateway_reference: str, reference: str, amount_minor: int, currency: str
    ) -> GatewayResult:
        response = await self.send(
            "POST", f"/v1/charges/{gateway_reference}/refunds", json={"amount": amount_minor, "reference": reference}
        )
        if isinstance(response, GatewayResult):
            return response
        if response.status_code != 200:
            return refused(response, _error_code(response))
        body = response.json()
        if body["status"] == "declined":
            return GatewayResult("declined", body["id"], decline_code=body["decline_code"])
        return GatewayResult("approved", body["id"])
