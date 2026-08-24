"""Payment lifecycle. See docs/design/payments.md and docs/design/gateways.md.

Every operation that calls a gateway runs in three steps: transaction A claims
the idempotency key and enters an in-flight state, the gateway call runs with
no transaction open, and transaction B records the outcome.

Transaction B lives in the finish_* functions, which the recovery job also
calls. Each one is guarded by the in-flight status, so whoever finishes an
operation first wins and the other does nothing.
"""

from uuid import UUID

import asyncpg

from app import idempotency, ledger
from app.auth import Merchant, merchant_owner
from app.connectors import Connector, GatewayResult
from app.connectors.base import NOT_PROCESSED
from app.errors import (
    ApiError,
    CaptureExceedsAuthorized,
    GatewayUnavailable,
    InvalidState,
    NoGatewayAvailable,
    NotFound,
    ProcessorDeclined,
    RefundExceedsAvailable,
)
from app.idempotency import StoredResponse
from app.models import PaymentCreate
from app.routing import Router

CLEARING = "system:gateway_clearing"

# What an authorization outcome makes of the attempt row.
ATTEMPT_STATUS = {
    "approved": "succeeded",
    "declined": "declined",
    "not_processed": "failed",
    "not_found": "failed",
}


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


def in_flight(body: dict) -> StoredResponse:
    """The answer when the gateway's outcome is unknown. Never stored, so the
    idempotency key stays in progress until recovery stores the real one."""
    return StoredResponse(202, body, replayed=False)


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


async def release_key(conn, merchant_id: UUID, key: str) -> None:
    """Nothing happened at the gateway, so the key may carry a fresh attempt."""
    await conn.execute("DELETE FROM idempotency_keys WHERE merchant_id = $1 AND key = $2", merchant_id, key)


async def stored_response(pool: asyncpg.Pool, merchant_id: UUID, key: str) -> StoredResponse:
    """What the other finisher stored, when recovery beat the request path."""
    row = await pool.fetchrow(
        "SELECT state, response_code, response_body FROM idempotency_keys WHERE merchant_id = $1 AND key = $2",
        merchant_id, key,
    )
    if row is None or row["state"] != "completed":
        error = GatewayUnavailable("The gateway did not process this request")
        return StoredResponse(error.status_code, error.body(), replayed=False)
    return StoredResponse(row["response_code"], row["response_body"], replayed=False)


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


# Authorization


async def create_payment(
    pool: asyncpg.Pool, gateways: dict[str, Connector], router: Router, max_attempts: int, merchant: Merchant,
    key: str, req: PaymentCreate,
) -> StoredResponse:
    # Before the key is claimed: with no gateway to try, nothing happens, so
    # the client may retry with the same key.
    plan = (await router.plan(req.currency, req.card.brand))[:max_attempts]
    if not plan:
        raise NoGatewayAvailable("No gateway can take this payment right now")
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
            INSERT INTO payments (merchant_id, amount_minor, currency, capture_method, status,
                                  card_brand, card_last4, gateway, idempotency_key)
            VALUES ($1, $2, $3, $4, 'processing', $5, $6, $7, $8)
            RETURNING *
            """,
            merchant.id, req.amount_minor, req.currency, req.capture_method, req.card.brand, req.card.last4,
            plan[0], key,
        )
        attempt_id = await conn.fetchval(
            "INSERT INTO payment_attempts (payment_id, gateway, status) VALUES ($1, $2, 'pending') RETURNING id",
            payment["id"], plan[0],
        )

    result = NOT_PROCESSED
    for position, gateway in enumerate(plan):
        if position > 0:
            attempt_id = await _fail_over(pool, payment["id"], attempt_id, gateway)
        if not await router.acquire(gateway):
            continue  # Its circuit is open, or another request holds the half-open probe.
        # The attempt id is our reference at the gateway, so recovery can look it up.
        result = await gateways[gateway].authorize(
            reference=str(attempt_id), amount_minor=req.amount_minor, currency=req.currency, card=req.card,
            capture=req.capture_method == "automatic",
        )
        await router.record(gateway, result.outcome)
        # Only "not processed" is safe to retry elsewhere. After "unknown" the
        # gateway may have charged, and a second gateway could charge again.
        if result.outcome != "not_processed":
            break

    if result.outcome == "unknown":
        await pool.execute(
            "UPDATE payment_attempts SET status = 'unknown', updated_at = now() WHERE id = $1 AND status = 'pending'",
            attempt_id,
        )
        return in_flight(payment_body(payment))
    return await finish_authorization(pool, attempt_id, result) or await stored_response(pool, merchant.id, key)


async def _fail_over(pool: asyncpg.Pool, payment_id: UUID, previous_attempt_id: UUID, gateway: str) -> UUID:
    """Close the attempt the last gateway did not process and open one on the next gateway."""
    async with pool.acquire() as conn, conn.transaction():
        # Payment first, then attempts: the global lock order.
        moved = await conn.fetchval(
            "UPDATE payments SET gateway = $2, updated_at = now() WHERE id = $1 AND status = 'processing' RETURNING id",
            payment_id, gateway,
        )
        if moved is None:
            raise RuntimeError(f"Payment {payment_id} left processing during failover")
        await conn.execute(
            """
            UPDATE payment_attempts SET status = 'failed', decline_code = 'gateway_unavailable', updated_at = now()
            WHERE id = $1 AND status = 'pending'
            """,
            previous_attempt_id,
        )
        return await conn.fetchval(
            "INSERT INTO payment_attempts (payment_id, gateway, status) VALUES ($1, $2, 'pending') RETURNING id",
            payment_id, gateway,
        )


async def finish_authorization(pool: asyncpg.Pool, attempt_id: UUID, result: GatewayResult) -> StoredResponse | None:
    """Record a definite authorization outcome. None if it was already recorded."""
    if result.outcome == "approved":
        status, reason = result.status, None
    elif result.outcome == "declined":
        status, reason = "failed", result.decline_code
    elif result.outcome == "not_processed":
        status, reason = "failed", "gateway_unavailable"
    elif result.outcome == "not_found":
        status, reason = "failed", "gateway_no_record"
    else:
        raise ValueError(f"cannot finish an authorization on outcome {result.outcome}")

    async with pool.acquire() as conn, conn.transaction():
        payment = await conn.fetchrow(
            """
            UPDATE payments
            SET status = $2,
                amount_captured = CASE WHEN $2 = 'captured' THEN amount_minor ELSE 0 END,
                failure_reason = $3, gateway_reference = $4, updated_at = now(),
                -- Captures and refunds must go where the authorization is.
                gateway = (SELECT gateway FROM payment_attempts WHERE id = $1)
            WHERE id = (SELECT payment_id FROM payment_attempts WHERE id = $1) AND status = 'processing'
            RETURNING *
            """,
            attempt_id, status, reason, result.gateway_reference,
        )
        if payment is None:
            return None
        await conn.execute(
            """
            UPDATE payment_attempts
            SET status = $2, gateway_reference = $3, decline_code = $4, updated_at = now()
            WHERE id = $1
            """,
            attempt_id, ATTEMPT_STATUS[result.outcome], result.gateway_reference,
            result.decline_code or (reason if result.outcome != "approved" else None),
        )
        if status == "captured":
            await post_capture(conn, payment, payment["amount_captured"])
        return await idempotency.complete(
            conn, merchant_id=payment["merchant_id"], key=payment["idempotency_key"], status_code=201,
            body=payment_body(payment),
        )


# Capture and void


async def capture_payment(
    pool: asyncpg.Pool, gateways: dict[str, Connector], merchant: Merchant, payment_id: UUID, key: str,
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
            SET status = 'capture_pending', pending_capture_minor = COALESCE($3, amount_minor),
                pending_op_key = $4, updated_at = now()
            WHERE id = $1 AND merchant_id = $2 AND status = 'authorized'
              AND COALESCE($3, amount_minor) <= amount_minor
            RETURNING *
            """,
            payment_id, merchant.id, amount_minor, key,
        )
        if payment is None:
            raise await _refusal(conn, merchant, payment_id, amount_minor)

    result = await send_capture(gateways[payment["gateway"]], payment)
    if result.outcome == "unknown":
        return in_flight(payment_body(payment))
    return await finish_capture(pool, payment_id, result) or await stored_response(pool, merchant.id, key)


def send_capture(connector: Connector, payment):
    return connector.capture(
        gateway_reference=payment["gateway_reference"], reference=f"capture:{payment['id']}",
        amount_minor=payment["pending_capture_minor"], currency=payment["currency"],
    )


async def finish_capture(pool: asyncpg.Pool, payment_id: UUID, result: GatewayResult) -> StoredResponse | None:
    async with pool.acquire() as conn, conn.transaction():
        payment = await conn.fetchrow(
            "SELECT * FROM payments WHERE id = $1 AND status = 'capture_pending' FOR NO KEY UPDATE", payment_id
        )
        if payment is None:
            return None
        merchant_id, key = payment["merchant_id"], payment["pending_op_key"]
        if result.outcome == "approved":
            payment = await conn.fetchrow(
                """
                UPDATE payments
                SET status = 'captured', amount_captured = pending_capture_minor,
                    pending_capture_minor = NULL, pending_op_key = NULL, updated_at = now()
                WHERE id = $1
                RETURNING *
                """,
                payment_id,
            )
            await post_capture(conn, payment, payment["amount_captured"])
            return await idempotency.complete(
                conn, merchant_id=merchant_id, key=key, status_code=200, body=payment_body(payment)
            )
        await conn.execute(
            """
            UPDATE payments
            SET status = 'authorized', pending_capture_minor = NULL, pending_op_key = NULL, updated_at = now()
            WHERE id = $1
            """,
            payment_id,
        )
        return await _finish_refused(conn, merchant_id, key, result)


async def void_payment(
    pool: asyncpg.Pool, gateways: dict[str, Connector], merchant: Merchant, payment_id: UUID, key: str
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
            UPDATE payments SET status = 'void_pending', pending_op_key = $3, updated_at = now()
            WHERE id = $1 AND merchant_id = $2 AND status = 'authorized'
            RETURNING *
            """,
            payment_id, merchant.id, key,
        )
        if payment is None:
            raise await _refusal(conn, merchant, payment_id)

    result = await send_void(gateways[payment["gateway"]], payment)
    if result.outcome == "unknown":
        return in_flight(payment_body(payment))
    return await finish_void(pool, payment_id, result) or await stored_response(pool, merchant.id, key)


def send_void(connector: Connector, payment):
    return connector.void(gateway_reference=payment["gateway_reference"], reference=f"void:{payment['id']}")


async def finish_void(pool: asyncpg.Pool, payment_id: UUID, result: GatewayResult) -> StoredResponse | None:
    async with pool.acquire() as conn, conn.transaction():
        payment = await conn.fetchrow(
            "SELECT * FROM payments WHERE id = $1 AND status = 'void_pending' FOR NO KEY UPDATE", payment_id
        )
        if payment is None:
            return None
        merchant_id, key = payment["merchant_id"], payment["pending_op_key"]
        payment = await conn.fetchrow(
            """
            UPDATE payments SET status = $2, pending_op_key = NULL, updated_at = now()
            WHERE id = $1 RETURNING *
            """,
            payment_id, "voided" if result.outcome == "approved" else "authorized",
        )
        if result.outcome == "approved":
            return await idempotency.complete(
                conn, merchant_id=merchant_id, key=key, status_code=200, body=payment_body(payment)
            )
        return await _finish_refused(conn, merchant_id, key, result)


async def _finish_refused(conn, merchant_id: UUID, key: str, result: GatewayResult) -> StoredResponse:
    """A capture or void the gateway did not do. The payment is already back in authorized."""
    if result.outcome == "not_processed":
        await release_key(conn, merchant_id, key)
        error = GatewayUnavailable("The gateway did not process this request. Retry with the same key.")
        return StoredResponse(error.status_code, error.body(), replayed=False)
    return await idempotency.complete(
        conn, merchant_id=merchant_id, key=key, status_code=402,
        body=ProcessorDeclined(result.decline_code).body(),
    )


# Refunds


async def refund_payment(
    pool: asyncpg.Pool, gateways: dict[str, Connector], merchant: Merchant, payment_id: UUID, key: str,
    amount_minor: int,
) -> StoredResponse:
    async with pool.acquire() as conn, conn.transaction():
        stored = await idempotency.claim(
            conn, merchant_id=merchant.id, key=key, method="POST", path=f"/payments/{payment_id}/refunds",
            fingerprint={"amount_minor": amount_minor},
        )
        if stored is not None:
            return stored
        # Refunds of one payment queue on this lock, so each one sees the
        # reservations of the ones before it.
        payment = await conn.fetchrow(
            "SELECT * FROM payments WHERE id = $1 AND merchant_id = $2 FOR NO KEY UPDATE", payment_id, merchant.id
        )
        if payment is None:
            raise NotFound("Payment not found")
        if payment["status"] != "captured":
            raise InvalidState(f"Payment is {payment['status']}")
        pending = await conn.fetchval(
            "SELECT COALESCE(SUM(amount_minor), 0) FROM refunds WHERE payment_id = $1 AND status = 'pending'",
            payment_id,
        )
        available = payment["amount_captured"] - payment["amount_refunded"] - pending
        if amount_minor > available:
            raise RefundExceedsAvailable(f"Only {available} can be refunded right now")
        refund = await conn.fetchrow(
            """
            INSERT INTO refunds (payment_id, amount_minor, status, idempotency_key)
            VALUES ($1, $2, 'pending', $3) RETURNING *
            """,
            payment_id, amount_minor, key,
        )

    result = await send_refund(gateways[payment["gateway"]], payment, refund)
    if result.outcome == "unknown":
        return in_flight(refund_body(refund))
    return await finish_refund(pool, refund["id"], result) or await stored_response(pool, merchant.id, key)


def send_refund(connector: Connector, payment, refund):
    # The refund id is our reference, so re-sending it can never refund twice.
    return connector.refund(
        gateway_reference=payment["gateway_reference"], reference=str(refund["id"]),
        amount_minor=refund["amount_minor"], currency=payment["currency"],
    )


async def finish_refund(pool: asyncpg.Pool, refund_id: UUID, result: GatewayResult) -> StoredResponse | None:
    async with pool.acquire() as conn, conn.transaction():
        # Payment first, then the refund, then accounts: the global lock order.
        payment = await conn.fetchrow(
            "SELECT * FROM payments WHERE id = (SELECT payment_id FROM refunds WHERE id = $1) FOR NO KEY UPDATE",
            refund_id,
        )
        refund = await conn.fetchrow(
            "SELECT * FROM refunds WHERE id = $1 AND status = 'pending' FOR NO KEY UPDATE", refund_id
        )
        if refund is None:
            return None
        merchant_id, key = payment["merchant_id"], refund["idempotency_key"]
        if result.outcome == "approved":
            refund = await conn.fetchrow(
                "UPDATE refunds SET status = 'succeeded', updated_at = now() WHERE id = $1 RETURNING *", refund_id
            )
            await conn.execute(
                """
                UPDATE payments
                SET amount_refunded = amount_refunded + $2,
                    status = CASE WHEN amount_refunded + $2 = amount_captured THEN 'refunded' ELSE status END,
                    updated_at = now()
                WHERE id = $1
                """,
                payment["id"], refund["amount_minor"],
            )
            await _post(
                conn, f"payment-refund:{refund_id}", merchant_owner(merchant_id), CLEARING,
                refund["amount_minor"], payment["currency"],
            )
            return await idempotency.complete(
                conn, merchant_id=merchant_id, key=key, status_code=201, body=refund_body(refund)
            )
        # A failed refund stops counting as pending, which frees its amount.
        reason = "gateway_unavailable" if result.outcome == "not_processed" else result.decline_code
        refund = await conn.fetchrow(
            """
            UPDATE refunds SET status = 'failed', failure_reason = $2, updated_at = now()
            WHERE id = $1 RETURNING *
            """,
            refund_id, reason,
        )
        if result.outcome == "not_processed":
            await release_key(conn, merchant_id, key)
            error = GatewayUnavailable("The gateway did not process this refund. Retry with the same key.")
            return StoredResponse(error.status_code, error.body(), replayed=False)
        return await idempotency.complete(
            conn, merchant_id=merchant_id, key=key, status_code=201, body=refund_body(refund)
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
