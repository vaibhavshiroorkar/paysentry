from dataclasses import dataclass
from uuid import UUID

import asyncpg

from app.errors import CurrencyMismatch, IdempotencyKeyReused, NotFound, SystemAccountNotAllowed
from app.idempotency import request_hash


@dataclass(frozen=True)
class PostingResult:
    status_code: int
    body: dict
    replayed: bool


def transfer_body(row) -> dict:
    return {
        "id": str(row["id"]),
        "from_account_id": str(row["from_account_id"]),
        "to_account_id": str(row["to_account_id"]),
        "amount_minor": row["amount_minor"],
        "currency": row["currency"],
        "status": row["status"],
        "failure_reason": row["failure_reason"],
        "created_at": row["created_at"].isoformat(),
    }


async def post_transfer(
    pool: asyncpg.Pool,
    *,
    idempotency_key: str,
    from_account_id: UUID,
    to_account_id: UUID,
    amount_minor: int,
    currency: str,
    allow_system_source: bool = False,
) -> PostingResult:
    """Move money between two accounts exactly once per idempotency key.

    Everything happens in one transaction. Request errors (unknown account,
    wrong currency) roll back and leave the key unused. Business outcomes
    (success, insufficient funds) commit and are replayed on retry.
    """
    async with pool.acquire() as conn, conn.transaction():
        return await post_in_transaction(
            conn,
            idempotency_key=idempotency_key,
            from_account_id=from_account_id,
            to_account_id=to_account_id,
            amount_minor=amount_minor,
            currency=currency,
            allow_system_source=allow_system_source,
        )


async def post_in_transaction(
    conn: asyncpg.Connection,
    *,
    idempotency_key: str,
    from_account_id: UUID,
    to_account_id: UUID,
    amount_minor: int,
    currency: str,
    allow_system_source: bool = False,
) -> PostingResult:
    """The body of post_transfer, for callers that already hold a transaction.

    Payments use it so that a state change and its money movement commit
    together. The caller must not hold locks on account rows, so that
    accounts are always locked last and in id order.
    """
    fingerprint = request_hash(
        {
            "from_account_id": from_account_id,
            "to_account_id": to_account_id,
            "amount_minor": amount_minor,
            "currency": currency,
        }
    )

    # Claim the key. If another transaction inserted the same key and has
    # not committed yet, Postgres blocks here until it does. So a duplicate
    # never runs the posting logic, even when both arrive at once.
    try:
        transfer = await conn.fetchrow(
            """
            INSERT INTO transfers (idempotency_key, request_hash, from_account_id,
                                   to_account_id, amount_minor, currency, status)
            VALUES ($1, $2, $3, $4, $5, $6, 'PROCESSING')
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING *
            """,
            idempotency_key, fingerprint, from_account_id, to_account_id, amount_minor, currency,
        )
    except asyncpg.ForeignKeyViolationError:
        raise NotFound("Account not found") from None

    if transfer is None:
        return await _replay(conn, idempotency_key, fingerprint)

    # Lock both accounts in id order. Two transfers A->B and B->A then wait
    # on the same first row instead of each holding one and waiting for the
    # other. FOR NO KEY UPDATE, not FOR UPDATE: the foreign key checks on
    # the INSERT above already took KEY SHARE locks on these rows, and
    # FOR UPDATE conflicts with KEY SHARE held by a concurrent transfer.
    rows = await conn.fetch(
        """
        SELECT id, currency, balance_minor, allow_negative
        FROM accounts
        WHERE id = ANY($1::uuid[])
        ORDER BY id
        FOR NO KEY UPDATE
        """,
        [from_account_id, to_account_id],
    )
    accounts = {row["id"]: row for row in rows}
    payer = accounts[from_account_id]

    if any(row["currency"] != currency for row in rows):
        raise CurrencyMismatch(f"Both accounts must hold {currency}")
    if payer["allow_negative"] and not allow_system_source:
        raise SystemAccountNotAllowed("Transfers cannot draw from a system account")

    if not payer["allow_negative"] and payer["balance_minor"] < amount_minor:
        return await _finish(conn, transfer, "FAILED", 422, failure_reason="insufficient_funds")

    await conn.executemany(
        "UPDATE accounts SET balance_minor = balance_minor + $2 WHERE id = $1",
        [(from_account_id, -amount_minor), (to_account_id, amount_minor)],
    )
    await conn.executemany(
        "INSERT INTO ledger_entries (transfer_id, account_id, amount_minor) VALUES ($1, $2, $3)",
        [(transfer["id"], from_account_id, -amount_minor), (transfer["id"], to_account_id, amount_minor)],
    )
    return await _finish(conn, transfer, "SUCCEEDED", 201)


async def _finish(conn, transfer, status: str, status_code: int, failure_reason: str | None = None) -> PostingResult:
    body = transfer_body({**transfer, "status": status, "failure_reason": failure_reason})
    await conn.execute(
        """
        UPDATE transfers
        SET status = $2, failure_reason = $3, response_code = $4, response_body = $5
        WHERE id = $1
        """,
        transfer["id"], status, failure_reason, status_code, body,
    )
    return PostingResult(status_code, body, replayed=False)


async def _replay(conn, idempotency_key: str, fingerprint: str) -> PostingResult:
    stored = await conn.fetchrow(
        "SELECT request_hash, response_code, response_body FROM transfers WHERE idempotency_key = $1",
        idempotency_key,
    )
    if stored["request_hash"] != fingerprint:
        raise IdempotencyKeyReused("This Idempotency-Key was already used with a different request body")
    return PostingResult(stored["response_code"], stored["response_body"], replayed=True)


async def account_id_for(conn, owner_id: str, currency: str) -> UUID:
    """The single account a system or merchant owner holds in a currency."""
    return await conn.fetchval(
        "SELECT id FROM accounts WHERE owner_id = $1 AND currency = $2", owner_id, currency
    )
