"""Payment lifecycle. See docs/design/payments.md.

Every operation that calls the processor runs in three steps: transaction A
claims the idempotency key and enters an in-flight state, the processor call
runs with no transaction open, and transaction B records the outcome.
"""

from uuid import UUID

import asyncpg

from app import idempotency, ledger
from app.auth import Merchant, merchant_owner
from app.errors import NotFound
from app.idempotency import StoredResponse
from app.models import PaymentCreate
from app.processor import FakeProcessor

CLEARING = "system:gateway_clearing"


def refund_body(row) -> dict:
    return {
        "id": str(row["id"]),
        "payment_id": str(row["payment_id"]),
        "amount_minor": row["amount_minor"],
        "status": row["status"],
        "failure_reason": row["failure_reason"],
        "created_at": row["created_at"].isoformat(),
    }


def payment_body(row, refunds=()) -> dict:
    return {
        "id": str(row["id"]),
        "amount_minor": row["amount_minor"],
        "currency": row["currency"],
        "capture_method": row["capture_method"],
        "status": row["status"],
        "amount_captured": row["amount_captured"],
        "amount_refunded": row["amount_refunded"],
        "card": {"brand": row["card_brand"], "last4": row["card_last4"]},
        "failure_reason": row["failure_reason"],
        "created_at": row["created_at"].isoformat(),
        "refunds": [refund_body(r) for r in refunds],
    }


async def _post(conn, key: str, from_owner: str, to_owner: str, amount_minor: int, currency: str) -> None:
    result = await ledger.post_in_transaction(
        conn,
        idempotency_key=key,
        from_account_id=await ledger.account_id_for(conn, from_owner, currency),
        to_account_id=await ledger.account_id_for(conn, to_owner, currency),
        amount_minor=amount_minor,
        currency=currency,
        allow_system_source=True,
    )
    if result.body["status"] != "SUCCEEDED":
        # Amounts are checked against the payment before posting, so a refusal
        # here means the payment and the ledger disagree. Roll back instead.
        raise RuntimeError(f"Ledger refused posting {key}: {result.body}")


async def post_capture(conn, payment, amount_minor: int) -> None:
    await _post(
        conn, f"payment-capture:{payment['id']}", CLEARING, merchant_owner(payment["merchant_id"]),
        amount_minor, payment["currency"],
    )


async def create_payment(
    pool: asyncpg.Pool, processor: FakeProcessor, merchant: Merchant, key: str, req: PaymentCreate
) -> StoredResponse:
    fingerprint = {
        "amount_minor": req.amount_minor,
        "currency": req.currency,
        "capture_method": req.capture_method,
        "card": req.card.fingerprint(),
    }
    async with pool.acquire() as conn, conn.transaction():
        stored = await idempotency.claim(
            conn, merchant_id=merchant.id, key=key, method="POST", path="/payments", fingerprint=fingerprint
        )
        if stored is not None:
            return stored
        payment = await conn.fetchrow(
            """
            INSERT INTO payments (merchant_id, amount_minor, currency, capture_method, status, card_brand, card_last4)
            VALUES ($1, $2, $3, $4, 'processing', $5, $6)
            RETURNING *
            """,
            merchant.id, req.amount_minor, req.currency, req.capture_method, req.card.brand, req.card.last4,
        )

    automatic = req.capture_method == "automatic"
    result = await processor.authorize(
        payment_id=payment["id"], amount_minor=req.amount_minor, currency=req.currency, card=req.card, capture=automatic
    )

    if not result.approved:
        status, captured = "failed", 0
    elif automatic:
        status, captured = "captured", req.amount_minor
    else:
        status, captured = "authorized", 0

    async with pool.acquire() as conn, conn.transaction():
        payment = await conn.fetchrow(
            """
            UPDATE payments
            SET status = $2, amount_captured = $3, failure_reason = $4, processor_reference = $5, updated_at = now()
            WHERE id = $1 AND status = 'processing'
            RETURNING *
            """,
            payment["id"], status, captured, result.decline_code, result.reference,
        )
        if status == "captured":
            await post_capture(conn, payment, captured)
        return await idempotency.complete(
            conn, merchant_id=merchant.id, key=key, status_code=201, body=payment_body(payment)
        )


async def get_payment(pool: asyncpg.Pool, merchant: Merchant, payment_id: UUID) -> dict:
    async with pool.acquire() as conn:
        # One snapshot, so the payment and its refunds agree.
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            payment = await conn.fetchrow(
                "SELECT * FROM payments WHERE id = $1 AND merchant_id = $2", payment_id, merchant.id
            )
            if payment is None:
                raise NotFound("Payment not found")
            refunds = await conn.fetch(
                "SELECT * FROM refunds WHERE payment_id = $1 ORDER BY created_at, id", payment_id
            )
    return payment_body(payment, refunds)
