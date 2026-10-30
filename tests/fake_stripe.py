"""A small in-memory stand-in for the parts of Stripe's API the connector uses.

It follows Stripe's documented behaviour for the test PaymentMethods: form
encoded requests, idempotency keys that replay the first response, 402 with the
PaymentIntent on a decline, and list pages ordered newest first.
"""

import itertools
import json
import time
from urllib.parse import parse_qsl

import httpx

DECLINES = {
    "pm_card_visa_chargeDeclined": ("card_declined", "generic_decline"),
    "pm_card_visa_chargeDeclinedInsufficientFunds": ("card_declined", "insufficient_funds"),
    "pm_card_chargeCustomerFail": ("card_declined", "generic_decline"),
}


class FakeStripe:
    def __init__(self, page_size: int = 100):
        self.intents: dict[str, dict] = {}
        self.refunds: dict[str, dict] = {}
        self.replies: dict[str, httpx.Response] = {}
        self.requests: list[httpx.Request] = []
        self.page_size = page_size
        self._ids = itertools.count(1)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = request.headers.get("Idempotency-Key")
        if request.method == "POST" and key in self.replies:
            return self.replies[key]
        response = self.route(request)
        if request.method == "POST" and key:
            self.replies[key] = response
        return response

    def route(self, request: httpx.Request) -> httpx.Response:
        form = dict(parse_qsl(request.content.decode()))
        parts = request.url.path.strip("/").split("/")
        if request.method == "GET" and parts == ["v1", "payment_intents"]:
            return self.list_intents(request.url.params)
        if parts == ["v1", "payment_intents"]:
            return self.create(form)
        if parts[:2] == ["v1", "payment_intents"] and len(parts) == 4:
            return self.act(parts[2], parts[3], form)
        if parts == ["v1", "refunds"]:
            return self.refund(form)
        return error(404, "resource_missing")

    def create(self, form: dict) -> httpx.Response:
        intent = {
            "id": f"pi_{next(self._ids)}",
            "object": "payment_intent",
            "amount": int(form["amount"]),
            "currency": form["currency"],
            "created": int(time.time()),
            "capture_method": form["capture_method"],
            "payment_method": form["payment_method"],
            "metadata": {"reference": form.get("metadata[reference]")},
            "last_payment_error": None,
        }
        self.intents[intent["id"]] = intent
        if form["payment_method"] in DECLINES:
            code, decline_code = DECLINES[form["payment_method"]]
            intent["status"] = "requires_payment_method"
            intent["last_payment_error"] = {"code": code, "decline_code": decline_code}
            return httpx.Response(
                402,
                json={"error": {"type": "card_error", "code": code, "decline_code": decline_code, "payment_intent": intent}},
            )
        intent["status"] = "succeeded" if form["capture_method"] == "automatic" else "requires_capture"
        return httpx.Response(200, json=intent)

    def act(self, intent_id: str, action: str, form: dict) -> httpx.Response:
        intent = self.intents.get(intent_id)
        if intent is None:
            return error(404, "resource_missing")
        if intent["status"] != "requires_capture":
            return error(400, "payment_intent_unexpected_state")
        if action == "capture":
            intent["status"] = "succeeded"
            intent["amount_received"] = int(form.get("amount_to_capture", intent["amount"]))
        elif action == "cancel":
            intent["status"] = "canceled"
        else:
            return error(404, "resource_missing")
        return httpx.Response(200, json=intent)

    def refund(self, form: dict) -> httpx.Response:
        intent = self.intents.get(form["payment_intent"])
        if intent is None or intent["status"] != "succeeded":
            return error(400, "charge_not_refundable" if intent else "resource_missing")
        refund = {"id": f"re_{next(self._ids)}", "object": "refund", "amount": int(form["amount"]), "status": "succeeded"}
        self.refunds[refund["id"]] = refund
        return httpx.Response(200, json=refund)

    def list_intents(self, params) -> httpx.Response:
        newest_first = sorted(self.intents.values(), key=lambda i: int(i["id"].split("_")[1]), reverse=True)
        newest_first = [i for i in newest_first if i["created"] >= int(params.get("created[gte]", 0))]
        if after := params.get("starting_after"):
            ids = [i["id"] for i in newest_first]
            newest_first = newest_first[ids.index(after) + 1:]
        page = newest_first[: min(self.page_size, int(params.get("limit", 10)))]
        return httpx.Response(
            200, json={"object": "list", "data": page, "has_more": len(newest_first) > len(page)}
        )

    def form(self, request: httpx.Request) -> dict:
        return dict(parse_qsl(request.content.decode()))


def error(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, content=json.dumps({"error": {"type": "invalid_request_error", "code": code}}))
