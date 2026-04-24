import hashlib
import hmac
import secrets
from dataclasses import dataclass
from uuid import UUID

from fastapi import Header, Request

from app.errors import Unauthorized


@dataclass(frozen=True)
class Merchant:
    id: UUID
    name: str


def new_api_key() -> str:
    return "sk_" + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    # The key is 32 random bytes, so a fast hash is enough. Slow hashes like
    # bcrypt exist to protect guessable passwords, which this is not.
    return hashlib.sha256(key.encode()).hexdigest()


def merchant_owner(merchant_id: UUID) -> str:
    return f"merchant:{merchant_id}"


async def require_admin(request: Request, key: str | None = Header(default=None, alias="X-Admin-Key")) -> None:
    expected = request.app.state.settings.admin_api_key
    if key is None or not hmac.compare_digest(key.encode(), expected.encode()):
        raise Unauthorized("Send a valid X-Admin-Key header")


async def current_merchant(
    request: Request, authorization: str | None = Header(default=None)
) -> Merchant:
    if authorization is None or not authorization.startswith("Bearer "):
        raise Unauthorized("Send Authorization: Bearer <secret key>")
    row = await request.app.state.pool.fetchrow(
        "SELECT id, name FROM merchants WHERE api_key_hash = $1",
        hash_api_key(authorization.removeprefix("Bearer ")),
    )
    if row is None:
        raise Unauthorized("Unknown API key")
    return Merchant(id=row["id"], name=row["name"])
