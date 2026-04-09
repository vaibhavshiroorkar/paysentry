import hashlib
import json

from fastapi import Header

from app.errors import MissingIdempotencyKey


def request_hash(fields: dict) -> str:
    # Sorted keys and fixed separators, so the same request always gives the same hash.
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def idempotency_key(key: str | None = Header(default=None, alias="Idempotency-Key")) -> str:
    if key is None or not 1 <= len(key) <= 255:
        raise MissingIdempotencyKey("Send an Idempotency-Key header of 1 to 255 characters")
    return key
