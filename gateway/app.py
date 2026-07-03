"""A mock card gateway, so PaySentry has something real to fail against.

One codebase, two API flavours (alpha and beta) that differ the way real
gateway APIs do: paths, field names, status names and money format. State is
in memory. Every operation is idempotent on the caller's reference, as real
gateways are. See docs/design/gateways.md.
"""

import asyncio
import os
import random
import uuid
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation

from fastapi import FastAPI
from fastapi.responses import JSONResponse

# The same test cards as docs/design/payments.md, so behaviour does not
# depend on which gateway a payment lands on.
DECLINE_CARDS = {"4000000000000002": "card_declined", "4000000000009995": "insufficient_funds"}
CARD_BEHAVIOURS = {"4000000000000341": "capture_fails", "4000000000005126": "refund_fails"}


@dataclass
class Behaviour:
    latency_ms: int = 0
    decline_rate: float = 0.0
    unavailable_rate: float = 0.0
    unknown_rate: float = 0.0
    hang_ms: int = 5_000
    seed: int | None = None


@dataclass
class Charge:
    id: str
    reference: str
    amount: int
    currency: str
    status: str  # authorized, captured, voided, declined
    decline_code: str | None = None
    behaviour: str = "ok"
    captured_amount: int = 0
    refunded_amount: int = 0
    capture_reference: str | None = None
    void_reference: str | None = None


@dataclass
class Refund:
    id: str
    reference: str
    charge_id: str
    amount: int
    currency: str
    status: str  # succeeded, declined
    decline_code: str | None = None


class OperationError(Exception):
    def __init__(self, status_code: int, code: str):
        super().__init__(code)
        self.status_code = status_code
        self.code = code


class GatewayState:
    def __init__(self) -> None:
        self.behaviour = Behaviour()
        self.rng = random.Random()
        self.charges: dict[str, Charge] = {}
        self.charges_by_reference: dict[str, Charge] = {}
        self.refunds_by_reference: dict[str, Refund] = {}

    def configure(self, **settings) -> None:
        for name, value in settings.items():
            if not hasattr(self.behaviour, name):
                raise ValueError(f"unknown setting {name}")
            setattr(self.behaviour, name, value)
        if settings.get("seed") is not None:
            self.rng.seed(settings["seed"])

    async def arrive(self) -> bool:
        """Network latency, then maybe refuse. False means answer 503 and do nothing."""
        if self.behaviour.latency_ms:
            await asyncio.sleep(self.behaviour.latency_ms / 1000)
        return self.rng.random() >= self.behaviour.unavailable_rate

    async def maybe_hang(self) -> None:
        """After acting, sometimes stall so the caller times out not knowing."""
        if self.rng.random() < self.behaviour.unknown_rate:
            await asyncio.sleep(self.behaviour.hang_ms / 1000)

    def charge(self, charge_id: str) -> Charge:
        if charge_id not in self.charges:
            raise OperationError(404, "not_found")
        return self.charges[charge_id]

    def lookup(self, reference: str) -> Charge:
        if reference not in self.charges_by_reference:
            raise OperationError(404, "not_found")
        return self.charges_by_reference[reference]

    def authorize(self, reference: str, amount: int, currency: str, number: str, capture: bool) -> Charge:
        if reference in self.charges_by_reference:
            return self.charges_by_reference[reference]
        charge = Charge(
            id="ch_" + uuid.uuid4().hex[:16], reference=reference, amount=amount, currency=currency,
            status="authorized", behaviour=CARD_BEHAVIOURS.get(number, "ok"),
        )
        if number in DECLINE_CARDS:
            charge.status, charge.decline_code = "declined", DECLINE_CARDS[number]
        elif self.rng.random() < self.behaviour.decline_rate:
            charge.status, charge.decline_code = "declined", "do_not_honor"
        elif capture:
            charge.status, charge.captured_amount = "captured", amount
        self.charges[charge.id] = charge
        self.charges_by_reference[reference] = charge
        return charge

    def capture(self, charge_id: str, amount: int, reference: str) -> Charge:
        charge = self.charge(charge_id)
        if charge.status == "captured" and charge.capture_reference == reference:
            return charge
        if charge.status != "authorized":
            raise OperationError(409, "invalid_state")
        if amount > charge.amount:
            raise OperationError(400, "amount_too_large")
        if charge.behaviour == "capture_fails":
            raise OperationError(402, "capture_declined")
        charge.status, charge.captured_amount, charge.capture_reference = "captured", amount, reference
        return charge

    def void(self, charge_id: str, reference: str) -> Charge:
        charge = self.charge(charge_id)
        if charge.status == "voided" and charge.void_reference == reference:
            return charge
        if charge.status != "authorized":
            raise OperationError(409, "invalid_state")
        if charge.behaviour == "capture_fails":
            raise OperationError(402, "void_declined")
        charge.status, charge.void_reference = "voided", reference
        return charge

    def refund(self, charge_id: str, amount: int, reference: str) -> Refund:
        if reference in self.refunds_by_reference:
            return self.refunds_by_reference[reference]
        charge = self.charge(charge_id)
        if charge.status != "captured":
            raise OperationError(409, "invalid_state")
        if amount > charge.captured_amount - charge.refunded_amount:
            raise OperationError(400, "amount_too_large")
        refund = Refund(
            id="re_" + uuid.uuid4().hex[:16], reference=reference, charge_id=charge.id, amount=amount,
            currency=charge.currency, status="succeeded",
        )
        if charge.behaviour == "refund_fails":
            refund.status, refund.decline_code = "declined", "refund_declined"
        else:
            charge.refunded_amount += amount
        self.refunds_by_reference[reference] = refund
        return refund


async def guarded(state: GatewayState, act, on_error) -> JSONResponse:
    """Run one gateway operation with the configured failure behaviour."""
    if not await state.arrive():
        return JSONResponse(status_code=503, content={"error": "temporarily unavailable"})
    try:
        body = act()
    except OperationError as error:
        return on_error(error)
    await state.maybe_hang()
    return JSONResponse(status_code=200, content=body)


# Alpha: snake case, integer minor units, lower case statuses.

def alpha_charge_body(charge: Charge) -> dict:
    return {
        "id": charge.id,
        "reference": charge.reference,
        "amount": charge.amount,
        "currency": charge.currency,
        "status": charge.status,
        "captured_amount": charge.captured_amount,
        "refunded_amount": charge.refunded_amount,
        "decline_code": charge.decline_code,
    }


def alpha_refund_body(refund: Refund) -> dict:
    return {
        "id": refund.id,
        "reference": refund.reference,
        "charge_id": refund.charge_id,
        "amount": refund.amount,
        "status": refund.status,
        "decline_code": refund.decline_code,
    }


def alpha_error(error: OperationError) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content={"error": {"code": error.code}})


def add_alpha_routes(app: FastAPI, state: GatewayState) -> None:
    @app.post("/v1/charges")
    async def create_charge(body: dict):
        card = body["card"]
        return await guarded(
            state,
            lambda: alpha_charge_body(
                state.authorize(body["reference"], body["amount"], body["currency"], card["number"], body["capture"])
            ),
            alpha_error,
        )

    @app.get("/v1/charges")
    async def find_charge(reference: str):
        return await guarded(state, lambda: alpha_charge_body(state.lookup(reference)), alpha_error)

    @app.post("/v1/charges/{charge_id}/capture")
    async def capture(charge_id: str, body: dict):
        return await guarded(
            state, lambda: alpha_charge_body(state.capture(charge_id, body["amount"], body["reference"])), alpha_error
        )

    @app.post("/v1/charges/{charge_id}/void")
    async def void(charge_id: str, body: dict):
        return await guarded(state, lambda: alpha_charge_body(state.void(charge_id, body["reference"])), alpha_error)

    @app.post("/v1/charges/{charge_id}/refunds")
    async def refund(charge_id: str, body: dict):
        return await guarded(
            state, lambda: alpha_refund_body(state.refund(charge_id, body["amount"], body["reference"])), alpha_error
        )


# Beta: camel case, decimal strings in major units, upper case states, and
# business refusals reported inside a 200 rather than as an error status.

BETA_STATES = {"authorized": "AUTHORISED", "captured": "CAPTURED", "voided": "CANCELLED", "declined": "REFUSED"}


def beta_money(minor: int, currency: str) -> dict:
    return {"value": f"{Decimal(minor) / 100:.2f}", "currency": currency}


def beta_minor(money: dict) -> int:
    try:
        value = Decimal(money["value"])
    except (InvalidOperation, KeyError, TypeError):
        raise OperationError(400, "invalid_money") from None
    if value <= 0 or value.as_tuple().exponent < -2:
        raise OperationError(400, "invalid_money")
    return int(value * 100)


def beta_payment_body(charge: Charge) -> dict:
    return {
        "paymentId": charge.id,
        "merchantRef": charge.reference,
        "state": BETA_STATES[charge.status],
        "refusalReason": charge.decline_code,
        "money": beta_money(charge.amount, charge.currency),
    }


def beta_operation_body(charge: Charge) -> dict:
    return {"paymentId": charge.id, "state": BETA_STATES[charge.status], "result": "ACCEPTED", "refusalReason": None}


def beta_refund_body(refund: Refund) -> dict:
    return {
        "refundId": refund.id,
        "merchantRef": refund.reference,
        "result": "ACCEPTED" if refund.status == "succeeded" else "REFUSED",
        "refusalReason": refund.decline_code,
        "money": beta_money(refund.amount, refund.currency),
    }


def beta_error(error: OperationError) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content={"errorCode": error.code.upper()})


def add_beta_routes(app: FastAPI, state: GatewayState) -> None:
    def refusable(charge_id: str, operation):
        def act():
            try:
                return beta_operation_body(operation())
            except OperationError as error:
                if error.status_code != 402:
                    raise
                charge = state.charge(charge_id)
                return {
                    "paymentId": charge.id,
                    "state": BETA_STATES[charge.status],
                    "result": "REFUSED",
                    "refusalReason": error.code,
                }

        return act

    @app.post("/api/payments")
    async def create_payment(body: dict):
        return await guarded(
            state,
            lambda: beta_payment_body(
                state.authorize(
                    body["merchantRef"], beta_minor(body["money"]), body["money"]["currency"],
                    body["cardNumber"], body["autoCapture"],
                )
            ),
            beta_error,
        )

    @app.get("/api/payments")
    async def find_payment(merchantRef: str):  # noqa: N803 (the gateway's own field name)
        return await guarded(state, lambda: beta_payment_body(state.lookup(merchantRef)), beta_error)

    @app.post("/api/payments/{payment_id}/captures")
    async def capture(payment_id: str, body: dict):
        return await guarded(
            state,
            refusable(payment_id, lambda: state.capture(payment_id, beta_minor(body["money"]), body["merchantRef"])),
            beta_error,
        )

    @app.post("/api/payments/{payment_id}/cancels")
    async def cancel(payment_id: str, body: dict):
        return await guarded(
            state, refusable(payment_id, lambda: state.void(payment_id, body["merchantRef"])), beta_error
        )

    @app.post("/api/payments/{payment_id}/refunds")
    async def refund(payment_id: str, body: dict):
        return await guarded(
            state,
            lambda: beta_refund_body(state.refund(payment_id, beta_minor(body["money"]), body["merchantRef"])),
            beta_error,
        )


def create_gateway_app(flavour: str) -> FastAPI:
    app = FastAPI(title=f"Mock gateway ({flavour})")
    state = GatewayState()
    app.state.gateway = state

    @app.post("/_control")
    async def control(settings: dict) -> dict:
        state.configure(**settings)
        return asdict(state.behaviour)

    if flavour == "alpha":
        add_alpha_routes(app, state)
    elif flavour == "beta":
        add_beta_routes(app, state)
    else:
        raise ValueError(f"unknown gateway flavour {flavour}")
    return app


app = create_gateway_app(os.environ.get("GATEWAY_FLAVOUR", "alpha"))
