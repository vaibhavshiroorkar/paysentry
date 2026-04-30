from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from app import ledger
from app.auth import require_admin
from app.errors import NotFound, SystemAccountNotAllowed
from app.idempotency import idempotency_key, to_response
from app.models import AccountCreate, TopUpCreate

router = APIRouter(prefix="/accounts", tags=["accounts"], dependencies=[Depends(require_admin)])


def account_body(row) -> dict:
    return {
        "id": str(row["id"]),
        "owner_id": row["owner_id"],
        "currency": row["currency"],
        "balance_minor": row["balance_minor"],
        "created_at": row["created_at"].isoformat(),
    }


async def _get_account(pool, account_id: UUID):
    row = await pool.fetchrow(
        "SELECT id, owner_id, currency, balance_minor, created_at FROM accounts WHERE id = $1", account_id
    )
    if row is None:
        raise NotFound("Account not found")
    return row


@router.post("", status_code=201)
async def create_account(body: AccountCreate, request: Request) -> dict:
    row = await request.app.state.pool.fetchrow(
        """
        INSERT INTO accounts (owner_id, currency) VALUES ($1, $2)
        RETURNING id, owner_id, currency, balance_minor, created_at
        """,
        body.owner_id, body.currency,
    )
    return account_body(row)


@router.get("/{account_id}")
async def get_account(account_id: UUID, request: Request) -> dict:
    return account_body(await _get_account(request.app.state.pool, account_id))


@router.get("/{account_id}/entries")
async def list_entries(account_id: UUID, request: Request, limit: int = Query(50, ge=1, le=200)) -> dict:
    pool = request.app.state.pool
    await _get_account(pool, account_id)
    rows = await pool.fetch(
        """
        SELECT id, transfer_id, amount_minor, created_at FROM ledger_entries
        WHERE account_id = $1 ORDER BY id DESC LIMIT $2
        """,
        account_id, limit,
    )
    entries = [
        {
            "id": row["id"],
            "transfer_id": str(row["transfer_id"]),
            "amount_minor": row["amount_minor"],
            "created_at": row["created_at"].isoformat(),
        }
        for row in rows
    ]
    return {"entries": entries}


@router.post("/{account_id}/top-ups")
async def top_up(
    account_id: UUID,
    body: TopUpCreate,
    request: Request,
    key: str = Depends(idempotency_key),
) -> JSONResponse:
    state = request.app.state
    account = await _get_account(state.pool, account_id)
    if account["owner_id"].startswith("system:"):
        raise SystemAccountNotAllowed("System accounts cannot be topped up")
    await state.limiter.check(account_id)
    result = await ledger.post_transfer(
        state.pool,
        idempotency_key=key,
        from_account_id=await ledger.account_id_for(state.pool, "system:funding", account["currency"]),
        to_account_id=account_id,
        amount_minor=body.amount_minor,
        currency=account["currency"],
        allow_system_source=True,
    )
    return to_response(result)
