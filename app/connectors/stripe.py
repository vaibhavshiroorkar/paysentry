import time

import httpx

from app.cards import Card
from app.connectors.base import NOT_FOUND, NOT_PROCESSED, UNKNOWN, GatewayResult, HttpConnector, refused

# Stripe refuses raw card numbers from a server that has not proven PCI
# compliance, test mode included. Its test PaymentMethods stand in for the
# test cards in docs/design/payments.md.
TEST_PAYMENT_METHODS = {
    "4242424242424242": "pm_card_visa",
    "5555555555554444": "pm_card_mastercard",
    "4000000000000002": "pm_card_visa_chargeDeclined",
    "4000000000009995": "pm_card_visa_chargeDeclinedInsufficientFunds",
    "4000000000000341": "pm_card_chargeCustomerFail",
    "4000000000005126": "pm_card_refundFail",
}
STATES = {"requires_capture": "authorized", "succeeded": "captured", "canceled": "voided"}

# Stripe has no lookup by our reference: search is eventually consistent and
# not offered in India. The list API is consistent, so lookup scans recent
# PaymentIntents. Recovery runs within minutes, well inside this window.
LOOKUP_WINDOW_SEC = 2 * 86_400
LOOKUP_PAGES = 10


def _error(response: httpx.Response) -> dict:
    try:
        error = response.json()["error"]
    except (ValueError, KeyError, TypeError):
        return {}
    return error if isinstance(error, dict) else {}


class StripeConnector(HttpConnector):
    """Stripe PaymentIntents: form encoded, integer minor units, our reference
    as the idempotency key and in metadata."""

    # Stripe makes no promise about a 503: the request may have been applied.
    unprocessed_statuses = frozenset()

    async def post(self, path: str, reference: str, data: dict) -> httpx.Response | GatewayResult:
        response = await self.send("POST", path, data=data, headers={"Idempotency-Key": reference})
        return self._classify(response)

    def _classify(self, response: httpx.Response | GatewayResult) -> httpx.Response | GatewayResult:
        if isinstance(response, GatewayResult) or response.status_code == 200:
            return response
        error = _error(response)
        if response.status_code in (401, 403, 429):
            # A bad key or a rate limit: Stripe did nothing, so another gateway may try.
            return NOT_PROCESSED
        if response.status_code == 409:
            # Another request with this idempotency key is still running.
            return UNKNOWN
        if response.status_code == 402:
            intent = error.get("payment_intent") or {}
            return GatewayResult(
                "declined", intent.get("id"), decline_code=error.get("decline_code") or error.get("code") or "card_declined"
            )
        return refused(response, error.get("code"))

    def _intent(self, response: httpx.Response | GatewayResult) -> GatewayResult:
        if isinstance(response, GatewayResult):
            return response
        intent = response.json()
        status = intent["status"]
        if status in STATES:
            return GatewayResult("approved", intent["id"], status=STATES[status])
        if status in ("requires_payment_method", "requires_action", "requires_confirmation"):
            last_error = intent.get("last_payment_error") or {}
            code = last_error.get("decline_code") or last_error.get("code")
            if status == "requires_action":
                code = "authentication_required"
            return GatewayResult("declined", intent["id"], decline_code=code or "card_declined")
        # processing: Stripe has not decided yet. Recovery asks again later.
        return UNKNOWN

    async def authorize(
        self, *, reference: str, amount_minor: int, currency: str, card: Card, capture: bool
    ) -> GatewayResult:
        payment_method = TEST_PAYMENT_METHODS.get(card.number.get_secret_value())
        if payment_method is None:
            return GatewayResult("declined", decline_code="stripe_test_card_required")
        response = await self.post(
            "/v1/payment_intents",
            reference,
            {
                "amount": amount_minor,
                "currency": currency.lower(),
                "payment_method": payment_method,
                "payment_method_types[]": "card",
                "confirm": "true",
                "capture_method": "automatic" if capture else "manual",
                "description": f"PaySentry {reference}",
                "metadata[reference]": reference,
            },
        )
        return self._intent(response)

    async def lookup(self, *, reference: str) -> GatewayResult:
        params = {"limit": 100, "created[gte]": int(time.time()) - LOOKUP_WINDOW_SEC}
        for _ in range(LOOKUP_PAGES):
            response = self._classify(await self.send("GET", "/v1/payment_intents", params=params))
            if isinstance(response, GatewayResult):
                return response if response.outcome != "declined" else UNKNOWN
            page = response.json()
            for intent in page["data"]:
                if (intent.get("metadata") or {}).get("reference") == reference:
                    return self._intent(httpx.Response(200, json=intent))
            if not page.get("has_more") or not page["data"]:
                return NOT_FOUND
            params["starting_after"] = page["data"][-1]["id"]
        # Too many recent intents to be sure it is absent.
        return UNKNOWN

    async def capture(
        self, *, gateway_reference: str, reference: str, amount_minor: int, currency: str
    ) -> GatewayResult:
        response = await self.post(
            f"/v1/payment_intents/{gateway_reference}/capture", reference, {"amount_to_capture": amount_minor}
        )
        return self._intent(response)

    async def void(self, *, gateway_reference: str, reference: str) -> GatewayResult:
        response = await self.post(f"/v1/payment_intents/{gateway_reference}/cancel", reference, {})
        return self._intent(response)

    async def refund(
        self, *, gateway_reference: str, reference: str, amount_minor: int, currency: str
    ) -> GatewayResult:
        response = await self.post(
            "/v1/refunds",
            reference,
            {"payment_intent": gateway_reference, "amount": amount_minor, "metadata[reference]": reference},
        )
        if isinstance(response, GatewayResult):
            return response
        refund = response.json()
        if refund["status"] in ("succeeded", "pending"):
            # Card refunds settle later. A refund that fails after this point
            # reaches Stripe's webhooks, which PaySentry does not consume.
            return GatewayResult("approved", refund["id"])
        return GatewayResult("declined", refund["id"], decline_code=refund.get("failure_reason") or "refund_failed")
