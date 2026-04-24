from fastapi import APIRouter, Depends, Request

from app.auth import Merchant, current_merchant, hash_api_key, merchant_owner, new_api_key, require_admin
from app.models import MerchantCreate

router = APIRouter(tags=["merchants"])


@router.post("/merchants", status_code=201, dependencies=[Depends(require_admin)])
async def create_merchant(body: MerchantCreate, request: Request) -> dict:
    api_key = new_api_key()
    async with request.app.state.pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """
            INSERT INTO merchants (name, api_key_hash, api_key_prefix) VALUES ($1, $2, $3)
            RETURNING id, name, api_key_prefix, created_at
            """,
            body.name, hash_api_key(api_key), api_key[:11],
        )
        await conn.executemany(
            "INSERT INTO accounts (owner_id, currency) VALUES ($1, $2)",
            [(merchant_owner(row["id"]), currency) for currency in ("INR", "USD")],
        )
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "api_key": api_key,
        "api_key_prefix": row["api_key_prefix"],
        "created_at": row["created_at"].isoformat(),
    }


@router.get("/balance")
async def balance(request: Request, merchant: Merchant = Depends(current_merchant)) -> dict:
    rows = await request.app.state.pool.fetch(
        "SELECT currency, balance_minor FROM accounts WHERE owner_id = $1 ORDER BY currency",
        merchant_owner(merchant.id),
    )
    return {"balances": [{"currency": r["currency"], "balance_minor": r["balance_minor"]} for r in rows]}
