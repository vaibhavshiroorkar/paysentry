"""Payment lifecycle. See docs/design/payments.md.

Every operation that calls the processor runs in three steps: transaction A
claims the idempotency key and enters an in-flight state, the processor call
runs with no transaction open, and transaction B records the outcome.
"""

from uuid import UUID

import asyncpg

from app import idempotency, ledger
from app.auth import Merchant, merchant_owner
from app.errors import ApiError, CaptureExceedsAuthorized, InvalidState, NotFound, ProcessorDeclined
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


async def _refusal(conn, merchant: Merchant, payment_id: UUID, capture_amount: int | None = None) -> ApiError:
    """Why a conditional transition matched no row."""
    row = await conn.fetchrow(
        "SELECT status, amount_minor FROM payments WHERE id = $1 AND merchant_id = $2", payment_id, merchant.id
    )
    if row is None:
        return NotFound("Payment not found")
    if row["status"] == "authorized" and capture_amount is not None and capture_amount > row["amount_minor"]:
        return CaptureExceedsAuthorized(f"Capture amount exceeds the authorized {row['amount_minor']}")
    return InvalidState(f"Payment is {row['status']}")


async def capture_payment(
    pool: asyncpg.Pool, processor: FakeProcessor, merchant: Merchant, payment_id: UUID, key: str,
    amount_minor: int | None,
) -> StoredResponse:
    async with pool.acquire() as conn, conn.transaction():
        stored = await idempotency.claim(
            conn, merchant_id=merchant.id, key=key, method="POST", path=f"/payments/{payment_id}/capture",
            fingerprint={"amount_minor": amount_minor},
        )
        if stored is not None:
            return stored
        # The status guard is the whole concurrency story: a second capture
        # waits for this row lock, then re-checks the WHERE against the new
        # row, finds capture_pending, and matches nothing.
        payment = await conn.fetchrow(
            """
            UPDATE payments
            SET status = 'capture_pending', pending_capture_minor = COALESCE($3, amount_minor), updated_at = now()
            WHERE id = $1 AND merchant_id = $2 AND status = 'authorized'
              AND COALESCE($3, amount_minor) <= amount_minor
            RETURNING *
            """,
            payment_id, merchant.id, amount_minor,
        )
        if payment is None:
            raise await _refusal(conn, merchant, payment_id, amount_minor)

    result = await processor.capture(
        reference=payment["processor_reference"], amount_minor=payment["pending_capture_minor"]
    )

    async with pool.acquire() as conn, conn.transaction():
        if result.approved:
            payment = await conn.fetchrow(
                """
                UPDATE payments
                SET status = 'captured', amount_captured = pending_capture_minor,
                    pending_capture_minor = NULL, updated_at = now()
                WHERE id = $1 AND status = 'capture_pending'
                RETURNING *
                """,
                payment_id,
            )
            await post_capture(conn, payment, payment["amount_captured"])
            status_code, body = 200, payment_body(payment)
        else:
            await conn.execute(
                """
                UPDATE payments SET status = 'authorized', pending_capture_minor = NULL, updated_at = now()
                WHERE id = $1 AND status = 'capture_pending'
                """,
                payment_id,
            )
            status_code, body = 402, ProcessorDeclined(result.decline_code).body()
        return await idempotency.complete(conn, merchant_id=merchant.id, key=key, status_code=status_code, body=body)


async def void_payment(
    pool: asyncpg.Pool, processor: FakeProcessor, merchant: Merchant, payment_id: UUID, key: str
) -> StoredResponse:
    async with pool.acquire() as conn, conn.transaction():
        stored = await idempotency.claim(
            conn, merchant_id=merchant.id, key=key, method="POST", path=f"/payments/{payment_id}/void",
            fingerprint={},
        )
        if stored is not None:
            return stored
        payment = await conn.fetchrow(
            """
            UPDATE payments SET status = 'void_pending', updated_at = now()
            WHERE id = $1 AND merchant_id = $2 AND status = 'authorized'
            RETURNING *
            """,
            payment_id, merchant.id,
        )
        if payment is None:
            raise await _refusal(conn, merchant, payment_id)

    result = await processor.void(reference=payment["processor_reference"])

    async with pool.acquire() as conn, conn.transaction():
        payment = await conn.fetchrow(
            """
            UPDATE payments SET status = $2, updated_at = now()
            WHERE id = $1 AND status = 'void_pending'
            RETURNING *
            """,
            payment_id, "voided" if result.approved else "authorized",
        )
        if result.approved:
            status_code, body = 200, payment_body(payment)
        else:
            status_code, body = 402, ProcessorDeclined(result.decline_code).body()
        return await idempotency.complete(conn, merchant_id=merchant.id, key=key, status_code=status_code, body=body)
