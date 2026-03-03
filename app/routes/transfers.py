from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app import ledger
from app.errors import NotFound
from app.idempotency import idempotency_key
from app.models import TransferCreate

router = APIRouter(prefix="/transfers", tags=["transfers"])


def posting_response(result: ledger.PostingResult) -> JSONResponse:
    headers = {"Idempotent-Replayed": "true"} if result.replayed else {}
    return JSONResponse(status_code=result.status_code, content=result.body, headers=headers)


@router.post("")
async def create_transfer(
    body: TransferCreate,
    request: Request,
    key: str = Depends(idempotency_key),
) -> JSONResponse:
    state = request.app.state
    # Before touching Postgres, so a flood of requests is turned away cheaply.
    await state.limiter.check(body.from_account_id)
    result = await ledger.post_transfer(
        state.pool,
        idempotency_key=key,
        from_account_id=body.from_account_id,
        to_account_id=body.to_account_id,
        amount_minor=body.amount_minor,
        currency=body.currency,
    )
    return posting_response(result)


@router.get("/{transfer_id}")
async def get_transfer(transfer_id: UUID, request: Request) -> dict:
    row = await request.app.state.pool.fetchrow("SELECT * FROM transfers WHERE id = $1", transfer_id)
    if row is None:
        raise NotFound("Transfer not found")
    return ledger.transfer_body(row)
