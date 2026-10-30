"""Fixed synthetic payment walkthrough, isolated by an opaque visitor session."""

import datetime as dt
import hashlib
import secrets
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from app import payments
from app.auth import Merchant, hash_api_key, merchant_owner, new_api_key
from app.errors import ApiError, ServiceUnavailable, Unauthorized
from app.models import PaymentCreate


router = APIRouter(prefix="/demo", tags=["demo"])
COOKIE = "paysentry_demo"
SESSION_LOCK = 727_002


class EmptyAction(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DemoForbidden(ApiError):
    status_code = 403
    code = "forbidden"


async def same_origin(request: Request) -> None:
    # A hosting proxy terminates TLS, so in production the app itself may see plain HTTP.
    scheme = "https" if request.app.state.settings.environment == "production" else request.url.scheme
    try:
        origin = urlsplit(request.headers.get("Origin", ""))
    except ValueError:
        origin = None
    if origin is None or origin.scheme != scheme or origin.netloc != request.url.netloc:
        raise DemoForbidden("Open the demo on this site before performing an action")


async def session_row(request: Request):
    token = request.cookies.get(COOKIE)
    if not token or len(token) > 128:
        return None
    state = request.app.state
    return await state.pool.fetchrow(
        """
        SELECT s.*, m.name FROM demo_sessions s JOIN merchants m ON m.id = s.merchant_id
        WHERE token_hash = $1 AND expires_at > now()
          AND (NOT $2::boolean OR s.created_at >= $3)
        """,
        hash_api_key(token), state.settings.demo_embedded_gateways, state.demo_boot_at,
    )


def client_address(request: Request) -> str:
    hops = request.app.state.settings.trusted_proxy_hops
    if hops:
        forwarded = [
            part.strip() for value in request.headers.getlist("X-Forwarded-For") for part in value.split(",")
        ]
        forwarded = [part for part in forwarded if part]
        if len(forwarded) >= hops:
            return forwarded[-hops]
    return request.client.host if request.client else "unknown"


async def demo_merchant(request: Request) -> Merchant:
    row = await session_row(request)
    if row is None:
        raise Unauthorized("This demo session ended. Start a new session to continue.")
    return Merchant(id=row["merchant_id"], name=row["name"])


@router.post("/session", dependencies=[Depends(same_origin)])
async def start_session(request: Request, body: EmptyAction | None = None):
    existing = await session_row(request)
    if existing is not None:
        return {"expires_at": existing["expires_at"].isoformat()}
    state = request.app.state
    address = client_address(request)
    await state.demo_session_limiter.check("demo-session:" + hashlib.sha256(address.encode()).hexdigest())
    token = secrets.token_urlsafe(32)
    async with state.pool.acquire() as conn, conn.transaction():
        # Serialize the count and insert so concurrent visitors cannot exceed the cap.
        await conn.execute("SELECT pg_advisory_xact_lock($1)", SESSION_LOCK)
        count = await conn.fetchval("SELECT count(*) FROM demo_sessions WHERE created_at >= date_trunc('day', now())")
        if count >= state.settings.demo_session_daily_limit:
            raise ServiceUnavailable("Today's demo session limit has been reached. Please try again tomorrow.")
        key = new_api_key()
        merchant_id = await conn.fetchval(
            "INSERT INTO merchants (name, api_key_hash, api_key_prefix) VALUES ($1, $2, $3) RETURNING id",
            "Demo merchant", hash_api_key(key), key[:11],
        )
        await conn.executemany(
            "INSERT INTO accounts (owner_id, currency) VALUES ($1, $2)",
            [(merchant_owner(merchant_id), currency) for currency in ("INR", "USD")],
        )
        expires_at = await conn.fetchval(
            "INSERT INTO demo_sessions (token_hash, merchant_id) VALUES ($1, $2) RETURNING expires_at",
            hash_api_key(token), merchant_id,
        )
    response = JSONResponse(status_code=201, content={"expires_at": expires_at.isoformat()})
    response.set_cookie(
        COOKIE, token, max_age=3600, path="/demo", httponly=True, samesite="strict",
        secure=state.settings.environment == "production",
    )
    return response


@router.delete("/session", dependencies=[Depends(same_origin)])
async def end_session(request: Request):
    """End the visitor's session so the walkthrough can start again.

    The demo payment's request key is fixed per merchant, so a fresh run needs
    a fresh session rather than a second payment in this one.
    """
    token = request.cookies.get(COOKIE)
    if token and len(token) <= 128:
        await request.app.state.pool.execute(
            "UPDATE demo_sessions SET expires_at = now() WHERE token_hash = $1 AND expires_at > now()",
            hash_api_key(token),
        )
    response = JSONResponse(status_code=200, content={"ended": True})
    response.delete_cookie(
        COOKIE, path="/demo", httponly=True, samesite="strict",
        secure=request.app.state.settings.environment == "production",
    )
    return response


async def snapshot(pool, merchant: Merchant) -> dict:
    owner = merchant_owner(merchant.id)
    async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        accounts = await conn.fetch("SELECT * FROM accounts WHERE owner_id = $1", owner)
        ids = [account["id"] for account in accounts]
        payment = await conn.fetchrow(
            "SELECT * FROM payments WHERE merchant_id = $1 ORDER BY created_at DESC LIMIT 1", merchant.id,
        )
        refunds = await conn.fetch("SELECT * FROM refunds WHERE payment_id = $1 ORDER BY created_at, id", payment["id"]) if payment else []
        transfers = await conn.fetch(
            "SELECT * FROM transfers WHERE from_account_id = ANY($1::uuid[]) OR to_account_id = ANY($1::uuid[]) ORDER BY created_at, id",
            ids,
        )
        entries = await conn.fetch(
            """
            SELECT e.*, a.owner_id, a.currency FROM ledger_entries e
            JOIN accounts a ON a.id = e.account_id
            WHERE transfer_id = ANY($1::uuid[]) ORDER BY e.id
            """,
            [transfer["id"] for transfer in transfers],
        )
    balances_match = all(
        account["balance_minor"] == sum(e["amount_minor"] for e in entries if e["account_id"] == account["id"])
        for account in accounts
    )
    transfers_balance = True
    for transfer in transfers:
        sides = [e for e in entries if e["transfer_id"] == transfer["id"]]
        if transfer["status"] == "SUCCEEDED":
            transfers_balance &= len(sides) == 2 and sorted(
                (str(e["account_id"]), e["amount_minor"]) for e in sides
            ) == sorted([
                (str(transfer["from_account_id"]), -transfer["amount_minor"]),
                (str(transfer["to_account_id"]), transfer["amount_minor"]),
            ])
        else:
            transfers_balance &= not sides
    successful = {t["idempotency_key"]: t["amount_minor"] for t in transfers if t["status"] == "SUCCEEDED"}
    payments_match = True
    if payment:
        payments_match = (
            payment["amount_captured"] == successful.get(f"payment-capture:{payment['id']}", 0)
            and payment["amount_refunded"] == sum(r["amount_minor"] for r in refunds if r["status"] == "succeeded")
            and payment["amount_refunded"] == sum(successful.get(f"payment-refund:{r['id']}", 0) for r in refunds)
        )
    return {
        "payment": payments.payment_body(payment, refunds) if payment else None,
        "balance_minor": next((a["balance_minor"] for a in accounts if a["currency"] == "INR"), 0),
        "ledger": [
            {
                "id": e["id"], "transfer_id": str(e["transfer_id"]), "amount_minor": e["amount_minor"],
                "account": "Your merchant" if e["owner_id"] == owner else "Gateway clearing",
                "currency": e["currency"], "created_at": e["created_at"].isoformat(),
            }
            for e in entries
        ],
        "checks": {"balances_match": balances_match, "transfers_balance": bool(transfers_balance), "payments_match": payments_match},
    }


@router.get("/state")
async def state(request: Request, merchant: Merchant = Depends(demo_merchant)):
    await request.app.state.demo_limiter.check("demo:" + str(merchant.id))
    return await snapshot(request.app.state.pool, merchant)


async def operation_response(request: Request, merchant: Merchant, result):
    return JSONResponse(
        status_code=result.status_code,
        headers={"Idempotent-Replayed": "true"} if result.replayed else {},
        content={"result": result.body, "replayed": result.replayed, "snapshot": await snapshot(request.app.state.pool, merchant)},
    )


@router.post("/payments", dependencies=[Depends(same_origin)])
async def create_payment(request: Request, body: EmptyAction | None = None, merchant: Merchant = Depends(demo_merchant)):
    state = request.app.state
    await state.demo_limiter.check("demo:" + str(merchant.id))
    card_year = dt.date.today().year + 2
    result = await payments.create_payment(
        state.pool, state.gateways, state.router, state.settings.routing_max_attempts, merchant, "demo-payment",
        PaymentCreate(amount_minor=50_000, currency="INR", capture_method="manual", card={
            "number": "4242424242424242", "exp_month": 12, "exp_year": card_year, "cvc": "123",
        }),
    )
    return await operation_response(request, merchant, result)


@router.post("/payments/{payment_id}/capture", dependencies=[Depends(same_origin)])
async def capture_payment(payment_id: UUID, request: Request, body: EmptyAction | None = None, merchant: Merchant = Depends(demo_merchant)):
    state = request.app.state
    await state.demo_limiter.check("demo:" + str(merchant.id))
    result = await payments.capture_payment(state.pool, state.gateways, merchant, payment_id, "demo-capture", 40_000)
    return await operation_response(request, merchant, result)


@router.post("/payments/{payment_id}/refund", dependencies=[Depends(same_origin)])
async def refund_payment(payment_id: UUID, request: Request, body: EmptyAction | None = None, merchant: Merchant = Depends(demo_merchant)):
    state = request.app.state
    await state.demo_limiter.check("demo:" + str(merchant.id))
    result = await payments.refund_payment(state.pool, state.gateways, merchant, payment_id, "demo-refund", 15_000)
    return await operation_response(request, merchant, result)
