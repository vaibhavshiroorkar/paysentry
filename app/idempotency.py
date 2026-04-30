import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from fastapi import Header
from fastapi.responses import JSONResponse

from app.errors import IdempotencyKeyReused, MissingIdempotencyKey, RequestInProgress


@dataclass(frozen=True)
class StoredResponse:
    status_code: int
    body: dict
    replayed: bool


def request_hash(fields: dict) -> str:
    # Sorted keys and fixed separators, so the same request always gives the same hash.
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def idempotency_key(key: str | None = Header(default=None, alias="Idempotency-Key")) -> str:
    if key is None or not 1 <= len(key) <= 255:
        raise MissingIdempotencyKey("Send an Idempotency-Key header of 1 to 255 characters")
    return key


def to_response(result) -> JSONResponse:
    """Turn a StoredResponse or ledger PostingResult into an HTTP response."""
    headers = {"Idempotent-Replayed": "true"} if result.replayed else {}
    return JSONResponse(status_code=result.status_code, content=result.body, headers=headers)


async def claim(
    conn, *, merchant_id: UUID, key: str, method: str, path: str, fingerprint: dict
) -> StoredResponse | None:
    """Claim a merchant's idempotency key inside the caller's transaction.

    Returns None when this request now owns the key. Returns the stored
    response when the same request already completed. Raises when the key is
    in use by a different request, or by this one still in flight.
    """
    digest = request_hash({"method": method, "path": path, "body": fingerprint})
    # As in the ledger: a concurrent uncommitted claim of the same key makes
    # this INSERT wait until that transaction ends.
    claimed = await conn.fetchval(
        """
        INSERT INTO idempotency_keys (merchant_id, key, request_hash, state)
        VALUES ($1, $2, $3, 'in_progress')
        ON CONFLICT (merchant_id, key) DO NOTHING
        RETURNING true
        """,
        merchant_id, key, digest,
    )
    if claimed:
        return None

    stored = await conn.fetchrow(
        """
        SELECT request_hash, state, response_code, response_body
        FROM idempotency_keys WHERE merchant_id = $1 AND key = $2
        """,
        merchant_id, key,
    )
    # Check the request first, so a different body is always an error.
    if stored["request_hash"] != digest:
        raise IdempotencyKeyReused("This Idempotency-Key was already used with a different request")
    if stored["state"] == "in_progress":
        raise RequestInProgress("A request with this Idempotency-Key is still being processed")
    return StoredResponse(stored["response_code"], stored["response_body"], replayed=True)


async def complete(conn, *, merchant_id: UUID, key: str, status_code: int, body: dict) -> StoredResponse:
    await conn.execute(
        """
        UPDATE idempotency_keys
        SET state = 'completed', response_code = $3, response_body = $4, completed_at = now()
        WHERE merchant_id = $1 AND key = $2
        """,
        merchant_id, key, status_code, body,
    )
    return StoredResponse(status_code, body, replayed=False)
